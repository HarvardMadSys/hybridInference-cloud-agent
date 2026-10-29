"""One conversation's context, assembled from its attempts' stored profiles.

:mod:`cloud_agent.context_profile` reduces each attempt; this module puts a
conversation's attempts in order and answers the questions the Context view
asks: how large the context was at each request, how it grew between turns,
when the CLI compacted it, which changes were largest, and what the stored
content is estimated to weigh. Every number keeps its kind (see the profile
module): nothing here calls the latest request the live context, forms a
percentage without a reported window, or calls a drop a compaction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime  # noqa: TC003 — Pydantic resolves the model annotations.
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.context_profile import (
    CONTENT_CATEGORIES,
    POINT_FIELDS,
    estimate_tokens,
    profile_is_current,
)

#: Request points one conversation's view returns. A longer conversation keeps
#: its latest points and says how many earlier ones it left out; its peak,
#: totals and turn rows still cover every request.
MAX_VIEW_POINTS = 5_000
#: Largest changes listed in each direction.
LARGEST_CHANGES = 5

Support = Literal["reported", "partly_reported", "not_reported"]
#: Job states in which the latest turn may still add requests.
_ACTIVE_JOB_STATES = frozenset({"queued", "waiting", "running"})


class RuntimeContextCapability(BaseModel):
    """What one runtime's stored events can say about context, and how often they did."""

    model_config = ConfigDict(extra="forbid")

    runtime: str
    turns: int = Field(ge=0)
    #: Per-request input and output as the CLI relayed them from the provider.
    request_usage: Support
    #: Whether the CLI marks a delegated agent's requests as its own.
    subagent_split: Literal["reported", "not_applicable", "not_reported"]
    compaction_events: Support
    #: The CLI's own before/after sizes for a compaction.
    compaction_sizes: Support
    context_window: Support
    #: Turns whose stored events reported a context window for the main model.
    window_reported_turns: int = Field(ge=0)
    #: No pinned CLI streams its live window occupancy.
    live_occupancy: Literal["not_collected"] = "not_collected"


class ContextPoint(BaseModel):
    """One model request of the conversation's own agent."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    job_id: str
    turn_no: int
    attempt_id: int
    seq: int
    event_id: int | None = None
    at: datetime | None = None
    prompt_tokens: int = Field(ge=0)
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    uncached_input_tokens: int | None = None
    output_tokens: int | None = None
    context_window: int | None = None
    model: str | None = None
    #: Change from the previous request of the conversation; None for the first.
    delta_tokens: int | None = None
    #: A reported compaction lies between the previous request and this one.
    after_compaction: bool = False


class LatestContext(BaseModel):
    """The latest observed request, and what is known about it."""

    model_config = ConfigDict(extra="forbid")

    point: ContextPoint
    #: Share of the reported window; None without one.
    window_share: float | None = None
    #: A compaction the CLI reported after this request: the observation is older.
    compacted_after: bool
    #: The conversation's latest turn is still running.
    turn_active: bool


class ContextTurn(BaseModel):
    """One turn: the requests its attempts made and how the context moved."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    turn_no: int
    created_at: datetime | None = None
    runtime: str | None = None
    model: str | None = None
    state: str | None = None
    #: A Fork's copy of its source's turn: its requests belong to the source.
    copied: bool = False
    attempts: int = Field(ge=0)
    requests: int = Field(ge=0)
    subagent_requests: int = Field(ge=0)
    first_prompt_tokens: int | None = None
    last_prompt_tokens: int | None = None
    peak_prompt_tokens: int | None = None
    #: Last request of this turn minus last request of the previous observed turn.
    change_tokens: int | None = None
    compactions: int = Field(ge=0)
    context_window: int | None = None
    #: First request index of this turn in ``requests``; None when it has none there.
    first_index: int | None = None


