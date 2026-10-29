"""Derive one attempt's context profile from its stored event ledger.

A *context profile* says how large each model request's input was, when the
CLI compacted its context, and roughly what the stored conversation content
weighs. Like :mod:`cloud_agent.thread_stats` it adds no measurement of its own:
it reads rows the platform already writes, so a conversation that ran before
this module existed has the profile its runtime's events allow.

Four kinds of number are kept apart, because they answer different questions:

- **Request usage** is what the CLI relayed from the provider for one request:
  uncached input, cache reads, cache writes and output.
- **Prompt tokens** is that request's whole input (uncached + cache read +
  cache write). It is the size of the context the model was given at that
  request. The latest one is the *latest observed* context, never the live
  occupancy of the CLI's window, which no pinned CLI streams.
- **Context window** is the capacity a CLI reported for the model. It is kept
  only when reported; a percentage is never formed from a guess.
- **Content estimate** is stored conversation text priced at four characters
  per token (the reference plugin's density). It is an estimate and, where a
  preview was clipped, an undercount.

A compaction is only ever a compaction the CLI reported. A falling request size
is not one, and nothing here turns a drop into a compaction.

Each runtime reports these differently; the readers below are per protocol:

- Claude Code: ``usage`` + ``usage_id`` on assistant block events (one response
  repeats its usage on each block), ``compact_boundary`` lifecycle events, and
  ``context_windows`` on the result (collected since context insights).
- Codex: the app-server's ``thread/tokenUsage/updated`` notifications, stored
  whole as raw events, whose ``last`` is the latest request and which name the
  model's context window; ``contextCompaction`` items.
- pi: one ``usage`` event per assistant message, and ``compact_boundary``
  lifecycle events for completed compactions (collected since context
  insights; stored raw before).
- OpenCode and Kilo: ``step-finish`` parts and ``session.compacted`` events in
  the bridge's stored raw lines.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from cloud_agent_shared.text_tokens import ESTIMATE_BASIS, estimate_from_counts

#: Rollups name their reducer; a semantic change bumps it so every stored
#: profile is rebuilt from the ledger instead of being served stale.
CONTEXT_PROFILE_VERSION = 4
#: Request points one attempt keeps. Past it, the first and last halves are
#: kept and the number left out is recorded; peaks and counts still see every
#: request.
MAX_ATTEMPT_POINTS = 2_000
#: Compactions one attempt keeps in detail. Counted beyond that.
MAX_ATTEMPT_COMPACTIONS = 200
#: Turn-start context reads one attempt notes (one per turn is usual).
MAX_ATTEMPT_SNAPSHOTS = 20
#: Context events one attempt keeps (injections, prunes, modes); counted beyond.
MAX_ATTEMPT_EVENTS = 200
#: Names one platform injection lists (skills, attachments); counted beyond.
MAX_INJECTION_NAMES = 20
#: The reference plugin's density, and the harness token meter it mirrors.
CHARS_PER_TOKEN = 4

ContentCategory = Literal[
    "user_messages",
    "assistant",
    "reasoning",
    "tool_calls",
    "tool_results",
    "compaction_summary",
    "injected",
]
CONTENT_CATEGORIES: tuple[ContentCategory, ...] = (
    "user_messages",
    "assistant",
    "reasoning",
    "tool_calls",
    "tool_results",
    "compaction_summary",
    "injected",
)
#: Lifecycle phases whose stored text the conversation carries forward.
_CARRIED_PHASES: dict[str, ContentCategory] = {
    "compaction_summary": "compaction_summary",
    "context_injection": "injected",
}
#: Claude Code's plan-mode tools: entering and leaving it are context events.
_PLAN_TOOLS = {"EnterPlanMode": "plan_entered", "ExitPlanMode": "plan_exit_requested"}
_CONTENT_EVENT: dict[str, ContentCategory] = {
    "message": "assistant",
    "thinking": "reasoning",
    "tool_use": "tool_calls",
    "tool_result": "tool_results",
    # Messages the owner sent while the turn ran; the turn's own prompt is
    # the job's, counted from ``task_prompt``.
    "user_message": "user_messages",
}
#: Where each runtime's evidence came from, as a profile names it.
RequestSource = Literal["claude_usage", "codex_token_usage", "pi_usage", "opencode_step"]
CompactionSource = Literal[
    "cli_compact_boundary",
    "codex_context_compaction",
    "codex_thread_compacted",
    "pi_compaction_raw",
    "opencode_session_compacted",
]

_CODEX_COMPACTION_ITEM = "contextCompaction"
_PI_RAW_COMPACTION = re.compile(r"^unknown type '(compaction_end)'$")
_RAW_TRIGGER = re.compile(r'"reason"\s*:\s*"(manual|threshold|overflow)"')


def estimate_tokens(chars: int) -> int:
    """Price stored characters at the reference density, rounding up."""
    return -(-max(0, chars) // CHARS_PER_TOKEN)


def _count(value: Any) -> int | None:
    """A non-negative whole count, or None for anything else (never a guess)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0 or value != int(value):
        return None
    return int(value)


