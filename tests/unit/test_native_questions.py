"""Native question requests remain answerable through their exact live turn."""

from __future__ import annotations

import copy
import json
import runpy
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.unit.test_live_sessions import _live, _LiveProcess, _turn_lines

from cloud_agent_host.drivers import (
    ClaudeCodeDriver,
    CodexDriver,
    KiloDriver,
    OpenCodeDriver,
    TurnInput,
)
from cloud_agent_host.runtimes import ClaudeCodeRuntime, CodexRuntime, KiloRuntime, OpencodeRuntime
from cloud_agent_shared.shared_protocol import INPUT_REFUSED, INPUT_TURN_ENDED

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/agent_runtime_streams"


def _fixture(runtime, kind="question"):
    return [
        json.loads(line)
        for line in (FIXTURES / f"{runtime}_{kind}_control_contract.jsonl").read_text().splitlines()
    ]


def _turn(tmp_path, runtime, kind="question"):
    process = _LiveProcess()
    context, spawned = _live(process, tmp_path)
    if runtime == "claude":
        session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(context)
    else:
        process.feed = _turn_lines(
            {"id": "initialize-1", "result": {}},
            {"id": "thread-2", "result": {"thread": {"id": "thread-a"}}},
        )
        session = CodexDriver(CodexRuntime()).launch_live(context)
        session.ensure_started(model="m", reasoning_effort=None)
        process.feed = _turn_lines({"id": "turn-3", "result": {"turn": {"id": "turn-a"}}})
    turn = session.start_turn(TurnInput(prompt="choose", model="m"))
    request, response = _fixture(runtime, kind)
    turn.events_for_line(json.dumps(request))
    return process, session, turn, request, response, spawned


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_native_request_retains_wire_identity_and_answers_once(tmp_path, runtime):
    process, _session, turn, request, response, spawned = _turn(tmp_path, runtime)
    (pending,) = turn.pending_questions()
    (question,) = pending["questions"]
    assert question["question"] == "Which approach?"
    assert question["allow_custom"] is True
    assert pending["request_id"] != str(request.get("id") or request.get("request_id"))
    # A returned snapshot cannot alter the input echoed to the CLI.
    question["question"] = "changed"
    question_id = question["id"]
    assert turn.answer_question(pending["request_id"], {question_id: ["B"]}).accepted
    assert json.loads(process.sent[-1]) == response
    assert turn.pending_questions() == []
    assert turn.answer_question(pending["request_id"], {question_id: ["A"]}).reason == INPUT_REFUSED
    if runtime == "claude":
        argv = spawned[0][0]
        assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_two_tabs_can_write_only_one_answer(tmp_path, runtime):
    process, _session, turn, _request, _response, _spawned = _turn(tmp_path, runtime)
    (pending,) = turn.pending_questions()
    question_id = pending["questions"][0]["id"]
    before = len(process.sent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _: turn.answer_question(pending["request_id"], {question_id: ["B"]}),
                range(2),
            )
        )
    assert sorted(outcome.accepted for outcome in outcomes) == [False, True]
    assert len(process.sent) == before + 1


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize(
    "answers", [{}, {"wrong": ["B"]}, {"correct": []}, {"correct": ["A", "B"]}]
)
def test_invalid_answers_leave_request_pending(tmp_path, runtime, answers):
    process, _session, turn, _request, _response, _spawned = _turn(tmp_path, runtime)
    (pending,) = turn.pending_questions()
    question_id = pending["questions"][0]["id"]
    answers = {question_id if key == "correct" else key: value for key, value in answers.items()}
    before = len(process.sent)
    assert turn.answer_question(pending["request_id"], answers).reason == INPUT_REFUSED
    assert len(process.sent) == before
    assert turn.pending_questions() == [pending]


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_reject_sends_native_skip_and_cancel_closes_requests(tmp_path, runtime):
    process, _session, turn, request, _response, _spawned = _turn(tmp_path, runtime)
    (pending,) = turn.pending_questions()
    assert turn.answer_question(pending["request_id"], {}, reject=True).accepted
    reply = json.loads(process.sent[-1])
    if runtime == "claude":
        assert reply["response"]["response"]["behavior"] == "deny"
    else:
        assert reply == {"id": request["id"], "result": {"answers": {}}}
    turn.cancel()
    assert turn.pending_questions() == []
    assert turn.answer_question(pending["request_id"], {}, reject=True).reason == INPUT_TURN_ENDED


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_native_request_cancellation_clears_popup(tmp_path, runtime):
    _process, _session, turn, request, _response, _spawned = _turn(tmp_path, runtime)
    if runtime == "claude":
        cancelled = {"type": "control_cancel_request", "request_id": request["request_id"]}
    else:
        cancelled = {
            "method": "serverRequest/resolved",
            "params": {"threadId": "thread-a", "requestId": request["id"]},
        }
    turn.events_for_line(json.dumps(cancelled))
    assert turn.pending_questions() == []
    # A duplicate native request cannot resurrect one already resolved.
    turn.events_for_line(json.dumps(request))
    assert turn.pending_questions() == []