class ContextCompaction(BaseModel):
    """A compaction the CLI reported, with the requests observed around it."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    turn_no: int
    attempt_id: int
    seq: int
    event_id: int | None = None
    at: datetime | None = None
    source: str
    trigger: str | None = None
    #: The CLI's own sizes; None when it did not say.
    pre_tokens: int | None = None
    post_tokens: int | None = None
    #: None when the stored record cannot say whether it finished.
    completed: bool | None = None
    #: Observed request sizes just before and after it: context, not proof.
    observed_before: int | None = None
    observed_after: int | None = None


class ContextChange(BaseModel):
    """One of the largest request-to-request changes."""

    model_config = ConfigDict(extra="forbid")

    direction: Literal["growth", "drop"]
    delta_tokens: int
    from_index: int = Field(ge=0)
    to_index: int = Field(ge=0)
    #: A reported compaction lies between the two requests. A drop without one
    #: is listed as a drop and nothing more.
    compaction_between: bool


class ContextComposition(BaseModel):
    """Stored content priced at four characters per token.

    An estimate of what the conversation's own text weighs, never a reading of
    what the CLI put in its window: system prompts, tool schemas and anything
    the CLI did not stream are invisible here, and clipped previews undercount.
    """

    model_config = ConfigDict(extra="forbid")

    basis: Literal["since_last_compaction", "whole_conversation"]
    chars_per_token: int = 4
    categories: dict[str, int]
    estimated_tokens: int = Field(ge=0)
    truncated: bool
    #: Latest observed request minus the estimate, when the request is the
    #: larger and comes after the content counted; otherwise None.
    unattributed_tokens: int | None = None


class ContextCoverage(BaseModel):
    """How much of the conversation the numbers above stand on."""

    model_config = ConfigDict(extra="forbid")

    turns: int = Field(ge=0)
    attempts: int = Field(ge=0)
    attempts_observed: int = Field(ge=0)
    main_requests: int = Field(ge=0)
    subagent_requests: int = Field(ge=0)
    compactions: int = Field(ge=0)
    compactions_unknown_outcome: int = Field(ge=0)
    #: Request points left out to keep the response bounded (earliest first).
    points_omitted: int = Field(ge=0)
    #: Stored raw lines that should have carried an observation but were clipped.
    unparsed_raw: int = Field(ge=0)


class ThreadContextView(BaseModel):
    """The Context view of one conversation."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    generated_at: datetime
    capabilities: list[RuntimeContextCapability]
    latest: LatestContext | None = None
    peak: ContextPoint | None = None
    turns: list[ContextTurn]
    requests: list[ContextPoint]
    compactions: list[ContextCompaction]
    largest_changes: list[ContextChange]
    composition: ContextComposition | None = None
    coverage: ContextCoverage


#: What each pinned runtime's stored events can carry, checked against the
#: protocol of the pinned version (see the context insights design record).
#: ``partly`` marks a fact collected only since context insights shipped.
_RUNTIME_SUPPORT: dict[str, dict[str, str]] = {
    "claude-code": {
        "request_usage": "reported",
        "subagent_split": "reported",
        "compaction_events": "reported",
        "compaction_sizes": "partly_reported",
        "context_window": "partly_reported",
    },
    "codex": {
        "request_usage": "reported",
        "subagent_split": "reported",
        "compaction_events": "reported",
        "compaction_sizes": "not_reported",
        "context_window": "reported",
    },
    "pi": {
        "request_usage": "reported",
        "subagent_split": "not_applicable",
        "compaction_events": "reported",
        "compaction_sizes": "partly_reported",
        "context_window": "not_reported",
    },
    "opencode": {
        "request_usage": "reported",
        "subagent_split": "not_applicable",
        "compaction_events": "reported",
        "compaction_sizes": "not_reported",
        "context_window": "not_reported",
    },
    "kilo": {
        "request_usage": "reported",
        "subagent_split": "not_applicable",
        "compaction_events": "reported",
        "compaction_sizes": "not_reported",
        "context_window": "not_reported",
    },
}
_UNKNOWN_RUNTIME = {
    "request_usage": "not_reported",
    "subagent_split": "not_reported",
    "compaction_events": "not_reported",
    "compaction_sizes": "not_reported",
    "context_window": "not_reported",
}


