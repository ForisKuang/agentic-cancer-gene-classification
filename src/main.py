"""
FastAPI application — manually invokable, Docker/K8s-ready.
"""

from __future__ import annotations

import logging
import platform
import sys
import json
from pathlib import Path
from subprocess import TimeoutExpired, run
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.models.schema import (
    AnnotateRequest,
    AnnotationResult,
    BenchmarkRequest,
    BenchmarkResult,
    GoogleSheetExportRequest,
    GoogleSheetExportResponse,
    GoogleSheetsConfigStatus,
    GoogleSheetsServiceAccountConfigRequest,
    GoogleSheetsServiceAccountConfigResponse,
    LocalBackendInstallRequest,
    LocalBackendInstallResponse,
    LocalBackendInstallerInfo,
    LocalBackendLoginRequest,
    LocalBackendLoginResponse,
    LocalBackendsStatusResponse,
    LocalBackendStatus,
    NCBIAPIKeyConfigRequest,
    NCBIAPIKeyConfigResponse,
    NCBIConfigStatus,
    OncoKBConfigStatus,
    OncoKBTokenConfigRequest,
    OncoKBTokenConfigResponse,
)
from src.pipeline.db_lookups import (
    OncoKBConfigurationError,
    oncokb_config_status,
    save_oncokb_api_token,
)
from src.pipeline.google_sheets_export import (
    GoogleSheetsExportError,
    export_annotation_results_to_google_sheet,
    google_sheets_config_status,
    save_service_account_credentials,
)
from src.pipeline.local_agents import (
    LOCAL_BACKEND_COMMANDS,
    LOCAL_BACKEND_VERSION_ARGS,
    resolve_local_agent_path,
)
from src.pipeline.llm_client import LOCAL_BACKENDS
from src.pipeline.literature import ncbi_config_status, save_ncbi_api_key
from src.pipeline.orchestrator import iter_pipeline_events, run_pipeline
from src.pipeline.results_export import annotation_results_csv_text
from src.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
LOCAL_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
INSTALL_TIMEOUT_SECONDS = 600
LOGIN_TIMEOUT_SECONDS = 600

