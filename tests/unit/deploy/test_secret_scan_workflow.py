"""History scans cover the checkout's ancestry without inheriting other PRs."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[3] / ".github/workflows/ci.yml"


def test_history_scan_includes_removed_content_and_both_merge_parents(tmp_path: Path) -> None:
    """Exercise the workflow's git-log scope against a reused-runner history.

    Scanning just the tree or a first-parent history would miss the removed
    content. Scanning every local ref would include the obsolete other PR.
    """
    steps = yaml.safe_load(CI.read_text())["jobs"]["gitleaks"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    history = next(step for step in steps if step.get("name") == "Scan history")
    command = shlex.split(history["run"])
    assert command[:2] == ["gitleaks", "detect"]
    assert "--no-git" not in command
    log_options = shlex.split(
        next(arg.split("=", 1)[1] for arg in command if arg.startswith("--log-opts="))
    )

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout

    def commit_file(name: str, text: str) -> None:
        (tmp_path / name).write_text(text)
        git("add", name)
        git("commit", "-m", "fixture")

    git("init", "--initial-branch=current")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", str(tmp_path / "no-hooks"))
    commit_file("base.txt", "base\n")
    base = git("rev-parse", "HEAD").strip()
    commit_file("removed.txt", "current-parent-historical-content\n")
    git("rm", "removed.txt")
    git("commit", "-m", "remove content from current parent")

    git("switch", "-c", "merged", base)
    commit_file("merged.txt", "second-parent-historical-content\n")
    git("rm", "merged.txt")
    git("commit", "-m", "remove content from second parent")
    git("switch", "current")
    git("merge", "--no-ff", "merged", "-m", "PR merge")

    git("switch", "-c", "other-pr", base)
    commit_file("unrelated.txt", "obsolete-other-pr-content\n")
    git("update-ref", "refs/remotes/pull/288/merge", "HEAD")
    git("switch", "--detach", "current")
    git("branch", "-D", "other-pr")

    all_refs = git("log", "-p", "--all")
    assert "obsolete-other-pr-content" in all_refs
    scoped = git("log", "-p", "-U0", *log_options)
    assert "current-parent-historical-content" in scoped
    assert "second-parent-historical-content" in scoped
    assert "obsolete-other-pr-content" not in scoped
