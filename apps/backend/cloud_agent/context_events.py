"""A conversation's context events: what entered, left or changed its context.

Five kinds, each only where a record says so (design record, section 7.2):

- **injection**: what the platform wrapped a turn's prompt in at claim
  (attachments, durable memory, skills, SSH servers, goal; plugins by name),
  and text the CLI itself added to the conversation (Claude's non-replayed
  user lines, pi's extension messages, a server's synthetic message parts);
- **compaction**: the CLI's compaction reports, with the CLI's own sizes when
  it gave them;
- **prune**: Kilo marking an earlier tool output as cleared before a request
  that would be too large (OpenCode's prune is off in the platform's config);
- **model_switch**: a turn run on another model than the turn before it, or
  a request reporting another model than the one before it in its turn;
- **mode**: Claude Code entering or leaving plan mode, or starting a turn in
  another permission mode than the previous one.

Everything is read from the attempt profiles the Context view already reads
(:mod:`cloud_agent.context_profile`), so this costs no read of its own. Sizes
are only shown where they are recorded: a CLI's compaction sizes as the CLI's,
character counts as estimates. A token rise or drop on its own is never an
event, and a clipped stored preview is not a prune.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime  # noqa: TC003 - Pydantic resolves the model fields.
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.context_profile import profile_is_current

EventKind = Literal["injection", "compaction", "prune", "model_switch", "mode"]
EVENT_KINDS: tuple[EventKind, ...] = ("injection", "compaction", "prune", "model_switch", "mode")
#: Events one view lists; the rest are counted (``events_omitted``).
MAX_VIEW_EVENTS = 500

EventSupport = Literal["reported", "partly_reported", "not_reported"]


class ContextEvent(BaseModel):
    """One event, with the sizes its record carries and no others."""

    model_config = ConfigDict(extra="forbid")

    kind: EventKind
    #: Who produced it: ``platform_memory``, ``claude_user_line``,
    #: ``opencode_prune``, ``cli_compact_boundary``, ``turn_settings``…
    source: str
    label: str | None = None
    names: list[str] = Field(default_factory=list)
    items: int | None = None
    job_id: str
    turn_no: int
    attempt_id: int | None = None
    seq: int | None = None
    event_id: int | None = None
    at: datetime | None = None
    #: Characters the record measured, and their estimate in tokens.
    chars: int | None = None
    tokens: int | None = None
    #: ``cli``: the CLI's own count (compactions); ``estimate``: from characters.
    tokens_basis: Literal["cli", "estimate"] | None = None
    #: A compaction's sizes as the CLI reported them.
    before_tokens: int | None = None
    after_tokens: int | None = None
    #: A model switch's or mode change's two sides; ``None`` when unknown.
    from_value: str | None = None
    to_value: str | None = None
    #: From an attempt that was retried, so its turn's context did not keep it.
    superseded: bool = False
    #: From a Fork's copy of its source's turn.
    copied: bool = False


class EventCapability(BaseModel):
    """Whether a runtime's records can show one kind of event, and why not."""

    model_config = ConfigDict(extra="forbid")

    runtime: str
    kind: EventKind
    support: EventSupport
    #: A reason key the client words: ``platform_only``, ``no_native_record``…
    reason: str | None = None


#: What each pinned runtime's records can show (checked against their streams).
_SUPPORT: dict[str, dict[EventKind, tuple[EventSupport, str | None]]] = {
    "claude-code": {
        "injection": ("partly_reported", "since_this_release"),
        "compaction": ("reported", None),
        "prune": ("not_reported", "claude_microcompact_unrecorded"),
        "model_switch": ("reported", "turn_settings"),
        # The start-up permission mode and the plan-mode tool calls were
        # always stored; a mode the owner toggles inside the CLI is not.
        "mode": ("partly_reported", "claude_plan_tools"),
    },
    "codex": {
        "injection": ("partly_reported", "platform_only"),
        "compaction": ("reported", None),
        "prune": ("not_reported", "no_native_record"),
        "model_switch": ("reported", "turn_settings"),
        "mode": ("not_reported", "platform_fixed_mode"),
    },
    "pi": {
        "injection": ("partly_reported", "since_this_release"),
        "compaction": ("reported", None),
        "prune": ("not_reported", "no_native_record"),
        "model_switch": ("reported", "per_request"),
        "mode": ("not_reported", "platform_fixed_mode"),
    },
    "opencode": {
        "injection": ("partly_reported", "since_this_release"),
        "compaction": ("reported", None),
        # 1.18.9 prunes only with ``compaction.prune`` set, which defaults to
        # false and which the platform's config does not set: none happen.
        "prune": ("not_reported", "opencode_prune_off"),
        "model_switch": ("reported", "turn_settings"),
        "mode": ("not_reported", "platform_fixed_mode"),
    },
}
_SUPPORT["kilo"] = {
    **_SUPPORT["opencode"],
    # 7.4.17 prunes before a request whose payload is too large (seen, mid
    # turn); its end-of-turn prune also needs ``compaction.prune`` set.
    "prune": ("partly_reported", "kilo_payload_limit_only"),
}
_UNKNOWN: dict[EventKind, tuple[EventSupport, str | None]] = dict.fromkeys(
    EVENT_KINDS, ("not_reported", "unknown_runtime")
)


def event_capabilities(runtimes: Sequence[str]) -> list[EventCapability]:
    """The capability rows for the runtimes a conversation used."""
    return [
        EventCapability(runtime=runtime, kind=kind, support=support, reason=reason)
        for runtime in runtimes
        for kind, (support, reason) in _SUPPORT.get(runtime, _UNKNOWN).items()
    ]


