"""Explicit OpenEvidence supplementary-evidence cache warmup for likely upcoming annotation runs.

Mirrors src.pipeline.literature_warmup's pattern for the PubMed Tier 1 cache,
but warms OpenEvidenceClient's Redis-backed cache (see src.pipeline.cache and
src.pipeline.openevidence) instead. Runs at its OWN configurable concurrency
(OPENEVIDENCE_WARMUP_CONCURRENCY), independent of ANNOTATION_GENE_CONCURRENCY
— an offline warmup pass is not live annotation traffic and can safely fan
out wider than the semaphore that gates concurrent per-gene annotation
requests.
"""

from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Dict, List, Optional, Union

from src.config import settings
from src.models.schema import FusionInput
from src.pipeline.normalization import is_fusion_input, normalize_fusions
from src.pipeline.openevidence import OpenEvidenceClient


def _elapsed_ms(start: float) -> float:
    return round((perf_counter() - start) * 1000, 2)


def _normalize_inputs(
    inputs: Union[List[str], List[FusionInput]],
) -> tuple[List[str], Dict[str, str]]:
    input_strings: List[str] = []
    tumor_type_map: Dict[str, str] = {}
    for item in inputs:
        if isinstance(item, str):
            input_strings.append(item)
        else:
            input_strings.append(item.fusion)
            if item.tumor_type:
                tumor_type_map[item.fusion] = item.tumor_type
    return input_strings, tumor_type_map


async def warm_openevidence_cache(
    inputs: Union[List[str], List[FusionInput]],
    *,
    concurrency: Optional[int] = None,
    client: Optional[OpenEvidenceClient] = None,
) -> dict:
    """
    Pre-fetch OpenEvidence supplementary-evidence analyses into the shared
    Redis cache for genes derived from `inputs`, ahead of live annotation
    traffic.

    Idempotent: OpenEvidenceClient.get_gene_analysis is itself backed by
    cached_call (see src.pipeline.cache), so re-running this against genes
    that are already warm is a cache hit — no duplicate live HTTP call.

    Derives each gene's associated fusion (if any) from its own raw
    `gene_inputs` — the same way orchestrator.py's annotate_one() derives
    `associated_fusions` — and passes it into get_gene_analysis as `fusion`.
    get_gene_analysis's cache key (see openevidence.py's _cache_key)
    includes `fusion` whenever present, so a generic-question entry and a
    fusion-specific one occupy separate cache slots: a warmed entry is only
    a hit for a later sidecar request (GET /v1/genes/{gene}/openevidence in
    main.py) that asks with the same gene/tumor_type/fusion.

    No-op when settings.openevidence_enabled is false: the feature flag is
    supposed to make OpenEvidence off end-to-end, not just hidden from the
    sidecar endpoint, so warmup must not make any OpenEvidenceClient calls
    (or construct one) while it's off either.
    """
    if not settings.openevidence_enabled:
        return {
            "inputs_processed": 0,
            "genes_total": 0,
            "genes_warmed": 0,
            "genes_failed": 0,
            "warmed": [],
            "errors": [],
            "timings_ms": {"total": 0.0},
            "skipped_reason": "openevidence_enabled is false",
        }

    total_start = perf_counter()
    input_strings, tumor_type_by_input = _normalize_inputs(inputs)
    gene_map = await normalize_fusions(input_strings)
    semaphore = asyncio.Semaphore(concurrency or max(1, settings.openevidence_warmup_concurrency))
    oe_client = client or OpenEvidenceClient()

    async def warm_one(gene: str, gene_inputs: List[str]) -> dict:
        tumor_type = next(
            (tumor_type_by_input[value] for value in gene_inputs if value in tumor_type_by_input),
            None,
        )
        associated_fusions = [value for value in gene_inputs if is_fusion_input(value)]
        fusion = associated_fusions[0] if associated_fusions else None
        start = perf_counter()
        async with semaphore:
            analysis = await oe_client.get_gene_analysis(gene, tumor_type=tumor_type, fusion=fusion)
        return {
            "gene": gene,
            "citation_count": len(analysis.citations),
            "timings_ms": {"openevidence_warmup": _elapsed_ms(start)},
        }

    results = await asyncio.gather(
        *[
            warm_one(gene, gene_inputs)
            for gene, (_resolved_gene, gene_inputs) in gene_map.items()
        ],
        return_exceptions=True,
    )

    warmed = []
    errors = []
    for gene, result in zip(gene_map.keys(), results):
        if isinstance(result, Exception):
            errors.append({"gene": gene, "error": str(result)})
        else:
            warmed.append(result)

    warmed.sort(key=lambda item: item["gene"])
    return {
        "inputs_processed": len(input_strings),
        "genes_total": len(gene_map),
        "genes_warmed": len(warmed),
        "genes_failed": len(errors),
        "warmed": warmed,
        "errors": errors,
        "timings_ms": {"total": _elapsed_ms(total_start)},
    }
