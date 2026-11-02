"""Context reads that leave the conversation alone.

No CLI is asked anything after a turn's result: the drivers' tests drive the
real session classes over scripted processes and check what was written to
them. OpenCode and Kilo are read by the bridge itself, beside the turn; its
tests run the real bridge against a loopback fake of the server, including a
read that hangs, one that is too large, and an immediate interrupt and
follow-up turn, and check that the turn never waits and a read never follows
its turn's end.
"""

from __future__ import annotations

import json
import queue
import runpy
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from cloud_agent_host.resident_transport import ResidentTurn
from cloud_agent_host.runtimes import (
    EMPTY_RUNTIME_MCP_CONFIG,
    ClaudeCodeRuntime,
    OpencodeRuntime,
    PiRuntime,
)
from cloud_agent_host.runtimes.context_snapshot import MAX_SNAPSHOT_ENTRIES, bridge_snapshot

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy/sandbox/http-agent-live-bridge"
BRIDGE = runpy.run_path(str(SCRIPT), run_name="bridge")


def _categories(snapshot):
    return {item["key"]: item for item in snapshot["categories"]}


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
    snapshot = bridge_snapshot(summary, runtime="opencode")
    categories = _categories(snapshot)
    assert snapshot["source"] == "opencode_session_messages"
    assert categories["system_prompt"]["exposed"] is False
    assert snapshot["excluded"] == {"compacted_messages": 0, "pruned_tool_outputs": 1}
    chinese = BRIDGE["_context_entry"]("user_messages", "user", "中文abc")
    assert (chinese["cjk"], chinese["ascii"]) == (2, 3)


def test_the_read_ends_at_this_turns_prompt_and_only_once_it_is_listed():
    history = [
        _msg("u1", "user", {"type": "text", "text": "继续"}, time={"created": 1000}),
        _msg("a1", "assistant", {"type": "text", "text": "ok"}, parentID="u1"),
    ]
    request_messages = BRIDGE["request_messages"]
    # The same words asked in an earlier turn are not this turn's prompt.
    assert request_messages(history, "继续", since_ms=5000) is None
    listed = [
        *history,
        _msg("u2", "user", {"type": "text", "text": "继续"}, time={"created": 6000}),
        _msg("a2", "assistant", {"type": "text", "text": "this turn's own output"}),
    ]
    carried = request_messages(listed, "继续", since_ms=5000)
    assert [m["info"]["id"] for m in carried] == ["u1", "a1", "u2"]


def test_snapshots_keep_the_largest_entries_and_count_the_rest():
    messages = [
        _msg(f"u{i}", "user", {"type": "text", "text": "x" * (i + 1)})
        for i in range(MAX_SNAPSHOT_ENTRIES + 20)
    ]
    summary = BRIDGE["context_summary"](messages, None)
    assert summary["tools_listed"] is False
    assert len(summary["entries"]) == MAX_SNAPSHOT_ENTRIES
    snapshot = bridge_snapshot(summary, runtime="kilo")
    assert len(snapshot["entries"]) == MAX_SNAPSHOT_ENTRIES
    assert snapshot["observed"] == "turn_start" and snapshot["v"] == 2
    assert min(e["chars"] for e in snapshot["entries"]) == 21


# -- the drivers ---------------------------------------------------------------------


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


def _context(process, tmp_path) -> LaunchContext:
    return LaunchContext(
        workdir="/w",
        gateway_base_url="http://gw",
        credential="agr",
        provides_isolation=True,
        mcp_config=EMPTY_RUNTIME_MCP_CONFIG,
        state_dir=str(tmp_path),
        spawn_live=lambda argv, env: process,
    )


_RESULT = {"type": "result", "subtype": "success", "result": "done"}
_ANSWER = {
    "type": "assistant",
    "message": {"id": "m1", "content": [{"type": "text", "text": "done"}]},
}


def _turn_events(session, prompt="go"):
    turn = session.start_turn(TurnInput(prompt=prompt, model="m"))
    events = list(ResidentTurn(turn).lines())
    return turn, events


def test_claude_is_asked_nothing_after_a_result_and_the_next_turn_goes_straight_in(tmp_path):
    def answer(process, record):
        if record.get("type") == "user":
            process.emit(_ANSWER, _RESULT)

    process = _QueuedProcess(answer)
    session = driver_for(ClaudeCodeRuntime()).launch_live(_context(process, tmp_path))
    assert isinstance(session, ClaudeCodeLiveSession)
    first, events = _turn_events(session)
    assert first.wait().status == "completed"
    second, _events = _turn_events(session, "again")
    assert second.wait().status == "completed"
    # Two prompts and nothing else: no get_context_usage, so no token counting.
    assert [record.get("type") for record in process.sent] == ["user", "user"]
    assert not any(e.payload.get("phase") == "context_snapshot" for e in events)


