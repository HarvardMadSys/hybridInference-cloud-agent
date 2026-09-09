"""Security and completeness tests for Docker-only source import archives."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile

import pytest

from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.source_archive import build_base_bundle, build_source_archive


def _checkout(tmp_path):
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text('[remote "origin"]\nurl=x\n')
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('ok')\n")
    os.symlink("src/main.py", root / "link.py")
    return root


def test_source_archive_contains_git_files_and_normalizes_ownership(tmp_path):
    root = _checkout(tmp_path)

    payload = build_source_archive(str(root), uid=10001, gid=10001)

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = {member.name: member for member in archive.getmembers()}
    assert {".git", ".git/config", "src", "src/main.py", "link.py"} <= members.keys()
    assert members["src/main.py"].uid == 10001
    assert members["src/main.py"].gid == 10001
    assert members["src/main.py"].uname == ""
    assert members["link.py"].issym()
    assert members["link.py"].linkname == "src/main.py"


def test_source_archive_never_follows_a_symlink_outside_the_checkout(tmp_path):
    root = _checkout(tmp_path)
    outside = tmp_path / "outside-secret"
    outside.write_text("must-not-enter")
    os.symlink(outside, root / "outside-link")

    payload = build_source_archive(str(root), uid=10001, gid=10001)

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        link = archive.getmember("outside-link")
        assert link.issym()
    assert b"must-not-enter" not in payload


def test_source_archive_requires_a_real_git_checkout(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(SandboxError, match="no Git checkout"):
        build_source_archive(str(empty), uid=10001, gid=10001)


def test_source_archive_refuses_oversized_checkout_before_import(tmp_path):
    root = _checkout(tmp_path)
    (root / "large.bin").write_bytes(b"x" * 1024)

    with pytest.raises(SandboxError, match="size limit"):
        build_source_archive(str(root), uid=10001, gid=10001, max_bytes=512)


def test_source_archive_refuses_special_files(tmp_path):
    root = _checkout(tmp_path)
    fifo = root / "pipe"
    os.mkfifo(fifo)

    try:
        with pytest.raises(SandboxError, match="refused file type"):
            build_source_archive(str(root), uid=10001, gid=10001)
    finally:
        fifo.unlink(missing_ok=True)


def test_base_bundle_contains_exact_commit_and_is_bounded(tmp_path):
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    (root / "file.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "--quiet", "-m", "one"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    payload = build_base_bundle(str(root), commit=commit)
    bundle = tmp_path / "base.bundle"
    bundle.write_bytes(payload)
    listed = subprocess.run(
        ["git", "-C", str(root), "bundle", "list-heads", str(bundle)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert listed == f"{commit} HEAD"
    with pytest.raises(SandboxError, match="size limit"):
        build_base_bundle(str(root), commit=commit, max_bytes=16)


def test_base_bundle_requires_a_full_object_id(tmp_path):
    root = _checkout(tmp_path)

    with pytest.raises(SandboxError, match="full Git object id"):
        build_base_bundle(str(root), commit="deadbee")
