"""
Retrieval-grounded LLM synthesis via Claude.
Reads retrieved literature + deterministic facts and fills the annotation schema.
Enforces three invariants:
  1. Every summary claim must cite a retrieved abstract.
  2. Every emitted PMID must exist in the retrieved set (verified post-response).
  3. "insufficient_evidence" is treated as a valid first-class output.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from src.config import settings
from src.models.schema import GeneAnnotation, LiteratureRecord
from src.pipeline.citation_precision import filter_and_rank_citations
from src.pipeline.literature import _HIGH_IMPACT_JOURNALS
from src.pipeline.llm_client import complete_with_tool

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a cancer genomics expert filling structured annotation rows for the OncoKB MSK TARGET Gene Triaging database.

You will receive:
1. A gene name and its associated fusion partners.
2. Deterministic facts from authoritative databases (HGNC identity, OncoKB membership, prevalence).
3. Retrieved PubMed abstracts (each with its PMID).

Your task is to call the `annotate_gene` tool with a structured annotation.

## Hard constraints — never violate these:
- Every claim in `gene_summary` must be directly traceable to one of the retrieved abstracts.
- `citations` must ONLY contain the strongest PMIDs that appear in the provided retrieved abstracts list.
- Do NOT invent, guess, or recall PMIDs from memory. If a fact cannot be grounded in the retrieved set, omit it.
- A fabricated PMID will cause patient safety errors. Treat citation fabrication as the most critical failure mode.
- Prefer citation precision over citation volume. Do not cite loosely related background papers just because they were retrieved.
- Use the HGNC identity to avoid same-symbol ambiguity. Do not cite papers that use the same symbol
  for a different entity, such as an lncRNA/circRNA/transcript name unrelated to the HGNC gene.
- If the retrieved evidence is insufficient to make a determination, set `insufficient_evidence: true` and leave classification fields null. This is a valid, preferred output over hallucination.

## Field guidance:
- `cancer_associated`: true if there is credible peer-reviewed evidence linking this gene to cancer biology.
- `cancer_association_rationale`: list the evidence types (structural-variant, expression, mutation, methylation, copy-number) with a brief justification.
- `cancer_type_prevalence`: cancer types and alteration contexts observed for this gene (e.g., "Lung adenocarcinoma (fusion), breast cancer (amplification)"). The deterministic facts above provide MSK/GENIE prevalence when available — use that value unchanged. When it is "not available", infer from the retrieved literature: list the cancer types explicitly mentioned in the abstracts along with the alteration type.
- `gene_class`: molecular/functional class (e.g., "Serine/threonine kinase", "RNA-binding protein", "Transcription factor").
- `signaling_pathways`: comma-separated associated signaling pathways (e.g., "PI3K/AKT", "RAS/MAPK", "WNT/β-catenin").
- `confidence`: 0.0–1.0 reflecting how well the retrieved evidence supports the annotation.
  - >4 papers with direct functional evidence → 0.8–1.0
  - 2–4 papers with functional/expression data → 0.5–0.8
  - <2 papers or only indirect evidence → 0.2–0.5
  - 0 papers → set insufficient_evidence: true, confidence: 0.0

## Literature quality signals:
- Abstracts marked ★ are from high-impact journals (NEJM, Lancet, Nature, Cell, JCO, Cancer Cell, etc.).
  Weight these more heavily when the evidence they provide is directly relevant to the gene's cancer role.

## Retrieval provenance:
The context will tell you which retrieval tier sourced the literature:
- **Tier 1** (direct NCBI structured query): well-characterised gene with abundant indexed literature.
- **Tier 2** (Claude agentic retrieval): sparse initial results; Claude searched iteratively using aliases,
  fusion-specific terms, and pathway names to surface relevant evidence.
End the `gene_summary` with one parenthetical sentence noting the retrieval tier, for example:
  "(Literature sourced via Tier 1 direct PubMed query.)" or
  "(Literature sourced via Tier 2 Claude agentic retrieval — sparse initial results required expanded search.)"
"""

