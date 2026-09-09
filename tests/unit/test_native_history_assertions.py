"""Keep request-level history regressions sensitive to false passes."""

import pytest

from tests.external.test_claude_driver_context import MODEL, _assert_history


def _request(turns):
    return {"model": MODEL, "messages": [{"role": role, "content": text} for role, text in turns]}


@pytest.mark.parametrize(
    "actual",
    [
        [("assistant", "reply"), ("user", "hi")],
        [("assistant", "hi"), ("user", "reply")],
        [("user", "hi"), ("assistant", "reply")],
        [("user", "hi"), ("assistant", "reply"), ("user", "prefix hi suffix")],
    ],
)
def test_reordered_wrong_role_missing_duplicate_and_partial_text_are_rejected(actual):
    with pytest.raises(AssertionError, match="native history"):
        _assert_history(_request(actual), [("user", "hi"), ("assistant", "reply"), ("user", "hi")])


def test_identical_repeated_user_messages_require_two_native_occurrences():
    turns = [("user", "hi"), ("assistant", "reply"), ("user", "hi")]
    _assert_history(_request(turns), turns)
