"""What one model request carried, as far as the platform can tell.

A request of a conversation (a point of the Context view) is shown three ways,
each labelled for what it is:

- **Provider input**: the request's own reported input, with cache reads and
  writes as an accounting split of that same input, never added to it.
- **CLI snapshot**: what the CLI itself said its context held once the turn
  settled (:mod:`cloud_agent_host.runtimes.context_snapshot`), when the
  runtime offers it and the run was collected with it. It describes the
  context after the turn's last request and its reply, so a request earlier
  in the turn says how many requests came after it.
- **Reconstructed**: the conversation content the platform stored, in the
  order it was stored, selected for this request: the turns up to it, after
  the last compaction the CLI reported before it, from the attempt that
  carried each turn, without delegated agents' own content and without this
  request's own output. It is a reconstruction from the transcript's order,
  never the assembled request: what the CLI added (system prompt, tool
  definitions, instructions) is not in it, previews may be clipped, and token
  counts are estimates (:mod:`cloud_agent_shared.text_tokens`).

Nothing here subtracts one number from another to say what fills the
difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.context_view import ContextPoint  # noqa: TC001 — Pydantic resolves it.
from cloud_agent_shared.text_tokens import ESTIMATE_BASIS, estimate_from_counts

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Entries one reconstruction lists; the largest are kept, all are counted.
MAX_REQUEST_ENTRIES = 300
#: Runtimes whose usage report follows the response it measures, so the
#: assistant content between the previous report and this one is this
#: request's own output.
_REPORT_AFTER_RESPONSE = frozenset({"codex_token_usage", "opencode_step"})
_ASSISTANT_KINDS = frozenset({"message", "thinking", "tool_use"})

Category = Literal[
    "user_messages",
    "assistant",
    "reasoning",
    "tool_calls",
    "tool_results",
    "compaction_summary",
]
CATEGORY_ORDER: tuple[Category, ...] = (
    "compaction_summary",
    "user_messages",
    "assistant",
    "reasoning",
    "tool_calls",
    "tool_results",
)
_EVENT_CATEGORY: dict[str, Category] = {
    "message": "assistant",
    "thinking": "reasoning",
    "tool_use": "tool_calls",
    "tool_result": "tool_results",
    "user_message": "user_messages",
}


class CompositionEntry(BaseModel):
    """One piece of stored content this request is reconstructed to carry."""

    model_config = ConfigDict(extra="forbid")

    category: str
    label: str
    #: The stored event; None for a turn's prompt, which is stored on the turn.
    event_id: int | None = None
    job_id: str
    turn_no: int
    chars: int = Field(ge=0)
    tokens: int = Field(ge=0)
    #: The stored copy is a clipped preview: chars and tokens undercount.
    truncated: bool = False
    preview: str


class CompositionCategory(BaseModel):
    """One category's totals over every entry, not only the listed ones."""

    model_config = ConfigDict(extra="forbid")

    key: str
    entries: int = Field(ge=0)
    chars: int = Field(ge=0)
    tokens: int = Field(ge=0)
    truncated_entries: int = Field(ge=0)


class ExcludedContent(BaseModel):
    """Stored content counted out of this request, by reason."""

    model_config = ConfigDict(extra="forbid")

    entries: int = Field(default=0, ge=0)
    chars: int = Field(default=0, ge=0)


class CompositionBoundary(BaseModel):
    """The last compaction the CLI reported before this request."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    turn_no: int
    seq: int
    event_id: int | None = None
    source: str
    #: Claude may keep a recent segment through a compaction; the transcript
    #: cannot say which messages, so they are counted as compacted here.
    may_preserve_recent: bool = False


class ReconstructedComposition(BaseModel):
    """Stored content selected for this request, in transcript order."""

    model_config = ConfigDict(extra="forbid")

    basis: Literal["stored_events_transcript_order"] = "stored_events_transcript_order"
    token_basis: str = ESTIMATE_BASIS
    categories: list[CompositionCategory]
    entries: list[CompositionEntry]
    entries_total: int = Field(ge=0)
    entries_omitted: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    boundary: CompositionBoundary | None = None
    excluded: dict[str, ExcludedContent]
    #: Turns copied from another conversation by a Fork: carried by the CLI,
    #: stored with the source conversation, not reconstructed here.
    copied_turns: int = Field(ge=0)
    #: The stored events were more than one read lists; the earliest are not here.
    events_capped: bool = False


class SnapshotRelation(BaseModel):
    """How the CLI's snapshot relates to this request."""

    model_config = ConfigDict(extra="forbid")

    event_id: int | None = None
    #: Requests of the same turn made after this one and before the snapshot.
    requests_after: int = Field(ge=0)


