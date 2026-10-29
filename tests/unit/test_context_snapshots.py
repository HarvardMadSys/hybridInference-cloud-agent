"""What each CLI says its context holds, and the protocol that asks it.

The Claude fixture is the real ``get_context_usage`` answer of the pinned
2.1.220 after one turn with a Bash call (captured against a loopback fake of
the Messages API; no credential). The driver tests drive the real session
classes over scripted processes and check that asking never takes a line the
turn or the next turn needs.
"""

from __future__ import annotations

import json
import queue
import runpy
import threading
from pathlib import Path

import pytest

from cloud_agent_host.drivers import (
    ClaudeCodeLiveSession,
    HttpAgentLiveSession,
    LaunchContext,
    PiRpcSession,
    TurnInput,
    driver_for,
)
from cloud_agent_host.runtimes import (
    EMPTY_RUNTIME_MCP_CONFIG,
    ClaudeCodeRuntime,
    OpencodeRuntime,
    PiRuntime,
)
from cloud_agent_host.runtimes.context_snapshot import (
    MAX_SNAPSHOT_ENTRIES,
    bridge_snapshot,
    claude_snapshot,
    pi_snapshot,
)
from cloud_agent_shared.text_tokens import estimate_text_tokens

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/context/claude_2_1_220_get_context_usage.json"
BRIDGE = runpy.run_path(str(REPO / "deploy/sandbox/http-agent-live-bridge"), run_name="bridge")


def _categories(snapshot):
    return {item["key"]: item for item in snapshot["categories"]}


# -- normalizers ----------------------------------------------------------------


def test_claude_meter_keeps_its_categories_and_splits_messages_by_kind():
    response = json.loads(FIXTURE.read_text())["response"]["response"]
    snapshot = claude_snapshot(response)
    categories = _categories(snapshot)
    assert snapshot["token_basis"] == "cli_meter"
    assert snapshot["total_tokens"] == 30328 and snapshot["context_window"] == 200_000
    assert categories["system_prompt"]["tokens"] == 7284
    assert categories["tool_definitions"]["tokens"] == 20151
    assert categories["skills"]["tokens"] == 1123
    # "Messages" is replaced by the CLI's own split; free space is not content.
    assert "messages" not in categories and "other" in categories
    assert categories["tool_calls"]["tokens"] == 32
    assert categories["tool_results"]["tokens"] == 28
    assert categories["injected"]["tokens"] == 1645
    assert all(item["key"] != "free_space" for item in snapshot["categories"])
    labels = {(e["category"], e["label"]) for e in snapshot["entries"]}
    assert ("tool_calls", "Bash") in labels and ("skills", "dataviz") in labels
    assert ("injected", "skill_listing") in labels
    assert snapshot["api_usage"]["input_tokens"] == 30328


def test_claude_meter_names_memory_files_and_mcp_tools_with_their_servers():
    snapshot = claude_snapshot(
        {
            "categories": [
                {"name": "MCP tools", "tokens": 900},
                {"name": "Memory files", "tokens": 120},
            ],
            "totalTokens": 1020,
            "maxTokens": 200_000,
            "memoryFiles": [{"path": "/state/claude/CLAUDE.md", "type": "User", "tokens": 120}],
            "mcpTools": [
                {"name": "mcp__session__search", "serverName": "session", "tokens": 500},
                {
                    "name": "mcp__session__fetch",
                    "serverName": "session",
                    "tokens": 400,
                    "isLoaded": False,
                },
            ],
        }
    )
    entries = {e["label"]: e for e in snapshot["entries"]}
    assert entries["/state/claude/CLAUDE.md"]["category"] == "instructions"
    assert entries["/state/claude/CLAUDE.md"]["source"] == "User"
    assert entries["mcp__session__search"]["source"] == "session"
    assert entries["mcp__session__fetch"]["deferred"] is True
    assert _categories(snapshot)["mcp_tools"]["tokens"] == 900


