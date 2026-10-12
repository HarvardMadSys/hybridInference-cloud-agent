"""Contract tests for fixed in-container retained workspace programs."""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize(
    ("raw", "media_type"),
    [
        (b"\x89PNG\r\n\x1a\n\x00image", "image/png"),
        (b"\xff\xd8\xff\xe0image", "image/jpeg"),
        (b"GIF89a\x00image", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPimage", "image/webp"),
        (b"\x00\xffbinary", None),
    ],
)
def test_retained_reader_exposes_exact_bounded_binary_bytes(tmp_path, raw, media_type):
    # Extension is deliberately unrelated: preview types come from bytes.
    (tmp_path / "artifact.dat").write_bytes(raw)

    result, payload = _read(tmp_path, "artifact.dat", max_bytes=len(raw))

    assert result.returncode == 0
    assert payload["content"] is None
    assert payload["binary"] is True
    assert payload["media_type"] == media_type
    assert base64.b64decode(payload["content_base64"], validate=True) == raw
    assert payload["size"] == len(raw)
    assert payload["truncated"] is False
    assert payload["writable"] is False


@pytest.mark.parametrize("content", ['<svg onload="alert(1)"/>', "<script>alert(1)</script>"])
def test_retained_reader_does_not_trust_image_extensions_for_active_content(tmp_path, content):
    (tmp_path / "fake.png").write_text(content)

    result, payload = _read(tmp_path, "fake.png")

    assert result.returncode == 0
    assert payload["content"] == content
    assert payload["media_type"] is None
    assert payload["content_base64"] is None


def test_retained_reader_does_not_return_partial_downloads(tmp_path):
    (tmp_path / "large.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

    result, payload = _read(tmp_path, "large.png", max_bytes=10)

    assert result.returncode == 0
    assert payload["truncated"] is True
    assert payload["omitted_reason"] == "too_large"
    assert payload["content"] is None
    assert payload.get("content_base64") is None


def test_retained_reader_never_returns_download_bytes_for_symlinks(tmp_path):
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00image")
    os.symlink("image.png", tmp_path / "linked.png")

    result, payload = _read(tmp_path, "linked.png")

    assert result.returncode == 0
    assert payload["kind"] == "symlink"
    assert payload.get("content_base64") is None


@pytest.mark.parametrize("path", ["../image.png", "./image.png", "nested/../image.png"])
def test_retained_reader_refuses_download_path_traversal(tmp_path, path):
    result, payload = _read(tmp_path, path)

    assert result.returncode == 40
    assert payload["error"] == "workspace path is invalid"


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
