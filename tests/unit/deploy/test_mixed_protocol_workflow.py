"""CI runs the mixed-protocol check when the host protocol changes, against the published release."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from tests.unit.deploy.test_artifacts_workflow import _classify_paths

ROOT = Path(__file__).resolve().parents[3]
_STEP = "Host protocol works one release apart in both directions"


def _steps() -> list[dict[str, object]]:
    document = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    return list(document["jobs"]["test-db"]["steps"])


@pytest.mark.parametrize(
    "path",
    [
        "apps/backend/cloud_agent/schemas/workers.py",
        "apps/backend/cloud_agent/schemas/hosts.py",
        "apps/backend/cloud_agent/api/worker.py",
        "apps/backend/cloud_agent/api/worker_events.py",
        "apps/backend/cloud_agent/api/hosts.py",
        "apps/backend/cloud_agent/sandbox_manager/sandbox_manager_client.py",
        "apps/backend/cloud_agent_shared/shared_protocol.py",
        "apps/backend/cloud_agent_host/control_plane_client.py",
        "apps/backend/cloud_agent_host/claimed_job.py",
        "apps/backend/cloud_agent_host/sandbox_manager_client.py",
        "apps/backend/cloud_agent_host/service/enrollment.py",
        "apps/backend/cloud_agent_host/service/host_control.py",
        "apps/backend/cloud_agent_host/service/host_service.py",
        "apps/backend/cloud_agent_host/manager_api/models.py",
        "scripts/check_mixed_protocol.py",
        "tests/support/mixed_protocol_host.py",
        "tests/support/mixed_protocol_control.py",
        "tests/support/mixed_protocol_wire.py",
        # A CI edit runs every check, this one included.
        ".github/workflows/ci.yml",
    ],
)
def test_protocol_changes_select_the_check(path: str) -> None:
    assert _classify_paths([path])["protocol"] == "true"


@pytest.mark.parametrize(
    "path",
    [
        "apps/backend/cloud_agent/api/jobs.py",
        "apps/backend/cloud_agent/schemas/jobs.py",
        "apps/backend/cloud_agent_host/runtimes/codex.py",
        "apps/frontend/src/app/page.tsx",
        "docs/developer/development.md",
    ],
)
def test_other_changes_leave_it_out(path: str) -> None:
    assert _classify_paths([path])["protocol"] == "false"


def test_it_runs_once_per_commit_after_the_migration_check_and_only_when_selected() -> None:
    names = [step.get("name") for step in _steps()]
    position = names.index(_STEP)
    assert names[position - 1] == "Previous release survives the candidate migration"
    assert _steps()[position]["if"] == (
        "matrix.shard == 1 && needs.changes.outputs.protocol == 'true'"
    )
    document = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert document["jobs"]["changes"]["outputs"]["protocol"] == (
        "${{ steps.classify.outputs.protocol }}"
    )


@pytest.mark.parametrize("base_variable", ["PR_BASE_SHA", "BEFORE_SHA"])
def test_it_selects_the_published_tag_from_the_event_base(
    tmp_path: Path, base_variable: str
) -> None:
    """The release the migration check uses: lightweight tags, newer ones ignored."""

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "--quiet")
    git("config", "user.name", "CI test")
    git("config", "user.email", "ci@example.test")
    git("config", "commit.gpgsign", "false")
    git("config", "tag.gpgsign", "false")
    git("commit", "--quiet", "--allow-empty", "-m", "published release")
    tag = f"agent-{git('rev-parse', 'HEAD')}"
    git("tag", tag)
    git("commit", "--quiet", "--allow-empty", "-m", "unpublished base")
    base = git("rev-parse", "HEAD")
    git("commit", "--quiet", "--allow-empty", "-m", "newer candidate")
    git("tag", f"agent-{git('rev-parse', 'HEAD')}")

    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$MIXED_TEST_ARGS"\n')
    uv.chmod(0o755)
    arguments = tmp_path / "check-args"
    script = next(step["run"] for step in _steps() if step.get("name") == _STEP)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PR_BASE_SHA": "",
        "BEFORE_SHA": "",
        "MIXED_TEST_ARGS": str(arguments),
    }
    env[base_variable] = base
    subprocess.run(["bash", "-c", str(script)], cwd=tmp_path, env=env, check=True)
    assert arguments.read_text().splitlines() == [
        "run",
        "python",
        "scripts/check_mixed_protocol.py",
        "--base",
        tag,
    ]
