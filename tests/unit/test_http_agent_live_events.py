"""Native OpenCode/Kilo events retain message ownership across the bridge."""

import json
import runpy
import threading
from pathlib import Path

import pytest
from tests.unit.test_live_sessions import _LiveProcess, _protocol_context, _turn_lines

from cloud_agent_host.drivers import KiloDriver, OpenCodeDriver, TurnInput
from cloud_agent_host.runtimes import KiloRuntime, OpencodeRuntime


def test_bridge_preserves_nested_message_metadata_only_for_its_session():
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    bridge_type = runpy.run_path(str(script))["Bridge"]
    bridge = object.__new__(bridge_type)
    bridge.session_id = "session-a"
    bridge._turn_id = "turn-a"
    bridge._turn_lock = threading.Lock()
    emitted = []
    bridge.emit = emitted.append
    for session_id in ("session-other", "session-a"):
        bridge._event_payload(
            json.dumps(
                {
                    "type": "message.updated",
                    "properties": {
                        "info": {"id": "answer", "sessionID": session_id, "role": "assistant"}
                    },
                }
            )
        )
    assert len(emitted) == 1
    assert emitted[0]["turn_id"] == "turn-a"
    assert emitted[0]["event"]["properties"]["info"]["role"] == "assistant"


def test_bridge_waits_for_the_initial_sse_subscription():
    """A turn cannot start in the gap before live-only events are subscribed."""
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    bridge_type = runpy.run_path(str(script))["Bridge"]
    bridge = object.__new__(bridge_type)
    bridge.runtime = "opencode"
    bridge._events_connected = threading.Event()
    bridge._turn_lock = threading.Lock()
    bridge._turn_id = None
    bridge.emit = lambda _value: None

    entered = threading.Event()
    release = threading.Event()
    errors = []

    def events():
        entered.set()
        release.wait()
        bridge._event_payload(json.dumps({"type": "server.connected", "properties": {}}))

    bridge._events = events

    def start_events():
        try:
            bridge._start_event_stream()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    waiter = threading.Thread(target=start_events)
    waiter.start()
    assert entered.wait(1)
    assert waiter.is_alive()
    release.set()
    waiter.join(1)

    assert not waiter.is_alive()
    assert errors == []


def test_bridge_backs_off_repeated_clean_sse_eof(monkeypatch, capsys):
    """HTTP 200 plus immediate EOF cannot spin or flood bridge diagnostics."""
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    namespace = runpy.run_path(str(script))
    bridge = object.__new__(namespace["Bridge"])
    bridge.runtime = "opencode"
    bridge.base_url = "http://127.0.0.1:1"
    bridge._stopping = threading.Event()
    bridge._headers = lambda **_kwargs: {}
    bridge._turn_lock = threading.Lock()
    bridge._turn_id = None
    bridge._events_connected = threading.Event()
    emitted = []
    losses = []
    delays = []
    bridge.emit = emitted.append

    connected = json.dumps({"type": "server.connected", "properties": {}}).encode()

    class ConnectedThenEof:
        def __enter__(self):
            return (b"data: " + connected + b"\n", b"\n")

        def __exit__(self, *_args):
            return False

    def lost(detail):
        losses.append(detail)
        bridge._events_connected.clear()
        if len(losses) == 6:
            bridge._stopping.set()

    bridge._event_stream_lost = lost
    monkeypatch.setattr(
        namespace["urllib"].request,
        "urlopen",
        lambda *_args, **_kwargs: ConnectedThenEof(),
    )
    monkeypatch.setattr(namespace["time"], "sleep", delays.append)

    bridge._events()

    assert losses == ["closed by server"] * 6
    assert delays == pytest.approx([0.05, 0.1, 0.15, 0.2, 0.25, 0.3])
    assert emitted == []
    diagnostics = capsys.readouterr().err
    assert "event stream reconnect 1: closed by server" in diagnostics
    assert "event stream reconnect 5: closed by server" in diagnostics


