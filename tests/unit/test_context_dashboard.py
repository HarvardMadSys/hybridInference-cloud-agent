"""The Context dashboard's accounting and timing: only what records show.

Profiles are built from stored event shapes; the view, the session token
summary and the timing assembly then read them as the endpoints do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from typing import Any

from cloud_agent.context_dashboard import session_tokens, step_durations
from cloud_agent.context_dna import bands_for, item_key, prepare_rows
from cloud_agent.context_profile import ContextProfileAccumulator
from cloud_agent.context_request import RequestScope, walk_composition
from cloud_agent.context_running import POINT_CATEGORIES, bound_intervals, merge_intervals
from cloud_agent.context_timing import assemble_timing
from cloud_agent.context_view import assemble_thread_context

T0 = datetime(2026, 10, 30, 9, tzinfo=UTC)
CAT = {key: index for index, key in enumerate(POINT_CATEGORIES)}


def _events(*rows: tuple[str, dict[str, Any]] | tuple[str, dict[str, Any], float]):
    events = []
    for seq, row in enumerate(rows, start=1):
        kind, payload = row[0], row[1]
        seconds = row[2] if len(row) > 2 else seq
        events.append(
            {
                "id": 100 + seq,
                "seq": seq,
                "event_type": kind,
                "created_at": T0 + timedelta(seconds=seconds),
                "payload": payload,
            }
        )
    return events


def _profile(rows) -> dict[str, Any]:
    accumulator = ContextProfileAccumulator(attempt_id=1, job_id="job")
    accumulator.extend(rows)
    return accumulator.finish()


def _claude(usage_id: str, cached: int, chars: int = 40, **extra: Any) -> dict[str, Any]:
    return {
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0,
            "output_tokens": 5,
        },
        "usage_id": usage_id,
        "chars": chars,
        "ascii": chars,
        "cjk": 0,
        **extra,
    }


def _text(chars: int, **extra: Any) -> dict[str, Any]:
    return {"chars": chars, "ascii": chars, "cjk": 0, **extra}


# --- per-request categories ------------------------------------------------------


def test_a_claude_request_carries_what_came_before_it_not_its_own_answer():
    profile = _profile(
        _events(
            ("message", _claude("a", 1000, chars=400)),  # request a's own answer
            ("tool_use", _text(80, tool_id="t1")),
            ("tool_result", _text(4000, tool_id="t1")),
            ("message", _claude("b", 2000, chars=40)),
        )
    )
    first, second = (point[10] for point in profile["requests"])
    assert sum(first) == 0
    # Before b: a's answer (100), the call (20) and its result (1000).
    assert (second[CAT["assistant"]], second[CAT["tool_calls"]], second[CAT["tool_results"]]) == (
        100,
        20,
        1000,
    )


def test_usage_reported_after_the_answer_leaves_that_answer_out():
    def codex(total: int, last: int) -> dict[str, Any]:
        params = {
            "tokenUsage": {
                "total": {"totalTokens": total},
                "last": {"inputTokens": last, "cachedInputTokens": 0, "outputTokens": 5},
            }
        }
        return {"notification": {"method": "thread/tokenUsage/updated", "params": params}}

    profile = _profile(
        _events(
            ("message", _text(400)),
            ("raw", codex(100, 90)),
            ("tool_result", _text(800)),
            ("message", _text(40)),
            ("raw", codex(300, 190)),
        )
    )
    first, second = (point[10] for point in profile["requests"])
    assert sum(first) == 0  # its own answer is not its input
    assert second[CAT["assistant"]] == 100 and second[CAT["tool_results"]] == 200
    assert second[CAT["assistant"]] != 110  # the second answer is its own


def test_a_completed_compaction_starts_the_content_over():
    profile = _profile(
        _events(
            ("message", _claude("a", 1000, chars=4000)),
            ("lifecycle", {"phase": "compact_boundary"}),
            ("lifecycle", {"phase": "compaction_summary", "text_chars": 400, "ascii": 400}),
            ("message", _claude("b", 500, chars=40)),
        )
    )
    last = profile["requests"][-1]
    assert last[10][CAT["assistant"]] == 0 and last[10][CAT["summary"]] == 100
    assert last[11] is True  # compacted earlier in this attempt
    assert profile["content_tail"]["compacted"] is True


def test_text_after_a_skill_call_is_the_skill_and_other_injections_are_not():
    profile = _profile(
        _events(
            ("tool_use", _text(20, tool_name="Skill", tool_id="s1")),
            ("tool_result", _text(8, tool_id="s1")),
            ("lifecycle", {"phase": "context_injection", "text_chars": 800, "ascii": 800}),
            ("message", _claude("a", 10, chars=4)),
            ("lifecycle", {"phase": "context_injection", "text_chars": 40, "ascii": 40}),
        )
    )
    categories = profile["content"]["categories"]
    assert categories["skills"]["tokens"] == 200
    assert categories["injected"]["tokens"] == 10


def _job(job_id: str, turn_no: int, prompt_tokens: int = 25) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "turn_no": turn_no,
        "created_at": T0 + timedelta(hours=turn_no),
        "runtime": "claude-code",
        "model": "m",
        "state": "succeeded",
        "prompt_chars": prompt_tokens * 4,
        "prompt_tokens": prompt_tokens,
    }


def test_turns_carry_the_kept_attempt_and_platform_context_into_the_next():
    started = (
        "lifecycle",
        {
            "phase": "started",
            "context_injections": [
                {"producer": "memory", "items": 1, "chars": 40, "ascii": 40, "cjk": 0},
                {"producer": "skills", "items": 1, "chars": 80, "ascii": 80, "cjk": 0},
            ],
        },
    )
    retried = _profile(_events(started, ("message", _claude("r", 900, chars=40_000))))
    kept = _profile(_events(started, ("message", _claude("k", 1000, chars=400))))
    second = _profile(_events(("message", _claude("s", 2000, chars=40))))
    view = assemble_thread_context(
        thread_id="th",
        generated_at=T0,
        jobs=[_job("j1", 1), _job("j2", 2)],
        attempts=[
            {"attempt_id": 1, "job_id": "j1", "profile": retried},
            {"attempt_id": 2, "job_id": "j1", "profile": kept},
            {"attempt_id": 3, "job_id": "j2", "profile": second},
        ],
    )
    keys = view.category_keys
    retry_point, kept_point, next_point = view.requests
    as_dict = lambda point: dict(zip(keys, point.categories, strict=True))  # noqa: E731
    assert as_dict(kept_point) == {**dict.fromkeys(keys, 0), "user": 25, "inject": 10, "skill": 20}
    # The retried attempt's 10,000-token answer is not carried; the kept one's is.
    nxt = as_dict(next_point)
    assert nxt["assistant"] == 100 and nxt["user"] == 50
    assert nxt["inject"] == 10 and nxt["skill"] == 20  # the second turn added none
    assert as_dict(retry_point)["assistant"] == 0

    tokens = view.tokens
    assert tokens is not None
    recorded = {share.key: share.tokens for share in tokens.recorded}
    # Each stored item once, kept attempts only; the retried 10,000 is not here.
    assert recorded == {"user": 50, "inject": 10, "skill": 20, "assistant": 110}
    assert tokens.not_recorded == ["system_prompt", "tool_schemas"]
    # Every attempt's requests were sent: all three are in the provider's sum.
    assert tokens.provider.requests == 3
    assert tokens.provider.input_tokens == 910 + 1010 + 2010
    assert tokens.provider.cache_hit == (900 + 1000 + 2000) / (910 + 1010 + 2010)


def test_a_conversation_without_profiles_says_so_and_has_no_numbers():
    tokens = session_tokens([_job("j1", 1)], {})
    assert tokens.turns_read == 0
    assert tokens.provider.input_tokens == 0 and tokens.provider.cache_hit is None
    assert [share.key for share in tokens.recorded] == ["user"]


# --- step durations -------------------------------------------------------------


def test_step_time_stays_inside_its_attempt():
    def point(index: int, attempt: int, seconds: int):
        return SimpleNamespace(index=index, attempt_id=attempt, at=T0 + timedelta(seconds=seconds))

    points = [point(0, 1, 0), point(1, 1, 30), point(2, 2, 600), point(3, 2, 610)]
    ends = {1: int((T0 + timedelta(seconds=45)).timestamp() * 1000)}
    durations = step_durations(points, ends)
    assert durations[0] == 30_000
    assert durations[1] == 15_000  # to its attempt's last event, not the retry
    assert durations[2] == 10_000
    assert 3 not in durations  # no recorded end for attempt 2


# --- timing -----------------------------------------------------------------------


def test_intervals_count_overlaps_once_and_bounding_discloses_what_it_closed():
    assert merge_intervals([[0, 10], [5, 20], [30, 40]]) == [[0, 20], [30, 40]]
    bounded, added = bound_intervals([[0, 10], [12, 20], [40, 50]], 2)
    assert bounded == [[0, 20], [40, 50]] and added == 2


def _timing_profile(rows) -> dict[str, Any]:
    return _profile(rows)


def test_parallel_tools_unpaired_calls_and_no_gateway():
    profile = _timing_profile(
        _events(
            ("lifecycle", {"phase": "init"}, 0),
            ("tool_use", {"tool_id": "a"}, 10),
            ("tool_use", {"tool_id": "b"}, 12),
            ("tool_result", {"tool_id": "a"}, 20),
            ("tool_result", {"tool_id": "b"}, 25),
            ("tool_use", {"tool_id": "cancelled"}, 30),
            ("message", {}, 40),
        )
    )
    timing = assemble_timing(
        [{"attempt_id": 1, "profile": profile}], [], gateway_gap=None, gateway_started=0
    )
    assert timing.active_ms == 40_000
    assert timing.totals["tools"] == 15_000  # 10..25 once, not 10 + 13
    assert timing.totals["unsplit"] == 25_000  # model and harness, not split
    assert timing.totals["ttft"] == 0 and timing.totals["other"] == 0
    assert timing.tool_calls == 3 and timing.tool_calls_unpaired == 1
    assert timing.gateway == "not_recorded"
    assert sum(timing.totals.values()) == timing.active_ms


def test_gateway_requests_split_model_time_and_tools_never_overlap_it():
    profile = _timing_profile(
        _events(
            ("lifecycle", {"phase": "init"}, 0),
            ("tool_use", {"tool_id": "task"}, 10),
            ("message", {"block_ms": 4000}, 12),
            ("tool_result", {"tool_id": "task"}, 30),
            ("message", {}, 40),
        )
    )

    def request(request_id: str, start: int, ttft: int, latency: int, out: int):
        return SimpleNamespace(
            request_id=request_id,
            response_id=f"msg_{request_id}",
            request_started_at=T0 + timedelta(seconds=start),
            ttft_ms=ttft,
            latency_ms=latency,
            status_code=200,
            tokens_out=out,
        )

    page = SimpleNamespace(
        requests=[
            request("r1", 1, 1000, 9000, 90),  # 1..10 s
            request("r2", 15, 2000, 5000, 30),  # inside the task window: model wins
            request("r2", 15, 2000, 5000, 30),  # a duplicate row is one request
            request("failed", 31, 100, 200, 0),
        ],
        next_cursor=None,
    )
    page.requests[3].status_code = 500
    timing = assemble_timing(
        [{"attempt_id": 1, "profile": profile}],
        [({"attempt_id": 1}, page)],
        gateway_gap=None,
        gateway_started=1,
    )
    totals = timing.totals
    assert timing.model_requests == 2
    assert totals["ttft"] == 3_000
    # Generation 8 s + 3 s, of which the streamed answer block took 4 s.
    assert totals["text"] == 4_000 and totals["gen"] == 7_000
    assert totals["tools"] == 20_000 - 5_000  # the task window minus model time in it
    assert totals["other"] == 40_000 - 3_000 - 11_000 - 15_000
    assert sum(totals.values()) == timing.active_ms
    assert timing.gateway == "available"
    assert timing.output_tps == (90 + 30) / 11
    assert {r.response_id for r in timing.requests} == {"msg_r1", "msg_r2"}
    kinds = [segment.kind for segment in timing.segments]
    assert kinds[0] == "other" and "ttft" in kinds
    assert all(a.end_ms <= b.start_ms for a, b in pairwise(timing.segments))


def test_an_execution_the_gateway_did_not_cover_stays_unsplit():
    covered = _timing_profile(_events(("message", {}, 0), ("message", {}, 10)))
    uncovered = _timing_profile(_events(("message", {}, 100), ("message", {}, 130)))
    timing = assemble_timing(
        [
            {"attempt_id": 1, "profile": covered},
            {"attempt_id": 2, "profile": uncovered},
        ],
        [({"attempt_id": 1}, SimpleNamespace(requests=[], next_cursor=None))],
        gateway_gap="sources_truncated",
        gateway_started=2,
    )
    assert timing.gateway == "partial" and timing.executions_split == 1
    # The retry's wait between 10 s and 100 s is no one's.
    assert timing.active_ms == 40_000
    assert timing.totals["other"] == 10_000 and timing.totals["unsplit"] == 30_000


def test_dna_bands_select_exactly_what_the_composition_walk_carries():
    """DNA prepares rows once per window; its items must equal the walk's entries."""
    point = SimpleNamespace(index=3, event_id=209, attempt_id=2, seq=9, usage_id="msg_b")

    def row(event_id: int, attempt: int, seq: int, kind: str, **extra: Any) -> dict[str, Any]:
        return {
            "id": event_id,
            "attempt_id": attempt,
            "seq": seq,
            "event_type": kind,
            "ascii": 40 * seq,
            "cjk": seq,
            "other": 0,
            "chars": 41 * seq,
            **extra,
        }

    events = [
        row(101, 1, 1, "tool_result"),  # before the compaction: compacted
        row(102, 1, 5, "message", usage_id="msg_a"),
        row(103, 1, 6, "tool_result"),
        row(201, 2, 2, "tool_result"),
        row(202, 2, 3, "message", subagent=True),  # a subagent's own context
        row(203, 2, 4, "thinking", usage_id="msg_b"),  # this request's own output
        row(204, 2, 5, "tool_use", name="Read"),
        row(205, 2, 12, "tool_result"),  # after this request
        row(301, 9, 1, "tool_result"),  # an attempt this request does not carry
        row(206, 2, 6, "lifecycle", phase="started"),  # no content
    ]
    prompts = [
        {"job_id": "j1", "turn_no": 1, "ascii": 30, "cjk": 4, "other": 0, "chars": 34},
        {"job_id": "j2", "turn_no": 2, "ascii": 12, "cjk": 0, "other": 0, "chars": 12},
    ]
    located = SimpleNamespace(
        point=point,
        carried_attempts={1: ("j1", 1, 0), 2: ("j2", 2, 0)},
        carried_jobs=["j1", "j2"],
        previous_report_seq=0,
        report_after_response=False,
        boundary_key=(1, 0, 3),
    )
    bands = bands_for(located, prompts=prompts, rows=prepare_rows(events))

    annotated = sorted(
        (
            {
                **e,
                "job_id": located.carried_attempts[e["attempt_id"]][0],
                "turn_no": located.carried_attempts[e["attempt_id"]][1],
                "attempt_rank": 0,
            }
            for e in events
            if e["attempt_id"] in located.carried_attempts
            and not (e["attempt_id"] == point.attempt_id and e["seq"] > point.seq)
        ),
        key=lambda e: (e["turn_no"], e["attempt_rank"], e["seq"]),
    )
    scope = RequestScope(
        attempt_id=2,
        seq=9,
        usage_id="msg_b",
        previous_report_seq=0,
        report_after_response=False,
        boundary_key=(1, 0, 3),
    )
    walk = walk_composition(
        scope=scope, turns=prompts, events=annotated, superseded={}, tool_names={}
    )
    expected = [(item_key(e), e.category, e.tokens) for e in walk.entries]

    assert [tuple(item) for item in bands.items] == expected
    assert [key for key, _, _ in expected] == ["pj2", "e102", "e103", "e201", "e204"]
    assert bands.folded_items == 0
