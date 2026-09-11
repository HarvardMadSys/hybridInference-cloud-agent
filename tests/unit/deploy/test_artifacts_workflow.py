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

from pathlib import Path
from typing import Any

import yaml

from deploy.release import release_manifest as rm, smoke

REPO_ROOT = Path(__file__).resolve().parents[3]
CI = REPO_ROOT / ".github/workflows/ci.yml"


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


def test_a_change_to_the_console_runs_the_image_jobs() -> None:
    """web/ is half of the console image, and used to skip every docker job.

    Dockerfile.web copies `web/`, so a pull request that only touched the app
    changed what that image contains and ran none of the jobs that build it —
    which is how the image went unverified in the first place.
    """
    classify = str(step("Classify changed paths", job="changes")["run"])
    assert "cloud_agent_host/|web/" in classify


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
