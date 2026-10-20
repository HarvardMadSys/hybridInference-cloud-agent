"""Compatibility code says when it may go, and goes by then.

Nothing fails when the last old host or control plane is gone, so a
rolling-deploy shim left to itself stays forever. Each one is marked

    # COMPAT(remove-after=YYYY-MM-DD): <what makes it removable>

and the second test below fails once that date has passed: remove the shim,
or move the date with the reason it must stay.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCANNED = ("apps/backend", "apps/frontend/src", "deploy", "scripts", "migrations")
SUFFIXES = {".py", ".ts", ".tsx", ".sh", ".yml", ".yaml"}
MARKER = re.compile(r"COMPAT\(remove-after=(?P<date>[0-9-]+)\): \S")


def _marker_lines() -> list[tuple[str, str]]:
    found = []
    for root in SCANNED:
        for path in sorted((REPO / root).rglob("*")):
            if path.suffix not in SUFFIXES or "node_modules" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "COMPAT(" in line:
                    found.append((f"{path.relative_to(REPO)}:{number}", line.strip()))
    return found


def _removal_date(line: str) -> date | None:
    match = MARKER.search(line)
    if match is None:
        return None
    try:
        return date.fromisoformat(match["date"])
    except ValueError:
        return None


def test_every_marker_names_a_date_and_a_condition() -> None:
    malformed = [place for place, line in _marker_lines() if _removal_date(line) is None]
    assert not malformed, f"expected COMPAT(remove-after=YYYY-MM-DD): <condition> at {malformed}"


def test_no_compatibility_code_outlives_its_date() -> None:
    today = datetime.now(UTC).date()
    expired = [
        f"{place}: {line}"
        for place, line in _marker_lines()
        if (removal := _removal_date(line)) is not None and removal < today
    ]
    assert not expired, "past its removal date:\n" + "\n".join(expired)


@pytest.mark.parametrize(
    ("line", "valid"),
    [
        ("# COMPAT(remove-after=2026-11-30): every host sends it.", True),
        ("// COMPAT(remove-after=2026-11-30): every host sends it.", True),
        ("# COMPAT(remove-after=2026-13-01): no such month.", False),
        ("# COMPAT(remove-after=2026-11-30):", False),
        ("# COMPAT: someday.", False),
    ],
)
def test_the_marker_needs_a_real_date_and_a_reason(line: str, valid: bool) -> None:
    assert (_removal_date(line) is not None) is valid
