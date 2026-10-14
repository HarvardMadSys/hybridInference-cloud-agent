"""The image job builds every artifact, starts it where it runs, and cannot publish.

One job does both halves, and it is the only place in this repository where a
container from a pull request is started. Two properties therefore matter beyond
coverage: that nothing in it can push, and that its matrix covers exactly the
artifacts a release carries — a missing cell is an image that reaches production
having never been run, which is the failure this job exists to prevent rather
than to embody.

The checks themselves are not asserted here; `test_artifact_smoke.py` covers the
table, and the job is only the wiring that runs it.
"""

from __future__ import annotations

import copy
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from deploy.release import build_host_bundle, release_manifest as rm, smoke
from scripts.ci_image_matrix import image_matrix

REPO_ROOT = Path(__file__).resolve().parents[3]
CI = REPO_ROOT / ".github/workflows/ci.yml"
CONSOLE_DOCKERFILE = REPO_ROOT / "deploy/control-plane/Dockerfile"
RUNNER_DOCKERFILE = REPO_ROOT / "deploy/host/Dockerfile.agent-runner"
HOST_STACK = REPO_ROOT / "deploy/host/docker-compose.sandbox.yml"
WEB_DOCKERFILE = REPO_ROOT / "deploy/control-plane/Dockerfile.web"


def workflow() -> dict[str, Any]:
    return yaml.safe_load(CI.read_text())


def cells() -> list[dict[str, Any]]:
    assert workflow()["jobs"]["artifacts"]["strategy"]["matrix"]["artifact"] == (
        "${{ fromJSON(needs.changes.outputs.artifacts) }}"
    )
    return image_matrix("all")


def step(name_fragment: str, job: str = "artifacts") -> dict[str, Any]:
    for candidate in workflow()["jobs"][job]["steps"]:
        if name_fragment in str(candidate.get("name", "")):
            return candidate
    raise AssertionError(f"no step in {job} matches {name_fragment!r}")


def expected_smoke_args() -> set[str]:
    expected = {f"--role {role}" for role in smoke.ROLES if role != "web"}
    expected |= {f"--role web --variant {variant}" for variant in smoke.ROLES["web"].variants}
    return expected


def test_every_artifact_a_release_carries_is_built_and_started_once() -> None:
    covered = [cell["smoke_args"] for cell in cells()]
    assert sorted(covered) == sorted(expected_smoke_args()), (
        "the matrix and the start checks disagree; every artifact in the release contract is "
        "built and started exactly once, and web once per variant"
    )
    assert len(covered) == len(set(covered)), "a cell is duplicated"


def test_each_cell_runs_on_its_own_architecture() -> None:
    """The pool label and the declared architecture have to be the same machine.

    A cell that declares arm64 and queues on the x64 pool builds the wrong image
    and passes, which is worse than failing: the verification would be about an
    architecture the artifact is never deployed on.
    """
    pools = {"linux/arm64": ("arm64", "ARM64"), "linux/amd64": ("amd64", "X64")}
    for cell in cells():
        role = cell["smoke_args"].split()[1]
        short, label = pools[rm.EXPECTED_PLATFORMS[role][0]]
        assert cell["arch"] == short, f"{cell['name']} declares {cell['arch']!r}"
        assert label in cell["runner"], (
            f"{cell['name']} declares {cell['arch']} and queues on {cell['runner']}"
        )


def test_nothing_in_ci_publishes() -> None:
    """A pull request must not be able to reach a registry, however it changes the file.

    `push: true` anywhere in CI is the whole risk: the job that builds on a pull
    request runs that pull request's Dockerfiles with whatever credentials the
    workflow holds. The publication workflow is a separate file with its own
    trigger, and this is what keeps the two from drifting into one.
    """
    text = CI.read_text()
    assert "push: true" not in text
    assert "push: false" in text


def test_the_start_check_runs_the_image_this_job_built() -> None:
    run = str(step("Start it")["run"])
    assert '--image "${IMAGE_TAG}"' in run
    assert "--revision" in run, (
        "the revision label is what a resolver compares with the release manifest, and the "
        "build argument that fills it is only proven to arrive by checking it here"
    )
    naming = str(step("Name this cell")["run"])
    assert "IMAGE_TAG=" in naming
    assert "GITHUB_RUN_ID" in naming and "GITHUB_RUN_ATTEMPT" in naming, (
        "per-run names are what keep a retry and a concurrent attempt independently diagnosable"
    )