def test_bridge_resets_reconnect_backoff_after_sse_heartbeat(monkeypatch, capsys):
    """A live subscription earns a fresh retry budget after its heartbeat."""
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    namespace = runpy.run_path(str(script))
    bridge = object.__new__(namespace["Bridge"])
    bridge.runtime = "opencode"
    bridge.base_url = "http://127.0.0.1:1"
    bridge._stopping = threading.Event()
    bridge._headers = lambda **_kwargs: {}
    bridge._turn_lock = threading.Lock()
    bridge._turn_id = None
    bridge._events_connected = threading.Event()
    emitted = []
    losses = []
    delays = []
    bridge.emit = emitted.append

    connected = json.dumps({"type": "server.connected", "properties": {}}).encode()
    responses = iter(
        [
            (b"data: " + connected + b"\n", b"\n"),
            (b"data: " + connected + b"\n", b"\n", b": heartbeat\n", b"\n"),
        ]
    )

    class Response:
        def __init__(self, lines):
            self.lines = lines

        def __enter__(self):
            return self.lines

        def __exit__(self, *_args):
            return False

    def lost(detail):
        losses.append(detail)
        bridge._events_connected.clear()
        if len(losses) == 2:
            bridge._stopping.set()

    bridge._event_stream_lost = lost
    monkeypatch.setattr(
        namespace["urllib"].request,
        "urlopen",
        lambda *_args, **_kwargs: Response(next(responses)),
    )
    monkeypatch.setattr(namespace["time"], "sleep", delays.append)

    bridge._events()

    assert losses == ["closed by server", "closed by server"]
    assert delays == pytest.approx([0.05, 0.05])
    assert emitted == []
    assert capsys.readouterr().err.count("event stream reconnect 1: closed by server") == 2


def test_bridge_waits_for_fresh_sse_after_idle_disconnect():
    """A later turn cannot enter while the live-only stream reconnects."""
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    bridge_type = runpy.run_path(str(script))["Bridge"]
    bridge = object.__new__(bridge_type)
    bridge.runtime = "opencode"
    bridge.session_id = "session-a"
    bridge.model = "model-a"
    bridge._events_connected = threading.Event()
    bridge._events_connected.set()
    bridge._turn_lock = threading.Lock()
    bridge._turn_id = None
    bridge._turn_saw_busy = False
    bridge._turn_failed = False
    bridge._questions = set()
    bridge._permission_sessions = {"session-a"}
    emitted = []
    requested = []
    bridge.emit = emitted.append
    bridge.request = lambda method, path, payload=None, **_kwargs: requested.append(
        (method, path, payload)
    )

    bridge._event_stream_lost("test disconnect")
    assert not bridge._events_connected.is_set()

    entered = threading.Event()
    errors = []

    def admit_turn():
        entered.set()
        try:
            bridge.command({"id": "turn-a", "type": "turn", "prompt": "hello"})
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    waiter = threading.Thread(target=admit_turn, daemon=True)
    waiter.start()
    assert entered.wait(1)
    assert waiter.is_alive()

    bridge._event_payload(json.dumps({"type": "server.connected", "properties": {}}))
    waiter.join(1)

    assert not waiter.is_alive()
    assert errors == []
    assert requested == [
        (
            "POST",
            "/session/session-a/prompt_async",
            {
                "model": {"providerID": "freeinference", "modelID": "model-a"},
                "parts": [{"type": "text", "text": "hello"}],
            },
        )
    ]
    assert emitted[-1] == {"bridge": "turn_accepted", "turn_id": "turn-a"}


def test_bridge_stream_loss_aborts_and_fails_the_active_turn():
    """Missing live-only events must become a terminal result, never a wait."""
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    bridge_type = runpy.run_path(str(script))["Bridge"]
    bridge = object.__new__(bridge_type)
    bridge.runtime = "kilo"
    bridge.session_id = "session-a"
    bridge._events_connected = threading.Event()
    bridge._events_connected.set()
    bridge._turn_lock = threading.Lock()
    bridge._turn_id = "turn-a"
    bridge._turn_saw_busy = True
    bridge._turn_failed = False
    bridge._questions = set()
    bridge._permission_sessions = {"session-a"}
    emitted = []
    requested = []
    bridge.emit = emitted.append
    bridge.request = lambda method, path, payload=None, **kwargs: requested.append(
        (method, path, payload, kwargs)
    )

    bridge._event_stream_lost("test disconnect")

    assert not bridge._events_connected.is_set()
    assert requested == [("POST", "/session/session-a/abort", None, {"timeout": 5})]
    assert bridge._turn_id is None
    assert emitted[0]["bridge"] == "diagnostic"
    assert emitted[-1] == {
        "bridge": "settled",
        "turn_id": "turn-a",
        "status": "failed",
    }


