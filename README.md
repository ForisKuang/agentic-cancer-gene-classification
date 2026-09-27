# Agentic Cancer Gene Classification

M0 annotation engine for candidate cancer genes and gene fusions. The pipeline accepts either singleton gene symbols or fusions, resolves HGNC symbols, retrieves PubMed literature, and asks an LLM to produce structured cancer-gene annotations with verified PMID citations.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Optional services:

- `ONCOKB_API_TOKEN`: enables OncoKB membership lookups.
- `NCBI_API_KEY`: raises NCBI E-utilities rate limits.

Citation precision knobs:

- `MAX_PAPERS_FOR_SYNTHESIS`: selected PubMed records passed to synthesis.
- `MAX_CITATIONS_PER_ANNOTATION`: verified PMIDs retained in final output.

Default model stack:

- Full synthesis first calls `SYNTHESIS_FAST_MODEL`
  (`claude-haiku-4-5-20251001` by default) and escalates to
  `SYNTHESIS_MODEL` (`claude-opus-4-7` by default) when quality gates fail.
- Core synthesis uses the same fast-first path, capped by
  `CORE_SYNTHESIS_MAX_TOKENS`, `CORE_SYNTHESIS_ABSTRACT_CHARS`, and
  `CORE_SYNTHESIS_MAX_PAPERS`.
- Paper selection uses `SELECTION_MODEL` (`claude-haiku-4-5-20251001` by
  default) when the retrieved corpus is small enough for the selection pass.
- Tier 2 agentic PubMed query planning uses `RETRIEVAL_MODEL`
  (`claude-haiku-4-5-20251001` by default), decoupled from `SYNTHESIS_MODEL` —
  deciding what to search for doesn't need a frontier-tier model, and this loop
  can run several tool turns per gene. Its system prompt is also marked for
  prompt caching, since it's static and reused across every Tier 2 gene and turn.

Latency knobs:

- `ANNOTATION_GENE_CONCURRENCY`: number of genes annotated concurrently.
- `LLM_CONCURRENCY`: maximum concurrent LLM calls across retrieval, selection,
  and synthesis.
- `SYNTHESIS_MODEL_ESCALATION`: when true, use `SYNTHESIS_FAST_MODEL` first and
  call `SYNTHESIS_MODEL` only when the fast pass is missing core fields,
  under-cited, low-support, or sourced from Tier 2 retrieval.
- `CORE_SYNTHESIS_MAX_TOKENS`, `CORE_SYNTHESIS_ABSTRACT_CHARS`, and
  `CORE_SYNTHESIS_MAX_PAPERS`: keep core-mode synthesis compact so the first
  response focuses on cancer association, rationale, summary, and citations.
- `CORE_SYNTHESIS_ESCALATION_MIN_SUPPORT_SCORE` and
  `CORE_SYNTHESIS_ESCALATION_TIER2`: let core mode avoid deep-model escalation
  for complete fast-pass answers while full mode keeps stricter escalation.
- `PUBMED_STAGED_RETRIEVAL`: when true, Tier 1 PubMed retrieval stops after
  enough abstracts are found for synthesis instead of running every query family.
- `SELECTION_LLM_THRESHOLD`: only use the selection LLM when the retrieved
  corpus is at or below this size; larger corpora use deterministic ranking.

Not a config knob, but relevant to latency: all NCBI E-utilities calls share one
pooled `httpx.AsyncClient` (`_get_ncbi_client()` in `src/pipeline/literature.py`)
instead of opening a fresh client per call, so keep-alive connections amortize
the TCP+TLS handshake instead of paying it on every request. Request pacing to
NCBI is enforced by a token-bucket rate limiter (`_get_ncbi_rate_limiter()`,
3 requests/second without `NCBI_API_KEY`, 10/second with one, matching NCBI's
documented E-utilities limits) rather than a flat per-call sleep — bursts up to
the limit go through immediately instead of every call paying a fixed tax.
Similarly, when Claude's Tier 2 agentic retrieval emits several `search_pubmed`
calls in one turn, they're dispatched concurrently rather than awaited one at a
time.