def test_the_build_loads_the_image_it_is_about_to_start() -> None:
    """The docker exporter puts the image on the daemon, uncompressed.

    The daemon is its only consumer, and on a hosted VM the sandbox spent a
    minute gzipping layers the daemon then unpacked. `load: true` is the same
    exporter with its default compression, so it is not set beside this.
    """
    for fragment in (
        "Build ${{ matrix.artifact.name }}",
        "Build ${{ matrix.artifact.name }} (retry)",
    ):
        build = step(fragment)
        assert build["with"]["outputs"] == "type=docker,compression=uncompressed", (
            f"{fragment} must load the image onto this runner's daemon; the next step starts it "
            "from there, and a self-hosted runner shares no filesystem with the job that built it"
        )
        assert "load" not in build["with"]
        assert build["with"]["push"] is False


def test_no_cell_exports_a_cache_and_only_the_sandbox_imports_one() -> None:
    """The persistent per-runner builder is the cache used by later builds.

    A GHA exporter without an importer uploaded the sandbox's large runtime
    layers on every run but no build ever consumed them. The export alone took
    minutes and occupied every x64 runner on their shared physical host. So no
    cell exports; `pr-caches` does, from a hosted VM, and only the sandbox —
    the one image whose cold build is long — imports it. Which events import
    is pinned in `test_ci_workflow_policy.py`, where the expression is evaluated.
    """
    for fragment in (
        "Build ${{ matrix.artifact.name }}",
        "Build ${{ matrix.artifact.name }} (retry)",
    ):
        build = step(fragment)
        assert "cache-to" not in build["with"]
        imported = str(build["with"]["cache-from"])
        assert "matrix.artifact.name == 'sandbox'" in imported
        assert "'type=gha,scope=sandbox-amd64'" in imported


def test_the_build_tags_the_image_the_smoke_starts() -> None:
    """`load: true` without a tag loads an image the next step cannot find.

    Found by this job's first real run: the build reported success, the smoke
    inspected the tag it had been told to start, and Docker answered `no such
    object`. The docker exporter needs a name to load under, so the tag is
    asserted here rather than rediscovered.
    """
    for fragment in (
        "Build ${{ matrix.artifact.name }}",
        "Build ${{ matrix.artifact.name }} (retry)",
    ):
        assert step(fragment)["with"]["tags"] == "${{ env.IMAGE_TAG }}"
    assert "IMAGE_TAG=" in str(step("Name this cell")["run"])


def test_mcp_interactions_run_before_smoke_removes_the_image() -> None:
    """The smoke harness cleans its image, so later CLI tests cannot start it."""
    steps = workflow()["jobs"]["artifacts"]["steps"]
    interactions = step("All five CLIs retain MCP")
    smoke_step = step("Start it, and assert")
    assert interactions["env"]["MCP_TEST_SANDBOX_IMAGE"] == "${{ env.IMAGE_TAG }}"
    assert steps.index(interactions) < steps.index(smoke_step)


def test_mcp_interactions_run_in_isolated_parallel_workers() -> None:
    """Each xdist worker needs the database suffix the test config selects."""
    interactions = step("All five CLIs retain MCP")
    assert interactions["env"]["MCP_TEST_WORKERS"] == "3"
    assert interactions["timeout-minutes"] >= 20
    run = str(interactions["run"])
    assert '"${TEST_DB_NAME}_gw${worker}"' in run
    assert "createdb" in run
    assert '-n "${MCP_TEST_WORKERS}" --dist load' in run
    assert run.index("createdb") < run.index("uv run pytest")


PARITY = "tests/external/test_resident_mcp_parity.py"

# Copied verbatim from `artifacts` into `mcp`, so the cases a pull request runs
# use the image the sandbox cell builds and starts, built and freed the same way.
SHARED_MCP_STEPS = (
    "Free unused SDKs on the hosted sandbox runner",
    "Reuse this runner's builder, or make one",
    "Build ${{ matrix.artifact.name }}",
    "Build ${{ matrix.artifact.name }} (retry)",
    "Free the hosted sandbox builder's unused cache",
    "Isolate uv cache dir",
    "Install uv",
    "Drop this run's containers and image",
    "Drop this pull request's builder",
)