def _label(value: Any, limit: int) -> str:
    """A short identifier from a stored payload: text only, bounded."""
    return value.strip()[:limit] if isinstance(value, str) else ""


def _iso(value: datetime | str | None) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) else None


@dataclass
class _Request:
    """One model request, before it is written into the profile."""

    seq: int
    event_id: int | None
    at: str | None
    prompt: int
    cache_read: int | None
    cache_write: int | None
    uncached: int | None
    output: int | None
    window: int | None = None
    model: str | None = None

    def point(self) -> list[Any]:
        """The compact array a stored profile holds per request."""
        return [
            self.seq,
            self.event_id,
            self.at,
            self.prompt,
            self.cache_read,
            self.cache_write,
            self.uncached,
            self.output,
            self.window,
            self.model,
        ]


#: The fields of a stored request point, in order.
POINT_FIELDS = (
    "seq",
    "event_id",
    "at",
    "prompt_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "context_window",
    "model",
)


def _anthropic_request(usage: Mapping[str, Any]) -> tuple[int, int | None, int | None, int] | None:
    """(prompt, cache read, cache write, uncached) from Anthropic-shaped usage."""
    uncached = _count(usage.get("input_tokens"))
    if uncached is None:
        return None
    read = _count(usage.get("cache_read_input_tokens"))
    write = _count(usage.get("cache_creation_input_tokens"))
    return uncached + (read or 0) + (write or 0), read, write, uncached


def _split_request(
    *, uncached: Any, read: Any, write: Any
) -> tuple[int, int | None, int | None, int] | None:
    """(prompt, cache read, cache write, uncached) where input excludes the cache."""
    base = _count(uncached)
    if base is None:
        return None
    cache_read, cache_write = _count(read), _count(write)
    return base + (cache_read or 0) + (cache_write or 0), cache_read, cache_write, base


@dataclass
class _Compaction:
    seq: int
    event_id: int | None
    at: str | None
    source: CompactionSource
    trigger: str | None = None
    pre_tokens: int | None = None
    post_tokens: int | None = None
    #: None when the stored record cannot say whether it finished.
    completed: bool | None = True
    turn_key: str | None = None

    def record(self) -> dict[str, Any]:
        """The stored form, without the reducer-internal turn key."""
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "at": self.at,
            "source": self.source,
            "trigger": self.trigger,
            "pre_tokens": self.pre_tokens,
            "post_tokens": self.post_tokens,
            "completed": self.completed,
        }


def _report_fields(report: Any) -> dict[str, Any]:
    """Read the trigger and sizes from a stored ``compaction`` report."""
    if not isinstance(report, Mapping):
        return {}
    trigger = report.get("trigger")
    return {
        "trigger": trigger if isinstance(trigger, str) and trigger else None,
        "pre_tokens": _count(report.get("pre_tokens")),
        "post_tokens": _count(report.get("post_tokens")),
    }


