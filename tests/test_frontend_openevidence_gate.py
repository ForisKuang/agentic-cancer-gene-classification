"""Runs tests/frontend/test_openevidence_gate.js under pytest.

There's no existing JS/frontend test runner in this repo (src/static/app.js
has never had automated coverage before this) — a plain Node script under
tests/frontend/ is the lightest-weight way to exercise app.js's actual
production code (not a Python reimplementation of its gating logic) without
adding a new JS toolchain (package.json, node_modules, a test framework) for
one behavior. See tests/frontend/openevidence_gate_harness.js for how it
loads app.js's declarations into a vm context with a minimal fake DOM.

Proves review gap #1: when settings.openevidence_enabled is off, the
frontend must render no OpenEvidence sidecar card and issue no GET
/v1/genes/{gene}/openevidence request — not just fall back to a runtime
available:false response after a wasted round-trip.

Also proves the sidecar sends the gene's fusion (annotation.fusions[0], the
same value openevidence_warmup.py warms) as `fusion=` so fusion inputs hit
the backend's fusion-specific cache slot, that fusion and plain-gene lookups
for the same gene don't share a client-side cache entry, and that a fusion
gene still issues zero requests with the flag off.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_FRONTEND_DIR = Path(__file__).parent / "frontend"
_TEST_SCRIPT = _FRONTEND_DIR / "test_openevidence_gate.js"
_POLLING_TEST_SCRIPT = _FRONTEND_DIR / "test_openevidence_polling.js"


def _run_node_script(script: Path, label: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")

    result = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"frontend OpenEvidence {label} tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_openevidence_frontend_gate():
    _run_node_script(_TEST_SCRIPT, "gate")


def test_openevidence_frontend_pending_polling():
    """The sidecar card's "pending + poll" handling (see
    tests/frontend/test_openevidence_polling.js): poll -> ready renders,
    failed/timeout removes the card, polling stops when the card is removed
    or replaced or the flag turns off, the flag off still makes zero
    requests, and pending cards don't hog the client fetch queue."""
    _run_node_script(_POLLING_TEST_SCRIPT, "pending-polling")