class RequestComposition(BaseModel):
    """Everything known about what one model request carried."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    request: ContextPoint
    runtime: str | None = None
    #: The CLI's own snapshot, as stored; None when there is none.
    native: dict[str, Any] | None = None
    native_relation: SnapshotRelation | None = None
    #: Why there is no usable snapshot: ``not_offered`` (runtime has none),
    #: ``not_collected`` (run predates collection), or the snapshot's status.
    native_status: str
    reconstructed: ReconstructedComposition


@dataclass
class _Tally:
    entries: int = 0
    chars: int = 0

    def add(self, chars: int) -> None:
        self.entries += 1
        self.chars += chars


@dataclass
class _Walk:
    """Running state while stored content is assigned to this request."""

    entries: list[CompositionEntry] = field(default_factory=list)
    excluded: dict[str, _Tally] = field(
        default_factory=lambda: {
            key: _Tally()
            for key in ("compacted", "superseded_attempts", "subagent", "this_request_output")
        }
    )


def _tokens(row: Mapping[str, Any]) -> int:
    return estimate_from_counts(
        int(row.get("ascii") or 0), int(row.get("cjk") or 0), int(row.get("other") or 0)
    )


def _entry(
    row: Mapping[str, Any], category: str, label: str, job_id: str, turn_no: int
) -> CompositionEntry:
    chars = int(row.get("chars") or 0)
    stored = row.get("stored_chars")
    truncated = bool(row.get("truncated")) or (isinstance(stored, int) and stored > chars)
    return CompositionEntry(
        category=category,
        label=label[:200],
        event_id=row.get("id"),
        job_id=job_id,
        turn_no=turn_no,
        chars=chars,
        tokens=_tokens(row),
        truncated=truncated,
        preview=" ".join(str(row.get("preview") or "").split())[:200],
    )


def _event_category(row: Mapping[str, Any]) -> str | None:
    kind = str(row.get("event_type") or "")
    if kind == "lifecycle":
        phase = row.get("phase")
        if phase == "compaction_summary":
            return "compaction_summary"
        if phase == "compact_boundary" and row.get("chars"):
            return "compaction_summary"
        return None
    return _EVENT_CATEGORY.get(kind)


@dataclass(frozen=True)
class RequestScope:
    """Where the selected request sits, as the walk needs it."""

    attempt_id: int
    seq: int
    usage_id: str | None
    #: The previous request's report in the same attempt (0 when none).
    previous_report_seq: int
    report_after_response: bool
    #: ``(turn_no, attempt_rank, seq)`` of the last compaction before it.
    boundary_key: tuple[int, int, int] | None


def _row_reason(
    row: Mapping[str, Any], key: tuple[int, int, int], scope: RequestScope
) -> str | None:
    """Why stored content is not counted as carried, or None when it is."""
    if row.get("subagent"):
        return "subagent"
    if scope.boundary_key is not None and key < scope.boundary_key:
        return "compacted"
    if row.get("attempt_id") != scope.attempt_id or row.get("event_type") not in _ASSISTANT_KINDS:
        return None
    if scope.usage_id and row.get("usage_id") == scope.usage_id:
        return "this_request_output"
    if scope.report_after_response and int(row.get("seq") or 0) > scope.previous_report_seq:
        return "this_request_output"
    return None


def walk_composition(
    *,
    scope: RequestScope,
    turns: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    superseded: Mapping[str, int],
    tool_names: Mapping[str, str],
) -> _Walk:
    """Assign each stored item of the carried turns to this request or a reason.

    ``turns`` carry ``job_id``, ``turn_no``, ``attempt_rank`` (the carried
    attempt's rank) and the prompt's counts and preview; ``events`` carry the
    projected rows of the carried attempts, each with ``turn_no`` and
    ``attempt_rank``.
    """
    walk = _Walk()
    for turn in turns:
        key = (int(turn["turn_no"]), -1, 0)
        row = {**turn, "id": None}
        if scope.boundary_key is not None and key < scope.boundary_key:
            walk.excluded["compacted"].add(int(turn.get("chars") or 0))
            continue
        walk.entries.append(
            _entry(row, "user_messages", "prompt", str(turn["job_id"]), int(turn["turn_no"]))
        )
    for event in events:
        category = _event_category(event)
        if category is None:
            continue
        key = (int(event["turn_no"]), int(event["attempt_rank"]), int(event.get("seq") or 0))
        reason = _row_reason(event, key, scope)
        if reason is not None:
            walk.excluded[reason].add(int(event.get("chars") or 0))
            continue
        label = str(event.get("name") or "")
        if category == "tool_results":
            label = tool_names.get(str(event.get("tool_use_id") or ""), "") or "tool result"
        walk.entries.append(
            _entry(event, category, label or category, str(event["job_id"]), int(event["turn_no"]))
        )
    walk.excluded["superseded_attempts"].entries += int(superseded.get("entries") or 0)
    walk.excluded["superseded_attempts"].chars += int(superseded.get("chars") or 0)
    return walk


def reconstructed(
    walk: _Walk,
    *,
    boundary: CompositionBoundary | None,
    copied_turns: int,
    events_capped: bool,
) -> ReconstructedComposition:
    """Totals per category over every entry, and the largest entries listed."""
    totals: dict[str, CompositionCategory] = {}
    for entry in walk.entries:
        known = totals.setdefault(
            entry.category,
            CompositionCategory(
                key=entry.category, entries=0, chars=0, tokens=0, truncated_entries=0
            ),
        )
        known.entries += 1
        known.chars += entry.chars
        known.tokens += entry.tokens
        known.truncated_entries += int(entry.truncated)
    kept = sorted(walk.entries, key=lambda entry: -entry.tokens)[:MAX_REQUEST_ENTRIES]
    return ReconstructedComposition(
        categories=[totals[key] for key in CATEGORY_ORDER if key in totals],
        entries=kept,
        entries_total=len(walk.entries),
        entries_omitted=len(walk.entries) - len(kept),
        estimated_tokens=sum(entry.tokens for entry in walk.entries),
        boundary=boundary,
        excluded={
            key: ExcludedContent(entries=tally.entries, chars=tally.chars)
            for key, tally in walk.excluded.items()
        },
        copied_turns=copied_turns,
        events_capped=events_capped,
    )


def report_after_response(sources: Sequence[str]) -> bool:
    """Whether this attempt's usage reports follow the responses they measure."""
    return bool(_REPORT_AFTER_RESPONSE.intersection(sources))


#: Runtimes that can answer a context snapshot (see the design record).
SNAPSHOT_RUNTIMES = frozenset({"claude-code", "pi", "opencode", "kilo"})


@dataclass(frozen=True)
class LocatedRequest:
    """A request found in its conversation, with what the reads need."""

    point: ContextPoint
    runtime: str | None
    scope_attempt_rank: int
    carried_attempts: dict[int, tuple[str, int, int]]
    superseded_attempts: list[int]
    carried_jobs: list[str]
    copied_turns: int
    boundary: CompositionBoundary | None
    boundary_key: tuple[int, int, int] | None
    previous_report_seq: int
    report_after_response: bool
    snapshot_event_id: int | None
    snapshot_status: str | None
    requests_after: int


def _attempt_ranks(
    jobs: Sequence[Mapping[str, Any]], attempts: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, list[Mapping[str, Any]]], dict[str, int]]:
    by_job: dict[str, list[Mapping[str, Any]]] = {}
    for attempt in attempts:
        by_job.setdefault(str(attempt["job_id"]), []).append(attempt)
    turn_of = {str(job["job_id"]): int(job.get("turn_no") or 1) for job in jobs}
    return by_job, turn_of


