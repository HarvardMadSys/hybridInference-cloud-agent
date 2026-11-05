"""The GitHub credential a claim asks for: the runner's read-only clone token.

A claimed turn on a repository needs one GitHub App installation token, which
the runner checks the repository out with. The sandbox gets none: git and
``gh`` there hold a residency's GitHub relay placeholder
(``cloud_agent.tokens.mint_residency_github_token``), which the GitHub relay
exchanges for the owner's own token. Before that relay, the claim also minted a
repository-scoped *write* installation token for the sandbox; it is gone, so no
GitHub credential that GitHub itself accepts enters a sandbox any more.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from fastapi import HTTPException

from cloud_agent.github_app import AppNotInstalled
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.ports import AttemptCommands

#: The claim route's logger name, which the deployment's log pipeline selects
#: on: these records were the route's own before they moved here.
logger = get_logger("cloud_agent.api.routes")

# The clone credential is narrowed at the point it is minted, not merely by
# convention: the App holds more than read access, and an installation token
# inherits every permission the App has unless it is asked for less.
_CLONE_SCOPE = {"contents": "read"}


async def mint_clone_token(
    attempt_commands: AttemptCommands,
    claim: dict[str, Any],
    *,
    app_credentials: Any | None,
) -> str | None:
    """Return the turn's clone token, or ``None`` when it clones without one.

    The clone token is the runner's alone: read-only, this repository only, one
    hour. The runner needs it to check the repository out — a runner that
    cannot clone runs the agent in an empty directory. It never enters the
    sandbox. Absent (null) is a working configuration: a public repository
    clones without any credential at all.

    - an App that does not cover the repository is a settled answer: the turn
      goes ahead without a token;
    - any other GitHub failure hands the claim back with 503, so no attempt is
      spent.

    Raises:
        HTTPException: 503 after releasing the claim when GitHub could not mint
            a token for a reason other than the App not covering the repository.
    """
    repo = claim["repo"]
    # A temporary workspace has no repository, so there is nothing to clone.
    if app_credentials is None or repo is None:
        return None
    try:
        return await app_credentials.token_for(
            repo, permissions=_CLONE_SCOPE, repository_scoped=True
        )
    except AppNotInstalled:
        # A settled answer: the App does not cover this repository. Hand the
        # job over without credentials — a public repository clones
        # anonymously, and a private one fails with a message the owner can act
        # on ("install the App"), which retrying would not improve.
        logger.info(
            "agent_job_clone_token_unavailable",
            extra={"event": "agent_job_clone_token_unavailable", "job_id": claim["id"]},
        )
        return None
    except Exception as exc:
        await _hand_back(attempt_commands, claim, exc)


async def _hand_back(
    attempt_commands: AttemptCommands, claim: dict[str, Any], exc: Exception
) -> NoReturn:
    """Return the claim to the queue after GitHub could not mint a token.

    Anything but an App that does not cover the repository is GitHub or the
    network having a bad minute. Returning no token here would send the runner
    off to clone anonymously, fail on a private repository, and mark the job
    *terminally* failed — turning a momentary outage into the owner's problem.

    Hand the claim back rather than just letting the lease lapse. A lapsed
    lease still spends a retry, so a GitHub outage lasting across three claim
    cycles would fail every queued private-repo job outright, without an agent
    ever having started — the very outcome this exists to avoid, only slower.

    Raises:
        HTTPException: 503, always, once the claim is released.
    """
    await attempt_commands.release_claim(
        job_id=claim["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        retry_after_seconds=30,
    )
    logger.warning(
        "agent_job_clone_token_error",
        extra={"event": "agent_job_clone_token_error", "job_id": claim["id"]},
    )
    raise HTTPException(
        status_code=503,
        detail={
            "error": {
                "type": "credential_unavailable",
                "message": (
                    "Could not mint a repository credential for this job; it has "
                    "been returned to the queue and no attempt was spent."
                ),
            }
        },
    ) from exc
