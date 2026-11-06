"""The gate must be able to fail.

A blocking type check is evidence only if it has been observed rejecting the
thing it is supposed to reject. This runs mypy over a deliberately mis-wired
fixture and fails when mypy accepts it, so "a mis-wired port would be caught"
is a demonstrated property rather than a hoped-for one.

The fixture lives under ``tests/`` and is therefore outside the gate's own
``files = ["cloud_agent"]`` scope; it can never make the real gate red.

A positive control is checked the same way: the repository classes the
assembly names directly, because their mirror ports were removed, must still
satisfy the narrow Protocols their callers declare.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "ports_negative" / "miswired_port.py"
POSITIVE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "ports_positive" / "narrow_callers.py"

#: Each shape, and the rejection it must produce. Matching on the message
#: rather than on a line number keeps this about the reason for the rejection:
#: a renamed function would move a line, but the type named in the message is
#: the thing being tested. All three are ``arg-type`` -- mypy rejects the
#: argument at the call, which is where a mis-wired port is wired.
EXPECTED_CASES = (
    (
        "missing member",
        r'incompatible type "NotAnAttemptStore"; expected "AttemptCommands"',
    ),
    (
        "wrong keyword, every member present",
        r'incompatible type "WrongSignature"; expected "AttemptCommands"',
    ),
    (
        "row offered where a capability was required",
        r'incompatible type "dict\[str, Any\]"; expected "HostPoolRepository"',
    ),
)


def _mypy(cache: Path, fixture: Path) -> subprocess.CompletedProcess[str]:
    """Run mypy over one fixture, with the project's own configuration."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            f"--cache-dir={cache}",
            "--no-error-summary",
            "--show-error-codes",
            str(fixture),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def mypy_run(pytestconfig: pytest.Config, worker_id: str) -> subprocess.CompletedProcess[str]:
    """Run mypy once over the fixture, with the project's own configuration."""
    # Mypy invalidates changed source/configuration itself. Reuse its import
    # cache across runs without sharing the production gate's cache, and keep
    # concurrent pytest workers from writing the same cache files.
    return _mypy(pytestconfig.cache.mkdir("mypy-negative") / worker_id, FIXTURE)


def test_mypy_accepts_the_narrow_callers_of_unported_repositories(
    pytestconfig: pytest.Config, worker_id: str
) -> None:
    """The repositories the assembly names still fit the Protocols their callers use."""
    run = _mypy(pytestconfig.cache.mkdir("mypy-positive") / worker_id, POSITIVE_FIXTURE)
    assert run.returncode == 0, (
        "a repository the assembly names no longer satisfies its caller's Protocol:\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )


def test_mypy_rejects_the_miswired_fixture(
    mypy_run: subprocess.CompletedProcess[str],
) -> None:
    """If this passes, the fixture was accepted and the gate proves nothing."""
    assert mypy_run.returncode != 0, (
        "mypy accepted a deliberately mis-wired fixture, so the gate cannot be "
        f"shown to reject one:\nstdout:\n{mypy_run.stdout}\nstderr:\n{mypy_run.stderr}"
    )


@pytest.mark.parametrize("label, pattern", EXPECTED_CASES, ids=[c[0] for c in EXPECTED_CASES])
def test_each_negative_case_is_reported(
    mypy_run: subprocess.CompletedProcess[str], label: str, pattern: str
) -> None:
    """Each shape is rejected for its own reason, not by one blanket failure."""
    assert re.search(pattern, mypy_run.stdout), (
        f"{label}: expected a rejection matching {pattern!r}, got:\n{mypy_run.stdout}"
    )
