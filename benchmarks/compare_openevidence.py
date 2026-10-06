"""Create reproducible per-gene metrics from two completed live arms.

Usage: uv run python -m benchmarks.compare_openevidence RESULTS_DIRECTORY
Reference recall uses existing holdout PMIDs only; absent/empty reference sets
produce null recall, never a fabricated zero or perfect score.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.run_openevidence_benchmark import write_json

CLASSIFICATION_FIELDS = ("cancer_associated", "in_oncokb", "insufficient_evidence")
TEXT_FIELDS = ("gene_summary", "cancer_association_rationale", "gene_class", "signaling_pathways")


def tokens(calls: list[dict]) -> dict:
    result = {key: sum(call.get(key, 0) for call in calls) for key in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    )}
    result["total_input_tokens"] = sum(result[key] for key in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    ))
    result["calls"] = len(calls)
    return result


def compare(disabled: dict, enabled: dict, gold: dict) -> dict:
    if disabled["status"] != "complete" or enabled["status"] != "complete":
        raise ValueError("Both arms must be complete")
    if disabled["genes"] != enabled["genes"]:
        raise ValueError("Arms must request the same gene list in the same order")
    if set(disabled["per_gene"]) != set(enabled["per_gene"]):
        raise ValueError("Resolved gene sets differ")
    rows = []
    for gene in sorted(disabled["per_gene"]):
        pair = [arm["per_gene"][gene] for arm in (disabled, enabled)]
        annotations = [row["annotation"] for row in pair]
        citation_sets = [set(a["citations"]) for a in annotations]
        reference = set(gold.get(gene, {}).get("citations", []))
        row = {"gene": gene, "reference_pmids": sorted(reference)}
        for label, raw, annotation, citations in zip(
            ("disabled", "enabled"), pair, annotations, citation_sets,
        ):
            row[label] = {
                "retrieval_tier": raw["retrieval_tier"],
                "retrieved_count": annotation["retrieval_count"],
                "citations": sorted(citations), "citation_count": len(citations),
                "reference_hits": sorted(citations & reference),
                "reference_recall": len(citations & reference) / len(reference) if reference else None,
                "synthesis_tokens": tokens([c for c in raw["llm_calls"]
                                            if c["purpose"].startswith("synthesis")]),
                "all_llm_tokens": tokens(raw["llm_calls"]),
                "latency_seconds": annotation["timings_ms"]["total"] / 1000,
                "openevidence_seconds": raw.get("openevidence_seconds", 0),
                "openevidence_succeeded": raw.get("openevidence_succeeded", False),
                "supplementary_citation_count": len(
                    (raw.get("openevidence_analysis") or {}).get("citations", [])
                ),
                "classification": {k: annotation.get(k) for k in CLASSIFICATION_FIELDS},
                "error": annotation["error"],
            }
        row["classification_changes"] = {
            key: [a.get(key) for a in annotations] for key in CLASSIFICATION_FIELDS
            if annotations[0].get(key) != annotations[1].get(key)
        }
        row["text_changes"] = {
            key: [a.get(key) for a in annotations] for key in TEXT_FIELDS
            if annotations[0].get(key) != annotations[1].get(key)
        }
        row["citations_added"] = sorted(citation_sets[1] - citation_sets[0])
        row["citations_removed"] = sorted(citation_sets[0] - citation_sets[1])
        row["identical_retrieval_pool"] = (
            {r["pmid"] for r in pair[0]["records"]} == {r["pmid"] for r in pair[1]["records"]}
        )
        row["identical_selected_pmids"] = set(pair[0]["selected_pmids"]) == set(
            pair[1]["selected_pmids"]
        )
        rows.append(row)
    return {"per_gene": rows, "wall_seconds": {
        "disabled": disabled["wall_seconds"], "enabled": enabled["wall_seconds"],
    }}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--models", action="store_true", help="Summarize sidecar model arms")
    args = parser.parse_args()
    if args.models:
        data = json.loads((args.directory / "models.json").read_text())
        write_json(args.directory / "comparison.json", compare_models(data))
        return
    holdout = Path(__file__).parent / "data" / "holdout.jsonl"
    gold = {r["gene"]: r for r in map(json.loads, holdout.read_text().splitlines())}
    arms = [json.loads((args.directory / f"{arm}.json").read_text())
            for arm in ("disabled", "enabled")]
    write_json(args.directory / "comparison.json", compare(*arms, gold))


def percentile(values: list[float], fraction: float) -> float:
    """Linearly interpolated percentile, including small samples."""
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def compare_models(data: dict) -> dict:
    """Summarize live sidecar measurements independently of clinical judging."""
    from statistics import mean, median
    import re
    from urllib.parse import urlsplit
    from src.pipeline import openevidence as oe
    from src.models.schema import OpenEvidenceCitation

    def guideline_citations(row):
        citations = []
        for raw in row["analysis"].get("citations", []):
            citation = OpenEvidenceCitation(**raw)
            if any(domain in citation.url.lower() for domain in oe._GUIDELINE_URL_DOMAINS) or (
                citation.authors.strip().lower() in oe._GUIDELINE_ISSUING_ORGANIZATIONS
                or oe._GUIDELINE_TITLE_PATTERN.search(citation.title)
            ):
                citations.append(raw)
        return citations

    if data["status"] != "complete":
        raise ValueError("Model benchmark must be complete")
    summaries = {}
    rows = []
    for model in data["models"]:
        all_rows = [pair[model] for pair in data["per_gene"].values()]
        successful = [row for row in all_rows if row["status"] == "success"]
        latencies = [row["wall_seconds"] for row in successful]
        ttfts = [row["ttft_seconds"] for row in successful if row["ttft_seconds"] is not None]
        summaries[model] = {
            "successes": len(successful), "requested": len(all_rows),
            "success_rate": len(successful) / len(all_rows),
            "median_seconds": median(latencies) if latencies else None,
            "p90_seconds": percentile(latencies, .9) if latencies else None,
            "median_ttft_seconds": median(ttfts) if ttfts else None,
            "p90_ttft_seconds": percentile(ttfts, .9) if ttfts else None,
            "under_300s": sum(row["wall_seconds"] < 300 for row in successful),
            **{f"mean_{key}": mean(values) if values else None for key, values in {
                "answer_chars": [row["answer_chars"] for row in successful],
                "citations": [row["distilled"]["citation_count"] for row in successful],
                "guidelines": [len(row["distilled"]["guidelines"]) for row in successful],
                "guideline_citations": [len(guideline_citations(row)) for row in successful],
                "page_anchored_guidelines": [sum(bool(g["page_anchor"]) for g in
                    row["distilled"]["guidelines"]) for row in successful],
                "trial_mentions": [len(row["distilled"]["trial_mentions"])
                                   for row in successful],
                "additive_citations": [row["additive"]["citation_count"] for row in successful],
            }.items()},
        }

    def labels(row):
        guidelines = {urlsplit(g["url"]).netloc.lower().removeprefix("www.") +
                      urlsplit(g["url"]).path.lower() for g in guideline_citations(row)}
        # Named trials outside the production extractor are captured separately;
        # count neither all uppercase words nor outcome-stat sentences as trials.
        trials = set(re.findall(
            r"\b(?:NCT\d{8}|(?:FLAURA|ADAURA|LAURA|MARIPOSA|PAPILLON|ALEX|CROWN|ALTA|"
            r"J-ALEX|ALTA-1L|LIBRETTO|ARROW|CodeBreaK|KRYSTAL|BEACON|BREAKWATER|"
            r"COMBI|CheckMate|KEYNOTE|OlympiA|OlympiAD|EMBRACA|POLO|TALAPRO|PROfound|"
            r"SOLO|PAOLA|PRIMA|ATHENA|TRITON|PROFILE|ASCEND|ALINA|eXalt3|TRIDENT|"
            r"WU-KONG|FOCUS4|MIRROS|PYNNACLE|PANDA|ALKAZAR|ANBL|COMPEL|CHRYSALIS)"
            r"(?:[- ]?\d+[A-Za-z]*|-[A-Za-z]+)?)\b", row["analysis"]["text"], re.IGNORECASE))
        return {"guidelines": sorted(guidelines),
                "trials": sorted({re.sub(r"[- ]", "", t.upper()) for t in trials})}

    for gene in data["genes"]:
        pair = data["per_gene"][gene]
        row = {"gene": gene, "models": {}}
        for model, raw in pair.items():
            row["models"][model] = {
                "status": raw["status"], "wall_seconds": raw["wall_seconds"],
                "ttft_seconds": raw["ttft_seconds"],
                **({"answer_chars": raw["answer_chars"],
                    "citations": raw["distilled"]["citation_count"],
                    "guidelines": raw["distilled"]["guidelines"],
                    "guideline_citations": guideline_citations(raw),
                    "trial_mentions": len(raw["distilled"]["trial_mentions"]),
                    "additive_citations": raw["additive"]["citation_count"],
                    "agreement_labels": labels(raw)} if raw["status"] == "success" else {}),
            }
        if all(pair.get(m, {}).get("status") == "success" for m in ("osler", "darwin")):
            for kind in ("guidelines", "trials"):
                reference = set(labels(pair["darwin"])[kind])
                candidate = set(labels(pair["osler"])[kind])
                row[f"{kind}_agreement"] = {
                    "reference": sorted(reference), "candidate": sorted(candidate),
                    "overlap": sorted(reference & candidate),
                    "darwin_reference_recall": len(reference & candidate) / len(reference)
                                               if reference else None,
                    "darwin_only": sorted(reference - candidate),
                    "osler_only": sorted(candidate - reference),
                }
        rows.append(row)
    return {"summary": summaries, "per_gene": rows,
            "paid_call_attempts": data["paid_call_attempts"]}


if __name__ == "__main__":
    main()
