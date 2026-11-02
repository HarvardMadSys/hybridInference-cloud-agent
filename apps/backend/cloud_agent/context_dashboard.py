"""The Context dashboard's per-request categories and session token summary.

Layout and charts adapt bowenliang123/dsh-context (Apache-2.0, commit
32982aa6aa2c70fbda6b81281de98a72ea625001); its accounting does not carry
over: dsh-context sees each assembled request, this platform sees what the
agent CLIs stream. Two families of numbers stay apart here:

- **Recorded content** (estimate, ``chars_cjk_v1``): stored text by category.
  A request's vector is what the conversation recorded before it -- the turn
  prompts and what the platform wrapped them in, then each attempt's content
  up to the request (:mod:`cloud_agent.context_running`). A completed
  compaction starts the carried content over; a retried attempt's content is
  not carried into the next turn (the kept attempt's is). CLI system prompts
  and tool schemas are never stored, so they have no number at all.
- **Provider actuals** (exact): every request's reported input, cache reads
  and writes, and output, summed over every attempt, the delegated agents'
  apart.

Nothing is scaled to make one family fit the other.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.context_profile import profile_is_current
from cloud_agent.context_running import POINT_CATEGORIES
from cloud_agent_shared.text_tokens import ESTIMATE_BASIS

_INDEX = {key: index for index, key in enumerate(POINT_CATEGORIES)}
#: Profile content categories, as the dashboard names them.
_CONTENT_KEY = {
    "user_messages": "user",
    "injected": "inject",
    "skills": "skill",
    "compaction_summary": "summary",
    "assistant": "assistant",
    "reasoning": "reasoning",
    "tool_calls": "tool_calls",
    "tool_results": "tool_results",
}
#: Categories every request carries but no stored record can size.
NOT_RECORDED: tuple[str, ...] = ("system_prompt", "tool_schemas")


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _profile(attempt: Mapping[str, Any]) -> Mapping[str, Any] | None:
    profile = attempt.get("profile")
    return profile if isinstance(profile, Mapping) and profile_is_current(profile) else None


def platform_vector(profile: Mapping[str, Any] | None) -> list[int]:
    """What the platform wrapped this attempt's prompt in: skills apart, the rest injected."""
    vector = [0] * len(POINT_CATEGORIES)
    for event in (profile or {}).get("events") or []:
        if not isinstance(event, Mapping) or event.get("kind") != "injection":
            continue
        source = str(event.get("source") or "")
        if not source.startswith("platform_"):
            continue
        key = "skill" if source == "platform_skills" else "inject"
        vector[_INDEX[key]] += _int(event.get("tokens"))
    return vector


def _add(left: Sequence[int], right: Sequence[int]) -> list[int]:
    return [a + b for a, b in zip(left, right, strict=True)]


class CategoryCarry:
    """Walks a conversation's turns, giving each request its recorded categories."""

    def __init__(self) -> None:
        #: What the conversation carries into the next turn.
        self.carried = [0] * len(POINT_CATEGORIES)
        self._job_base = list(self.carried)

    def start_job(self, job: Mapping[str, Any]) -> None:
        """A turn begins: its own prompt joins what is carried."""
        prompt = [0] * len(POINT_CATEGORIES)
        prompt[_INDEX["user"]] = _int(job.get("prompt_tokens"))
        self._job_base = _add(self.carried, prompt)

    def attempt_base(self, profile: Mapping[str, Any] | None) -> list[int]:
        """One attempt starts from the turn's base and its own platform wrapping."""
        return _add(self._job_base, platform_vector(profile))

    @staticmethod
    def point(base: Sequence[int], raw: Mapping[str, Any]) -> list[int] | None:
        """A request's vector; None when the profile kept none for it."""
        local = raw.get("categories")
        if not isinstance(local, list) or len(local) != len(POINT_CATEGORIES):
            return None
        local = [_int(value) for value in local]
        return local if raw.get("compacted_in_attempt") else _add(base, local)

    def end_job(self, kept: Mapping[str, Any] | None) -> None:
        """The turn's kept (latest) attempt decides what the next turn starts from."""
        profile = _profile(kept) if kept is not None else None
        base = self.attempt_base(profile)
        tail = (profile or {}).get("content_tail")
        if not isinstance(tail, Mapping):
            self.carried = base
            return
        values = tail.get("categories") or {}
        local = [_int(values.get(key)) for key in POINT_CATEGORIES]
        self.carried = local if tail.get("compacted") else _add(base, local)


class SessionCategoryShare(BaseModel):
    """One category of recorded content, estimated."""

    model_config = ConfigDict(extra="forbid")

    key: str
    tokens: int = Field(ge=0)
    items: int = Field(ge=0)