def test_pi_messages_become_entries_with_estimates_and_join_keys():
    messages = [
        {"role": "user", "content": "请修复 bug 函数返回值错误。"},
        {
            "role": "assistant",
            "responseId": "resp_1",
            "content": [
                {"type": "thinking", "thinking": "Look at utils.py first."},
                {
                    "type": "toolCall",
                    "id": "call_1",
                    "name": "read",
                    "arguments": {"path": "utils.py"},
                },
            ],
        },
        {
            "role": "toolResult",
            "toolCallId": "call_1",
            "toolName": "read",
            "content": [{"type": "text", "text": "def add(a, b):\n    return a - b\n" * 50}],
        },
        {"role": "compactionSummary", "summary": "之前 读取了 utils.py。", "tokensBefore": 900},
        {"role": "custom", "customType": "reminder", "content": "Use tests."},
    ]
    snapshot = pi_snapshot(messages, {"contextUsage": {"tokens": 4200, "contextWindow": 128_000}})
    categories = _categories(snapshot)
    assert snapshot["status"] == "ok" and snapshot["total_basis"] == "cli_estimate"
    assert snapshot["total_tokens"] == 4200 and snapshot["context_window"] == 128_000
    # pi does not expose these over RPC: unavailable, never zero.
    assert categories["system_prompt"] == {"key": "system_prompt", "tokens": None, "exposed": False}
    assert categories["tool_definitions"]["exposed"] is False
    user = next(e for e in snapshot["entries"] if e["category"] == "user_messages")
    assert user["tokens"] == estimate_text_tokens("请修复 bug 函数返回值错误。")
    result = next(e for e in snapshot["entries"] if e["category"] == "tool_results")
    assert result["ref"] == {"tool_use_id": "call_1"} and len(result["preview"]) <= 160
    call = next(e for e in snapshot["entries"] if e["category"] == "tool_calls")
    assert call["ref"] == {"tool_use_id": "call_1", "usage_id": "resp_1"}
    assert categories["compaction_summary"]["entries"] == 1
    assert categories["injected"]["entries"] == 1


def test_pi_without_messages_is_partial_and_says_so():
    snapshot = pi_snapshot(None, {"contextUsage": {"tokens": 250_000, "contextWindow": 400_000}})
    assert snapshot["status"] == "partial" and snapshot["detail"] == "messages_not_read"
    assert snapshot["entries"] == [] and snapshot["total_tokens"] == 250_000


def test_snapshots_keep_the_largest_entries_and_count_the_rest():
    messages = [
        {"role": "user", "content": "x" * (i + 1)} for i in range(MAX_SNAPSHOT_ENTRIES + 20)
    ]
    snapshot = pi_snapshot(messages, None)
    assert len(snapshot["entries"]) == MAX_SNAPSHOT_ENTRIES
    assert snapshot["entries_omitted"] == 20
    assert _categories(snapshot)["user_messages"]["entries"] == MAX_SNAPSHOT_ENTRIES + 20
    assert min(e["chars"] for e in snapshot["entries"]) == 21


# -- the bridge (OpenCode 1.18.9, Kilo 7.4.17) -------------------------------------


def _msg(mid, role, *parts, **info):
    return {"info": {"id": mid, "role": role, **info}, "parts": list(parts)}


def test_bridge_carries_what_the_server_carries_after_a_compaction():
    messages = [
        _msg("u1", "user", {"type": "text", "text": "old question"}),
        _msg("a1", "assistant", {"type": "text", "text": "old answer"}, parentID="u1"),
        _msg("u2", "user", {"type": "compaction", "auto": True}),
        _msg(
            "a2",
            "assistant",
            {"type": "text", "text": "摘要 之前的工作。"},
            summary=True,
            finish="stop",
            parentID="u2",
        ),
        _msg("u3", "user", {"type": "text", "text": "continue"}),
    ]
    carried = BRIDGE["carried_messages"](messages)
    assert [m["info"]["id"] for m in carried] == ["u2", "a2", "u3"]
    summary = BRIDGE["context_summary"](messages, [], provider="p")
    assert summary["excluded"]["compacted_messages"] == 2
    categories = {e["category"] for e in summary["entries"]}
    assert "compaction_summary" in categories and "user_messages" in categories
    assert all("old" not in e["preview"] for e in summary["entries"])


