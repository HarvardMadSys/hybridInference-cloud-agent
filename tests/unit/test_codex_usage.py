"""Codex's own usage report, from the pinned app-server to the usage summary.

Codex 0.145.0 reports token use in ``thread/tokenUsage/updated``: ``last`` is the
thread's latest model call, ``total`` the thread's running sum, both in
camelCase. Its ``turn/completed`` carries no usage at all. These cases feed
notifications of exactly that shape (each is checked against the schema the
pinned app-server generates, kept under ``tests/fixtures``) through
``CodexAppServerSession``, then reduce what it emitted the way the usage pages
do: one rollup per attempt, then the turn's runtime-reported summary.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from tests.unit.test_live_sessions import _LiveProcess, _protocol_context, _turn_lines

from cloud_agent.runtime_usage import summarize_runtime_job
from cloud_agent.thread_stats import (
    AttemptStatsAccumulator,
    attempt_rollup_from_payload,
    attempt_rollup_payload,
)
from cloud_agent_host.drivers import TurnInput, driver_for
from cloud_agent_host.runtimes import CodexRuntime

_SCHEMAS = Path(__file__).parents[1] / "fixtures" / "codex_app_server_0_145_0"


def _schema(name: str) -> jsonschema.Draft7Validator:
    return jsonschema.Draft7Validator(json.loads((_SCHEMAS / f"{name}.json").read_text()))


_USAGE_SCHEMA = _schema("ThreadTokenUsageUpdatedNotification")
_COMPLETED_SCHEMA = _schema("TurnCompletedNotification")


def _counts(inputs: int, cached: int, output: int, reasoning: int) -> dict[str, int]:
    """One ``TokenUsageBreakdown``: input includes cached input, output includes reasoning."""
    return {
        "inputTokens": inputs,
        "cachedInputTokens": cached,
        "cacheWriteInputTokens": 0,
        "outputTokens": output,
        "reasoningOutputTokens": reasoning,
        "totalTokens": inputs + output,
    }


def _usage(thread: str, turn: str, *, last: tuple, total: tuple) -> dict:
    params = {
        "threadId": thread,
        "turnId": turn,
        "tokenUsage": {
            "last": _counts(*last),
            "total": _counts(*total),
            "modelContextWindow": 200000,
        },
    }
    _USAGE_SCHEMA.validate(params)
    return {"method": "thread/tokenUsage/updated", "params": params}


def _completed(thread: str, turn: str) -> dict:
    params = {"threadId": thread, "turn": {"id": turn, "status": "completed", "items": []}}
    _COMPLETED_SCHEMA.validate(params)
    return {"method": "turn/completed", "params": params}


def _spawn(thread: str, child: str) -> dict:
    item = {
        "type": "collabAgentToolCall",
        "id": f"call-{child}",
        "tool": "spawnAgent",
        "receiverThreadIds": [child],
    }
    return {"method": "item/completed", "params": {"threadId": thread, "item": item}}


def _session(tmp_path, *, resume: str | None = None):
    process = _LiveProcess()
    context, _spawned = _protocol_context(process, tmp_path)
    thread = resume or "thread-a"
    process.feed = _turn_lines(
        {"id": "initialize-1", "result": {"userAgent": "codex"}},
        {"id": "thread-2", "result": {"thread": {"id": thread}}},
    )
    driver = driver_for(CodexRuntime())
    session = (
        driver.launch_resident_live(context, session_id=resume)
        if resume
        else driver.launch_live(context)
    )
    session.ensure_started(model="gpt", reasoning_effort=None)
    return process, session


def _run(process, session, turn_id: str, *lines: dict) -> list:
    request = len([line for line in process.sent if '"turn/start"' in line]) + 3
    process.feed = _turn_lines({"id": f"turn-{request}", "result": {"turn": {"id": turn_id}}})
    turn = session.start_turn(TurnInput(prompt="go", model="gpt"))
    process.feed = _turn_lines(*lines)
    events = list(turn.iter_events())
    assert turn.wait().status == "completed"
    return events


def _summary(events: list, *, attempt_id: int = 1) -> dict:
    """The turn's usage as the usage pages compute it, through a stored rollup."""
    accumulator = AttemptStatsAccumulator(attempt_id=attempt_id, job_id="job")
    accumulator.extend(
        {
            "job_id": "job",
            "attempt_id": attempt_id,
            "seq": seq,
            "event_type": event.event_type,
            "created_at": None,
            "payload": event.payload,
        }
        for seq, event in enumerate(events, 1)
    )
    # Through the cache encoding, as a stored rollup is read back.
    rollup = attempt_rollup_from_payload(
        attempt_id=attempt_id,
        job_id="job",
        payload=json.loads(json.dumps(attempt_rollup_payload(accumulator.finish()))),
    )
    row = {
        "attempt_id": attempt_id,
        "attempt_no": 1,
        "saw_usage": rollup.saw_usage,
        "input_tokens": rollup.input_tokens,
        "output_tokens": rollup.output_tokens,
        "cache_read_tokens": rollup.cache_read_tokens,
        "cache_write_tokens": rollup.cache_write_tokens,
        "reasoning_tokens": rollup.reasoning_tokens,
    }
    usage = summarize_runtime_job({"runtime": "codex", "usage": [row]}, terminal=True)
    assert usage.source == "runtime_reported"
    return {
        "status": usage.status,
        **(usage.totals.model_dump() if usage.totals else {}),
    }


def _counted(events: list) -> list[str]:
    return [event.payload["usage_id"] for event in events if event.event_type == "usage"]


def _uncounted(events: list) -> list[dict]:
    return [
        event.payload["notification"]["params"]
        for event in events
        if event.event_type == "raw"
        and event.payload["notification"].get("method") == "thread/tokenUsage/updated"
    ]


