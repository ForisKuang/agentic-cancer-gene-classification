"""Live paired benchmark; instrumentation is confined to this executable.

Run each arm in a fresh process (never mock network/LLM responses):
  uv run python -m benchmarks.run_openevidence_benchmark --arm disabled --output DIR
  uv run python -m benchmarks.run_openevidence_benchmark --arm enabled --output DIR

Each arm starts with an empty in-memory reference cache, avoiding production
Redis and cross-arm cache warming. OpenEvidence always bypasses that cache.
Outputs include full annotations, selected/retrieved literature, SDK-reported
usage (including escalation), and incremental checkpoints for long live runs.
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import subprocess
from time import perf_counter
from unittest.mock import patch

from src.config import settings
from src.logging_utils import install_secret_redaction_filter
from src.pipeline import db_lookups, literature, llm_client, normalization, openevidence, orchestrator

GENES = [
    "TP53", "KRAS", "EGFR", "BRAF", "BRCA1", "ALK",
    "ACACB", "AIRE", "ANKRD13A", "CRACD", "DENND2C", "FAM117A",
    "RFX7", "RP1", "TRARG1", "CLCN3P1",
]
CURRENT_GENE: ContextVar[str] = ContextVar("benchmark_gene", default="unattributed")


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


async def run(arm: str, output: Path, timeout: float) -> None:
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{arm}.json"
    if target.exists():
        raise SystemExit(f"Refusing to overwrite {target}; use a new output directory")
    settings.openevidence_enabled = arm == "enabled"
    settings.openevidence_timeout_seconds = timeout
    if not settings.anthropic_api_key and settings.anthropic_sdk_provider == "anthropic":
        raise SystemExit("An Anthropic SDK credential is required")
    if settings.openevidence_enabled and not settings.openevidence_api_key:
        raise SystemExit("An OpenEvidence credential is required")
    data = {
        "arm": arm, "genes": GENES,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "settings": {name: getattr(settings, name) for name in (
            "anthropic_sdk_provider", "synthesis_model", "synthesis_fast_model",
            "selection_model", "retrieval_model", "synthesis_model_escalation",
            "annotation_gene_concurrency", "llm_concurrency", "max_papers_for_synthesis",
            "max_citations_per_annotation", "min_papers_for_strong_association",
            "openevidence_enabled", "openevidence_model", "openevidence_timeout_seconds",
        )},
        "cache_policy": "empty per-arm in-memory reference cache; OpenEvidence always live",
        "per_gene": {}, "status": "running",
    }
    cache = {}
    original_annotate = orchestrator._annotate_gene
    original_usage = llm_client.record_llm_usage
    original_retrieve = orchestrator.retrieve_literature
    original_synthesize = orchestrator.synthesize_gene_annotation

    async def reference_cache(key, compute, ttl_seconds=None):
        if key not in cache:
            cache[key] = await compute()
        # Match Redis serialization isolation; callers may mutate their results.
        return json.loads(json.dumps(cache[key]))

    async def live_oe_cache(key, compute, ttl_seconds=None):
        return await compute()

    async def annotate(*args, **kwargs):
        gene = kwargs.get("gene", args[0] if args else None)
        token = CURRENT_GENE.set(gene)
        data["per_gene"][gene] = {"llm_calls": []}
        try:
            return await original_annotate(*args, **kwargs)
        finally:
            CURRENT_GENE.reset(token)

    def usage(model, model_purpose, value):
        original_usage(model, model_purpose, value)
        if value is not None:
            row = {"model": model, "purpose": model_purpose}
            for name in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                         "cache_read_input_tokens"):
                row[name] = getattr(value, name, 0) or 0
            data["per_gene"][CURRENT_GENE.get()]["llm_calls"].append(row)
            write_json(target, data)

    async def retrieve(*args, **kwargs):
        records, tier = await original_retrieve(*args, **kwargs)
        data["per_gene"][CURRENT_GENE.get()].update(
            retrieval_tier=tier, records=[r.model_dump() for r in records],
        )
        return records, tier

    async def synthesize(*args, **kwargs):
        data["per_gene"][CURRENT_GENE.get()]["selected_pmids"] = [
            r.pmid for r in kwargs["records"]
        ]
        return await original_synthesize(*args, **kwargs)

    async def checkpoint(annotation):
        data["per_gene"][annotation.gene]["annotation"] = annotation.model_dump()
        write_json(target, data)
        print(f"{arm}: {annotation.gene} completed; "
              f"{len(annotation.citations)} citations; "
              f'{annotation.timings_ms.get("total", 0) / 1000:.1f}s', flush=True)

    from contextlib import ExitStack
    started = perf_counter()
    write_json(target, data)
    with ExitStack() as stack:
        for module in (db_lookups, literature, normalization):
            stack.enter_context(patch.object(module, "cached_call", reference_cache))
        for module in (llm_client, literature):
            stack.enter_context(patch.object(module, "record_llm_usage", usage))
        stack.enter_context(patch.object(openevidence, "cached_call", live_oe_cache))
        for name, replacement in (
            ("_annotate_gene", annotate), ("retrieve_literature", retrieve),
            ("synthesize_gene_annotation", synthesize),
        ):
            stack.enter_context(patch.object(orchestrator, name, replacement))
        try:
            result = await orchestrator.run_pipeline(
                GENES, force_refresh=True, on_annotation=checkpoint,
            )
            data["pipeline_result"] = result.model_dump()
            data["status"] = "complete"
        except BaseException:
            data["status"] = "failed"
            raise
        finally:
            data["wall_seconds"] = perf_counter() - started
            data["finished_at"] = datetime.now(timezone.utc).isoformat()
            write_json(target, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("disabled", "enabled"))
    parser.add_argument("--compare-models", nargs="+", choices=("osler", "darwin"),
                        help="Benchmark the live production sidecar with these model arms")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=900,
                        help="OpenEvidence read timeout in seconds (both arms record this setting)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    install_secret_redaction_filter()
    if args.compare_models:
        from benchmarks.openevidence_model_benchmark import run_models
        asyncio.run(run_models(args.output, args.timeout, args.concurrency, args.compare_models))
    elif args.arm:
        asyncio.run(run(args.arm, args.output, args.timeout))
    else:
        parser.error("Specify --arm or --compare-models")


if __name__ == "__main__":
    main()
