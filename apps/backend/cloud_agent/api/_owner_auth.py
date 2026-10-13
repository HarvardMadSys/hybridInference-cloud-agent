"""Owner authentication and authorization for the agent surface.

The three names every owner-facing handler depends on: the session credential
check, the Gateway admission that follows it, and the ownership fence that
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

from typing import TYPE_CHECKING

from fastapi import Depends, Header, HTTPException, Request

from cloud_agent.api.deps import (
    bearer_token,
    get_current_user,
    get_gateway_client,
    looks_like_agent_token,
)
from cloud_agent.identity import session_from_cookies

if TYPE_CHECKING:
    from typing import Any

    from cloud_agent.gateway import GatewayClient
    from cloud_agent.ports import ConversationQueries


async def authenticate_agent_owner(
    request: Request,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Authenticate an Agent owner from this service's session.

    There is **one** owner credential here: the session token
    :mod:`cloud_agent.identity` mints, carried either in the ``HttpOnly``
    cookie ``/v1/session/login`` sets — which is all the web app holds — or as
    a bearer, for a programmatic caller that has one. ``get_current_user``
    accepts both, preferring the cookie.

    **The API-key branch was deleted on 2026-08-04**, not merely disabled. It
    resolved an ``hyi-*`` key through the gateway's ``api_keys`` table, which
    did not move; nothing in this repository ever set the store it read, and
    ``GatewayClient`` answers "may this user sign in", never "who owns this
    key". A branch with no producer answers 503 in every real deployment while
    looking implemented, and invites a later reader to "fix" it by reaching
    back into the gateway's database — the coupling this split exists to
    remove. Its ported test is recorded in MIGRATION.md's discarded register.
    Restoring a programmatic owner credential means adding a gateway contract
    under ``gateway_contracts/``, not resurrecting this.

    One namespace is still refused **before** the session verifier sees it: a
    worker capability token (``ajt.*``) is the sandbox's credential, and a
    credential that escaped the sandbox must be told apart from an unknown one
    rather than reported as a missing session. The gateway spelled this as an
    allowlist of inference paths because the same dependency also guarded
    ``/v1/chat/completions``; this service serves no inference route, so it
    collapses to a flat 403 ``insufficient_scope``. The cookie still wins when
    both are present, which is the precedence ``session_token`` documents.
    """
    presented = bearer_token(authorization)
    if not session_from_cookies(request.cookies) and looks_like_agent_token(presented):
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "type": "insufficient_scope",
                    "message": "This credential may only be used for model inference.",
                }
            },
        )

    return await get_current_user(request=request, authorization=authorization)


async def require_agent_owner(
    request: Request,
    user: dict[str, Any] = Depends(authenticate_agent_owner),
    gateway: GatewayClient = Depends(get_gateway_client),
) -> dict[str, Any]:
    """Require current Gateway admission for every owner read and write.

    Identity selects the owner; the Gateway's explicit agent.use permission
    admits the request. Local ownership checks still fence every resource.

    The owner identity carries no admin flag. Admin authority lives behind
    ``verify_admin_access`` on the ``/admin`` surface, which asks the Gateway
    itself — an ``is_admin`` here would be a second answer to that question,
    and one every owner route could branch on.
    """
    from cloud_agent.api.deps import require_agent_access

    decision = await require_agent_access(gateway, user["user_id"])
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
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    return job