def test_bridge_disconnect_during_prompt_admission_aborts_after_acceptance():
    """Loss waits for admission ownership, then terminates that exact turn."""
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    bridge_type = runpy.run_path(str(script))["Bridge"]
    bridge = object.__new__(bridge_type)
    bridge.runtime = "opencode"
    bridge.session_id = "session-a"
    bridge.model = "model-a"

    cleared = threading.Event()

    class ConnectionEvent(threading.Event):
        def clear(self):
            super().clear()
            cleared.set()

    bridge._events_connected = ConnectionEvent()
    bridge._events_connected.set()
    bridge._turn_lock = threading.Lock()
    bridge._turn_id = None
    bridge._turn_saw_busy = False
    bridge._turn_failed = False
    bridge._questions = set()
    bridge._permission_sessions = {"session-a"}
    emitted = []
    requested = []
    prompt_entered = threading.Event()
    release_prompt = threading.Event()
    errors = []
    bridge.emit = emitted.append

    def request(method, path, payload=None, **kwargs):
        requested.append((method, path, payload, kwargs))
        if path.endswith("/prompt_async"):
            prompt_entered.set()
            assert release_prompt.wait(1)
        return True

    bridge.request = request

    def admit_turn():
        try:
            bridge.command({"id": "turn-a", "type": "turn", "prompt": "hello"})
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    admission = threading.Thread(target=admit_turn, daemon=True)
    admission.start()
    assert prompt_entered.wait(1)

    loss = threading.Thread(
        target=bridge._event_stream_lost,
        args=("test disconnect",),
        daemon=True,
    )
    loss.start()
    assert cleared.wait(1)
    assert loss.is_alive()

    release_prompt.set()
    admission.join(1)
    loss.join(1)

    assert not admission.is_alive()
    assert not loss.is_alive()
    assert errors == []
    assert [item["bridge"] for item in emitted] == [
        "turn_accepted",
        "diagnostic",
        "settled",
    ]
    assert [item[1] for item in requested] == [
        "/session/session-a/prompt_async",
        "/session/session-a/abort",
    ]
    assert bridge._turn_id is None


def test_bridge_abort_drains_old_events_before_replacement_admission():
    """Queued abort cleanup remains owned by the failed turn through native idle."""
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    bridge_type = runpy.run_path(str(script))["Bridge"]
    bridge = object.__new__(bridge_type)
    bridge.runtime = "kilo"
    bridge.session_id = "session-a"
    bridge.model = "model-b"
    bridge._events_connected = threading.Event()
    bridge._events_connected.set()
    bridge._turn_lock = threading.Lock()
    bridge._turn_id = "turn-a"
    bridge._turn_saw_busy = True
    bridge._turn_failed = False
    bridge._questions = set()
    bridge._permission_sessions = {"session-a"}
    emitted = []
    requested = []
    errors = []
    abort_entered = threading.Event()
    release_abort = threading.Event()
    replacement_started = threading.Event()
    replacement_done = threading.Event()
    bridge.emit = emitted.append

    def request(method, path, payload=None, **kwargs):
        requested.append((method, path, payload, kwargs))
        if path.endswith("/abort"):
            abort_entered.set()
            assert release_abort.wait(1)
        return True

    bridge.request = request
    failure = threading.Thread(
        target=bridge._fail_active_turn,
        args=("turn-a", "test permission failure"),
        kwargs={"settle_without_idle": False},
        daemon=True,
    )
    failure.start()
    assert abort_entered.wait(1)

    def admit_replacement():
        replacement_started.set()
        try:
            bridge.command({"id": "turn-b-early", "type": "turn", "prompt": "too soon"})
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            replacement_done.set()

    replacement = threading.Thread(target=admit_replacement, daemon=True)
    replacement.start()
    assert replacement_started.wait(1)
    assert not replacement_done.wait(0.05)

    release_abort.set()
    failure.join(1)
    replacement.join(1)

    assert not failure.is_alive()
    assert not replacement.is_alive()
    assert len(errors) == 1
    assert "already active" in str(errors[0])
    assert bridge._turn_id == "turn-a"

    bridge._event_payload(
        json.dumps(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "old-tool",
                        "sessionID": "session-a",
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "error"},
                    }
                },
            }
        )
    )
    bridge._event_payload(
        json.dumps(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "session-a",
                    "status": {"type": "idle"},
                },
            }
        )
    )

    assert emitted[1]["bridge"] == "event"
    assert emitted[1]["turn_id"] == "turn-a"
    assert emitted[2] == {
        "bridge": "settled",
        "turn_id": "turn-a",
        "status": "failed",
    }

    bridge.command({"id": "turn-b", "type": "turn", "prompt": "next"})

    assert [item["bridge"] for item in emitted] == [
        "diagnostic",
        "event",
        "settled",
        "turn_accepted",
    ]
    assert [item[1] for item in requested] == [
        "/session/session-a/abort",
        "/session/session-a/prompt_async",
    ]
    assert bridge._turn_id == "turn-b"


