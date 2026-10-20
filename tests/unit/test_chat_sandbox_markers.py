"""The marker programs, run for real against a temporary directory.

In production they run as guest root inside the exact container, expecting
uid and gid 0. Here they run as whoever runs the tests, told to expect that
user instead: the checks are the same checks, against a directory the test
controls.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from cloud_agent_host import chat_sandbox_markers as markers

if TYPE_CHECKING:
    from pathlib import Path

NAME = "final-archive-v1.json"
LIMIT = 64


def _run(
    program: str, root: Path, *extra: str, stdin: bytes = b"", limit: int = LIMIT
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(root),
            NAME,
            str(limit),
            str(os.getuid()),
            str(os.getgid()),
            *extra,
        ],
        input=stdin,
        capture_output=True,
        check=False,
        timeout=30,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    # The programs refuse a parent anyone else may write to.
    tmp_path.chmod(0o755)
    return tmp_path / "freeinference-broker"


def test_an_absent_marker_is_the_one_soft_outcome(root: Path) -> None:
    result = _run(markers._READ_PROGRAM, root)

    assert result.returncode == markers._ABSENT
    assert result.stdout == b""
    # The read created the private directory it reads from.
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_a_published_marker_reads_back_exactly_and_privately(root: Path) -> None:
    assert _run(markers._WRITE_PROGRAM, root, "0", stdin=b"receipt").returncode == 0

    assert _run(markers._READ_PROGRAM, root).stdout == b"receipt"
    assert stat.S_IMODE((root / NAME).stat().st_mode) == 0o600
    # No temporary file is left beside it.
    assert sorted(os.listdir(root)) == [NAME]


def test_a_different_marker_is_replaced_only_when_the_caller_says_so(root: Path) -> None:
    _run(markers._WRITE_PROGRAM, root, "0", stdin=b"first")

    same = _run(markers._WRITE_PROGRAM, root, "0", stdin=b"first")
    refused = _run(markers._WRITE_PROGRAM, root, "0", stdin=b"second")
    assert same.returncode == 0
    assert refused.returncode != 0
    assert b"a different marker already exists" in refused.stderr
    assert _run(markers._READ_PROGRAM, root).stdout == b"first"

    assert _run(markers._WRITE_PROGRAM, root, "1", stdin=b"second").returncode == 0
    assert _run(markers._READ_PROGRAM, root).stdout == b"second"


@pytest.mark.parametrize("payload", [b"", b"x" * (LIMIT + 1)])
def test_a_payload_outside_its_bound_is_never_written(root: Path, payload: bytes) -> None:
    result = _run(markers._WRITE_PROGRAM, root, "0", stdin=payload)

    assert result.returncode != 0
    assert not (root / NAME).exists()


def test_a_symlinked_marker_fails_closed(root: Path, tmp_path: Path) -> None:
    root.mkdir(mode=0o700)
    (tmp_path / "elsewhere").write_bytes(b"forged")
    (root / NAME).symlink_to(tmp_path / "elsewhere")

    result = _run(markers._READ_PROGRAM, root)

    assert result.returncode not in (0, markers._ABSENT)
    assert b"marker file cannot be trusted" in result.stderr


@pytest.mark.parametrize(
    ("mode", "size"), [(0o644, 8), (0o600, LIMIT + 1)], ids=["shared mode", "over the limit"]
)
def test_a_marker_with_the_wrong_mode_or_size_fails_closed(
    root: Path, mode: int, size: int
) -> None:
    root.mkdir(mode=0o700)
    marker = root / NAME
    marker.write_bytes(b"x" * size)
    marker.chmod(mode)

    result = _run(markers._READ_PROGRAM, root)

    assert result.returncode not in (0, markers._ABSENT)
    assert b"marker file cannot be trusted" in result.stderr


def test_a_shared_marker_directory_fails_closed(root: Path) -> None:
    root.mkdir()
    root.chmod(0o755)

    result = _run(markers._READ_PROGRAM, root)

    assert result.returncode not in (0, markers._ABSENT)
    assert b"marker directory cannot be trusted" in result.stderr


def test_clearing_removes_the_marker_and_clearing_nothing_is_fine(root: Path) -> None:
    assert _run(markers._CLEAR_PROGRAM, root).returncode == 0
    _run(markers._WRITE_PROGRAM, root, "0", stdin=b"fence")

    assert _run(markers._CLEAR_PROGRAM, root).returncode == 0
    assert _run(markers._READ_PROGRAM, root).returncode == markers._ABSENT


def test_deleting_removes_only_the_marker_the_caller_read(root: Path) -> None:
    _run(markers._WRITE_PROGRAM, root, "0", stdin=b"receipt")

    stale = _run(markers._DELETE_PROGRAM, root, stdin=b"another")
    assert stale.returncode != 0
    assert b"marker changed before deletion" in stale.stderr
    assert _run(markers._READ_PROGRAM, root).stdout == b"receipt"

    assert _run(markers._DELETE_PROGRAM, root, stdin=b"receipt").returncode == 0
    assert _run(markers._READ_PROGRAM, root).returncode == markers._ABSENT
