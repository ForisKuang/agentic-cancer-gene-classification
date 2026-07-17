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
from src.pipeline.llm_cache import read_cached_json, text_digest, write_cached_json
from src.pipeline.llm_client import complete_with_tool

logger = logging.getLogger(__name__)

EVIDENCE_CACHE_VERSION = "paper-evidence-v1"
SYNTHESIS_CACHE_VERSION = "gene-synthesis-v1"

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
                    f"List of up to {settings.max_citations_per_annotation} strongest PMIDs "
                    "supporting this annotation. MUST be a subset of the retrieved abstracts provided. "
                    "No extras."
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

EXTRACT_EVIDENCE_TOOL: dict = {
    "name": "extract_paper_evidence",
    "description": (
        "Extract compact cancer-gene evidence from one PubMed abstract. "
        "Only summarize facts present in this abstract."
    ),
    "input_schema": {
        "type": "object",
        "required": ["relevance", "key_findings"],
        "properties": {
            "relevance": {
                "type": "string",
                "enum": ["direct", "indirect", "none", "ambiguous"],
                "description": "How directly this paper supports cancer relevance for the HGNC gene.",
            },
            "evidence_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Evidence types present, such as mutation, fusion, expression, functional assay, clinical association.",
            },
            "key_findings": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 4 concise findings from the abstract relevant to cancer annotation.",
            },
            "cancer_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Cancer types mentioned in the abstract.",
            },
            "caveats": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Reasons to downweight the paper, including broad screen, review article, symbol ambiguity, or indirect evidence.",
            },
        },
    },
}

EXTRACT_EVIDENCE_BATCH_TOOL: dict = {
    "name": "extract_paper_evidence_batch",
    "description": (
        "Extract compact cancer-gene evidence from multiple PubMed abstracts. "
        "Return one evidence object per PMID and only summarize facts present in each abstract."
    ),
    "input_schema": {
        "type": "object",
        "required": ["papers"],
        "properties": {
            "papers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["pmid", "relevance", "key_findings"],
                    "properties": {
                        "pmid": {
                            "type": "string",
                            "description": "PMID for the abstract being summarized.",
                        },
                        "relevance": {
                            "type": "string",
                            "enum": ["direct", "indirect", "none", "ambiguous"],
                            "description": "How directly this paper supports cancer relevance for the HGNC gene.",
                        },
                        "evidence_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Evidence types present, such as mutation, fusion, expression, functional assay, clinical association.",
                        },
                        "key_findings": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Up to 4 concise findings from the abstract relevant to cancer annotation.",
                        },
                        "cancer_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Cancer types mentioned in the abstract.",
                        },
                        "caveats": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Reasons to downweight the paper, including broad screen, review article, symbol ambiguity, or indirect evidence.",
                        },
                    },
                },
                "description": "One compact evidence packet per PMID.",
            }
        },
    },
}

_EVIDENCE_SYSTEM = """\
You extract concise evidence from one PubMed abstract for cancer gene curation.

Read the full abstract and return only information stated in the abstract. Do not infer beyond it.
Use the HGNC identity to identify same-symbol ambiguity. If the abstract is about a different entity
or only mentions the gene in passing, mark relevance as "ambiguous", "indirect", or "none".
"""


def _paper_evidence_cache_key(
    gene: str,
    record: LiteratureRecord,
    gene_identity: Optional[str],
    local_mode: bool,
    local_backend: Optional[str],
) -> Dict:
    return {
        "version": EVIDENCE_CACHE_VERSION,
        "gene": gene.upper(),
        "gene_identity": gene_identity or "",
        "pmid": record.pmid,
        "title_sha256": text_digest(record.title),
        "abstract_sha256": text_digest(record.abstract),
        "model": settings.selection_model,
        "backend": local_backend if local_mode else "anthropic-sdk",
    }


async def _extract_paper_evidence(
    gene: str,
    record: LiteratureRecord,
    gene_identity: Optional[str],
    local_mode: bool,
    local_backend: Optional[str],
) -> Dict:
    cache_key = _paper_evidence_cache_key(
        gene,
        record,
        gene_identity,
        local_mode,
        local_backend,
    )
    cached = read_cached_json("paper-evidence", cache_key)
    if cached is not None:
        logger.info("Evidence cache hit for %s PMID %s", gene, record.pmid)
        return cached

    prompt = (
        f"Gene: {gene}\n"
        f"Gene identity: {gene_identity or 'canonical symbol only'}\n"
        f"PMID: {record.pmid}\n"
        f"Title: {record.title}\n"
        f"Abstract:\n{record.abstract}"
    )
    evidence = await complete_with_tool(
        model=settings.selection_model,
        system=_EVIDENCE_SYSTEM,
        user=prompt,
        tool=EXTRACT_EVIDENCE_TOOL,
        max_tokens=512,
        local_mode=local_mode,
        local_backend=local_backend,
    )
    write_cached_json("paper-evidence", cache_key, evidence)
    return evidence


