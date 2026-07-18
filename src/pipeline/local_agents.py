"""Local agent CLI discovery for shell and packaged app contexts."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

LOCAL_BACKEND_COMMANDS = {
    "claude-code": "claude",
    "codex": "codex",
    "copilot": "copilot",
    "antigravity": "antigravity",
}
LOCAL_BACKEND_VERSION_ARGS = {
    "claude-code": ["--version"],
    "codex": ["--version"],
    "copilot": ["version"],
    "antigravity": ["--version"],
}

COMMAND_PATH_OVERRIDES = {
    "claude": "AGCG_CLAUDE_CODE_PATH",
    "codex": "AGCG_CODEX_PATH",
    "copilot": "AGCG_COPILOT_PATH",
    "antigravity": "AGCG_ANTIGRAVITY_PATH",
}


def local_agent_subprocess_env() -> dict[str, str]:
    """Return an env with common CLI install locations visible to child agents."""
    env = os.environ.copy()
    env["PATH"] = _augmented_path()
    return env


def resolve_local_agent_path(command: str) -> Optional[str]:
    """Find a local agent executable even when launched from a macOS .app."""
    override_name = COMMAND_PATH_OVERRIDES.get(command)
    if override_name:
        override = os.environ.get(override_name)
        if override:
            return override

    path = shutil.which(command, path=_augmented_path())
    if path:
        return path

    for candidate in _candidate_paths(command):
        if candidate.exists():
            return str(candidate)

    return _command_from_login_shell(command)


def _augmented_path() -> str:
    seen = set()
    path_dirs = []
    for item in [
        *os.environ.get("PATH", "").split(os.pathsep),
        *_login_shell_path_dirs(),
        *_common_path_dirs(),
    ]:
        if item and item not in seen:
            seen.add(item)
            path_dirs.append(item)
    return os.pathsep.join(path_dirs)


def _candidate_paths(command: str) -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / command,
        home / ".npm-global" / "bin" / command,
        home / ".bun" / "bin" / command,
        home / ".claude" / "local" / command,
        Path("/opt/homebrew/bin") / command,
        Path("/usr/local/bin") / command,
        Path("/usr/bin") / command,
        Path("/bin") / command,
    ]
    if platform.system() == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.extend(
                [
                    Path(local_app_data)
                    / "Programs"
                    / "OpenAI"
                    / "Codex"
                    / "bin"
                    / f"{command}.exe",
                    Path(local_app_data)
                    / "Programs"
                    / command
                    / f"{command}.exe",
                ]
            )
    return candidates


def _common_path_dirs() -> list[str]:
    home = Path.home()
    dirs = [
        home / ".local" / "bin",
        home / ".npm-global" / "bin",
        home / ".bun" / "bin",
        home / ".claude" / "local",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
    ]
    if platform.system() == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            dirs.extend(
                [
                    Path(local_app_data) / "Programs" / "OpenAI" / "Codex" / "bin",
                    Path(local_app_data) / "Programs",
                ]
            )
    return [str(path) for path in dirs]


def _login_shell_path_dirs() -> list[str]:
    shell = os.environ.get("SHELL")
    if not shell or platform.system() == "Windows":
        return []
    try:
        completed = subprocess.run(
            [shell, "-lc", 'printf "%s" "$PATH"'],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [item for item in completed.stdout.split(os.pathsep) if item]


def _command_from_login_shell(command: str) -> Optional[str]:
    shell = os.environ.get("SHELL")
    if not shell or platform.system() == "Windows":
        return None
    try:
        completed = subprocess.run(
            [shell, "-lc", f"command -v {command}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = completed.stdout.strip().splitlines()
    return path[0] if completed.returncode == 0 and path else None
