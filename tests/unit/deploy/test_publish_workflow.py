"""The publication workflow: what may reach it, and what it must not skip.

None of this shows in CI, because CI is what it governs. A `pull_request`
trigger pasted in, a cell dropped from the matrix, a smoke pointed at the
builder's image instead of the registry's, a publish job given `if: always()`
— each is one edit, each passes every other test, and each turns a release
into something a distribution should not trust. So they are pinned here, where
a change to the workflow has to change a test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import yaml
from deploy.release import publish_inputs as pi, release_manifest as rm

REPO_ROOT = Path(__file__).resolve().parents[3]
PUBLISH = REPO_ROOT / ".github/workflows/publish.yml"
CI = REPO_ROOT / ".github/workflows/ci.yml"


def workflow() -> dict[str, Any]:
    return yaml.safe_load(PUBLISH.read_text())


def triggers() -> dict[str, Any]:
    document = workflow()
    # YAML reads a bare `on` as the boolean true.
    return document.get("on") or document[True]


def job(name: str) -> dict[str, Any]:
    return workflow()["jobs"][name]


def steps(job_name: str) -> list[dict[str, Any]]:
    return job(job_name)["steps"]


def step(fragment: str, job_name: str = "artifacts") -> dict[str, Any]:
    for candidate in steps(job_name):
        if fragment in str(candidate.get("name", "")):
            return candidate
    raise AssertionError(f"no step in {job_name} matches {fragment!r}")


def position(fragment: str, job_name: str = "artifacts") -> int:
    for index, candidate in enumerate(steps(job_name)):
        if fragment in str(candidate.get("name", "")):
            return index
    raise AssertionError(f"no step in {job_name} matches {fragment!r}")


def live_lines(script: str) -> list[str]:
    return [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# ---------------------------------------------------------------- reachability


def test_publication_is_never_reachable_from_a_pull_request() -> None:
    """No `pull_request`: a run is a push to dev, CI finishing on dev, or a hand.

    A `push` fires only for commits on dev, and `workflow_run` executes the copy
    of this file on the default branch, which is dev: either runs merged code,
    so a pull request cannot even edit its way in.
    """
    assert set(triggers()) == {"push", "workflow_run", "workflow_dispatch"}
    assert triggers()["push"] == {"branches": ["dev"]}
    assert triggers()["workflow_run"] == {
        "workflows": ["CI"],
        "branches": ["dev"],
        "types": ["completed"],
    }
    assert workflow()["concurrency"]["cancel-in-progress"] is False


def test_the_gate_admits_a_push_to_dev_or_ci_finishing_green_on_one() -> None:
    condition = job("commit")["if"]
    for fragment in (
        "github.event_name == 'workflow_dispatch'",
        "(github.event_name == 'push' && github.ref == 'refs/heads/dev')",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'dev'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
        "github.event.workflow_run.conclusion == 'success'",
    ):
        assert fragment in condition, fragment


def test_a_named_commit_must_be_on_dev_with_its_own_green_ci() -> None:
    """A dispatch can name anything. The job proves what the event would have said."""
    resolve = live_lines(str(step("Resolve the commit", "commit")["run"]))
    assert any(line.startswith("if ! git merge-base --is-ancestor") for line in resolve)
    assert "exit 1" in resolve
    assert 'echo "tag=agent-${sha}" >> "${GITHUB_OUTPUT}"' in resolve
    checkout = steps("commit")[0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["ref"] == "dev", (
        "the ancestry check is against dev, whatever was dispatched"
    )

    required = step("Require the commit's own CI", "commit")
    # A push's CI is still running when this job does; the `ci` job waits for
    # it before anything is published.
    assert required["if"] == "github.event_name != 'push'"
    script = str(required["with"]["script"])
    for fragment in (
        "listWorkflowRuns",
        "workflow_id: 'ci.yml'",
        "head_sha: sha",
        "event: 'push'",
        "status: 'success'",
        "core.setFailed",
    ):
        assert fragment in script, fragment


def test_a_published_release_is_never_rebuilt_or_rewritten() -> None:
    script = str(step("Refuse to overwrite", "commit")["with"]["script"])
    assert "getReleaseByTag" in script
    assert "core.setOutput('existing', 'true')" in script
    assert "error.status !== 404" in script
    for name in ("artifacts", "acceptance", "ci"):
        assert job(name)["if"] == "needs.commit.outputs.existing == ''", name
    assert job("publish")["if"] == (
        "needs.commit.outputs.existing == '' && needs.ci.outputs.green == 'true'"
    )
    assert job("artifacts")["needs"] == "commit"
    assert job("acceptance")["needs"] == ["commit", "artifacts"]
    assert job("ci")["needs"] == ["commit", "acceptance"]
    assert job("publish")["needs"] == ["commit", "acceptance", "ci"]


def test_nothing_is_published_until_ci_passed_this_exact_commit() -> None:
    """A push builds alongside CI; only CI's own verdict on it releases it.

    The wait reads CI's newest push run for this commit and says green only
    when it completed with success: a failed run, or one cancelled by the next
    push, leaves `publish` skipped.
    """
    ci = job("ci")
    assert ci["permissions"] == {"actions": "read"}
    assert ci["outputs"] == {"green": "${{ steps.verdict.outputs.green }}"}
    wait = step("Wait for CI to conclude", "ci")
    assert wait["id"] == "verdict"
    assert wait["with"]["script"]
    script = live_lines(str(wait["with"]["script"]))
    joined = "\n".join(script)
    for fragment in (
        "listWorkflowRuns",
        "workflow_id: 'ci.yml'",
        "head_sha: sha,",
        "event: 'push',",
        "run.status === 'completed'",
        "const green = run.conclusion === 'success';",
        "core.setOutput('green', green ? 'true' : 'false');",
        "core.setFailed(",
    ):
        assert fragment in joined, fragment
    assert "status: 'success'" not in joined, "the wait must see a run that is still going"
    assert wait["env"]["SHA"] == "${{ needs.commit.outputs.sha }}"


# --------------------------------------------------------------------- matrix


def test_the_matrix_is_the_one_ci_verifies() -> None:
    """Same cells, same pools, same build arguments, or the release is not what CI checked."""
    mine = job("artifacts")["strategy"]["matrix"]["artifact"]
    theirs = yaml.safe_load(CI.read_text())["jobs"]["artifacts"]["strategy"]["matrix"]["artifact"]
    assert sorted(mine, key=lambda cell: cell["name"]) == sorted(
        theirs, key=lambda cell: cell["name"]
    )
    assert job("artifacts")["strategy"]["fail-fast"] is False


def test_the_cells_are_the_ones_the_collector_assembles() -> None:
    names = {cell["name"] for cell in job("artifacts")["strategy"]["matrix"]["artifact"]}
    assert names == set(pi.expected_cells())


def test_every_checkout_pins_the_vetted_commit() -> None:
    for job_name in ("artifacts", "acceptance", "publish"):
        checkouts = [
            s for s in steps(job_name) if str(s.get("uses", "")).startswith("actions/checkout@")
        ]
        assert checkouts, job_name
        for checkout in checkouts:
            assert checkout["with"]["ref"] == "${{ needs.commit.outputs.sha }}", job_name


# ---------------------------------------------------------------------- cells


def test_every_cell_pushes_by_digest_under_the_registry_prefix() -> None:
    for fragment in (
        "Build ${{ matrix.artifact.name }} and push it by digest",
        "Build ${{ matrix.artifact.name }} and push it by digest (retry)",
    ):
        build = step(fragment)
        outputs = str(build["with"]["outputs"])
        for option in ("push-by-digest=true", "name-canonical=true", "push=true"):
            assert option in outputs, f"{fragment}: {option}"
        assert "name=${{ env.PUBLISH_REPOSITORY }}" in outputs
        assert "tags" not in build["with"], "a tag can be moved; the manifest names digests"
        assert build["with"].get("load") is not True
        assert build["with"]["platforms"] == "linux/${{ matrix.artifact.arch }}"
        assert "SOURCE_COMMIT=${{ env.SHA }}" in str(build["with"]["build-args"])

    for key in ("IMAGE_REPOSITORY", "SANDBOX_REPOSITORY"):
        assert workflow()["env"][key].startswith(rm.REGISTRY_PREFIX), key
    naming = str(step("Name this cell")["run"])
    assert 'if [ "${{ matrix.artifact.name }}" = "sandbox" ]' in naming
    assert "PUBLISH_REPOSITORY=${SANDBOX_REPOSITORY}" in naming
    assert "PUBLISH_REPOSITORY=${IMAGE_REPOSITORY}" in naming
    assert "SHA=${{ needs.commit.outputs.sha }}" in naming


def test_the_retry_is_the_only_step_allowed_to_fail_quietly() -> None:
    quiet = [s["name"] for s in steps("artifacts") if s.get("continue-on-error")]
    assert quiet == ["Build ${{ matrix.artifact.name }} and push it by digest"]
    retry = step("(retry)")
    assert retry["if"] == "steps.push.outcome == 'failure'"
    assert "continue-on-error" not in retry
    digest = str(step("Record the digest")["run"])
    assert "FIRST:-${RETRY:-}" in digest
    assert "sha256:[0-9a-f]{64}" in digest and "exit 1" in digest


def test_the_smoke_runs_on_the_bytes_the_registry_holds() -> None:
    """Push, pull the digest back, start that. Not the builder's copy."""
    assert (
        position("Record the digest")
        < position("Pull the pushed digest")
        < position("Start it")
        < position("Record what this cell published")
        < position("Tag the digest")
        < position("Hand the record")
    )
    pull = str(step("Pull the pushed digest")["run"])
    assert 'docker pull "${IMAGE_REF}"' in pull
    assert 'docker tag "${IMAGE_REF}" "${IMAGE_TAG}"' in pull
    smoke = str(step("Start it")["run"])
    assert "deploy/release/smoke.py" in smoke
    assert '--image "${IMAGE_TAG}"' in smoke
    assert '--revision "${SHA}"' in smoke
    assert "${{ matrix.artifact.smoke_args }}" in smoke