async def _extract_paper_evidence_batch(
    gene: str,
    records: List[LiteratureRecord],
    gene_identity: Optional[str],
    local_mode: bool,
    local_backend: Optional[str],
) -> Dict[str, Dict]:
    prompt_blocks = [
        f"Gene: {gene}",
        f"Gene identity: {gene_identity or 'canonical symbol only'}",
        "",
        "Extract evidence separately for each PubMed abstract below. Do not mix facts across PMIDs.",
    ]
    for index, record in enumerate(records, start=1):
        prompt_blocks += [
            "",
            f"## Paper {index}",
            f"PMID: {record.pmid}",
            f"Title: {record.title}",
            f"Abstract:\n{record.abstract}",
        ]

    result = await complete_with_tool(
        model=settings.selection_model,
        system=_EVIDENCE_SYSTEM,
        user="\n".join(prompt_blocks),
        tool=EXTRACT_EVIDENCE_BATCH_TOOL,
        max_tokens=min(2048, 512 + (256 * len(records))),
        local_mode=local_mode,
        local_backend=local_backend,
    )
    papers = result.get("papers", [])
    if not isinstance(papers, list):
        return {}

    evidence_by_pmid: Dict[str, Dict] = {}
    expected_pmids = {record.pmid for record in records}
    for item in papers:
        if not isinstance(item, dict):
            continue
        pmid = str(item.get("pmid", ""))
        if pmid in expected_pmids:
            evidence_by_pmid[pmid] = item
    return evidence_by_pmid


def _fallback_evidence_packet(record: LiteratureRecord) -> Dict:
    return {
        "relevance": "ambiguous",
        "evidence_types": [],
        "key_findings": [record.abstract[: settings.synthesis_evidence_max_chars]],
        "cancer_types": [],
        "caveats": ["Evidence extraction failed; compact abstract context shown."],
    }


def _evidence_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


async def _extract_evidence_packets(
    gene: str,
    records: List[LiteratureRecord],
    gene_identity: Optional[str],
    local_mode: bool,
    local_backend: Optional[str],
) -> List[Dict]:
    batch_size = max(1, settings.evidence_extraction_batch_size)
    if batch_size == 1:
        packets = []
        for record in records:
            try:
                evidence = await _extract_paper_evidence(
                    gene,
                    record,
                    gene_identity,
                    local_mode,
                    local_backend,
                )
            except Exception as exc:
                logger.warning("Evidence extraction failed for %s PMID %s: %s", gene, record.pmid, exc)
                evidence = _fallback_evidence_packet(record)
            packets.append({"record": record, "evidence": evidence})
        return packets

    evidence_by_pmid: Dict[str, Dict] = {}
    missing_records: List[LiteratureRecord] = []
    for record in records:
        cache_key = _paper_evidence_cache_key(
            gene,
            record,
            gene_identity,
            local_mode,
            local_backend,
        )
        cached = read_cached_json("paper-evidence", cache_key)
        if cached is not None:
            logger.info("Evidence cache hit for %s PMID %s", gene, record.pmid)
            evidence_by_pmid[record.pmid] = cached
        else:
            missing_records.append(record)

    for start in range(0, len(missing_records), batch_size):
        batch = missing_records[start : start + batch_size]
        try:
            extracted = await _extract_paper_evidence_batch(
                gene,
                batch,
                gene_identity,
                local_mode,
                local_backend,
            )
            logger.info(
                "Batch evidence extraction for %s returned %d/%d papers",
                gene,
                len(extracted),
                len(batch),
            )
        except Exception as exc:
            logger.warning("Batch evidence extraction failed for %s: %s", gene, exc)
            extracted = {}

        for record in batch:
            evidence = extracted.get(record.pmid) or _fallback_evidence_packet(record)
            evidence_by_pmid[record.pmid] = evidence
            write_cached_json(
                "paper-evidence",
                _paper_evidence_cache_key(
                    gene,
                    record,
                    gene_identity,
                    local_mode,
                    local_backend,
                ),
                evidence,
            )

    return [
        {"record": record, "evidence": evidence_by_pmid.get(record.pmid) or _fallback_evidence_packet(record)}
        for record in records
    ]


def _record_cache_payload(record: LiteratureRecord) -> Dict:
    return {
        "pmid": record.pmid,
        "title_sha256": text_digest(record.title),
        "abstract_sha256": text_digest(record.abstract),
    }


def _synthesis_cache_key(
    gene: str,
    fusions: List[str],
    in_oncokb: Optional[bool],
    cancer_type_prevalence: Optional[str],
    records: List[LiteratureRecord],
    retrieval_tier: int,
    gene_identity: Optional[str],
    local_mode: bool,
    local_backend: Optional[str],
) -> Dict:
    return {
        "version": SYNTHESIS_CACHE_VERSION,
        "gene": gene.upper(),
        "fusions": sorted(dict.fromkeys(fusions)),
        "in_oncokb": in_oncokb,
        "cancer_type_prevalence": cancer_type_prevalence or "",
        "retrieval_tier": retrieval_tier,
        "gene_identity": gene_identity or "",
        "records": [_record_cache_payload(record) for record in records],
        "selection_model": settings.selection_model,
        "synthesis_model": settings.synthesis_model,
        "max_citations": settings.max_citations_per_annotation,
        "backend": local_backend if local_mode else "anthropic-sdk",
    }