def runtime_capability(runtime: str, *, turns: int, window_turns: int) -> RuntimeContextCapability:
    """The fixed capability of a runtime, with how many turns reported a window."""
    support = _RUNTIME_SUPPORT.get(runtime, _UNKNOWN_RUNTIME)
    return RuntimeContextCapability(
        runtime=runtime,
        turns=turns,
        window_reported_turns=window_turns,
        **support,  # type: ignore[arg-type]
    )


def _point(raw: Sequence[Any]) -> dict[str, Any]:
    return dict(zip(POINT_FIELDS, raw, strict=False))


class _Assembly:
    """The conversation's attempts, walked once in turn and sequence order."""

    def __init__(self, jobs: Sequence[Mapping[str, Any]]) -> None:
        self.jobs = jobs
        self.points: list[ContextPoint] = []
        self.compactions: list[ContextCompaction] = []
        self.turns: list[ContextTurn] = []
        self.coverage = ContextCoverage(
            turns=len(jobs),
            attempts=0,
            attempts_observed=0,
            main_requests=0,
            subagent_requests=0,
            compactions=0,
            compactions_unknown_outcome=0,
            points_omitted=0,
            unparsed_raw=0,
        )
        self.peak: ContextPoint | None = None
        self.content_total = dict.fromkeys(CONTENT_CATEGORIES, 0)
        self.content_after: dict[str, int] | None = None
        self.user_total = 0
        self.user_after = 0
        self.truncated = False
        self.window_turns: dict[str, int] = {}
        self.last_compaction_index = -1
        self._pending_compaction = False
        self._awaiting_after: list[ContextCompaction] = []

    def add_job(self, job: Mapping[str, Any], attempts: Sequence[Mapping[str, Any]]) -> None:
        turn = ContextTurn(
            job_id=str(job["job_id"]),
            turn_no=int(job.get("turn_no") or 1),
            created_at=job.get("created_at"),
            runtime=job.get("runtime"),
            model=job.get("model"),
            state=job.get("state"),
            copied=bool(job.get("copied")),
            attempts=len(attempts),
            requests=0,
            subagent_requests=0,
            compactions=0,
        )
        prompt_chars = int(job.get("prompt_chars") or 0)
        self.user_total += prompt_chars
        self.user_after += prompt_chars
        for attempt in attempts:
            self._add_attempt(turn, attempt)
        if turn.context_window is not None and turn.runtime:
            self.window_turns[turn.runtime] = self.window_turns.get(turn.runtime, 0) + 1
        previous = next((t for t in reversed(self.turns) if t.last_prompt_tokens is not None), None)
        if previous is not None and turn.last_prompt_tokens is not None:
            turn.change_tokens = turn.last_prompt_tokens - (previous.last_prompt_tokens or 0)
        self.turns.append(turn)

    def _add_attempt(self, turn: ContextTurn, attempt: Mapping[str, Any]) -> None:
        self.coverage.attempts += 1
        profile = attempt.get("profile")
        if not isinstance(profile, Mapping) or not profile_is_current(profile):
            return
        attempt_id = int(attempt["attempt_id"])
        summary = profile.get("summary") or {}
        if summary.get("main_requests") or profile.get("compactions"):
            self.coverage.attempts_observed += 1
        self.coverage.points_omitted += int(profile.get("requests_omitted") or 0)
        self.coverage.unparsed_raw += int(profile.get("unparsed_raw") or 0)
        subagent = int(profile.get("subagent_requests") or 0)
        turn.subagent_requests += subagent
        self.coverage.subagent_requests += subagent
        compactions = sorted(profile.get("compactions") or [], key=lambda c: int(c.get("seq") or 0))
        requests = [_point(raw) for raw in profile.get("requests") or []]
        self._merge(turn, attempt_id, requests, compactions)
        self._content(profile.get("content") or {})

    def _merge(
        self,
        turn: ContextTurn,
        attempt_id: int,
        requests: list[dict[str, Any]],
        compactions: list[Mapping[str, Any]],
    ) -> None:
        """Interleave one attempt's requests and compactions by sequence."""
        pending = list(compactions)
        for request in requests:
            while pending and int(pending[0].get("seq") or 0) < int(request["seq"]):
                self._compaction(turn, attempt_id, pending.pop(0))
            self._request(turn, attempt_id, request)
        for compaction in pending:
            self._compaction(turn, attempt_id, compaction)

    def _request(self, turn: ContextTurn, attempt_id: int, raw: dict[str, Any]) -> None:
        previous = self.points[-1] if self.points else None
        point = ContextPoint(
            index=len(self.points),
            job_id=turn.job_id,
            turn_no=turn.turn_no,
            attempt_id=attempt_id,
            delta_tokens=raw["prompt_tokens"] - previous.prompt_tokens if previous else None,
            after_compaction=self._pending_compaction,
            **raw,
        )
        self._pending_compaction = False
        for compaction in self._awaiting_after:
            compaction.observed_after = point.prompt_tokens
        self._awaiting_after = []
        self.points.append(point)
        self.coverage.main_requests += 1
        if turn.first_index is None:
            turn.first_index = point.index
        turn.requests += 1
        turn.first_prompt_tokens = (
            turn.first_prompt_tokens if turn.requests > 1 else point.prompt_tokens
        )
        turn.last_prompt_tokens = point.prompt_tokens
        turn.peak_prompt_tokens = max(turn.peak_prompt_tokens or 0, point.prompt_tokens)
        if point.context_window:
            turn.context_window = point.context_window
        if self.peak is None or point.prompt_tokens > self.peak.prompt_tokens:
            self.peak = point

    def _compaction(self, turn: ContextTurn, attempt_id: int, raw: Mapping[str, Any]) -> None:
        completed = raw.get("completed")
        if completed is False:
            return
        compaction = ContextCompaction(
            job_id=turn.job_id,
            turn_no=turn.turn_no,
            attempt_id=attempt_id,
            seq=int(raw.get("seq") or 0),
            event_id=raw.get("event_id"),
            at=raw.get("at"),
            source=str(raw.get("source") or "unknown"),
            trigger=raw.get("trigger"),
            pre_tokens=raw.get("pre_tokens"),
            post_tokens=raw.get("post_tokens"),
            completed=completed if isinstance(completed, bool) else None,
            observed_before=self.points[-1].prompt_tokens if self.points else None,
        )
        self.compactions.append(compaction)
        self._awaiting_after.append(compaction)
        self.coverage.compactions += 1
        if completed is None:
            self.coverage.compactions_unknown_outcome += 1
        turn.compactions += 1
        self._pending_compaction = True
        self.last_compaction_index = len(self.points)
        # The turn's own prompt went in before the compaction.
        self.user_after = 0

    def _content(self, content: Mapping[str, Any]) -> None:
        total = content.get("total") or {}
        after = content.get("after_compaction")
        for category in CONTENT_CATEGORIES:
            self.content_total[category] += int(total.get(category) or 0)
        if isinstance(after, Mapping):
            self.content_after = {c: int(after.get(c) or 0) for c in CONTENT_CATEGORIES}
        elif self.content_after is not None:
            for category in CONTENT_CATEGORIES:
                self.content_after[category] += int(total.get(category) or 0)
        self.truncated = self.truncated or bool(content.get("truncated"))