def _mcp_case_step() -> dict[str, Any]:
    return next(
        candidate
        for candidate in workflow()["jobs"]["mcp"]["steps"]
        if "retains MCP through approval" in str(candidate.get("name", ""))
    )


SANDBOX_CELL_SELECTION = "${{ github.event_name == 'pull_request' && 'not kilo]' || '' }}"


def test_pull_requests_give_kilo_its_own_vms_and_trusted_events_run_every_case_together() -> None:
    """Every event runs all fifteen cases; only where they run differs.

    Each Kilo case needs most of the test's 180s turn timeout on a hosted VM by
    itself, and beside another one it runs past it — the cause of most hosted
    failures of this suite. Three cases of any other runtime take 20-40s
    together. A pull request therefore runs the other twelve in the sandbox
    cell and each Kilo case on a VM of its own. Trusted events keep all fifteen
    in the persistent-pool step.
    """
    jobs = workflow()["jobs"]
    in_cell = step("All five CLIs retain MCP")
    assert in_cell["if"] == "matrix.artifact.name == 'sandbox'"
    assert in_cell["env"]["MCP_TEST_SELECTION"] == SANDBOX_CELL_SELECTION
    assert jobs["mcp"]["if"] == (
        "github.event_name == 'pull_request' && needs.changes.outputs.docker == 'true' "
        "&& needs.changes.outputs.image_scope != 'web'"
    )
    assert jobs["mcp"]["needs"] == "changes"
    assert jobs["mcp"]["strategy"]["fail-fast"] is False
    gate = jobs["ci-gate"]
    assert "mcp" in gate["needs"]
    assert "mcp=${{ needs.mcp.result }}" in str(step("Every job succeeded", job="ci-gate")["env"])


def _selects(expression: str, case: str) -> bool:
    """`-k` for the two shapes used here: `<id fragment>` and `not <id fragment>`.

    Each fragment ends in `]`, which only an item name can contain, so a
    substring of the collected id is exactly what `-k` matches.
    """
    negated = expression.startswith("not ")
    fragment = expression.removeprefix("not ").lower()
    return (fragment in case.lower()) != negated


def test_the_mcp_cells_partition_the_parity_suite() -> None:
    """On a pull request every case runs exactly once; on a trusted event, all of them.

    Collected from the real test file, so a runtime or policy added to its
    parametrization and not to these selections fails here rather than silently
    going untested.
    """
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "external", PARITY],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    cases = {line for line in collected.splitlines() if line.startswith(f"{PARITY}::")}
    assert len(cases) == 15
    template = _mcp_case_step()["env"]["MCP_TEST_SELECTION"]
    assert template == "${{ matrix.case }}-kilo]"
    selections = ["not kilo]"] + [
        template.replace("${{ matrix.case }}", case)
        for case in workflow()["jobs"]["mcp"]["strategy"]["matrix"]["case"]
    ]
    selected: list[str] = []
    for selection in selections:
        mine = sorted(case for case in cases if _selects(selection, case))
        assert mine, f"{selection} selects nothing"
        selected.extend(mine)
    assert sorted(selected) == sorted(cases), "the cells overlap or leave a case out"
    assert len([case for case in cases if _selects("not kilo]", case)]) == 12


def test_the_mcp_cells_build_and_clean_up_exactly_like_the_sandbox_cell() -> None:
    jobs = workflow()["jobs"]
    sandbox = next(cell for cell in cells() if cell["name"] == "sandbox")
    assert jobs["mcp"]["strategy"]["matrix"]["artifact"] == [sandbox]
    for name in SHARED_MCP_STEPS:
        assert step(name, job="mcp") == step(name), f"{name} differs between artifacts and mcp"
    ours, theirs = _mcp_case_step(), step("All five CLIs retain MCP")
    assert ours["run"] == theirs["run"], "the cases must run the way the sandbox cell runs them"
    assert ours["timeout-minutes"] == theirs["timeout-minutes"]
    selection = {"MCP_TEST_SELECTION": "compared in the partition test"}
    assert {**ours["env"], **selection} == {**theirs["env"], **selection}
    assert '-k "${MCP_TEST_SELECTION}"' in ours["run"]