def test_pi_is_asked_nothing_after_its_turn(tmp_path):
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

    process = _QueuedProcess(answer)
    session = driver_for(PiRuntime()).launch_live(_context(process, tmp_path))
    assert isinstance(session, PiRpcSession)
    turn, events = _turn_events(session)
    assert turn.wait().status == "completed"
    sent = [record["type"] for record in process.sent]
    assert "get_session_stats" not in sent and "get_messages" not in sent
    assert sent[-1] == "prompt"
    assert not any(e.payload.get("phase") == "context_snapshot" for e in events)


def test_the_bridges_read_arrives_inside_its_turn_and_nothing_is_asked_for_it(tmp_path):
    summary = BRIDGE["context_summary"](
        [_msg("u1", "user", {"type": "text", "text": "hi"})], [], provider="freeinference"
    )

    def answer(process, record):
        kind = record.get("type")
        if kind == "turn":
            process.emit({"bridge": "turn_accepted", "turn_id": record["id"]})
            # A read for another turn is never this turn's.
            process.emit({"bridge": "context", "turn_id": "turn-other", "context": summary})
            process.emit({"bridge": "context", "turn_id": record["id"], "context": summary})
            process.emit({"bridge": "settled", "turn_id": record["id"], "status": "completed"})
        elif kind == "ping":
            process.emit({"bridge": "pong", "id": record["id"], "session_id": "ses_1"})

    process = _QueuedProcess(answer)
    process.emit({"bridge": "ready", "session_id": "ses_1"})
    session = driver_for(OpencodeRuntime()).launch_live(_context(process, tmp_path))
    assert isinstance(session, HttpAgentLiveSession)
    turn, events = _turn_events(session)
    assert turn.wait().status == "completed"
    snapshots = [e for e in events if e.payload.get("phase") == "context_snapshot"]
    assert len(snapshots) == 1
    snapshot = snapshots[0].payload["snapshot"]
    assert (snapshot["status"], snapshot["observed"]) == ("ok", "turn_start")
    assert snapshot["source"] == "opencode_session_messages"
    assert [r["type"] for r in process.sent if r["type"] != "ping"] == ["turn"]


# -- the bridge's own read, against a loopback fake of the server -------------------


class _FakeServer:
    """The pinned servers' endpoints the bridge uses, with a scriptable message list."""

    def __init__(self) -> None:
        self.messages: list[dict] = [
            _msg("u0", "user", {"type": "text", "text": "earlier"}, time={"created": 1}),
            _msg("a0", "assistant", {"type": "text", "text": "earlier answer"}, parentID="u0"),
        ]
        self.requests: list[str] = []
        self.list_gate = threading.Event()
        self.list_gate.set()
        self.list_body: bytes | None = None
        self.unlisted_reads = 0
        self._pending: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, body: bytes, code: int = 200) -> None:
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                server.requests.append(f"GET {self.path}")
                if self.path.endswith("/message"):
                    server.list_gate.wait(10)
                    if server.unlisted_reads:
                        server.unlisted_reads -= 1
                    else:
                        server.messages.extend(server._pending)
                        server._pending.clear()
                    body = server.list_body or json.dumps(server.messages).encode()
                    return self._send(body)
                if self.path.startswith("/experimental/tool"):
                    tools = [{"id": "bash", "description": "Run", "parameters": {}}]
                    return self._send(json.dumps(tools).encode())
                return self._send(b"{}", 404)

            def do_POST(self):
                server.requests.append(f"POST {self.path}")
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path.endswith("/prompt_async"):
                    text = payload["parts"][0]["text"]
                    created = time.time() * 1000
                    server._pending += [
                        _msg(
                            f"u-{text}",
                            "user",
                            {"type": "text", "text": text},
                            time={"created": created},
                        ),
                        _msg(f"a-{text}", "assistant", {"type": "text", "text": "streaming"}),
                    ]
                    return self._send(b"", 204)
                return self._send(b"true")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.list_gate.set()
        self.httpd.shutdown()


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("OPENCODE_GATEWAY_MODEL", "model-a")
    monkeypatch.setenv("AGENT_LIVE_SESSION_ID", "ses-1")
    server = _FakeServer()
    instance = BRIDGE["Bridge"]("opencode")
    instance.base_url = server.url
    instance._events_connected.set()
    instance.emitted = []
    instance.emit = lambda value: instance.emitted.append((time.monotonic(), value))
    globals_ = BRIDGE["Bridge"]._read_context.__globals__
    monkeypatch.setitem(globals_, "CONTEXT_PROMPT_POLL_S", 0.01)
    yield instance, server
    instance._stopping.set()
    server.close()