@pytest.mark.parametrize("runtime", ["opencode", "kilo"])
def test_bridge_auto_approves_only_each_http_harness_active_task_child_permissions(runtime):
    """Both server-mode harnesses must cover the child a Task call starts."""
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    bridge_type = runpy.run_path(str(script))["Bridge"]
    bridge = object.__new__(bridge_type)
    bridge.runtime = runtime
    bridge.session_id = "session-a"
    bridge._turn_id = "turn-a"
    bridge._turn_lock = threading.Lock()
    bridge._turn_saw_busy = True
    bridge._turn_failed = False
    bridge._questions = set()
    bridge._permission_sessions = {"session-a"}
    emitted = []
    requested = []
    bridge.emit = emitted.append
    bridge.request = lambda method, path, payload=None, **_kwargs: requested.append(
        (method, path, payload)
    )

    bridge._event_payload(
        json.dumps(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "task-part",
                        "sessionID": "session-a",
                        "type": "tool",
                        "tool": "task",
                        "state": {
                            "status": "running",
                            "input": {"subagent_type": "general"},
                            "metadata": {"sessionId": "session-child"},
                        },
                    }
                },
            }
        )
    )
    for request_id, session_id in (
        ("permission-child", "session-child"),
        ("permission-unrelated", "session-unrelated"),
    ):
        bridge._event_payload(
            json.dumps(
                {
                    "type": "permission.asked",
                    "properties": {
                        "id": request_id,
                        "sessionID": session_id,
                        "permission": "bash",
                        "patterns": ["pytest"],
                    },
                }
            )
        )

    assert requested == [
        (
            "POST",
            "/session/session-child/permissions/permission-child",
            {"response": "once"},
        )
    ]
    assert [item["event"]["type"] for item in emitted] == ["message.part.updated"]


def test_bridge_abort_settlement_timeout_fails_closed_and_ignores_stale_turn():
    """Missing native idle is fatal, while an old turn's watchdog is harmless."""
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    bridge_type = runpy.run_path(str(script))["Bridge"]
    bridge = object.__new__(bridge_type)
    bridge.session_id = "session-a"
    bridge._events_connected = threading.Event()
    bridge._events_connected.set()
    bridge._turn_lock = threading.Lock()
    bridge._turn_id = "turn-a"
    bridge._turn_saw_busy = True
    bridge._turn_failed = True
    bridge._questions = {"question-a"}
    bridge._permission_sessions = {"session-a", "session-child"}
    emitted = []
    bridge.emit = emitted.append

    bridge._abort_settlement_timeout("turn-a", "permission reply failed")

    assert not bridge._events_connected.is_set()
    assert bridge._turn_id is None
    assert bridge._turn_saw_busy is False
    assert bridge._turn_failed is False
    assert bridge._questions == set()
    assert bridge._permission_sessions == {"session-a"}
    assert emitted == [
        {
            "bridge": "fatal",
            "detail": ("permission reply failed; native idle did not follow abort"),
        }
    ]

    bridge._events_connected.set()
    bridge._turn_id = "turn-b"
    bridge._turn_saw_busy = True
    bridge._turn_failed = False
    bridge._questions = set()
    bridge._permission_sessions = {"session-a"}
    bridge._abort_settlement_timeout("turn-a", "stale watchdog")

    assert bridge._events_connected.is_set()
    assert bridge._turn_id == "turn-b"
    assert emitted[-1]["detail"] == ("permission reply failed; native idle did not follow abort")


