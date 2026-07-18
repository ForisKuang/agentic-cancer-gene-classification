# Agentic Cancer Gene Classification

M0 annotation engine for candidate cancer gene fusions. The pipeline splits fusions into genes, resolves HGNC symbols, retrieves PubMed literature, and asks an LLM to produce structured cancer-gene annotations with verified PMID citations.

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

Context budget knobs:

- `SELECTION_PREFILTER_LIMIT`: deterministic top records retained before LLM
  relevance selection. Defaults to `24`.
- `SELECTION_CHUNK_SIZE`: number of candidate papers sent per relevance-selection
  call. Defaults to `10`.
- `SELECTION_CHUNK_KEEP`: PMIDs retained from each selection chunk before the
  final merge. Defaults to `3`.
- `SYNTHESIS_EVIDENCE_MAX_CHARS`: fallback compact evidence text per paper when
  per-paper evidence extraction fails. Defaults to `900`.
- `LOCAL_TIER2_MIN_PREFILTER_SCORE`: in local mode, skip Tier 2 exploratory
  retrieval when Tier 1 records have weaker deterministic relevance than this
  threshold. Defaults to `2`.

Latency knob:

- `MAX_GENE_ANNOTATION_CONCURRENCY`: number of genes annotated concurrently.
  Defaults to `3`. Higher values can reduce batch latency, but they also create
  more simultaneous SDK calls or local-agent processes.
- `ONCOKB_GENE_CACHE_TTL_HOURS`: hours to reuse the local OncoKB curated-gene
  cache before refreshing. Defaults to `24`; set to `0` to disable persistent
  OncoKB gene-list caching.

## API Keys

Copy `.env.example` to `.env` and add only the keys you need for the mode you are
running.

- `ANTHROPIC_API_KEY`: required for Anthropic SDK mode and for benchmark judge
  scoring. Create a Claude Console account, then generate a key from API Keys:
  https://platform.claude.com/settings/keys.
- `ONCOKB_API_TOKEN`: optional, but recommended for OncoKB membership lookups.
  OncoKB requires an account and data-access approval/license. After approval,
  the token is available in account settings. Non-technical users can paste this
  token in the UI setup screen. Engineers can also set this value in `.env`.
  OncoKB account settings: https://www.oncokb.org/account/settings. OncoKB API
  documentation: https://api.oncokb.org/oncokb-website/api.
- `NCBI_API_KEY`: optional. PubMed E-utilities work without it, but NCBI raises
  the limit from 3 requests/second to 10 requests/second when a key is supplied.
  Non-technical users can paste this key in the UI setup screen. Engineers can
  also set this value in `.env`. Sign in to NCBI, open account settings, and
  create a key under API Key Management:
  https://support.nlm.nih.gov/kbArticle/?pn=KA-05317.
- `GOOGLE_SERVICE_ACCOUNT_JSON`: optional. Enables direct Google Sheets export
  from the browser UI. Create a Google Cloud service account, enable the Google
  Sheets API, and download the service account JSON key. Non-technical users can
  upload that JSON from the UI setup screen. Engineers can also set this value to
  a local JSON file path in `.env`. Share the target spreadsheet with the service
  account email before exporting.

Do not commit `.env` or paste real keys into tracked files.

## Run With Anthropic API Key

Set `ANTHROPIC_API_KEY` in `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

Then run the CLI:

```bash
python -m src.cli \
  --fusions "TP53::BRAF" \
  --output results.json \
  --output-csv results.csv
```

To test multiple fusions in one run, pass each fusion after `--fusions`:

```bash
python -m src.cli \
  --fusions "TP53::BRAF" "ETV6::NTRK3" "BCR::ABL1" \
  --output results.json \
  --output-csv results.csv
```

For larger batches, put one fusion per line in a text file:

```text
TP53::BRAF
ETV6::NTRK3
BCR::ABL1
```

Then run:

```bash
python -m src.cli \
  --input fusions.txt \
  --output results.json \
  --output-csv results.csv
```

Or run the API:

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000
curl -X POST http://127.0.0.1:8000/v1/annotate \
  -H "Content-Type: application/json" \
  -d '{"fusions":["TP53::BRAF"]}'
```

This path uses the Anthropic SDK for selection, synthesis, and Tier 2 agentic retrieval.

## Output Files

- `--output results.json`: full structured pipeline result, including run
  metadata and one `GeneAnnotation` object per annotated gene.
- `--output-csv results.csv`: curator-facing spreadsheet export with one row per
  gene. This file is intended for direct Google Sheets import.

The CSV keeps the core annotation fields from the JSON and separates supporting
citations from the broader retrieval set. `supporting_citation_pmids` and
`supporting_citation_publication_links` are the capped PMIDs/links actually used
as evidence in the generated annotation. `retrieved_pmids`, which appears after
`retrieval_count`, lists every PMID retrieved before selection/synthesis so a
curator can perform additional due diligence. Multi-value fields such as
`fusions`, `supporting_citation_pmids`, and `retrieved_pmids` are
semicolon-separated so they stay in one spreadsheet cell. Unknown optional values
are left blank.

