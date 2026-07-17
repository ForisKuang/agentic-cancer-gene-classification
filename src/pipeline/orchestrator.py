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
from typing import Any, AsyncIterator, Dict, List, Optional

from src.config import settings
from src.models.schema import AnnotationResult, GeneAnnotation, ResolvedGene
from src.pipeline.db_lookups import (
    OncoKBGeneLookup,
    check_oncokb_membership,
    get_msk_genie_prevalence,
)
from src.pipeline.literature import retrieve_literature
from src.pipeline.llm_client import resolve_local_backend
from src.pipeline.normalization import normalize_fusions
from src.pipeline.selection import select_papers_for_synthesis
from src.pipeline.synthesis import (
    build_gene_annotation,
    build_no_selected_evidence_result,
    synthesize_gene_annotation,
)

logger = logging.getLogger(__name__)


def _is_terminal_model_capacity_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    if not message:
        return False
    if "429" in message:
        return True
    if "usage limit" in message or "limit reached" in message:
        return True
    if "credit balance" in message or "billing" in message:
        return True
    if "quota" in message:
        return True
    if "insufficient" in message and any(
        token in message for token in ("token", "credit", "quota", "balance")
    ):
        return True
    return False


def _terminal_model_capacity_message(exc: BaseException) -> str:
    detail = str(exc).strip() or exc.__class__.__name__
    return (
        "Run stopped early because the model/local agent could not continue "
        f"literature retrieval or synthesis: {detail}"
    )


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
    local_mode: bool = False,
    local_backend: Optional[str] = None,
    oncokb_lookup: Optional[OncoKBGeneLookup] = None,
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
        check_oncokb_membership(gene, lookup=oncokb_lookup),
        retrieve_literature(gene, fusions, local_mode=local_mode, local_backend=local_backend),
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
    if not selected_records:
        logger.info("No directly relevant papers selected for %s — skipping synthesis", gene)
        return build_gene_annotation(
            gene=gene,
            fusions=fusions,
            in_oncokb=oncokb_membership,
            cancer_type_prevalence=prevalence,
            records=records,
            synthesis_result=build_no_selected_evidence_result(gene, retrieval_tier),
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
            retrieved_pmids=list(dict.fromkeys(record.pmid for record in records if record.pmid)),
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


async def run_pipeline(
    fusions: List[str],
    local_mode: bool = False,
    local_backend: Optional[str] = None,
) -> AnnotationResult:
    """
    Main entry point: accepts a list of fusion strings and returns
    a structured AnnotationResult with one GeneAnnotation per gene.
    """
    final_result: Optional[AnnotationResult] = None
    async for event in iter_pipeline_events(
        fusions,
        local_mode=local_mode,
        local_backend=local_backend,
    ):
        if event["type"] in {"complete", "error"}:
            final_result = event["result"]
    if final_result is None:
        raise RuntimeError("Pipeline did not produce a result")
    return final_result


async def iter_pipeline_events(
    fusions: List[str],
    local_mode: bool = False,
    local_backend: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """
    Stream pipeline progress as genes finish.

    Emits:
      - start: run metadata after normalization
      - annotation: one finished GeneAnnotation
      - error: terminal model/quota error plus partial AnnotationResult
      - complete: final AnnotationResult
    """
    run_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    local_backend = resolve_local_backend(local_mode=local_mode, local_backend=local_backend)
    local_mode = local_backend is not None
    logger.info("Pipeline run %s started — %d fusions", run_id, len(fusions))

    gene_map = await normalize_fusions(fusions)
    logger.info("Resolved %d unique genes from %d fusions", len(gene_map), len(fusions))

    yield {
        "type": "start",
        "run_id": run_id,
        "timestamp": timestamp,
        "fusions_processed": len(fusions),
        "genes_total": len(gene_map),
    }

    concurrency = max(1, settings.max_gene_annotation_concurrency)
    semaphore = asyncio.Semaphore(concurrency)
    oncokb_lookup = OncoKBGeneLookup()
    annotations: List[GeneAnnotation] = []

    async def annotate_one(
        canonical: str,
        resolved_gene: ResolvedGene,
        gene_fusions: List[str],
    ) -> GeneAnnotation:
        async with semaphore:
            annotation = await _annotate_gene(
                gene=canonical,
                fusions=gene_fusions,
                resolved_gene=resolved_gene,
                unresolvable=resolved_gene.unresolvable,
                local_mode=local_mode,
                local_backend=local_backend,
                oncokb_lookup=oncokb_lookup,
            )
            logger.info(
                "Annotated %s — cancer_associated=%s, citations=%d, confidence=%.2f",
                canonical,
                annotation.cancer_associated,
                len(annotation.citations),
                annotation.confidence,
            )
            return annotation

    def build_result(run_error: Optional[str] = None) -> AnnotationResult:
        sorted_annotations = sorted(annotations, key=lambda a: a.gene)
        return AnnotationResult(
            run_id=run_id,
            timestamp=timestamp,
            fusions_processed=len(fusions),
            genes_annotated=len(sorted_annotations),
            annotations=sorted_annotations,
            run_error=run_error,
        )

    logger.info("Annotating genes with concurrency=%d", concurrency)
    task_to_gene = {
        asyncio.create_task(annotate_one(canonical, resolved_gene, gene_fusions)): canonical
        for canonical, (resolved_gene, gene_fusions) in gene_map.items()
    }
    pending = set(task_to_gene)

    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            terminal_error: Optional[tuple[str, str]] = None
            for task in done:
                gene = task_to_gene[task]
                try:
                    annotation = task.result()
                except Exception as exc:
                    if _is_terminal_model_capacity_error(exc):
                        message = _terminal_model_capacity_message(exc)
                        terminal_error = (gene, message)
                        continue

                    logger.exception("Annotation failed for gene %s", gene)
                    annotation = GeneAnnotation(
                        gene=gene,
                        fusions=list(dict.fromkeys(gene_map[gene][1])),
                        insufficient_evidence=True,
                        confidence=0.0,
                        error=f"Annotation error: {exc}",
                    )

                annotations.append(annotation)
                yield {
                    "type": "annotation",
                    "annotation": annotation,
                    "completed_count": len(annotations),
                    "genes_total": len(gene_map),
                }

            if terminal_error is not None:
                gene, message = terminal_error
                logger.error("Stopping run %s early for %s: %s", run_id, gene, message)
                for pending_task in pending:
                    pending_task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                yield {
                    "type": "error",
                    "message": message,
                    "gene": gene,
                    "result": build_result(run_error=message),
                }
                return
    finally:
        for task in pending:
            task.cancel()

    yield {"type": "complete", "result": build_result()}
