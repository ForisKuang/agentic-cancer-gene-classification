"""One blinded rubric-judge call for a completed sidecar model benchmark.

Model identities and timing/citation-count metadata are withheld. This is a
screening assessment, not expert clinical adjudication or independent proof.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from benchmarks.run_openevidence_benchmark import write_json
from src.config import settings
from src.pipeline.llm_client import make_async_sdk_client, resolve_sdk_model

RUBRIC = """Compare each pair of oncology evidence answers using the same rubric.
Model names have been withheld. Treat neither answer as ground truth.
Score each answer 1..5 for factual_correctness (5=claims consistent with cited
sources and known oncology evidence; 1=major contradictions), specificity
(5=appropriate named drugs, mutation/tumor context, named trials and guideline
recommendations; for genes with no established therapy, explicit restraint
and absence-of-evidence explanation is specific), and hallucination_safety
(5=low apparent risk with cited, appropriately qualified claims; 1=invented
trials/IDs, unjustified extrapolation or unsupported certainty). Do not reward
length alone. Publication metadata establishes source identity, not full
claim support; acknowledge uncertainty when support cannot be verified.
For every gene explain important content omissions or conflicts, and flag
suspicious NCT IDs, trial names or numeric outcomes for subsequent independent
spot-checking. Assess full prose and the supplied citation metadata.
Compare only content actually present in THIS gene's two answers. Do not
attribute a drug, trial, statistic or safety statement to an answer where it
is absent. Use neutral primary-source spot-check facts when supplied; a
numerical contradiction or overbroad approval claim must reduce correctness
and safety even when BOTH answers share it. Citation metadata alone does not
justify certainty. Explain real omissions, not generic equivalence.
Return all genes exactly once via the tool. You have no web access; do not
claim that you independently verified sources. A 5 is not clinical validation.
"""


async def judge(directory: Path, genes=None, suffix=""):
    source = json.loads((directory / "models.json").read_text())
    if source["status"] != "complete":
        raise ValueError("Both arms must complete before judging")
    requested = genes or source["genes"]
    stem = "blinded_judge" + (f"_{suffix}" if suffix else "")
    target = directory / f"{stem}.json"
    if target.exists():
        raise SystemExit("Refusing to overwrite judge result")
    if source["paid_call_attempts"] > 42:
        raise SystemExit("Insufficient remaining paid-call budget for judge")
    pairs, mapping = [], {}
    for gene in requested:
        order = ["osler", "darwin"]
        if hashlib.sha256(gene.encode()).digest()[0] % 2:
            order.reverse()
        mapping[gene] = dict(zip(("A", "B"), order))
        pair = {"gene": gene}
        for label, model in mapping[gene].items():
            row = source["per_gene"][gene][model]
            analysis = row.get("analysis")
            if analysis:
                pair[label] = {"question": analysis["question"], "text": analysis["text"],
                               "citations": [{k: c.get(k, "") for k in (
                                   "citation_key", "title", "authors", "journal", "date", "doi", "url"
                               )} for c in analysis["citations"]]}
            else:
                pair[label] = {"error": row.get("error_type")}
        pairs.append(pair)
    score_schema = {"type": "object", "properties": {
        k: {"type": "integer", "minimum": 1, "maximum": 5} for k in
        ("factual_correctness", "specificity", "hallucination_safety")},
        "required": ["factual_correctness", "specificity", "hallucination_safety"]}
    tool = {"name": "score_pairs", "description": "Record blinded paired rubric scores",
            "input_schema": {"type": "object", "properties": {"per_gene": {
                "type": "array", "items": {"type": "object", "properties": {
                    "gene": {"type": "string"}, "A": score_schema, "B": score_schema,
                    "comparison": {"type": "string"}, "spot_check_flags": {"type": "string"}},
                    "required": ["gene", "A", "B", "comparison", "spot_check_flags"]}}},
                "required": ["per_gene"]}}
    checks_path = directory / "publication_spot_checks.json"
    facts = [{k: value for k, value in check.items() if k != "finding"}
             for check in json.loads(checks_path.read_text())["checks"]] if checks_path.exists() else []
    request = {"benchmark_date": "2026-10-06", "rubric": RUBRIC,
               "pairs": pairs, "primary_source_facts": facts}
    write_json(directory / f"{stem}_input.json", request)
    model = resolve_sdk_model(settings.selection_model, "selection")
    # Explicitly disable SDK retries: one judge attempt counts toward total budget.
    async with make_async_sdk_client().with_options(max_retries=0, timeout=600) as client:
        response = await client.messages.create(
            model=model, max_tokens=12000, system=RUBRIC,
            messages=[{"role": "user", "content": json.dumps(request)}], tools=[tool],
            tool_choice={"type": "tool", "name": "score_pairs"})
    result = next(block.input for block in response.content if block.type == "tool_use")
    returned = [row["gene"] for row in result["per_gene"]]
    if sorted(returned) != sorted(requested):
        raise ValueError("Judge did not return each gene exactly once")
    write_json(target, {"judge_model": model, "paid_judge_calls": 1,
                       "usage": response.usage.model_dump(), "rubric": RUBRIC,
                       "mapping": mapping, **result})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--genes", nargs="+")
    parser.add_argument("--suffix", default="")
    args = parser.parse_args()
    if args.genes and not args.suffix:
        parser.error("Subset judging requires a unique --suffix")
    asyncio.run(judge(args.directory, args.genes, args.suffix))