To import into Google Sheets, create or open a sheet, use **File > Import**,
upload `results.csv`, and choose whether to insert it as a new sheet or append it
to an existing sheet. Treat this CSV as a review artifact; it does not write back
to the project source-of-truth sheet.

The browser UI can also export reviewed results directly to Google Sheets. Open
the setup screen, upload a Google service account JSON key, share the target
spreadsheet with the service account email shown by the UI, then use **Export
Google Sheet** after reviewing results. Engineers may alternatively set
`GOOGLE_SERVICE_ACCOUNT_JSON` in `.env`. The app asks for the spreadsheet ID or
URL and a tab name. It creates the tab if needed, then replaces that tab's
contents with the same columns used by the CSV export. Use a dedicated review
spreadsheet or review tab rather than the source-of-truth sheet unless the team
explicitly approves overwriting that tab.

Google account OAuth is possible, but it requires a separate OAuth client,
consent screen, local token storage, and potentially app verification for Sheets
scopes. For this local desktop workflow, the service account path is simpler and
keeps write access limited to spreadsheets explicitly shared with that service
account.

## Browser UI

The preferred non-terminal workflow is to use the packaged executable for your
operating system. When launched, it starts the local UI server on an available
localhost port and opens the browser automatically.

For development, the same launcher can be run from the installed Python
environment:

```bash
annotation-ui
```

To build a distributable executable on the current OS:

```bash
pip install -e ".[desktop]"
python scripts/build_desktop_app.py
```

The artifact is written under `dist/`:

- macOS/Linux: `dist/GeneFusionAnnotator`
- Windows: `dist/GeneFusionAnnotator.exe`

Builds must be produced on the target OS, so a macOS build creates a macOS
executable, a Windows build creates a Windows `.exe`, and a Linux build creates
a Linux executable. Add `--windowed` to hide the console window where supported:

```bash
python scripts/build_desktop_app.py --windowed
```

For macOS end-user testing, build a drag-and-drop disk image instead of sending
the raw executable:

```bash
python scripts/build_desktop_app.py --format dmg
```

This writes a file like `dist/GeneFusionAnnotator-darwin-arm64.dmg`. Upload that
DMG to Google Drive, GitHub Releases, or another internal file host, then send
users the link. The DMG contains `GeneFusionAnnotator.app`, an Applications
shortcut, and a short README. API keys are not bundled into the artifact; users
configure tokens from the setup screen after launching the app.

The current DMG is unsigned. For internal beta testing, users may need to
right-click the installed app and choose **Open** the first time. For broader
distribution, sign and notarize the app with an Apple Developer ID before
sharing it with nontechnical users.

The FastAPI service can still be run directly for API development:

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000
```

Open http://127.0.0.1:8000 in a browser.

The UI supports:

- Showing a first-time guide that explains the workflow and why each token or
  local agent setup is used.
- Pasting one or more fusions into a text area, one per line.
- Choosing Anthropic SDK mode, local Codex, local Claude Code, local
  GitHub Copilot, or local Antigravity.
- Reading a short mode description that distinguishes new annotation runs from
  holdout benchmark runs.
- Running the annotation pipeline and reviewing one editable card per gene as
  each gene finishes.
- Showing model/local-agent token, quota, or usage-limit errors in the UI while
  preserving any completed gene cards for review/export.
- Exporting the proofread result as JSON, CSV, or directly to a Google Sheet.
- Pasting an OncoKB API token in setup so annotation runs can perform OncoKB
  membership lookups without editing `.env`.
- Pasting an NCBI API key in setup so PubMed retrieval uses higher E-utilities
  rate limits without editing `.env`.
- Running the benchmark from the same page, with an option to skip LLM judge
  scoring for local/credit-free runs.

On first load, the UI opens Settings with a tutorial that explains the overall
workflow, execution options, OncoKB token, NCBI key, and export flow. It also
checks whether `codex`, `claude`, `copilot`, or `antigravity` is available on
the server running FastAPI. If no supported local backend is detected, Settings
shows links and commands for Codex, Claude Code, GitHub Copilot, and
Antigravity. The setup panel remains available from the sidebar.

The UI opens Settings automatically until the minimum setup is complete. A user
needs one execution path, either `ANTHROPIC_API_KEY` for Anthropic SDK mode or an
installed local agent, plus an OncoKB API token for membership lookup.

The OncoKB curated-gene list is cached locally after the first successful token
authenticated lookup. Subsequent app runs reuse that cache until
`ONCOKB_GENE_CACHE_TTL_HOURS` expires, which avoids repeatedly downloading the
same gene list during normal local testing.

When at least one local backend is installed, the execution dropdown defaults to
the first detected local backend instead of Anthropic SDK. Users can still select
Anthropic SDK explicitly when they want to run with `ANTHROPIC_API_KEY`. The Run
panel does not list every missing local agent; if a user selects a local backend
that is not installed, the UI shows a targeted message and points them to
Settings.

For non-technical users, the setup screen also includes install buttons for
Codex, Claude Code, and GitHub Copilot on supported operating systems. These
buttons run only the official, allowlisted installer for the selected backend:

- Codex on macOS/Linux:
  `curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh`
- Codex on Windows PowerShell:
  `$env:CODEX_NON_INTERACTIVE=1; irm https://chatgpt.com/codex/install.ps1 | iex`
