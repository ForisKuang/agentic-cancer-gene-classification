"""Tests for the slim gene query API (src/api/gene_query.py).

run_pipeline and HGNC normalization are stubbed and the MySQL-backed run store
is replaced with an in-memory fake (via main.RunStore, which the app lifespan
calls), so these run without MySQL, Redis, network, or LLM credentials.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from src import main
from src.api import gene_query
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

_RUN_ID = "33333333-3333-3333-3333-333333333333"
_ABSTRACT = "SECRET-ABSTRACT-TEXT about ALK rearrangements"
_QUOTE = "SECRET-QUOTE-TEXT from the paper"
# Alias -> canonical symbol, shared by the pipeline and normalization stubs.
_ALIASES = {"MOZ": "KAT6A"}


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

    async def save_run(self, run_id, timestamp, request_payload, result_payload):
        if self.fail_saves:
            raise RuntimeError("mysql: SECRET-DB-ERROR")
        self.saved_runs.append((run_id, timestamp, request_payload, result_payload))


def _rich_annotation(gene: str = "ALK", **overrides) -> GeneAnnotation:
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
    return GeneAnnotation(**fields)


@pytest.fixture
def pipeline_calls(monkeypatch):
    calls = []

    async def fake_run_pipeline(fusions, local_backend=None, run_store=None, force_refresh=False, **kwargs):
        calls.append({"fusions": fusions, "local_backend": local_backend, "force_refresh": force_refresh, **kwargs})
        annotations = [_rich_annotation(gene) for gene in dict.fromkeys(_canonical(item.fusion) for item in fusions)]
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


@pytest.fixture(autouse=True)
def fake_normalization(monkeypatch):
    async def fake_normalize_fusions(symbols):
        gene_map = {}
        for symbol in symbols:
            canonical = _canonical(symbol)
            resolved = ResolvedGene(input_symbol=symbol, canonical_symbol=canonical, resolved=True)
            gene_map.setdefault(canonical, (resolved, []))[1].append(symbol)
        return gene_map

    monkeypatch.setattr(gene_query, "normalize_fusions", fake_normalize_fusions)


@pytest.fixture
def client(monkeypatch, pipeline_calls):
    monkeypatch.setattr(FakeRunStore, "fail_saves", False)
    monkeypatch.setattr(main, "RunStore", FakeRunStore)
    monkeypatch.setattr(settings, "public_app_base_url", "https://acgc.example.org/")
    with TestClient(main.app) as test_client:
        yield test_client


# --- slim mapping -----------------------------------------------------------


def test_slim_mapping_is_an_allowlist():
    slim = to_gene_rationale(_rich_annotation(), tumor_type="LUAD")

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


def test_query_maps_tumor_types_per_gene_and_through_aliases(client):
    response = client.post(
        "/v1/genes/query",
        json={"genes": [{"gene": "MOZ", "tumor_type": "AML"}, "ALK", {"gene": "TP53", "tumor_type": "LUAD"}]},
    )

    results = {item["gene"]: item["tumor_type"] for item in response.json()["results"]}
    assert results == {"KAT6A": "AML", "ALK": None, "TP53": "LUAD"}


def test_query_alias_without_tumor_type_is_not_assigned_another_genes(client):
    response = client.post("/v1/genes/query", json={"genes": ["MOZ", {"gene": "TP53", "tumor_type": "LUAD"}]})

    results = {item["gene"]: item["tumor_type"] for item in response.json()["results"]}
    assert results == {"KAT6A": None, "TP53": "LUAD"}


def test_query_tumor_types_fall_back_to_exact_symbols_if_normalization_fails(client, monkeypatch):
    async def broken(symbols):
        raise RuntimeError("HGNC down")

    monkeypatch.setattr(gene_query, "normalize_fusions", broken)

    response = client.post("/v1/genes/query", json={"genes": ["ALK", {"gene": "tp53", "tumor_type": "LUAD"}]})

    results = {item["gene"]: item["tumor_type"] for item in response.json()["results"]}
    assert results == {"ALK": None, "TP53": "LUAD"}


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

