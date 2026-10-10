"""Native question forwarding stays attached to its resident turn."""

from types import SimpleNamespace

import pytest

from cloud_agent_host.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.drivers import InputOutcome
from cloud_agent_host.resident_transport import (
    ResidentLaunch,
    ResidentTransportFactory,
    ResidentTurn,
)


def test_resident_turn_forwards_native_question_reads_and_replies():
    questions = [{"request_id": "request-one", "questions": []}]
    replies = []

    def answer(request_id, answers, *, reject=False):
        replies.append((request_id, answers, reject))
        return InputOutcome(True)

    turn = ResidentTurn(
        SimpleNamespace(
            pending_questions=lambda: questions,
            answer_question=answer,
        )
    )
    assert turn.pending_questions() is questions
    answers = {"question": ["Selected option"]}
    assert turn.answer_question("request-one", answers) == InputOutcome(True)
    assert turn.answer_question("request-two", {}, reject=True) == InputOutcome(True)
    assert replies == [("request-one", answers, False), ("request-two", {}, True)]
    assert replies[0][1] is answers


@pytest.mark.parametrize(
    "runtime_id,has_mcp,requires_helpers",
    [
        ("opencode", False, True),
        ("kilo", False, True),
        ("claude-code", False, False),
        ("codex", False, False),
        ("pi", False, False),
        ("claude-code", True, True),
        ("codex", True, True),
        ("pi", True, True),
    ],
)
def test_retained_http_launch_refreshes_the_trusted_bridge_without_mcp(
    runtime_id, has_mcp, requires_helpers
):
    events = []
    process = object()

    def spawn(spec, argv, **kwargs):
        events.append(("spawn", spec, argv))
        return process

    manager = SimpleNamespace(
        backend=SimpleNamespace(gid=10001, base_env=lambda: {}),
        reap_member_processes=lambda spec, **kwargs: events.append(("reap", spec)),
        ensure_mcp_helpers=lambda spec, runtime: events.append(("helpers", spec, runtime)),
        ensure_kilo_mcp_runtime=lambda spec, version: events.append(("kilo", spec, version)),
        ensure_codex_mcp_policy=lambda spec, base_url: events.append(("policy", spec, base_url)),
        spawn=spawn,
    )
    spec = ChatSandboxSpec(
        thread_id="athr_retained",
        sandbox_id="asbx_retained",
        generation=1,
        runtime_id=runtime_id,
        shared_live=True,
    )
    launch = ResidentLaunch(
        thread_id="athr_retained",
        runtime_id=runtime_id,
        runtime_version=None,
        native_session_id="native-retained",
        model="model",
        reasoning_effort=None,
        gateway_base_url="http://gateway",
        credential="grant",
        mcp_server_ids=("tool",) if has_mcp else (),
        mcp_base_url="http://mcp" if has_mcp else "",
    )
    context = ResidentTransportFactory(manager)._launch_context(
        spec, launch, credential="grant", state_dir="/agent-state/persisted-member"
    )
    assert context.spawn_live(["agent", "resume", "native-retained"], {}) is process
    assert context.spawn_live(["agent", "probe"], {}) is process

    helper_calls = [event for event in events if event[0] == "helpers"]
    assert helper_calls == ([("helpers", spec, runtime_id)] if requires_helpers else [])
    assert [event[0] for event in events].count("reap") == 1
    if requires_helpers:
        assert events.index(helper_calls[0]) < next(
            index for index, event in enumerate(events) if event[0] == "spawn"
        )
    assert context.session_state_dir == "/agent-state/persisted-member"
    assert launch.native_session_id == "native-retained"