app = FastAPI(
    title="Agentic Cancer Gene Classification",
    description=(
        "M0: LLM annotation engine for candidate cancer gene fusions. "
        "Automates Nicole's MSK TARGET Gene Triaging workflow."
    ),
    version="0.1.0",
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def _check_backend(backend: str) -> LocalBackendStatus:
    command = LOCAL_BACKEND_COMMANDS[backend]
    path = _find_command(command)
    if not path:
        return LocalBackendStatus(
            backend=backend,
            command=command,
            installed=False,
        )

    try:
        completed = run(
            [path, *LOCAL_BACKEND_VERSION_ARGS[backend]],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except TimeoutExpired:
        return LocalBackendStatus(
            backend=backend,
            command=command,
            installed=True,
            path=path,
            error="Version check timed out",
        )
    except OSError as exc:
        return LocalBackendStatus(
            backend=backend,
            command=command,
            installed=False,
            path=path,
            error=str(exc),
        )

    version = (completed.stdout or completed.stderr).strip().splitlines()
    return LocalBackendStatus(
        backend=backend,
        command=command,
        installed=completed.returncode == 0,
        version=version[0] if version else None,
        path=path,
        error=None if completed.returncode == 0 else completed.stderr.strip(),
    )


def _find_command(command: str) -> Optional[str]:
    return resolve_local_agent_path(command)


def _installer_info(backend: str) -> LocalBackendInstallerInfo:
    os_name = platform.system()
    if backend == "codex":
        post_install_steps = [
            "After installation, sign in with `codex login`.",
            "Use `codex --version` to confirm the install.",
        ]
        if os_name in {"Darwin", "Linux"}:
            return LocalBackendInstallerInfo(
                backend="codex",
                supported=True,
                command=[
                    "sh",
                    "-lc",
                    "curl -fsSL https://chatgpt.com/codex/install.sh | "
                    "CODEX_NON_INTERACTIVE=1 sh",
                ],
                display_command=(
                    "curl -fsSL https://chatgpt.com/codex/install.sh | "
                    "CODEX_NON_INTERACTIVE=1 sh"
                ),
                post_install_steps=post_install_steps,
            )
        if os_name == "Windows":
            return LocalBackendInstallerInfo(
                backend="codex",
                supported=True,
                command=[
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "$env:CODEX_NON_INTERACTIVE=1; "
                    "irm https://chatgpt.com/codex/install.ps1 | iex",
                ],
                display_command=(
                    "$env:CODEX_NON_INTERACTIVE=1; "
                    "irm https://chatgpt.com/codex/install.ps1 | iex"
                ),
                post_install_steps=post_install_steps,
            )

    if backend == "claude-code":
        post_install_steps = [
            "After installation, run `claude` and complete browser sign-in.",
            "Use `claude --version` to confirm the install.",
        ]
        if os_name in {"Darwin", "Linux"}:
            return LocalBackendInstallerInfo(
                backend="claude-code",
                supported=True,
                command=[
                    "sh",
                    "-lc",
                    "curl -fsSL https://claude.ai/install.sh | bash",
                ],
                display_command="curl -fsSL https://claude.ai/install.sh | bash",
                post_install_steps=post_install_steps,
            )
        if os_name == "Windows":
            return LocalBackendInstallerInfo(
                backend="claude-code",
                supported=True,
                command=[
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "irm https://claude.ai/install.ps1 | iex",
                ],
                display_command="irm https://claude.ai/install.ps1 | iex",
                post_install_steps=post_install_steps,
            )

    if backend == "copilot":
        post_install_steps = [
            "After installation, sign in with `copilot login` and follow the GitHub browser/SSO flow.",
            "Use `copilot version` to confirm the install.",
            "Copilot CLI can also use GitHub CLI credentials when `gh auth login` is already configured.",
        ]
        if os_name in {"Darwin", "Linux"}:
            return LocalBackendInstallerInfo(
                backend="copilot",
                supported=True,
                command=[
                    "sh",
                    "-lc",
                    "curl -fsSL https://gh.io/copilot-install | bash",
                ],
                display_command="curl -fsSL https://gh.io/copilot-install | bash",
                post_install_steps=post_install_steps,
            )
        if os_name == "Windows":
            return LocalBackendInstallerInfo(
                backend="copilot",
                supported=True,
                command=[
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "winget install GitHub.Copilot",
                ],
                display_command="winget install GitHub.Copilot",
                post_install_steps=post_install_steps,
            )

    return LocalBackendInstallerInfo(
        backend=backend,
        supported=False,
        command=[],
        display_command="",
        setup_url="https://antigravity.google/" if backend == "antigravity" else None,
        post_install_steps=["Use the linked official setup guide for this operating system."],
    )


def _truncate_output(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def _require_local_request(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if client_host not in LOCAL_CLIENT_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="Local backend installation is only available from localhost.",
        )


@app.get("/v1/local-backends/status", response_model=LocalBackendsStatusResponse)
async def local_backend_status() -> LocalBackendsStatusResponse:
    statuses = [_check_backend(backend) for backend in LOCAL_BACKENDS]
    local_setup_backends = {"claude-code", "codex", "copilot", "antigravity"}
    local_backend_configured = any(
        status.installed and status.backend in local_setup_backends for status in statuses
    )
    anthropic_sdk_configured = bool(settings.anthropic_api_key)
    oncokb_configured = oncokb_config_status().configured
    setup_messages = []
    if not (anthropic_sdk_configured or local_backend_configured):
        setup_messages.append(
            "Set up at least one execution path: install a local agent or configure Anthropic SDK."
        )
    if not oncokb_configured:
        setup_messages.append(
            "Paste an OncoKB API token from OncoKB account settings."
        )
    minimum_setup_complete = (
        (anthropic_sdk_configured or local_backend_configured) and oncokb_configured
    )
    return LocalBackendsStatusResponse(
        backends=statuses,
        setup_required=not minimum_setup_complete,
        minimum_setup_complete=minimum_setup_complete,
        anthropic_sdk_configured=anthropic_sdk_configured,
        local_backend_configured=local_backend_configured,
        oncokb_configured=oncokb_configured,
        setup_messages=setup_messages,
        operating_system=platform.system(),
    )


@app.get(
    "/v1/local-backends/installers",
    response_model=list[LocalBackendInstallerInfo],
)
async def local_backend_installers() -> list[LocalBackendInstallerInfo]:
    return [
        _installer_info("codex"),
        _installer_info("claude-code"),
        _installer_info("copilot"),
        _installer_info("antigravity"),
    ]


@app.post("/v1/local-backends/install", response_model=LocalBackendInstallResponse)
async def install_local_backend(
    install_request: LocalBackendInstallRequest,
    request: Request,
) -> LocalBackendInstallResponse:
    _require_local_request(request)
    installer = _installer_info(install_request.backend)
    if not installer.supported:
        raise HTTPException(
            status_code=400,
            detail="Automatic installation is not supported on this operating system.",
        )

    try:
        completed = run(
            installer.command,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
    except TimeoutExpired as exc:
        return LocalBackendInstallResponse(
            backend=install_request.backend,
            installed=False,
            return_code=124,
            command=installer.display_command,
            stdout=_truncate_output(exc.stdout or ""),
            stderr=_truncate_output(exc.stderr or "Installation timed out."),
            next_steps=[
                "Try the official manual setup guide from the setup screen.",
            ],
        )
    except OSError as exc:
        return LocalBackendInstallResponse(
            backend=install_request.backend,
            installed=False,
            return_code=127,
            command=installer.display_command,
            stderr=str(exc),
            next_steps=[
                "Try the official manual setup guide from the setup screen.",
            ],
        )

    status = _check_backend(install_request.backend)
    return LocalBackendInstallResponse(
        backend=install_request.backend,
        installed=status.installed,
        return_code=completed.returncode,
        command=installer.display_command,
        stdout=_truncate_output(completed.stdout or ""),
        stderr=_truncate_output(completed.stderr or ""),
        next_steps=installer.post_install_steps,
    )


@app.post("/v1/local-backends/login", response_model=LocalBackendLoginResponse)
async def login_local_backend(
    login_request: LocalBackendLoginRequest,
    request: Request,
) -> LocalBackendLoginResponse:
    _require_local_request(request)
    command = LOCAL_BACKEND_COMMANDS[login_request.backend]
    path = _find_command(command)
    if not path:
        raise HTTPException(
            status_code=400,
            detail=f"{command} was not detected. Install the CLI before logging in.",
        )

    try:
        completed = run(
            [path, "login"],
            capture_output=True,
            text=True,
            timeout=LOGIN_TIMEOUT_SECONDS,
            check=False,
        )
    except TimeoutExpired as exc:
        return LocalBackendLoginResponse(
            backend=login_request.backend,
            return_code=124,
            command=f"{command} login",
            stdout=_truncate_output(exc.stdout or ""),
            stderr=_truncate_output(exc.stderr or "Login timed out."),
            next_steps=[
                f"Run `{command} login` in a terminal and complete the browser/SSO flow.",
            ],
        )
    except OSError as exc:
        return LocalBackendLoginResponse(
            backend=login_request.backend,
            return_code=127,
            command=f"{command} login",
            stderr=str(exc),
            next_steps=[
                f"Run `{command} login` in a terminal and complete the browser/SSO flow.",
            ],
        )

    return LocalBackendLoginResponse(
        backend=login_request.backend,
        return_code=completed.returncode,
        command=f"{command} login",
        stdout=_truncate_output(completed.stdout or ""),
        stderr=_truncate_output(completed.stderr or ""),
        next_steps=[
            "Refresh status after completing GitHub browser/SSO authentication.",
        ],
    )


@app.post("/v1/annotate", response_model=AnnotationResult)
async def annotate(request: AnnotateRequest) -> AnnotationResult:
    """
    Annotate a list of candidate gene fusions.

    Input: `{ "fusions": ["GENE1::GENE2", "GENE3::GENE4"] }`

    Each fusion is split into its partner genes. The unit of annotation
    is the gene. Returns one annotation row per unique gene, matching
    the MSK TARGET Gene Triaging schema.
    """
    try:
        result = await run_pipeline(request.fusions, local_backend=request.local_backend)
        return result
    except Exception as e:
        logger.exception("Pipeline error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/v1/annotate/stream")
async def annotate_stream(request: AnnotateRequest) -> StreamingResponse:
    async def events():
        try:
            async for event in iter_pipeline_events(
                request.fusions,
                local_backend=request.local_backend,
            ):
                yield json.dumps(jsonable_encoder(event)) + "\n"
        except Exception as exc:
            logger.exception("Pipeline stream error")
            event = {
                "type": "error",
                "message": str(exc),
                "result": None,
            }
            yield json.dumps(event) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/v1/export/annotation-results.csv")
async def export_annotation_results_csv(result: AnnotationResult) -> Response:
    csv_text = annotation_results_csv_text(result)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="annotation_results.csv"',
        },
    )


@app.post("/v1/export/google-sheet", response_model=GoogleSheetExportResponse)
async def export_google_sheet(request: GoogleSheetExportRequest) -> GoogleSheetExportResponse:
    try:
        return export_annotation_results_to_google_sheet(
            request.result,
            spreadsheet_id=request.spreadsheet_id,
            sheet_name=request.sheet_name,
        )
    except GoogleSheetsExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/google-sheets/config", response_model=GoogleSheetsConfigStatus)
async def google_sheets_config() -> GoogleSheetsConfigStatus:
    return google_sheets_config_status()


@app.post(
    "/v1/google-sheets/service-account",
    response_model=GoogleSheetsServiceAccountConfigResponse,
)
async def configure_google_sheets_service_account(
    config_request: GoogleSheetsServiceAccountConfigRequest,
    request: Request,
) -> GoogleSheetsServiceAccountConfigResponse:
    _require_local_request(request)
    try:
        return save_service_account_credentials(config_request.service_account_json)
    except GoogleSheetsExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/oncokb/config", response_model=OncoKBConfigStatus)
async def oncokb_config() -> OncoKBConfigStatus:
    return oncokb_config_status()


@app.post("/v1/oncokb/token", response_model=OncoKBTokenConfigResponse)
async def configure_oncokb_token(
    config_request: OncoKBTokenConfigRequest,
    request: Request,
) -> OncoKBTokenConfigResponse:
    _require_local_request(request)
    try:
        return save_oncokb_api_token(config_request.api_token)
    except OncoKBConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/ncbi/config", response_model=NCBIConfigStatus)
async def ncbi_config() -> NCBIConfigStatus:
    return ncbi_config_status()


@app.post("/v1/ncbi/api-key", response_model=NCBIAPIKeyConfigResponse)
async def configure_ncbi_api_key(
    config_request: NCBIAPIKeyConfigRequest,
    request: Request,
) -> NCBIAPIKeyConfigResponse:
    _require_local_request(request)
    try:
        return save_ncbi_api_key(config_request.api_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/benchmark", response_model=BenchmarkResult)
async def benchmark(request: BenchmarkRequest) -> BenchmarkResult:
    try:
        from benchmarks.metrics import compute_categorical_metrics
        from benchmarks.run_benchmark import (
            DEFAULT_HOLDOUT,
            _align_predictions,
            _get_fusions_from_holdout,
            _run_pipeline,
            build_per_gene_report,
            load_holdout,
        )

        holdout = load_holdout(DEFAULT_HOLDOUT)
        fusions = _get_fusions_from_holdout(holdout)
        pipeline_result = await _run_pipeline(fusions, local_backend=request.local_backend)
        aligned_pred, aligned_gold = _align_predictions(holdout, pipeline_result)
        metrics = compute_categorical_metrics(aligned_pred, aligned_gold)
        per_gene_report = build_per_gene_report(aligned_pred, aligned_gold)

        judge_results = None
        if not request.no_judge:
            from benchmarks.judge import run_judge

            genes = [gene["gene"] for gene in aligned_gold]
            pred_summaries = [prediction.get("gene_summary") for prediction in aligned_pred]
            gold_summaries = [gene.get("gene_summary") for gene in aligned_gold]
            judge_results = run_judge(genes, pred_summaries, gold_summaries)

        return BenchmarkResult(
            n_genes=len(holdout),
            categorical_metrics=metrics,
            per_gene_report=per_gene_report,
            judge=judge_results,
            pipeline_result=AnnotationResult.model_validate(pipeline_result),
        )
    except Exception as e:
        logger.exception("Benchmark error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


if __name__ == "__main__":
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=False)
