"""The GitHub credentials a claim asks for, asked for at once.

A claimed turn on a repository needs two GitHub App installation tokens: a
read-only one the runner clones with and a write one for the sandbox. Neither
depends on the other, yet the claim waited for each in turn, so a turn started
a GitHub round trip later than it had to.

They are asked for together now and answered as if one at a time: the first of
them to fail, in the order they used to run, decides what is released or
logged, and what the claim answers.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, NoReturn

from fastapi import HTTPException

from cloud_agent.github_app import AppNotInstalled
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from cloud_agent.ports import AttemptCommands

#: The claim route's logger name, which the deployment's log pipeline selects
#: on: these records were the route's own before they moved here.
logger = get_logger("cloud_agent.api.routes")

# The clone credential is narrowed at the point it is minted, not merely by
# convention: the App also holds `contents: write` for the publisher, and an
# installation token inherits every permission the App has unless it is asked
# for less. Keep checkout authority separate from the token explicitly
# delivered to the sandbox for branch and pull-request publication. That one
# has the publisher's scope, and is minted for its conversation alone: code in
# a sandbox can revoke the token it holds, which must not be the publisher's
# or another conversation's.
_CLONE_SCOPE = {"contents": "read"}
_SANDBOX_GITHUB_SCOPE = {"contents": "write", "pull_requests": "write"}


async def mint_claim_credentials(
    attempt_commands: AttemptCommands,
    claim: dict[str, Any],
    *,
    app_credentials: Any | None,
) -> tuple[str | None, str | None]:
    """Return the turn's clone token and sandbox GitHub token.

    The clone token is the runner's alone: read-only, this repository only, one
    hour. The runner needs it to check the repository out — a runner that
    cannot clone runs the agent in an empty directory — and it is deliberately
    not the publisher's token, which can write. It never enters the sandbox.
    Absent (null) is a working configuration: a public repository clones
    without any credential at all.

    **Both run to their end, whatever the other does. Then the first to fail in
    the old order decides,** clone token, then GitHub token, exactly as when the
    one after it was never asked:

    - an App that does not cover the repository is a settled answer: the turn
      goes ahead with the token minted before it and none after;
    - any other GitHub failure hands the claim back with 503, so no attempt is
      spent.

    A cancelled request cancels the mint still running and is raised only once
    both have finished, so none goes on for a request nobody holds.

    Raises:
        HTTPException: 503 after releasing the claim when GitHub could not mint
            a token for a reason other than the App not covering the repository.
    """
    repo = claim["repo"]
    # A temporary workspace has no repository, so there is nothing to clone and
    # nothing a repository-scoped token could be scoped to: it gets neither.
    if app_credentials is None or repo is None:
        return None, None
    mints: list[Awaitable[Any]] = [
        app_credentials.token_for(repo, permissions=_CLONE_SCOPE, repository_scoped=True),
        # Cached per conversation rather than minted per claim: the CLI
        # fixes GH_TOKEN when it starts, so a new token on every follow-up
        # would replace the live process each turn.
        app_credentials.token_for(
            repo,
            permissions=_SANDBOX_GITHUB_SCOPE,
            repository_scoped=True,
            holder=str(claim["thread_id"]),
        ),
    ]
    outcomes = await asyncio.gather(*mints, return_exceptions=True)
    tokens: list[str | None] = [None, None]
    for index, outcome in enumerate(outcomes):
        if isinstance(outcome, AppNotInstalled):
            # A settled answer: the App does not cover this repository. Hand
            # the job over without credentials — a public repository clones
            # anonymously, and a private one fails with a message the owner can
            # act on ("install the App"), which retrying would not improve.
            logger.info(
                "agent_job_clone_token_unavailable",
                extra={"event": "agent_job_clone_token_unavailable", "job_id": claim["id"]},
            )
            break
        if isinstance(outcome, Exception):
            await _hand_back(attempt_commands, claim, outcome)
        elif isinstance(outcome, BaseException):
            # A cancellation is no failure to mint, and goes on as it did.
            raise outcome
        tokens[index] = outcome
    return tokens[0], tokens[1]


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