def test_the_record_describes_the_pulled_digest() -> None:
    run = str(step("Record what this cell published")["run"])
    assert "docker image inspect --format '{{json .Config.Labels}}' \"${IMAGE_REF}\"" in run
    assert "publish_inputs record" in run
    assert '--image "${IMAGE_REF}"' in run
    assert '--source-commit "${SHA}"' in run
    assert '--platform "linux/${{ matrix.artifact.arch }}"' in run
    assert '--output "cells/${{ matrix.artifact.name }}.json"' in run
    upload = step("Hand the record")["with"]
    assert upload["name"] == "release-cell-${{ matrix.artifact.name }}"
    assert upload["path"] == "cells/${{ matrix.artifact.name }}.json"
    assert upload["if-no-files-found"] == "error"


def test_the_human_tag_names_the_cell_and_the_commit() -> None:
    run = str(step("Tag the digest")["run"])
    assert (
        'imagetools create --tag "${PUBLISH_REPOSITORY}:${{ matrix.artifact.name }}-${SHA}" "${IMAGE_REF}"'
        in run
    )


def test_cleanup_is_scoped_to_this_cell_and_always_runs() -> None:
    cleanup = step("Drop this run's")
    assert cleanup["if"] == "always()"
    run = str(cleanup["run"])
    assert 'label=io.hybridinference.smoke.run=${SUFFIX}"' in run
    assert run.index("docker rm") < run.index("docker image rm")
    assert "docker image rm -f" not in run
    assert 'docker image rm "${IMAGE_REF}"' in run
    naming = str(step("Name this cell")["run"])
    assert (
        "GITHUB_RUN_ID" in naming
        and "GITHUB_RUN_ATTEMPT" in naming
        and "matrix.artifact.name" in naming
    )


