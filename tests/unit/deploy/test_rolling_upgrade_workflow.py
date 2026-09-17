"""Exercise CI's release selection against real lightweight Git tags."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("base_variable", ["PR_BASE_SHA", "BEFORE_SHA"])
def test_migration_check_selects_published_tag_from_event_base(
    tmp_path: Path, base_variable: str
) -> None:
    """Both event paths must find lightweight releases and ignore newer tags."""

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "--quiet")
    git("config", "user.name", "CI test")
    git("config", "user.email", "ci@example.test")
    git("config", "commit.gpgsign", "false")
    git("config", "tag.gpgsign", "false")
    git("commit", "--quiet", "--allow-empty", "-m", "published release")
    released = git("rev-parse", "HEAD")
    tag = f"agent-{released}"
    git("tag", tag)
    git("commit", "--quiet", "--allow-empty", "-m", "unpublished base")
    base = git("rev-parse", "HEAD")
    git("commit", "--quiet", "--allow-empty", "-m", "newer candidate")
    git("tag", f"agent-{git('rev-parse', 'HEAD')}")

    # Capture the chosen release at the migration runner's boundary. The
    # workflow's complete shell block and Git lookup execute unchanged.
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$ROLLING_TEST_ARGS"\n')
    uv.chmod(0o755)
    arguments = tmp_path / "migration-args"
    steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["test-db"][
        "steps"
    ]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Previous release survives the candidate migration"
    )
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PR_BASE_SHA": "",
        "BEFORE_SHA": "",
        "ROLLING_TEST_ARGS": str(arguments),
    }
    env[base_variable] = base
    subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, check=True)
    assert arguments.read_text().splitlines() == [
        "run",
        "python",
        "scripts/check_rolling_upgrade.py",
        "--base",
        tag,
    ]