class ProviderTokens(BaseModel):
    """What the providers reported, summed over every request (exact)."""

    model_config = ConfigDict(extra="forbid")

    requests: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    output_unreported: int = Field(ge=0)
    subagent_requests: int = Field(ge=0)
    subagent_input_tokens: int = Field(ge=0)
    subagent_output_tokens: int = Field(ge=0)
    #: Cache reads over all input; None without input.
    cache_hit: float | None = None


class SessionTokens(BaseModel):
    """The conversation's tokens two ways that are never added together."""

    model_config = ConfigDict(extra="forbid")

    basis: str = ESTIMATE_BASIS
    #: Each stored item once: turn prompts, what the platform and the CLI
    #: injected (skills apart), compaction summaries, answers, reasoning,
    #: tool calls and results. The kept attempt of each turn only.
    recorded: list[SessionCategoryShare]
    recorded_total: int = Field(ge=0)
    #: Every request carries these, but no stored record sizes them.
    not_recorded: list[str] = Field(default_factory=lambda: list(NOT_RECORDED))
    #: Turns whose kept attempt has a current profile, and turns in all.
    turns_read: int = Field(ge=0)
    turns: int = Field(ge=0)
    truncated: bool = False
    provider: ProviderTokens
    reported_by: Literal["cli_relayed_provider_usage"] = "cli_relayed_provider_usage"


def session_tokens(
    jobs: Sequence[Mapping[str, Any]],
    by_job: Mapping[str, Sequence[Mapping[str, Any]]],
) -> SessionTokens:
    """Recorded content (kept attempts, once) beside the providers' totals (every attempt)."""
    tokens = dict.fromkeys(POINT_CATEGORIES, 0)
    items = dict.fromkeys(POINT_CATEGORIES, 0)
    sums = dict.fromkeys(
        (
            "requests",
            "prompt_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "output_tokens",
            "output_unreported",
        ),
        0,
    )
    sub = {"requests": 0, "prompt_tokens": 0, "output_tokens": 0}
    turns_read = 0
    truncated = False
    for job in jobs:
        prompt = _int(job.get("prompt_tokens"))
        tokens["user"] += prompt
        items["user"] += 1 if prompt else 0
        attempts = list(by_job.get(str(job["job_id"]), []))
        for attempt in attempts:
            totals = (_profile(attempt) or {}).get("totals") or {}
            for key in sums:
                sums[key] += _int((totals.get("main") or {}).get(key))
            for key in sub:
                sub[key] += _int((totals.get("subagent") or {}).get(key))
        kept = _profile(attempts[-1]) if attempts else None
        if kept is None:
            continue
        turns_read += 1
        for index, value in enumerate(platform_vector(kept)):
            tokens[POINT_CATEGORIES[index]] += value
            items[POINT_CATEGORIES[index]] += 1 if value else 0
        content = kept.get("content") or {}
        truncated = truncated or bool(content.get("truncated"))
        for name, share in (content.get("categories") or {}).items():
            category = _CONTENT_KEY.get(name)
            if category is not None and isinstance(share, Mapping):
                tokens[category] += _int(share.get("tokens"))
                items[category] += _int(share.get("items"))
    input_tokens = sums["prompt_tokens"]
    return SessionTokens(
        recorded=[
            SessionCategoryShare(key=key, tokens=tokens[key], items=items[key])
            for key in POINT_CATEGORIES
            if tokens[key]
        ],
        recorded_total=sum(tokens.values()),
        turns_read=turns_read,
        turns=len(jobs),
        truncated=truncated,
        provider=ProviderTokens(
            requests=sums["requests"],
            input_tokens=input_tokens,
            cache_read_tokens=sums["cache_read_tokens"],
            cache_write_tokens=sums["cache_write_tokens"],
            output_tokens=sums["output_tokens"],
            output_unreported=sums["output_unreported"],
            subagent_requests=sub["requests"],
            subagent_input_tokens=sub["prompt_tokens"],
            subagent_output_tokens=sub["output_tokens"],
            cache_hit=sums["cache_read_tokens"] / input_tokens if input_tokens else None,
        ),
    )


def step_durations(points: Sequence[Any], spans: Mapping[int, int]) -> dict[int, int]:
    """Event-derived step time per request index, within its own attempt only.

    A request's step runs from its usage event to the next request's in the
    same attempt; the attempt's last request runs to the attempt's last event
    (``spans``: attempt id to its last event, ms). Receipt times on the
    platform: they include the answer, tool runs and the harness between.
    Never across attempts, so a retry's wait is nobody's step.
    """
    durations: dict[int, int] = {}
    for current, following in zip(points, [*points[1:], None], strict=False):
        start = _epoch_ms(current.at)
        if start is None:
            continue
        if following is not None and following.attempt_id == current.attempt_id:
            end = _epoch_ms(following.at)
        else:
            end = spans.get(current.attempt_id)
        if end is not None and end >= start:
            durations[current.index] = end - start
    return durations


def _epoch_ms(value: Any) -> int | None:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp() * 1000)
        except ValueError:
            return None
    return None