_RECORDING_DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_CALLS"
if [ "$1" = ps ]; then printf '%s\n' $EXISTING_CONTAINERS; fi
exit 0
"""


def run_builder_step(tmp_path: Path, existing: list[str]) -> tuple[dict[str, str], list[str]]:
    """Execute the cell's builder step against a recording `docker`.

    No daemon or registry is contacted. Returns what the step exported to
    later steps and every docker invocation it made.
    """
    script = str(step("Make this job's own builder")["run"]).replace(
        "${{ matrix.artifact.name }}", "sandbox"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(_RECORDING_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    calls = tmp_path / "docker-calls"
    github_env = tmp_path / "github-env"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_CALLS": str(calls),
        "EXISTING_CONTAINERS": " ".join(existing),
        "GITHUB_REPOSITORY": "HarvardMadSys/hybridInference-cloud-agent",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_ENV": str(github_env),
        "RUNNER_TEMP": str(tmp_path / "job-temp"),
        "RUNNER_WORKSPACE": str(tmp_path / "persistent-runner" / "work"),
        "RUNNER_NAME": "image-verify-x64-1",
    }
    subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, check=True)
    outputs = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    return outputs, calls.read_text().splitlines()


def test_each_cell_builds_through_a_fresh_builder_no_other_job_used(tmp_path: Path) -> None:
    """A release must not consume layer or cache-mount state another job left.

    CI's trusted cells keep a persistent `ci-<runner>` builder under `_work`
    on these same runners, and that state outlives whoever wrote it. The
    publisher therefore creates its own builder, configured under this job's
    RUNNER_TEMP, and never reuses or self-heals into an existing one.
    """
    outputs, calls = run_builder_step(tmp_path, existing=[])
    builder = "publish-harvardmadsys-hybridinference-cloud-agent-image-verify-x64-1-123-2-sandbox"
    assert outputs["BUILDER"] == builder
    assert outputs["BUILDX_CONFIG"] == str(tmp_path / "job-temp" / "buildx")
    assert not (tmp_path / "persistent-runner").exists(), "touched the runner's _work state"
    assert f"buildx create --name {builder} --driver docker-container --use" in calls
    assert f"buildx inspect {builder} --bootstrap" in calls
    assert not any("ci-" in call or call.startswith("buildx use") for call in calls), calls


def test_a_cell_sweeps_only_its_own_runners_orphaned_publish_builders(tmp_path: Path) -> None:
    prefix = "buildx_buildkit_publish-harvardmadsys-hybridinference-cloud-agent-image-verify-x64-1-"
    orphan = f"{prefix}99-1-web-root0"
    kept = [
        "buildx_buildkit_ci-image-verify-x64-10",
        "buildx_buildkit_publish-harvardmadsys-hybridinference-cloud-agent-image-verify-x64-2-7-1-tunnel0",
        f"{prefix}b-7-1-tunnel0",
    ]
    _, calls = run_builder_step(tmp_path, existing=[orphan, *kept])
    removed = {call.split()[-1] for call in calls if call.startswith(("rm ", "volume rm "))}
    assert removed == {orphan, f"{orphan}_state"}


def test_the_cells_builder_is_removed_whatever_happened() -> None:
    """Its state volume goes with it, so the next job cannot read what it built."""
    cleanup = step("Drop this job's builder")
    assert cleanup["if"] == "always()"
    assert 'docker buildx rm "${BUILDER}"' in str(cleanup["run"])
    assert "--keep-state" not in str(cleanup["run"])
    assert steps("artifacts")[-1] == cleanup
    assert position("Make this job's own builder") < position(
        "Build ${{ matrix.artifact.name }} and push it by digest"
    )


# ------------------------------------------------------------------- publish


def test_write_permissions_sit_only_where_writing_happens() -> None:
    assert workflow()["permissions"] == {"contents": "read"}
    assert job("commit")["permissions"] == {"contents": "read", "actions": "read"}
    assert job("artifacts")["permissions"] == {"contents": "read", "packages": "write"}
    assert job("ci")["permissions"] == {"actions": "read"}
    assert job("publish")["permissions"] == {"contents": "write"}


def test_the_publish_job_has_no_escape_hatch() -> None:
    assert "always()" not in str(job("publish").get("if", ""))
    for candidate in steps("publish"):
        assert not candidate.get("continue-on-error"), candidate.get("name")
        assert "always()" not in str(candidate.get("if", "")), candidate.get("name")
    collect = step("Collect the documents", "publish")["with"]
    assert collect == {"name": "release-documents", "path": "release"}


def test_the_manifest_names_the_run_and_the_recipe_asset() -> None:
    """Generated in the acceptance job, because that is the job that deploys it."""
    package = str(step("Package the recipe", "acceptance")["run"])
    assert "build_recipe.py --output release/recipe.tar.gz" in package
    assert "RECIPE_SHA256=$(sha256sum release/recipe.tar.gz" in package

    assemble = str(step("Assemble the manifest", "acceptance")["run"])
    assert "publish_inputs assemble" in assemble and "--cells cells" in assemble

    manifest = str(step("Write the manifest", "acceptance")["run"])
    assert "deploy/release/release_manifest.py" in manifest
    assert (
        '--recipe-url "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/releases/download/${TAG}/recipe.tar.gz"'
        in manifest
    )
    assert '--recipe-sha256 "${RECIPE_SHA256}"' in manifest
    assert (
        '--publisher-run-url "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"'
        in manifest
    )
    assert (
        position("Package the recipe", "acceptance")
        < position("Assemble the manifest", "acceptance")
        < position("Write the manifest", "acceptance")
        < position("Deploy it", "acceptance")
        < position("Hand the documents", "acceptance")
    )
    summary = str(step("Record what a distribution will pin", "publish")["run"])
    assert "MANIFEST_SHA256=$(sha256sum release/release.json" in summary
    assert (
        position("Record what a distribution will pin", "publish")
        < position("Write the release notes", "publish")
        < position("Publish the release", "publish")
    )


# ------------------------------------------------------------------ acceptance


def test_a_release_is_deployed_before_it_is_published() -> None:
    """The order that makes "published" mean "installs"."""
    deploy = str(step("Deploy it", "acceptance")["run"])
    assert "deploy/release/acceptance.py" in deploy
    assert "--manifest release/release.json" in deploy
    assert "--recipe release/recipe.tar.gz" in deploy
    assert "--variant agents" in deploy
    assert '--project "${PROJECT}"' in deploy
    assert '--workdir "${WORKDIR}"' in deploy


def test_the_documents_are_generated_once_and_carried_forward() -> None:
    """Two copies of a manifest is one sha256 over bytes nobody deployed."""
    hand = step("Hand the documents", "acceptance")["with"]
    assert hand["name"] == "release-documents"
    assert "release/recipe.tar.gz" in hand["path"] and "release/release.json" in hand["path"]
    assert hand["if-no-files-found"] == "error"

    published = [str(s.get("run", "")) for s in steps("publish")]
    assert not any("release_manifest.py" in run for run in published), (
        "publish must attach the documents the acceptance deployed, not write new ones"
    )
    assert not any("build_recipe.py" in run for run in published)


def test_the_acceptance_may_pull_the_release_but_never_write_one() -> None:
    assert job("acceptance")["permissions"] == {"contents": "read", "packages": "read"}
    registries = [
        s["with"].get("registry", "docker.io")
        for s in steps("acceptance")
        if str(s.get("uses", "")).startswith("docker/login-action")
    ]
    assert sorted(registries) == ["docker.io", "ghcr.io"], (
        "the stack pulls Postgres and nginx from Docker Hub and the release's own images from GHCR"
    )
    assert "ARM64" in str(job("acceptance")["runs-on"]), (
        "the acceptance starts the control and web images, which are built for arm64"
    )


def test_the_acceptance_cleans_up_whatever_happened() -> None:
    cleanup = step("Drop this acceptance's stack", "acceptance")
    assert cleanup["if"] == "always()"
    run = str(cleanup["run"])
    assert "docker rm -f" in run and "docker volume rm" in run
    assert "${PROJECT}-refusal" in run, "the negative case gets a project of its own, and a cleanup"
    naming = str(step("Name this acceptance", "acceptance")["run"])
    assert "GITHUB_RUN_ID" in naming and "GITHUB_RUN_ATTEMPT" in naming
    assert 'echo "DOCKER_CONFIG=${RUNNER_TEMP}/docker-config" >> "${GITHUB_ENV}"' in naming


def test_a_release_becomes_visible_only_with_both_assets_on_it() -> None:
    script = str(step("Publish the release", "publish")["with"]["script"])
    assert "const assets = ['recipe.tar.gz', 'release.json'];" in script
    assert (
        script.index("draft: true")
        < script.index("uploadReleaseAsset")
        < script.index("draft: false")
    )
    assert "target_commitish: sha" in script
    assert "tag_name: tag" in script
    assert "make_latest: 'false'" in script
    assert "uploaded.data.state !== 'uploaded'" in script
    assert "deleteRelease" in script, (
        "a draft that could not be completed is deleted, not left half-made"
    )
    summary = str(step("What a distribution pins", "publish")["run"])
    assert "${MANIFEST_SHA256}" in summary and "GITHUB_STEP_SUMMARY" in summary


def test_each_cell_logs_in_to_a_config_of_its_own() -> None:
    """Runners on one host share $HOME, and login-action's post step logs out of it.

    First live run, 2026-09-11: web-root's logout at 14:44:16 removed the
    credentials control was tagging with three seconds later, and the sandbox
    push arrived after the runner cell had logged the shared file out. A
    config directory per job makes the logout reach nobody else.
    """
    naming = str(step("Name this cell")["run"])
    assert 'echo "DOCKER_CONFIG=${RUNNER_TEMP}/docker-config" >> "${GITHUB_ENV}"' in naming
    assert (
        position("Name this cell") < position("Log in to Docker Hub") < position("Log in to GHCR")
    )