## API Keys

Copy `.env.example` to `.env` and add only the keys you need for the mode you are
running.

- `ANTHROPIC_SDK_PROVIDER`: optional SDK transport. Use `anthropic` for the
  direct Anthropic API or `bedrock` for AWS Bedrock. Defaults to `anthropic`.
- `ANTHROPIC_API_KEY`: required when `ANTHROPIC_SDK_PROVIDER=anthropic`,
  including benchmark judge scoring. Create a Claude Console account, then
  generate a key from API Keys:
  https://platform.claude.com/settings/keys.
- `BEDROCK_AWS_DEFAULT_REGION`, `BEDROCK_AWS_ACCESS_KEY_ID`,
  `BEDROCK_AWS_SECRET_ACCESS_KEY`, `BEDROCK_AWS_SESSION_TOKEN`: used when
  `ANTHROPIC_SDK_PROVIDER=bedrock`. These mirror the LibreChat Bedrock env
  names. If explicit keys are omitted, the AWS credential chain/profile is used.
- `AWS_PROFILE` or `BEDROCK_AWS_PROFILE`: optional AWS profile for Bedrock.
- `BEDROCK_SYNTHESIS_MODEL`, `BEDROCK_SELECTION_MODEL`, and
  `BEDROCK_RETRIEVAL_MODEL`: optional Bedrock model IDs for ACGC's synthesis,
  selection, and Tier 2 retrieval-planning calls. Use these when the normal
  `SYNTHESIS_MODEL` / `SELECTION_MODEL` / `RETRIEVAL_MODEL` values are direct
  Anthropic model names.
- `BEDROCK_SYNTHESIS_FAST_MODEL`: optional Bedrock model ID for the lightweight
  synthesis pass when `SYNTHESIS_MODEL_ESCALATION=true`.
- `ONCOKB_API_TOKEN`: optional, but recommended for OncoKB membership lookups.
  OncoKB requires an account and data-access approval/license. After approval,
  the token is available in account settings:
  https://www.oncokb.org/account/settings. OncoKB API documentation:
  https://api.oncokb.org/oncokb-website/api.
- `NCBI_API_KEY`: optional. PubMed E-utilities work without it, but NCBI raises
  the limit from 3 requests/second to 10 requests/second when a key is supplied.
  Sign in to NCBI, open account settings, and create a key under API Key
  Management:
  https://support.nlm.nih.gov/kbArticle/?pn=KA-05317.

Do not commit `.env` or paste real keys into tracked files.

### Optional feature flags

| Setting | Default | Description |
| --- | --- | --- |
| `OPENEVIDENCE_ENABLED` | `false` | Shows an on-demand, non-blocking OpenEvidence guideline/trial evidence card per gene (`GET /v1/genes/{gene}/openevidence`); never part of core annotation or synthesis. |
| `PMID_DISTILLATION_ENABLED` | `false` | Distills retrieved abstracts in the background into the permanent MySQL `pmid_evidence` cache for reuse by later synthesis runs. |
| `FEEDBACK_ISSUE_CREATION_ENABLED` | `true` | Drafts and files curator feedback as GitHub issues; when `false`, feedback is still stored but no issue is drafted or created. |

Turning on OpenEvidence also needs these (see `.env.example`):

