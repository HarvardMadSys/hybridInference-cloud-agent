"""Sharded CI jobs still run every test exactly once per commit.

A pull request splits the slow tiers over two hosted VMs; trusted events keep
them whole on the persistent pool. The split itself is `scripts/ci_shard.py`'s
and is tested there. What is pinned here is the wiring around it: each shard
asks for the tier that its pytest then runs, the steps that must run once per
commit run in one shard, and a shard cannot report checks it skipped as green.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from tests.unit.test_ci_workflow_policy import _expression

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
SHARDED = ("test", "test-db", "check")
SHARDS = "${{ fromJSON(github.event_name == 'pull_request' && '[1, 2]' || '[1]') }}"
SHARD = "${{ matrix.shard }}/${{ github.event_name == 'pull_request' && 2 || 1 }}"


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _step(job: str, name: str) -> dict:
    return next(step for step in _jobs()[job]["steps"] if step.get("name") == name)


@pytest.mark.parametrize("job", SHARDED)
@pytest.mark.parametrize(
    ("event", "expected"),
    [("pull_request", [1, 2]), ("push", [1]), ("workflow_dispatch", [1])],
)
def test_pull_requests_split_the_slow_jobs_and_trusted_events_keep_them_whole(
    job: str, event: str, expected: list[int]
) -> None:
    definition = _jobs()[job]
    assert definition["strategy"]["fail-fast"] is False
    assert definition["strategy"]["matrix"]["shard"] == SHARDS
    assert _expression(SHARDS, event, {}) == expected
    assert definition["env"]["SHARD"] == SHARD
    assert "${{ matrix.shard }}" in definition["name"], "each shard needs its own check name"


@pytest.mark.parametrize(("job", "step"), [("test", "Pytest"), ("test-db", "Pytest (dbtest tier)")])
def test_each_shard_asks_for_the_tier_its_pytest_runs(job: str, step: str) -> None:
    """A shard computed for one marker and run with another drops tests silently."""
    script = _step(job, step)["run"]
    assert "set -euo pipefail" in script
    asked = re.search(r'ci_shard\.py --shard "\$\{SHARD\}" -- -m ("[^"]+"|\S+)', script)
    run = re.search(r"uv run pytest .*?-m (\"[^\"]+\"|\S+)", script)
    assert asked and run, script
    assert asked.group(1) == run.group(1)
    # A substitution, so a failing script fails the step instead of handing
    # pytest an empty list, which would run every test path.
    assert 'files="$(uv run python scripts/ci_shard.py' in script
    assert script.rstrip().endswith('"${paths[@]}"')


@pytest.mark.parametrize(
    ("job", "step"),
    [
        ("test-db", "Previous release survives the candidate migration"),
        ("test", "Shared member permissions survive a sibling CLI restart"),
        ("test", "Set up Node for native CLI regression"),
        ("test", "Install the sandbox's pinned Claude CLI"),
        ("test", "Install the sandbox's pinned Claude SDK"),
        ("test", "Native history survives close, restore and failed-turn retry"),
        ("check", "ESLint"),
        ("check", "Prettier"),
        ("check", "TypeScript"),
    ],
)
def test_once_per_commit_steps_run_in_the_first_shard(job: str, step: str) -> None:
    assert _step(job, step)["if"] == "matrix.shard == 1"


def test_vitest_runs_this_shard_of_the_frontend_tests() -> None:
    assert _step("check", "Tests")["run"] == 'npm run test -- --shard="${SHARD}"'
    assert "if" not in _step("check", "Tests")


@pytest.mark.parametrize(
    ("shard", "quick_checks", "tests", "passes"),
    [
        ("1/2", "success", "success", True),
        ("1/2", "skipped", "success", False),
        ("2/2", "skipped", "success", True),
        ("2/2", "skipped", "failure", False),
        ("1/1", "skipped", "success", False),
    ],
)
def test_a_shard_cannot_report_checks_it_skipped(
    shard: str, quick_checks: str, tests: str, passes: bool
) -> None:
    """Execute the gate step; only a shard that never runs a check may skip it."""
    step = _step("check", "Every check passed")
    results = (
        f"eslint={quick_checks} prettier={quick_checks} typescript={quick_checks} tests={tests}"
    )
    completed = subprocess.run(
        ["bash", "-c", step["run"]],
        env={**os.environ, "RESULTS": results, "SHARD": shard},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (completed.returncode == 0) is passes, completed.stdout


@pytest.mark.parametrize(("job", "step"), [("test", "Pytest"), ("test-db", "Pytest (dbtest tier)")])
@pytest.mark.parametrize("shard", ["1/1", "1/2", "2/2"])
def test_one_shard_skips_duplicate_collection_but_split_runs_keep_it(
    tmp_path: Path, job: str, step: str, shard: str
) -> None:
    """Run the actual shell; the tier and paths must survive both branches."""
    log = tmp_path / "commands"
    script = _step(job, step)["run"]
    uv_double = r"""
    uv() {
      printf "%s\n" "$*" >> "$COMMAND_LOG"
      if [ "$2" = python ]; then
        printf '%s\n' tests/integration/example.py tests/unit/example.py
      fi
    }
    """
    subprocess.run(
        ["bash", "-c", uv_double + script],
        env={**os.environ, "SHARD": shard, "COMMAND_LOG": str(log), "RUNNER_TEMP": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
    )
    commands = log.read_text().splitlines()
    assert len(commands) == (1 if shard == "1/1" else 2)
    assert commands[-1].startswith("run pytest ")
    if shard == "1/1":
        assert commands[-1].endswith(" tests")
    else:
        assert f"ci_shard.py --shard {shard}" in commands[0]
        assert commands[-1].endswith(" tests/integration/example.py tests/unit/example.py")


@pytest.mark.parametrize(("job", "step"), [("test", "Pytest"), ("test-db", "Pytest (dbtest tier)")])
def test_failed_shard_collection_never_falls_back_to_the_whole_suite(
    tmp_path: Path, job: str, step: str
) -> None:
    log = tmp_path / "commands"
    script = _step(job, step)["run"]
    completed = subprocess.run(
        ["bash", "-c", 'uv() { echo "$*" >> "$COMMAND_LOG"; return 1; }\n' + script],
        env={**os.environ, "SHARD": "2/2", "COMMAND_LOG": str(log)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    commands = log.read_text().splitlines()
    assert len(commands) == 1 and "ci_shard.py" in commands[0]