def test_each_mcp_cell_removes_only_what_it_started() -> None:
    """The cells of one run share its id, so the label carries the case too."""
    steps = workflow()["jobs"]["mcp"]["steps"]
    naming = str(step("Name this cell", job="mcp")["run"])
    assert "matrix.case" in naming and "GITHUB_RUN_ATTEMPT" in naming
    assert 'echo "DOCKER_CONFIG=${RUNNER_TEMP}/docker-config" >> "${GITHUB_ENV}"' in naming
    assert steps[-1]["name"] == "Drop this pull request's builder"
    assert not any("secrets." in str(candidate) for candidate in steps)


def test_cleanup_cannot_reach_another_cell() -> None:
    """Six cells of one run share a run id, an attempt — and often a Docker host.

    A label built from the first two is identical for all of them, so the first
    cell to finish removes the others' containers while they are still being
    tested. That is what happened to the control cell and the since-removed egress
    cell, twice, before the artifact name went into the label.
    """
    naming = str(step("Name this cell")["run"])
    assert "matrix.artifact.name" in naming, (
        "the label has to differ per cell, or cleanup reaches into the other five"
    )
    labels = {f"345-1-{cell['name']}" for cell in cells()}
    assert len(labels) == len(cells())
    assert "label=io.hybridinference.smoke.run=${SUFFIX}" in str(step("Drop this run's")["run"])


def test_the_web_image_ships_the_node_major_ci_checks_the_app_with() -> None:
    """CI lints, tests and builds the app on one Node major; the image must run that one.

    Both stages stayed on node:20 after CI moved to 22, so every check ran on a
    different runtime from the one serving traffic, and Node 20's end of life
    on 2026-04-30 left that internet-facing server without security fixes.
    """
    majors = re.findall(r"^FROM node:(\d+)\b", WEB_DOCKERFILE.read_text(), re.MULTILINE)
    stages = re.findall(r"^FROM ", WEB_DOCKERFILE.read_text(), re.MULTILINE)
    assert len(majors) == len(stages) == 2, "a builder and a runtime stage, both on Node"
    expected = str(workflow()["env"]["NODE_VERSION"])
    assert set(majors) == {expected}, f"Dockerfile.web uses Node {majors}, CI uses {expected}"
    caches = yaml.safe_load((REPO_ROOT / ".github/workflows/pr-caches.yml").read_text())
    assert str(caches["env"]["NODE_VERSION"]) == expected


def test_the_build_passes_the_commit_every_image_must_name() -> None:
    build = step("Build ${{ matrix.artifact.name }}")
    assert "SOURCE_COMMIT=${{ github.sha }}" in str(build["with"]["build-args"])


@pytest.mark.parametrize("dockerfile", sorted({str(cell["dockerfile"]) for cell in cells()}))
def test_the_commit_reaches_no_layer(dockerfile: str) -> None:
    """A RUN after `ARG SOURCE_COMMIT` has the commit in its environment.

    Every such layer then rebuilds on every commit, in every builder and every
    layer cache. In the sandbox that was five layers below the label, rebuilt and
    exported again on each run while nothing in them had changed.
    """
    text = re.sub(r"\\\n\s*", " ", (REPO_ROOT / dockerfile).read_text())
    instructions = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    declared = [i for i, line in enumerate(instructions) if line.startswith("ARG SOURCE_COMMIT")]
    assert declared, f"{dockerfile} never declares SOURCE_COMMIT"
    after = instructions[declared[-1] :]
    assert not [line for line in after if line.startswith("RUN ")], (
        f"{dockerfile} runs a layer after ARG SOURCE_COMMIT; declare it after the last RUN"
    )


def _classify_paths(paths: list[str]) -> dict[str, str]:
    """Execute the classifier with changed paths and a local-only Git double."""
    script = str(step("Classify changed paths", job="changes")["run"])
    with tempfile.TemporaryDirectory(prefix="ci-classifier-") as directory:
        temporary = Path(directory)
        git = temporary / "git"
        git.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "cat-file) exit 0 ;;\n"
            "describe) echo agent-release ;;\n"
            'diff) printf "%s\\n" "$CHANGED_PATH" ;;\n'
            "*) exit 1 ;;\n"
            "esac\n"
        )
        git.chmod(0o755)
        output = temporary / "outputs"
        environment = {
            **os.environ,
            "PATH": f"{temporary}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_SHA": "b" * 40,
            "BEFORE": "a" * 40,
            "GITHUB_OUTPUT": str(output),
            "CHANGED_PATH": "\n".join(paths),
        }
        subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", script],
            env=environment,
            cwd=temporary,
            check=True,
            capture_output=True,
            text=True,
        )
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return values


