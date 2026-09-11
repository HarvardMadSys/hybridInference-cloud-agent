"""Run performance keeps retries, incomplete timing and usage coverage distinct."""

from __future__ import annotations

import pytest

from cloud_agent.session_statistics import summarize_session_statistics
from tests.unit.test_thread_stats import _attempt, _event


def _stats(events, attempts=()):
    return summarize_session_statistics(job_ids=["job-1"], attempts=attempts, events=events)


def _execution(attempt_id=1, offset=0, output_tokens=40):
    return [
        _event(seq=1, event_type="lifecycle", offset=offset, attempt_id=attempt_id),
        _event(seq=2, event_type="thinking", offset=offset + 2, attempt_id=attempt_id),
        _event(
            seq=3,
            event_type="message",
            offset=offset + 10,
            attempt_id=attempt_id,
            payload={
                "has_text": True,
                "usage_scope": "attempt",
                "usage": {} if output_tokens is None else {"output_tokens": output_tokens},
            },
        ),
    ]


def test_retries_sum_active_windows_and_average_each_attempts_first_output():
    events = _execution() + _execution(attempt_id=2, offset=100, output_tokens=80)
    # The second execution takes four seconds from attempt start to first output.
    attempts = [
        {**_attempt(), "attempt_id": 1},
        {**_attempt(offset=98), "attempt_id": 2},
    ]
    stats = _stats(list(reversed(events)), attempts)
    assert stats.model_seconds == 20  # excludes the 90-second gap between attempts
    assert stats.tool_seconds == 0
    assert stats.ttft_seconds == 3  # mean of 2 and 4, never first-attempt-to-last-output
    assert stats.output_tokens_per_second == 6  # 120 tokens / 20 measured seconds
    assert stats.timing_source == "message_events"


def test_overlapping_tool_windows_count_once_with_reused_ids_in_a_retry():
    events = []
    for attempt_id, offset in [(1, 0), (2, 100)]:
        for seq, kind, payload, at in [
            (1, "lifecycle", {}, 0),
            (2, "tool_use", {"id": "same-a"}, 1),
            (3, "tool_use", {"id": "same-b"}, 2),
            (4, "tool_result", {"tool_use_id": "same-a"}, 5),
            (5, "tool_result", {"tool_use_id": "same-b"}, 7),
            (6, "message", {"usage": {"output_tokens": 40}}, 10),
        ]:
            events.append(
                _event(
                    seq=seq,
                    event_type=kind,
                    payload=payload,
                    offset=offset + at,
                    attempt_id=attempt_id,
                )
            )
    stats = _stats(events)
    assert stats.tool_seconds == 12  # each execution's union spans seconds 1-7
    assert stats.model_seconds == 8
    assert stats.output_tokens_per_second == 10


def test_tool_result_in_a_retry_cannot_finish_the_previous_attempts_call():
    events = [
        _event(seq=1, event_type="tool_use", payload={"id": "same"}, offset=1),
        _event(seq=2, event_type="message", offset=10),
        _event(
            seq=1,
            event_type="tool_result",
            payload={"tool_use_id": "same"},
            offset=100,
            attempt_id=2,
        ),
        _event(seq=2, event_type="message", offset=110, attempt_id=2),
    ]
    stats = _stats(events)
    assert stats.tool_seconds is None
    assert stats.model_seconds is None
    assert stats.output_tokens_per_second is None


def test_sequential_reuse_of_a_closed_tool_identity_preserves_both_windows():
    events = [
        _event(seq=seq, event_type=kind, payload=payload, offset=at)
        for seq, (kind, payload, at) in enumerate(
            [
                ("tool_use", {"id": "a"}, 0),
                ("tool_result", {"tool_use_id": "a"}, 2),
                ("tool_use", {"id": "a"}, 4),
                ("tool_result", {"tool_use_id": "a"}, 6),
                ("message", {}, 10),
            ],
            start=1,
        )
    ]
    stats = _stats(events)
    assert stats.tool_seconds == 4
    assert stats.model_seconds == 6


@pytest.mark.parametrize(
    "events",
    [
        [],
        [_event(seq=1, event_type="message")],
        [_event(seq=1, event_type="message"), _event(seq=2, event_type="message")],
        [_event(seq=1, event_type="lifecycle"), _event(seq=2, event_type="raw", offset=10)],
        [
            _event(seq=1, event_type="message", offset=10),
            _event(seq=2, event_type="message", offset=2),
        ],
        [
            _event(seq=1, event_type="message"),
            {**_event(seq=2, event_type="message", offset=10), "created_at": None},
        ],
        [{**event, "attempt_id": None} for event in _execution()],
    ],
)
def test_absent_or_invalid_measurements_remain_null(events):
    stats = _stats(events)
    assert stats.model_seconds is None
    assert stats.tool_seconds is None
    assert stats.ttft_seconds is None
    assert stats.output_tokens_per_second is None


@pytest.mark.parametrize(
    "tool_events",
    [
        [("tool_use", {"id": "a"})],
        [("tool_result", {"tool_use_id": "a"})],
        [("tool_use", {}), ("tool_result", {})],
        [("tool_use", {"id": "a"}), ("tool_use", {"id": "a"})],
    ],
)
def test_incomplete_tool_timing_is_not_reclassified_as_model_time(tool_events):
    events = [_event(seq=1, event_type="thinking")]
    events.extend(
        _event(seq=seq, event_type=kind, payload=payload, offset=seq)
        for seq, (kind, payload) in enumerate(tool_events, start=2)
    )
    events.append(
        _event(seq=10, event_type="message", offset=20, payload={"usage": {"output_tokens": 50}})
    )
    stats = _stats(events)
    assert stats.tool_seconds is None
    assert stats.model_seconds is None
    assert stats.output_tokens_per_second is None


def test_tps_excludes_time_without_counts_and_counts_without_timing():
    events = _execution(output_tokens=40) + _execution(attempt_id=2, offset=100, output_tokens=None)
    events.append(
        _event(
            attempt_id=3,
            seq=1,
            event_type="usage",
            offset=200,
            payload={"usage": {"output_tokens": 1000}},
        )
    )
    stats = _stats(events)
    assert stats.model_seconds is None  # incomplete total timing for execution 3
    assert stats.tool_seconds is None
    assert stats.output_tokens_per_second == 4  # only execution 1 has both measurements


def test_no_runtime_counts_keeps_timing_but_withholds_tps():
    stats = _stats(_execution(output_tokens=None))
    assert stats.model_seconds == 10
    assert stats.tool_seconds == 0
    assert stats.output_tokens_per_second is None


def test_false_zero_output_does_not_become_zero_tps():
    stats = _stats(_execution(output_tokens=0))
    assert stats.model_seconds == 10
    assert stats.output_tokens_per_second is None


def test_unobserved_execution_withholds_total_time_but_not_available_ttft_samples():
    stats = _stats(
        _execution(),
        [{**_attempt(), "attempt_id": 1}, {**_attempt(offset=100), "attempt_id": 2}],
    )
    assert stats.model_seconds is None
    assert stats.tool_seconds is None
    assert stats.ttft_seconds == 2
    assert stats.output_tokens_per_second == 4


def test_other_runs_do_not_contribute_to_session_statistics():
    foreign = [{**event, "job_id": "other-job"} for event in _execution(attempt_id=2, offset=100)]
    stats = _stats(_execution() + foreign)
    assert stats.model_seconds == 10
    assert stats.output_tokens_per_second == 4