def _kinds(bridge) -> list[tuple[str, str | None]]:
    """What the bridge wrote, without the server events it relays."""
    return [
        (value["bridge"], value.get("turn_id"))
        for _at, value in bridge.emitted
        if value["bridge"] != "event"
    ]


def _settle(bridge, status="idle"):
    for kind in ("busy", status):
        event = {
            "type": "session.status",
            "properties": {"sessionID": "ses-1", "status": {"type": kind}},
        }
        bridge._event_payload(json.dumps(event))


def _until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def test_the_bridge_reads_the_first_requests_messages_while_the_turn_runs(bridge):
    bridge, server = bridge
    bridge.command({"id": "turn-1", "type": "turn", "prompt": "写测试 write tests"})
    _until(lambda: any(kind == "context" for kind, _turn in _kinds(bridge)))
    _settle(bridge)
    assert _kinds(bridge) == [
        ("turn_accepted", "turn-1"),
        ("context", "turn-1"),
        ("settled", "turn-1"),
    ]
    context = next(v for _at, v in bridge.emitted if v["bridge"] == "context")["context"]
    previews = [entry["preview"] for entry in context["entries"]]
    assert "写测试 write tests" in previews and "earlier answer" in previews
    # The turn's own output is not what its first request was sent with.
    assert "streaming" not in previews
    assert ("tool_definitions", "bash") in {(e["category"], e["label"]) for e in context["entries"]}
    assert all(not path.startswith("POST") or "prompt_async" in path for path in server.requests)


def test_a_hung_read_holds_back_no_interrupt_settlement_or_next_turn(bridge):
    bridge, server = bridge
    server.list_gate.clear()  # the server never answers the read in time
    started = time.monotonic()
    bridge.command({"id": "turn-1", "type": "turn", "prompt": "one"})
    bridge.command({"id": "int-1", "type": "interrupt", "turn_id": "turn-1"})
    _settle(bridge)
    bridge.command({"id": "turn-2", "type": "turn", "prompt": "two"})
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, elapsed
    assert "POST /session/ses-1/abort" in server.requests
    assert _kinds(bridge) == [
        ("turn_accepted", "turn-1"),
        ("interrupt_accepted", None),
        ("settled", "turn-1"),
        ("turn_accepted", "turn-2"),
    ]
    # The hung read answers late: turn 1 is over, so it is dropped, and turn 2
    # was not read while it was in flight.
    server.list_gate.set()
    bridge._context_reader.join(5)
    assert not bridge._context_reader.is_alive()
    _settle(bridge)
    assert [kind for kind, _turn in _kinds(bridge)].count("context") == 0
    assert _kinds(bridge)[-1] == ("settled", "turn-2")


def test_a_read_over_its_size_bound_or_never_listing_the_prompt_is_dropped(bridge, monkeypatch):
    bridge, server = bridge
    globals_ = BRIDGE["Bridge"]._read_context.__globals__
    monkeypatch.setitem(globals_, "CONTEXT_READ_MAX_BYTES", 64)
    bridge.command({"id": "turn-1", "type": "turn", "prompt": "big"})
    bridge._context_reader.join(5)
    _settle(bridge)
    monkeypatch.setitem(globals_, "CONTEXT_READ_MAX_BYTES", 8 * 1024 * 1024)
    server.unlisted_reads = 1000  # the prompt never shows up in the list
    bridge.command({"id": "turn-2", "type": "turn", "prompt": "unlisted"})
    bridge._context_reader.join(5)
    _settle(bridge)
    assert [kind for kind, _turn in _kinds(bridge)] == [
        "turn_accepted",
        "settled",
        "turn_accepted",
        "settled",
    ]


def test_the_read_waits_for_the_prompt_to_be_listed(bridge):
    bridge, server = bridge
    server.unlisted_reads = 3  # listed only from the fourth read on
    bridge.command({"id": "turn-1", "type": "turn", "prompt": "later"})
    bridge._context_reader.join(5)
    assert ("context", "turn-1") in _kinds(bridge)
    assert sum(1 for path in server.requests if path.endswith("/message")) == 4


def test_queued_process_helper_is_thread_safe():
    process = _QueuedProcess()
    threading.Thread(target=lambda: process.emit({"a": 1})).start()
    assert next(process.lines()) == '{"a": 1}'
