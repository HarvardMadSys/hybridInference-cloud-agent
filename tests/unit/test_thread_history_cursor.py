"""Cursor validation cannot turn untrusted query strings into unbounded seeks."""

import pytest

from cloud_agent.domain.thread_history import history_cursor, parse_history_cursor


def test_history_cursor_preserves_the_full_order_key():
    assert parse_history_cursor(history_cursor("thread", (2, 1, 999)), "thread") == (2, 1, 999)
    assert parse_history_cursor(None, "thread") is None


@pytest.mark.parametrize(
    "value",
    [
        "bad",
        "!",
        "a" * 513,
        "e30",
        "WzFd",
        history_cursor("other", (2, 1, 1)),
        history_cursor("thread", (2**63, 1, 1)),
        history_cursor("thread", (2, 1, 2**63)),
        history_cursor("thread", (True, 1, 1)),
    ],
)
def test_invalid_or_cross_thread_cursors_fail_closed(value):
    with pytest.raises(ValueError, match="cursor"):
        parse_history_cursor(value, "thread")
