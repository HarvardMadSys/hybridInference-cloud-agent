"""Real-git tests: the broker's Git runs none of the commands a workspace names.

The Git view, the shared-workspace snapshot and the base-bundle import run Git
as the group UID against a repository whose ``.git/config`` any member can
write. These tests plant each config-driven command Git would otherwise run
and prove the broker still reads the workspace correctly without running any
of them.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from cloud_agent_host import chat_sandbox
from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
from cloud_agent_host.sandbox import ContainerBackend, ProcessBackend, SandboxError
from cloud_agent_host.workspace_broker import (
    _GIT_VIEW_ENV,
    _NEUTRALIZE_GIT_FILTERS,
    _run_git,
    _run_sandbox,
)

if TYPE_CHECKING:
    from pathlib import Path

_IDENTITY = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0000",
    "GIT_COMMITTER_DATE": "1700000000 +0000",
}


def _git(*args: str, cwd: Path, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        input=stdin,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            **_IDENTITY,
        },
    ).stdout


def _planted(tmp_path: Path, name: str, body: str = "") -> tuple[Path, Path]:
    """A command that records it ran, so a test can assert it never did."""
    marker = tmp_path / f"ran-{name}"
    script = tmp_path / f"planted-{name}"
    script.write_text(f'#!/bin/sh\n: > "{marker}"\n{body}\n')
    script.chmod(0o755)
    return script, marker


def _repo(path: Path) -> Path:
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    (path / "a.txt").write_text("one\n")
    _git("add", "a.txt", cwd=path)
    _git("commit", "-q", "-m", "first", cwd=path)
    return path


@pytest.fixture
def backend(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    # The view runs with this backend's HOME; keep the developer's own
    # ~/.gitconfig (signing, helpers) out of what is being tested.
    monkeypatch.setenv("HOME", str(home))
    return ProcessBackend(acknowledged_unsafe=True)


def _sign_head(repo: Path) -> None:
    """Replace HEAD with the same commit carrying a (fake) signature header."""
    head = _git("cat-file", "commit", "HEAD", cwd=repo)
    header, _, message = head.partition("\n\n")
    signed = (
        f"{header}\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n AAAA\n"
        f" -----END PGP SIGNATURE-----\n\n{message}"
    )
    sha = _git("hash-object", "-t", "commit", "-w", "--stdin", cwd=repo, stdin=signed).strip()
    _git("update-ref", "HEAD", sha, cwd=repo)


def test_git_view_runs_no_command_the_workspace_config_names(backend, tmp_path):
    repo = _repo(tmp_path / "ws")
    fsmonitor, fsmonitor_ran = _planted(tmp_path, "fsmonitor", "exit 1")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook, hook_ran = _planted(tmp_path, "hook")
    hook.rename(hooks / "post-index-change")
    clean, clean_ran = _planted(tmp_path, "clean", "cat")
    gpg, gpg_ran = _planted(tmp_path, "gpg", "exit 1")
    _sign_head(repo)
    (repo / ".gitattributes").write_text("*.txt filter=planted\n")
    for key, value in (
        ("core.fsmonitor", str(fsmonitor)),
        ("core.hooksPath", str(hooks)),
        ("filter.planted.clean", str(clean)),
        ("filter.planted.process", str(clean)),
        ("filter.planted.required", "true"),
        ("log.showSignature", "true"),
        ("gpg.program", str(gpg)),
    ):
        _git("config", key, value, cwd=repo)
    (repo / "a.txt").write_text("two\n")
    (repo / "new.txt").write_text("new\n")

    view = _run_git(backend, repo)

    ran = [m.name for m in (fsmonitor_ran, hook_ran, clean_ran, gpg_ran) if m.exists()]
    assert ran == []
    assert view["branch"] == "main"
    assert {"code": " M", "path": "a.txt"} in view["changes"]
    assert "-one\n+two\n" in view["patch"]
    assert "+new\n" in view["patch"]
    assert [commit["subject"] for commit in view["commits"]] == ["first"]


def test_git_view_never_lazily_fetches_through_a_planted_promisor(backend, tmp_path):
    repo = _repo(tmp_path / "ws")
    upload_pack, upload_pack_ran = _planted(tmp_path, "upload-pack", "exit 1")
    ssh, ssh_ran = _planted(tmp_path, "ssh", "exit 1")
    for key, value in (
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "planted"),
        ("remote.planted.url", str(tmp_path / "elsewhere")),
        ("remote.planted.promisor", "true"),
        ("remote.planted.uploadpack", str(upload_pack)),
        ("protocol.file.allow", "always"),
        ("core.sshCommand", str(ssh)),
    ):
        _git("config", key, value, cwd=repo)

    with pytest.raises(HTTPException) as refused:
        _run_git(backend, repo, "1" * 40)

    assert refused.value.status_code == 502
    assert not upload_pack_ran.exists()
    assert not ssh_ran.exists()


def test_git_view_does_not_run_git_under_a_submodule_s_own_config(backend, tmp_path):
    repo = _repo(tmp_path / "ws")
    sub = _repo(repo / "sub")
    clean, clean_ran = _planted(tmp_path, "sub-clean", "cat")
    (sub / ".gitattributes").write_text("*.txt filter=inner\n")
    _git("config", "filter.inner.clean", str(clean), cwd=sub)
    _git("add", "sub", cwd=repo)
    _git("commit", "-q", "-m", "add sub", cwd=repo)
    # Same size, so Git has to read the file (through the filter) to compare.
    (sub / "a.txt").write_text("two\n")

    view = _run_git(backend, repo)

    assert not clean_ran.exists()
    assert [commit["subject"] for commit in view["commits"]] == ["add sub", "first"]


def test_only_git_lfs_own_filter_survives_the_neutralized_config(backend, tmp_path):
    repo = _repo(tmp_path / "ws")
    planted = {
        "filter.LFS.clean": "planted",
        "filter.with space.process": "planted",
        "filter.a.b.clean": "planted",
        "filter.x=y.smudge": "planted",
        "filter.planted.required": "true",
        "filter.lfs.required": "true",
        "lfs.extension.planted.clean": "planted",
    }
    kept = {
        "filter.lfs.smudge": "git-lfs smudge --skip -- %f",
        "filter.lfs.process": "git-lfs filter-process",
    }
    for key, value in {**planted, **kept, "filter.lfs.clean": "git-lfs clean -- %f"}.items():
        _git("config", key, value, cwd=repo)
    # The last value is the one Git uses, so it is the one that is judged.
    _git("config", "--add", "filter.lfs.clean", "planted", cwd=repo)
    keys = [*planted, *kept, "filter.lfs.clean"]
    report = "".join(f"printf '%s\\0' \"$(git config --get {shlex.quote(k)})\"; " for k in keys)

    status, stdout, _stderr = _run_sandbox(
        backend,
        workdir=repo,
        script=_NEUTRALIZE_GIT_FILTERS + report,
        env=dict(_GIT_VIEW_ENV),
        timeout_s=30,
    )

    assert status == 0
    assert dict(zip(keys, stdout.split("\0")[:-1], strict=True)) == {
        **dict.fromkeys(planted, ""),
        **kept,
        "filter.lfs.clean": "",
    }


def test_git_view_patch_still_carries_every_kind_of_change(backend, tmp_path):
    repo = _repo(tmp_path / "ws")
    (repo / "gone.txt").write_text("gone\n")
    (repo / ".gitignore").write_text("ignored*\n")
    sub = _repo(repo / "sub")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "second", cwd=repo)
    recorded = _git("rev-parse", "HEAD", cwd=sub).strip()
    (sub / "b.txt").write_text("b\n")
    _git("add", "b.txt", cwd=sub)
    _git("commit", "-q", "-m", "moved", cwd=sub)
    moved = _git("rev-parse", "HEAD", cwd=sub).strip()
    (sub / "a.txt").write_text("dirty\n")
    (repo / "a.txt").write_text("two\n")
    (repo / "gone.txt").unlink()
    (repo / "deep" / "er").mkdir(parents=True)
    (repo / "deep" / "er" / "new.txt").write_text("new\n")
    (repo / "ignored.txt").write_text("ignored\n")
    (repo / ":(glob)*").write_text("magic\n")
    (repo / "line\nbreak").write_text("odd\n")

    view = _run_git(backend, repo)

    headers = [line for line in view["patch"].splitlines() if line.startswith("diff --git")]
    assert headers == [
        "diff --git a/:(glob)* b/:(glob)*",
        "diff --git a/a.txt b/a.txt",
        "diff --git a/deep/er/new.txt b/deep/er/new.txt",
        "diff --git a/gone.txt b/gone.txt",
        'diff --git "a/line\\nbreak" "b/line\\nbreak"',
        "diff --git a/sub b/sub",
    ]
    assert f"-Subproject commit {recorded}\n+Subproject commit {moved}\n" in view["patch"]
    assert {"code": " M", "path": "sub"} in view["changes"]


def test_base_bundle_import_never_lazily_fetches_a_missing_prerequisite(tmp_path, monkeypatch):
    source = _repo(tmp_path / "source")
    base = _git("rev-parse", "HEAD", cwd=source).strip()
    (source / "b.txt").write_text("b\n")
    _git("add", "b.txt", cwd=source)
    _git("commit", "-q", "-m", "second", cwd=source)
    tip = _git("rev-parse", "HEAD", cwd=source).strip()
    bundle = tmp_path / "base.bundle"
    _git("bundle", "create", "-q", str(bundle), f"{base}..HEAD", cwd=source)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _git("init", "-q", "-b", "main", cwd=workspace)
    upload_pack, upload_pack_ran = _planted(tmp_path, "upload-pack", "exit 1")
    for key, value in (
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "planted"),
        ("remote.planted.url", str(source)),
        ("remote.planted.promisor", "true"),
        ("remote.planted.uploadpack", str(upload_pack)),
    ):
        _git("config", key, value, cwd=workspace)
    tmpfs = tmp_path / "tmpfs"
    tmpfs.mkdir()
    monkeypatch.setattr(chat_sandbox, "ACTIVATION_TMPFS", str(tmpfs))
    manager = ChatSandboxManager(ContainerBackend(image="img"))

    def exec_here(_spec, argv, *, env, workdir, timeout_s, input_bytes):
        # The container's exec without Docker: the same argv, env and stdin.
        return subprocess.run(
            argv,
            cwd=workspace,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **env},
            input=input_bytes,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )

    monkeypatch.setattr(manager, "exec_capture", exec_here)
    spec = ChatSandboxSpec(
        thread_id="athr_01", sandbox_id="asbx_01", generation=1, runtime_id="codex"
    )

    # The prerequisite is missing, so the import fails, and fetches nothing.
    with pytest.raises(SandboxError):
        manager.import_base_bundle(spec, bundle.read_bytes(), commit=tip, env={})

    assert not upload_pack_ran.exists()
