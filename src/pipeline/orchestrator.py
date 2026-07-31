"""
M0 pipeline orchestrator.
Coordinates normalization → DB lookups → literature retrieval → LLM synthesis
for each gene derived from an input gene/fusion list.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

from src.config import settings
from src.models.schema import AnnotationResult, FusionInput, GeneAnnotation, ResolvedGene
from src.pipeline.db_lookups import check_oncokb_membership, get_msk_genie_prevalence
from src.pipeline.literature import retrieve_literature
from src.pipeline.llm_client import resolve_local_backend
from src.pipeline.normalization import is_fusion_input, normalize_fusions
from src.pipeline.selection import select_papers_for_synthesis
from src.pipeline.synthesis import build_gene_annotation, synthesize_gene_annotation

logger = logging.getLogger(__name__)


def _format_gene_identity(resolved_gene: ResolvedGene) -> Optional[str]:
    """Return concise HGNC identity context for retrieval-grounded LLM prompts."""
    if not resolved_gene.resolved:
        return None

    parts = []
    if resolved_gene.name:
        parts.append(f"HGNC name: {resolved_gene.name}")
    if resolved_gene.hgnc_id:
        parts.append(f"HGNC ID: {resolved_gene.hgnc_id}")
    if resolved_gene.locus_type:
        parts.append(f"Locus type: {resolved_gene.locus_type}")
    if resolved_gene.alias_symbols:
        aliases = ", ".join(resolved_gene.alias_symbols[:8])
        parts.append(f"Accepted aliases: {aliases}")
    return "; ".join(parts) if parts else None


async def _annotate_gene(
    gene: str,
    fusions: List[str],
    resolved_gene: ResolvedGene,
    unresolvable: bool,
    tumor_type: Optional[str] = None,
    local_mode: bool = False,
    local_backend: Optional[str] = None,
) -> GeneAnnotation:
    """Run the full annotation pipeline for a single gene."""
    if unresolvable:
        logger.info("Gene %s is unresolvable (bare Ensembl / unannotated locus)", gene)
        return GeneAnnotation(
            gene=gene,
            fusions=list(dict.fromkeys(fusions)),
            in_oncokb=False,
            cancer_associated=None,
            insufficient_evidence=True,
            confidence=0.0,
            error="Unresolvable gene symbol — bare Ensembl ID or unannotated locus",
        )

    # Run DB lookup and literature retrieval concurrently
    oncokb_membership, (records, retrieval_tier) = await asyncio.gather(
        check_oncokb_membership(gene),
        retrieve_literature(
            gene, fusions,
            tumor_type=tumor_type,
            local_mode=local_mode,
            local_backend=local_backend,
        ),
    )

    prevalence = get_msk_genie_prevalence(gene)
    gene_identity = _format_gene_identity(resolved_gene)

    # Citation selection pass: filter broad retrieval corpus down to the
    # most directly relevant papers before synthesis to improve precision
    # without shrinking the recall pool.
    selected_records = await select_papers_for_synthesis(
        gene,
        records,
        settings.max_papers_for_synthesis,
        gene_identity=gene_identity,
        local_mode=local_mode,
        local_backend=local_backend,
    )

    try:
        synthesis = await synthesize_gene_annotation(
            gene=gene,
            fusions=fusions,
            in_oncokb=oncokb_membership,
            cancer_type_prevalence=prevalence,
            records=selected_records,
            retrieval_tier=retrieval_tier,
            gene_identity=gene_identity,
            local_mode=local_mode,
            local_backend=local_backend,
        )
    except Exception as e:
        logger.error("Synthesis failed for gene %s: %s", gene, e)
        return GeneAnnotation(
            gene=gene,
            fusions=list(dict.fromkeys(fusions)),
            in_oncokb=oncokb_membership,
            retrieval_count=len(records),
            insufficient_evidence=True,
            confidence=0.0,
            error=f"Synthesis error: {e}",
        )

    return build_gene_annotation(
        gene=gene,
        fusions=fusions,
        in_oncokb=oncokb_membership,
        cancer_type_prevalence=prevalence,
        records=records,       # full count for retrieval_count field
        synthesis_result=synthesis,
    )


def _normalize_fusion_inputs(
    fusions: Union[List[str], List[FusionInput]],
) -> tuple[List[str], Dict[str, str]]:
    """
    Accept either plain strings or FusionInput objects.
    Returns (input_strings, tumor_type_by_input_string).
    """
    input_strings: List[str] = []
    tumor_type_map: Dict[str, str] = {}

    for item in fusions:
        if isinstance(item, str):
            input_strings.append(item)
        else:
            input_strings.append(item.fusion)
            if item.tumor_type:
                tumor_type_map[item.fusion] = item.tumor_type

    return input_strings, tumor_type_map


async def run_pipeline(
    fusions: Union[List[str], List[FusionInput]],
    local_mode: bool = False,
    local_backend: Optional[str] = None,
) -> AnnotationResult:
    """
    Main entry point: accepts a list of gene/fusion strings (or FusionInput objects) and returns
    a structured AnnotationResult with one GeneAnnotation per gene.
    """
    run_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    local_backend = resolve_local_backend(local_mode=local_mode, local_backend=local_backend)
    local_mode = local_backend is not None

    input_strings, tumor_type_by_input = _normalize_fusion_inputs(fusions)
    logger.info("Pipeline run %s started — %d inputs", run_id, len(input_strings))

    gene_map = await normalize_fusions(input_strings)
    logger.info("Resolved %d unique genes from %d inputs", len(gene_map), len(input_strings))

    # Build per-gene tumor_type: first non-null tumor_type from the gene's submitted inputs.
    gene_tumor_type: Dict[str, Optional[str]] = {}
    for canonical, (_, gene_inputs) in gene_map.items():
        for f in gene_inputs:
            tt = tumor_type_by_input.get(f)
            if tt:
                gene_tumor_type[canonical] = tt
                break

    # Annotate all genes; run sequentially to respect rate limits
    # (PubMed: 3 req/s without key; LLM calls are already async within each gene)
    annotations: List[GeneAnnotation] = []
    for canonical, (resolved_gene, gene_inputs) in gene_map.items():
        associated_fusions = [value for value in gene_inputs if is_fusion_input(value)]
        annotation = await _annotate_gene(
            gene=canonical,
            fusions=associated_fusions,
            resolved_gene=resolved_gene,
            unresolvable=resolved_gene.unresolvable,
            tumor_type=gene_tumor_type.get(canonical),
            local_mode=local_mode,
            local_backend=local_backend,
        )
        annotations.append(annotation)
        logger.info(
            "Annotated %s — cancer_associated=%s, citations=%d, confidence=%.2f",
            canonical,
            annotation.cancer_associated,
            len(annotation.citations),
            annotation.confidence,
        )

    annotations.sort(key=lambda a: a.gene)

    return AnnotationResult(
        run_id=run_id,
        timestamp=timestamp,
        fusions_processed=len(input_strings),
        genes_annotated=len(annotations),
        annotations=annotations,
    )
