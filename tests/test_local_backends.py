"""Unit tests for local LLM backend selection."""

from pathlib import Path

import pytest

from benchmarks.run_benchmark import _run_pipeline
from src.cli import parse_args
from src.models.schema import AnnotateRequest
from src.pipeline.local_agents import local_agent_subprocess_env, resolve_local_agent_path
from src.pipeline.llm_client import (
    DEFAULT_LOCAL_BACKEND,
    _run_claude_code,
    resolve_local_backend,
)


def test_resolve_local_backend_defaults_for_legacy_bool():
    assert resolve_local_backend(local_mode=True) == DEFAULT_LOCAL_BACKEND


def test_resolve_local_backend_prefers_explicit_backend():
    assert resolve_local_backend(local_mode=False, local_backend="codex") == "codex"
    assert resolve_local_backend(local_mode=False, local_backend="copilot") == "copilot"
    assert (
        resolve_local_backend(local_mode=False, local_backend="antigravity")
        == "antigravity"
    )


def test_resolve_local_backend_rejects_unknown_backend():
    with pytest.raises(ValueError):
        resolve_local_backend(local_backend="unknown")


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


def test_cli_local_accepts_copilot(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--fusions", "TP53::BRAF", "--local", "copilot"],
    )
    args = parse_args()
    assert args.local == "copilot"


def test_cli_local_accepts_antigravity(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--fusions", "TP53::BRAF", "--local", "antigravity"],
    )
    args = parse_args()
    assert args.local == "antigravity"


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

    request = AnnotateRequest(fusions=["TP53::BRAF"], local_backend="copilot")
    assert request.local_backend == "copilot"

    request = AnnotateRequest(fusions=["TP53::BRAF"], local_backend="antigravity")
    assert request.local_backend == "antigravity"


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


def test_local_agent_resolver_finds_hidden_user_bin(tmp_path, monkeypatch):
    claude_path = tmp_path / ".local" / "bin" / "claude"
    claude_path.parent.mkdir(parents=True)
    claude_path.write_text("#!/bin/sh\n", encoding="utf-8")
    claude_path.chmod(0o755)

    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("src.pipeline.local_agents.shutil.which", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.pipeline.local_agents._command_from_login_shell", lambda command: None)
    monkeypatch.setattr("src.pipeline.local_agents._login_shell_path_dirs", lambda: [])

    assert resolve_local_agent_path("claude") == str(claude_path)
    assert str(tmp_path / ".local" / "bin") in local_agent_subprocess_env()["PATH"]


async def test_claude_code_uses_resolved_absolute_path(monkeypatch):
    seen = {}

    async def fake_communicate(args, *, tool_name, input_text=None, timeout=180):
        seen["args"] = args
        seen["tool_name"] = tool_name
        return '{"ok": true}', ""

    monkeypatch.setattr(
        "src.pipeline.llm_client.resolve_local_agent_path",
        lambda command: "/Users/person/.local/bin/claude" if command == "claude" else None,
    )
    monkeypatch.setattr("src.pipeline.llm_client._communicate", fake_communicate)

    result = await _run_claude_code("prompt", "curate_gene")

    assert result == '{"ok": true}'
    assert seen["args"] == ["/Users/person/.local/bin/claude", "-p", "prompt"]
    assert seen["tool_name"] == "curate_gene"
