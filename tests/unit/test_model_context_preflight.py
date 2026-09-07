"""Prevent a green preflight from meaning only HTTP 200 or a partial answer."""

import json
import re

import httpx
import pytest

from deploy.preflight.check_model_context import read_answer, recall_payload, validate_answer


@pytest.mark.parametrize("case,expected", [("hi", "hi"), ("repeat", "hi"), ("task", "comet_731")])
def test_prompt_requires_recalling_earlier_content(case, expected):
    body, recalled = recall_payload(case)
    assert recalled == expected
    assert body["model"] == "deepseek-v4-flash"
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
        read_answer(_response(events))


def test_stream_error_cannot_be_hidden_by_an_earlier_valid_answer():
    with pytest.raises(ValueError, match="streaming error"):
        read_answer(
            _response(
                [
                    {"type": "content_block_delta", "delta": {"text": "hi"}},
                    {"type": "error", "error": {"type": "overloaded_error"}},
                ]
            )
        )


def test_complete_stream_keeps_all_answer_fragments():
    answer = read_answer(
        _response(
            [
                {"type": "content_block_delta", "delta": {"text": "You said "}},
                {"type": "content_block_delta", "delta": {"text": "hi."}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ]
        )
    )
    assert answer == "You said hi."
    validate_answer(answer, "hi")
