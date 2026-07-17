"""
Citation selection pass.

Sits between retrieval and synthesis. Given a large retrieved corpus,
uses a fast/cheap Claude call to filter down to the papers that most
directly establish the gene's cancer role.

This separates "retrieve broadly" (recall) from "synthesise narrowly"
(precision) without inflating the synthesis context window.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from src.config import settings
from src.models.schema import LiteratureRecord
from src.pipeline.citation_precision import citation_support_score
from src.pipeline.llm_client import complete_with_tool

logger = logging.getLogger(__name__)

_SELECT_TOOL: dict = {
    "name": "select_papers",
    "description": (
        "Return the PMIDs of the abstracts that most directly establish "
        "or refute this gene's role in cancer. Ordered by relevance, most direct first."
    ),
    "input_schema": {
        "type": "object",
        "required": ["selected_pmids"],
        "properties": {
            "selected_pmids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "PMIDs to keep, ordered by relevance descending.",
            }
        },
    },
}

_SELECT_SYSTEM = """\
You are a cancer genomics literature curator. Given a list of PubMed abstracts retrieved for a gene,
select the subset that most directly establishes or refutes the gene's role in cancer.

Prefer abstracts that:
- Directly demonstrate an oncogenic or tumor-suppressive mechanism (functional assays, knockouts,
  overexpression models, recurrent somatic mutations in patient cohorts)
- Provide clinical evidence linking the gene to cancer survival, treatment response, or incidence
- Are focused primarily on this gene (not papers where it appears in a large gene list)
- Match the provided HGNC identity for this gene, including full name and locus type

Deprioritize abstracts that:
- Mention the gene only in passing or as one of dozens of hits in a multi-gene screen
- Are prognostic signature studies with no mechanistic follow-up on this gene specifically
- Focus on non-cancer biology with only tangential cancer relevance
- Use the same symbol for a different entity, such as an lncRNA/circRNA/transcript name
  that does not match the provided HGNC identity
- Duplicate the finding of another selected abstract

