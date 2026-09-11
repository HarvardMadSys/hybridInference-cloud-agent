"""The fuse that disarms direct deployment, one switch per environment.

Merging to `dev` deploys FreeInference's staging hosts today, and a dispatch
deploys production. That authority is what the distribution split moves to the
FreeInference repository, and the point at which it is given up has to be one
variable per environment rather than a workflow edit made under pressure.

Three properties, none of which a reviewer can confirm by reading the diff once:

* **Every job.** Each workflow has two jobs on two machines, and each one
  replaces something a user can reach. This walks whatever jobs the file has
  rather than the two that exist today: a third job added later would otherwise
  reach a host with no gate at all.
* **Fail-closed.** Only `true` deploys: `false`, `yes`, `1`, an empty value, a
  deleted variable and an unset variable all skip the jobs. A fuse that fires
  when it cannot read itself is not a fuse, and the opposite choice — deploying
  because the switch could not be read — fails in the direction this exists to
  prevent. GitHub's `==` *ignores case* when comparing strings, so `True` and
  `TRUE` deploy as well; that half is an expression-language semantic rather
  than something this file can enforce, which is why
  `test_the_documented_switch_matches_github_semantics` pins it where a person
  reads it.
* **One environment per switch.** Staging's variable must not appear in
  production's workflow. A single value that disarmed both would take the
  rehearsed rollback path with it.

The fuse is a migration artifact. It is deleted with the workflows themselves
once both environments are deployed by FreeInference from its own lock; until
then production's disarmed workflow *is* the rollback path, which is why it is
gated rather than removed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = REPO_ROOT / ".github" / "workflows"
_CD_DOC = REPO_ROOT / "deploy" / "control-plane" / "README-cd.md"

#: Each direct-deploy workflow, and the variable that disarms it.
FUSES = {
    "deploy-staging.yml": "LEGACY_DIRECT_STAGING_DEPLOY",
    "deploy-prod.yml": "LEGACY_DIRECT_PRODUCTION_DEPLOY",
}

_CASES = sorted(FUSES.items())


def _jobs(workflow: str) -> dict[str, dict]:
    document = yaml.safe_load((_WORKFLOWS / workflow).read_text())
    return document.get("jobs") or {}


def _condition(workflow: str, job: str) -> str:
    """A job's `if`, with folded-scalar line breaks collapsed."""
    return re.sub(r"\s+", " ", _jobs(workflow)[job].get("if") or "")


@pytest.mark.parametrize(("workflow", "variable"), _CASES)
def test_every_job_is_behind_the_fuse(workflow: str, variable: str) -> None:
    jobs = _jobs(workflow)
    assert jobs, f"{workflow} declares no jobs, so this check would pass vacuously"

    for job in jobs:
        condition = _condition(workflow, job)
        assert condition, (
            f"{workflow}: job `{job}` has no `if`, so nothing stops it deploying "
            f"when {variable} is false"
        )
        assert f"vars.{variable} == 'true'" in condition, (
            f"{workflow}: job `{job}` does not require {variable} to be exactly "
            f"'true': {condition!r}"
        )


@pytest.mark.parametrize(("workflow", "variable"), _CASES)
def test_the_fuse_fails_closed(workflow: str, variable: str) -> None:
    for job in _jobs(workflow):
        condition = _condition(workflow, job)

        for fail_open in ("!= 'false'", '!= "false"', "|| true", "|| 'true'"):
            assert fail_open not in condition, (
                f"{workflow}: job `{job}` is gated fail-open (`{fail_open}`), so a "
                f"deleted or unreadable {variable} deploys instead of skipping: "
                f"{condition!r}"
            )

        # A bare `vars.NAME` is truthy for *any* non-empty value — including
        # `false`. Compared against something, or it is not a check.
        bare = re.search(rf"vars\.{variable}\s*(?=[&|)])", condition)
        assert bare is None, (
            f"{workflow}: job `{job}` tests {variable} for existence rather than "
            f"for `true`, so `false` deploys: {condition!r}"
        )


@pytest.mark.parametrize(("workflow", "variable"), _CASES)
def test_one_environment_cannot_disarm_the_other(workflow: str, variable: str) -> None:
    text = (_WORKFLOWS / workflow).read_text()

    for other_workflow, other in FUSES.items():
        if other == variable:
            continue
        assert other not in text, (
            f"{workflow} names {other}, which belongs to {other_workflow}: one "
            "value would then disarm both environments, and production's "
            "disarmed workflow is the rehearsed rollback path"
        )


@pytest.mark.parametrize(("workflow", "variable"), _CASES)
def test_the_fuse_is_a_job_condition_and_not_a_step_condition(workflow: str, variable: str) -> None:
    for job, body in _jobs(workflow).items():
        for step in body.get("steps") or []:
            step_if = re.sub(r"\s+", " ", step.get("if") or "")
            assert variable not in step_if, (
                f"{workflow}: step `{step.get('name')}` in job `{job}` tests "
                f"{variable} itself. A step-level check still allocates the "
                "runner and runs every other step of the job; the condition "
                "belongs on the job."
            )


def test_the_fuse_does_not_replace_what_already_gated_the_deploy() -> None:
    """Adding the fuse must not drop the conditions the deploy already had.

    Staging deploys on a green `dev` push or a dispatch, and that is the whole
    reason it is safe to leave automatic. A fuse added by rewriting the
    condition instead of extending it would deploy on a red CI — the guard
    removed by the commit that added a guard.
    """
    staging = _condition("deploy-staging.yml", "deploy")
    assert "github.event.workflow_run.conclusion == 'success'" in staging
    assert "github.event_name == 'workflow_dispatch'" in staging

    for job in _jobs("deploy-staging.yml"):
        assert "'success'" in _condition("deploy-staging.yml", job), (
            f"staging job `{job}` no longer requires CI to have concluded successfully"
        )


@pytest.mark.parametrize(("workflow", "variable"), _CASES)
def test_the_switch_is_written_down(workflow: str, variable: str) -> None:
    doc = _CD_DOC.read_text()
    assert f"`{variable}`" in doc, (
        f"{variable} disarms {workflow} and appears nowhere an operator would "
        f"look for it: add it to {_CD_DOC.relative_to(REPO_ROOT)}"
    )


def test_the_documented_switch_matches_github_semantics() -> None:
    """The one property of this fuse that the YAML cannot state.

    An Actions `==` comparison ignores case when comparing strings, so
    `== 'true'` accepts `True` and `TRUE` — the operator is not the strict
    literal match it reads as. That was wrong in this file's first draft and in
    the workflows' own comments, which is why the document is checked: no
    assertion over the YAML can catch a semantic of the expression language,
    and the next person to read `== 'true'` will assume exactly what the first
    draft assumed.
    """
    doc = _CD_DOC.read_text()
    assert "ignores case" in doc, (
        f"{_CD_DOC.relative_to(REPO_ROOT)} no longer records that GitHub's `==` "
        "ignores case, so `True` and `TRUE` deploy. A reviewer reading "
        "`== 'true'` will assume they skip, and the YAML cannot correct them."
    )
    assert "`True`" in doc, (
        f"{_CD_DOC.relative_to(REPO_ROOT)} should name the accepted spellings it "
        "documents (at least `True`), not assert the rule abstractly"
    )
