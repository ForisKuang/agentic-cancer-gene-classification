# TCGA expression statistics

Isolated TCGA producer ported from `gene-expression-relevance` at `a7c7cc6`,
now method m3: the original lineage arm is blocked for broadly expressed genes,
and a pooled-P99 subgroup arm rescues supported tails. Dependency constraints,
HGNC checksum, sample selection, and date-free identity semantics remain.
Each (gene, cohort) also carries alteration-type detail (Point Mutation, Structural
Variant, Copy Number Alteration) from the same pinned mutation/GISTIC/SV files; it never
changes expression results. Full builds also require the alteration controls (13
enriched, 66 not enriched). `--delete-alteration-downloads` removes each ~0.1–0.9 GB raw
file once its compact summary is cached (the workflow uses it); `--skip-alterations` is
review-only. Full builds require 51/51 curated positives and 0/27 negative passes; a failed
control gate stops before PubMed and writes `<out>-failed/controls-report.json`.
Each successful bundle includes `controls-report.json`. Subsets skip the gate
and are review-only. The panel in `controls.yaml` uses JSON-compatible YAML.

The single authoritative producer/consumer contract is [docs/contract.md](docs/contract.md).
Weekly/manual builds publish verified GitHub release bundles for the reader
being developed in #167. The application never imports this package.

From this directory, with Python 3.12:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -c constraints.txt setuptools
pip install --no-build-isolation -c constraints.txt -e '.[dev]'
ruff check .
pytest
python -m gene_expression_relevance build \
  --datahub-commit "$(cat datahub-commit.txt)" --out artifacts \
  --genes ERBB2,ALB,KLK3,ESR1,TP53
python scripts/bundle.py artifacts bundle
```

Omit `--genes` for a full build; blank, whitespace and comma-only lists also
mean full build. PubMed defaults to ~3 requests/s with or without an API key.
Configure `EXPRESSION_STATS_PUBMED_REQUESTS_PER_SECOND` (at most 3 unkeyed or
10 keyed) only after accounting for shared ACGC production usage; set
`NCBI_API_KEY` for keyed access. Workflow prechecks require this secret.
Operational defaults live in `runtime-settings.json` outside the hashed method
parameters; the manifest records the effective rate in `source`. Changing the
rate does not change release IDs. Cache expiry is staggered over 15–30 days.
A 300-minute precheck budget (also capped 315 minutes from build start) fails
cleanly as incomplete, saves successful counts and preserves prior output.
Set `EXPRESSION_STATS_PRECHECK_TIME_BUDGET_SECONDS` to lower the phase budget.
Rerun to resume from cache; inspect `<out>-incomplete/precheck-report.json`.
Skipped prechecks neither join the PubMed queue nor restore/save its cache.
`--skip-precheck` and subsets are review-only. No page or web server is needed.
Downloads and PubMed counts persist in `.cache/`; failed builds stage output
without replacing completed artifacts. Generated files are ignored.

Pipeline CI runs its own stricter ruff rules and tests. Root ruff explicitly
excludes this directory; ACGC pytest's existing `testpaths = ["tests"]` excludes
these tests. The pipeline isolation test checks both directions, including bare app module
aliases exposed by adding src/ to sys.path. A stdlib-only root test also guards
app imports on every app CI run and passes if this directory is removed.
The app dependencies are unchanged, and `.dockerignore` excludes this directory.

Only the release workflow calls `scripts/publish.py`; running the build and
bundle commands above never creates a release. Publish requires `GH_TOKEN`
with contents write permission and uses GitHub CLI on the runner. Upload/verification
failures best-effort delete their own draft; surviving stale drafts fail loudly
and require explicit deletion. Only a separate main-only publish job receives
write permission, and GH_TOKEN is scoped to its publication step. See the
contract for concurrency and freshness semantics.

Contract 1.5 makes `confidence_tier: high` an either-arm decision: a v3 pass is high
when the lineage arm (still broad-blocked) or the subgroup arm meets its strong cutoffs,
even when `pass_mode` is lineage. The subgroup arm must still pass its base v3 gate
(counts, P95 shift and FDR); fraction and ratio alone are not enough. The additive
`strong_arm` field records the promoting arm; purity demotion still applies afterwards,
and its high-purity re-test rechecks only the recorded promoting arm(s).

Contract 1.4 adds the pinned GDC ABSOLUTE tumor-purity R2 gate: high pairs can
be demoted to candidate, preserving every v3 statistical call and alteration
result. Exempt or insufficient-coverage pairs retain high with an explicit
unassessed note. Source acknowledgement, thresholds, coverage semantics and
consumer fail-closed behavior are in [the contract](docs/contract.md#contract-14-supplementary-tumor-purity-gate).
