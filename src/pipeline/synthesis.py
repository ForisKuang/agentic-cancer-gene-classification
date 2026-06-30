"""
Retrieval-grounded LLM synthesis via Claude.
Reads retrieved literature + deterministic facts and fills the annotation schema.
Enforces three invariants:
  1. Every summary claim must cite a retrieved abstract.
  2. Every emitted PMID must exist in the retrieved set (verified post-response).
  3. "insufficient_evidence" is treated as a valid first-class output.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Set

import anthropic

from src.config import settings
from src.models.schema import GeneAnnotation, LiteratureRecord

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

SYSTEM_PROMPT = """\
You are a cancer genomics expert filling structured annotation rows for the OncoKB MSK TARGET Gene Triaging database.

You will receive:
1. A gene name and its associated fusion partners.
2. Deterministic facts from authoritative databases (OncoKB membership, prevalence).
3. Retrieved PubMed abstracts (each with its PMID).

Your task is to call the `annotate_gene` tool with a structured annotation.

## Hard constraints — never violate these:
- Every claim in `gene_summary` must be directly traceable to one of the retrieved abstracts.
- `citations` must ONLY contain PMIDs that appear in the provided retrieved abstracts list.
- Do NOT invent, guess, or recall PMIDs from memory. If a fact cannot be grounded in the retrieved set, omit it.
- A fabricated PMID will cause patient safety errors. Treat citation fabrication as the most critical failure mode.
- If the retrieved evidence is insufficient to make a determination, set `insufficient_evidence: true` and leave classification fields null. This is a valid, preferred output over hallucination.
- If `cancer_associated` is false OR `insufficient_evidence` is true, you MUST leave `cancer_associated_gene_tier` and `og_or_tsg` null. Do not fill these fields for non-cancer genes or genes with insufficient evidence.

## Field guidance:
- `cancer_associated`: true if there is credible peer-reviewed evidence linking this gene to cancer biology.
- `cancer_association_rationale`: list the evidence types (structural-variant, expression, mutation, methylation, copy-number) with a brief justification.
- `cancer_associated_gene_tier`: ONLY set this when `cancer_associated` is true. Use the most conservative tier supported by the evidence — do not promote a gene's tier beyond what the retrieved abstracts directly demonstrate:
    - "Class I - Driver": high bar — requires recurrent somatic mutations with direct functional validation (e.g., murine models, CRISPR knockouts demonstrating tumour initiation), OR recurrent oncogenic fusions with demonstrated transforming activity. Expression/correlation data alone does NOT qualify.
    - "Class II - Likely Driver": expression upregulation, copy-number alteration, or functional knockdown/overexpression in cancer cell lines or xenografts with a plausible mechanistic hypothesis. No requirement for in vivo murine tumour initiation models.
    - "Class III - Cancer Relevant": indirect or contextual association only — prognostic signature membership, immune microenvironment role, metabolic co-dependency, or single-study correlation without mechanistic follow-up. When in doubt between Class II and Class III, choose Class III.
- `og_or_tsg`: ONLY set this when `cancer_associated` is true AND `cancer_associated_gene_tier` is "Class I - Driver" or "Class II - Likely Driver". Leave null for "Class III - Cancer Relevant" genes — contextual or indirect associations do not warrant a directional OG/TSG call. "OG" (promotes growth/survival), "TSG" (suppresses growth), "OG, TSG" (context-dependent dual role with evidence for both in the retrieved abstracts).
- `gene_class`: molecular/functional class (e.g., "Serine/threonine kinase", "RNA-binding protein", "Transcription factor").
- `signaling_pathways`: comma-separated canonical pathways (e.g., "PI3K/AKT", "RAS/MAPK", "WNT/β-catenin").
- `confidence`: 0.0–1.0 reflecting how well the retrieved evidence supports the annotation.
  - >4 papers with direct functional evidence → 0.8–1.0
  - 2–4 papers with functional/expression data → 0.5–0.8
  - <2 papers or only indirect evidence → 0.2–0.5
  - 0 papers → set insufficient_evidence: true, confidence: 0.0

## Retrieval provenance:
The context will tell you which retrieval tier sourced the literature:
- **Tier 1** (direct NCBI structured query): well-characterised gene with abundant indexed literature.
- **Tier 2** (Claude agentic retrieval): sparse initial results; Claude searched iteratively using aliases,
  fusion-specific terms, and pathway names to surface relevant evidence.
