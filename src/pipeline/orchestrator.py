"""
M0 pipeline orchestrator.
Coordinates normalization → DB lookups → literature retrieval → LLM synthesis
for each gene derived from an input fusion list.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import List

from src.models.schema import AnnotationResult, GeneAnnotation
from src.pipeline.db_lookups import check_oncokb_membership, get_msk_genie_prevalence
from src.pipeline.literature import retrieve_literature
from src.pipeline.normalization import normalize_fusions
from src.pipeline.synthesis import build_gene_annotation, synthesize_gene_annotation

logger = logging.getLogger(__name__)


async def _annotate_gene(
    gene: str,
    fusions: List[str],
    resolved: bool,
    unresolvable: bool,
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
        retrieve_literature(gene, fusions),
    )

    prevalence = get_msk_genie_prevalence(gene)

    try:
        synthesis = await synthesize_gene_annotation(
            gene=gene,
            fusions=fusions,
            in_oncokb=oncokb_membership,
            cancer_type_prevalence=prevalence,
            records=records,
            retrieval_tier=retrieval_tier,
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
        records=records,
        synthesis_result=synthesis,
    )


async def run_pipeline(fusions: List[str]) -> AnnotationResult:
    """
    Main entry point: accepts a list of fusion strings and returns
    a structured AnnotationResult with one GeneAnnotation per gene.
    """
    run_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    logger.info("Pipeline run %s started — %d fusions", run_id, len(fusions))

    gene_map = await normalize_fusions(fusions)
    logger.info("Resolved %d unique genes from %d fusions", len(gene_map), len(fusions))

    # Annotate all genes; run sequentially to respect rate limits
    # (PubMed: 3 req/s without key; LLM calls are already async within each gene)
    annotations: List[GeneAnnotation] = []
    for canonical, (resolved_gene, gene_fusions) in gene_map.items():
        annotation = await _annotate_gene(
            gene=canonical,
            fusions=gene_fusions,
            resolved=resolved_gene.resolved,
            unresolvable=resolved_gene.unresolvable,
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
        fusions_processed=len(fusions),
        genes_annotated=len(annotations),
        annotations=annotations,
    )
