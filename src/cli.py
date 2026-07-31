"""
CLI entry point for manual invocation.
Usage:
  # Annotate from a text file (one gene or fusion per line)
  python -m src.cli --input inputs.txt --output results.json

  # Annotate from command-line args
  python -m src.cli --fusions "ALK" "ANKRD13A::ACACB" "ASAP3::HNRNPR"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from src.pipeline.llm_client import DEFAULT_LOCAL_BACKEND, LOCAL_BACKENDS
from src.pipeline.orchestrator import run_pipeline
from src.pipeline.results_export import write_annotation_results_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agentic Cancer Gene Classification — M0 CLI"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--input",
        metavar="FILE",
        help="Path to a text file with one gene or fusion per line (e.g. ALK or GENE1::GENE2)",
    )
    group.add_argument(
        "--fusions",
        nargs="+",
        metavar="INPUT",
        help="One or more gene or fusion strings inline (e.g. ALK or ANKRD13A::ACACB)",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        default="-",
        help="Output JSON file path. Use '-' for stdout (default).",
    )
    parser.add_argument(
        "--output-csv",
        metavar="FILE",
        help="Write full gene-level annotation results as a CSV for spreadsheet import.",
    )
    parser.add_argument(
        "--local",
        nargs="?",
        const=DEFAULT_LOCAL_BACKEND,
        choices=LOCAL_BACKENDS,
        metavar="BACKEND",
        help=(
            "Route LLM calls through a local agent CLI instead of the Anthropic SDK. "
            f"Choices: {', '.join(LOCAL_BACKENDS)}. Defaults to {DEFAULT_LOCAL_BACKEND} "
            "when --local is provided without a backend."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.input:
        with open(args.input) as f:
            fusions = [line.strip() for line in f if line.strip()]
    else:
        fusions = args.fusions

    if not fusions:
        print("No genes or fusions provided.", file=sys.stderr)
        sys.exit(1)

    if args.local:
        print(
            f"Local mode: LLM calls routed through `{args.local}` (no API key required).",
            file=sys.stderr,
        )

    result = asyncio.run(run_pipeline(fusions, local_backend=args.local))
    output = result.model_dump_json(indent=2)

    if args.output_csv:
        write_annotation_results_csv(result, args.output_csv)
        print(f"Results CSV written to {args.output_csv}", file=sys.stderr)

    if args.output == "-":
        print(output)
    else:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Results written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
