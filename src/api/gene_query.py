"""Slim gene query API: classification + rationale and a link to the full run.

Intended for scripts run by MSK staff. Responses carry only the rationale-level
fields of a GeneAnnotation (see ``to_gene_rationale``); the full report —
abstracts, evidence cards, quotes, clinical actionability — is viewed in the UI
via ``view_url``, which requires an ACGC login.

Route handlers reuse helpers defined in ``src.main`` (run store persistence,
the in-memory annotation job store, metrics). They are looked up on the module
at call time rather than imported at load time, because ``src.main`` includes
this router and tests monkeypatch ``src.main.run_pipeline``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, List, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from src.auth import AuthenticatedUser, record_user_annotation_activity, require_auth
from src.config import settings
from src.models.schema import AnnotateRequest, AnnotationResult, CacheStatus, FusionInput, GeneAnnotation
from src.observability import record_user_action, record_user_seen, tag_current_span
from src.pipeline.normalization import FUSION_SEPARATORS

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/genes", tags=["gene query"])

_MAX_SYMBOL_LENGTH = 64
# Conservative HGNC-style symbol (e.g. ALK, HLA-A, C1orf112) or Ensembl ID
# (ENSG00000141510.17): letters, digits, '-' and '.', starting and ending with a
# letter or digit. Fusion separators ('::', '--', '/') are rejected separately,
# including dangling forms like 'ALK::' that would normalize into one gene.
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?$")
_UNRESOLVABLE_PREFIX = "Unresolvable gene symbol"
_UNRESOLVABLE_ERROR = "Gene symbol could not be resolved."
_LOOKUP_FAILED_ERROR = "Gene symbol lookup is temporarily unavailable; please retry."
_GENE_NOT_FOUND = "Gene symbol not found."
_GENERIC_GENE_ERROR = "Annotation failed for this gene; open view_url for details."
_GENERIC_FAILURE = "Gene query failed. Please retry; contact the ACGC team if this persists."


def _app() -> "ModuleType":
    from src import main

    return main


async def _save_run_or_raise(http_request: Request, request_payload: dict, result: AnnotationResult) -> None:
    """Only return a run link after persistence succeeds; hide storage errors."""
    try:
        await _app()._save_run_result(http_request, request_payload, result)
    except Exception as exc:
        logger.exception("Gene query failed to save run %s", result.run_id)
        raise HTTPException(status_code=500, detail=_GENERIC_FAILURE) from exc


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class GeneQueryItem(BaseModel):
    """One gene to query, with optional tumor-type context."""

    gene: str = Field(
        ...,
        description=(
            "HGNC-style gene symbol or Ensembl ID, e.g. `ALK`, `HLA-A`: letters, digits, `-` and `.`, "
            "starting and ending with a letter or digit. Fusions are not accepted."
        ),
    )
    tumor_type: Optional[str] = Field(
        default=None, description="Optional tumor type used to focus literature retrieval, e.g. `LUAD`."
    )

    @field_validator("gene")
    @classmethod
    def _validate_gene(cls, value: str) -> str:
        return _validate_symbol(value)

    @field_validator("tumor_type")
    @classmethod
    def _normalize_tumor_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return " ".join(value.split()) or None


class GeneQueryRequest(BaseModel):
    """Batch of genes to classify. Plain strings and objects may be mixed."""

    genes: List[Union[GeneQueryItem, str]] = Field(
        ...,
        min_length=1,
        description=(
            "Genes to query, as symbols (`\"ALK\"`) or objects (`{\"gene\": \"ALK\", \"tumor_type\": \"LUAD\"}`). "
            "At most `GENE_QUERY_MAX_GENES` entries (default 50). Duplicate symbols (case-insensitive) "
            "are collapsed; the first tumor type given for a gene is used."
        ),
    )
    force_refresh: bool = Field(
        default=False,
        description="Bypass cached gene annotations and recompute (triggers new LLM runs).",
    )

    @model_validator(mode="before")
    @classmethod
    def _check_size(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("genes"), list):
            limit = settings.gene_query_max_genes
            if len(data["genes"]) > limit:
                raise ValueError(f"At most {limit} genes may be queried per request")
        return data

    @field_validator("genes", mode="after")
    @classmethod
    def _coerce_items(cls, value: List[Union[GeneQueryItem, str]]) -> List[GeneQueryItem]:
        # Validate bare strings here (ValueError -> 422) so constructing the item cannot fail.
        return [GeneQueryItem(gene=_validate_symbol(item)) if isinstance(item, str) else item for item in value]

    def unique_items(self) -> List[GeneQueryItem]:
        seen: set[str] = set()
        items: List[GeneQueryItem] = []
        for item in self.genes:
            assert isinstance(item, GeneQueryItem)
            key = item.gene.upper()
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
        return items


class GeneRationale(BaseModel):
    """Rationale-level classification of one gene. Abstracts, evidence cards,
    supporting quotes, clinical actionability, and OpenEvidence content are
    deliberately omitted — open `view_url` for the full report."""

    gene: str = Field(..., description="Canonical gene symbol the pipeline annotated.")
    tumor_type: Optional[str] = Field(
        default=None,
        description=(
            "Tumor type the classification actually used for this gene, or null if none. When several inputs "
            "resolve to the same gene (e.g. an alias and its symbol), the pipeline uses one of their tumor types."
        ),
    )
    cancer_associated: Optional[bool] = Field(
        default=None, description="Whether the gene is classified as cancer-associated."
    )
    gene_class: Optional[str] = Field(default=None, description="Functional gene class, e.g. kinase.")
    in_oncokb: Optional[bool] = Field(
        default=None, description="Whether the gene is in OncoKB (null when OncoKB is not configured)."
    )
    rationale: Optional[str] = Field(default=None, description="Cancer-association rationale.")
    gene_summary: Optional[str] = Field(default=None, description="Short summary of the gene's function.")
    citation_pmids: List[str] = Field(
        default_factory=list, description="Verified PubMed IDs cited by the rationale."
    )
    evidence_support_score: float = Field(
        default=0.0,
        description=(
            "Deterministic 0-1 estimate of how well the annotation is grounded in retrieved literature; "
            "not a probability of biological truth."
        ),
    )
    quality_flags: List[str] = Field(default_factory=list, description="Codes of any quality flags raised.")
    cache_status: Optional[CacheStatus] = Field(
        default=None, description="Whether a cached annotation was reused, refreshed, or newly computed."
    )
    cached_at: Optional[str] = Field(default=None, description="When the cached annotation was produced.")
    error: Optional[str] = Field(default=None, description="Set when this gene could not be annotated.")


class GeneQueryResponse(BaseModel):
    """Slim results plus a link to the full saved run."""

    run_id: str = Field(..., description="ID of the saved annotation run.")
    view_url: str = Field(..., description="Absolute link that opens the full run in the ACGC UI (requires login).")
    results: List[GeneRationale] = Field(default_factory=list, description="One entry per annotated gene.")


class GeneQueryJobCreateResponse(BaseModel):
    """Handle for an asynchronous gene query."""

    job_id: str = Field(..., description="Job ID to poll.")
    status_url: str = Field(..., description="Relative URL to poll for job status.")


class GeneQueryJobStatusResponse(BaseModel):
    """Status of an asynchronous gene query. `run_id`, `view_url`, and the
    complete `results` are set once `status` is `complete`."""

    job_id: str = Field(..., description="Job ID.")
    status: Literal["queued", "running", "complete", "failed"] = Field(..., description="Job state.")
    genes_completed: int = Field(default=0, description="Genes annotated so far.")
    genes_total: Optional[int] = Field(default=None, description="Total genes, once known.")
    run_id: Optional[str] = Field(default=None, description="Saved run ID, once complete.")
    view_url: Optional[str] = Field(default=None, description="Link to the full run in the UI, once complete.")
    results: List[GeneRationale] = Field(
        default_factory=list, description="Slim results for genes completed so far."
    )
    error: Optional[str] = Field(default=None, description="Generic failure message when `status` is `failed`.")


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _validate_symbol(value: str) -> str:
    symbol = value.strip()
    if not symbol:
        raise ValueError("Gene symbol must not be empty")
    if len(symbol) > _MAX_SYMBOL_LENGTH:
        raise ValueError(f"Gene symbol must be at most {_MAX_SYMBOL_LENGTH} characters")
    if FUSION_SEPARATORS.search(symbol):
        raise ValueError("Fusions are not supported by the gene query API; use POST /v1/annotate")
    if not _SYMBOL_PATTERN.match(symbol):
        raise ValueError(
            "Gene symbol may contain only letters, digits, '-' and '.', "
            "and must start and end with a letter or digit"
        )
    return symbol


def _is_unresolvable(annotation: GeneAnnotation) -> bool:
    return bool(annotation.error and annotation.error.startswith(_UNRESOLVABLE_PREFIX))


def _is_confirmed_absent(annotation: GeneAnnotation) -> bool:
    """Unresolvable because HGNC/Ensembl said so, not because the lookup failed."""
    return _is_unresolvable(annotation) and not annotation.symbol_lookup_failed


def _slim_error(annotation: GeneAnnotation) -> Optional[str]:
    if not annotation.error:
        return None
    # Fixed messages only: per-gene errors can embed raw exception text, which
    # stays in the UI behind view_url.
    if _is_unresolvable(annotation):
        return _LOOKUP_FAILED_ERROR if annotation.symbol_lookup_failed else _UNRESOLVABLE_ERROR
    return _GENERIC_GENE_ERROR


def to_gene_rationale(annotation: GeneAnnotation) -> GeneRationale:
    """Explicit allowlist mapping. Never dump-and-delete: new GeneAnnotation
    fields must stay out of this response unless deliberately added here.

    tumor_type comes from the pipeline itself (analysis_tumor_type), so it is
    always the context the classification ran with, including for aliases."""
    return GeneRationale(
        gene=annotation.gene,
        tumor_type=annotation.analysis_tumor_type,
        cancer_associated=annotation.cancer_associated,
        gene_class=annotation.gene_class,
        in_oncokb=annotation.in_oncokb,
        rationale=annotation.cancer_association_rationale,
        gene_summary=annotation.gene_summary,
        citation_pmids=[str(pmid) for pmid in annotation.citations],
        evidence_support_score=annotation.evidence_support_score,
        quality_flags=[flag.code for flag in annotation.quality_flags],
        cache_status=annotation.cache_status,
        cached_at=annotation.cached_at,
        error=_slim_error(annotation),
    )


def _to_annotate_request(items: List[GeneQueryItem], force_refresh: bool) -> AnnotateRequest:
    # Default mode/backend only: the gene query API intentionally exposes no overrides.
    return AnnotateRequest(
        fusions=[FusionInput(fusion=item.gene, tumor_type=item.tumor_type) for item in items],
        force_refresh=force_refresh,
    )


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def _record_gene_query_request(
    http_request: Request,
    current_user: Optional[AuthenticatedUser],
    items: List[GeneQueryItem],
    *,
    route: str,
    force_refresh: bool,
) -> None:
    app = _app()
    user = current_user if isinstance(current_user, AuthenticatedUser) else None
    user_id = app._request_user_id(http_request, user)
    tags = [f"route:{route}", f"force_refresh:{force_refresh}"]
    record_user_seen(user_id, tags=tags)
    effective_user = user.email if user else (user_id or "anonymous")
    record_user_action(
        user_id=effective_user,
        action="gene_query",
        details={
            "route": route,
            "genes_count": len(items),
            "genes": ",".join(item.gene for item in items[:5]),
            "force_refresh": force_refresh,
        },
        tags=tags,
    )
    tag_current_span(
        {
            "acgc.user.present": bool(user_id),
            "acgc.gene_query.route": route,
            "acgc.gene_query.genes": len(items),
            "usr.id": effective_user,
        }
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _run_gene_query(
    items: List[GeneQueryItem],
    force_refresh: bool,
    http_request: Request,
    current_user: Optional[AuthenticatedUser],
    *,
    route: str,
) -> AnnotationResult:
    """Run the pipeline and save the run, raising a generic 500 on failure."""
    app = _app()
    _record_gene_query_request(http_request, current_user, items, route=route, force_refresh=force_refresh)
    annotate_request = _to_annotate_request(items, force_refresh)
    try:
        result: AnnotationResult = await app.run_pipeline(
            annotate_request.fusions,
            local_backend=annotate_request.local_backend,
            run_store=http_request.app.state.run_store,
            force_refresh=annotate_request.force_refresh,
            skip_literature_for_oncokb=annotate_request.skip_literature_for_oncokb,
            mode=annotate_request.mode,
            strict_gene_lookup=True,
        )
    except Exception as exc:
        logger.exception("Gene query pipeline error")
        raise HTTPException(status_code=500, detail=_GENERIC_FAILURE) from exc

    await _save_run_or_raise(http_request, annotate_request.model_dump(), result)
    if current_user and current_user.email:
        await record_user_annotation_activity(current_user.email, count=result.genes_annotated)
    record_user_action(
        user_id=current_user.email if current_user else app._request_user_id(http_request),
        action="gene_query_complete",
        details={
            "route": route,
            "run_id": result.run_id,
            "genes_count": len(items),
            "genes_annotated": result.genes_annotated,
            "duration_ms": round(result.timings_ms.get("total", 0.0), 1),
        },
    )
    return result


def _response(http_request: Request, result: AnnotationResult) -> GeneQueryResponse:
    return GeneQueryResponse(
        run_id=result.run_id,
        view_url=_app()._run_view_url(http_request, result.run_id),
        results=[to_gene_rationale(annotation) for annotation in result.annotations],
    )


@router.post("/query", response_model=GeneQueryResponse)
async def query_genes(
    request: GeneQueryRequest,
    http_request: Request,
    current_user: AuthenticatedUser = Depends(require_auth),
) -> GeneQueryResponse:
    """
    Classify one or more genes and return only the rationale-level fields.

    Cached annotations are reused when fresh (set `force_refresh` to recompute,
    which triggers new LLM runs). All genes are saved as ONE run; `view_url`
    opens it in the ACGC UI. For long batches, use POST /v1/genes/query/jobs.
    """
    result = await _run_gene_query(
        request.unique_items(), request.force_refresh, http_request, current_user, route="query"
    )
    return _response(http_request, result)


@router.post("/query/jobs", response_model=GeneQueryJobCreateResponse)
async def create_gene_query_job(
    request: GeneQueryRequest,
    http_request: Request,
    current_user: AuthenticatedUser = Depends(require_auth),
) -> GeneQueryJobCreateResponse:
    """Start an asynchronous gene query; poll `status_url` for results."""
    app = _app()
    items = request.unique_items()
    _record_gene_query_request(
        http_request, current_user, items, route="query_jobs", force_refresh=request.force_refresh
    )
    job_id = await app._launch_annotation_job(
        _to_annotate_request(items, request.force_refresh),
        http_request,
        current_user,
        kind="gene_query",
        require_persistence=True,
        complete_action="gene_query_complete",
        error_action="gene_query_error",
        extra_details={"route": "query_jobs", "genes_count": len(items)},
    )
    return GeneQueryJobCreateResponse(job_id=job_id, status_url=f"/v1/genes/query/jobs/{job_id}")


@router.get("/query/jobs/{job_id}", response_model=GeneQueryJobStatusResponse)
async def get_gene_query_job(
    job_id: str,
    http_request: Request,
    current_user: AuthenticatedUser = Depends(require_auth),
) -> GeneQueryJobStatusResponse:
    """Poll an asynchronous gene query."""
    app = _app()
    try:
        job = await app._get_annotation_job(job_id)
    except HTTPException:
        job = None
    if job is None or job.kind != "gene_query":
        raise HTTPException(status_code=404, detail="Gene query job not found")


    run_id = job.result.run_id if job.status == "complete" and job.result else None
    return GeneQueryJobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        genes_completed=job.genes_completed,
        genes_total=job.genes_total,
        run_id=run_id,
        view_url=app._run_view_url(http_request, run_id) if run_id else None,
        results=[to_gene_rationale(annotation) for annotation in job.annotations],
        error=_GENERIC_FAILURE if job.status == "failed" else None,
    )


@router.get("/{symbol}", response_model=GeneQueryResponse)
async def query_gene(
    http_request: Request,
    symbol: str = Path(..., description="HGNC gene symbol, e.g. `ALK`."),
    tumor_type: Optional[str] = Query(default=None, description="Optional tumor type, e.g. `LUAD`."),
    force_refresh: bool = Query(default=False, description="Bypass cached annotations and recompute."),
    current_user: AuthenticatedUser = Depends(require_auth),
) -> GeneQueryResponse:
    """Convenience form of POST /v1/genes/query for a single gene.

    Returns 404 when HGNC/Ensembl confirm the symbol doesn't exist, and 503
    when the lookup service failed (the batch POST instead reports either case
    per gene in `error`). The run is still saved either way.
    """
    try:
        item = GeneQueryItem(gene=symbol, tumor_type=tumor_type)
    except ValueError as exc:
        # pydantic.ValidationError subclasses ValueError; surface its messages as a 422.
        errors = getattr(exc, "errors", None)
        detail = [{"loc": ["path", "symbol"], "msg": err["msg"]} for err in errors()] if errors else str(exc)
        raise HTTPException(status_code=422, detail=detail) from exc
    result = await _run_gene_query([item], force_refresh, http_request, current_user, route="gene")
    if result.annotations and all(_is_unresolvable(annotation) for annotation in result.annotations):
        if all(_is_confirmed_absent(annotation) for annotation in result.annotations):
            raise HTTPException(status_code=404, detail=_GENE_NOT_FOUND)
        raise HTTPException(status_code=503, detail=_LOOKUP_FAILED_ERROR)
    return _response(http_request, result)
