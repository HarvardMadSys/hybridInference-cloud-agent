"""Per-token native deltas are stored merged, never dropped or reordered.

Codex reports reasoning a few characters at a time and command output one
chunk at a time; OpenCode and Kilo report reasoning one token at a time. Every
row the runner receives is a stored event and a synchronous control-plane
request, so adjacent deltas of one stream are merged into one row instead.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from tests.unit.test_live_sessions import _LiveProcess, _protocol_context, _turn_lines

from cloud_agent_host.drivers import (
    FLOOR_EVENT_TYPES,
    LINE,
    CodexDriver,
    DeltaBatcher,
    DriverError,
    HarnessEvent,
    KiloDriver,
    OpenCodeDriver,
    TurnInput,
)
from cloud_agent_host.resident_transport import ResidentTurn
from cloud_agent_host.runtimes import CodexRuntime, KiloRuntime, OpencodeRuntime

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

SUMMARY = "item/reasoning/summaryTextDelta"
REASONING = "item/reasoning/textDelta"
OUTPUT = "item/commandExecution/outputDelta"


def _frozen() -> float:
    return 0.0


def _codex_turn(tmp_path: Path, clock: Callable[[], float] = _frozen) -> tuple[_LiveProcess, Any]:
    process = _LiveProcess()
    context, _ = _protocol_context(process, tmp_path)
    process.feed = _turn_lines(
        {"id": "initialize-1", "result": {"userAgent": "codex"}},
        {"id": "thread-2", "result": {"thread": {"id": "thread-a"}}},
    )
    session = CodexDriver(CodexRuntime()).launch_live(context)
    session.ensure_started(model="gpt", reasoning_effort=None)
    process.feed = _turn_lines({"id": "turn-3", "result": {"turn": {"id": "turn-a"}}})
    turn = session.start_turn(TurnInput(prompt="think", model="gpt"))
    turn.deltas = DeltaBatcher(clock=clock)
    return process, turn


def _delta(method: str, item_id: str, text: str, **extra: Any) -> dict[str, Any]:
    params = {"threadId": "thread-a", "turnId": "turn-a", "itemId": item_id, **extra}
    return {"method": method, "params": {**params, "delta": text}}


def _turn_completed(status: str = "completed") -> dict[str, Any]:
    return {"method": "turn/completed", "params": {"turn": {"id": "turn-a", "status": status}}}


def _stored(events: list[HarnessEvent]) -> list[HarnessEvent]:
    """The rows the runner stores: neither floor events nor bare lines."""
    return [
        event
        for event in events
        if event.event_type != LINE and event.event_type not in FLOOR_EVENT_TYPES
    ]


def _codex_deltas(rows: list[HarnessEvent]) -> list[tuple[str, str, str]]:
    """(method, item, text) of each stored delta notification, in order."""
    found = []
    for row in rows:
        notification = row.payload.get("notification") or {}
        params = notification.get("params") or {}
        if notification.get("method") in {SUMMARY, REASONING, OUTPUT}:
            found.append((notification["method"], params["itemId"], params["delta"]))
    return found


@pytest.mark.parametrize(
    ("method", "completed_item", "completed_kind"),
    [
        (SUMMARY, {"type": "reasoning", "id": "i", "summary": ["done"]}, "thinking"),
        (REASONING, {"type": "reasoning", "id": "i", "summary": ["done"]}, "thinking"),
        (OUTPUT, {"type": "commandExecution", "id": "i", "exitCode": 0}, "tool_result"),
    ],
)
def test_codex_per_token_deltas_become_a_few_rows(tmp_path, method, completed_item, completed_kind):
    """Fifty deltas are two stored rows: the first piece and the rest, merged."""
    process, turn = _codex_turn(tmp_path)
    pieces = [f"t{index} " for index in range(50)]
    process.feed = _turn_lines(
        *(_delta(method, "i", piece) for piece in pieces),
        {"method": "item/completed", "params": {"item": completed_item}},
        _turn_completed(),
    )

    rows = _stored(list(turn.iter_events()))

    assert _codex_deltas(rows) == [
        (method, "i", pieces[0]),
        (method, "i", "".join(pieces[1:])),
    ]
    # Everything gathered is stored before the item that ends the stream.
    assert [row.event_type for row in rows] == ["raw", "raw", completed_kind, "lifecycle"]
    merged = rows[1]
    assert merged.payload["protocol"] == "codex-app-server"
    # A merged row is a notification of the same shape the trajectory reads,
    # and its line is that notification, whole.
    assert merged.payload["notification"] == _delta(method, "i", "".join(pieces[1:]))
    assert json.loads(merged.raw_line or "") == merged.payload["notification"]
    assert merged.payload["text"] == merged.raw_line


def test_codex_merging_never_reorders_interleaved_streams(tmp_path):
    """Only adjacent deltas of one stream merge; any other line flushes first."""
    process, turn = _codex_turn(tmp_path)
    process.feed = _turn_lines(
        _delta(SUMMARY, "r", "A1", summaryIndex=0),
        _delta(SUMMARY, "r", "A2", summaryIndex=0),
        _delta(SUMMARY, "r", "A3", summaryIndex=0),
        _delta(SUMMARY, "r", "B1", summaryIndex=1),
        _delta(SUMMARY, "r", "B2", summaryIndex=1),
        _delta(REASONING, "r", "C1", contentIndex=0),
        {"method": "item/agentMessage/delta", "params": {"itemId": "m", "delta": "Hi"}},
        _delta(SUMMARY, "r", "A4", summaryIndex=0),
        _delta(SUMMARY, "r", "A5", summaryIndex=0),
        _delta(OUTPUT, "c", "out"),
        _turn_completed(),
    )

    rows = _stored(list(turn.iter_events()))

    labels = []
    for row in rows:
        notification = row.payload.get("notification") or {}
        if row.payload.get("protocol") == "reply-preview":
            labels.append(("preview", row.payload["delta"]))
        elif notification.get("method") in {SUMMARY, REASONING, OUTPUT}:
            params = notification["params"]
            index = params.get("summaryIndex", params.get("contentIndex"))
            labels.append((notification["method"], index, params["delta"]))
        else:
            labels.append((row.event_type, None))
    assert labels == [
        (SUMMARY, 0, "A1"),
        (SUMMARY, 0, "A2A3"),
        (SUMMARY, 1, "B1"),
        (SUMMARY, 1, "B2"),
        (REASONING, 0, "C1"),
        ("preview", "Hi"),
        (SUMMARY, 0, "A4A5"),
        (OUTPUT, None, "out"),
        ("lifecycle", None),
    ]


def test_codex_deltas_are_flushed_at_most_one_interval_apart(tmp_path):
    """A continuing stream is reported as it goes, not only when it ends."""
    times = iter([0.0, 0.05, 0.1, 0.25, 0.3, 0.3, 0.3])
    process, turn = _codex_turn(tmp_path, clock=lambda: next(times))
    process.feed = _turn_lines(
        *(_delta(OUTPUT, "c", f"d{index}") for index in range(5)),
        _turn_completed(),
    )

    rows = _stored(list(turn.iter_events()))

    assert [text for _, _, text in _codex_deltas(rows)] == ["d0", "d1d2d3", "d4"]


def test_a_merged_line_stays_within_what_a_raw_row_keeps(tmp_path):
    """Merging stops short of the 4,000 characters a raw row keeps of a line."""
    process, turn = _codex_turn(tmp_path)
    pieces = ["x" * 1500 for _ in range(6)]
    process.feed = _turn_lines(*(_delta(OUTPUT, "c", piece) for piece in pieces), _turn_completed())

    rows = [row for row in _stored(list(turn.iter_events())) if row.event_type == "raw"]

    assert "".join(text for _, _, text in _codex_deltas(rows)) == "".join(pieces)
    assert len(rows) < len(pieces)
    for row in rows:
        assert len(row.raw_line or "") <= DeltaBatcher.MAX_LINE_CHARS
        assert row.payload["text"] == row.raw_line


def test_an_interrupted_turn_reports_its_gathered_deltas_before_it_settles(tmp_path):
    process, turn = _codex_turn(tmp_path)
    process.feed = _turn_lines(_delta(REASONING, "r", "first "), _delta(REASONING, "r", "second"))
    events: Iterator[HarnessEvent] = turn.iter_events()
    seen = [next(events), next(events), next(events)]
    turn.cancel()
    assert json.loads(process.sent[-1])["method"] == "turn/interrupt"
    process.feed = _turn_lines(_turn_completed("interrupted"))
    seen.extend(events)

    rows = _stored(seen)
    assert [text for _, _, text in _codex_deltas(rows)] == ["first ", "second"]
    assert rows[-1].event_type == "lifecycle"
    assert turn.wait().status == "cancelled"


def test_a_stream_that_breaks_still_reports_its_gathered_deltas(tmp_path):
    """The process dying mid-turn is no reason to lose what it already said."""
    process, turn = _codex_turn(tmp_path)
    process.feed = _turn_lines(*(_delta(REASONING, "r", piece) for piece in ("a", "b", "c")))
    events: list[HarnessEvent] = []

    with pytest.raises(DriverError, match="exited unexpectedly"):
        for event in ResidentTurn(turn).lines():
            events.append(event)

    assert [text for _, _, text in _codex_deltas(_stored(events))] == ["a", "bc"]


def test_a_subagents_merged_deltas_stay_the_subagents(tmp_path):
    """A subagent thread's reasoning is merged as its own, never the conversation's.

    The pinned app-server sends a subagent thread's notifications to this
    client too, and the spawn names the thread before its own items arrive.
    """
    process, turn = _codex_turn(tmp_path)
    spawn = {
        "type": "collabAgentToolCall",
        "id": "call-spawn",
        "tool": "spawnAgent",
        "receiverThreadIds": ["child-t"],
    }
    process.feed = _turn_lines(
        {"method": "item/completed", "params": {"threadId": "thread-a", "item": spawn}},
        *(
            {"method": REASONING, "params": {"threadId": "child-t", "itemId": "r", "delta": piece}}
            for piece in ("c1 ", "c2 ", "c3")
        ),
        _delta(REASONING, "r", "own"),
        _turn_completed(),
    )

    rows = _stored(list(turn.iter_events()))

    owner = {"agent_thread_id": "child-t", "parent_tool_use_id": "call-spawn"}
    merged = []
    for row in rows:
        notification = row.payload.get("notification") or {}
        if notification.get("method") == REASONING:
            marks = {key: row.payload[key] for key in owner if key in row.payload}
            merged.append((notification["params"]["delta"], marks))
    assert merged == [("c1 ", owner), ("c2 c3", owner), ("own", {})]


@pytest.mark.parametrize(
    ("driver", "runtime"), [(OpenCodeDriver, OpencodeRuntime), (KiloDriver, KiloRuntime)]
)
def test_http_reasoning_deltas_are_merged_and_the_answer_still_previewed(tmp_path, driver, runtime):
    process = _LiveProcess()
    context, _ = _protocol_context(process, tmp_path)
    process.feed = _turn_lines({"bridge": "ready", "session_id": "session-a"})
    session = driver(runtime()).launch_live(context)
    session.ensure_started(model="deepseek-v4-flash", reasoning_effort=None)
    turn = session.start_turn(TurnInput(prompt="look", model="deepseek-v4-flash"))
    turn.deltas = DeltaBatcher(clock=_frozen)
    turn_id = json.loads(process.sent[-1])["id"]

    def event(kind: str, properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "bridge": "event",
            "turn_id": turn_id,
            "event": {"type": kind, "properties": properties},
        }

    def part(part_id: str, kind: str, text: str, **extra: Any) -> dict[str, Any]:
        return event(
            "message.part.updated",
            {"part": {"id": part_id, "messageID": "answer", "type": kind, "text": text, **extra}},
        )

    def delta(part_id: str, text: str) -> dict[str, Any]:
        return event(
            "message.part.delta",
            {"messageID": "answer", "partID": part_id, "field": "text", "delta": text},
        )

    thought = [f"step {index}. " for index in range(20)]
    process.feed = _turn_lines(
        event("message.updated", {"info": {"id": "answer", "role": "assistant"}}),
        part("thought", "reasoning", "", time={"start": 1}),
        *(delta("thought", piece) for piece in thought),
        part("thought", "reasoning", "".join(thought), time={"start": 1, "end": 2}),
        part("reply", "text", "", time={"start": 2}),
        delta("reply", "Done"),
        delta("reply", "."),
        part("reply", "text", "Done.", time={"start": 2, "end": 3}),
        {"bridge": "settled", "turn_id": turn_id, "status": "completed"},
    )

    rows = _stored(list(turn.iter_events()))

    reasoning = [
        json.loads(row.payload["text"])["event"]["properties"]["delta"]
        for row in rows
        if row.event_type == "raw"
        and '"message.part.delta"' in row.payload.get("text", "")
        and '"thought"' in row.payload["text"]
    ]
    assert reasoning == [thought[0], "".join(thought[1:])]
    shown = [
        "preview" if row.payload.get("protocol") == "reply-preview" else row.event_type
        for row in rows
        if row.event_type in {"thinking", "message"} or row.payload.get("protocol")
    ]
    # The merged reasoning is stored before the thinking block it builds, and
    # the answer is still previewed and then completed in its place.
    assert shown[0] == "thinking"
    assert shown.count("preview") >= 1
    assert shown[-1] == "message"
    assert turn.wait().final_output == "Done."