@pytest.mark.parametrize("abort_fails", [False, True])
def test_bridge_permission_reply_failure_aborts_instead_of_waiting_forever(abort_fails):
    """A one-shot permission event cannot leave the native turn pending."""
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    namespace = runpy.run_path(str(script))
    bridge_type = namespace["Bridge"]
    bridge_error = namespace["BridgeError"]
    bridge = object.__new__(bridge_type)
    bridge.runtime = "opencode"
    bridge.session_id = "session-a"
    bridge._events_connected = threading.Event()
    bridge._events_connected.set()
    bridge._turn_id = "turn-a"
    bridge._turn_lock = threading.Lock()
    bridge._turn_saw_busy = True
    bridge._turn_failed = False
    bridge._questions = set()
    bridge._permission_sessions = {"session-a"}
    emitted = []
    requested = []
    bridge.emit = emitted.append

    def request(method, path, payload=None, **kwargs):
        requested.append((method, path, payload, kwargs))
        if "/permissions/" in path or (abort_fails and path.endswith("/abort")):
            raise bridge_error("permission reply failed")
        return True

    bridge.request = request
    bridge._event_payload(
        json.dumps(
            {
                "type": "permission.asked",
                "properties": {
                    "id": "permission-root",
                    "sessionID": "session-a",
                    "permission": "bash",
                    "patterns": ["pytest"],
                },
            }
        )
    )

    permission_replies = [item for item in requested if "/permissions/" in item[1]]
    assert len(permission_replies) == 3
    assert requested[-1] == (
        "POST",
        "/session/session-a/abort",
        None,
        {"timeout": 5},
    )
    if abort_fails:
        assert bridge._turn_id is None
        assert not bridge._events_connected.is_set()
        assert emitted[-1]["bridge"] == "fatal"
        return

    assert bridge._turn_id == "turn-a"
    bridge._event_payload(
        json.dumps(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "cancelled-tool",
                        "sessionID": "session-a",
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "error"},
                    }
                },
            }
        )
    )
    bridge._event_payload(
        json.dumps(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "session-a",
                    "status": {"type": "idle"},
                },
            }
        )
    )

    assert bridge._turn_id is None
    assert emitted[-2]["turn_id"] == "turn-a"
    assert emitted[-1] == {
        "bridge": "settled",
        "turn_id": "turn-a",
        "status": "failed",
    }


@pytest.mark.parametrize(
    "driver,runtime", [(OpenCodeDriver, OpencodeRuntime), (KiloDriver, KiloRuntime)]
)
@pytest.mark.parametrize("metadata_first", [True, False])
def test_reply_excludes_user_unknown_and_stale_turn_parts(
    tmp_path, driver, runtime, metadata_first
):
    process = _LiveProcess()
    context, _ = _protocol_context(process, tmp_path)
    process.feed = _turn_lines({"bridge": "ready", "session_id": "session-a"})
    session = driver(runtime()).launch_live(context)
    session.ensure_started(model="deepseek-v4-flash", reasoning_effort=None)

    for index in range(2):
        turn = session.start_turn(
            TurnInput(prompt=f"user question {index}", model="deepseek-v4-flash")
        )
        turn_id = json.loads(process.sent[-1])["id"]

        def event(kind, properties, owner=turn_id):
            return {
                "bridge": "event",
                "turn_id": owner,
                "event": {"type": kind, "properties": properties},
            }

        def message(message_id, role):
            return event("message.updated", {"info": {"id": message_id, "role": role}})

        def text(part_id, message_id, content, owner=turn_id):
            return event(
                "message.part.updated",
                {"part": {"id": part_id, "messageID": message_id, "type": "text", "text": content}},
                owner,
            )

        metadata = [message("user", "user"), message("answer", "assistant")]
        parts = [
            text("user-part", "user", f"user question {index}"),
            text("unknown-part", "unknown", "unattributed content"),
            text("answer-part", "answer", "partial"),
            text("answer-part", "answer", f"assistant answer {index}"),
            text("stale-part", "answer", "old turn content", "stale-turn"),
        ]
        process.feed = _turn_lines(
            *(metadata + parts if metadata_first else parts + metadata),
            {"bridge": "settled", "turn_id": "stale-turn", "status": "completed"},
            {"bridge": "settled", "turn_id": turn_id, "status": "completed"},
        )
        assert turn.wait().final_output == f"assistant answer {index}"