@pytest.mark.parametrize("native_id", [41, "41"])
def test_codex_fences_native_questions_to_exact_thread_and_turn(tmp_path, native_id):
    _process, _session, turn, request, _response, _spawned = _turn(tmp_path, "codex")
    turn.events_for_line(
        json.dumps(
            {
                "method": "serverRequest/resolved",
                "params": {"threadId": "thread-a", "requestId": request["id"]},
            }
        )
    )
    for field, other in (("threadId", "other-thread"), ("turnId", "other-turn")):
        wrong = copy.deepcopy(request)
        wrong["id"] = native_id
        wrong["params"][field] = other
        turn.events_for_line(json.dumps(wrong))
    assert turn.pending_questions() == []


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_terminal_native_boundary_clears_question_before_yielding(tmp_path, runtime):
    process, _session, turn, _request, _response, _spawned = _turn(tmp_path, runtime)
    (pending,) = turn.pending_questions()
    terminal = (
        {"type": "result", "subtype": "success"}
        if runtime == "claude"
        else {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-a", "status": "completed"}},
        }
    )
    process.feed = _turn_lines(terminal)
    next(turn.raw_lines())
    assert turn.pending_questions() == []
    assert turn.answer_question(pending["request_id"], {}, reject=True).reason == INPUT_TURN_ENDED


def _http_turn(tmp_path, driver=OpenCodeDriver, runtime=OpencodeRuntime):
    process = _LiveProcess()
    process.feed = _turn_lines({"bridge": "ready", "session_id": "session-a"})
    session = driver(runtime()).launch_live(_live(process, tmp_path)[0])
    session.ensure_started(model="m", reasoning_effort=None)
    turn = session.start_turn(TurnInput(prompt="choose", model="m"))
    request = {
        "bridge": "event",
        "turn_id": turn.protocol_turn_id,
        "event": {
            "type": "question.asked",
            "properties": {
                "id": "que-1",
                "sessionID": "session-a",
                "questions": [
                    {
                        "question": "Which approach?",
                        "header": "Approach",
                        "multiple": True,
                        "options": [
                            {"label": "A", "description": "First"},
                            {"label": "B", "description": "Second"},
                        ],
                    }
                ],
            },
        },
    }
    turn.events_for_line(json.dumps(request))
    return process, turn, request


@pytest.mark.parametrize(
    "driver,runtime", [(OpenCodeDriver, OpencodeRuntime), (KiloDriver, KiloRuntime)]
)
def test_http_questions_preserve_order_and_restore_failed_posts(tmp_path, driver, runtime):
    process, turn, _request = _http_turn(tmp_path, driver, runtime)
    (pending,) = turn.pending_questions()
    assert turn.answer_question(pending["request_id"], {"0": ["A", "B"]}).accepted
    command = json.loads(process.sent[-1])
    assert command["request_id"] == "que-1"
    assert command["answers"] == [["A", "B"]]
    assert turn.pending_questions() == []
    turn.events_for_line(
        json.dumps({"bridge": "command_error", "id": command["id"], "detail": "POST failed"})
    )
    (restored,) = turn.pending_questions()
    assert restored["request_id"] != pending["request_id"]
    assert turn.answer_question(pending["request_id"], {"0": ["B"]}).reason == INPUT_REFUSED
    assert turn.answer_question(restored["request_id"], {"0": ["B"]}).accepted
    command = json.loads(process.sent[-1])
    turn.events_for_line(json.dumps({"bridge": "question_answered", "id": command["id"]}))
    assert turn.pending_questions() == []


