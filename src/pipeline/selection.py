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
from typing import List, Optional

from src.config import settings
from src.models.schema import LiteratureRecord
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
    Falls back to records[:max_papers] on any API or parse failure.
    """
    if len(records) <= max_papers:
        return records

    abstracts_text = "\n\n".join(
        f"PMID {r.pmid}\nTitle: {r.title}\nAbstract: {r.abstract[:400]}"
        for r in records
    )
    prompt = (
        f"Gene: {gene}\n"
        f"Gene identity: {gene_identity or 'canonical symbol only'}\n"
        f"Select up to {max_papers} of the following {len(records)} abstracts "
        f"that most directly establish {gene}'s role in cancer.\n\n"
        f"{abstracts_text}"
    )

    try:
        result = await complete_with_tool(
            model=settings.selection_model,
            system=_SELECT_SYSTEM,
            user=prompt,
            tool=_SELECT_TOOL,
            max_tokens=512,
            local_mode=local_mode,
            local_backend=local_backend,
            model_purpose="selection",
        )

        selected_pmids = [
            pmid
            for pmid in dict.fromkeys(result.get("selected_pmids", []))
            if isinstance(pmid, str)
        ][:max_papers]

        if not selected_pmids:
            logger.info("Selection pass returned no relevant PMIDs for %s", gene)
            return []

        records_by_pmid = {r.pmid: r for r in records}
        selected = [records_by_pmid[pmid] for pmid in selected_pmids if pmid in records_by_pmid]
        logger.info("Selection pass for %s: %d → %d papers", gene, len(records), len(selected))
        return selected if selected else records[:max_papers]

    except Exception as exc:
        logger.warning("Selection pass failed for %s (%s) — using top %d", gene, exc, max_papers)
        return records[:max_papers]