def _largest_changes(points: Sequence[ContextPoint]) -> list[ContextChange]:
    changes = [
        ContextChange(
            direction="growth" if (point.delta_tokens or 0) > 0 else "drop",
            delta_tokens=point.delta_tokens or 0,
            from_index=point.index - 1,
            to_index=point.index,
            compaction_between=point.after_compaction,
        )
        for point in points
        if point.delta_tokens
    ]
    growth = sorted((c for c in changes if c.delta_tokens > 0), key=lambda c: -c.delta_tokens)
    drops = sorted((c for c in changes if c.delta_tokens < 0), key=lambda c: c.delta_tokens)
    return [*growth[:LARGEST_CHANGES], *drops[:LARGEST_CHANGES]]


def _composition(assembly: _Assembly, latest: ContextPoint | None) -> ContextComposition | None:
    since_compaction = bool(assembly.compactions)
    content = (
        assembly.content_after
        if since_compaction and assembly.content_after is not None
        else assembly.content_total
    )
    user = assembly.user_after if since_compaction else assembly.user_total
    categories = {"user": estimate_tokens(user)}
    categories.update({c: estimate_tokens(content[c]) for c in CONTENT_CATEGORIES})
    estimated = sum(categories.values())
    if estimated == 0 and latest is None:
        return None
    unattributed = None
    if (
        latest is not None
        and latest.prompt_tokens > estimated
        and not (since_compaction and latest.index < assembly.last_compaction_index)
    ):
        unattributed = latest.prompt_tokens - estimated
    return ContextComposition(
        basis="since_last_compaction" if since_compaction else "whole_conversation",
        categories=categories,
        estimated_tokens=estimated,
        truncated=assembly.truncated,
        unattributed_tokens=unattributed,
    )