| Setting | Default | Description |
| --- | --- | --- |
| `OPENEVIDENCE_API_KEY` | _(empty)_ | Required for live calls; org-provisioned access (see https://github.com/oncokb/oe-api-exp). Cached results are served without it. |
| `OPENEVIDENCE_BASE_URL` | `https://api.openevidence.com` | API base URL. |
| `OPENEVIDENCE_MODEL` | `darwin` | OpenEvidence model name; also part of the cache key. |
| `OPENEVIDENCE_TIMEOUT_SECONDS` | `60` | Per-call timeout; a timeout is not retried and just hides the card. |
| `OPENEVIDENCE_CACHE_TTL_SECONDS` | `604800` | Redis TTL for cached analyses (one week). |
| `OPENEVIDENCE_SIDECAR_CONCURRENCY` | `3` | Max concurrent live OpenEvidence calls across all sidecar requests. |
| `OPENEVIDENCE_WARMUP_CONCURRENCY` | `5` | Concurrency for the offline `benchmarks/warm_openevidence_cache.py` warmup. |

## Run With Anthropic SDK

For the direct Anthropic API, set `ANTHROPIC_API_KEY` in `.env`:

```bash
ANTHROPIC_SDK_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

For AWS Bedrock, use the same `BEDROCK_AWS_*` env family as the LibreChat
deployment. This talks to Bedrock through the AWS credential chain (no static
API key) — locally, that means a SAML-federated session via `saml2aws login`
(or your org's equivalent), which writes temporary credentials to
`~/.aws/credentials` under a named profile. Point `AWS_PROFILE` /
`BEDROCK_AWS_PROFILE` at that same profile name:

```bash
ANTHROPIC_SDK_PROVIDER=bedrock
BEDROCK_AWS_DEFAULT_REGION=us-east-1
AWS_PROFILE=490004633549-SA
BEDROCK_SYNTHESIS_MODEL=us.anthropic.claude-sonnet-4-5-20250929-v1:0
BEDROCK_SYNTHESIS_FAST_MODEL=us.anthropic.claude-haiku-4-5-20251001-v1:0
BEDROCK_SELECTION_MODEL=us.anthropic.claude-haiku-4-5-20251001-v1:0
```

Model IDs use the `us.` cross-region inference profile prefix — the bare
`anthropic.<model>-v1:0` form is rejected by Bedrock for these models with
"on-demand throughput isn't supported".

Then run the CLI:

```bash
python -m src.cli \
  --fusions "ALK" \
  --output results.json \
  --output-csv results.csv
```

To prioritize the latency-sensitive fields (`cancer_associated`, rationale,
gene summary, citations, and evidence support), run in core mode:

```bash
python -m src.cli \
  --fusions "TP53::BRAF" \
  --mode core \
  --output results.json
```

To test multiple genes or fusions in one run, pass each input after `--fusions`:

```bash
python -m src.cli \
  --fusions "ALK" "TP53::BRAF" "ETV6::NTRK3" "BCR::ABL1" \
  --output results.json \
  --output-csv results.csv
```

For larger batches, put one gene or fusion per line in a text file:

```text
ALK
TP53::BRAF
ETV6::NTRK3
BCR::ABL1
```

Then run:

```bash
python -m src.cli \
  --input inputs.txt \
  --output results.json \
  --output-csv results.csv
```

Or run the API. To annotate one gene and return the same JSON payload used by a
result card:

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000
curl -X POST http://127.0.0.1:8000/v1/annotate/gene \
  -H "Content-Type: application/json" \
  -d '{"gene":"ALK","tumor_type":"LUAD"}'
```

The response is one `GeneAnnotation` object with fields such as `gene`,
`fusions`, `in_oncokb`, `cancer_associated`,
`cancer_association_rationale`, `cancer_type_prevalence`, `gene_class`,
`signaling_pathways`, `gene_summary`, `citations`, `supporting_quotes`,
`retrieval_count`, `retrieved_pmids`, `evidence_support_score`, and cache
metadata.

For batch runs or mixed genes and fusions, use `/v1/annotate`:

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000
curl -X POST http://127.0.0.1:8000/v1/annotate \
  -H "Content-Type: application/json" \
  -d '{"fusions":["ALK",{"fusion":"EML4::ALK","tumor_type":"LUAD"}],"mode":"core"}'
```

To save compute for known curated genes, set `skip_literature_for_oncokb` to
`true`. Genes confirmed present in OncoKB return a deterministic OncoKB-based
annotation without PubMed retrieval or LLM synthesis; genes absent from OncoKB
continue through the normal literature retrieval path.

```bash
curl -X POST http://127.0.0.1:8000/v1/annotate \
  -H "Content-Type: application/json" \
  -d '{"fusions":["BRAF","TP53"],"skip_literature_for_oncokb":true}'
```

For progressive delivery, create an annotation job and poll its status. The
status response includes completed gene annotations as they finish:

```bash
curl -X POST http://127.0.0.1:8000/v1/annotate/jobs \
  -H "Content-Type: application/json" \
  -d '{"fusions":["TP53::BRAF","ETV6::NTRK3"],"mode":"core"}'

curl http://127.0.0.1:8000/v1/annotate/jobs/{job_id}
```

Core results can be enriched lazily after the first response returns. The UI
exposes this through **Enrich results**; API callers can submit returned
annotations to the enrichment job endpoint:

```bash
curl -X POST http://127.0.0.1:8000/v1/annotate/enrichment/jobs \
  -H "Content-Type: application/json" \
  -d '{"annotations":[{"gene":"TP53","fusions":["TP53::BRAF"],"cancer_associated":true,"citations":["12345"]}]}'

curl http://127.0.0.1:8000/v1/annotate/enrichment/jobs/{job_id}
```

Enrichment runs full retrieval/synthesis off the core request path and streams
expanded annotations with fields such as supporting quotes, pathway/class
context, prevalence context, and fuller summaries.

Enriched/full annotations also include deterministic review metadata:

- `evidence_cards`: one card per verified citation with PMID, title, journal,
  evidence type, selected reason, and a supporting excerpt when available.
- `quality_flags`: review-priority badges for no verified citations, low
  evidence score, Tier 2 retrieval, deep-model escalation, and mixed/contextual
  evidence language.
- `clinical_actionability`: omitted unless ACGC finds high-confidence,
  literature-derived therapeutic precedent from verified cited PMIDs. This is
  not a treatment recommendation. When present, it is stored with the initial
  annotation result and includes score components so the UI can lazily expand
  the derivation without another API/model call.
- Cache provenance stays on each annotation through `cache_status` and
  `cache_reason`; the UI renders these as badges such as freshly computed,
  cached final, cached after PubMed check, or force refreshed.

These fields are computed from existing retrieval/synthesis metadata. They do
not add extra calls to the core annotation request.

Clinical actionability confidence is deterministic and citation-bound. A result
is surfaced only when the score is at least 0.85. The score starts from verified
cited abstracts only and applies this rubric:

- +0.35 for direct human clinical evidence.
- +0.25 for drug/domain/actionability language tied to the queried gene.
- +0.15 when the supporting abstract matches the query tumor type.
- +0.15 for two or more independent supporting PMIDs.
- +0.05 for a high-impact journal signal.
- +0.20 when OncoKB membership corroborates the gene context.
- -0.25 for preclinical-only support.
- -0.15 for case-report-only support.
- -0.30 for conflicting or negative-response language.

If a candidate lacks verified cited PMID support, is review-only, is
preclinical-only below the threshold, or depends on indirect pathway inference,
the field stays absent.

Each surfaced actionability result stores `score_components` with the component
code, label, score delta, PMIDs, and detail text such as matched therapies or
matched domain terms. This makes the confidence calculation auditable in JSON,
CSV, and the UI.

This path uses the Anthropic SDK for selection, synthesis, benchmark judging, and
Tier 2 agentic retrieval. In Bedrock mode, direct Anthropic model names must be
replaced with Bedrock model IDs via `BEDROCK_SYNTHESIS_MODEL`,
`BEDROCK_SYNTHESIS_FAST_MODEL`, `BEDROCK_SELECTION_MODEL`, and
`BEDROCK_RETRIEVAL_MODEL`, or by setting `SYNTHESIS_MODEL`,
`SYNTHESIS_FAST_MODEL`, `SELECTION_MODEL`, and `RETRIEVAL_MODEL` to Bedrock IDs
directly.

## Latency Comparison

The comparison runner executes the same input set twice:

- baseline-like settings: sequential genes, broad Tier 1 retrieval, selection
  LLM enabled
- optimized settings: configured concurrency, staged Tier 1 retrieval, and
  selection skipping for large corpora

```bash
python -m benchmarks.compare_latency \
  --fusions "TP53::BRAF" "ETV6::NTRK3" \
  --mode core \
  --output latency_report.json
```

The report includes run-level timings, per-gene retrieval/selection/synthesis
timings, and total milliseconds plus percent savings.

## Synthesis Paper Count A/B

Use the synthesis-paper-count runner to compare whether passing more selected
abstracts into synthesis improves rationale support or high-confidence
clinical-actionability findings:

```bash
python -m benchmarks.compare_synthesis_paper_counts \
  --fusions "HAPSTR1::ABAT" "EML4::ALK" \
  --paper-counts 4 8 \
  --mode full \
  --output synthesis_ab_report.json
```

The report includes per-arm timings, average evidence-support score, verified
citation counts, insufficient-evidence counts, high-confidence
clinical-actionability counts, deep-model escalation counts, and per-gene
deltas between the smallest and largest paper-count arms.

Initial smoke benchmark on `EML4::ALK` and `HAPSTR1::ABAT` supported keeping
full-mode synthesis context at 8 selected papers. Moving from 4 to 8 synthesis
papers improved average evidence-support score from 0.65 to 0.75 and average
verified citation count from 3.5 to 3.75, with no change in high-confidence
clinical-actionability count — most genes in that sample didn't have many
strong candidates beyond ~4, so raising the synthesis-context size alone had
a modest effect on citation count. `MAX_CITATIONS_PER_ANNOTATION` now defaults
to 8, matching `MAX_PAPERS_FOR_SYNTHESIS`, so a gene that genuinely does have
more than 4 strong supporting papers isn't artificially capped below what was
retrieved and considered.

## Output Files

- `--output results.json`: full structured pipeline result, including run
  metadata and one `GeneAnnotation` object per annotated gene.
- `--output-csv results.csv`: curator-facing spreadsheet export with one row per
  gene. This file is intended for direct Google Sheets import.

The CSV keeps the core annotation fields from the JSON and adds
`publication_links`, a semicolon-separated list of PubMed URLs derived from the
verified PMID citations. Multi-value fields such as `fusions`, `citations`, and
`publication_links` are semicolon-separated so they stay in one spreadsheet cell.
Unknown optional values are left blank.

`evidence_support_score` is an explainable 0.0-1.0 grounding score calculated by
ACGC after PMID verification. It estimates how strongly the generated annotation
is supported by retrieved literature and verified citations; it is not a
calibrated probability of biological truth or clinical actionability.

PubMed retrieval excludes records marked as `Retracted Publication` or
`Retraction of Publication`, and citation verification ignores any retracted
records that reach synthesis through cached or test fixtures.

Fusion-level evidence is intentionally outside the core annotation critical
path. The UI starts `/v1/fusion-evidence/jobs` after gene annotations are
available, polls it in the background, and displays results in a separate
Fusion evidence tab. Each exact fusion/tumor lookup is cached in Redis under
`fusion_evidence:*` using `FUSION_EVIDENCE_CACHE_TTL_SECONDS`, with bounded
per-job fan-out controlled by `FUSION_EVIDENCE_CONCURRENCY`.

When a gene is marked `insufficient_evidence`, the JSON result still includes
`retrieved_pmids` and any grounded `gene_summary` produced from the retrieved
abstracts. The UI surfaces those fields in the No result tab so researchers can
open the candidate papers and make their own judgment.

To import into Google Sheets, create or open a sheet, use **File > Import**,
upload `results.csv`, and choose whether to insert it as a new sheet or append it
to an existing sheet. Treat this CSV as a review artifact; it does not write back
to the project source-of-truth sheet.

## Run Locally Without Anthropic API Credits

Use `--local [BACKEND]` to route LLM calls through a locally authenticated agent CLI.

Supported backends:

- `codex`
- `claude-code`
- `antigravity`

Bare `--local` defaults to `claude-code`.

Examples:

```bash
python -m src.cli \
  --fusions "ALK" \
  --skip-literature-for-oncokb \
  --output results.json

python -m src.cli \
  --fusions "ALK" \
  --local codex \
  --output results.json \
  --output-csv results.csv

python -m src.cli \
  --fusions "ALK" \
  --local claude-code \
  --output results.json \
  --output-csv results.csv

python -m src.cli \
  --fusions "ALK" \
  --local antigravity \
  --output results.json \
  --output-csv results.csv
```

For local mode, make sure the selected tool is installed and logged in on the host:

```bash
codex --version
claude --version
```

Antigravity defaults to:

```bash
antigravity -p {prompt}
```

Override that command shape if needed:

```bash
ANTIGRAVITY_LOCAL_COMMAND='your-command {prompt}' \
  python -m src.cli --fusions "ALK" --local antigravity
```

## Docker

Default API image:

```bash
docker compose up --build annotation-service
curl http://127.0.0.1:8000/health
```

CI publishes a fresh image to GitHub Container Registry on every merge to
`main` — no manual build/handoff needed. IT (or any deploy tooling) pulls it
directly:

```bash
docker pull ghcr.io/oncokb/agentic-cancer-gene-classification:latest
```

For a reproducible, pinned deploy, use the commit-SHA tag instead of
`:latest` — find it at
[the package's tag list](https://github.com/oncokb/agentic-cancer-gene-classification/pkgs/container/agentic-cancer-gene-classification):

```bash
docker pull ghcr.io/oncokb/agentic-cancer-gene-classification:main-<short-sha>
```

The default Docker service uses the Anthropic SDK path. Provide
`ANTHROPIC_API_KEY` in a local `.env` for direct Anthropic Compose-based
annotation calls, or set `ANTHROPIC_SDK_PROVIDER=bedrock` plus AWS Bedrock env
vars for Bedrock calls. For Kubernetes or Argo CD deployments, keep `.env` out
of git and provide runtime configuration through Kubernetes Secrets, External
Secrets, or the cluster's approved secret-management path.

The Docker image includes Datadog Python tracing, starts the API with
`ddtrace-run`, and includes Datadog correlation fields in stdout logs. In Argo
CD/Kubernetes, follow the existing cBioPortal/OncoKB Datadog convention by
enabling the Datadog admission controller on the pod and setting service
tags/env vars at deploy time:

```yaml
metadata:
  labels:
    tags.datadoghq.com/env: <cluster-env>
    tags.datadoghq.com/service: agentic-cancer-gene-classification
    tags.datadoghq.com/version: <image-tag-or-git-sha>
    admission.datadoghq.com/enabled: "true"
```

```yaml
env:
  - name: DD_ENV
    value: <cluster-env>
  - name: DD_SERVICE
    value: agentic-cancer-gene-classification
  - name: DD_VERSION
    value: <image-tag-or-git-sha>
  - name: DD_LOGS_INJECTION
    value: "true"
  - name: DD_TRACE_ENABLED
    value: "true"
  - name: DD_APM_ENABLED
    value: "true"
```

Local Docker Compose sets `DD_TRACE_ENABLED=false` so development containers do
not attempt to send traces to a missing local Datadog Agent. Override that env
var if you are testing with a local Agent.

The app also emits optional DogStatsD product metrics for dashboarding. Enable
them only when the pod/container can reach a Datadog Agent:

```bash
DATADOG_METRICS_ENABLED=true
DATADOG_METRICS_NAMESPACE=acgc
```

Leave `DATADOG_STATSD_HOST`/`DATADOG_STATSD_PORT` unset unless you need to pin
a fixed UDP target — by default the client falls through to DogStatsd's own
`DD_DOGSTATSD_URL`/`DD_AGENT_HOST`/`DD_DOGSTATSD_PORT` env var detection,
which is what resolves to the Datadog Agent's Unix domain socket in this
cluster (the same env vars the Datadog admission controller already injects
for APM tracing).

Metrics emitted:

- `acgc.users.active`: unique hashed users seen per DogStatsD flush interval.
  The app reads the identity from `DATADOG_USER_ID_HEADER`, default `x-user-id`.
- `acgc.users.anonymous_requests`: annotation requests without a user header.
- `acgc.inputs.submitted`: number of submitted genes/fusions.
- `acgc.genes.queried`: number of unique normalized genes derived from the
  submitted inputs. This is the primary "genes queried" metric.
- `acgc.genes.annotated`: completed gene annotations, tagged with
  `cache_status` (`reused`/`refreshed`/`bypassed`) and `is_fusion` — the
  primary cache hit-rate / fresh-query-rate signal.
- `acgc.genes.errors`: gene annotations that ended with an error field.
- `acgc.pipeline.runs`: annotation pipeline runs.
- `acgc.pipeline.duration_ms`, `acgc.gene.annotation.duration_ms`, and
  `acgc.gene.total_duration_ms`: duration distributions. The last is tagged
  with `cache_status`/`is_fusion` and covers the whole per-gene path
  (including the cache lookup), so cache-hit vs. fresh-compute latency can
  be compared directly.
- `acgc.redis_cache.lookups`: Redis cache-aside lookups (HGNC/OncoKB/PubMed),
  tagged with `cache_name` (the key prefix, e.g. `hgnc`/`oncokb`/`pubmed`) and
  `result` (`hit`/`miss`/`error`) — the Redis-layer hit rate, distinct from
  the MySQL-backed gene annotation reuse above.
- `acgc.feedback.submitted`: curator feedback submissions, tagged with
  `category`.
- `acgc.feedback.llm_draft_failed`: feedback issue drafts that fell back to
  the non-LLM template because the LLM call failed.
- `acgc.feedback.github_issue_created` /
  `acgc.feedback.github_issue_creation_failed` /
  `acgc.feedback.github_issue_creation_skipped`: outcomes of filing the
  drafted issue via the GitHub API.
- `acgc.llm.requests`: every Anthropic/Bedrock Messages API call, tagged with
  `model` (the resolved model ID actually billed) and `model_purpose`
  (`synthesis`/`synthesis_fast`/`selection`/`retrieval`/`retrieval_agentic`) —
  the two axes Anthropic/Bedrock bill on, so LLM spend can be broken down by
  model and by feature. Local-backend calls (`--local`) aren't SDK calls and
  don't emit this.
- `acgc.llm.tokens.input` / `acgc.llm.tokens.output`: per-call token counts
  from the API response's `usage` block, same tags as `llm.requests`.
- `acgc.llm.tokens.cache_creation` / `acgc.llm.tokens.cache_read`: prompt
  cache token usage — Anthropic bills cache reads far cheaper than a fresh
  input token, so a low cache-read share relative to cache-creation points at
  wasted spend on repeated system prompts that aren't hitting cache.

Metric tags are intentionally low-cardinality: `mode`, `local_backend`,
`tumor_type_present`, `cache_status`, `is_fusion`, `cache_name`, `result`,
`category`, `model`, and `model_purpose`. Raw user IDs, gene symbols, and
full cache keys are never metric tags.

Quick verification:

```bash
curl -X POST http://127.0.0.1:8000/v1/annotate \
  -H "Content-Type: application/json" \
  -H "x-user-id: curator@example.com" \
  -d '{"fusions":["TP53::BRAF","ETV6::NTRK3"],"mode":"core"}'
```

Then check Metrics Explorer for `acgc.genes.queried`, `acgc.users.active`, and
`acgc.pipeline.runs`; check APM for spans named `acgc.pipeline.normalize` and
`acgc.gene.annotate`.

Suggested dashboard widgets:

- Active users: `sum:acgc.users.active{*}.rollup(sum)`
- Genes queried: `sum:acgc.genes.queried{*}.as_count()`
- Inputs submitted: `sum:acgc.inputs.submitted{*}.as_count()`
- Pipeline runs: `sum:acgc.pipeline.runs{*}.as_count()`
- Gene errors: `sum:acgc.genes.errors{*}.as_count()`
- Pipeline latency p95: `p95:acgc.pipeline.duration_ms{*}`
- Gene latency p95: `p95:acgc.gene.annotation.duration_ms{*}`

Dockerized local Codex:

```bash
docker compose --profile local up --build annotation-service-local
curl -X POST http://127.0.0.1:8001/v1/annotate \
  -H "Content-Type: application/json" \
  -d '{"fusions":["ALK","TP53::BRAF"],"local_backend":"codex"}'
```

The local Docker profile builds the image with:

```bash
docker build --build-arg INSTALL_LOCAL_AGENTS=true .
```

It installs Linux-native `@openai/codex` and `@anthropic-ai/claude-code`, then mounts host local-agent config:

```text
${HOME}/.codex      -> /home/appuser/.codex
${HOME}/.claude     -> /home/appuser/.claude
${HOME}/.claude.json -> /home/appuser/.claude.json
```

Dockerized Codex has been validated end-to-end with host `~/.codex` mounted. Dockerized Claude Code may require logging in inside the container; mounting host Claude config did not reliably carry the login session into Linux during testing.

## MySQL Run Store And Gene Cache

When MySQL is configured, each successful annotation request is saved in the
`runs` table by `run_id`. Curators can share a run ID and peers can retrieve the
same result without recomputing it:

```bash
curl http://127.0.0.1:8000/v1/annotate/<run_id>
```

The same store also keeps the latest reusable annotation per canonical gene and
tumor-type context in `gene_annotations`. This is separate from run sharing:
`runs` preserves exact request/response history, while `gene_annotations`
reduces future LLM/PubMed work for genes that are still fresh enough. Tumor type
is part of the reusable cache key, so `BRAF` with `melanoma` and `BRAF` with
`LUAD` are cached independently; requests without tumor type use a general
empty tumor-type bucket.

Default freshness policy:

- Any non-error final gene annotation is reused for 180 days before the PubMed
  freshness probe runs. This is the aggressive latency path for repeated genes.
- OncoKB-positive cached genes are reused for 90 days before a lightweight PubMed
  freshness check.
- High evidence support (`evidence_support_score >= 0.8`) is reused for 60 days.
- Medium evidence support (`>= 0.5`) is reused for 30 days.
- Low support or insufficient-evidence annotations are reused for 14 days.
- Stale cache entries trigger a cheap PubMed PMID-only search for papers newer
  than `last_pubmed_checked_at`. If no new PMIDs are found, the cache is reused
  and `last_pubmed_checked_at` is updated. If new PMIDs are found, the gene is
  rerun through retrieval and synthesis.
- Requests can set `force_refresh: true` to bypass gene cache reuse.

Warm the Tier 1 PubMed literature cache ahead of benchmark or batch runs:

```bash
python -m benchmarks.warm_literature_cache \
  --fusions "TP53::BRAF" "ETV6::NTRK3" \
  --output benchmarks/results/literature_warmup.json
```

The warmer only runs Tier 1 PubMed retrieval, so it populates Redis-backed
`esearch`/`efetch` cache entries without invoking Tier 2 agentic retrieval.

## Benchmark

Run the benchmark with API credits:

```bash
python -m benchmarks.run_benchmark \
  --route local \
  --mode core \
  --output benchmark_report.json \
  --results-csv benchmark_results.csv
```

Run the benchmark locally and skip the SDK judge call:

```bash
python -m benchmarks.run_benchmark \
  --route local \
  --local codex \
  --no-judge \
  --output benchmark_report.json \
  --results-csv benchmark_results.csv
```

`--no-judge` is required for a fully credit-free benchmark run because `benchmarks/judge.py` still uses the Anthropic SDK for LLM-as-judge summary scoring.

Benchmark outputs:

- `--output benchmark_report.json`: metrics, per-gene deltas, optional judge
  results, and the raw `pipeline_result`.
- `--results-csv benchmark_results.csv`: one-row-per-gene CSV generated from the
  same `pipeline_result` used for metric scoring.

## Tests

```bash
pytest
ruff check src benchmarks tests
docker compose --profile local config --quiet
```