def _runs_the_image_jobs(path: str) -> bool:
    return _classify_paths([path])["docker"] == "true"


def _copied_paths(dockerfile: Path) -> set[str]:
    """Repository paths one Dockerfile COPYs out of the build context.

    The full relative source, not its first component: the layouts this has to
    survive put the shared packages at the root today and under a common parent
    after the R5 move, and only the full path keeps the check meaningful in both.
    """
    sources = set()
    text = re.sub(r"\\\n\s*", " ", dockerfile.read_text())
    for line in text.splitlines():
        if not line.startswith("COPY") or "--from=" in line:
            continue
        arguments = [word for word in shlex.split(line)[1:] if not word.startswith("--")]
        for source in arguments[:-1]:
            if not source.startswith("/"):
                sources.add(source.rstrip("/"))
    return sources


def _packages_both_images_ship() -> set[str]:
    """What the console and the runner images both COPY, read from the files."""
    return _copied_paths(CONSOLE_DOCKERFILE) & _copied_paths(RUNNER_DOCKERFILE)


def _all_image_inputs() -> set[str]:
    sources = {
        ".dockerignore",
        "tests/external/test_upload_edge_limits.py",
        "tests/external/test_retained_command_limits.py",
    }
    # The control image embeds these reviewed source inputs using a bind
    # mount, rather than a separate COPY instruction for each archive member.
    sources.update(build_host_bundle.PACKAGE_ROOTS)
    sources.update(build_host_bundle.HOST_FILES)
    for cell in cells():
        dockerfile = Path(str(cell["dockerfile"]))
        sources.add(dockerfile.as_posix())
        sources.update(_copied_paths(REPO_ROOT / dockerfile))
    return sources


@pytest.mark.parametrize("entry", sorted(_all_image_inputs()))
def test_every_release_image_input_selects_image_checks(entry: str) -> None:
    """COPY and the embedded Host bundle's source inputs all need coverage."""
    probe = f"{entry}/module.py" if (REPO_ROOT / entry).is_dir() else entry
    assert _runs_the_image_jobs(probe), f"image input {entry} selects no image checks"


@pytest.mark.parametrize("path", ["docs/developer/operations.md", "tests/unit/test_identity.py"])
def test_non_image_inputs_do_not_select_image_checks(path: str) -> None:
    """Full input coverage should preserve useful classification of unrelated changes."""
    assert not _runs_the_image_jobs(path)


def test_new_external_tests_select_image_checks() -> None:
    """New integration files need the image jobs without classifier edits."""
    assert _runs_the_image_jobs("tests/external/test_new_container_integration.py")


def test_the_default_test_tier_runs_whatever_changed() -> None:
    """Its tests read files the classifier counts as docs or the web app.

    The credential backstop scans every tracked file, the environment
    inventory reads apps/frontend/ and ENVIRONMENT.md, and the deployment tests
    read every Markdown file under docs/. Gated on `backend`, a docs- or
    frontend-only change that broke one of them skipped this job, passed the
    gate, and left the failure to the next backend change.
    """
    jobs = workflow()["jobs"]
    assert "if" not in jobs["test"], jobs["test"]["if"]
    assert jobs["test"]["needs"] == "changes"
    assert "test" in jobs["ci-gate"]["needs"]


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repository: Path, path: str) -> str:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{path}\n")
    _git(repository, "add", path)
    _git(repository, "commit", "--quiet", "-m", f"change {path}")
    return _git(repository, "rev-parse", "HEAD")


def _release(repository: Path, sha: str) -> None:
    """What publication leaves behind: a lightweight `agent-<sha>` tag."""
    _git(repository, "tag", f"agent-{sha}", sha)


def _classify_push(repository: Path, sha: str, *, before: str) -> dict[str, str]:
    """Run the real classifier for a push, against real history and tags."""
    script = str(step("Classify changed paths", job="changes")["run"])
    output = repository.parent / f"outputs-{sha}"
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        cwd=repository,
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_SHA": sha,
            "BEFORE": before,
            "GITHUB_OUTPUT": str(output),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.fixture