def test_bridge_keeps_a_retained_tail_before_the_summary_in_server_order():
    messages = [
        _msg("u1", "user", {"type": "text", "text": "dropped"}),
        _msg("u2", "user", {"type": "text", "text": "kept tail"}),
        _msg("u3", "user", {"type": "compaction", "auto": False, "tail_start_id": "u2"}),
        _msg(
            "a3",
            "assistant",
            {"type": "text", "text": "summary"},
            summary=True,
            finish="stop",
            parentID="u3",
        ),
        _msg("u4", "user", {"type": "text", "text": "next"}),
    ]
    assert [m["info"]["id"] for m in BRIDGE["carried_messages"](messages)] == [
        "u3",
        "a3",
        "u2",
        "u4",
    ]


def test_bridge_lists_pruned_outputs_as_pruned_and_tools_by_source():
    tool = {
        "type": "tool",
        "id": "p1",
        "callID": "call-1",
        "tool": "bash",
        "state": {
            "status": "completed",
            "input": {"command": "ls"},
            "output": "huge",
            "time": {"compacted": 1},
        },
    }
    messages = [
        _msg(
            "u1",
            "user",
            {"type": "text", "text": "hi"},
            {"type": "text", "text": "<reminder>", "synthetic": True},
        ),
        _msg("a1", "assistant", tool, providerID="p", modelID="m"),
    ]
    tools = [
        {"id": "bash", "description": "Run a command", "parameters": {"type": "object"}},
        {"id": "session_github__search", "description": "Search", "parameters": {}},
        {"id": "invalid", "description": "internal", "parameters": {}},
    ]
    summary = BRIDGE["context_summary"](messages, tools, provider="p")
    entries = {(e["category"], e["label"]): e for e in summary["entries"]}
    assert entries[("tool_results", "bash")]["pruned"] is True
    assert entries[("tool_results", "bash")]["preview"] == BRIDGE["PRUNED_OUTPUT"]
    assert summary["excluded"]["pruned_tool_outputs"] == 1
    assert entries[("injected", "user")]["synthetic"] is True
    assert ("mcp_tools", "session_github__search") in entries
    assert ("tool_definitions", "bash") in entries
    # Never offered to the model, so not part of what a request carries.
    assert ("tool_definitions", "invalid") not in entries
    assert BRIDGE["latest_model"](messages) == ("p", "m")
    snapshot = bridge_snapshot(summary, runtime="opencode")
    categories = _categories(snapshot)
    assert snapshot["source"] == "opencode_session_messages"
    assert categories["system_prompt"]["exposed"] is False
    assert snapshot["excluded"] == {"compacted_messages": 0, "pruned_tool_outputs": 1}
    chinese = BRIDGE["_context_entry"]("user_messages", "user", "中文abc")
    assert (chinese["cjk"], chinese["ascii"]) == (2, 3)


# -- the protocol ---------------------------------------------------------------------


class _QueuedProcess:
    """A resident process whose stdout blocks until a line is queued.

    ``on_write`` answers stdin lines, as the CLI would.
    """

    def __init__(self, on_write=None) -> None:
        self.sent: list[dict] = []
        self._out: queue.Queue[str | None] = queue.Queue()
        self._on_write = on_write
        self.killed = False

    def emit(self, *records: dict) -> None:
        for record in records:
            self._out.put(json.dumps(record))

    def write_line(self, text: str) -> None:
        record = json.loads(text)
        self.sent.append(record)
        if self._on_write:
            self._on_write(self, record)

    def close_stdin(self) -> None:
        self._out.put(None)

    def lines(self):
        while True:
            line = self._out.get()
            if line is None:
                return
            yield line

    def kill(self) -> None:
        self.killed = True

    def poll(self):
        return None

    def wait(self) -> int:
        return 0

    def stderr_text(self) -> str:
        return ""


def _context(process, tmp_path, *, snapshots=True) -> LaunchContext:
    return LaunchContext(
        workdir="/w",
        gateway_base_url="http://gw",
        credential="agr",
        provides_isolation=True,
        mcp_config=EMPTY_RUNTIME_MCP_CONFIG,
        state_dir=str(tmp_path),
        spawn_live=lambda argv, env: process,
        context_snapshots=snapshots,
    )


