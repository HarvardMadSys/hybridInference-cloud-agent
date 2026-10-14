"""The pull-request gate hook checks the checkout that holds the commit under review."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / ".claude" / "hooks"

_spec = importlib.util.spec_from_file_location("pr_target", HOOKS / "pr_target.py")
assert _spec is not None and _spec.loader is not None
pr_target = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_target)


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A repository on `main` with a second worktree on `feature`."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / "file").write_text("x\n")
    _git(main, "add", "file")
    _git(main, "commit", "-q", "-m", "init")
    _git(main, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "feature"))
    return main


@pytest.fixture
def project(repository: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make ``repository`` the project the hook gates, as Claude Code does."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repository))
    _git(repository, "remote", "add", "origin", "git@github.com:Owner/project.git")
    return repository


@pytest.fixture
def other(tmp_path: Path) -> Path:
    """A checkout of an unrelated repository."""
    path = tmp_path / "other"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "for f in a b; do echo $f; done",
        "cat <<'EOF'\nhello\nEOF",
        'git commit -m "explain why gh pr create was blocked"',
    ],
)
def test_commands_that_create_no_pull_request_are_allowed(command: str) -> None:
    """Claude Code starts the hook for commands it cannot classify; they must pass."""
    assert pr_target.parse_command(command, "/session") is None


def test_the_session_directory_is_used_without_cd() -> None:
    assert pr_target.parse_command("gh pr create --fill", "/session") == (
        "/session",
        None,
        None,
    )


def test_a_cd_before_gh_pr_create_selects_that_checkout() -> None:
    command = 'cd /work/tree && gh pr create --base dev --title "x" --body "$(cat file)"'
    assert pr_target.parse_command(command, "/session") == ("/work/tree", None, None)


def test_a_relative_cd_resolves_against_the_session_directory() -> None:
    assert pr_target.parse_command("cd ../other; gh pr create", "/a/session") == (
        "/a/other",
        None,
        None,
    )


@pytest.mark.parametrize(
    ("args", "head"),
    [("--head topic", "topic"), ("-H topic", "topic"), ("--head=owner:topic", "owner:topic")],
)
def test_the_head_branch_is_read(args: str, head: str) -> None:
    assert pr_target.parse_command(f"gh pr create {args} --base dev", "/s") == ("/s", head, None)


@pytest.mark.parametrize("args", ["--repo Owner/other", "-R Owner/other", "--repo=Owner/other"])
def test_the_repository_is_read(args: str) -> None:
    assert pr_target.parse_command(f"gh pr create {args}", "/s") == ("/s", None, "Owner/other")


def test_a_pull_request_from_another_repositorys_checkout_is_not_gated(
    project: Path, other: Path
) -> None:
    """That repository has its own checks; this one's `make check` is not among them."""
    event = {
        "tool_name": "Bash",
        "cwd": str(project),
        "tool_input": {"command": f"cd {other} && gh pr create --base main"},
    }
    assert pr_target.target(event) is None


@pytest.mark.parametrize(
    "repo", ["Owner/other", "github.com/Owner/other", "https://github.com/owner/other.git"]
)
def test_a_pull_request_for_another_named_repository_is_not_gated(project: Path, repo: str) -> None:
    event = {
        "tool_name": "Bash",
        "cwd": str(project),
        "tool_input": {"command": f"gh pr create --repo {repo} --head topic"},
    }
    assert pr_target.target(event) is None


def test_a_pull_request_naming_this_repository_is_still_gated(project: Path) -> None:
    event = {
        "tool_name": "Bash",
        "cwd": str(project),
        "tool_input": {"command": "gh pr create --repo owner/PROJECT --head feature"},
    }
    assert Path(pr_target.target(event)).resolve() == (project.parent / "feature").resolve()


def test_an_mcp_pull_request_for_another_repository_is_not_gated(project: Path) -> None:
    event = {
        "tool_name": "mcp__github__create_pull_request",
        "cwd": str(project),
        "tool_input": {"owner": "Owner", "repo": "other", "head": "topic", "base": "main"},
    }
    assert pr_target.target(event) is None


def test_a_head_branch_resolves_to_the_worktree_that_has_it(repository: Path) -> None:
    feature = repository.parent / "feature"
    assert Path(pr_target.resolve(str(repository), "feature")).resolve() == feature.resolve()
    assert Path(pr_target.resolve(str(repository), "owner:feature")).resolve() == feature.resolve()
    assert pr_target.resolve(str(repository), "main") == str(repository)


def test_a_head_branch_checked_out_nowhere_is_refused(repository: Path) -> None:
    _git(repository, "branch", "elsewhere")
    with pytest.raises(pr_target.TargetError, match="not checked out"):
        pr_target.resolve(str(repository), "elsewhere")


def test_an_mcp_pull_request_names_its_head(project: Path) -> None:
    event = {
        "tool_name": "mcp__github__create_pull_request",
        "cwd": str(project),
        "tool_input": {"owner": "Owner", "repo": "project", "head": "feature", "base": "main"},
    }
    assert Path(pr_target.target(event)).resolve() == (project.parent / "feature").resolve()


def _run_hook(event: dict, project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOKS / "pre-pr-check.sh")],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        cwd=event["cwd"],
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
        timeout=60,
    )


def test_the_hook_allows_a_command_that_is_not_a_pull_request(repository: Path) -> None:
    (repository / "untracked").write_text("dirty\n")
    result = _run_hook(
        {"tool_name": "Bash", "cwd": str(repository), "tool_input": {"command": "ls"}},
        repository,
    )
    assert result.returncode == 0, result.stderr
    assert "gate" not in result.stderr


def test_the_hook_checks_the_worktree_the_pull_request_comes_from(repository: Path) -> None:
    """A dirty session checkout must not block, or stand in for, another worktree."""
    feature = repository.parent / "feature"
    (repository / "untracked-in-session").write_text("dirty\n")
    (feature / "untracked-in-feature").write_text("dirty\n")
    command = f"cd {feature} && gh pr create --base main"
    result = _run_hook(
        {"tool_name": "Bash", "cwd": str(repository), "tool_input": {"command": command}},
        repository,
    )
    assert result.returncode == 2
    assert "untracked-in-feature" in result.stderr
    assert "untracked-in-session" not in result.stderr


def test_the_hook_allows_a_pull_request_from_another_repository(
    repository: Path, other: Path
) -> None:
    """The gate is this repository's; another repository's dirty checkout is its own concern."""
    (other / "untracked").write_text("dirty\n")
    result = _run_hook(
        {
            "tool_name": "Bash",
            "cwd": str(repository),
            "tool_input": {"command": f"cd {other} && gh pr create --base main"},
        },
        repository,
    )
    assert result.returncode == 0, result.stderr
    assert "gate" not in result.stderr
