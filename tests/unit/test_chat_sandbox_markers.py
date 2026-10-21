"""The marker programs, and how the manager runs them.

The programs run for real against a temporary directory. In production they
run as guest root inside the exact container, expecting uid and gid 0. Here
they run as whoever runs the tests, told to expect that user instead: the
checks are the same checks, against a directory the test controls. What
production tells them is pinned at the manager's exec call instead.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from cloud_agent_host import chat_sandbox_markers as markers
from cloud_agent_host.chat_sandbox import ChatSandboxManager
from cloud_agent_host.chat_sandbox_models import ChatSandboxSpec
from cloud_agent_host.sandbox import ContainerBackend
from cloud_agent_shared.environment_snapshots import SETUP_MARKER_NAME

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


CONTAINER = "sha256:container-one"
PAYLOAD = b"receipt"
INITIAL = (markers.INITIAL_ARCHIVE_MARKER_NAME, markers.INITIAL_ARCHIVE_MARKER_MAX_BYTES)
SETUP = (SETUP_MARKER_NAME, markers.SETUP_MARKER_MAX_BYTES)
FINAL = (markers.FINAL_ARCHIVE_MARKER_NAME, markers.FINAL_ARCHIVE_MARKER_MAX_BYTES)

#: Each manager method that runs a program: the program, its marker and bound,
#: what follows the five arguments every program takes, and whether it sends a
#: payload on stdin.
INVOCATIONS = {
    "read_initial_archive_marker": (markers._READ_PROGRAM, INITIAL, (), False),
    # The only safe retry proof: a different initial marker is never replaced.
    "write_initial_archive_marker": (markers._WRITE_PROGRAM, INITIAL, ("0",), True),
    "delete_initial_archive_marker": (markers._DELETE_PROGRAM, INITIAL, (), True),
    "read_environment_setup_marker": (markers._READ_PROGRAM, SETUP, (), False),
    "write_environment_setup_marker": (markers._WRITE_PROGRAM, SETUP, ("1",), True),
    "read_running_final_archive_marker": (markers._READ_PROGRAM, FINAL, (), False),
    "write_final_archive_marker": (markers._WRITE_PROGRAM, FINAL, ("1",), True),
    "clear_final_archive_marker": (markers._CLEAR_PROGRAM, FINAL, (), False),
}


@pytest.mark.parametrize("method", list(INVOCATIONS))
def test_the_manager_runs_each_program_as_guest_root_trusting_only_root(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    program, (name, limit), extra, sends_payload = INVOCATIONS[method]
    spec = ChatSandboxSpec(
        thread_id="athr_01", sandbox_id="asbx_01", generation=3, runtime_id="claude-code"
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    # The final marker's methods first check that this is the exact running container.
    monkeypatch.setattr(
        manager, "_require", lambda _spec: SimpleNamespace(container_id=CONTAINER, running=True)
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def exec_capture(_spec: ChatSandboxSpec, argv: list[str], **options: object) -> SimpleNamespace:
        calls.append((list(argv), options))
        return SimpleNamespace(returncode=0, stdout=PAYLOAD, stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", exec_capture)
    payload = (PAYLOAD,) if sends_payload else ()

    getattr(manager, method)(spec, *payload, expected_container_id=CONTAINER)

    ((argv, options),) = calls
    # Root, name and bound, then the only owner a program trusts: guest root.
    assert argv == [
        "python3",
        "-c",
        program,
        markers.INITIAL_ARCHIVE_MARKER_ROOT,
        name,
        str(limit),
        "0",
        "0",
        *extra,
    ]
    assert options == {
        "workdir": "/",
        "timeout_s": 20,
        "user_uid": 0,
        "user_gid": 0,
        "expected_container_id": CONTAINER,
        **({"input_bytes": PAYLOAD} if sends_payload else {}),
    }