def test_http_native_resolution_prevents_late_error_reopening(tmp_path):
    process, turn, request = _http_turn(tmp_path)
    (pending,) = turn.pending_questions()
    turn.answer_question(pending["request_id"], {"0": ["B"]})
    command = json.loads(process.sent[-1])
    request["event"] = {
        "type": "question.replied",
        "properties": {"sessionID": "session-a", "requestID": "que-1", "answers": [["B"]]},
    }
    turn.events_for_line(json.dumps(request))
    turn.events_for_line(
        json.dumps({"bridge": "command_error", "id": command["id"], "detail": "late error"})
    )
    assert turn.pending_questions() == []


def test_bridge_keeps_failed_question_post_retryable_and_fences_siblings():
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    module = runpy.run_path(str(script))
    bridge = object.__new__(module["Bridge"])
    bridge.session_id = "session-a"
    bridge._turn_id = "turn-a"
    bridge._turn_lock = threading.Lock()
    bridge._questions = {"que-1"}
    calls = []
    bridge.emit = lambda value: None

    def fail(*args):
        raise module["BridgeError"]("POST failed")

    bridge.request = fail
    command = {
        "id": "question-1",
        "type": "question",
        "turn_id": "turn-a",
        "request_id": "que-1",
        "answers": [["B"]],
    }
    with pytest.raises(RuntimeError, match="POST failed"):
        bridge.command(command)
    assert bridge._questions == {"que-1"}
    bridge.request = lambda *args: calls.append(args)
    with pytest.raises(RuntimeError, match="stale"):
        bridge.command({**command, "turn_id": "sibling-turn"})
    assert calls == []
    bridge.command(command)
    assert calls == [("POST", "/question/que-1/reply", {"answers": [["B"]]})]
    assert bridge._questions == set()


@pytest.mark.parametrize(
    "tool_input",
    [
        {},
        {"allowedPrompts": [{"tool": "Bash", "prompt": "run tests"}]},
    ],
)
def test_claude_exit_plan_mode_approval_allows_the_original_input(tmp_path, tool_input):
    process, _session, turn, request, _response, _spawned = _turn(tmp_path, "claude")
    turn.events_for_line(
        json.dumps({"type": "control_cancel_request", "request_id": request["request_id"]})
    )
    request["request_id"] = "exit-plan-1"
    request["request"]["tool_name"] = "ExitPlanMode"
    request["request"]["input"] = tool_input

    before = len(process.sent)
    turn.events_for_line(json.dumps(request))
    assert len(process.sent) == before
    (pending,) = turn.pending_questions()
    assert turn.answer_question(pending["request_id"], {"0": ["Approve plan"]}).accepted

    reply = json.loads(process.sent[-1])
    assert reply == {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "exit-plan-1",
            "response": {
                "behavior": "allow",
                "updatedInput": request["request"]["input"],
            },
        },
    }
    assert turn.pending_questions() == []


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("unsupported-tool", {}),
        ("AskUserQuestion", {}),
        ("ExitPlanMode", None),
    ],
)
def test_claude_unsupported_or_malformed_interactions_are_denied(tmp_path, tool_name, tool_input):
    process, _session, turn, request, _response, _spawned = _turn(tmp_path, "claude")
    turn.events_for_line(
        json.dumps({"type": "control_cancel_request", "request_id": request["request_id"]})
    )
    request["request_id"] = "unsupported-1"
    request["request"]["tool_name"] = tool_name
    request["request"]["input"] = tool_input
    turn.events_for_line(json.dumps(request))
    reply = json.loads(process.sent[-1])
    assert reply["response"]["request_id"] == "unsupported-1"
    assert reply["response"]["response"]["behavior"] == "deny"
    assert "Ask the user in chat" in reply["response"]["response"]["message"]
    assert turn.pending_questions() == []


def _plan_turn(tmp_path, original):
    process, session, turn, request, _response, _spawned = _turn(tmp_path, "claude")
    turn.events_for_line(
        json.dumps({"type": "control_cancel_request", "request_id": request["request_id"]})
    )
    request["request_id"] = "plan-native-1"
    request["request"]["tool_name"] = "ExitPlanMode"
    request["request"]["input"] = original
    turn.events_for_line(json.dumps(request))
    return process, session, turn, request


