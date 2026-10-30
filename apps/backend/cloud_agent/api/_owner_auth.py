"""Owner authentication and authorization for the agent surface.

The three names every owner-facing handler depends on: the session credential
check, the account admission that follows it, and the ownership fence that
turns another account's job into a 404. They live here rather than in the
aggregator so the job, turn, thread, workspace, source-control, catalog and
config routers all share one home for "who is this, and may they".

These callables are the objects tests hand to ``dependency_overrides``, so the
aggregator re-exports them unchanged rather than wrapping them: FastAPI matches
an override by object identity.

``Request`` is a runtime import here rather than a ``TYPE_CHECKING`` one on
purpose. FastAPI resolves a dependency callable's signature while the app is
being assembled, so a name that exists only for the type checker leaves the
annotation an unresolved forward reference; FastAPI then reads ``request`` as an
ordinary parameter, answers 422, and ``create_app().openapi()`` raises trying to
build a schema for it.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import Depends, Header, HTTPException, Request

from cloud_agent.accounts.session_cookie import local_session_from_cookies
from cloud_agent.api.deps import (
    bearer_token,
    get_current_user,
    get_owner_directory,
    local_session_owner,
    looks_like_agent_token,
    require_agent_access,
    resolve_agent_access,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from cloud_agent.accounts.directory import LocalOwnerDirectory
    from cloud_agent.ports import ConversationQueries


#: The longest one owner SSE response stays open: a job's events, the account's
#: change feed, a terminal's output. Each stream ends after this long, and
#: every client resumes from what it last applied (an event id, a terminal
#: sequence, or a ``hello`` that re-reads its lists); that reconnect is
#: admitted again.
OWNER_STREAM_MAX_S = 15 * 60.0

#: An open owner stream also re-reads its session and the account's
#: admission this often, and ends on a refusal. Revoking or
#: suspending an account therefore ends its streams within this bound plus
#: one keepalive, not :data:`OWNER_STREAM_MAX_S`.
LOCAL_STREAM_RECHECK_S = 30.0


def owner_stream_guard(request: Request, user_id: str) -> Callable[[], Awaitable[bool]]:
    """A check an owner stream calls between frames; ``False`` ends the stream.

    It re-reads the session and the account at most every
    :data:`LOCAL_STREAM_RECHECK_S`, from this database, and fails closed.
    """
    checked = time.monotonic()

    async def admitted() -> bool:
        nonlocal checked
        if time.monotonic() - checked < LOCAL_STREAM_RECHECK_S:
            return True
        checked = time.monotonic()
        request.state.local_session = None
        try:
            owner = await local_session_owner(request, request.headers.get("authorization"))
            if owner is None or owner.account.id != user_id:
                return False
            decision = await resolve_agent_access(get_owner_directory(request), user_id)
        except HTTPException:
            return False
        return decision.permits("agent.use")

    return admitted


async def authenticate_agent_owner(
    request: Request,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Authenticate an Agent owner from their account session.

    There is **one** owner credential: the account session, carried in the
    ``HttpOnly`` cookie sign-in sets (all the web app holds) or as a bearer of
    the session's own shape, for a programmatic caller that has one.
    ``get_current_user`` accepts both, preferring the cookie.

    One namespace is refused **before** the session lookup sees it: a worker
    capability token (``ajt.*``) is the sandbox's credential, and a credential
    that escaped the sandbox must be told apart from an unknown one rather than
    reported as a missing session, so it is a flat 403 ``insufficient_scope``.
    The cookie still wins when both are present.
    """
    presented = bearer_token(authorization)
    cookie = local_session_from_cookies(request.cookies)
    if not cookie and looks_like_agent_token(presented):
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "type": "insufficient_scope",
                    "message": "This credential is a sandbox's, not an owner's.",
                }
            },
        )

    return await get_current_user(request=request, authorization=authorization)


async def require_agent_owner(
    user: dict[str, Any] = Depends(authenticate_agent_owner),
    owners: LocalOwnerDirectory = Depends(get_owner_directory),
) -> dict[str, Any]:
    """Require the account's current ``agent.use`` for every owner read and write.

    Identity selects the owner; the account's permission, read fresh from this
    database, admits the request. Ownership checks still fence every resource.

    The owner identity carries no admin flag. Admin authority lives behind
    ``verify_admin_access`` on the ``/admin`` surface, which reads it itself;
    an ``is_admin`` here would be a second answer to that question, and one
    every owner route could branch on.
    """
    decision = await require_agent_access(owners, user["user_id"])
    return {
        **user,
        "agent_access": decision.public().model_dump(),
    }


async def _owned_job(
    store: ConversationQueries,
    job_id: str,
    user: dict[str, Any],
) -> dict[str, Any]:
    """Fetch a job the caller owns, or raise 404.

    A job owned by someone else is reported as 404 rather than 403 so job ids
    cannot be probed for existence across accounts.
    """
    job = await store.get_job(job_id)
    if job is None or job["user_id"] != user["user_id"] or job.get("deleted_at") is not None:
        raise _no_such_job(job_id)
    return job


async def _require_owned_job(
    store: ConversationQueries,
    job_id: str,
    user: dict[str, Any],
) -> None:
    """Refuse a job that is not the caller's, exactly as :func:`_owned_job` does.

    For a route that reads nothing of the job but whether it may: two
    primary-key probes instead of the full job read with its sandbox,
    conversation-state and skills subqueries. The 404 is the same, so ids still
    cannot be probed across accounts.
    """
    if not await store.owns_job(job_id=job_id, user_id=user["user_id"]):
        raise _no_such_job(job_id)


def _no_such_job(job_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
    )
