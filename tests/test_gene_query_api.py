"""Tests for the slim gene query API (src/api/gene_query.py).

Most tests stub run_pipeline outright. The tumor-context tests instead run the
real run_pipeline and real normalize_fusions, stubbing only the HGNC/Ensembl
HTTP resolvers and the per-gene LLM step, so input ordering and alias merging
match production. The MySQL-backed run store is replaced with an in-memory fake
(via main.RunStore, which the app lifespan calls), so nothing here needs MySQL,
network, or LLM credentials.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from src import main
from src.api.gene_query import GeneQueryItem, GeneRationale, to_gene_rationale
from src.config import settings
from src.models.schema import (
    AnnotationResult,
    ClinicalActionability,
    EvidenceCard,
    GeneAnnotation,
    QualityFlag,
    ResolvedGene,
    SupportingQuote,
)
from src.pipeline import normalization, orchestrator

_RUN_ID = "33333333-3333-3333-3333-333333333333"
_ABSTRACT = "SECRET-ABSTRACT-TEXT about ALK rearrangements"
_QUOTE = "SECRET-QUOTE-TEXT from the paper"
# Alias -> canonical symbol for the HGNC resolver stub.
_ALIASES = {"MOZ": "KAT6A"}
_TP53_ENSEMBL = "ENSG00000141510.17"


def _canonical(symbol: str) -> str:
    return _ALIASES.get(symbol.upper(), symbol.upper())


class FakeRunStore:
    fail_saves = False

    def __init__(self):
        self.saved_runs = []

    @classmethod
    async def create(cls):
        return cls()

    async def close(self):
        pass

    async def save_gene_annotation(self, annotation, now, tumor_type=None):
        pass

    async def save_run(self, run_id, timestamp, request_payload, result_payload):
        if self.fail_saves:
            raise RuntimeError("mysql: SECRET-DB-ERROR")
        self.saved_runs.append((run_id, timestamp, request_payload, result_payload))


def _rich_annotation(gene: str = "ALK", analysis_tumor_type=None, **overrides) -> GeneAnnotation:
    fields = dict(
        gene=gene,
        in_oncokb=True,
        cancer_associated=True,
        cancer_association_rationale=f"{gene} is a known oncogenic kinase.",
        gene_class="Receptor tyrosine kinase",
        gene_summary=f"{gene} summary.",
        citations=["12345", "67890"],
        supporting_quotes=[SupportingQuote(pmid="12345", quote=_QUOTE)],
        evidence_cards=[
            EvidenceCard.model_validate(
                {"pmid": "12345", "title": "SECRET-TITLE", "abstract": _ABSTRACT}
            )
        ],
        clinical_actionability=ClinicalActionability(
            confidence_score=0.9, summary="SECRET-ACTIONABILITY", confidence_explanation="SECRET-EXPLANATION"
        ),
        quality_flags=[QualityFlag(code="low_citations", label="Few citations", detail="SECRET-DETAIL")],
        evidence_support_score=0.9,
        cache_status="reused",
        cached_at="2026-10-01T00:00:00+00:00",
    )
    fields.update(overrides)
    annotation = GeneAnnotation(**fields)
    annotation.analysis_tumor_type = analysis_tumor_type
    return annotation


@pytest.fixture
def pipeline_calls(monkeypatch):
    calls = []

    async def fake_run_pipeline(fusions, local_backend=None, run_store=None, force_refresh=False, **kwargs):
        calls.append({"fusions": fusions, "local_backend": local_backend, "force_refresh": force_refresh, **kwargs})
        # No aliases or collisions here; tumor-context tests use the real pipeline.
        annotations = [
            _rich_annotation(item.fusion.upper(), analysis_tumor_type=item.tumor_type) for item in fusions
        ]
        on_annotation = kwargs.get("on_annotation")
        if on_annotation:
            for annotation in annotations:
                await on_annotation(annotation)
        return AnnotationResult(
            run_id=_RUN_ID,
            timestamp="2026-10-06T12:00:00+00:00",
            fusions_processed=len(fusions),
            genes_annotated=len(annotations),
            annotations=annotations,
        )

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    return calls


@pytest.fixture
def client(monkeypatch, pipeline_calls):
    monkeypatch.setattr(FakeRunStore, "fail_saves", False)
    monkeypatch.setattr(main, "RunStore", FakeRunStore)
    monkeypatch.setattr(settings, "public_app_base_url", "https://acgc.example.org/")
    with TestClient(main.app) as test_client:
        yield test_client


# --- slim mapping -----------------------------------------------------------


def test_slim_mapping_is_an_allowlist():
    slim = to_gene_rationale(_rich_annotation(analysis_tumor_type="LUAD"))

    assert set(GeneRationale.model_fields) == {
        "gene",
        "tumor_type",
        "cancer_associated",
        "gene_class",
        "in_oncokb",
        "rationale",
        "gene_summary",
        "citation_pmids",
        "evidence_support_score",
        "quality_flags",
        "cache_status",
        "cached_at",
        "error",
    }
    assert slim.rationale == "ALK is a known oncogenic kinase."
    assert slim.citation_pmids == ["12345", "67890"]
    assert slim.quality_flags == ["low_citations"]
    assert slim.tumor_type == "LUAD"

    serialized = slim.model_dump_json()
    for forbidden in (_ABSTRACT, _QUOTE, "SECRET-TITLE", "SECRET-DETAIL", "SECRET-ACTIONABILITY"):
        assert forbidden not in serialized
    for forbidden_key in ("abstract", "evidence_cards", "supporting_quotes", "clinical_actionability", "openevidence"):
        assert forbidden_key not in serialized.lower()


def test_slim_mapping_returns_fixed_error_messages():
    raw = to_gene_rationale(_rich_annotation(error="Synthesis error: Traceback secret"))
    unresolved = to_gene_rationale(
        _rich_annotation(error="Unresolvable gene symbol — bare Ensembl ID or unannotated locus")
    )
    smuggled = to_gene_rationale(
        _rich_annotation(error="Unresolvable gene symbol SECRET-APPENDED: password=hunter2")
    )

    assert raw.error and "secret" not in raw.error.lower()
    assert unresolved.error == "Gene symbol could not be resolved."
    assert smuggled.error == "Gene symbol could not be resolved."


# --- POST /v1/genes/query -----------------------------------------------------


def test_query_returns_run_id_view_url_and_slim_results(client, pipeline_calls):
    response = client.post(
        "/v1/genes/query",
        json={"genes": ["ALK", {"gene": "tp53", "tumor_type": "LUAD"}, "alk"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == _RUN_ID
    assert body["view_url"] == f"https://acgc.example.org/?run={_RUN_ID}"
    assert [item["gene"] for item in body["results"]] == ["ALK", "TP53"]
    assert body["results"][0]["tumor_type"] is None
    assert body["results"][1]["tumor_type"] == "LUAD"
    assert body["results"][0]["citation_pmids"] == ["12345", "67890"]
    assert _ABSTRACT not in response.text and _QUOTE not in response.text

    # Deduped, default mode/backend, one saved run.
    assert len(pipeline_calls) == 1
    call = pipeline_calls[0]
    assert [item.fusion for item in call["fusions"]] == ["ALK", "tp53"]
    assert call["local_backend"] is None
    assert call["mode"] == "full"
    assert call["force_refresh"] is False
    assert len(main.app.state.run_store.saved_runs) == 1


def test_query_view_url_falls_back_to_request_base_url(client, monkeypatch):
    monkeypatch.setattr(settings, "public_app_base_url", "")

    response = client.post("/v1/genes/query", json={"genes": ["ALK"]})

    assert response.json()["view_url"] == f"http://testserver/?run={_RUN_ID}"


def test_query_rejects_overrides_and_oversized_lists(client, monkeypatch):
    monkeypatch.setattr(settings, "gene_query_max_genes", 3)

    too_many = client.post("/v1/genes/query", json={"genes": ["A1", "A2", "A3", "A4"]})
    at_limit = client.post("/v1/genes/query", json={"genes": ["A1", "A2", "A3"]})

    assert too_many.status_code == 422
    assert at_limit.status_code == 200


def test_query_default_cap_is_50(client):
    assert settings.gene_query_max_genes == 50
    response = client.post("/v1/genes/query", json={"genes": [f"G{i}" for i in range(51)]})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "EML4::ALK", "X" * 65, "!!!", "ALK TP53", "::ALK", "ALK::", "ALK--", "--ALK", "ALK/", "ALK-", "AL;K"],
)
def test_query_rejects_invalid_symbols(client, bad):
    assert client.post("/v1/genes/query", json={"genes": [bad]}).status_code == 422
    assert client.post("/v1/genes/query", json={"genes": [{"gene": bad}]}).status_code == 422


@pytest.mark.parametrize("good", ["ALK", "tp53", "HLA-A", "C1orf112", "ENSG00000141510.17", "A"])
def test_symbol_validation_accepts_hgnc_style_symbols(good):
    assert GeneQueryItem(gene=f"  {good} ").gene == good


@pytest.mark.parametrize("path", ["post", "get"])
def test_query_fails_when_run_cannot_be_saved(client, monkeypatch, path):
    monkeypatch.setattr(FakeRunStore, "fail_saves", True)

    if path == "post":
        response = client.post("/v1/genes/query", json={"genes": ["ALK"]})
    else:
        response = client.get("/v1/genes/ALK")

    assert response.status_code == 500
    assert "view_url" not in response.text
    assert "SECRET-DB-ERROR" not in response.text


def test_query_hides_pipeline_exception_text(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("db password=hunter2")

    monkeypatch.setattr(main, "run_pipeline", boom)

    response = client.post("/v1/genes/query", json={"genes": ["ALK"]})

    assert response.status_code == 500
    assert "hunter2" not in response.text


def test_query_records_user_actions(client, monkeypatch):
    actions = []
    monkeypatch.setattr(
        "src.api.gene_query.record_user_action",
        lambda user_id, action, details=None, tags=None: actions.append((action, details)),
    )

    client.post("/v1/genes/query", json={"genes": ["ALK", "TP53"]})

    names = [name for name, _ in actions]
    assert names == ["gene_query", "gene_query_complete"]
    assert actions[0][1]["genes_count"] == 2
    assert actions[1][1]["genes_annotated"] == 2


# --- GET /v1/genes/{symbol} ---------------------------------------------------


def test_get_single_gene_convenience_route(client, pipeline_calls):
    response = client.get("/v1/genes/BRAF", params={"tumor_type": "MEL", "force_refresh": "true"})

    assert response.status_code == 200
    body = response.json()
    assert body["view_url"] == f"https://acgc.example.org/?run={_RUN_ID}"
    assert len(body["results"]) == 1
    assert body["results"][0]["gene"] == "BRAF"
    assert body["results"][0]["tumor_type"] == "MEL"
    assert pipeline_calls[0]["force_refresh"] is True


def test_get_single_gene_rejects_fusion(client):
    assert client.get("/v1/genes/EML4::ALK").status_code == 422


def _unresolvable_pipeline(calls):
    async def fake(fusions, **kwargs):
        calls.append(fusions)
        annotation = GeneAnnotation(
            gene=fusions[0].fusion,
            cache_status="bypassed",
            error="Unresolvable gene symbol — bare Ensembl ID or unannotated locus",
        )
        return AnnotationResult(
            run_id=_RUN_ID, timestamp="t", fusions_processed=1, genes_annotated=1, annotations=[annotation]
        )

    return fake


def test_get_unresolvable_symbol_returns_404(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_pipeline", _unresolvable_pipeline(calls))

    response = client.get("/v1/genes/NOTAGENE1")

    assert response.status_code == 404
    assert response.json() == {"detail": "Gene symbol not found."}
    assert len(calls) == 1


def test_batch_reports_unresolvable_symbol_per_gene(client, monkeypatch):
    monkeypatch.setattr(main, "run_pipeline", _unresolvable_pipeline([]))

    response = client.post("/v1/genes/query", json={"genes": ["NOTAGENE1"]})

    assert response.status_code == 200
    assert response.json()["results"][0]["error"] == "Gene symbol could not be resolved."


# --- tumor context (real run_pipeline + normalize_fusions) ----------------------


@pytest.fixture
def real_pipeline(client, monkeypatch):
    """Run the real pipeline; record the tumor type each gene was classified with."""
    used = {}
    lookups = {"count": 0, "fail_after": None}

    def _lookup():
        lookups["count"] += 1
        if lookups["fail_after"] is not None and lookups["count"] > lookups["fail_after"]:
            raise RuntimeError("HGNC unavailable")

    async def fake_resolve_ensembl(symbols, client):
        _lookup()
        return {
            symbol: ResolvedGene(input_symbol=symbol, canonical_symbol="TP53", resolved=True)
            for symbol in dict.fromkeys(symbols)
        }

    async def fake_resolve_hgnc(symbols, client):
        _lookup()
        # Like production: asyncio.gather over the (sorted) symbols, input order preserved.
        return {
            symbol: ResolvedGene(input_symbol=symbol, canonical_symbol=_canonical(symbol), resolved=True)
            for symbol in symbols
        }

    async def no_cache(**kwargs):
        return None

    async def fake_annotate_gene(**kwargs):
        used[kwargs["gene"]] = kwargs["tumor_type"]
        return _rich_annotation(kwargs["gene"], cache_status="refreshed")

    monkeypatch.setattr(main, "run_pipeline", orchestrator.run_pipeline)
    monkeypatch.setattr(normalization, "_resolve_ensembl_ids", fake_resolve_ensembl)
    monkeypatch.setattr(normalization, "_resolve_hgnc_symbols_concurrently", fake_resolve_hgnc)
    monkeypatch.setattr(orchestrator, "_maybe_reuse_cached_annotation", no_cache)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    monkeypatch.setattr(settings, "gene_cache_enabled", False)
    return {"used": used, "lookups": lookups}


def _reported(response_json):
    return {item["gene"]: item["tumor_type"] for item in response_json["results"]}


_COLLISIONS = [
    # Ensembl ID and symbol for the same gene: production resolves Ensembl IDs first.
    ({"gene": "TP53", "tumor_type": "LUAD"}, {"gene": _TP53_ENSEMBL, "tumor_type": "AML"}, "TP53", "AML"),
    # Alias and symbol: production resolves HGNC symbols in sorted order (KAT6A < MOZ).
    ({"gene": "MOZ", "tumor_type": "AML"}, {"gene": "KAT6A", "tumor_type": "LUAD"}, "KAT6A", "LUAD"),
]


@pytest.mark.parametrize("first,second,gene,expected", _COLLISIONS)
@pytest.mark.parametrize("reverse", [False, True])
def test_collision_reports_tumor_type_the_pipeline_used(client, real_pipeline, first, second, gene, expected, reverse):
    genes = [second, first] if reverse else [first, second]

    response = client.post("/v1/genes/query", json={"genes": genes})

    assert response.status_code == 200
    assert _reported(response.json()) == {gene: real_pipeline["used"][gene]}
    assert real_pipeline["used"][gene] == expected


@pytest.mark.parametrize("first,second,gene,expected", _COLLISIONS)
def test_collision_in_job_reports_tumor_type_the_pipeline_used(client, real_pipeline, first, second, gene, expected):
    created = client.post("/v1/genes/query/jobs", json={"genes": [second, first]})
    body = _poll(client, created.json()["status_url"])

    assert body["status"] == "complete"
    assert _reported(body) == {gene: real_pipeline["used"][gene]} == {gene: expected}


def test_mixed_and_alias_tumor_types_with_real_pipeline(client, real_pipeline):
    response = client.post(
        "/v1/genes/query",
        json={"genes": [{"gene": "MOZ", "tumor_type": "AML"}, "ALK", {"gene": "TP53", "tumor_type": "LUAD"}]},
    )

    assert _reported(response.json()) == real_pipeline["used"] == {"KAT6A": "AML", "ALK": None, "TP53": "LUAD"}


# normalize_fusions calls each resolver family exactly once per pipeline run, so
# allowing 2 lookups lets the pipeline normalize and makes any later lookup fail.
_PIPELINE_LOOKUPS = 2


def test_sync_tumor_types_need_no_lookup_after_pipeline_normalization(client, real_pipeline):
    real_pipeline["lookups"]["fail_after"] = _PIPELINE_LOOKUPS

    response = client.post("/v1/genes/query", json={"genes": [{"gene": "MOZ", "tumor_type": "AML"}, "ALK"]})

    assert response.status_code == 200
    assert _reported(response.json()) == {"KAT6A": "AML", "ALK": None}
    assert real_pipeline["lookups"]["count"] == _PIPELINE_LOOKUPS


def test_job_tumor_types_need_no_lookup_after_pipeline_normalization(client, real_pipeline):
    real_pipeline["lookups"]["fail_after"] = _PIPELINE_LOOKUPS

    created = client.post("/v1/genes/query/jobs", json={"genes": [{"gene": "MOZ", "tumor_type": "AML"}]})
    body = _poll(client, created.json()["status_url"])
    again = client.get(created.json()["status_url"]).json()

    assert body["status"] == "complete"
    assert _reported(body) == _reported(again) == {"KAT6A": "AML"}
    assert real_pipeline["lookups"]["count"] == _PIPELINE_LOOKUPS


# --- jobs -----------------------------------------------------------------------


def _poll(client, status_url, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(status_url).json()
        if body["status"] in ("complete", "failed") or time.monotonic() > deadline:
            return body
        time.sleep(0.02)


def test_gene_query_jobs_flow(client):
    created = client.post("/v1/genes/query/jobs", json={"genes": ["ALK", {"gene": "TP53", "tumor_type": "LUAD"}]})

    assert created.status_code == 200
    job_id = created.json()["job_id"]
    assert created.json()["status_url"] == f"/v1/genes/query/jobs/{job_id}"

    body = _poll(client, created.json()["status_url"])

    assert body["status"] == "complete"
    assert body["run_id"] == _RUN_ID
    assert body["view_url"] == f"https://acgc.example.org/?run={_RUN_ID}"
    assert [item["gene"] for item in body["results"]] == ["ALK", "TP53"]
    assert body["results"][0]["tumor_type"] is None
    assert body["results"][1]["tumor_type"] == "LUAD"
    assert _ABSTRACT not in json.dumps(body)
    assert len(main.app.state.run_store.saved_runs) == 1


def test_gene_query_job_failure_is_generic(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret upstream detail")

    monkeypatch.setattr(main, "run_pipeline", boom)

    created = client.post("/v1/genes/query/jobs", json={"genes": ["ALK"]})
    body = _poll(client, created.json()["status_url"])

    assert body["status"] == "failed"
    assert body["error"]
    assert "secret" not in body["error"]


def test_gene_query_job_fails_when_run_cannot_be_saved(client, monkeypatch):
    monkeypatch.setattr(FakeRunStore, "fail_saves", True)

    created = client.post("/v1/genes/query/jobs", json={"genes": ["ALK"]})
    body = _poll(client, created.json()["status_url"])

    assert body["status"] == "failed"
    assert body["run_id"] is None and body["view_url"] is None
    assert "SECRET-DB-ERROR" not in json.dumps(body)


def test_plain_annotation_job_still_completes_when_run_cannot_be_saved(client, monkeypatch):
    # Unchanged /v1/annotate/jobs behavior: persistence is best-effort there.
    monkeypatch.setattr(FakeRunStore, "fail_saves", True)

    created = client.post("/v1/annotate/jobs", json={"fusions": ["ALK"]})
    body = _poll(client, created.json()["status_url"])

    assert body["status"] == "complete"


def test_gene_query_job_status_ignores_plain_annotation_jobs(client):
    created = client.post("/v1/annotate/jobs", json={"fusions": ["ALK"]})
    job_id = created.json()["job_id"]

    assert client.get(f"/v1/genes/query/jobs/{job_id}").status_code == 404
    assert client.get("/v1/genes/query/jobs/does-not-exist").status_code == 404
    # The original job endpoint still serves it, without the internal fields.
    original = client.get(f"/v1/annotate/jobs/{job_id}").json()
    assert "context" not in original and "kind" not in original


# --- POST /v1/annotate/gene ---------------------------------------------------


def test_annotate_gene_includes_run_id_and_view_url(client):
    response = client.post("/v1/annotate/gene", json={"gene": "ALK"})

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == _RUN_ID
    assert body["view_url"] == f"https://acgc.example.org/?run={_RUN_ID}"
    # Backward compatible: full GeneAnnotation payload is still there.
    assert body["gene"] == "ALK"
    assert body["evidence_cards"]
    # The in-process tumor context never leaks into /v1/annotate* payloads or stored runs.
    assert "analysis_tumor_type" not in body
    assert "analysis_tumor_type" not in main.app.state.run_store.saved_runs[0][3]["annotations"][0]
    schemas = main.app.openapi()["components"]["schemas"]
    assert "analysis_tumor_type" not in schemas["GeneAnnotation"]["properties"]


# --- auth -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("post", "/v1/genes/query", {"json": {"genes": ["ALK"]}}),
        ("get", "/v1/genes/ALK", {}),
        ("post", "/v1/genes/query/jobs", {"json": {"genes": ["ALK"]}}),
        ("get", "/v1/genes/query/jobs/some-job", {}),
    ],
)
def test_gene_query_endpoints_require_auth(client, monkeypatch, pipeline_calls, method, path, kwargs):
    monkeypatch.setattr(settings, "auth_enabled", True)

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401
    assert pipeline_calls == []

