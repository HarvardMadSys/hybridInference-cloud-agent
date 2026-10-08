"""Security and completeness tests for Docker-only source import archives."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import tempfile

import pytest

from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.source_archive import build_base_bundle, build_source_archive


def _archive(root, **kwargs) -> bytes:
    out = io.BytesIO()
    written = build_source_archive(str(root), out, uid=10001, gid=10001, **kwargs)
    assert written == out.tell()
    return out.getvalue()


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

    payload = _archive(root)

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

    payload = _archive(root)

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        link = archive.getmember("outside-link")
        assert link.issym()
    assert b"must-not-enter" not in payload


def test_source_archive_requires_a_real_git_checkout(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(SandboxError, match="no Git checkout"):
        _archive(empty)


def test_source_archive_refuses_oversized_checkout_before_import(tmp_path):
    root = _checkout(tmp_path)
    (root / "large.bin").write_bytes(b"x" * 1024)

    with pytest.raises(SandboxError, match="size limit"):
        _archive(root, max_bytes=512)


def test_oversized_checkout_refusal_names_its_size_and_the_limit(tmp_path):
    root = _checkout(tmp_path)
    (root / "large.bin").write_bytes(b"x" * (3 * 1024 * 1024))

    with pytest.raises(SandboxError) as refused:
        _archive(root, max_bytes=2 * 1024 * 1024)

    message = str(refused.value)
    assert "3.00 MiB including its Git objects" in message
    assert "2.00 MiB retained import size limit" in message


def test_source_archive_refuses_tar_overhead_past_the_limit(tmp_path):
    root = _checkout(tmp_path)
    file_bytes = sum(
        p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink()
    )

    with pytest.raises(SandboxError, match=r"source archive exceeds .* size limit"):
        _archive(root, max_bytes=file_bytes + 1)


def test_source_archive_streams_into_a_real_file(tmp_path):
    root = _checkout(tmp_path)

    with tempfile.TemporaryFile() as out:
        written = build_source_archive(str(root), out, uid=10001, gid=10001)
        out.seek(0)
        with tarfile.open(fileobj=out, mode="r:") as archive:
            names = set(archive.getnames())

    assert written > 0
    assert {".git/config", "src/main.py", "link.py"} <= names


def test_source_archive_refuses_special_files(tmp_path):
    root = _checkout(tmp_path)
    fifo = root / "pipe"
    os.mkfifo(fifo)

    try:
        with pytest.raises(SandboxError, match="refused file type"):
            _archive(root)
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
    with pytest.raises(SandboxError, match="prerequisite must be a full Git object id"):
        build_base_bundle(str(root), commit="a" * 40, prerequisite="deadbee")


def _git(*args: str, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    ).stdout.strip()


def _prerequisites(payload: bytes) -> list[str]:
    """The commits a bundle requires the receiving repository to hold."""
    header = payload.split(b"\n\n", 1)[0].decode()
    return [line[1:].split()[0] for line in header.splitlines() if line.startswith("-")]


def test_base_bundle_after_a_prerequisite_carries_only_what_the_commit_adds(tmp_path):
    # A large incompressible file the workspace already has, and a small change.
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "--quiet", ".", cwd=root)
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=root)
    (root / "large.bin").write_bytes(os.urandom(512 * 1024))
    (root / "app.py").write_text("print('one')\n")
    _git("add", "-A", cwd=root)
    _git("commit", "--quiet", "-m", "base", cwd=root)
    base = _git("rev-parse", "HEAD", cwd=root)
    (root / "app.py").write_text("print('two')\n")
    _git("commit", "--quiet", "-am", "change", cwd=root)
    commit = _git("rev-parse", "HEAD", cwd=root)

    whole = build_base_bundle(str(root), commit=commit)
    thin = build_base_bundle(str(root), commit=commit, prerequisite=base)

    assert len(whole) > 512 * 1024
    assert len(thin) < 4096
    assert _prerequisites(whole) == []
    assert _prerequisites(thin) == [base]

    # The workspace: a shallow checkout of the base on its own branch. The
    # bundle adds the commit's objects and nothing else.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git("init", "--quiet", ".", cwd=workspace)
    _git("fetch", "--quiet", "--depth", "1", f"file://{root}", base, cwd=workspace)
    _git("checkout", "--quiet", "-b", "agent/athr_x", "FETCH_HEAD", cwd=workspace)
    bundle = tmp_path / "base.bundle"
    bundle.write_bytes(thin)
    assert _git("bundle", "list-heads", str(bundle), "HEAD", cwd=workspace) == f"{commit} HEAD"
    _git("bundle", "verify", str(bundle), cwd=workspace)
    _git("bundle", "unbundle", str(bundle), cwd=workspace)
    _git("cat-file", "-e", f"{commit}^{{commit}}", cwd=workspace)
    assert _git("rev-parse", "HEAD", cwd=workspace) == base
    assert _git("branch", "--show-current", cwd=workspace) == "agent/athr_x"

    # A workspace without the prerequisite refuses it rather than import a
    # commit it cannot complete.
    empty = tmp_path / "empty"
    empty.mkdir()
    _git("init", "--quiet", ".", cwd=empty)
    with pytest.raises(subprocess.CalledProcessError):
        _git("bundle", "verify", str(bundle), cwd=empty)


def test_base_bundle_refusal_names_its_size(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "--quiet", ".", cwd=root)
    (root / "file.txt").write_text("one\n")
    _git("add", "file.txt", cwd=root)
    _git("commit", "--quiet", "-m", "one", cwd=root)

    with pytest.raises(SandboxError, match=r"base bundle \(\d+ B\) exceeds the 16 B import size"):
        build_base_bundle(str(root), commit=_git("rev-parse", "HEAD", cwd=root), max_bytes=16)
