"""Contract tests for fixed in-container retained workspace programs."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

from cloud_agent_host.retained_workspace import _READ_PROGRAM, _WRITE_PROGRAM


def _read(root, relative: str, *, max_bytes: int = 1024, entries: int = 100):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _READ_PROGRAM,
            str(root),
            relative,
            str(max_bytes),
            str(entries),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result, json.loads(result.stdout) if result.stdout else None


def test_retained_reader_matches_file_and_directory_response_contract(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("hello\n")
    os.symlink("README.md", tmp_path / "linked")

    root_result, root = _read(tmp_path, "")
    file_result, file = _read(tmp_path, "README.md")

    assert root_result.returncode == file_result.returncode == 0
    assert [entry["name"] for entry in root["entries"]] == ["src", "linked", "README.md"]
    assert all(entry["name"] != ".git" for entry in root["entries"])
    assert (
        next(entry for entry in root["entries"] if entry["name"] == "linked")["omitted_reason"]
        == "symlink"
    )
    assert file["content"] == "hello\n"
    assert file["source"] == "workspace"
    assert file["writable"] is True


def test_retained_reader_never_follows_a_symlink_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not visible")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.symlink(outside, workspace / "escape")

    result, payload = _read(workspace, "escape/secret.txt")

    assert result.returncode == 40
    assert payload["error"] == "workspace path crosses an invalid directory"


def test_retained_writer_uses_stdin_and_refuses_symlink_targets(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("before")
    command = [
        sys.executable,
        "-c",
        _WRITE_PROGRAM,
        str(tmp_path),
        "note.txt",
        "100",
    ]

    written = subprocess.run(command, input=b"after\n", capture_output=True, check=False)
    assert written.returncode == 0
    assert target.read_text() == "after\n"

    os.symlink(target, tmp_path / "linked.txt")
    refused = subprocess.run(
        [*command[:-2], "linked.txt", "100"],
        input=b"overwrite",
        capture_output=True,
        check=False,
    )
    assert refused.returncode == 49
    assert target.read_text() == "after\n"


def test_retained_writer_keeps_files_group_writable_for_fork_members(tmp_path):
    command = [
        sys.executable,
        "-c",
        _WRITE_PROGRAM,
        str(tmp_path),
        "shared.txt",
        "100",
    ]

    written = subprocess.run(command, input=b"shared\n", capture_output=True, check=False)

    assert written.returncode == 0
    assert stat.S_IMODE((tmp_path / "shared.txt").stat().st_mode) == 0o664
