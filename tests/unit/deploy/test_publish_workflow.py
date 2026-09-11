"""The publication workflow: what may reach it, and what it must not skip.

None of this shows in CI, because CI is what it governs. A `pull_request`
trigger pasted in, a cell dropped from the matrix, a smoke pointed at the
builder's image instead of the registry's, a publish job given `if: always()`
— each is one edit, each passes every other test, and each turns a release
into something a distribution should not trust. So they are pinned here, where
a change to the workflow has to change a test.
"""

from __future__ import annotations

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
    """No `pull_request`, no `push`: a run is CI finishing on dev, or a hand.

    `workflow_run` executes the copy of this file on the default branch, so a
    pull request cannot even edit its way in.
    """
    assert set(triggers()) == {"workflow_run", "workflow_dispatch"}
    assert triggers()["workflow_run"] == {
        "workflows": ["CI"],
        "branches": ["dev"],
        "types": ["completed"],
    }
    assert workflow()["concurrency"]["cancel-in-progress"] is False


def test_the_gate_admits_only_ci_finishing_green_on_a_push_to_dev() -> None:
    condition = job("commit")["if"]
    for fragment in (
        "github.event_name == 'workflow_dispatch'",
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

    script = str(step("Require the commit's own CI", "commit")["with"]["script"])
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
    assert job("artifacts")["if"] == "needs.commit.outputs.existing == ''"
    assert job("publish")["if"] == "needs.commit.outputs.existing == ''"
    assert job("artifacts")["needs"] == "commit"
    assert job("publish")["needs"] == ["commit", "artifacts"]


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
    for job_name in ("artifacts", "publish"):
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


# ------------------------------------------------------------------- publish


def test_write_permissions_sit_only_where_writing_happens() -> None:
    assert workflow()["permissions"] == {"contents": "read"}
    assert job("commit")["permissions"] == {"contents": "read", "actions": "read"}
    assert job("artifacts")["permissions"] == {"contents": "read", "packages": "write"}
    assert job("publish")["permissions"] == {"contents": "write"}


def test_the_publish_job_has_no_escape_hatch() -> None:
    assert "always()" not in str(job("publish").get("if", ""))
    for candidate in steps("publish"):
        assert not candidate.get("continue-on-error"), candidate.get("name")
        assert "always()" not in str(candidate.get("if", "")), candidate.get("name")
    collect = step("Collect what the cells published", "publish")["with"]
    assert collect == {"pattern": "release-cell-*", "merge-multiple": True, "path": "cells"}


def test_the_manifest_names_the_run_and_the_recipe_asset() -> None:
    package = str(step("Package the recipe", "publish")["run"])
    assert "build_recipe.py --output release/recipe.tar.gz" in package
    assert "RECIPE_SHA256=$(sha256sum release/recipe.tar.gz" in package

    assemble = str(step("Assemble the manifest", "publish")["run"])
    assert "publish_inputs assemble" in assemble and "--cells cells" in assemble

    manifest = str(step("Write the manifest", "publish")["run"])
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
    assert "MANIFEST_SHA256=$(sha256sum release/release.json" in manifest
    assert (
        position("Package the recipe", "publish")
        < position("Assemble the manifest", "publish")
        < position("Write the manifest", "publish")
        < position("Write the release notes", "publish")
        < position("Publish the release", "publish")
    )


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