ANNOTATE_TOOL: dict = {
    "name": "annotate_gene",
    "description": (
        "Produce a structured cancer gene annotation grounded in the retrieved literature. "
        "Only cite PMIDs explicitly provided in the context."
    ),
    "input_schema": {
        "type": "object",
        "required": ["cancer_associated", "insufficient_evidence", "confidence"],
        "properties": {
            "cancer_associated": {
                "type": "boolean",
                "description": "Whether this gene has credible evidence of cancer association.",
            },
            "insufficient_evidence": {
                "type": "boolean",
                "description": (
                    "True when the retrieved literature is too sparse to make a confident determination. "
                    "Prefer this over a low-confidence guess."
                ),
            },
            "cancer_association_rationale": {
                "type": "string",
                "description": (
                    "Brief rationale covering evidence types observed "
                    "(structural-variant, expression, mutation, methylation, copy-number) "
                    "and which cancer types."
                ),
            },
            "cancer_type_prevalence": {
                "type": "string",
                "description": (
                    "Cancer types and alteration contexts for this gene. "
                    "Use the MSK/GENIE value from deterministic facts if available; "
                    "otherwise infer from retrieved literature."
                ),
            },
            "gene_class": {
                "type": "string",
                "description": "Molecular/functional class of the gene product.",
            },
            "signaling_pathways": {
                "type": "string",
                "description": "Comma-separated associated signaling pathways.",
            },
            "gene_summary": {
                "type": "string",
                "description": (
                    "2–5 sentence prose summary of cancer relevance grounded in retrieved abstracts. "
                    "Cite PMIDs inline as (PMID XXXXXXXX). Only cite retrieved PMIDs."
                ),
            },
            "citations": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    f"List of up to {settings.max_citations_per_annotation} strongest PMIDs "
                    "supporting this annotation. MUST be a subset of the retrieved abstracts provided. "
                    "No extras."
                ),
            },
            "supporting_quotes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["pmid", "quote"],
                    "properties": {
                        "pmid": {
                            "type": "string",
                            "description": "PMID of the source abstract.",
                        },
                        "quote": {
                            "type": "string",
                            "description": (
                                "1–2 sentence verbatim or near-verbatim passage from the abstract "
                                "that directly supports the cancer_association_rationale. "
                                "Must be traceable to the retrieved text — do not paraphrase or invent."
                            ),
                        },
                    },
                },
                "description": (
                    "1–3 direct quotes from retrieved abstracts grounding the rationale. "
                    "Only include quotes from PMIDs that appear in the citations list. "
                    "Omit entirely if insufficient_evidence is true."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence score 0–1 reflecting evidence quality and quantity.",
            },
        },
    },
}


def _build_user_prompt(
    gene: str,
    fusions: List[str],
    in_oncokb: Optional[bool],
    cancer_type_prevalence: Optional[str],
    records: List[LiteratureRecord],
    retrieval_tier: int,
    gene_identity: Optional[str] = None,
) -> str:
    tier_label = (
        "Tier 1 (direct NCBI structured query — abundant indexed literature)"
        if retrieval_tier == 1
        else "Tier 2 (Claude agentic retrieval — sparse initial results required expanded search)"
    )
    lines = [
        f"## Gene: {gene}",
        f"Associated fusions: {', '.join(fusions) if fusions else 'none'}",
        f"Retrieval tier: {tier_label}",
        "",
        "### Deterministic database facts (do not contradict or regenerate):",
        f"- HGNC identity: {gene_identity or 'not available'}",
        f"- In OncoKB: {'Yes' if in_oncokb else ('No' if in_oncokb is False else 'Unknown (token not configured)')}",
        f"- Cancer-type prevalence (MSK/GENIE): {cancer_type_prevalence or 'not available'}",
        "",
        f"### Retrieved PubMed abstracts ({len(records)} papers):",
    ]

    if not records:
        lines.append("No abstracts retrieved. Set insufficient_evidence: true.")
    else:
        for rec in records:
            journal_tag = f" [{rec.journal}]" if rec.journal else ""
            impact_tag = " ★" if rec.journal in _HIGH_IMPACT_JOURNALS else ""
            lines += [
                "---",
                f"PMID: {rec.pmid}{journal_tag}{impact_tag}",
                f"Title: {rec.title}",
                f"Abstract: {rec.abstract}",
            ]

    return "\n".join(lines)


