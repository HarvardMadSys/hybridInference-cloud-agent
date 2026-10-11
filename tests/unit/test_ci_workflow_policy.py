"""Unreviewed PR code never shares a runner or credential with publication.

Exercise the workflow's runner expressions for every cell rather than accepting
an expression merely because it contains a hosted runner somewhere. Runner-group
restrictions are also required in GitHub: a fork can propose workflow edits.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"


def _document() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _expression(value: str, event: str, cell: dict) -> Any:
    """Evaluate the small Actions expression subset used for runner routing.

    An explicit AST allowlist avoids executing workflow text as Python and fails
    on new syntax until the routing test understands it.
    """
    source = value.removeprefix("${{").removesuffix("}}").strip()
    for name, replacement in {
        "github.event_name": event,
        "matrix.artifact.arch": cell.get("arch"),
        "matrix.artifact.runner": cell.get("runner"),
    }.items():
        source = source.replace(name, repr(replacement))
    source = source.replace("&&", " and ").replace("||", " or ")

    def evaluate(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp):
            result = evaluate(node.values[0])
            for operand in node.values[1:]:
                if (isinstance(node.op, ast.And) and not result) or (
                    isinstance(node.op, ast.Or) and result
                ):
                    return result
                result = evaluate(operand)
            return result
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = evaluate(node.left), evaluate(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "fromJSON"
            and len(node.args) == 1
            and not node.keywords
        ):
            return json.loads(evaluate(node.args[0]))
        raise AssertionError(f"unsupported runner routing syntax: {ast.dump(node)}")

    return evaluate(ast.parse(source, mode="eval").body)


def _runner_labels(job: dict, event: str, cell: dict) -> list[str]:
    runs_on = job["runs-on"]
    return runs_on if isinstance(runs_on, list) else _expression(runs_on, event, cell)


def _cells(job: dict) -> list[dict]:
    return job.get("strategy", {}).get("matrix", {}).get("artifact", [{}])


def test_pull_requests_run_every_check_on_disposable_native_hosted_runners() -> None:
    """Forks, collaborators and same-repository PRs all take the hosted path."""
    for name, job in _document()["jobs"].items():
        if name == "kata":
            continue  # Its explicit PR exclusion is checked below.
        for cell in _cells(job):
            expected = "ubuntu-24.04-arm" if cell.get("arch") == "arm64" else "ubuntu-24.04"
            assert _runner_labels(job, "pull_request", cell) == [expected], (name, cell)


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_trusted_events_keep_the_existing_self_hosted_pools(event: str) -> None:
    special = {
        "compose": ["self-hosted", "Linux", "ARM64", "image-verify-arm64"],
        "kata": ["self-hosted", "Linux", "X64", "image-verify-x64"],
    }
    for name, job in _document()["jobs"].items():
        for cell in _cells(job):
            expected = (
                json.loads(cell["runner"])
                if name == "artifacts"
                else special.get(name, ["self-hosted", "Linux", "ci-general"])
            )
            assert _runner_labels(job, event, cell) == expected, (name, cell)


def test_pr_workflow_has_only_read_permissions_and_no_privileged_event_trigger() -> None:
    document = _document()
    triggers = document.get("on") or document[True]
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main", "dev"]}
    assert document["permissions"] == {"contents": "read"}
    for name, job in document["jobs"].items():
        assert job.get("permissions", document["permissions"]) == {"contents": "read"}, name
        assert "secrets." not in json.dumps(job.get("env", {})), name
        assert "secrets." not in json.dumps(job.get("services", {})), name
        for step in job["steps"]:
            if "secrets." in json.dumps(step):
                assert step.get("if") == "github.event_name != 'pull_request'", name


def test_checkouts_leave_no_token_in_the_unreviewed_repository() -> None:
    for name, job in _document()["jobs"].items():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False, name


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_builder_configuration_is_disposable_for_prs_and_persistent_for_pushes(
    tmp_path: Path, event: str
) -> None:
    """Execute both branches without contacting a daemon or a registry."""
    steps = _document()["jobs"]["artifacts"]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Reuse this runner's builder, or make one"
    ).replace("${{ matrix.artifact.name }}", "sandbox")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o755)
    runner_temp = tmp_path / "job-temp"
    runner_workspace = tmp_path / "persistent-runner" / "work"
    github_env = tmp_path / "github-env"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_EVENT_NAME": event,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_ENV": str(github_env),
        "RUNNER_TEMP": str(runner_temp),
        "RUNNER_WORKSPACE": str(runner_workspace),
        "RUNNER_NAME": "trusted-runner",
    }
    subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, check=True)
    outputs = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    if event == "pull_request":
        assert outputs["BUILDX_CONFIG"] == str(runner_temp / "buildx")
        assert outputs["BUILDER"] == "ci-pr-123-2-sandbox"
        assert not runner_workspace.parent.exists()
    else:
        assert outputs["BUILDX_CONFIG"] == str(
            runner_workspace.parent / "buildx" / "trusted-runner"
        )
        assert outputs["BUILDER"] == "ci-trusted-runner"


def test_real_kata_probe_is_unreachable_from_pull_requests_and_reported_as_skipped() -> None:
    """A green PR is never presented as qualification of a deployment host."""
    jobs = _document()["jobs"]
    kata = jobs["kata"]
    assert "github.event_name != 'pull_request'" in str(kata.get("if", ""))
    assert "github.ref == 'refs/heads/dev'" in kata["if"]
    assert kata["runs-on"] == ["self-hosted", "Linux", "X64", "image-verify-x64"]
    report = next(
        step
        for step in jobs["ci-gate"]["steps"]
        if step.get("name") == "Report the pull request qualification boundary"
    )
    assert report["if"] == "github.event_name == 'pull_request'"
    assert "skipped" in report["run"] and "GITHUB_STEP_SUMMARY" in report["run"]