def test_claude_plan_approval_matches_the_pinned_native_contract(tmp_path):
    process, _session, turn, request, response, _spawned = _turn(tmp_path, "claude", "plan")
    (pending,) = turn.pending_questions()
    assert request["request"]["input"]["plan"] in pending["questions"][0]["question"]
    assert turn.answer_question(pending["request_id"], {"0": ["Approve plan"]}).accepted
    assert json.loads(process.sent[-1]) == response


@pytest.mark.parametrize(
    "original",
    [
        {},
        {"allowedPrompts": [{"tool": "Bash", "prompt": "run tests"}]},
        {
            "plan": "# Plan\n\n1. Implement the fix.\n2. Run tests.",
            "planFilePath": "/state/plan.md",
        },
    ],
)
def test_claude_plan_approval_waits_and_preserves_native_input(tmp_path, original):
    process, _session, turn, request = _plan_turn(tmp_path, original)
    expected = copy.deepcopy(original)
    before = len(process.sent)
    turn.events_for_line(json.dumps(request))
    (pending,) = turn.pending_questions()
    (question,) = pending["questions"]
    assert question["header"] == "Plan approval"
    assert question["multiple"] is False
    assert question["allow_custom"] is True
    assert question["kind"] == "plan_approval"
    assert [option["label"] for option in question["options"]] == ["Approve plan"]
    if expected.get("plan"):
        assert expected["plan"] in question["question"]
    assert len(process.sent) == before
    # Neither the caller's native input nor a public snapshot controls the reply.
    original["plan"] = "changed"
    question["options"][0]["label"] = "changed"
    answers = {question["id"]: ["Approve plan"]}
    assert turn.answer_question(pending["request_id"], answers).accepted
    assert json.loads(process.sent[-1]) == {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "plan-native-1",
            "response": {"behavior": "allow", "updatedInput": expected},
        },
    }
    assert turn.pending_questions() == []
    assert turn.answer_question(pending["request_id"], answers).reason == INPUT_REFUSED
    turn.events_for_line(json.dumps(request))
    assert turn.pending_questions() == []
    assert len(process.sent) == before + 1


def test_claude_declining_plan_sends_native_denial(tmp_path):
    process, _session, turn, _request = _plan_turn(tmp_path, {})
    (pending,) = turn.pending_questions()
    assert turn.answer_question(pending["request_id"], {}, reject=True).accepted
    reply = json.loads(process.sent[-1])
    assert reply["response"]["request_id"] == "plan-native-1"
    assert reply["response"]["response"] == {
        "behavior": "deny",
        "message": "The user declined this plan. Keep planning.",
    }
    assert turn.pending_questions() == []


@pytest.mark.parametrize("reject", [False, True])
def test_claude_plan_feedback_declines_and_carries_the_comment_back(tmp_path, reject):
    process, _session, turn, _request = _plan_turn(tmp_path, {})
    (pending,) = turn.pending_questions()
    feedback = "Skip the migration; fix the endpoint in place instead."
    assert turn.answer_question(pending["request_id"], {"0": [feedback]}, reject=reject).accepted
    reply = json.loads(process.sent[-1])
    assert reply["response"]["request_id"] == "plan-native-1"
    assert reply["response"]["response"] == {
        "behavior": "deny",
        "message": f"The user declined this plan: {feedback}",
    }
    assert turn.pending_questions() == []


def test_claude_plan_rejection_wins_over_feedback_matching_the_approval_label(tmp_path):
    process, _session, turn, _request = _plan_turn(tmp_path, {})
    (pending,) = turn.pending_questions()
    assert turn.answer_question(
        pending["request_id"], {"0": ["Approve plan"]}, reject=True
    ).accepted
    assert json.loads(process.sent[-1])["response"]["response"] == {
        "behavior": "deny",
        "message": "The user declined this plan: Approve plan",
    }
    assert turn.pending_questions() == []


@pytest.mark.parametrize("answer", [[], ["Approve plan", "Other"]])
def test_claude_plan_approval_requires_the_explicit_choice(tmp_path, answer):
    process, _session, turn, _request = _plan_turn(tmp_path, {})
    (pending,) = turn.pending_questions()
    before = len(process.sent)
    assert turn.answer_question(pending["request_id"], {"0": answer}).reason == INPUT_REFUSED
    assert turn.pending_questions() == [pending]
    assert len(process.sent) == before