class ContextProfileAccumulator:
    """Reduce one attempt's events, added in ledger sequence order.

    Only what can cross a page boundary is retained: the requests and
    compactions found so far, per-request identities for deduplication, and
    per-category character counts.
    """

    def __init__(self, *, attempt_id: int, job_id: str) -> None:
        self.attempt_id = attempt_id
        self.job_id = job_id
        self.sources: set[str] = set()
        self.main_model: str | None = None
        self.windows: dict[str, int] = {}
        self.subagent_requests = 0
        self.subagent_peak: int | None = None
        self.unparsed_raw = 0
        self._requests: list[_Request] = []
        self._by_usage_id: dict[str, _Request] = {}
        self._last_codex: Any = None
        self._seen_parts: set[str] = set()
        self._compactions: list[_Compaction] = []
        self._compaction_items: set[str] = set()
        self._codex_compacted_turns: set[str] = set()
        self._opencode_trigger: str | None = None
        #: Recorded content per category: chars, its ASCII and CJK counts, items.
        self._content = {category: [0, 0, 0, 0] for category in CONTENT_CATEGORIES}
        self.truncated = False
        #: Context events found so far (see ``cloud_agent.context_events``).
        self._events: list[dict[str, Any]] = []
        self._events_seen = 0
        #: The CLI's own context snapshots of this attempt, without entries.
        self._snapshots: list[dict[str, Any]] = []
        self._snapshot_window: int | None = None

    # -- entry -------------------------------------------------------------

    def extend(self, events: Iterable[Mapping[str, Any]]) -> None:
        """Fold a page of events into the attempt."""
        for event in events:
            self.add(event)

    def add(self, event: Mapping[str, Any]) -> None:
        """Fold one next-sequence event into the attempt."""
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        event_type = str(event.get("event_type") or "")
        meta = (int(event.get("seq") or 0), _count(event.get("id")), _iso(event.get("created_at")))
        subagent = bool(payload.get("parent_tool_use_id") or payload.get("agent_thread_id"))
        if event_type == "lifecycle":
            if not (subagent and payload.get("phase") in {"context_injection", "context_prune"}):
                # A delegated agent's own context is not the conversation's.
                self._lifecycle(payload, meta)
            carried = _CARRIED_PHASES.get(str(payload.get("phase") or ""))
            if carried is None and payload.get("phase") == "compact_boundary":
                # pi's completed compaction carries its summary on the event.
                carried = "compaction_summary" if payload.get("text_chars") else None
            if carried is not None and not subagent:
                self._add_content(carried, payload, chars_key="text_chars")
        elif event_type == "error":
            self._windows(payload.get("context_windows"))
        elif event_type == "usage":
            self._pi_usage(payload, meta)
        elif event_type == "raw":
            self._raw(payload, meta, subagent=subagent)
        if event_type in _CONTENT_EVENT:
            self._claude_usage(payload, meta, subagent=subagent)
            if not subagent:
                self._add_content(_CONTENT_EVENT[event_type], payload)
        tool = payload.get("tool_name")
        if event_type == "tool_use" and tool in _PLAN_TOOLS and not subagent:
            self._event(meta, kind="mode", source="claude_plan_tool", mode=_PLAN_TOOLS[tool])

    # -- requests ----------------------------------------------------------

    def _record(self, request: _Request, *, subagent: bool) -> None:
        if subagent:
            self.subagent_requests += 1
            peak = self.subagent_peak
            self.subagent_peak = request.prompt if peak is None else max(peak, request.prompt)
            return
        self._requests.append(request)

    def _claude_usage(
        self,
        payload: Mapping[str, Any],
        meta: tuple[int, int | None, str | None],
        *,
        subagent: bool,
    ) -> None:
        usage = payload.get("usage")
        if not isinstance(usage, Mapping) or payload.get("usage_scope") == "attempt":
            return
        parts = _anthropic_request(usage)
        if parts is None:
            return
        self.sources.add("claude_usage")
        usage_id = payload.get("usage_id")
        key = f"{'sub' if subagent else 'main'}:{usage_id}" if usage_id else None
        known = self._by_usage_id.get(key) if key else None
        output = _count(usage.get("output_tokens"))
        if known is not None:
            # Later blocks of the same response repeat its usage; the output
            # count may have grown, the input cannot have.
            if output is not None:
                known.output = max(known.output or 0, output)
            return
        prompt, read, write, uncached = parts
        request = _Request(meta[0], meta[1], meta[2], prompt, read, write, uncached, output)
        if key:
            self._by_usage_id[key] = request
        self._record(request, subagent=subagent)

    def _pi_usage(
        self, payload: Mapping[str, Any], meta: tuple[int, int | None, str | None]
    ) -> None:
        parts = _split_request(
            uncached=payload.get("input_tokens"),
            read=payload.get("cache_read_tokens"),
            write=payload.get("cache_write_tokens"),
        )
        if parts is None:
            return
        self.sources.add("pi_usage")
        prompt, read, write, uncached = parts
        model = payload.get("model")
        self._record(
            _Request(
                meta[0],
                meta[1],
                meta[2],
                prompt,
                read,
                write,
                uncached,
                _count(payload.get("output_tokens")),
                model=model[:256] if isinstance(model, str) and model else None,
            ),
            subagent=False,
        )

    def _codex_usage(
        self,
        params: Mapping[str, Any],
        meta: tuple[int, int | None, str | None],
        *,
        subagent: bool,
    ) -> None:
        usage = params.get("tokenUsage")
        if not isinstance(usage, Mapping):
            return
        last = usage.get("last")
        if not isinstance(last, Mapping):
            return
        identity = (
            subagent,
            json.dumps(usage.get("total"), sort_keys=True),
            json.dumps(last, sort_keys=True),
        )
        if identity == self._last_codex:
            # Codex resends the same totals without a new request (for one, on
            # a rate-limit update); only a changed total is another request.
            return
        self._last_codex = identity
        prompt = _count(last.get("inputTokens"))
        if prompt is None:
            return
        self.sources.add("codex_token_usage")
        read = _count(last.get("cachedInputTokens"))
        write = _count(last.get("cacheWriteInputTokens"))
        window = _count(usage.get("modelContextWindow")) or None
        # OpenAI's input already counts cached input; cache writes are
        # reported beside it, not inside.
        self._record(
            _Request(
                meta[0],
                meta[1],
                meta[2],
                prompt + (write or 0),
                read,
                write,
                prompt - (read or 0) if read is not None and read <= prompt else None,
                _count(last.get("outputTokens")),
                window=window,
            ),
            subagent=subagent,
        )

    def _opencode_step(
        self, part: Mapping[str, Any], meta: tuple[int, int | None, str | None]
    ) -> None:
        part_id = str(part.get("id") or "")
        if part_id and part_id in self._seen_parts:
            return
        if part_id:
            self._seen_parts.add(part_id)
        tokens = part.get("tokens")
        if not isinstance(tokens, Mapping):
            return
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, Mapping) else {}
        parts = _split_request(
            uncached=tokens.get("input"), read=cache.get("read"), write=cache.get("write")
        )
        if parts is None:
            return
        self.sources.add("opencode_step")
        prompt, read, write, uncached = parts
        output = _count(tokens.get("output"))
        reasoning = _count(tokens.get("reasoning"))
        if output is not None and reasoning is not None:
            output += reasoning
        self._record(
            _Request(meta[0], meta[1], meta[2], prompt, read, write, uncached, output),
            subagent=False,
        )

    # -- compactions and windows --------------------------------------------

    def _compacted(self, compaction: _Compaction) -> None:
        self._compactions.append(compaction)

    def _windows(self, value: Any) -> None:
        if not isinstance(value, Mapping):
            return
        for model, window in value.items():
            count = _count(window)
            if isinstance(model, str) and count:
                self.windows[model] = count

    def _lifecycle(
        self, payload: Mapping[str, Any], meta: tuple[int, int | None, str | None]
    ) -> None:
        phase = payload.get("phase")
        if phase == "started":
            self._platform_injections(payload.get("context_injections"), meta)
        elif phase == "context_injection":
            injection = payload.get("injection")
            injection = injection if isinstance(injection, Mapping) else {}
            self._event(
                meta,
                kind="injection",
                source=_label(injection.get("source"), 64) or "cli",
                label=_label(injection.get("label"), 200) or None,
                chars=_count(injection.get("chars")),
                tokens=self._estimate(payload),
            )
        elif phase == "context_prune":
            prune = payload.get("prune")
            prune = prune if isinstance(prune, Mapping) else {}
            self._event(
                meta,
                kind="prune",
                source=_label(prune.get("source"), 64) or "cli",
                label=_label(prune.get("tool"), 200) or None,
                call_id=_label(prune.get("call_id"), 256) or None,
                chars=_count(prune.get("chars")),
            )
        if phase == "init":
            model = payload.get("model")
            if isinstance(model, str) and model and self.main_model is None:
                self.main_model = model[:256]
            mode = payload.get("permission_mode")
            if isinstance(mode, str) and mode:
                self._event(meta, kind="mode", source="claude_init", mode=mode[:64])
            # Claude's start-up inventory (tool and MCP server names).
            self._snapshot(payload.get("snapshot"), meta)
        elif phase == "result":
            self._windows(payload.get("context_windows"))
        elif phase == "context_snapshot":
            self._snapshot(payload.get("snapshot"), meta)
        elif phase == "compact_boundary":
            # Claude Code's own boundary, and pi's completed compaction (which
            # says ``completed`` alongside); either is a compaction that happened.
            completed = payload.get("completed")
            self._compacted(
                _Compaction(
                    *meta,
                    source="cli_compact_boundary",
                    completed=completed if isinstance(completed, bool) else True,
                    **_report_fields(payload.get("compaction")),
                )
            )

    def _snapshot(self, report: Any, meta: tuple[int, int | None, str | None]) -> None:
        """Note a CLI context snapshot; its entries stay on the event."""
        if not isinstance(report, Mapping) or len(self._snapshots) >= MAX_ATTEMPT_SNAPSHOTS:
            return
        status = str(report.get("status") or "")
        window = _count(report.get("context_window")) or None
        self._snapshots.append(
            {
                "seq": meta[0],
                "event_id": meta[1],
                "at": meta[2],
                "source": str(report.get("source") or "")[:64],
                "status": status[:32],
                "total_tokens": _count(report.get("total_tokens")),
                "context_window": window,
            }
        )
        if status in {"ok", "partial"} and window:
            self._snapshot_window = window

    # -- raw protocol rows ---------------------------------------------------

    def _raw(
        self,
        payload: Mapping[str, Any],
        meta: tuple[int, int | None, str | None],
        *,
        subagent: bool,
    ) -> None:
        notification = payload.get("notification")
        if isinstance(notification, Mapping):
            self._codex(notification, meta, subagent=subagent)
            return
        reason = payload.get("reason")
        if isinstance(reason, str) and _PI_RAW_COMPACTION.match(reason):
            self._pi_raw_compaction(payload.get("raw_text"), meta)
            return
        text = payload.get("raw_text")
        if isinstance(text, str):
            self._opencode(text, meta)

    def _codex(
        self,
        notification: Mapping[str, Any],
        meta: tuple[int, int | None, str | None],
        *,
        subagent: bool,
    ) -> None:
        method = notification.get("method")
        params = notification.get("params")
        params = params if isinstance(params, Mapping) else {}
        if method == "thread/tokenUsage/updated":
            self._codex_usage(params, meta, subagent=subagent)
            return
        if subagent:
            return
        turn = str(params.get("turnId") or "")
        item = params.get("item")
        item = item if isinstance(item, Mapping) else {}
        if method == "item/completed" and item.get("type") == _CODEX_COMPACTION_ITEM:
            item_id = str(item.get("id") or "")
            if item_id and item_id in self._compaction_items:
                return
            self._compaction_items.add(item_id)
            if turn and turn in self._codex_compacted_turns:
                # The deprecated notification already stood for this one.
                self._drop_codex_thread_compacted(turn)
            self._compacted(_Compaction(*meta, source="codex_context_compaction", turn_key=turn))
            return
        if method == "thread/compacted":
            if turn and any(
                c.turn_key == turn and c.source == "codex_context_compaction"
                for c in self._compactions
            ):
                return
            self._codex_compacted_turns.add(turn)
            self._compacted(_Compaction(*meta, source="codex_thread_compacted", turn_key=turn))

    def _drop_codex_thread_compacted(self, turn: str) -> None:
        self._compactions = [
            c
            for c in self._compactions
            if not (c.source == "codex_thread_compacted" and c.turn_key == turn)
        ]
        self._codex_compacted_turns.discard(turn)

    def _pi_raw_compaction(self, text: Any, meta: tuple[int, int | None, str | None]) -> None:
        """A ``compaction_end`` stored raw, before pi compactions were classified.

        The stored line is its first 4000 characters and pi writes the summary
        before the outcome, so a long one no longer says whether it finished.
        """
        trigger = None
        completed: bool | None = None
        pre = None
        if isinstance(text, str):
            match = _RAW_TRIGGER.search(text[:200])
            trigger = match.group(1) if match else None
            try:
                value = json.loads(text)
            except ValueError:
                value = None
            if isinstance(value, Mapping):
                result = value.get("result")
                completed = isinstance(result, Mapping) and value.get("aborted") is not True
                pre = _count(result.get("tokensBefore")) if isinstance(result, Mapping) else None
        self._compacted(
            _Compaction(
                *meta,
                source="pi_compaction_raw",
                trigger=trigger,
                pre_tokens=pre,
                completed=completed,
            )
        )

    def _opencode(self, text: str, meta: tuple[int, int | None, str | None]) -> None:
        try:
            line = json.loads(text)
        except ValueError:
            # Clipped at 4000 characters when stored: not an observation.
            self.unparsed_raw += 1
            return
        event = line.get("event") if isinstance(line, Mapping) else None
        if not isinstance(event, Mapping):
            return
        kind = event.get("type")
        properties = event.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        part = properties.get("part")
        part = part if isinstance(part, Mapping) else {}
        if kind == "message.part.updated" and part.get("type") == "step-finish":
            self._opencode_step(part, meta)
        elif kind == "message.part.updated" and part.get("type") == "compaction":
            auto = part.get("auto")
            self._opencode_trigger = (
                ("auto" if auto else "manual") if isinstance(auto, bool) else None
            )
        elif kind == "session.compacted":
            self._compacted(
                _Compaction(
                    *meta, source="opencode_session_compacted", trigger=self._opencode_trigger
                )
            )
            self._opencode_trigger = None

    # -- events --------------------------------------------------------------

    def _event(self, meta: tuple[int, int | None, str | None], **fields: Any) -> None:
        """Note one context event, bounded; the rest are counted."""
        self._events_seen += 1
        if len(self._events) < MAX_ATTEMPT_EVENTS:
            self._events.append(
                {
                    "seq": meta[0],
                    "event_id": meta[1],
                    "at": meta[2],
                    **{key: value for key, value in fields.items() if value is not None},
                }
            )

    def _platform_injections(self, value: Any, meta: tuple[int, int | None, str | None]) -> None:
        """What the platform wrapped this attempt's prompt in at claim (``started``)."""
        for item in value if isinstance(value, list) else []:
            if not isinstance(item, Mapping):
                continue
            names = [str(n)[:120] for n in item.get("names") or [] if isinstance(n, str)]
            counts = [_count(item.get(key)) or 0 for key in ("ascii", "cjk")]
            chars = _count(item.get("chars"))
            self._event(
                meta,
                kind="injection",
                source="platform_" + _label(item.get("producer"), 32),
                items=_count(item.get("items")),
                names=names[:MAX_INJECTION_NAMES] or None,
                chars=chars,
                tokens=(
                    estimate_from_counts(counts[0], counts[1], (chars or 0) - sum(counts))
                    if chars is not None
                    else None
                ),
            )

    @staticmethod
    def _estimate(payload: Mapping[str, Any]) -> int | None:
        """The estimate for a lifecycle event's stored text, when it has one."""
        chars = _count(payload.get("text_chars"))
        if chars is None:
            return None
        ascii_chars, cjk = _count(payload.get("ascii")) or 0, _count(payload.get("cjk")) or 0
        return estimate_from_counts(ascii_chars, cjk, chars - ascii_chars - cjk)

    # -- content -------------------------------------------------------------

    def _add_content(
        self, category: ContentCategory, payload: Mapping[str, Any], *, chars_key: str = "chars"
    ) -> None:
        chars = _count(payload.get(chars_key))
        if chars:
            known = self._content[category]
            known[0] += chars
            known[1] += _count(payload.get("ascii")) or 0
            known[2] += _count(payload.get("cjk")) or 0
            known[3] += 1
        if payload.get("truncated") is True:
            self.truncated = True

    # -- result ----------------------------------------------------------------

    def finish(self) -> dict[str, Any]:
        """The attempt's stored profile and its sortable summary columns."""
        window = self._main_window()
        requests = self._requests
        if window is not None:
            for request in requests:
                request.window = request.window or window
        kept, omitted = _bounded(requests)
        completed = [c for c in self._compactions if c.completed is not False]
        latest = requests[-1] if requests else None
        windows = [r.window for r in requests if r.window]
        compacted_after = bool(latest is not None and any(c.seq > latest.seq for c in completed))
        return {
            "v": CONTEXT_PROFILE_VERSION,
            "sources": sorted(self.sources),
            "main_model": self.main_model,
            "windows": dict(sorted(self.windows.items())),
            "requests": [request.point() for request in kept],
            "requests_omitted": omitted,
            "subagent_requests": self.subagent_requests,
            "subagent_peak": self.subagent_peak,
            "compactions": [c.record() for c in self._compactions[:MAX_ATTEMPT_COMPACTIONS]],
            "compactions_omitted": max(0, len(self._compactions) - MAX_ATTEMPT_COMPACTIONS),
            "content": {
                "basis": ESTIMATE_BASIS,
                "categories": {
                    category: {
                        "chars": chars,
                        "tokens": estimate_from_counts(ascii_chars, cjk, chars - ascii_chars - cjk),
                        "items": items,
                    }
                    for category, (chars, ascii_chars, cjk, items) in self._content.items()
                    if items
                },
                "truncated": self.truncated,
            },
            "events": self._events,
            "events_omitted": max(0, self._events_seen - len(self._events)),
            "unparsed_raw": self.unparsed_raw,
            "snapshots": self._snapshots,
            "summary": {
                "main_requests": len(requests),
                "subagent_requests": self.subagent_requests,
                "peak_prompt_tokens": max((r.prompt for r in requests), default=None),
                "latest_prompt_tokens": latest.prompt if latest else None,
                "latest_observed_at": latest.at if latest else None,
                "context_window": windows[-1] if windows else None,
                "compactions": len(completed),
                "compacted_after_latest": compacted_after,
            },
        }

    def _main_window(self) -> int | None:
        """The window the conversation's own model ran under, when reported."""
        if self.main_model and self.main_model in self.windows:
            return self.windows[self.main_model]
        if len(self.windows) == 1:
            return next(iter(self.windows.values()))
        # A CLI snapshot's window (pi reports its model's) when nothing else did.
        return self._snapshot_window if not self.windows else None


def _bounded(requests: list[_Request]) -> tuple[list[_Request], int]:
    if len(requests) <= MAX_ATTEMPT_POINTS:
        return requests, 0
    half = MAX_ATTEMPT_POINTS // 2
    return [*requests[:half], *requests[-half:]], len(requests) - MAX_ATTEMPT_POINTS


@dataclass(frozen=True)
class StoredProfile:
    """A stored attempt profile, read back with the identity it belongs to."""

    attempt_id: int
    job_id: str
    profile: dict[str, Any] = field(default_factory=dict)


def profile_is_current(profile: Any) -> bool:
    """Whether a stored profile was written by this reducer version."""
    return isinstance(profile, Mapping) and profile.get("v") == CONTEXT_PROFILE_VERSION
