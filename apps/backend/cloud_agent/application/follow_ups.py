"""The checks a follow-up passes before its turn is written.

Lifted out of the follow-up route unchanged, so that every writer of a person's
next turn asks the same questions in the same order: is the session still open,
is its native context still there, is the harness the thread's own, does the
model resolve, is the effort offered for that model, may this owner still reach
the repository, and do the skills and uploads it names still exist. A second
copy of these rules would be a second place for them to drift.

What comes back is the resolved form a store writes — canonical model id,
validated effort, skill snapshots and the upload manifest — and never the raw
request. Refusals are the route's own ``HTTPException``s, with the same types and
messages the route has always answered with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import HTTPException

from cloud_agent.application.job_admission import (
    _require_offered_effort,
    _require_registered_runtime,
    _require_resolvable_model,
    _resolve_attachment_selection,
    _resolve_skill_selection,
)
from cloud_agent.entitlement import RepoNotAllowed, require_entitled_repo
from cloud_agent.schemas import RESET_REASONING_EFFORT

if TYPE_CHECKING:
    from cloud_agent.ports import (
        AttachmentRepository,
        SkillRepository,
        SourceControlRepository as SourceControlPort,
    )


class _FollowUpStorage(Protocol):
    """What resolving a follow-up reads: uploads, skills and repository access."""

    attachments: AttachmentRepository
    skills: SkillRepository
    source_control: SourceControlPort


@dataclass(frozen=True)
class PreparedFollowUp:
    """A follow-up's per-turn choices, resolved and checked.

    ``model`` is the canonical id, or ``None`` to inherit the parent's.
    ``reasoning_effort`` is the validated level, or ``None`` to inherit;
    ``reset_reasoning_effort`` is the one way to clear an inherited level back
    to Auto. ``skills`` is ``None`` to inherit the previous turn's snapshots.
    ``attachments`` is the manifest of uploads this turn adds, or ``None``.
    """

    model: str | None
    reasoning_effort: str | None
    reset_reasoning_effort: bool
    skills: list[dict[str, Any]] | None
    attachments: list[dict[str, Any]] | None


def sandbox_context_lost_error() -> HTTPException:
    """Return the stable owner-facing error for an unrunnable retained chat."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "sandbox_context_lost",
                "message": (
                    "This chat's retained sandbox has an indeterminate or lost "
                    "context and cannot safely accept another message."
                ),
            }
        },
    )


def session_archived_error() -> HTTPException:
    """Require an explicit unarchive before accepting another conversation turn."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "session_archived",
                "message": "Unarchive this session before continuing the conversation.",
            }
        },
    )


async def prepare_follow_up(
    parent: dict[str, Any],
    *,
    user: dict[str, Any],
    skill_ids: list[int] | None,
    attachment_ids: list[int] | None,
    runtime: str | None,
    model: str | None,
    reasoning_effort: str | None,
    store: _FollowUpStorage,
    app_credentials: Any | None,
    gateway: Any,
) -> PreparedFollowUp:
    """Check a follow-up against the turn it would follow, and resolve it.

    ``parent`` is the owner's read of that turn (``get_job``), which carries the
    thread's archive state and its retained sandbox. Both of those checks are
    advisory here — the store repeats them under the thread lock — and they are
    asked first because a closed conversation should not cost a model lookup.
    """
    if parent.get("archived_at") is not None:
        raise session_archived_error()
    retained = parent.get("thread_sandbox")
    if isinstance(retained, dict) and (
        retained.get("context_status") == "lost"
        or retained.get("state") in {"stopped_dirty", "lost"}
    ):
        raise sandbox_context_lost_error()
    # The harness is fixed per thread: continuity is the harness
    # session, and no other harness can continue it. The field survives as an
    # explicit no-op so existing callers that echo the thread's runtime keep
    # working; a *different* runtime is refused rather than silently starting
    # a turn the thread's memory cannot follow.
    if runtime is not None:
        _require_registered_runtime(runtime)
        if runtime != parent["runtime"]:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request",
                        "message": (
                            f"this thread runs on {parent['runtime']!r} and a thread's "
                            "harness is fixed; start a new chat to use "
                            f"{runtime!r}"
                        ),
                    }
                },
            )
    # A follow-up may switch models; the switched-to model gets the same
    # fail-fast as a fresh job. An omitted model inherits the parent's, which
    # was validated when it was chosen.
    canonical = None
    if model:
        canonical = await _require_resolvable_model(model, gateway=gateway, user=user)
    # Effort is per turn, like the model, and inherits when omitted. Unlike the
    # model it needs a way *back*: null already means "no override, inherit", so
    # clearing an inherited effort takes the explicit reset sentinel. Validated
    # against the model this turn will run — which may be the switched-to one,
    # and a pair that was offered last turn need not be offered this one.
    effort: str | None = None
    reset_effort = reasoning_effort == RESET_REASONING_EFFORT
    if reasoning_effort is not None and not reset_effort:
        effort = await _require_offered_effort(
            reasoning_effort,
            runtime=parent["runtime"],
            model=canonical or parent["model"],
            gateway=gateway,
            user=user,
        )
    try:
        await require_entitled_repo(
            parent["repo"],
            user["user_id"],
            store=store.source_control,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc
    # An unavailable account integration must not prevent unrelated conversation
    # work. The runner receives only usable servers, resolved when it claims
    # this turn, and Tools retains each connection's diagnosis.
    skills = (
        await _resolve_skill_selection(store.skills, user_id=user["user_id"], skill_ids=skill_ids)
        if skill_ids is not None
        else None
    )
    attachments = await _resolve_attachment_selection(
        store.attachments, user_id=user["user_id"], attachment_ids=attachment_ids
    )
    return PreparedFollowUp(
        # Canonical, or the parent's when omitted — never the alias the caller
        # typed, which the gateway's grant would not match.
        model=canonical if model else None,
        reasoning_effort=effort,
        reset_reasoning_effort=reset_effort,
        skills=skills,
        attachments=attachments,
    )
