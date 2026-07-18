"""
Unified LLM client supporting two execution paths:
  - SDK path (default): AsyncAnthropic with tool_use + prompt caching
  - Local path (--local BACKEND): local agent CLI with JSON-in-text prompting

Local mode lets team members run the full pipeline without an Anthropic API key,
routing synthesis and selection calls through their local agent CLI instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import tempfile
from typing import Any, Dict, List, Optional

import anthropic

from src.config import settings
from src.pipeline.local_agents import (
    LOCAL_BACKEND_COMMANDS,
    local_agent_subprocess_env,
    resolve_local_agent_path,
)

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_BACKEND = "claude-code"
LOCAL_BACKENDS = ("claude-code", "codex", "copilot", "antigravity")


def _make_sdk_client() -> anthropic.AsyncAnthropic:
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. "
            "Either add it to .env or run with --local to use a local agent CLI."
        )
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def resolve_local_backend(local_mode: bool = False, local_backend: Optional[str] = None) -> Optional[str]:
    """Normalize local-mode flags into a concrete backend name."""
    if local_backend:
        if local_backend not in LOCAL_BACKENDS:
            raise ValueError(
                f"Unsupported local backend {local_backend!r}. "
                f"Expected one of: {', '.join(LOCAL_BACKENDS)}"
            )
        return local_backend
    if local_mode:
        return DEFAULT_LOCAL_BACKEND
    return None


async def complete_with_tool(
    *,
    model: str,
    system: str,
    user: str,
    tool: Dict[str, Any],
    max_tokens: int = 2048,
    local_mode: bool = False,
    local_backend: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Call an LLM with a single required tool and return the tool input dict.

    SDK mode  — uses tool_use with prompt caching on the system prompt.
    Local mode — shells out to the selected local agent CLI with JSON-in-text prompting.
    """
    backend = resolve_local_backend(local_mode=local_mode, local_backend=local_backend)
    if backend:
        return await _complete_local(system=system, user=user, tool=tool, backend=backend)
    return await _complete_sdk(
        model=model, system=system, user=user, tool=tool, max_tokens=max_tokens
    )


async def _complete_sdk(
    *,
    model: str,
    system: str,
    user: str,
    tool: Dict[str, Any],
    max_tokens: int,
) -> Dict[str, Any]:
    client = _make_sdk_client()
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input  # type: ignore[return-value]
    return {}


async def _complete_local(
    *,
    system: str,
    user: str,
    tool: Dict[str, Any],
    backend: str,
) -> Dict[str, Any]:
    """Shell out to a local agent CLI and parse JSON-in-text response."""
    schema_str = json.dumps(tool["input_schema"], indent=2)
    full_prompt = (
        f"{system}\n\n"
        f"---\n\n"
        f"{user}\n\n"
        f"---\n\n"
        f"Respond with a single JSON object that exactly matches the following schema "
        f"(tool name: {tool['name']}):\n"
        f"{schema_str}\n\n"
        f"Output ONLY the JSON object. Begin your response with {{ and end with }}. "
        f"No markdown fences, no prose before or after the JSON."
    )

    if backend == "claude-code":
        raw = await _run_claude_code(full_prompt, tool["name"])
    elif backend == "codex":
        raw = await _run_codex(full_prompt, tool["name"])
    elif backend == "copilot":
        raw = await _run_copilot(full_prompt, tool["name"])
    elif backend == "antigravity":
        raw = await _run_antigravity(full_prompt, tool["name"])
    else:
        raise ValueError(f"Unsupported local backend {backend!r}")

    return _extract_json(raw, tool["name"])


async def _communicate(
    args: List[str],
    *,
    tool_name: str,
    input_text: Optional[str] = None,
    timeout: int = 180,
) -> tuple[str, str]:
    logger.debug("Local mode: invoking `%s` for tool %s", " ".join(args[:2]), tool_name)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=local_agent_subprocess_env(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Local agent command {args[0]!r} was not found. "
            "If you launched the standalone app from Finder, the app may not see "
            "your shell PATH. Open Setup, reinstall or refresh the local agent, "
            "or set the matching AGCG_*_PATH environment override."
        ) from exc

    try:
        input_bytes = input_text.encode() if input_text is not None else None
        stdout, stderr = await asyncio.wait_for(proc.communicate(input_bytes), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"`{' '.join(args[:2])}` timed out after {timeout} s for tool {tool_name}")

    if proc.returncode != 0:
        out_snippet = stdout.decode(errors="replace")[:500] if stdout else ""
        err_snippet = stderr.decode(errors="replace")[:500] if stderr else ""
        raise RuntimeError(
            f"`{' '.join(args[:2])}` exited with code {proc.returncode} for tool {tool_name}: "
            f"{err_snippet or out_snippet}"
        )

    return stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()


async def _run_claude_code(prompt: str, tool_name: str) -> str:
    stdout, _ = await _communicate(
        [_local_agent_executable("claude-code"), "-p", prompt],
        tool_name=tool_name,
    )
    return stdout


async def _run_codex(prompt: str, tool_name: str) -> str:
    fd, output_path = tempfile.mkstemp(prefix="codex-local-", suffix=".txt")
    os.close(fd)
    try:
        args = [
            _local_agent_executable("codex"),
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "--output-last-message",
            output_path,
            "-",
        ]
        stdout, _ = await _communicate(args, tool_name=tool_name, input_text=prompt)
        try:
            with open(output_path) as f:
                final_message = f.read().strip()
        except OSError:
            final_message = ""
        return final_message or stdout
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


async def _run_copilot(prompt: str, tool_name: str) -> str:
    stdout, _ = await _communicate(
        [_local_agent_executable("copilot"), "-p", prompt, "-s", "--no-ask-user"],
        tool_name=tool_name,
        timeout=240,
    )
    return stdout


async def _run_antigravity(prompt: str, tool_name: str) -> str:
    command = os.environ.get("ANTIGRAVITY_LOCAL_COMMAND", "antigravity -p {prompt}")
    args = [part if part != "{prompt}" else prompt for part in shlex.split(command)]
    if "{prompt}" not in command:
        args.append(prompt)
    if args and args[0] == "antigravity":
        args[0] = _local_agent_executable("antigravity")
    stdout, _ = await _communicate(args, tool_name=tool_name)
    return stdout


def _local_agent_executable(backend: str) -> str:
    command = LOCAL_BACKEND_COMMANDS[backend]
    path = resolve_local_agent_path(command)
    if path:
        return path
    raise RuntimeError(
        f"{command!r} was not detected. Install the local agent from Setup, then "
        f"refresh status. If it is already installed in a custom location, set "
        f"the AGCG_*_PATH override for that agent."
    )


def _extract_json(text: str, tool_name: str) -> Dict[str, Any]:
    """Extract the first valid top-level JSON object from arbitrary text."""
    # Direct parse (ideal path — model output is clean JSON)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences if present
    stripped = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Greedy outermost object extraction
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    logger.error(
        "Could not parse JSON from local mode output for tool %s:\n%.500s",
        tool_name,
        text,
    )
    return {}