_RESULT = {"type": "result", "subtype": "success", "result": "done"}
_ANSWER = {
    "type": "assistant",
    "message": {"id": "m1", "content": [{"type": "text", "text": "done"}]},
}


def _turn_events(session, prompt="go"):
    turn = session.start_turn(TurnInput(prompt=prompt, model="m"))
    from cloud_agent_host.resident_transport import ResidentTurn

    events = list(ResidentTurn(turn).lines())
    return turn, events


def test_claude_asks_after_the_result_and_puts_back_what_is_not_its_answer(tmp_path):
    fixture = json.loads(FIXTURE.read_text())

    def answer(process, record):
        if record.get("type") == "user":
            process.emit(_ANSWER, _RESULT)
        elif record.get("request", {}).get("subtype") == "get_context_usage":
            # Something else arrives first: it belongs to whoever reads next.
            process.emit({"type": "system", "subtype": "status", "status": None})
            reply = json.loads(json.dumps(fixture))
            reply["response"]["request_id"] = record["request_id"]
            process.emit(reply)

    process = _QueuedProcess(answer)
    session = driver_for(ClaudeCodeRuntime()).launch_live(_context(process, tmp_path))
    assert isinstance(session, ClaudeCodeLiveSession)
    turn, events = _turn_events(session)
    snapshots = [e for e in events if e.payload.get("phase") == "context_snapshot"]
    assert len(snapshots) == 1
    assert snapshots[0].payload["snapshot"]["source"] == "claude_get_context_usage"
    # Asked only after the turn's result, and the turn completed as before.
    kinds = [record.get("type") for record in process.sent]
    assert kinds == ["user", "control_request"]
    assert turn.wait().status == "completed"
    assert list(session._pushed_back) == [
        json.dumps({"type": "system", "subtype": "status", "status": None})
    ]


def test_claude_late_answer_never_reaches_the_next_turn(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud_agent_host.drivers.CLAUDE_CONTEXT_USAGE_S", 0.2)
    pending: list[dict] = []

    def answer(process, record):
        if record.get("type") == "user":
            if pending:
                # The context answer the first turn gave up on arrives now,
                # ahead of this turn's own lines.
                late = json.loads(FIXTURE.read_text())
                late["response"]["request_id"] = pending[0]["request_id"]
                process.emit(late)
            process.emit(_ANSWER, _RESULT)
        elif record.get("type") == "control_request":
            pending.append(record)

    process = _QueuedProcess(answer)
    session = driver_for(ClaudeCodeRuntime()).launch_live(_context(process, tmp_path))
    first, events = _turn_events(session)
    snapshot = next(e for e in events if e.payload.get("phase") == "context_snapshot")
    assert snapshot.payload["snapshot"]["status"] == "timeout"
    assert first.wait().status == "completed"
    monkeypatch.setattr("cloud_agent_host.drivers.CLAUDE_CONTEXT_USAGE_S", 5.0)
    pending_answered = len(pending)
    second, events = _turn_events(session, "again")
    assert second.wait().status == "completed"
    assert pending_answered == 1
    # The late reply was dropped, not read as this turn's line.
    assert not any("control_response" in (e.raw_line or "") for e in events)
    assert all(e.payload.get("phase") != "unknown" for e in events)


def test_claude_does_not_ask_after_an_error_result_or_when_not_launched_for_it(tmp_path):
    def answer(process, record):
        if record.get("type") == "user":
            process.emit({"type": "result", "subtype": "error_during_execution", "is_error": True})

    process = _QueuedProcess(answer)
    session = driver_for(ClaudeCodeRuntime()).launch_live(_context(process, tmp_path))
    _turn, events = _turn_events(session)
    assert not any(e.payload.get("phase") == "context_snapshot" for e in events)
    assert [r.get("type") for r in process.sent] == ["user"]

    quiet = _QueuedProcess(
        lambda p, r: p.emit(_ANSWER, _RESULT) if r.get("type") == "user" else None
    )
    session = driver_for(ClaudeCodeRuntime()).launch_live(
        _context(quiet, tmp_path, snapshots=False)
    )
    _turn, events = _turn_events(session)
    assert [r.get("type") for r in quiet.sent] == ["user"]


def _pi_answer(stats_tokens):
    def answer(process, record):
        kind = record.get("type")
        if kind == "get_state":
            process.emit(
                {
                    "type": "response",
                    "id": record["id"],
                    "command": "get_state",
                    "success": True,
                    "data": {"sessionId": "s1"},
                }
            )
        elif kind == "prompt":
            process.emit(
                {"type": "response", "id": record["id"], "command": "prompt", "success": True},
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": [], "stopReason": "stop"}],
                },
            )
        elif kind == "get_session_stats":
            # An auto-compaction pi started after agent_end comes first.
            process.emit({"type": "compaction_start", "reason": "threshold"})
            process.emit(
                {
                    "type": "response",
                    "id": record["id"],
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {"tokens": stats_tokens, "contextWindow": 128_000}},
                }
            )
        elif kind == "get_messages":
            process.emit(
                {
                    "type": "response",
                    "id": record["id"],
                    "command": "get_messages",
                    "success": True,
                    "data": {"messages": [{"role": "user", "content": "你好"}]},
                }
            )

    return answer