def _verify_citations(
    gene: str,
    citations: List[str],
    records: List[LiteratureRecord],
    max_citations: int,
    gene_identity: Optional[str] = None,
) -> List[str]:
    """
    Remove ambiguous or unretrieved PMIDs from the LLM's citation list, then rank.
    An identifier that was not retrieved is a rejection, not a warning.
    """
    retrieved_pmids = {record.pmid for record in records}
    verified = filter_and_rank_citations(
        gene=gene,
        emitted_citations=citations,
        records=records,
        max_citations=max_citations,
        gene_identity=gene_identity,
        min_score=-99,
    )
    rejected = set(citations) - retrieved_pmids
    if rejected:
        logger.warning(
            "Rejected %d unverified PMIDs from LLM output: %s",
            len(rejected),
            rejected,
        )
    if len(citations) > len(verified):
        logger.info(
            "Kept %d/%d emitted citations after PMID verification, identity filtering, and precision cap",
            len(verified),
            len(citations),
        )
    return verified


async def synthesize_gene_annotation(
    gene: str,
    fusions: List[str],
    in_oncokb: Optional[bool],
    cancer_type_prevalence: Optional[str],
    records: List[LiteratureRecord],
    retrieval_tier: int = 1,
    gene_identity: Optional[str] = None,
    local_mode: bool = False,
    local_backend: Optional[str] = None,
) -> Dict:
    """
    Call Claude to produce a structured annotation. Returns raw tool-use input dict.
    Raises on API error.
    """
    user_prompt = _build_user_prompt(
        gene,
        fusions,
        in_oncokb,
        cancer_type_prevalence,
        records,
        retrieval_tier,
        gene_identity,
    )
    tool_input = await complete_with_tool(
        model=settings.synthesis_model,
        system=SYSTEM_PROMPT,
        user=user_prompt,
        tool=ANNOTATE_TOOL,
        max_tokens=2048,
        local_mode=local_mode,
        local_backend=local_backend,
        model_purpose="synthesis",
    )

    if not tool_input:
        logger.error("No annotation returned for gene %s", gene)
        return {"insufficient_evidence": True, "cancer_associated": None, "confidence": 0.0}

    # PMID verification — reject any citation not in retrieved set
    if "citations" in tool_input:
        tool_input["citations"] = _verify_citations(
            gene,
            tool_input["citations"],
            records,
            settings.max_citations_per_annotation,
            gene_identity,
        )

    return tool_input


def build_gene_annotation(
    gene: str,
    fusions: List[str],
    in_oncokb: Optional[bool],
    cancer_type_prevalence: Optional[str],
    records: List[LiteratureRecord],
    synthesis_result: Dict,
) -> GeneAnnotation:
    """Merge synthesis output with deterministic facts into a GeneAnnotation."""
    verified_pmids = set(synthesis_result.get("citations", []))
    raw_quotes = synthesis_result.get("supporting_quotes", []) or []
    # Only keep quotes whose PMIDs survived citation verification
    verified_quotes = [
        q for q in raw_quotes
        if isinstance(q, dict) and q.get("pmid") in verified_pmids
    ]
    # Use deterministic DB prevalence when available; fall back to LLM inference from literature
    effective_prevalence = cancer_type_prevalence or synthesis_result.get("cancer_type_prevalence")
    return GeneAnnotation(
        gene=gene,
        fusions=list(dict.fromkeys(fusions)),  # deduplicate, preserve order
        in_oncokb=in_oncokb,
        cancer_associated=synthesis_result.get("cancer_associated"),
        cancer_association_rationale=synthesis_result.get("cancer_association_rationale"),
        cancer_type_prevalence=effective_prevalence,
        gene_class=synthesis_result.get("gene_class"),
        signaling_pathways=synthesis_result.get("signaling_pathways"),
        gene_summary=synthesis_result.get("gene_summary"),
        citations=synthesis_result.get("citations", []),
        supporting_quotes=verified_quotes,
        retrieval_count=len(records),
        retrieved_pmids=[r.pmid for r in records],
        insufficient_evidence=synthesis_result.get("insufficient_evidence", False),
        confidence=synthesis_result.get("confidence", 0.0),
    )