def _build_user_prompt(
    gene: str,
    fusions: List[str],
    in_oncokb: Optional[bool],
    cancer_type_prevalence: Optional[str],
    records: List[LiteratureRecord],
    retrieval_tier: int,
    gene_identity: Optional[str] = None,
    evidence_packets: Optional[List[Dict]] = None,
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
        f"### Retrieved PubMed evidence packets ({len(records)} papers):",
        "Each packet was extracted from the full PubMed abstract for that PMID.",
    ]

    if not records:
        lines.append("No abstracts retrieved. Set insufficient_evidence: true.")
    else:
        packets_by_pmid = {
            packet["record"].pmid: packet["evidence"]
            for packet in evidence_packets or []
            if packet.get("record")
        }
        for rec in records:
            evidence = packets_by_pmid.get(rec.pmid) or _fallback_evidence_packet(rec)
            lines += [
                "---",
                f"PMID: {rec.pmid}",
                f"Title: {rec.title}",
                f"Relevance: {evidence.get('relevance', 'ambiguous')}",
                f"Evidence types: {', '.join(_evidence_list(evidence.get('evidence_types'))) or 'none'}",
                f"Cancer types: {', '.join(_evidence_list(evidence.get('cancer_types'))) or 'none'}",
                "Key findings:",
                *[
                    f"- {finding}"
                    for finding in _evidence_list(evidence.get("key_findings"))[:4]
                ],
                "Caveats:",
                *[
                    f"- {caveat}"
                    for caveat in _evidence_list(evidence.get("caveats"))[:3]
                ],
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


def build_no_selected_evidence_result(gene: str, retrieval_tier: int) -> Dict:
    tier_note = (
        "Tier 1 direct PubMed query"
        if retrieval_tier == 1
        else "Tier 2 Claude agentic retrieval"
    )
    return {
        "cancer_associated": False,
        "insufficient_evidence": True,
        "cancer_association_rationale": (
            "No directly relevant PubMed evidence was selected for synthesis."
        ),
        "gene_summary": (
            f"No directly relevant PubMed evidence was selected for {gene}; "
            "therefore no cancer association is assigned. "
            f"(Literature sourced via {tier_note}.)"
        ),
        "citations": [],
        "confidence": 0.0,
    }


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
    cache_key = _synthesis_cache_key(
        gene,
        fusions,
        in_oncokb,
        cancer_type_prevalence,
        records,
        retrieval_tier,
        gene_identity,
        local_mode,
        local_backend,
    )
    cached = read_cached_json("gene-synthesis", cache_key)
    if cached is not None:
        logger.info("Synthesis cache hit for %s (%d selected papers)", gene, len(records))
        return cached

    evidence_packets = await _extract_evidence_packets(
        gene,
        records,
        gene_identity,
        local_mode,
        local_backend,
    )
    user_prompt = _build_user_prompt(
        gene,
        fusions,
        in_oncokb,
        cancer_type_prevalence,
        records,
        retrieval_tier,
        gene_identity,
        evidence_packets,
    )
    tool_input = await complete_with_tool(
        model=settings.synthesis_model,
        system=SYSTEM_PROMPT,
        user=user_prompt,
        tool=ANNOTATE_TOOL,
        max_tokens=2048,
        local_mode=local_mode,
        local_backend=local_backend,
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

    write_cached_json("gene-synthesis", cache_key, tool_input)
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
    tier = synthesis_result.get("cancer_associated_gene_tier")
    og_or_tsg = synthesis_result.get("og_or_tsg")
    cancer_associated = synthesis_result.get("cancer_associated")
    insufficient_evidence = synthesis_result.get("insufficient_evidence", False)

    if cancer_associated is False or insufficient_evidence:
        tier = None
        og_or_tsg = None
    elif in_oncokb is False and tier == "Class I - Driver":
        logger.info(
            "Downgrading non-OncoKB Class I call for %s to Class II pending stronger curation",
            gene,
        )
        tier = "Class II - Likely Driver"

    retrieved_pmids = list(dict.fromkeys(record.pmid for record in records if record.pmid))

    return GeneAnnotation(
        gene=gene,
        fusions=list(dict.fromkeys(fusions)),  # deduplicate, preserve order
        in_oncokb=in_oncokb,
        cancer_associated=cancer_associated,
        cancer_association_rationale=synthesis_result.get("cancer_association_rationale"),
        cancer_associated_gene_tier=tier,
        og_or_tsg=og_or_tsg,
        cancer_type_prevalence=cancer_type_prevalence,
        gene_class=synthesis_result.get("gene_class"),
        signaling_pathways=synthesis_result.get("signaling_pathways"),
        gene_summary=synthesis_result.get("gene_summary"),
        citations=synthesis_result.get("citations", []),
        retrieval_count=len(records),
        retrieved_pmids=retrieved_pmids,
        insufficient_evidence=insufficient_evidence,
        confidence=synthesis_result.get("confidence", 0.0),
    )
