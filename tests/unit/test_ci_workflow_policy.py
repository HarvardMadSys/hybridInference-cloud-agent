"""Untrusted pull requests never execute on repository-owned machines."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"


def _document() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _excludes_pull_requests(spec: dict) -> bool:
    condition = str(spec.get("if", ""))
    return "github.event_name != 'pull_request'" in condition


def test_every_pull_request_reachable_job_selects_a_hosted_runner() -> None:
    offenders = []
    for name, spec in _document()["jobs"].items():
        if _excludes_pull_requests(spec):
            continue
        runs_on = spec["runs-on"]
        if not (
            isinstance(runs_on, str)
            and "github.event_name == 'pull_request'" in runs_on
            and ("ubuntu-" in runs_on or "matrix.artifact.pull_request_runner" in runs_on)
            and "fromJSON(" in runs_on
        ):
            offenders.append(name)

    assert not offenders, (
        f"untrusted pull_request code could reach a self-hosted runner in these jobs: {offenders}"
    )


def test_artifact_matrix_preserves_native_architecture_on_hosted_runners() -> None:
    artifacts = _document()["jobs"]["artifacts"]
    assert "matrix.artifact.pull_request_runner" in artifacts["runs-on"]

    for entry in artifacts["strategy"]["matrix"]["artifact"]:
        expected = "ubuntu-24.04-arm" if entry["arch"] == "arm64" else "ubuntu-latest"
        assert entry["pull_request_runner"] == expected
        assert "self-hosted" in entry["runner"]


def test_hosted_artifact_builders_use_a_valid_job_local_name() -> None:
    artifacts = _document()["jobs"]["artifacts"]
    setup = next(
        step
        for step in artifacts["steps"]
        if step.get("name") == "Reuse this runner's builder, or make one"
    )
    script = setup["run"]
    assert '[ "${GITHUB_EVENT_NAME}" = "pull_request" ]' in script
    assert 'buildx_config="${RUNNER_TEMP}/buildx"' in script
    assert 'builder="ci-${SUFFIX}"' in script
    assert 'builder="ci-${RUNNER_NAME}"' in script


def test_real_kata_probe_is_unreachable_from_pull_requests() -> None:
    kata = _document()["jobs"]["kata"]
    assert _excludes_pull_requests(kata)
    assert "github.ref == 'refs/heads/dev'" in kata["if"]
    assert kata["runs-on"] == ["self-hosted", "Linux", "X64", "image-verify-x64"]


def test_pull_request_steps_do_not_require_repository_secrets() -> None:
    offenders = []
    for job_name, job in _document()["jobs"].items():
        if _excludes_pull_requests(job):
            continue
        for step in job.get("steps", []):
            if "secrets." not in str(step):
                continue
            if "github.event_name != 'pull_request'" not in str(step.get("if", "")):
                offenders.append(f"{job_name}:{step.get('name') or step.get('uses')}")

    assert not offenders, f"fork pull requests require unavailable secrets: {offenders}"