@pytest.mark.parametrize(
    "driver,runtime", [(OpenCodeDriver, OpencodeRuntime), (KiloDriver, KiloRuntime)]
)
def test_a_task_result_keeps_its_whole_answer_and_other_clipped_results_say_so(
    tmp_path, driver, runtime
):
    """A subagent's answer is its task result; a shortened tool result is marked."""
    process = _LiveProcess()
    context, _ = _protocol_context(process, tmp_path)
    process.feed = _turn_lines({"bridge": "ready", "session_id": "session-a"})
    session = driver(runtime()).launch_live(context)
    session.ensure_started(model="deepseek-v4-flash", reasoning_effort=None)
    turn = session.start_turn(TurnInput(prompt="delegate", model="deepseek-v4-flash"))
    turn_id = json.loads(process.sent[-1])["id"]
    # The pinned 1.18.9 (and Kilo) wrap a foreground task's answer this way.
    envelope = (
        '<task id="ses_child" state="completed">\n<task_result>\n'
        + "finding\n" * 1000
        + "</task_result>\n</task>"
    )

    def tool(call_id, name, output):
        return {
            "bridge": "event",
            "turn_id": turn_id,
            "event": {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": f"part-{call_id}",
                        "callID": call_id,
                        "type": "tool",
                        "tool": name,
                        "state": {"status": "completed", "input": {}, "output": output},
                    }
                },
            },
        }

    process.feed = _turn_lines(
        tool("task-call", "task", envelope),
        tool("bash-call", "bash", "x" * 5000),
        {"bridge": "settled", "turn_id": turn_id, "status": "completed"},
    )
    results = {
        event.payload["tool_use_id"]: event.payload
        for event in turn.iter_events()
        if event.event_type == "tool_result"
    }

    assert results["task-call"]["content"] == envelope
    assert "truncated" not in results["task-call"]
    assert results["bash-call"]["content"] == "x" * 4000
    assert results["bash-call"]["truncated"] is True


