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

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from deploy.release import release_manifest as rm, smoke

REPO_ROOT = Path(__file__).resolve().parents[3]
CI = REPO_ROOT / ".github/workflows/ci.yml"
CONSOLE_DOCKERFILE = REPO_ROOT / "deploy/control-plane/Dockerfile"
RUNNER_DOCKERFILE = REPO_ROOT / "deploy/host/Dockerfile.agent-runner"


def workflow() -> dict[str, Any]:
    return yaml.safe_load(CI.read_text())


def cells() -> list[dict[str, Any]]:
    return workflow()["jobs"]["artifacts"]["strategy"]["matrix"]["artifact"]


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


def test_the_matrix_covers_the_roles_the_release_names() -> None:
    role_args = [cell["smoke_args"] for cell in cells()]
    roles = {arg.split()[1] for arg in role_args}
    assert roles == set(rm.EXPECTED_PLATFORMS)


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
    for fragment in (
        "Build ${{ matrix.artifact.name }}",
        "Build ${{ matrix.artifact.name }} (retry)",
    ):
        build = step(fragment)
        assert build["with"]["load"] is True, (
            f"{fragment} must load the image onto this runner's daemon; the next step starts it "
            "from there, and a self-hosted runner shares no filesystem with the job that built it"
        )
        assert build["with"]["push"] is False


def test_the_build_does_not_export_an_unused_remote_cache() -> None:
    """The persistent per-runner builder is the cache used by later builds.

    A GHA exporter without an importer uploaded the sandbox's large runtime
    layers on every run but no build ever consumed them. The export alone took
    minutes and occupied every x64 runner on their shared physical host.
    """
    for fragment in (
        "Build ${{ matrix.artifact.name }}",
        "Build ${{ matrix.artifact.name }} (retry)",
    ):
        build = step(fragment)
        assert "cache-to" not in build["with"]
        assert "cache-from" not in build["with"]


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


def test_cleanup_cannot_reach_another_cell() -> None:
    """Seven cells of one run share a run id, an attempt — and often a Docker host.

    A label built from the first two is identical for all of them, so the first
    cell to finish removes the others' containers while they are still being
    tested. That is what happened to the control and egress cells, twice, before
    the artifact name went into the label.
    """
    naming = str(step("Name this cell")["run"])
    assert "matrix.artifact.name" in naming, (
        "the label has to differ per cell, or cleanup reaches into the other six"
    )
    labels = {f"345-1-{cell['name']}" for cell in cells()}
    assert len(labels) == len(cells())
    assert "label=io.hybridinference.smoke.run=${SUFFIX}" in str(step("Drop this run's")["run"])


def test_the_build_passes_the_commit_every_image_must_name() -> None:
    build = step("Build ${{ matrix.artifact.name }}")
    assert "SOURCE_COMMIT=${{ github.sha }}" in str(build["with"]["build-args"])


def _docker_patterns() -> list[str]:
    """The regexes the classifier's docker decision is actually made of.

    Read out of the `changes` job rather than restated here: a test that keeps
    its own copy of the pattern stops testing the pattern the moment either one
    is edited.
    """
    classify = str(step("Classify changed paths", job="changes")["run"])
    block = classify.split("# Docker:", 1)[1].split("# A change to CI itself", 1)[0]
    return re.findall(r"matches '([^']+)'", block)


def _runs_the_image_jobs(path: str) -> bool:
    return any(re.search(pattern, path) for pattern in _docker_patterns())


def _copied_paths(dockerfile: Path) -> set[str]:
    """Repository paths one Dockerfile COPYs out of the build context.

    The full relative source, not its first component: the layouts this has to
    survive put the shared packages at the root today and under a common parent
    after the R5 move, and only the full path keeps the check meaningful in both.
    """
    sources = set()
    for line in dockerfile.read_text().splitlines():
        if not line.startswith("COPY") or "--from=" in line:
            continue
        source = line.split()[1]
        if source.startswith("/"):  # a stage or an absolute path, not the context
            continue
        sources.add(source.rstrip("/"))
    return sources


def _packages_both_images_ship() -> set[str]:
    """What the console and the runner images both COPY, read from the files."""
    return _copied_paths(CONSOLE_DOCKERFILE) & _copied_paths(RUNNER_DOCKERFILE)


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
    cleanup = step("Drop this run's")
    assert cleanup["if"] == "always()"
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
        "is how the egress cell's container disappeared between `docker run` and its first "
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
