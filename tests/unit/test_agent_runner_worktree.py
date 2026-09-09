"""Real-git tests for the trusted initial checkout streamed into a chat."""

from __future__ import annotations

import base64
import subprocess
from typing import TYPE_CHECKING

import pytest

from cloud_agent_host.runner import (
    WorktreeError,
    _assert_no_credential_on_disk,
    _auth_env,
    checkout_output_branch,
    prepare_worktree,
    repo_clone_base_url,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    ).stdout


def test_clone_origin_defaults_to_github_and_accepts_a_local_git_service():
    assert repo_clone_base_url({}) == "https://github.com"
    assert (
        repo_clone_base_url({"AGENT_REPO_CLONE_BASE_URL": "git://local-repo/"})
        == "git://local-repo"
    )


def test_clone_origin_never_sends_a_credential_over_plaintext():
    with pytest.raises(WorktreeError, match="refusing to send a repository credential"):
        repo_clone_base_url({"AGENT_REPO_CLONE_BASE_URL": "git://local-repo"}, clone_token="x")
    assert (
        repo_clone_base_url(
            {"AGENT_REPO_CLONE_BASE_URL": "https://git.example.test"}, clone_token="x"
        )
        == "https://git.example.test"
    )


@pytest.fixture()
def remote(tmp_path: Path) -> tuple[str, str, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "--quiet", "--initial-branch", "main", ".", cwd=source)
    (source / "hello.py").write_text("def hello():\n    return 1\n")
    _git("add", "-A", cwd=source)
    _git("commit", "--quiet", "-m", "first", cwd=source)
    first = _git("rev-parse", "HEAD", cwd=source).strip()
    (source / "second.txt").write_text("later\n")
    _git("add", "-A", cwd=source)
    _git("commit", "--quiet", "-m", "second", cwd=source)
    head = _git("rev-parse", "HEAD", cwd=source).strip()

    bare = tmp_path / "remotes" / "owner" / "name.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(source), str(bare)],
        check=True,
        capture_output=True,
    )
    return f"file://{tmp_path / 'remotes'}", "owner/name", first, head


def test_initial_chat_checkout_is_populated_and_has_no_push_remote(tmp_path: Path, remote):
    base, repo, _first, head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()

    resolved = prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)

    assert resolved == head
    assert (workdir / "hello.py").read_text() == "def hello():\n    return 1\n"
    assert _git("remote", cwd=workdir).strip() == ""


def test_a_pinned_commit_is_the_one_streamed_into_the_container(tmp_path: Path, remote):
    base, repo, first, head = remote
    assert first != head
    workdir = tmp_path / "job"
    workdir.mkdir()

    resolved = prepare_worktree(workdir=str(workdir), repo=repo, base_sha=first, remote_base=base)

    assert resolved == first
    assert (workdir / "hello.py").exists()
    assert not (workdir / "second.txt").exists()


def test_checkout_uses_the_publishers_output_branch(tmp_path: Path, remote):
    base, repo, _first, _head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()
    prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)

    assert checkout_output_branch(str(workdir), "athr_example") == "agent/athr_example"
    assert _git("branch", "--show-current", cwd=workdir).strip() == "agent/athr_example"


def test_an_unreachable_repository_fails_with_a_reason(tmp_path: Path):
    with pytest.raises(WorktreeError, match="could not fetch"):
        prepare_worktree(
            workdir=str(tmp_path),
            repo="owner/missing",
            base_sha=None,
            remote_base="file:///definitely-not-present",
        )


@pytest.mark.parametrize(
    "repo",
    ("", "owner", "owner/name/extra", "../name", "owner/../name", "owner/name.git/other"),
)
def test_a_malformed_repo_never_reaches_git(tmp_path: Path, repo: str):
    with pytest.raises(WorktreeError, match="owner/name"):
        prepare_worktree(workdir=str(tmp_path), repo=repo, base_sha=None)


def test_a_non_commit_base_sha_is_refused(tmp_path: Path):
    with pytest.raises(WorktreeError, match="commit hash"):
        prepare_worktree(workdir=str(tmp_path), repo="owner/name", base_sha="--upload-pack=/bin/sh")


def test_the_clone_credential_stays_out_of_git_argv_and_config():
    env = _auth_env("ghs_secret")
    assert "ghs_secret" not in " ".join(env)
    encoded = env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(encoded).decode() == "x-access-token:ghs_secret"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert _auth_env(None) == {}


def test_a_credential_left_on_disk_stops_the_job(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("token=ghs_leaked")
    with pytest.raises(WorktreeError, match="credential was written"):
        _assert_no_credential_on_disk(str(tmp_path), "ghs_leaked")

    (tmp_path / ".git" / "config").write_text("safe")
    _assert_no_credential_on_disk(str(tmp_path), "ghs_leaked")


def test_a_missing_pinned_commit_stops_initialization(tmp_path: Path, remote):
    base, repo, _first, _head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()
    with pytest.raises(WorktreeError, match="could not fetch"):
        prepare_worktree(workdir=str(workdir), repo=repo, base_sha="0" * 40, remote_base=base)
