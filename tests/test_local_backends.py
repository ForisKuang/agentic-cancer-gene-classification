"""Unit tests for local LLM backend selection."""

import pytest

from benchmarks.run_benchmark import _run_pipeline
from src.cli import parse_args
from src.models.schema import AnnotateRequest
from src.pipeline import llm_client
from src.pipeline.llm_client import (
    DEFAULT_LOCAL_BACKEND,
    make_async_sdk_client,
    resolve_local_backend,
    resolve_sdk_model,
    resolve_sdk_provider,
)


def test_resolve_local_backend_defaults_for_legacy_bool():
    assert resolve_local_backend(local_mode=True) == DEFAULT_LOCAL_BACKEND


def test_resolve_local_backend_prefers_explicit_backend():
    assert resolve_local_backend(local_mode=False, local_backend="codex") == "codex"


def test_resolve_local_backend_rejects_unknown_backend():
    with pytest.raises(ValueError):
        resolve_local_backend(local_backend="unknown")


def test_resolve_sdk_provider_accepts_bedrock_alias(monkeypatch):
    monkeypatch.setattr(llm_client.settings, "anthropic_sdk_provider", "aws-bedrock")

    assert resolve_sdk_provider() == "bedrock"


def test_resolve_sdk_model_uses_bedrock_selection_override(monkeypatch):
    monkeypatch.setattr(llm_client.settings, "anthropic_sdk_provider", "bedrock")
    monkeypatch.setattr(
        llm_client.settings,
        "bedrock_selection_model",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
    )

    assert resolve_sdk_model("claude-haiku-4-5-20251001", "selection") == (
        "anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def test_resolve_sdk_model_maps_known_bedrock_alias(monkeypatch):
    monkeypatch.setattr(llm_client.settings, "anthropic_sdk_provider", "bedrock")
    monkeypatch.setattr(llm_client.settings, "bedrock_selection_model", "")

    assert resolve_sdk_model("claude-haiku-4-5-20251001", "selection") == (
        "anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def test_resolve_sdk_model_requires_bedrock_model_id(monkeypatch):
    monkeypatch.setattr(llm_client.settings, "anthropic_sdk_provider", "bedrock")
    monkeypatch.setattr(llm_client.settings, "bedrock_synthesis_model", "")

    with pytest.raises(RuntimeError, match="requires Bedrock model IDs"):
        resolve_sdk_model("claude-opus-4-7", "synthesis")


def test_make_async_sdk_client_passes_bedrock_kwargs(monkeypatch):
    seen = {}

    class FakeBedrockClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(llm_client.settings, "anthropic_sdk_provider", "bedrock")
    monkeypatch.setattr(llm_client.settings, "bedrock_aws_access_key_id", "access")
    monkeypatch.setattr(llm_client.settings, "bedrock_aws_secret_access_key", "secret")
    monkeypatch.setattr(llm_client.settings, "bedrock_aws_session_token", "token")
    monkeypatch.setattr(llm_client.settings, "bedrock_aws_default_region", "us-west-2")
    monkeypatch.setattr(llm_client.settings, "bedrock_aws_profile", "")
    monkeypatch.setattr(llm_client.settings, "aws_profile", "")
    monkeypatch.setattr(llm_client.settings, "bedrock_reverse_proxy", "proxy.example.com")
    monkeypatch.setattr(llm_client.anthropic, "AsyncAnthropicBedrock", FakeBedrockClient)

    client = make_async_sdk_client()

    assert isinstance(client, FakeBedrockClient)
    assert seen == {
        "aws_region": "us-west-2",
        "aws_access_key": "access",
        "aws_secret_key": "secret",
        "aws_session_token": "token",
        "base_url": "https://proxy.example.com",
    }


def test_cli_local_without_backend_defaults_to_claude_code(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--fusions", "TP53::BRAF", "--local"],
    )
    args = parse_args()
    assert args.local == DEFAULT_LOCAL_BACKEND


def test_cli_local_accepts_codex(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--fusions", "TP53::BRAF", "--local", "codex"],
    )
    args = parse_args()
    assert args.local == "codex"


def test_cli_accepts_output_csv(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--fusions",
            "TP53::BRAF",
            "--output",
            "results.json",
            "--output-csv",
            "results.csv",
        ],
    )
    args = parse_args()
    assert args.output == "results.json"
    assert args.output_csv == "results.csv"


def test_annotate_request_accepts_local_backend():
    request = AnnotateRequest(fusions=["TP53::BRAF"], local_backend="codex")
    assert request.local_backend == "codex"


async def test_benchmark_run_pipeline_passes_local_backend(monkeypatch):
    seen = {}

    async def fake_run_pipeline(fusions, local_backend=None):
        seen["fusions"] = fusions
        seen["local_backend"] = local_backend

        class Result:
            def model_dump(self):
                return {"annotations": []}

        return Result()

    monkeypatch.setattr("src.pipeline.orchestrator.run_pipeline", fake_run_pipeline)

    result = await _run_pipeline(["TP53::BRAF"], local_backend="codex")

    assert result == {"annotations": []}
    assert seen == {"fusions": ["TP53::BRAF"], "local_backend": "codex"}