Return up to the requested maximum. If no papers are truly directly relevant, return an empty
list — do not pad with loosely related papers.
"""


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


def _record_selection_score(
    gene: str,
    record: LiteratureRecord,
    gene_identity: Optional[str] = None,
) -> int:
    return citation_support_score(gene, record, gene_identity)


def prefilter_records_for_selection(
    gene: str,
    records: List[LiteratureRecord],
    limit: int,
    gene_identity: Optional[str] = None,
) -> List[LiteratureRecord]:
    """Deterministically rank candidate records before any LLM selection call."""
    if limit <= 0 or len(records) <= limit:
        return records

    scored = [
        (_record_selection_score(gene, record, gene_identity), -index, record)
        for index, record in enumerate(records)
    ]
    scored.sort(reverse=True)
    return [record for _, _, record in scored[:limit]]


def strongest_selection_score(
    gene: str,
    records: List[LiteratureRecord],
    gene_identity: Optional[str] = None,
) -> int:
    if not records:
        return 0
    return max(_record_selection_score(gene, record, gene_identity) for record in records)


def conservative_token_mode_enabled() -> bool:
    return settings.token_budget_mode.strip().lower() == "conservative"


def _structured_record_context(
    gene: str,
    record: LiteratureRecord,
    gene_identity: Optional[str] = None,
) -> str:
    score = _record_selection_score(gene, record, gene_identity)
    abstract_sentences = _sentences(record.abstract)
    lower_gene = gene.lower()
    evidence_terms = (
        "cancer",
        "tumor",
        "tumour",
        "carcinoma",
        "oncogene",
        "fusion",
        "mutation",
        "knockdown",
        "overexpression",
        "xenograft",
        "survival",
        "proliferation",
    )
    evidence_sentences = [
        sentence
        for sentence in abstract_sentences
        if lower_gene in sentence.lower()
        or any(term in sentence.lower() for term in evidence_terms)
    ][:3]
    if not evidence_sentences:
        evidence_sentences = abstract_sentences[:2]
    evidence_text = " ".join(evidence_sentences)[:900]
    return (
        f"PMID {record.pmid}\n"
        f"Title: {record.title}\n"
        f"Deterministic relevance score: {score}\n"
        f"Evidence-bearing abstract context: {evidence_text}"
    )


async def _select_from_records(
    gene: str,
    records: List[LiteratureRecord],
    max_papers: int,
    gene_identity: Optional[str],
    local_mode: bool,
    local_backend: Optional[str],
    stage_label: str,
) -> Optional[List[str]]:
    abstracts_text = "\n\n".join(
        _structured_record_context(gene, record, gene_identity)
        for record in records
    )
    prompt = (
        f"Gene: {gene}\n"
        f"Gene identity: {gene_identity or 'canonical symbol only'}\n"
        f"Selection stage: {stage_label}\n"
        f"Select up to {max_papers} of the following {len(records)} candidate abstracts "
        f"that most directly establish or refute {gene}'s role in cancer.\n\n"
        f"{abstracts_text}"
    )

    result = await complete_with_tool(
        model=settings.selection_model,
        system=_SELECT_SYSTEM,
        user=prompt,
        tool=_SELECT_TOOL,
        max_tokens=512,
        local_mode=local_mode,
        local_backend=local_backend,
    )
    return [
        pmid
        for pmid in dict.fromkeys(result.get("selected_pmids", []))
        if isinstance(pmid, str)
    ][:max_papers]


async def select_papers_for_synthesis(
    gene: str,
    records: List[LiteratureRecord],
    max_papers: int,
    gene_identity: Optional[str] = None,
    local_mode: bool = False,
    local_backend: Optional[str] = None,
) -> List[LiteratureRecord]:
    """
    Filter retrieved records to the most directly cancer-relevant subset.

    If records <= max_papers, returns all (no API call needed).
    Falls back to deterministic prefilter output on any API or parse failure.
    """
    if len(records) <= max_papers:
        return records

    candidate_records = prefilter_records_for_selection(
        gene,
        records,
        settings.selection_prefilter_limit,
        gene_identity,
    )
    if len(candidate_records) <= max_papers:
        return candidate_records
    if conservative_token_mode_enabled():
        logger.info(
            "Conservative token mode selected deterministic top %d/%d papers for %s",
            max_papers,
            len(candidate_records),
            gene,
        )
        return candidate_records[:max_papers]

    try:
        chunk_size = max(1, settings.selection_chunk_size)
        chunk_keep = max(1, settings.selection_chunk_keep)
        chunk_selected_pmids: list[str] = []

        for chunk_index in range(0, len(candidate_records), chunk_size):
            chunk = candidate_records[chunk_index : chunk_index + chunk_size]
            selected = await _select_from_records(
                gene=gene,
                records=chunk,
                max_papers=chunk_keep,
                gene_identity=gene_identity,
                local_mode=local_mode,
                local_backend=local_backend,
                stage_label=f"chunk {chunk_index // chunk_size + 1}",
            )
            chunk_selected_pmids.extend(selected or [])

        if not chunk_selected_pmids:
            logger.info("Selection pass returned no relevant PMIDs for %s", gene)
            return []

        records_by_pmid = {r.pmid: r for r in candidate_records}
        semifinalists = [
            records_by_pmid[pmid]
            for pmid in dict.fromkeys(chunk_selected_pmids)
            if pmid in records_by_pmid
        ]
        if not semifinalists:
            return candidate_records[:max_papers]

        final_pmids = await _select_from_records(
            gene=gene,
            records=semifinalists,
            max_papers=max_papers,
            gene_identity=gene_identity,
            local_mode=local_mode,
            local_backend=local_backend,
            stage_label="final merge",
        )
        selected_pmids = final_pmids or [record.pmid for record in semifinalists[:max_papers]]
        selected = [records_by_pmid[pmid] for pmid in selected_pmids if pmid in records_by_pmid]
        logger.info(
            "Selection pass for %s: %d retrieved → %d prefiltered → %d selected",
            gene,
            len(records),
            len(candidate_records),
            len(selected),
        )
        return selected if selected else candidate_records[:max_papers]

    except Exception as exc:
        logger.warning(
            "Selection pass failed for %s (%s) — using deterministic top %d",
            gene,
            exc,
            max_papers,
        )
        return candidate_records[:max_papers]