def history(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet", "--initial-branch=dev")
    for key, value in (
        ("user.name", "CI fixture"),
        ("user.email", "ci@example.invalid"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
        ("core.hooksPath", str(tmp_path / "no-hooks")),
    ):
        _git(repository, "config", key, value)
    return repository


SELECTS_NOTHING = {
    "backend": "false",
    "web": "false",
    "docker": "false",
    "docs": "false",
    "image_scope": "all",
}
SELECTS_EVERYTHING = {
    "backend": "true",
    "web": "true",
    "docker": "true",
    "docs": "true",
    "image_scope": "all",
}


def test_a_push_is_checked_against_the_newest_release_not_the_previous_push(
    history: Path,
) -> None:
    """A docs-only push after an unreleased backend change still checks the backend.

    The backend commit's own run failed, or was cancelled by the push after it,
    so it was never released. Diffing from the previous push left its change
    out: every job skipped, the gate passed, and publication released a commit
    whose backend no run had checked.
    """
    released = _commit(history, "README.md")
    _release(history, released)
    unreleased = _commit(history, "apps/backend/cloud_agent/feature.py")
    docs = _commit(history, "docs/feature.md")
    selected = _classify_push(history, docs, before=unreleased)
    assert selected["backend"] == "true"
    assert selected["docker"] == "true"


def test_a_push_right_after_a_release_keeps_the_path_filter(history: Path) -> None:
    released = _commit(history, "apps/backend/cloud_agent/feature.py")
    _release(history, released)
    docs = _commit(history, "docs/feature.md")
    # A Markdown change builds the documentation site and nothing else.
    assert _classify_push(history, docs, before=released) == {**SELECTS_NOTHING, "docs": "true"}


def test_a_push_with_no_release_behind_it_runs_everything(history: Path) -> None:
    first = _commit(history, "README.md")
    docs = _commit(history, "docs/feature.md")
    assert _classify_push(history, docs, before=first) == SELECTS_EVERYTHING


def test_a_rerun_of_a_released_commit_is_not_compared_with_its_own_release(
    history: Path,
) -> None:
    """Otherwise the re-run would find nothing changed and check nothing."""
    released = _commit(history, "README.md")
    _release(history, released)
    backend = _commit(history, "apps/backend/cloud_agent/feature.py")
    first_run = _classify_push(history, backend, before=released)
    _release(history, backend)
    assert _classify_push(history, backend, before=released) == first_run
    assert first_run["backend"] == "true"


def test_real_upload_limits_are_checked_by_compose_ci() -> None:
    """Static caps also need a real nginx syntax/path/body-limit regression."""
    check = step("Upload limits match encoded attachments", job="compose")
    assert check["env"]["AGENT_UPLOAD_TEST_NGINX_IMAGE"] == "nginx:1.27.5-alpine"
    assert "docker --host=unix:///var/run/docker.sock pull" in check["run"]
    assert "pytest -q -m external tests/external/test_upload_edge_limits.py" in check["run"]


def _check_rendered_stack(tmp_path: Path, stack: dict[str, Any]) -> subprocess.CompletedProcess:
    """Run the compose job's security check on ``stack``, as the job runs it."""
    run = str(step("The rendered stack keeps its security properties", job="compose")["run"])
    source = run.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    (tmp_path / "rendered.yml").write_text(yaml.safe_dump(stack))
    return subprocess.run(
        [sys.executable, "-c", source],
        env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_compose_job_pins_who_holds_the_docker_socket(tmp_path: Path) -> None:
    """`:ro` does not narrow the Docker API; the holders are what CI can pin.

    Whatever can connect to the socket can start a privileged container with
    the host's root mounted. The check used to fail only a writable mount and
    report "docker socket read-only" as the property, so a third service
    holding the socket read-only passed. The shipped stack is the positive
    control: its values are uninterpolated, and every comparison still holds.
    """
    shipped = yaml.safe_load(HOST_STACK.read_text())
    passed = _check_rendered_stack(tmp_path, shipped)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert "held by runner and sandbox manager only" in passed.stdout
    assert "read-only" not in passed.stdout

    third = copy.deepcopy(shipped)
    tunnel = third["services"]["control-plane-tunnel"]
    tunnel["volumes"].append("/var/run/docker.sock:/var/run/docker.sock:ro")
    refused = _check_rendered_stack(tmp_path, third)
    assert refused.returncode == 1
    assert "control-plane-tunnel" in refused.stdout

    renamed = copy.deepcopy(shipped)
    renamed["services"]["control-plane-tunnel"]["volumes"].append(
        {
            "type": "bind",
            "source": "/var/run/docker.sock",
            "target": "/run/engine.sock",
            "read_only": True,
        }
    )
    refused = _check_rendered_stack(tmp_path, renamed)
    assert refused.returncode == 1, "a socket mounted under another name is still the socket"
    assert "control-plane-tunnel" in refused.stdout


def test_real_command_cleanup_is_checked_by_compose_ci() -> None:
    """Timeout and output bounds must reap actual Docker process siblings."""
    check = step("Retained commands clean up Docker siblings", job="compose")
    assert check["env"]["AGENT_COMMAND_TEST_IMAGE"] == "postgres:15"
    assert "docker --host=unix:///var/run/docker.sock pull" in check["run"]
    assert (
        "uv run --frozen pytest -q -m external tests/external/test_retained_command_limits.py"
        in check["run"]
    )


def test_a_change_to_the_console_runs_the_image_jobs() -> None:
    """apps/frontend/ is half of the console image, and used to skip every docker job.

    Dockerfile.web copies `apps/frontend/`, so a pull request that only touched the app
    changed what that image contains and ran none of the jobs that build it —
    which is how the image went unverified in the first place.
    """
    assert _runs_the_image_jobs("apps/frontend/src/app/page.tsx")


@pytest.mark.parametrize("entry", sorted(_packages_both_images_ship()))
def test_a_change_to_what_both_images_ship_runs_the_image_jobs(entry: str) -> None:
    """Both images COPY and import the shared packages, so both must rebuild.

    `cloud_agent_git/` and `cloud_agent_contracts/` reached both Dockerfiles
    when the R2/R3 moves created them, and the classifier was never told: a
    change to the shared Git algorithms or the shared protocol alone ran no
    docker job at all. The failure mode is the one those jobs exist for — it
    imports in this checkout and not in the image.

    The set comes from the Dockerfiles, so the next package that reaches both
    images cannot be forgotten the same way.
    """
    probe = f"{entry}/module.py" if (REPO_ROOT / entry).is_dir() else entry
    assert _runs_the_image_jobs(probe), (
        f"{entry} is COPYed into both images but a change to it builds neither"
    )


def test_a_failed_run_leaves_nothing_behind() -> None:
    """On every persistent runner, whatever happened; a hosted VM is deleted whole."""
    cleanup = step("Drop this run's")
    assert cleanup["if"] == "always() && runner.environment != 'github-hosted'"
    run = str(cleanup["run"])
    assert run.index("docker rm") < run.index("docker image rm"), (
        "containers first: an image with a container still referencing it is not removed, "
        "even with -f, so the other order silently leaves both"
    )
    assert 'label=io.hybridinference.smoke.run=${SUFFIX}"' in run, (
        "cleanup must be scoped to this run: a shared runner hosts two jobs at once, and "
        "`--filter name=smoke-` reaches into the other one — which is how the control cell "
        "lost its application container mid-poll"
    )
    assert "docker image rm -f" not in run, (
        "not forced: two runs of the same commit build the same image id under different "
        "tags on a shared runner, and a forced removal takes their containers with it. That "
        "is how the since-removed egress cell's container disappeared between `docker run` "
        "and its first "
        "`docker inspect`."
    )


def test_the_gate_waits_for_the_image_jobs() -> None:
    gate = workflow()["jobs"]["ci-gate"]
    assert "artifacts" in gate["needs"]
    assert "build" not in gate["needs"], "the old build-only job is gone"
    results = str(step("Every job succeeded", job="ci-gate")["env"]["RESULTS"])
    assert "artifacts=" in results


def test_the_job_logs_in_to_a_config_of_its_own() -> None:
    """The login's post step logs out of $DOCKER_CONFIG, and $HOME is shared.

    Without a directory of its own, every finished cell logged the runner
    user out of Docker Hub — the persistent login the service containers and
    `docker pull` steps on this machine rely on.
    """
    naming = str(step("Name this cell")["run"])
    assert 'echo "DOCKER_CONFIG=${RUNNER_TEMP}/docker-config" >> "${GITHUB_ENV}"' in naming
    names = [str(s.get("name", "")) for s in workflow()["jobs"]["artifacts"]["steps"]]
    assert names.index("Name this cell's artifacts") < names.index("Log in to Docker Hub")