- Claude Code on macOS/Linux:
  `curl -fsSL https://claude.ai/install.sh | bash`
- Claude Code on Windows PowerShell:
  `irm https://claude.ai/install.ps1 | iex`
- GitHub Copilot on macOS/Linux:
  `curl -fsSL https://gh.io/copilot-install | bash`
- GitHub Copilot on Windows:
  `winget install GitHub.Copilot`

The install endpoint is restricted to localhost clients. Installation does not
complete account authentication; after installing, the user still needs to sign
in with `codex login` or by running `claude` and following the provider's browser
prompts. For GitHub Copilot, the setup screen can invoke `copilot login` from
the local machine. Copilot login uses GitHub's browser/device auth flow, so any
organization SSO requirement is handled by GitHub rather than by this app.

The standalone macOS app does not inherit the same shell `PATH` that Terminal
uses. To avoid false missing-tool errors, the app checks common install
locations such as `~/.local/bin`, `/opt/homebrew/bin`, and `/usr/local/bin`, and
it runs local agents by absolute path when possible. If a local agent was
installed somewhere custom, set one of these overrides before launching the app:
`AGCG_CLAUDE_CODE_PATH`, `AGCG_CODEX_PATH`, `AGCG_COPILOT_PATH`, or
`AGCG_ANTIGRAVITY_PATH`.

Antigravity is also available as a local execution path. Because there is no
documented stable headless installer/login command in the public setup surface,
the setup screen opens the official Antigravity setup page instead of running an
installer. After Antigravity is installed and signed in, the app detects the
`antigravity` command and can route runs through `--local antigravity`.

Local setup references:

- Codex CLI: https://learn.chatgpt.com/docs/codex/cli. Sign in with
  `codex login`, or use API-key auth with
  `printenv OPENAI_API_KEY | codex login --with-api-key`.
- Claude Code: https://code.claude.com/docs/en/setup. Install with the native
  installer or package manager, then run `claude` to authenticate and
  `claude --version` to verify.
- GitHub Copilot CLI:
  https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli.
  Install the CLI, then run `copilot login` or use the UI login button. Copilot
  requires an active Copilot plan and may require organization policy approval.
  It can also use `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, or an
  authenticated GitHub CLI session.
- Antigravity: https://antigravity.google/. Install and sign in through the
  official app flow, then refresh local backend status in this UI.

## Run Locally Without Anthropic API Credits

Use `--local [BACKEND]` to route LLM calls through a locally authenticated agent CLI.

Supported backends:

- `codex`
- `claude-code`
- `copilot`
- `antigravity`

Bare `--local` defaults to `claude-code`.

When a run contains multiple unique genes, the pipeline annotates genes in
parallel up to `MAX_GENE_ANNOTATION_CONCURRENCY`. In local mode this means
multiple local agent CLI invocations may run at once.

Examples:

```bash
python -m src.cli \
  --fusions "TP53::BRAF" \
  --local codex \
  --output results.json \
  --output-csv results.csv

python -m src.cli \
  --fusions "TP53::BRAF" \
  --local claude-code \
  --output results.json \
  --output-csv results.csv

python -m src.cli \
  --fusions "TP53::BRAF" \
  --local copilot \
  --output results.json \
  --output-csv results.csv

python -m src.cli \
  --fusions "TP53::BRAF" \
  --local antigravity \
  --output results.json \
  --output-csv results.csv
```

For local mode, make sure the selected tool is installed and logged in on the host:

```bash
codex --version
claude --version
copilot version
```

If Terminal can run a tool but the standalone app cannot, the CLI is probably in
a shell-only path. The app searches common user and Homebrew locations, but a
custom install can be pointed at explicitly:

```bash
AGCG_CLAUDE_CODE_PATH="$HOME/.local/bin/claude" \
  python -m src.cli --fusions "TP53::BRAF" --local claude-code
```

Antigravity defaults to:

```bash
antigravity -p {prompt}
```

Override that command shape if needed:

```bash
ANTIGRAVITY_LOCAL_COMMAND='your-command {prompt}' \
  python -m src.cli --fusions "TP53::BRAF" --local antigravity
```

## Docker

Default API image:

```bash
docker compose up --build annotation-service
curl http://127.0.0.1:8000/health
```

The default Docker service uses the Anthropic SDK path. Provide `ANTHROPIC_API_KEY` in `.env` for annotation calls.

Dockerized local Codex:

```bash
docker compose --profile local up --build annotation-service-local
curl -X POST http://127.0.0.1:8001/v1/annotate \
  -H "Content-Type: application/json" \
  -d '{"fusions":["TP53::BRAF"],"local_backend":"codex"}'
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

## Benchmark

Run the benchmark with API credits:

```bash
python -m benchmarks.run_benchmark \
  --output benchmark_report.json \
  --results-csv benchmark_results.csv
```

Run the benchmark locally and skip the SDK judge call:

```bash
python -m benchmarks.run_benchmark \
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
