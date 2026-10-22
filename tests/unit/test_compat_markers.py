"""Compatibility code says when it may go, and goes by then.

Nothing fails when the last old host or control plane is gone, so a
rolling-deploy shim left to itself stays forever. Each one is marked

    # COMPAT(remove-after=YYYY-MM-DD): <what makes it removable>

and the second test below fails once that date has passed: remove the shim,
or move the date with the reason it must stay. Every text file under the
scanned roots is read, whatever its name, and a line that says COMPAT in any
case is taken for a marker, so one written any other way is reported rather
than skipped.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCANNED = ("apps/backend", "apps/frontend/src", "deploy", "scripts", "migrations")
CANDIDATE = re.compile(r"\bCOMPAT\b", re.IGNORECASE)
MARKER = re.compile(r"COMPAT\(remove-after=(?P<date>\d{4}-\d{2}-\d{2})\): \S")


def _text(path: Path) -> str | None:
    """The file's text, or None for a binary file."""
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _marker_lines(repo: Path = REPO) -> list[tuple[str, str]]:
    found = []
    for root in SCANNED:
        for path in sorted((repo / root).rglob("*")):
            if not path.is_file() or "node_modules" in path.parts:
                continue
            for number, line in enumerate((_text(path) or "").splitlines(), 1):
                if CANDIDATE.search(line):
                    found.append((f"{path.relative_to(repo)}:{number}", line.strip()))
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
        # Python reads this as 2026-11-30, but it is not the convention's form.
        ("# COMPAT(remove-after=20261130): every host sends it.", False),
        ("# COMPAT(remove-after=2026-11-30):", False),
        ("# COMPAT: someday.", False),
        ("# compat(remove-after=2026-11-30): every host sends it.", False),
        ("# COMPAT (remove-after=2026-11-30): every host sends it.", False),
    ],
)
def test_any_spelling_of_the_marker_is_found_and_only_one_is_valid(
    tmp_path: Path, line: str, valid: bool
) -> None:
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/shim.sh").write_text(f"#!/bin/sh\n{line}\n")

    assert _marker_lines(tmp_path) == [("deploy/shim.sh:2", line)]
    assert (_removal_date(line) is not None) is valid


@pytest.mark.parametrize(
    "name",
    [
        "apps/backend/cloud_agent/shim.py",
        "deploy/sandbox/Dockerfile.agent-sandbox",
        "deploy/host/.env.example",
        "deploy/sandbox/member-process-reaper",
    ],
)
def test_every_text_file_is_read_whatever_its_name_and_no_binary_one(
    tmp_path: Path, name: str
) -> None:
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text("# COMPAT(remove-after=2026-11-30): every host sends it.\n")
    (path.parent / "shim.cpython-312.pyc").write_bytes(b"\0\xffCOMPAT(remove-after=never)")

    assert [place for place, _line in _marker_lines(tmp_path)] == [f"{name}:1"]