def test_pi_reads_stats_then_messages_and_keeps_other_lines_for_the_next_turn(tmp_path):
    process = _QueuedProcess(_pi_answer(5000))
    session = driver_for(PiRuntime()).launch_live(_context(process, tmp_path))
    assert isinstance(session, PiRpcSession)
    _turn, events = _turn_events(session)
    snapshot = next(e for e in events if e.payload.get("phase") == "context_snapshot")
    assert snapshot.payload["snapshot"]["status"] == "ok"
    assert snapshot.payload["snapshot"]["total_tokens"] == 5000
    assert [r["type"] for r in process.sent if r["type"].startswith("get_")][-2:] == [
        "get_session_stats",
        "get_messages",
    ]
    assert any('"compaction_start"' in line for line in session._prefetched)


def test_pi_does_not_read_messages_past_the_size_limit(tmp_path):
    process = _QueuedProcess(_pi_answer(500_000))
    session = driver_for(PiRuntime()).launch_live(_context(process, tmp_path))
    _turn, events = _turn_events(session)
    snapshot = next(e for e in events if e.payload.get("phase") == "context_snapshot")
    assert snapshot.payload["snapshot"]["status"] == "partial"
    assert "get_messages" not in [r["type"] for r in process.sent]


@pytest.mark.parametrize("supported", [True, False])
def test_bridge_context_is_read_between_turns_and_an_old_bridge_is_unsupported(tmp_path, supported):
    def answer(process, record):
        kind = record.get("type")
        if kind == "turn":
            process.emit({"bridge": "turn_accepted", "turn_id": record["id"]})
            process.emit({"bridge": "settled", "turn_id": record["id"], "status": "completed"})
        elif kind == "context":
            if supported:
                summary = BRIDGE["context_summary"](
                    [_msg("u1", "user", {"type": "text", "text": "hi"})], [], provider="p"
                )
                process.emit({"bridge": "context_result", "id": record["id"], "context": summary})
            else:
                process.emit(
                    {
                        "bridge": "command_error",
                        "id": record["id"],
                        "detail": "unknown command type: 'context'",
                    }
                )
        elif kind == "ping":
            process.emit({"bridge": "pong", "id": record["id"], "session_id": "ses_1"})

    process = _QueuedProcess(answer)
    process.emit({"bridge": "ready", "session_id": "ses_1"})
    session = driver_for(OpencodeRuntime()).launch_live(_context(process, tmp_path))
    assert isinstance(session, HttpAgentLiveSession)
    try:
        _turn, events = _turn_events(session)
    except Exception as exc:  # pragma: no cover - diagnostic
        pytest.fail(f"bridge turn failed: {exc}")
    snapshot = next(e for e in events if e.payload.get("phase") == "context_snapshot")
    expected = "ok" if supported else "unsupported"
    assert snapshot.payload["snapshot"]["status"] == expected


def test_queued_process_helper_is_thread_safe():
    process = _QueuedProcess()
    threading.Thread(target=lambda: process.emit({"a": 1})).start()
    assert next(process.lines()) == '{"a": 1}'
