"""CI runs on machines this organisation owns, while this repository is private.

GitHub-hosted minutes are billed for a private repository, and every pull
request here comes from somebody who already has push access to the code, so
routing pull requests to hosted runners spends quota to remove a risk that does
not exist yet. Publishing the repository inverts that: a fork's pull request
then executes code nobody has vetted — tests, package install hooks, Docker
builds — and a self-hosted runner is a machine of ours.

The routing for that day is prepared as its own pull request and merges when
the repository becomes public. Until then this test is what fails if it lands
early, or if one job drifts onto a hosted runner by hand: a run that quietly
picks `ubuntu-latest` costs money and looks exactly like a run that did not.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"

HOSTED_PREFIXES = ("ubuntu-", "windows-", "macos-")


def _document() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _runner_text(job: dict) -> str:
    """Every runner label a job can reach, including through its matrix."""
    runs_on = job["runs-on"]
    parts = list(runs_on) if isinstance(runs_on, list) else [str(runs_on)]
    for cells in job.get("strategy", {}).get("matrix", {}).values():
        if not isinstance(cells, list):
            continue
        for cell in cells:
            if isinstance(cell, dict):
                parts.extend(str(value) for value in cell.values())
    return " ".join(parts)


def test_no_job_can_spend_github_hosted_minutes() -> None:
    offenders = []
    for name, job in _document()["jobs"].items():
        text = _runner_text(job)
        if any(prefix in text for prefix in HOSTED_PREFIXES):
            offenders.append(name)

    assert not offenders, (
        "these jobs bill GitHub-hosted minutes against the organisation while "
        f"the repository is private: {offenders}"
    )


def test_every_job_names_a_pool_this_organisation_owns() -> None:
    offenders = [
        name for name, job in _document()["jobs"].items() if "self-hosted" not in _runner_text(job)
    ]

    assert not offenders, f"these jobs name no self-hosted pool: {offenders}"


def test_real_kata_probe_is_unreachable_from_pull_requests() -> None:
    """One job runs a real sandbox, and only a reviewed dev commit may reach it."""
    kata = _document()["jobs"]["kata"]
    assert "github.event_name != 'pull_request'" in str(kata.get("if", ""))
    assert "github.ref == 'refs/heads/dev'" in kata["if"]
    assert kata["runs-on"] == ["self-hosted", "Linux", "X64", "image-verify-x64"]
