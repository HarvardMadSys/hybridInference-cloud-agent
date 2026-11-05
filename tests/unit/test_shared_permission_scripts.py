"""Run the shared-permission scripts against a real directory tree.

The member pass runs at the end of every turn, so it must change only what the
turn left without its group bits: an entry that is already open keeps its
ctime, and a second pass over an unchanged tree changes nothing. These tests
run the scripts the manager builds, with the sandbox's two roots moved under a
temporary directory, as the user that owns the tree.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from cloud_agent_host.chat_sandbox import (
    CHAT_STATE_DIR,
    CHAT_WORKDIR,
    MEMBER_UID_BASE,
    SHARED_WORKSPACE_PERMISSIONS_SCRIPT,
    ChatSandboxManager,
    ChatSandboxSpec,
)
from cloud_agent_host.sandbox import ContainerBackend

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(shutil.which("find") is None, reason="needs find(1)")


def _gnu_find() -> bool:
    found = subprocess.run(["find", "--version"], capture_output=True, text=True, check=False)
    return "GNU findutils" in found.stdout


def _member_script(monkeypatch: pytest.MonkeyPatch) -> str:
    spec = ChatSandboxSpec(
        thread_id="athr_shared",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
        resident_slot_capacity=2,
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    scripts: list[str] = []

    def capture(_spec, argv, **_kwargs):
        scripts.append(argv[2])
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", capture)
    manager.normalize_member_files(
        spec, slot_id=0, user_uid=MEMBER_UID_BASE, user_gid=manager.backend.gid
    )
    return scripts[0]


def _relocated(script: str, root: Path) -> str:
    """Point the script at ``root``'s two trees instead of the sandbox's roots."""
    moved = script.replace(CHAT_STATE_DIR, "\0state\0").replace(CHAT_WORKDIR, "\0work\0")
    assert CHAT_STATE_DIR not in moved and CHAT_WORKDIR not in moved
    return moved.replace("\0state\0", str(root / "agent-state")).replace(
        "\0work\0", str(root / "workspace")
    )


def _run(script: str, *args: str) -> None:
    result = subprocess.run(
        ["/bin/sh", "-c", script, "sh", *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _make(path: Path, mode: int, *, directory: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if directory:
        path.mkdir(exist_ok=True)
    else:
        path.write_text(path.name)
    os.chmod(path, mode)
    return path


def _ctimes(root: Path) -> dict[Path, int]:
    return {path: path.lstat().st_ctime_ns for path in [root, *root.rglob("*")]}


def _settle() -> None:
    # A chmod stamps the current time; make any stamp a later one than the
    # ctimes recorded before it, even on a coarse kernel clock.
    time.sleep(0.05)


def test_the_member_pass_opens_what_the_turn_left_and_nothing_else(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    state = tmp_path / "agent-state"
    _make(workspace, 0o2775, directory=True)
    package = _make(workspace / "pkg", 0o755, directory=True)
    module = _make(workspace / "pkg" / "mod.py", 0o644)
    opened = _make(workspace / "done", 0o2775, directory=True)
    kept = _make(workspace / "done" / "ok.txt", 0o664)
    _make(state, 0o2775, directory=True)
    notes = _make(state / "notes.txt", 0o600)
    platform = _make(state / ".platform", 0o700, directory=True)
    receipt = _make(state / ".platform" / "receipt", 0o600)
    home = _make(state / "codex-homes" / "member-0", 0o700, directory=True)
    settings = _make(state / "claude" / "settings.json", 0o600)
    history = _make(state / "claude" / "projects" / "project", 0o700, directory=True)
    transcript = _make(state / "claude" / "projects" / "project" / "child.jsonl", 0o600)
    script = _relocated(_member_script(monkeypatch), tmp_path)
    untouched = {path: path.lstat().st_ctime_ns for path in (opened, kept)}
    _settle()

    _run(script, str(os.getuid()))

    for directory in (package, history):
        assert _mode(directory) & 0o2070 == 0o2070, directory
    for file in (module, notes, transcript):
        assert _mode(file) & 0o060 == 0o060, file
    # Pruned: the platform's own files, members' private homes, and every
    # shared Claude file but the projects.
    assert _mode(platform) == 0o700 and _mode(receipt) == 0o600
    assert _mode(home) == 0o700
    assert _mode(settings) == 0o600
    # Already open: not changed, so not even its ctime moves.
    assert {path: path.lstat().st_ctime_ns for path in untouched} == untouched

    before = _ctimes(tmp_path)
    _settle()
    _run(script, str(os.getuid()))
    assert _ctimes(tmp_path) == before, "a pass over an unchanged tree changes nothing"


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to own files as another user")
def test_the_member_pass_leaves_every_other_users_entry_alone(monkeypatch, tmp_path):
    """``-uid`` covers both branches: another user's directory and file are skipped."""
    workspace = tmp_path / "workspace"
    _make(tmp_path / "agent-state", 0o2775, directory=True)
    _make(workspace, 0o2775, directory=True)
    theirs_dir = _make(workspace / "theirs", 0o755, directory=True)
    theirs_file = _make(workspace / "theirs.txt", 0o644)
    for path in (theirs_dir, theirs_file):
        os.chown(path, 54321, 54321)
    mine = _make(workspace / "mine.txt", 0o644)
    script = _relocated(_member_script(monkeypatch), tmp_path)

    _run(script, str(os.getuid()))

    assert _mode(theirs_dir) == 0o755
    assert _mode(theirs_file) == 0o644
    assert _mode(mine) & 0o060 == 0o060


@pytest.mark.skipif(not _gnu_find(), reason="the script uses GNU find's -readable")
def test_the_new_chat_pass_opens_the_imported_tree_once(tmp_path):
    workspace = tmp_path / "workspace"
    state = tmp_path / "agent-state"
    _make(workspace, 0o2775, directory=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    source = _make(workspace / "src", 0o755, directory=True)
    code = _make(workspace / "src" / "main.py", 0o644)
    opened = _make(workspace / "docs", 0o2775, directory=True)
    kept = _make(workspace / "docs" / "README", 0o664)
    _make(state, 0o2775, directory=True)
    config = _make(state / "config.toml", 0o600)
    script = _relocated(SHARED_WORKSPACE_PERMISSIONS_SCRIPT, tmp_path)
    untouched = {path: path.lstat().st_ctime_ns for path in (opened, kept)}
    _settle()

    _run(script)

    assert _mode(source) & 0o2070 == 0o2070
    assert _mode(code) & 0o060 == 0o060
    assert _mode(config) & 0o060 == 0o060
    assert {path: path.lstat().st_ctime_ns for path in untouched} == untouched
    shared = subprocess.run(
        ["git", "-C", str(workspace), "config", "core.sharedRepository"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert shared.stdout.strip() == "group"
