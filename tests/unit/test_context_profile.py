"""Context profiles: per-runtime request sizes, compactions, and their honesty.

The fixtures use each pinned CLI's stored event shapes, as the projection in
``storage/_sql/context.py`` hands them to the reducer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from cloud_agent.context_profile import (
    CONTEXT_PROFILE_VERSION,
    MAX_ATTEMPT_POINTS,
    ContextProfileAccumulator,
    estimate_tokens,
)
from cloud_agent.context_view import assemble_thread_context

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _events(*rows: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": 100 + seq,
            "seq": seq,
            "event_type": kind,
            "created_at": T0 + timedelta(seconds=seq),
            "payload": payload,
        }
        for seq, (kind, payload) in enumerate(rows, start=1)
    ]


def _profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    accumulator = ContextProfileAccumulator(attempt_id=1, job_id="job")
    accumulator.extend(rows)
    return accumulator.finish()


def _claude_usage(usage_id: str, input_tokens: int, read: int, write: int, out: int, **extra: Any):
    return {
        "usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
            "output_tokens": out,
        },
        "usage_id": usage_id,
        **extra,
    }


def test_claude_blocks_of_one_response_are_one_request():
    profile = _profile(
        _events(
            ("lifecycle", {"phase": "init", "model": "claude-sonnet"}),
            ("thinking", {**_claude_usage("msg_1", 10, 1000, 200, 1), "chars": 40}),
            ("tool_use", {**_claude_usage("msg_1", 10, 1000, 200, 30), "chars": 80}),
            ("tool_result", {"chars": 4000, "truncated": True}),
            ("message", {**_claude_usage("msg_2", 5, 1300, 50, 12), "chars": 400}),
            (
                "lifecycle",
                {
                    "phase": "result",
                    "usage": {"input_tokens": 999},
                    "usage_scope": "attempt",
                    "context_windows": {"claude-sonnet": 200_000, "claude-haiku": 100_000},
                },
            ),
        )
    )
    summary = profile["summary"]
    assert profile["v"] == CONTEXT_PROFILE_VERSION
    assert summary["main_requests"] == 2
    # 10 + 1000 + 200, then 5 + 1300 + 50; the attempt-scope total is no request.
    assert [point[3] for point in profile["requests"]] == [1210, 1355]
    assert profile["requests"][0][7] == 30  # output grew across the blocks
    # The init model picks its own window out of the two reported.
    assert summary["context_window"] == 200_000
    assert profile["content"]["total"] == {
        "assistant": 400,
        "reasoning": 40,
        "tool_calls": 80,
        "tool_results": 4000,
    }
    assert profile["content"]["truncated"] is True
    assert profile["content"]["after_compaction"] is None


def test_claude_subagent_requests_are_counted_apart_from_the_main_context():
    profile = _profile(
        _events(
            ("message", _claude_usage("a", 10, 0, 0, 1)),
            ("message", _claude_usage("b", 50_000, 0, 0, 1, parent_tool_use_id="toolu_1")),
            ("message", {**_claude_usage("c", 20, 0, 0, 1), "chars": 8}),
        )
    )
    assert profile["summary"]["main_requests"] == 2
    assert profile["summary"]["peak_prompt_tokens"] == 20
    assert profile["subagent_requests"] == 1
    assert profile["subagent_peak"] == 50_000


def test_claude_without_a_reported_window_has_none():
    profile = _profile(_events(("message", _claude_usage("a", 10, 0, 0, 1))))
    assert profile["summary"]["context_window"] is None
    assert profile["requests"][0][8] is None


def test_a_drop_without_a_reported_compaction_is_not_a_compaction():
    profile = _profile(
        _events(
            ("message", _claude_usage("a", 10, 90_000, 0, 1)),
            ("message", _claude_usage("b", 10, 1_000, 0, 1)),
        )
    )
    assert profile["summary"]["compactions"] == 0
    assert profile["compactions"] == []


def test_claude_compaction_with_and_without_the_clis_sizes():
    profile = _profile(
        _events(
            ("message", {**_claude_usage("a", 10, 90_000, 0, 1), "chars": 4000}),
            ("lifecycle", {"phase": "compact_boundary"}),
            (
                "lifecycle",
                {
                    "phase": "compact_boundary",
                    "compaction": {"trigger": "auto", "pre_tokens": 95_000},
                },
            ),
            ("message", {**_claude_usage("b", 10, 9_000, 0, 1), "chars": 12}),
        )
    )
    first, second = profile["compactions"]
    assert first["trigger"] is None and first["pre_tokens"] is None
    assert second["trigger"] == "auto" and second["pre_tokens"] == 95_000
    assert second["post_tokens"] is None
    assert profile["summary"]["compactions"] == 2
    assert profile["summary"]["compacted_after_latest"] is False
    # Content counted again from the last compaction only.
    assert profile["content"]["after_compaction"]["assistant"] == 12


def _codex(method: str, params: dict[str, Any], **owner: str) -> dict[str, Any]:
    return {"notification": {"method": method, "params": params}, **owner}


def _codex_usage(total: int, last_input: int, cached: int, out: int, window: int | None):
    return {
        "turnId": "t1",
        "tokenUsage": {
            "total": {"totalTokens": total},
            "last": {
                "inputTokens": last_input,
                "cachedInputTokens": cached,
                "cacheWriteInputTokens": 0,
                "outputTokens": out,
            },
            "modelContextWindow": window,
        },
    }


def test_codex_token_usage_is_the_last_request_with_its_window():
    repeat = _codex("thread/tokenUsage/updated", _codex_usage(1500, 1200, 1000, 300, 272_000))
    profile = _profile(
        _events(
            ("raw", repeat),
            # Same totals again (Codex resends them): not another request.
            ("raw", repeat),
            (
                "raw",
                _codex("thread/tokenUsage/updated", _codex_usage(3200, 1500, 1200, 200, 272_000)),
            ),
            (
                "raw",
                _codex(
                    "thread/tokenUsage/updated",
                    _codex_usage(9000, 40_000, 0, 1, None),
                    agent_thread_id="sub-1",
                ),
            ),
        )
    )
    assert profile["summary"]["main_requests"] == 2
    assert [point[3] for point in profile["requests"]] == [1200, 1500]
    # OpenAI input includes cached input: uncached is the difference.
    assert profile["requests"][0][6] == 200
    assert profile["summary"]["context_window"] == 272_000
    assert profile["subagent_requests"] == 1


def test_codex_compaction_item_and_deprecated_notification_count_once():
    profile = _profile(
        _events(
            ("raw", _codex("thread/compacted", {"turnId": "t1"})),
            (
                "raw",
                _codex(
                    "item/completed",
                    {"turnId": "t1", "item": {"id": "c1", "type": "contextCompaction"}},
                ),
            ),
            (
                "raw",
                _codex(
                    "item/completed",
                    {"turnId": "t1", "item": {"id": "c1", "type": "contextCompaction"}},
                ),
            ),
        )
    )
    assert [c["source"] for c in profile["compactions"]] == ["codex_context_compaction"]


def test_pi_usage_adds_the_cache_back_into_the_prompt():
    profile = _profile(
        _events(
            (
                "usage",
                {
                    "input_tokens": 100,
                    "cache_read_tokens": 4000,
                    "cache_write_tokens": 50,
                    "output_tokens": 20,
                    "model": "glm-4.6",
                },
            ),
            (
                "lifecycle",
                {
                    "phase": "compact_boundary",
                    "completed": True,
                    "compaction": {"trigger": "threshold", "pre_tokens": 4150},
                },
            ),
            ("lifecycle", {"phase": "compaction_aborted", "completed": False, "compaction": {}}),
        )
    )
    assert profile["requests"][0][3] == 4150
    assert profile["requests"][0][9] == "glm-4.6"
    assert profile["summary"]["compactions"] == 1  # the aborted one did not happen
    assert profile["summary"]["compacted_after_latest"] is True


def test_pi_raw_compaction_before_classification_keeps_what_it_can():
    clipped = '{"type":"compaction_end","reason":"overflow","result":{"summary":"' + "s" * 3900
    whole = json.dumps(
        {
            "type": "compaction_end",
            "reason": "manual",
            "result": {"tokensBefore": 77},
            "aborted": False,
        }
    )
    profile = _profile(
        _events(
            ("raw", {"reason": "unknown type 'compaction_end'", "raw_text": clipped}),
            ("raw", {"reason": "unknown type 'compaction_end'", "raw_text": whole}),
        )
    )
    unknown, known = profile["compactions"]
    assert unknown["trigger"] == "overflow" and unknown["completed"] is None
    assert known["trigger"] == "manual" and known["pre_tokens"] == 77 and known["completed"] is True


def _bridge(event: dict[str, Any]) -> dict[str, Any]:
    return {"raw_text": json.dumps({"bridge": "event", "turn_id": "x", "event": event})}


def _step(part_id: str, uncached: int, read: int, write: int, out: int, reasoning: int):
    return _bridge(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "id": part_id,
                    "type": "step-finish",
                    "tokens": {
                        "input": uncached,
                        "output": out,
                        "reasoning": reasoning,
                        "cache": {"read": read, "write": write},
                    },
                }
            },
        }
    )


def test_opencode_steps_and_compaction_from_stored_bridge_lines():
    profile = _profile(
        _events(
            ("raw", _step("p1", 300, 9000, 100, 40, 10)),
            ("raw", _step("p1", 300, 9000, 100, 40, 10)),
            (
                "raw",
                _bridge(
                    {
                        "type": "message.part.updated",
                        "properties": {"part": {"type": "compaction", "auto": True}},
                    }
                ),
            ),
            ("raw", _bridge({"type": "session.compacted", "properties": {"sessionID": "s"}})),
            ("raw", {"raw_text": '{"bridge":"event","event":{"type":"message.part.updated","prop'}),
        )
    )
    assert profile["summary"]["main_requests"] == 1
    assert profile["requests"][0][3] == 9400
    assert profile["requests"][0][7] == 50
    assert profile["compactions"][0]["trigger"] == "auto"
    assert profile["unparsed_raw"] == 1


def test_long_attempts_keep_both_ends_and_count_what_they_leave_out():
    rows = _events(
        *[("message", _claude_usage(f"m{i}", i, 0, 0, 1)) for i in range(MAX_ATTEMPT_POINTS + 10)]
    )
    profile = _profile(rows)
    assert len(profile["requests"]) == MAX_ATTEMPT_POINTS
    assert profile["requests_omitted"] == 10
    assert profile["summary"]["main_requests"] == MAX_ATTEMPT_POINTS + 10
    assert profile["summary"]["peak_prompt_tokens"] == MAX_ATTEMPT_POINTS + 9
    assert profile["requests"][-1][3] == MAX_ATTEMPT_POINTS + 9


def test_estimate_tokens_rounds_up_at_four_characters():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(1) == 1
    assert estimate_tokens(8) == 2


# --- the conversation view -----------------------------------------------------


def _job(job_id: str, turn_no: int, *, runtime: str = "claude-code", state: str = "completed"):
    return {
        "job_id": job_id,
        "turn_no": turn_no,
        "created_at": T0 + timedelta(hours=turn_no),
        "runtime": runtime,
        "model": "m",
        "state": state,
        "prompt_chars": 400,
    }


def test_view_orders_turns_marks_compaction_and_lists_changes():
    first = _profile(
        _events(
            ("message", {**_claude_usage("a", 10, 1000, 0, 1), "chars": 400}),
            ("message", {**_claude_usage("b", 10, 60_000, 0, 1), "chars": 400}),
            ("lifecycle", {"phase": "result", "context_windows": {"m": 200_000}}),
        )
    )
    second = _profile(
        _events(
            ("lifecycle", {"phase": "compact_boundary", "compaction": {"trigger": "auto"}}),
            ("message", {**_claude_usage("c", 10, 8_000, 0, 1), "chars": 800}),
            ("message", {**_claude_usage("d", 10, 2_000, 0, 1), "chars": 40}),
        )
    )
    view = assemble_thread_context(
        thread_id="th",
        generated_at=T0,
        jobs=[_job("j1", 1), _job("j2", 2, state="running")],
        attempts=[
            {"attempt_id": 1, "job_id": "j1", "profile": first},
            {"attempt_id": 2, "job_id": "j2", "profile": second},
        ],
    )
    assert [p.prompt_tokens for p in view.requests] == [1010, 60_010, 8_010, 2_010]
    assert view.requests[2].after_compaction is True
    assert view.requests[3].after_compaction is False
    assert view.peak is not None and view.peak.prompt_tokens == 60_010
    compaction = view.compactions[0]
    assert compaction.observed_before == 60_010 and compaction.observed_after == 8_010
    assert view.turns[1].change_tokens == 2_010 - 60_010
    # Largest growth first; the drop after the compaction says so, the later
    # drop without one is a drop and nothing else.
    growth = [c for c in view.largest_changes if c.direction == "growth"]
    drops = [c for c in view.largest_changes if c.direction == "drop"]
    assert growth[0].delta_tokens == 59_000
    assert [(c.delta_tokens, c.compaction_between) for c in drops] == [
        (-52_000, True),
        (-6_000, False),
    ]
    assert view.latest is not None
    assert view.latest.turn_active is True
    assert view.latest.compacted_after is False
    # The second turn reported no window: no share, never 0%.
    assert view.latest.window_share is None
    assert view.turns[0].context_window == 200_000
    claude = view.capabilities[0]
    assert claude.window_reported_turns == 1 and claude.live_occupancy == "not_collected"


def test_view_shares_only_a_reported_window_and_flags_a_later_compaction():
    profile = _profile(
        _events(
            ("raw", _codex("thread/tokenUsage/updated", _codex_usage(10, 68_000, 0, 1, 272_000))),
            (
                "raw",
                _codex(
                    "item/completed",
                    {"turnId": "t", "item": {"id": "c", "type": "contextCompaction"}},
                ),
            ),
        )
    )
    view = assemble_thread_context(
        thread_id="th",
        generated_at=T0,
        jobs=[_job("j1", 1, runtime="codex")],
        attempts=[{"attempt_id": 1, "job_id": "j1", "profile": profile}],
    )
    assert view.latest is not None
    assert view.latest.window_share == 68_000 / 272_000
    assert view.latest.compacted_after is True


def test_view_of_a_conversation_without_observations_is_honestly_empty():
    view = assemble_thread_context(
        thread_id="th",
        generated_at=T0,
        jobs=[_job("j1", 1, runtime="opencode")],
        attempts=[{"attempt_id": 1, "job_id": "j1", "profile": None}],
    )
    assert view.latest is None and view.peak is None
    assert view.requests == [] and view.compactions == []
    assert view.coverage.attempts == 1 and view.coverage.attempts_observed == 0
    assert view.capabilities[0].context_window == "not_reported"
