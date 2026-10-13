"""Unreviewed PR code never shares a runner or credential with publication.

Exercise the workflow's runner expressions for every cell rather than accepting
an expression merely because it contains a hosted runner somewhere. Runner-group
restrictions are also required in GitHub: a fork can propose workflow edits.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
CACHE_WORKFLOW = WORKFLOW.with_name("pr-caches.yml")


def _document() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _cache_document() -> dict:
    return yaml.safe_load(CACHE_WORKFLOW.read_text())


def _expression(
    value: str,
    event: str,
    cell: dict,
    *,
    fork: bool = False,
    runner_environment: str = "github-hosted",
) -> Any:
    """Evaluate the small Actions expression subset used for runner routing.

    An explicit AST allowlist avoids executing workflow text as Python and fails
    on new syntax until the routing test understands it.
    """
    source = value.removeprefix("${{").removesuffix("}}").strip()
    repository = "freeinference/cloud-agent"
    head_repository = "contributor/cloud-agent" if fork else repository
    for name, replacement in {
        "github.event.pull_request.head.repo.full_name": head_repository,
        "github.repository": repository,
        "github.event_name": event,
        "runner.environment": runner_environment,
        "matrix.artifact.name": cell.get("name"),
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


def _runner_labels(job: dict, event: str, cell: dict, *, fork: bool = False) -> list[str]:
    runs_on = job["runs-on"]
    return runs_on if isinstance(runs_on, list) else _expression(runs_on, event, cell, fork=fork)


def _cells(job: dict) -> list[dict]:
    return job.get("strategy", {}).get("matrix", {}).get("artifact", [{}])


def _self_hosted_labels(name: str, cell: dict) -> list[str]:
    special = {
        "compose": ["self-hosted", "Linux", "ARM64", "image-verify-arm64"],
        "kata": ["self-hosted", "Linux", "X64", "image-verify-x64"],
    }
    return (
        json.loads(cell["runner"])
        if name in {"artifacts", "mcp"}
        else special.get(name, ["self-hosted", "Linux", "ci-general"])
    )


# The hosted-only cache warmer has its own workflow, so its completion cannot
# delay the CI workflow's verdict used by publication.
CACHE_WARMER = "pr-caches"


@pytest.mark.parametrize("fork", [True, False], ids=["fork", "same-repository"])
def test_pull_requests_run_every_check_on_disposable_native_hosted_runners(fork: bool) -> None:
    """Forks, collaborators and same-repository PRs all take the hosted path."""
    for name, job in _document()["jobs"].items():
        if name == "kata":
            continue  # Its explicit PR exclusion is checked below.
        for cell in _cells(job):
            expected = "ubuntu-24.04-arm" if cell.get("arch") == "arm64" else "ubuntu-24.04"
            assert _runner_labels(job, "pull_request", cell, fork=fork) == [expected], (
                name,
                cell,
            )


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_trusted_events_keep_the_existing_self_hosted_pools(event: str) -> None:
    for name, job in _document()["jobs"].items():
        for cell in _cells(job):
            expected = _self_hosted_labels(name, cell)
            assert _runner_labels(job, event, cell) == expected, (name, cell)


def test_only_an_optional_cache_warmer_runs_a_trusted_event_on_a_hosted_vm() -> None:
    """Trusted CI and publication must never wait for a hosted VM.

    `pr-caches` runs dev's reviewed code on a hosted VM so that a pull request's
    caches match its own VM. Its separate workflow prevents a queued or slow
    hosted job from delaying the CI workflow's completion, which Publish waits
    for even after CI Gate has passed.
    """
    jobs = _document()["jobs"]
    document = _cache_document()
    triggers = document.get("on") or document[True]
    assert triggers == {"push": {"branches": ["dev"]}}
    assert document["permissions"] == {"contents": "read"}
    assert set(document["jobs"]) == {CACHE_WARMER}
    assert CACHE_WARMER not in jobs
    warmer = document["jobs"][CACHE_WARMER]
    assert warmer["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/dev'"
    assert warmer["runs-on"] == ["ubuntu-24.04"]
    assert warmer["continue-on-error"] is True
    assert "needs" not in warmer
    for name, job in jobs.items():
        needs = job.get("needs", [])
        assert CACHE_WARMER not in ([needs] if isinstance(needs, str) else needs), name
    assert warmer.get("permissions", document["permissions"]) == {"contents": "read"}
    assert "secrets." not in json.dumps(document)
    build = next(
        step for step in warmer["steps"] if "docker/build-push-action" in step.get("uses", "")
    )
    assert build["with"]["push"] is False
    assert build["with"]["outputs"] == "type=cacheonly", "the warmer exports layers, never an image"


def test_publication_waits_only_for_the_ci_workflow() -> None:
    """An optional job in CI still delays its workflow-level publication signal."""
    document = _document()
    assert CACHE_WARMER not in document["jobs"]
    assert _cache_document()["name"] != document["name"]
    publish = yaml.safe_load(WORKFLOW.with_name("publish.yml").read_text())
    triggers = publish.get("on") or publish[True]
    assert triggers["workflow_run"]["workflows"] == [document["name"]]
    assert triggers["workflow_run"]["types"] == ["completed"]
    scripts = "\n".join(
        step.get("with", {}).get("script", "")
        for job in publish["jobs"].values()
        for step in job.get("steps", [])
    )
    assert set(re.findall(r"workflow_id:\s*'([^']+)'", scripts)) == {WORKFLOW.name}


SANDBOX_CACHE = "type=gha,scope=sandbox-amd64"


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
@pytest.mark.parametrize("runner_environment", ["github-hosted", "self-hosted"])
def test_only_a_hosted_pull_request_imports_the_sandbox_layers(
    event: str, runner_environment: str
) -> None:
    """A cold hosted VM imports what dev exported; a warm persistent builder never does.

    On a persistent builder the import downloads layers it already holds. No cell
    exports, either: a pull request must not be able to write what another one
    restores, and the persistent pools' uploads are too slow to carry it.
    """
    jobs = _document()["jobs"]
    for name in ("artifacts", "mcp"):
        builds = [s for s in jobs[name]["steps"] if "docker/build-push-action" in s.get("uses", "")]
        assert len(builds) == 2, name  # the build and its retry
        for build in builds:
            assert "cache-to" not in build["with"], name
            for cell in _cells(jobs[name]):
                expected = (
                    SANDBOX_CACHE
                    if event == "pull_request"
                    and runner_environment == "github-hosted"
                    and cell["name"] == "sandbox"
                    else ""
                )
                imported = _expression(
                    build["with"]["cache-from"],
                    event,
                    cell,
                    runner_environment=runner_environment,
                )
                assert imported == expected, (name, cell["name"])
    warmer = next(
        step
        for step in _cache_document()["jobs"][CACHE_WARMER]["steps"]
        if "docker/build-push-action" in step.get("uses", "")
    )
    assert warmer["with"]["file"] == "deploy/sandbox/Dockerfile.agent-sandbox"
    assert warmer["with"]["cache-from"] == SANDBOX_CACHE
    assert warmer["with"]["cache-to"] == f"{SANDBOX_CACHE},mode=max"


def test_the_gate_waits_for_every_ci_job() -> None:
    """A job the gate does not wait for is a check that cannot fail a commit."""
    jobs = _document()["jobs"]
    gate = jobs["ci-gate"]
    assert set(gate["needs"]) == set(jobs) - {"ci-gate"}
    report = next(
        step
        for step in gate["steps"]
        if step.get("name") == "Every job succeeded or was deliberately skipped"
    )
    for need in gate["needs"]:
        assert f"${{{{ needs.{need}.result }}}}" in report["env"]["RESULTS"], need


def test_warmed_dependency_caches_use_the_paths_pull_requests_restore() -> None:
    """The cache service keys an entry by its path; a different path never hits."""
    jobs = _document()["jobs"]
    warmer_job = _cache_document()["jobs"][CACHE_WARMER]
    warmer = "\n".join(step.get("run", "") for step in warmer_job["steps"])
    for name in ("lint", "test", "test-db", "artifacts", "mcp", "compose"):
        assert any(
            'echo "UV_CACHE_DIR=${RUNNER_TEMP}/uv-cache"' in step.get("run", "")
            for step in jobs[name]["steps"]
        ), name
    for name in ("check", "web-build"):
        assert any(
            'echo "npm_config_cache=${RUNNER_TEMP}/npm-cache"' in step.get("run", "")
            for step in jobs[name]["steps"]
        ), name
    assert 'echo "UV_CACHE_DIR=${RUNNER_TEMP}/uv-cache"' in warmer
    assert 'echo "npm_config_cache=${RUNNER_TEMP}/npm-cache"' in warmer
    node = next(step for step in warmer_job["steps"] if step.get("name") == "Set up Node")
    assert node["with"] == next(
        step["with"] for step in jobs["check"]["steps"] if step.get("name") == "Set up Node"
    )


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


#: Every workflow this repository runs, publication and synchronisation included.
WORKFLOWS = sorted([*WORKFLOW.parent.glob("*.yml"), *WORKFLOW.parent.glob("*.yaml")])
_PINNED = re.compile(r"[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}")
_PINNED_LINE = re.compile(r"(?:-\s+)?uses:\s+\S+@[0-9a-f]{40} # v\d+(?:\.\d+)*")


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_every_action_is_pinned_to_a_commit(workflow: Path) -> None:
    """A tag can be moved to other code; a commit cannot.

    Publication runs these actions beside `packages: write`, `contents: write`
    and the Docker Hub token, on persistent runners where whatever a moved tag
    delivers can also outlive the job. Each reference therefore names the
    commit, and a comment names the release it is, which is how a person or an
    update tool knows what to move it to.
    """
    text = workflow.read_text()
    jobs = yaml.safe_load(text)["jobs"].values()
    references = [job["uses"] for job in jobs if "uses" in job]
    references += [step["uses"] for job in jobs for step in job.get("steps", []) if "uses" in step]
    assert references, f"{workflow.name} uses no action; the check would prove nothing"
    remote = [reference for reference in references if not reference.startswith("./")]
    for reference in remote:
        assert _PINNED.fullmatch(reference), f"{workflow.name}: {reference} is not a commit"
    lines = [
        line.strip() for line in text.splitlines() if re.match(r"\s*(?:-\s+)?uses:\s+(?!\./)", line)
    ]
    assert len(lines) == len(remote)
    for line in lines:
        assert _PINNED_LINE.fullmatch(line), f"{workflow.name}: {line!r} names no release"


def _syncs(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if re.match(r"\s*uv sync\b", line)]


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_every_dependency_sync_refuses_a_stale_lock(workflow: Path) -> None:
    """A bare `uv sync` re-resolves a uv.lock that no longer matches pyproject.toml.

    The images install the committed lock as it stands, so a job that tested a
    fresh resolution would vouch for packages no image ships. `--locked` makes
    that job fail instead, on every event and in every workflow. Each job that
    syncs installs the uv CI pins, so every one of them judges the lock alike.
    """
    document = yaml.safe_load(workflow.read_text())
    syncing = []
    for name, job in document["jobs"].items():
        steps = job.get("steps", [])
        syncs = [command for step in steps for command in _syncs(str(step.get("run", "")))]
        for command in syncs:
            assert "--locked" in command.split(), f"{workflow.name}: {command}"
        if syncs:
            syncing.append(name)
            installs = [step for step in steps if "astral-sh/setup-uv@" in step.get("uses", "")]
            versions = [step.get("with", {}).get("version") for step in installs]
            assert versions == ["${{ env.UV_VERSION }}"], f"{workflow.name}: {name}"
            assert document["env"]["UV_VERSION"] == _document()["env"]["UV_VERSION"]
    if workflow.name in {"ci.yml", "publish.yml", "pr-caches.yml"}:
        assert syncing, f"{workflow.name} no longer syncs; the check would prove nothing"


def test_the_local_launcher_refuses_a_stale_lock_like_ci() -> None:
    """`make ci` is the local copy of these jobs; it must fail where they fail."""
    launcher = (WORKFLOW.parents[2] / "scripts/ci-local.sh").read_text()
    syncs = _syncs(launcher)
    assert syncs
    for command in syncs:
        assert "--locked" in command.split(), command


def test_checkouts_leave_no_token_in_the_unreviewed_repository() -> None:
    for document in (_document(), _cache_document()):
        for name, job in document["jobs"].items():
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
    assert 'docker ps -a --filter "name=${stale_prefix}"' in script
    assert 'docker rm -fv "${container}"' in script
    assert 'docker volume rm "${container}_state"' in script
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
        "GITHUB_REPOSITORY": "HarvardMadSys/hybridInference-cloud-agent",
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
        assert (
            outputs["BUILDER"]
            == "ci-pr-harvardmadsys-hybridinference-cloud-agent-trusted-runner-123-2-sandbox"
        )
        assert not runner_workspace.parent.exists()
    else:
        assert outputs["BUILDX_CONFIG"] == str(
            runner_workspace.parent / "buildx" / "trusted-runner"
        )
        assert outputs["BUILDER"] == "ci-trusted-runner"


def test_pull_request_builder_is_always_removed_after_the_artifact_job() -> None:
    """Whatever happened, on any persistent runner. A hosted VM is deleted with it."""
    steps = _document()["jobs"]["artifacts"]["steps"]
    cleanup = [step for step in steps if 'docker buildx rm "${BUILDER}"' in step.get("run", "")]
    assert len(cleanup) == 1
    assert cleanup[0]["if"] == (
        "always() && github.event_name == 'pull_request' && runner.environment != 'github-hosted'"
    )
    assert steps[-1] is cleanup[0]


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
@pytest.mark.parametrize("runner_environment", ["github-hosted", "self-hosted"])
def test_sandbox_disk_cleanup_runs_only_for_hosted_pull_request_cells(
    event: str, runner_environment: str
) -> None:
    """Persistent runners and other artifact cells must retain their SDKs and caches."""
    artifacts = _document()["jobs"]["artifacts"]
    cleanup = [step for step in artifacts["steps"] if step.get("name", "").startswith("Free ")]
    assert len(cleanup) == 2
    for cell in _cells(artifacts):
        expected = (
            event == "pull_request"
            and runner_environment == "github-hosted"
            and cell["name"] == "sandbox"
        )
        for step in cleanup:
            assert (
                _expression(step["if"], event, cell, runner_environment=runner_environment)
                is expected
            )


GIB_IN_KIB = 1024 * 1024
FREED = [
    "sudo",
    "rm",
    "-rf",
    "/usr/local/lib/android",
    "/usr/share/dotnet",
    "/opt/ghc",
    "/opt/hostedtoolcache/CodeQL",
    "docker",
    "buildx",
    "prune",
    "--builder",
    "ci-pr-123-2-sandbox",
    "--all",
    "--force",
]


@pytest.mark.parametrize(
    ("available_kib", "expected"),
    [
        (None, FREED),  # a df that says nothing is treated as a full disk
        (39 * GIB_IN_KIB, FREED),  # a private repository's runner
        (41 * GIB_IN_KIB, []),
        (100 * GIB_IN_KIB, []),  # a public repository's runner after the build
    ],
    ids=["unreadable", "tight", "just-enough", "public"],
)
def test_sandbox_disk_cleanup_keeps_loaded_images_and_other_builder_caches(
    tmp_path: Path, available_kib: int | None, expected: list[str]
) -> None:
    """Execute cleanup with command recorders instead of SDK files or a Docker daemon.

    Only a runner short of space pays the 90s deletion. Whatever it frees, it
    frees nothing else: no loaded image, no other builder's cache.
    """
    steps = _document()["jobs"]["artifacts"]["steps"]
    sdks = next(
        step
        for step in steps
        if step.get("name") == "Free unused SDKs on the hosted sandbox runner"
    )
    cache = next(
        step
        for step in steps
        if step.get("name") == "Free the hosted sandbox builder's unused cache"
    )
    builder = next(
        step for step in steps if step.get("name") == "Reuse this runner's builder, or make one"
    )
    retry = next(
        step for step in steps if step.get("name") == "Build ${{ matrix.artifact.name }} (retry)"
    )
    uv = next(step for step in steps if step.get("name") == "Install uv")
    assert steps.index(sdks) < steps.index(builder)
    assert steps.index(retry) < steps.index(cache) < steps.index(uv)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = tmp_path / "commands"
    for tool in ("sudo", "docker"):
        executable = bin_dir / tool
        executable.write_text(
            '#!/bin/sh\nprintf "%s\\n" "${0##*/}" "$@" >> "$CLEANUP_COMMAND_LOG"\n'
        )
        executable.chmod(0o755)
    # `df --output=avail -k /` prints a header and the free KiB.
    df = bin_dir / "df"
    df.write_text(
        "#!/bin/sh\nexit 0\n"
        if available_kib is None
        else f'#!/bin/sh\nprintf "     Avail\\n %s\\n" {available_kib}\n'
    )
    df.chmod(0o755)
    # Define recorders inside Bash so managed shell startup cannot reorder PATH
    # and expose the host's real sudo, Docker, or df commands.
    recorders = "\n".join(
        f'{tool}() {{ {shlex.quote(str(bin_dir / tool))} "$@"; }}'
        for tool in ("sudo", "docker", "df")
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLEANUP_COMMAND_LOG": str(commands),
        "BUILDER": "ci-pr-123-2-sandbox",
        "IMAGE_TAG": "sandbox:ci-123-2-sandbox",
    }
    for step in (sdks, cache):
        subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
        subprocess.run(
            ["bash", "-c", f"{recorders}\n{step['run']}"],
            cwd=tmp_path,
            env=env,
            check=True,
        )
    recorded = commands.read_text().splitlines() if commands.exists() else []
    assert recorded == expected


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