@pytest.mark.parametrize("boundary", ["control_cancel_request", "result", "cancel"])
def test_claude_plan_approval_cannot_survive_its_native_boundary(tmp_path, boundary):
    process, _session, turn, request = _plan_turn(tmp_path, {})
    (pending,) = turn.pending_questions()
    if boundary == "cancel":
        turn.cancel()
    else:
        turn.events_for_line(json.dumps({"type": boundary, "request_id": "plan-native-1"}))
    assert turn.pending_questions() == []
    turn.events_for_line(json.dumps(request))
    assert turn.pending_questions() == []
    before = len(process.sent)
    assert not turn.answer_question(pending["request_id"], {"0": ["Approve plan"]}).accepted
    assert len(process.sent) == before


@pytest.mark.parametrize("original", [None, [], {"plan": 42}, {"plan": None}])
def test_claude_malformed_plan_requests_are_denied_without_blocking(tmp_path, original):
    process, _session, turn, _request = _plan_turn(tmp_path, original)
    reply = json.loads(process.sent[-1])
    assert reply["response"]["request_id"] == "plan-native-1"
    assert reply["response"]["response"]["behavior"] == "deny"
    assert "invalid" in reply["response"]["response"]["message"]
    assert turn.pending_questions() == []


def test_claude_plan_preview_is_bounded_without_truncating_native_input(tmp_path):
    original = {"plan": "plan detail\n" * 2_000}
    process, _session, turn, _request = _plan_turn(tmp_path, original)
    (pending,) = turn.pending_questions()
    text = pending["questions"][0]["question"]
    assert len(text) == 10_000
    assert text.endswith("[Plan preview truncated.]")
    assert turn.answer_question(pending["request_id"], {"0": ["Approve plan"]}).accepted
    assert json.loads(process.sent[-1])["response"]["response"]["updatedInput"] == original


def test_codex_malformed_native_question_gets_invalid_params_response(tmp_path):
    process, _session, turn, request, _response, _spawned = _turn(tmp_path, "codex")
    request["id"] = "malformed-1"
    request["params"]["questions"][0]["id"] = "x" * 129
    turn.events_for_line(json.dumps(request))
    reply = json.loads(process.sent[-1])
    assert reply["id"] == "malformed-1"
    assert reply["error"]["code"] == -32602
    assert len(turn.pending_questions()) == 1


def test_codex_string_and_number_request_ids_remain_distinct(tmp_path):
    process, _session, turn, request, _response, _spawned = _turn(tmp_path, "codex")
    request["id"] = "41"
    turn.events_for_line(json.dumps(request))
    pending = turn.pending_questions()
    assert len(pending) == 2
    for question in pending:
        assert turn.answer_question(question["request_id"], {"approach": ["B"]}).accepted
    replies = [json.loads(line)["id"] for line in process.sent[-2:]]
    assert replies == [41, "41"]


def test_claude_multiple_answers_are_keyed_by_original_question_text(tmp_path):
    process, _session, turn, request, _response, _spawned = _turn(tmp_path, "claude")
    request["request_id"] = "multiple-1"
    question = request["request"]["input"]["questions"][0]
    question["multiSelect"] = True
    turn.events_for_line(json.dumps(request))
    pending = turn.pending_questions()[-1]
    assert turn.answer_question(pending["request_id"], {"0": ["A", "B"]}).accepted
    reply = json.loads(process.sent[-1])
    assert reply["response"]["response"]["updatedInput"]["answers"] == {"Which approach?": "A, B"}


def test_http_malformed_native_question_is_rejected_instead_of_blocking(tmp_path):
    process, turn, request = _http_turn(tmp_path)
    request["event"]["properties"]["id"] = "que-invalid"
    request["event"]["properties"]["questions"] = []
    turn.events_for_line(json.dumps(request))
    reply = json.loads(process.sent[-1])
    assert reply["type"] == "question"
    assert reply["request_id"] == "que-invalid"
    assert reply["turn_id"] == turn.protocol_turn_id
    assert reply["reject"] is True
    assert len(turn.pending_questions()) == 1
