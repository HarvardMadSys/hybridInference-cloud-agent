"""A failed sandbox deletion's retry decision, made without a database.

``fail_admin_sandbox_deletion`` locks the request, reads it and writes what
:func:`decide_deletion_failure` decides. The end-to-end behaviour is covered
by ``tests/integration/storage/test_admin_sandbox_deletion.py``; this pins the
decision's own branches.
"""

from __future__ import annotations

import pytest

from cloud_agent.domain.sandbox_deletion import (
    DELETION_RETRY_MAX_ATTEMPTS,
    DELETION_RETRY_MAX_SECONDS,
    DeletionFailure,
    decide_deletion_failure,
)


def _failure(**overrides: object) -> DeletionFailure:
    values: dict[str, object] = {
        "deletion_state": "archived",
        "retry_count": 1,
        "window_open": True,
        "retain_pending": False,
        "retry_class": "transient",
        "error": "upload timed out",
    }
    values.update(overrides)
    return DeletionFailure(**values)  # type: ignore[arg-type]


def test_a_transient_failure_of_an_archived_request_is_retried_with_backoff() -> None:
    first = decide_deletion_failure(_failure(retry_count=1))
    third = decide_deletion_failure(_failure(retry_count=3))
    assert (first.retry_scheduled, first.delay_seconds, first.terminal_reason) == (True, 60, None)
    assert third.delay_seconds == 240
    assert first.error == "upload timed out"


def test_the_backoff_stops_at_its_cap() -> None:
    late = decide_deletion_failure(_failure(retry_count=DELETION_RETRY_MAX_ATTEMPTS - 1))
    assert late.delay_seconds == min(60 * 2 ** (DELETION_RETRY_MAX_ATTEMPTS - 2), 3600)
    assert late.delay_seconds <= DELETION_RETRY_MAX_SECONDS


@pytest.mark.parametrize(
    "overrides",
    [{"retry_count": DELETION_RETRY_MAX_ATTEMPTS}, {"window_open": False}],
)
def test_a_spent_budget_or_closed_window_stops_retries(overrides: dict[str, object]) -> None:
    decision = decide_deletion_failure(_failure(**overrides))
    assert not decision.retry_scheduled
    assert decision.retry_exhausted and decision.retry_stopped
    assert decision.terminal_reason == "retry_budget_exhausted"
    assert decision.error.endswith("an administrator can resume this request.")


def test_an_unknown_retry_class_needs_an_administrator() -> None:
    decision = decide_deletion_failure(_failure(retry_class="mystery"))
    assert decision.retry_class == "operator_action"
    assert decision.retry_stopped and not decision.retry_exhausted
    assert decision.terminal_reason == "operator_action_required"
    assert "after resolving it" in decision.error


def test_a_pending_request_not_kept_fails() -> None:
    decision = decide_deletion_failure(
        _failure(deletion_state="pending", retry_class="permanent", error="x" * 600)
    )
    assert not (decision.retry_scheduled or decision.retry_stopped or decision.retry_exhausted)
    assert decision.terminal_reason == "permanent_failure"
    assert decision.error == "x" * 500


def test_a_pending_request_kept_retries_like_an_archived_one() -> None:
    decision = decide_deletion_failure(_failure(deletion_state="pending", retain_pending=True))
    assert decision.retry_scheduled