End the `gene_summary` with one parenthetical sentence noting the retrieval tier, for example:
  "(Literature sourced via Tier 1 direct PubMed query.)" or
  "(Literature sourced via Tier 2 Claude agentic retrieval — sparse initial results required expanded search.)"
"""

ANNOTATE_TOOL: anthropic.types.ToolParam = {
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
            "cancer_associated_gene_tier": {
                "type": "string",
                "enum": ["Class I - Driver", "Class II - Likely Driver", "Class III - Cancer Relevant"],
                "description": "Driver tier based on strength of functional evidence.",
            },
            "og_or_tsg": {
                "type": "string",
                "enum": ["OG", "TSG", "OG, TSG"],
                "description": "Oncogene, tumor suppressor, or context-dependent dual role.",
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
                    "List of PMIDs supporting this annotation. "
                    "MUST be a subset of the retrieved abstracts provided. No extras."
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
        f"- In OncoKB: {'Yes' if in_oncokb else ('No' if in_oncokb is False else 'Unknown (token not configured)')}",
        f"- Cancer-type prevalence (MSK/GENIE): {cancer_type_prevalence or 'not available'}",
        "",
        f"### Retrieved PubMed abstracts ({len(records)} papers):",
    ]

    if not records:
        lines.append("No abstracts retrieved. Set insufficient_evidence: true.")
    else:
        for rec in records:
            lines += [
                f"---",
                f"PMID: {rec.pmid}",
                f"Title: {rec.title}",
                f"Abstract: {rec.abstract}",
            ]

    return "\n".join(lines)


def _verify_citations(
    citations: List[str],
    retrieved_pmids: Set[str],
) -> List[str]:
    """
    Remove any PMID from the LLM's citation list that was not in the retrieved set.
    An identifier that was not retrieved is a rejection, not a warning.
    """
    verified = [pmid for pmid in citations if pmid in retrieved_pmids]
    rejected = set(citations) - retrieved_pmids
    if rejected:
        logger.warning(
            "Rejected %d unverified PMIDs from LLM output: %s",
            len(rejected),
            rejected,
        )
    return verified


async def synthesize_gene_annotation(
    gene: str,
    fusions: List[str],
    in_oncokb: Optional[bool],
    cancer_type_prevalence: Optional[str],
    records: List[LiteratureRecord],
    retrieval_tier: int = 1,
) -> Dict:
    """
    Call Claude to produce a structured annotation. Returns raw tool-use input dict.
    Raises on API error.
    """
    user_prompt = _build_user_prompt(gene, fusions, in_oncokb, cancer_type_prevalence, records, retrieval_tier)
    retrieved_pmids: Set[str] = {r.pmid for r in records}

    response = await _client.messages.create(
        model=settings.synthesis_model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # prompt caching
            }
        ],
        tools=[ANNOTATE_TOOL],
        tool_choice={"type": "tool", "name": "annotate_gene"},
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Extract tool_use block
    tool_input: dict = {}
    for block in response.content:
        if block.type == "tool_use" and block.name == "annotate_gene":
            tool_input = block.input
            break

    if not tool_input:
        logger.error("No tool_use block returned by model for gene %s", gene)
        return {"insufficient_evidence": True, "cancer_associated": None, "confidence": 0.0}

    # PMID verification — reject any citation not in retrieved set
    if "citations" in tool_input:
        tool_input["citations"] = _verify_citations(tool_input["citations"], retrieved_pmids)

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
    return GeneAnnotation(
        gene=gene,
        fusions=list(dict.fromkeys(fusions)),  # deduplicate, preserve order
        in_oncokb=in_oncokb,
        cancer_associated=synthesis_result.get("cancer_associated"),
        cancer_association_rationale=synthesis_result.get("cancer_association_rationale"),
        cancer_associated_gene_tier=synthesis_result.get("cancer_associated_gene_tier"),
        og_or_tsg=synthesis_result.get("og_or_tsg"),
        cancer_type_prevalence=cancer_type_prevalence,
        gene_class=synthesis_result.get("gene_class"),
        signaling_pathways=synthesis_result.get("signaling_pathways"),
        gene_summary=synthesis_result.get("gene_summary"),
        citations=synthesis_result.get("citations", []),
        retrieval_count=len(records),
        insufficient_evidence=synthesis_result.get("insufficient_evidence", False),
        confidence=synthesis_result.get("confidence", 0.0),
    )