def _boundary(
    carried: Mapping[int, tuple[str, int, int]],
    profiles: Mapping[int, Mapping[str, Any]],
    before: tuple[int, int, int],
) -> tuple[CompositionBoundary | None, tuple[int, int, int] | None]:
    best: tuple[tuple[int, int, int], CompositionBoundary] | None = None
    for attempt_id, (job_id, turn_no, rank) in carried.items():
        for compaction in (profiles.get(attempt_id) or {}).get("compactions") or []:
            if compaction.get("completed") is False:
                continue
            key = (turn_no, rank, int(compaction.get("seq") or 0))
            if key >= before or (best is not None and key <= best[0]):
                continue
            source = str(compaction.get("source") or "")
            best = (
                key,
                CompositionBoundary(
                    job_id=job_id,
                    turn_no=turn_no,
                    seq=key[2],
                    event_id=compaction.get("event_id"),
                    source=source,
                    may_preserve_recent=source == "cli_compact_boundary",
                ),
            )
    return (best[1], best[0]) if best else (None, None)


def locate_request(
    *,
    view_points: Sequence[ContextPoint],
    jobs: Sequence[Mapping[str, Any]],
    attempts: Sequence[Mapping[str, Any]],
    event_id: int,
) -> LocatedRequest | None:
    """Find the request ``event_id`` reported and what surrounds it."""
    point = next((p for p in view_points if p.event_id == event_id), None)
    if point is None:
        return None
    by_job, turn_of = _attempt_ranks(jobs, attempts)
    profiles = {int(a["attempt_id"]): a.get("profile") or {} for a in attempts}
    carried: dict[int, tuple[str, int, int]] = {}
    superseded: list[int] = []
    rank_a = 0
    for job in jobs:
        job_id = str(job["job_id"])
        turn_no = turn_of[job_id]
        rows = by_job.get(job_id, [])
        if turn_no > point.turn_no or not rows:
            continue
        ids = [int(row["attempt_id"]) for row in rows]
        chosen = point.attempt_id if job_id == point.job_id else ids[-1]
        rank = ids.index(chosen) if chosen in ids else len(ids) - 1
        carried[chosen] = (job_id, turn_no, rank)
        superseded.extend(i for i in ids[:rank])
        if job_id == point.job_id:
            rank_a = rank
    before = (point.turn_no, rank_a, point.seq)
    boundary, boundary_key = _boundary(carried, profiles, before)
    profile = profiles.get(point.attempt_id) or {}
    reports = [raw[0] for raw in profile.get("requests") or [] if raw and raw[0] < point.seq]
    snapshot = next(
        (snap for snap in profile.get("snapshots") or [] if int(snap.get("seq") or 0) > point.seq),
        None,
    )
    after = 0
    if snapshot is not None:
        after = sum(
            1
            for raw in profile.get("requests") or []
            if raw and point.seq < raw[0] < int(snapshot["seq"])
        )
    job = next((j for j in jobs if str(j["job_id"]) == point.job_id), {})
    return LocatedRequest(
        point=point,
        runtime=job.get("runtime"),
        scope_attempt_rank=rank_a,
        carried_attempts=carried,
        superseded_attempts=superseded,
        carried_jobs=[
            str(j["job_id"])
            for j in jobs
            if turn_of[str(j["job_id"])] <= point.turn_no and not j.get("copied")
        ],
        copied_turns=sum(
            1 for j in jobs if j.get("copied") and turn_of[str(j["job_id"])] <= point.turn_no
        ),
        boundary=boundary,
        boundary_key=boundary_key,
        previous_report_seq=max(reports, default=0),
        report_after_response=report_after_response(profile.get("sources") or []),
        snapshot_event_id=snapshot.get("event_id") if snapshot else None,
        snapshot_status=str(snapshot.get("status")) if snapshot else None,
        requests_after=after,
    )