def test_the_pinned_turn_carries_no_usage_of_its_own():
    """Why ``turn/completed`` is not read for usage: the pinned Turn has no such field."""
    turn = json.loads((_SCHEMAS / "TurnCompletedNotification.json").read_text())
    assert "usage" not in turn["definitions"]["Turn"]["properties"]


def test_two_turns_on_one_resident_thread_count_each_call_once_with_its_subagent(tmp_path):
    process, session = _session(tmp_path)
    first_call = _usage("thread-a", "turn-a", last=(1000, 800, 50, 20), total=(1000, 800, 50, 20))
    child_call = _usage("child-t", "child-turn", last=(300, 0, 40, 10), total=(300, 0, 40, 10))
    first = _run(
        process,
        session,
        "turn-a",
        first_call,
        # Repeated without a new model call: the same total, counted once.
        first_call,
        _spawn("thread-a", "child-t"),
        child_call,
        child_call,
        _usage("thread-a", "turn-a", last=(1200, 1000, 60, 0), total=(2200, 1800, 110, 20)),
        # A notification naming another turn of this thread is not this turn's.
        _usage("thread-a", "turn-old", last=(9, 9, 9, 9), total=(9000, 9, 9, 9)),
        _completed("child-t", "child-turn"),
        _completed("thread-a", "turn-a"),
    )
    assert _counted(first) == [
        "codex:thread-a:1050",
        "codex:thread-a:1050",
        "codex:child-t:340",
        "codex:child-t:340",
        "codex:thread-a:2310",
    ]
    child = next(event for event in first if event.payload.get("usage_id") == "codex:child-t:340")
    assert child.payload["agent_thread_id"] == "child-t"
    assert child.payload["parent_tool_use_id"] == "call-child-t"
    assert [params["turnId"] for params in _uncounted(first)] == ["turn-old"]
    assert _summary(first) == {
        "status": "available",
        "calls": None,
        "tokens_in": 1000 + 300 + 1200,
        "tokens_out": 50 + 40 + 60,
        "cache_read": 800 + 0 + 1000,
        "cache_write": 0,
        # Part of output, never added to it.
        "reasoning": 20 + 10 + 0,
        "spent_usd": None,
    }

    # The thread's total carries the first turn; the second counts only its own call.
    second = _run(
        process,
        session,
        "turn-b",
        # The first turn's subagent is still talking: not this turn's to claim.
        _usage("child-t", "child-turn-2", last=(100, 0, 5, 0), total=(400, 0, 45, 10)),
        _usage("thread-a", "turn-b", last=(1500, 1400, 70, 30), total=(3700, 3200, 180, 50)),
        _usage("thread-a", "turn-b", last=(1500, 1400, 70, 30), total=(3700, 3200, 180, 50)),
        _completed("thread-a", "turn-b"),
    )
    assert _counted(second) == ["codex:thread-a:3880", "codex:thread-a:3880"]
    assert [params["threadId"] for params in _uncounted(second)] == ["child-t"]
    assert _summary(second, attempt_id=2) == {
        "status": "available",
        "calls": None,
        "tokens_in": 1500,
        "tokens_out": 70,
        "cache_read": 1400,
        "cache_write": 0,
        "reasoning": 30,
        "spent_usd": None,
    }


def test_a_resumed_thread_counts_only_the_calls_of_the_turn_it_runs(tmp_path):
    """The restored thread total needs no baseline: each call is its own ``last``."""
    process, session = _session(tmp_path, resume="thread-prev")
    events = _run(
        process,
        session,
        "turn-r",
        # Replayed for the turn the earlier process ran: not this one's.
        _usage("thread-prev", "turn-before", last=(800, 0, 30, 0), total=(90000, 0, 4000, 0)),
        _usage("thread-prev", "turn-r", last=(500, 400, 20, 5), total=(90500, 400, 4020, 5)),
        _completed("thread-prev", "turn-r"),
    )
    assert _counted(events) == ["codex:thread-prev:94520"]
    assert _summary(events)["tokens_in"] == 500
    assert _summary(events)["tokens_out"] == 20
    assert _summary(events)["reasoning"] == 5


def test_a_subagent_spawned_before_this_process_is_not_credited_to_a_turn(tmp_path):
    """No spawn was seen, so no turn can be proven to own its calls: kept raw, counted nowhere."""
    process, session = _session(tmp_path, resume="thread-prev")
    events = _run(
        process,
        session,
        "turn-r",
        _usage("orphan-t", "orphan-turn", last=(70, 0, 7, 0), total=(70, 0, 7, 0)),
        _completed("thread-prev", "turn-r"),
    )
    assert _counted(events) == []
    assert [params["threadId"] for params in _uncounted(events)] == ["orphan-t"]
    # A turn whose agent reported nothing it can claim is unavailable, never zero.
    assert _summary(events) == {"status": "unavailable"}


@pytest.mark.parametrize(
    "broken",
    [
        {"last": None},
        {"total": {"totalTokens": -1}},
        {"last": {"outputTokens": True}},
    ],
)
def test_a_malformed_report_is_kept_raw_rather_than_partly_counted(tmp_path, broken):
    process, session = _session(tmp_path)
    notification = _usage("thread-a", "turn-a", last=(10, 0, 1, 0), total=(10, 0, 1, 0))
    usage = notification["params"]["tokenUsage"]
    for key, value in broken.items():
        usage[key] = value if value is None else {**usage[key], **value}
    events = _run(process, session, "turn-a", notification, _completed("thread-a", "turn-a"))
    assert _counted(events) == []
    assert _summary(events) == {"status": "unavailable"}
