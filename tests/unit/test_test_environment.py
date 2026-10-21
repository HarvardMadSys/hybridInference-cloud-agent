"""Every test runs with CI's environment (see ``tests/conftest.py``)."""

from __future__ import annotations

import os


def _umask() -> int:
    current = os.umask(0o022)
    os.umask(current)
    return current


def test_every_test_starts_with_cis_umask() -> None:
    assert _umask() == 0o022


def test_a_launchers_codex_version_never_reaches_a_test() -> None:
    assert "CODEX_VERSION" not in os.environ


def test_a_test_may_leave_the_umask_changed() -> None:
    # As a CLI's main() run in-process does: it sets the umask for its own
    # process and never expects to share it.
    os.umask(0o077)


def test_the_next_test_still_starts_with_cis_umask() -> None:
    assert _umask() == 0o022