def assemble_request(
    *,
    thread_id: str,
    located: LocatedRequest,
    usage_id: str | None,
    prompts: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    superseded: Mapping[str, int],
    snapshot: Mapping[str, Any] | None,
    events_capped: bool,
) -> RequestComposition:
    """Put the reads together into the request's composition."""
    carried = located.carried_attempts
    ranked = []
    for row in events:
        owner = carried.get(int(row["attempt_id"]))
        if owner is not None:
            ranked.append(
                {**row, "job_id": owner[0], "turn_no": owner[1], "attempt_rank": owner[2]}
            )
    ranked.sort(key=lambda row: (row["turn_no"], row["attempt_rank"], int(row.get("seq") or 0)))
    tool_names = {
        str(row["call_id"]): str(row.get("name") or "")
        for row in ranked
        if row.get("event_type") == "tool_use" and row.get("call_id")
    }
    scope = RequestScope(
        attempt_id=located.point.attempt_id,
        seq=located.point.seq,
        usage_id=usage_id,
        previous_report_seq=located.previous_report_seq,
        report_after_response=located.report_after_response,
        boundary_key=located.boundary_key,
    )
    walk = walk_composition(
        scope=scope, turns=prompts, events=ranked, superseded=superseded, tool_names=tool_names
    )
    if snapshot is not None:
        native_status = str(snapshot.get("status") or "unknown")
    elif located.snapshot_status is not None:
        native_status = located.snapshot_status
    elif located.runtime in SNAPSHOT_RUNTIMES:
        native_status = "not_collected"
    else:
        native_status = "not_offered"
    return RequestComposition(
        thread_id=thread_id,
        request=located.point,
        runtime=located.runtime,
        native=dict(snapshot) if snapshot is not None else None,
        native_relation=(
            SnapshotRelation(
                event_id=located.snapshot_event_id, requests_after=located.requests_after
            )
            if located.snapshot_event_id is not None
            else None
        ),
        native_status=native_status,
        reconstructed=reconstructed(
            walk,
            boundary=located.boundary,
            copied_turns=located.copied_turns,
            events_capped=events_capped,
        ),
    )