def assemble_thread_context(
    *,
    thread_id: str,
    generated_at: datetime,
    jobs: Sequence[Mapping[str, Any]],
    attempts: Sequence[Mapping[str, Any]],
) -> ThreadContextView:
    """Build the Context view from a conversation's jobs and attempt profiles.

    ``jobs`` are in turn order with ``job_id``, ``turn_no``, ``created_at``,
    ``runtime``, ``model``, ``state`` and ``prompt_chars``; ``attempts`` carry
    ``attempt_id``, ``job_id`` and their stored ``profile``, in attempt order.
    """
    by_job: dict[str, list[Mapping[str, Any]]] = {}
    for attempt in attempts:
        by_job.setdefault(str(attempt["job_id"]), []).append(attempt)
    assembly = _Assembly(jobs)
    for job in jobs:
        assembly.add_job(job, by_job.get(str(job["job_id"]), []))
    points = assembly.points
    latest_point = points[-1] if points else None
    latest = None
    if latest_point is not None:
        window = latest_point.context_window
        last_job = jobs[-1] if jobs else {}
        latest = LatestContext(
            point=latest_point,
            window_share=latest_point.prompt_tokens / window if window else None,
            compacted_after=assembly.last_compaction_index > latest_point.index,
            turn_active=str(last_job.get("state") or "") in _ACTIVE_JOB_STATES,
        )
    runtimes: dict[str, int] = {}
    for job in jobs:
        runtime = str(job.get("runtime") or "unknown")
        runtimes[runtime] = runtimes.get(runtime, 0) + 1
    omitted_here = max(0, len(points) - MAX_VIEW_POINTS)
    coverage = assembly.coverage
    coverage.points_omitted += omitted_here
    return ThreadContextView(
        thread_id=thread_id,
        generated_at=generated_at,
        capabilities=[
            runtime_capability(name, turns=count, window_turns=assembly.window_turns.get(name, 0))
            for name, count in runtimes.items()
        ],
        latest=latest,
        peak=assembly.peak,
        turns=assembly.turns,
        requests=points[omitted_here:],
        compactions=assembly.compactions,
        largest_changes=_largest_changes(points),
        composition=_composition(assembly, latest_point),
        coverage=coverage,
    )
