"""Tests for dev-only benchmark API endpoints."""

from fastapi.testclient import TestClient

from src import main
from src.main import app


def test_dev_status_reflects_config(monkeypatch):
    monkeypatch.setattr(main.settings, "agcg_dev_mode", True)
    client = TestClient(app)

    response = client.get("/v1/dev/status")

    assert response.status_code == 200
    assert response.json() == {"enabled": True}


def test_benchmark_endpoint_hidden_when_dev_mode_disabled(monkeypatch):
    monkeypatch.setattr(main.settings, "agcg_dev_mode", False)
    client = TestClient(app)

    response = client.post("/v1/dev/benchmark", json={"no_judge": True})

    assert response.status_code == 404


def test_benchmark_endpoint_runs_when_dev_mode_enabled(monkeypatch):
    seen = {}

    async def fake_run_benchmark(**kwargs):
        seen.update(kwargs)
        return {
            "n_genes": 1,
            "categorical_metrics": {
                "n": 1,
                "cancer_associated": {"accuracy": 1.0, "cohen_kappa": 1.0},
                "citations": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
            },
            "per_gene_report": [],
            "judge": None,
            "pipeline_result": {"annotations": []},
        }

    monkeypatch.setattr(main.settings, "agcg_dev_mode", True)
    monkeypatch.setattr(main, "run_benchmark", fake_run_benchmark)
    client = TestClient(app)

    response = client.post(
        "/v1/dev/benchmark",
        json={"no_judge": True, "max_genes": 1, "local_backend": "codex"},
    )

    assert response.status_code == 200
    assert response.json()["n_genes"] == 1
    assert seen["no_judge"] is True
    assert seen["max_genes"] == 1
    assert seen["local_backend"] == "codex"
