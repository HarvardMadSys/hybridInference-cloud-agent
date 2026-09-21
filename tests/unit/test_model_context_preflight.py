"""Prevent a green preflight from meaning only HTTP 200 or a partial answer."""

import json
import re
import sys

import httpx
import pytest
from deploy.preflight.check_model_context import (
    ModelReply,
    assess_reply,
    main,
    read_reply,
    recall_payload,
    validate_answer,
)


@pytest.mark.parametrize("case,expected", [("hi", "hi"), ("repeat", "hi"), ("task", "comet_731")])
def test_prompt_requires_recalling_earlier_content(case, expected):
    body, recalled = recall_payload(case)
    assert recalled == expected
    assert body["model"] == "deepseek-v4-flash"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert expected in body["messages"][0]["content"]
    # The current message must not supply the answer to its own recall question.
    assert not re.search(
        rf"(?<![a-zA-Z0-9_]){re.escape(expected)}(?![a-zA-Z0-9_])",
        json.dumps(body["messages"][-1], ensure_ascii=False),
    )
    assert len(body["messages"][-1]["content"]) == 1


@pytest.mark.parametrize(
    "reply",
    [
        "这是我们第一次交流,我没有之前的对话记录。",
        "hi. This is our first exchange.",
        "你之前说了 hi。但每次对话都是全新的开始。",
        "I don't have any memory of earlier tasks. You said hi.",
        "Nothing has been requested in this conversation.",
        "This answer does not recall the greeting.",
    ],
)
def test_original_failure_is_rejected_even_after_a_successful_http_response(reply):
    with pytest.raises(ValueError):
        validate_answer(reply, "hi")


@pytest.mark.parametrize(
    "reply,expected",
    [("你之前说了 hi,没有布置具体任务。", "hi"), ("你让我确认名为comet_731的函数。", "comet_731")],
)
def test_no_task_is_distinct_from_no_history(reply, expected):
    validate_answer(reply, expected)


def test_state_update_checks_user_and_assistant_facts_without_leaking_answers():
    body, expected = recall_payload("state_update")
    assert isinstance(expected, dict)
    assert expected["project_code"] in body["messages"][0]["content"]
    assert expected["original_function_name"] in body["messages"][1]["content"]
    question = json.dumps(body["messages"][-1], ensure_ascii=False)
    assert all(value not in question for value in expected.values())
    validate_answer(json.dumps(expected), expected)
    validate_answer(f"```json\n{json.dumps(expected)}\n```", expected)


@pytest.mark.parametrize(
    "field", ["project_code", "original_function_name", "original_format", "current_format"]
)
def test_one_incorrect_fact_cannot_pass_by_mentioning_the_other_answers(field):
    _, expected = recall_payload("state_update")
    incorrect = dict(expected)
    incorrect[field] = "wrong"
    with pytest.raises(ValueError, match="required facts"):
        validate_answer(json.dumps(incorrect), expected)


def test_swapping_original_and_current_values_is_a_failure():
    _, expected = recall_payload("state_update")
    incorrect = dict(expected)
    incorrect["original_format"], incorrect["current_format"] = (
        expected["current_format"],
        expected["original_format"],
    )
    with pytest.raises(ValueError, match="required facts"):
        validate_answer(json.dumps(incorrect), expected)


def test_negative_control_does_not_supply_or_expect_history():
    body, expected = recall_payload("no_history")
    assert len(body["messages"]) == 1
    assert set(expected.values()) == {"UNKNOWN"}
    validate_answer(json.dumps(expected), expected)
    incorrect = dict(expected, project_code="hallucinated_project")
    with pytest.raises(ValueError, match="required facts"):
        validate_answer(json.dumps(incorrect), expected)


def test_factual_check_rejects_prose_that_only_contains_expected_keywords():
    _, expected = recall_payload("state_update")
    with pytest.raises(ValueError, match="JSON object"):
        validate_answer("I do not know. Possible values: " + str(expected), expected)


@pytest.mark.parametrize("case", ["state_update", "no_history"])
def test_fact_checks_use_the_observed_native_cli_reasoning_settings(case):
    body, _ = recall_payload(case)
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}


def test_missing_thinking_does_not_hide_a_correct_recall_result():
    events = [
        {"type": "content_block_delta", "delta": {"text": "hi"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]
    result = assess_reply(read_reply(_response(events)), "hi")
    assert result["answer"] == "hi"
    assert result["recall_passed"] is True
    assert result["thinking_observed"] is False
    assert result["passed"] is False
    assert result["errors"] == ["no thinking output observed for the requested adaptive/high mode"]
    events.insert(0, {"type": "content_block_delta", "delta": {"thinking": "Synthetic reason"}})
    result = assess_reply(read_reply(_response(events)), "hi")
    assert result["thinking_observed"] is True
    assert result["recall_passed"] is True
    assert result["passed"] is True


@pytest.mark.parametrize("thinking_observed", [True, False])
def test_incorrect_recall_fails_independently_of_thinking(thinking_observed):
    result = assess_reply(
        ModelReply("这是我们第一次交流,我没有之前的对话记录。", thinking_observed), "hi"
    )
    assert result["recall_passed"] is False
    assert result["thinking_observed"] is thinking_observed
    assert result["passed"] is False
    assert len(result["errors"]) == (1 if thinking_observed else 2)


def test_cli_reports_recall_even_when_thinking_output_is_missing(tmp_path, monkeypatch, capsys):
    key_file = tmp_path / "test-key"
    key_file.write_text("synthetic-local-fixture-key")
    monkeypatch.setattr(
        sys,
        "argv",
        ["preflight", "http://127.0.0.1", "--key-file", str(key_file), "--repetitions", "1"],
    )
    events = [
        {"type": "content_block_delta", "delta": {"text": "你之前说了 hi,没有布置具体任务。"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]
    client_type = httpx.Client
    transport = httpx.MockTransport(lambda request: _response(events))
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: client_type(transport=transport, **kwargs)
    )
    assert main() == 1
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(results) == 5
    hi = results[0]
    assert hi["case"] == "hi"
    assert hi["recall_passed"] is True
    assert hi["thinking_observed"] is False
    assert hi["passed"] is False
    assert hi["answer"] == "你之前说了 hi,没有布置具体任务。"


def _response(events):
    return httpx.Response(200, text="".join(f"data: {json.dumps(event)}\n\n" for event in events))


@pytest.mark.parametrize("stop_reason", [None, "max_tokens", "tool_use"])
def test_incomplete_or_truncated_stream_is_not_a_pass(stop_reason):
    events = [{"type": "content_block_delta", "delta": {"text": "hi"}}]
    if stop_reason:
        events += [
            {"type": "message_delta", "delta": {"stop_reason": stop_reason}},
            {"type": "message_stop"},
        ]
    with pytest.raises(ValueError, match=r"incomplete|truncated"):
        read_reply(_response(events))


def test_stream_error_cannot_be_hidden_by_an_earlier_valid_answer():
    with pytest.raises(ValueError, match="streaming error"):
        read_reply(
            _response(
                [
                    {"type": "content_block_delta", "delta": {"text": "hi"}},
                    {"type": "error", "error": {"type": "overloaded_error"}},
                ]
            )
        )


def test_complete_stream_keeps_all_answer_fragments():
    reply = read_reply(
        _response(
            [
                {"type": "content_block_delta", "delta": {"text": "You said "}},
                {"type": "content_block_delta", "delta": {"text": "hi."}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ]
        )
    )
    assert reply.text == "You said hi."
    assert reply.thinking_observed is False
    validate_answer(reply.text, "hi")