@pytest.mark.parametrize(
    "driver,runtime", [(OpenCodeDriver, OpencodeRuntime), (KiloDriver, KiloRuntime)]
)
def test_the_answer_is_previewed_and_each_part_completes_in_its_place(tmp_path, driver, runtime):
    """The pinned 1.18.9 shape: an empty part, a delta per token, the whole part."""
    process = _LiveProcess()
    context, _ = _protocol_context(process, tmp_path)
    process.feed = _turn_lines({"bridge": "ready", "session_id": "session-a"})
    session = driver(runtime()).launch_live(context)
    session.ensure_started(model="deepseek-v4-flash", reasoning_effort=None)
    turn = session.start_turn(TurnInput(prompt="look", model="deepseek-v4-flash"))
    turn_id = json.loads(process.sent[-1])["id"]

    def event(kind, properties):
        return {
            "bridge": "event",
            "turn_id": turn_id,
            "event": {"type": kind, "properties": properties},
        }

    def message(message_id, role):
        return event("message.updated", {"info": {"id": message_id, "role": role}})

    def part(part_id, message_id, kind, text, **extra):
        return event(
            "message.part.updated",
            {"part": {"id": part_id, "messageID": message_id, "type": kind, "text": text, **extra}},
        )

    def delta(part_id, message_id, text):
        return event(
            "message.part.delta",
            {"messageID": message_id, "partID": part_id, "field": "text", "delta": text},
        )

    def tool(status):
        return event(
            "message.part.updated",
            {
                "part": {
                    "id": "tool-part",
                    "callID": "call-1",
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": status, "input": {"command": "pwd"}, "output": "/w"},
                }
            },
        )

    process.feed = _turn_lines(
        message("user", "user"),
        part("user-part", "user", "text", "look"),
        message("answer", "assistant"),
        part("thought", "answer", "reasoning", "", time={"start": 1}),
        delta("thought", "answer", "hmm"),
        part("thought", "answer", "reasoning", "hmm", time={"start": 1, "end": 2}),
        part("first", "answer", "text", "", time={"start": 2}),
        delta("first", "answer", "Let me"),
        delta("first", "answer", " check."),
        part("first", "answer", "text", "Let me check.", time={"start": 2, "end": 3}),
        tool("running"),
        tool("completed"),
        # Metadata after its part: previewed once the owner is known.
        part("second", "late", "text", ""),
        delta("second", "late", "All"),
        message("late", "assistant"),
        delta("second", "late", " done."),
        part("second", "late", "text", "All done.", time={"start": 4, "end": 5}),
        {"bridge": "settled", "turn_id": turn_id, "status": "completed"},
    )
    events = list(turn.iter_events())

    previews = [
        (event.payload["preview_id"], event.payload["delta"])
        for event in events
        if event.payload.get("protocol") == "reply-preview"
    ]
    # Neither the user's text nor reasoning is previewed; a finished part is
    # shown whole; a late owner's part is previewed with everything it had.
    assert [preview_id for preview_id, _ in previews] == ["first", "first", "second"]
    assert "".join(text for preview_id, text in previews if preview_id == "first") == (
        "Let me check."
    )
    assert previews[-1] == ("second", "All done.")
    shown = [
        "preview" if event.payload.get("protocol") == "reply-preview" else event.event_type
        for event in events
        if event.event_type in {"thinking", "tool_use", "tool_result", "message"}
        or event.payload.get("protocol") == "reply-preview"
    ]
    assert shown == [
        "thinking",
        "preview",
        "preview",
        "tool_use",
        "tool_result",
        "preview",
        "message",
        "message",
    ]
    answer = [event.payload for event in events if event.event_type == "message"]
    assert answer == [
        {"text": "Let me check.", "preview_id": "first"},
        {"text": "All done.", "preview_id": "second"},
    ]
    assert turn.wait().final_output == "Let me check.\n\nAll done."


@pytest.mark.parametrize(
    "driver,runtime", [(OpenCodeDriver, OpencodeRuntime), (KiloDriver, KiloRuntime)]
)
@pytest.mark.parametrize(
    ("error", "text"),
    [
        (
            {"name": "APIError", "data": {"message": "403 no active API key", "statusCode": 403}},
            "403 no active API key",
        ),
        ({"name": "MessageOutputLengthError", "data": {}}, "MessageOutputLengthError"),
    ],
)
def test_a_session_error_reads_the_way_the_clis_own_output_prints_it(
    tmp_path, driver, runtime, error, text
):
    process = _LiveProcess()
    context, _ = _protocol_context(process, tmp_path)
    process.feed = _turn_lines({"bridge": "ready", "session_id": "session-a"})
    session = driver(runtime()).launch_live(context)
    session.ensure_started(model="deepseek-v4-flash", reasoning_effort=None)
    turn = session.start_turn(TurnInput(prompt="look", model="deepseek-v4-flash"))
    turn_id = json.loads(process.sent[-1])["id"]
    process.feed = _turn_lines(
        {
            "bridge": "event",
            "turn_id": turn_id,
            "event": {"type": "session.error", "properties": {"error": error}},
        },
        {"bridge": "settled", "turn_id": turn_id, "status": "failed"},
    )
    events = list(turn.iter_events())

    assert [event.payload for event in events if event.event_type == "error"] == [{"text": text}]