def _count(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


class _Turn:
    """One job's place in the conversation, as the events need it."""

    def __init__(self, job: Mapping[str, Any]) -> None:
        self.job_id = str(job["job_id"])
        self.turn_no = int(job.get("turn_no") or 1)
        self.copied = bool(job.get("copied"))
        self.at = job.get("created_at")


def _profile_events(
    turn: _Turn, attempt: Mapping[str, Any], *, superseded: bool
) -> list[ContextEvent]:
    profile = attempt.get("profile")
    if not isinstance(profile, Mapping) or not profile_is_current(profile):
        return []
    attempt_id = int(attempt["attempt_id"])
    common: dict[str, Any] = {
        "job_id": turn.job_id,
        "turn_no": turn.turn_no,
        "attempt_id": attempt_id,
        "superseded": superseded,
        "copied": turn.copied,
    }
    events: list[ContextEvent] = []
    for raw in profile.get("events") or []:
        if not isinstance(raw, Mapping) or raw.get("kind") not in {"injection", "prune", "mode"}:
            continue
        where: dict[str, Any] = {
            "seq": raw.get("seq"),
            "event_id": raw.get("event_id"),
            "at": raw.get("at"),
        }
        tokens = _count(raw.get("tokens"))
        if raw["kind"] == "mode":
            events.append(
                ContextEvent(
                    kind="mode",
                    source=str(raw.get("source") or ""),
                    to_value=str(raw.get("mode") or "") or None,
                    **where,
                    **common,
                )
            )
            continue
        events.append(
            ContextEvent(
                kind=raw["kind"],
                source=str(raw.get("source") or ""),
                label=raw.get("label"),
                names=[str(n) for n in raw.get("names") or []],
                items=_count(raw.get("items")),
                chars=_count(raw.get("chars")),
                tokens=tokens,
                tokens_basis="estimate" if tokens is not None else None,
                **where,
                **common,
            )
        )
    for raw in profile.get("compactions") or []:
        if not isinstance(raw, Mapping) or raw.get("completed") is False:
            continue
        before, after = _count(raw.get("pre_tokens")), _count(raw.get("post_tokens"))
        events.append(
            ContextEvent(
                kind="compaction",
                source=str(raw.get("source") or ""),
                label=raw.get("trigger"),
                seq=raw.get("seq"),
                event_id=raw.get("event_id"),
                at=raw.get("at"),
                before_tokens=before,
                after_tokens=after,
                tokens=after - before if before is not None and after is not None else None,
                tokens_basis="cli" if before is not None and after is not None else None,
                **common,
            )
        )
    return events


def _request_models(
    turn: _Turn, attempt: Mapping[str, Any], *, superseded: bool
) -> list[ContextEvent]:
    """A request reporting another model than the one before it in this attempt."""
    profile = attempt.get("profile")
    if not isinstance(profile, Mapping) or not profile_is_current(profile):
        return []
    events = []
    previous: str | None = None
    for raw in profile.get("requests") or []:
        model = raw[9] if len(raw) > 9 and isinstance(raw[9], str) and raw[9] else None
        if model and previous and model != previous:
            events.append(
                ContextEvent(
                    kind="model_switch",
                    source="request_usage",
                    from_value=previous,
                    to_value=model,
                    job_id=turn.job_id,
                    turn_no=turn.turn_no,
                    attempt_id=int(attempt["attempt_id"]),
                    seq=raw[0],
                    event_id=raw[1],
                    at=raw[2],
                    superseded=superseded,
                    copied=turn.copied,
                )
            )
        previous = model or previous
    return events


def _changes_only(events: list[ContextEvent]) -> list[ContextEvent]:
    """Permission modes kept only where they differ from the one before."""
    kept: list[ContextEvent] = []
    previous: str | None = None
    for event in events:
        if event.kind == "mode" and event.source == "claude_init":
            if event.superseded:
                continue
            if previous is None or event.to_value == previous:
                previous = event.to_value
                continue
            event.from_value, previous = previous, event.to_value
        kept.append(event)
    return kept


def assemble_context_events(
    *,
    jobs: Sequence[Mapping[str, Any]],
    by_job: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[list[ContextEvent], dict[str, int], int]:
    """The conversation's events in turn, attempt and stream order; counts by kind.

    Returns the listed events (bounded), the count of every kind over all of
    them, and how many were left out of the list.
    """
    events: list[ContextEvent] = []
    previous_model: str | None = None
    for job in jobs:
        turn = _Turn(job)
        model = job.get("model") if isinstance(job.get("model"), str) else None
        if model and previous_model and model != previous_model:
            events.append(
                ContextEvent(
                    kind="model_switch",
                    source="turn_settings",
                    from_value=previous_model,
                    to_value=model,
                    job_id=turn.job_id,
                    turn_no=turn.turn_no,
                    at=turn.at,
                    copied=turn.copied,
                )
            )
        previous_model = model or previous_model
        attempts = list(by_job.get(turn.job_id, []))
        for rank, attempt in enumerate(attempts):
            superseded = rank < len(attempts) - 1
            found = [
                *_profile_events(turn, attempt, superseded=superseded),
                *_request_models(turn, attempt, superseded=superseded),
            ]
            events.extend(sorted(found, key=lambda event: event.seq or 0))
    events = _changes_only(events)
    counts: dict[str, int] = dict.fromkeys(EVENT_KINDS, 0)
    for event in events:
        if not event.superseded:
            counts[event.kind] += 1
    # Events an attempt's profile could not keep are counted, not listed.
    kept_back = sum(
        int((attempt.get("profile") or {}).get("events_omitted") or 0)
        for attempts in by_job.values()
        for attempt in attempts
        if isinstance(attempt.get("profile"), Mapping)
    )
    omitted = max(0, len(events) - MAX_VIEW_EVENTS)
    return events[omitted:], counts, omitted + kept_back
