"""Real pinned CLI regression for the production driver's persistence boundary.

Set CLOUD_AGENT_TEST_CLAUDE_BINARY to an installed pinned Claude executable.
The model endpoint is a local protocol fixture; no real inference is performed.
"""

from __future__ import annotations

import os
import subprocess

import pytest

import cloud_agent_host.drivers as drivers
from cloud_agent_host.runtimes import ERROR, ClaudeCodeRuntime
from cloud_agent_host.sandbox import ProcessBackend, SandboxSpec
from tests.external.test_credential_rotation import _pinned_cli_version
from tests.support.claude_cli import FakeAnthropicGateway

pytestmark = pytest.mark.external
MODEL = "deepseek-v4-flash"


@pytest.fixture
def native_driver(tmp_path, monkeypatch):
    binary = os.environ.get("CLOUD_AGENT_TEST_CLAUDE_BINARY")
    if not binary:
        pytest.skip("set CLOUD_AGENT_TEST_CLAUDE_BINARY to the pinned native executable")
    version = subprocess.check_output([binary, "--version"], text=True).split()[0]
    assert version == _pinned_cli_version()
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    config = tmp_path / "claude"
    config.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(drivers, "CLAUDE_STATE_CONFIG_DIR", str(config))
    gateway = FakeAnthropicGateway()
    backend = ProcessBackend(acknowledged_unsafe=True)

    def spawn(argv, env):
        # Only the process-placement seam is local. Launch flags, stdin,
        # event parsing, settlement, close and resume are production code.
        return backend.spawn(
            SandboxSpec(
                argv=[binary, *argv[1:], "--tools", ""],
                workdir=str(workdir),
                env={"PATH": os.environ["PATH"], "HOME": str(home), **env},
                live_input=True,
            )
        )

    context = drivers.LaunchContext(
        workdir=str(workdir),
        gateway_base_url=f"http://127.0.0.1:{gateway.port}",
        credential="local-context-test",
        provides_isolation=False,
        state_dir=str(tmp_path),
        spawn_live=spawn,
    )
    sessions = []

    def open_session(session_id=None):
        driver = drivers.ClaudeCodeDriver(ClaudeCodeRuntime())
        session = (
            driver.launch_resident_live(context, session_id=session_id)
            if session_id
            else driver.launch_live(context)
        )
        sessions.append(session)
        return session

    yield gateway, open_session, config
    for session in sessions:
        session.close()
    gateway.close()


def _turn(session, prompt):
    turn = session.start_turn(drivers.TurnInput(prompt=prompt, model=MODEL))
    events = list(turn.iter_events())
    return turn.wait(), events


def _assert_history(request, expected):
    assert request["model"] == MODEL
    # Match exact text, role, order and occurrence count. Membership checks
    # alone accept a reordered transcript or one copy of two repeated turns.
    actual = []
    for message in request["messages"]:
        content = message["content"]
        if isinstance(content, str):
            actual.append((message["role"], content))
        else:
            actual.extend(
                (message["role"], block["text"])
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    remaining = iter(actual)
    for role, text in expected:
        assert any(item == (role, text) for item in remaining), (
            f"native history lost, reordered or changed {role} message: {text!r}"
        )


@pytest.mark.parametrize("retry_turn", [0, 1])
def test_successful_internal_api_retry_preserves_native_parent_chain(native_driver, retry_turn):
    gateway, open_session, _ = native_driver
    session = open_session()
    session_id = None
    expected = []
    retirement_requested = False
    for index, prompt in enumerate(["hi", "我刚才让你做什么了", "请原样引用第一条消息。"]):
        if index == retry_turn:
            gateway.response_statuses.extend([500, 200])
        gateway.reply_text = f"Independent reply {index}, which must survive native resume."
        result, _ = _turn(session, prompt)
        assert result.status == "completed"
        assert result.native_turn_settled
        session_id = session_id or session.runtime.session_id
        assert session.runtime.session_id == session_id
        _assert_history(gateway.requests[-1], expected)
        expected.extend([("user", prompt), ("assistant", result.final_output)])
        if index == retry_turn:
            retirement_requested = session.retire_after_turn
        # Mirror broker settlement: save the locator, then EOF the flagged
        # process and follow the normal native restoration path.
        if session.retire_after_turn:
            session.close()
            session = open_session(session_id)
    session.close()
    session = open_session(session_id)
    restored, _ = _turn(session, "Check the whole conversation after another restart.")
    assert restored.status == "completed"
    assert session.runtime.session_id == session_id
    _assert_history(gateway.requests[-1], expected)
    assert retirement_requested, "a recovered API retry must flush before accepting another user"


@pytest.mark.parametrize("original", ["hi", "Remember harbor-lantern-9631."])
def test_driver_flushes_last_reply_before_immediate_resume_and_failed_turn_retry(
    native_driver, original
):
    gateway, open_session, _ = native_driver
    session = open_session()
    gateway.reply_text = "First reply, preserved independently."
    first, _ = _turn(session, original)
    assert first.status == "completed"
    session_id = session.runtime.session_id
    assert session_id
    gateway.reply_text = "Second reply with a newline.\nThis must survive immediate close."
    follow_up = "我刚才让你做什么了"
    second, _ = _turn(session, follow_up)
    assert second.status == "completed"
    expected = [
        ("user", original),
        ("assistant", first.final_output),
        ("user", follow_up),
        ("assistant", second.final_output),
    ]
    _assert_history(gateway.requests[-1], expected[:2])
    # No artificial sleep: a completed turn must be safe to close immediately.
    session.close()
    session = open_session(session_id)
    gateway.reply_text = "Third reply after resume."
    restored, _ = _turn(session, "请原样引用第一条消息。")
    assert restored.status == "completed"
    assert session.runtime.session_id == session_id
    _assert_history(gateway.requests[-1], expected)

    # 400, not 401: the pinned CLI answers an auth failure with a long
    # credential-refresh retry, so a 401 turn settles as failed only after
    # about three minutes of silence — and settling is all this line needs.
    # The retry is not what this test asserts; it asserts the flush, the close
    # and the resume that follow, and the 400 drives those identically (the
    # gateway's error body is the same for any non-200). A credential failure
    # worth asserting belongs in test_credential_rotation.py, where it would
    # arrive with an assertion rather than a wait.
    gateway.response_status = 400
    failed, events = _turn(session, "Retry continuity probe.")
    assert any(event.event_type == ERROR for event in events)
    assert failed.status == "failed"
    assert failed.native_turn_settled is True
    session.close()
    gateway.response_status = 200
    session = open_session(session_id)
    retried, _ = _turn(session, "Retry continuity probe.")
    assert retried.status == "completed"
    assert session.runtime.session_id == session_id
    _assert_history(gateway.requests[-1], expected)
