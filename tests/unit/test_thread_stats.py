"""Unit tests for the thread stats projection.

The cases below pin the two properties the module exists to protect: that
parallel tool calls cannot inflate tool time past wall time, and that a
measure we do not have stays ``None`` instead of becoming a zero someone
would read as a fact.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cloud_agent.thread_stats import summarize_thread

_T0 = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)


def _event(
    *,
    job_id: str = "job-1",
    seq: int,
    event_type: str,
    payload: dict | None = None,
    offset: float = 0.0,
    attempt_id: int = 1,
) -> dict:
    return {
        "job_id": job_id,
        "attempt_id": attempt_id,
        "seq": seq,
        "event_type": event_type,
        "payload": payload or {},
        "created_at": _T0 + timedelta(seconds=offset),
    }


def _attempt(*, job_id: str = "job-1", offset: float = 0.0) -> dict:
    return {"job_id": job_id, "started_at": _T0 + timedelta(seconds=offset)}


def test_serial_tool_calls_sum_their_windows() -> None:
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(seq=1, event_type="tool_use", payload={"id": "a"}, offset=1),
            _event(seq=2, event_type="tool_result", payload={"tool_use_id": "a"}, offset=4),
            _event(seq=3, event_type="tool_use", payload={"id": "b"}, offset=6),
            _event(seq=4, event_type="tool_result", payload={"tool_use_id": "b"}, offset=8),
            _event(seq=5, event_type="message", payload={"text": "done"}, offset=10),
        ],
    )

    assert stats.tool_calls == 2
    assert stats.tool_seconds == 5.0  # 3s + 2s, no overlap
    assert stats.wall_seconds == 9.0
    assert stats.model_seconds == 4.0


def test_parallel_tool_calls_count_overlap_once() -> None:
    """Two calls overlapping in time must not report more tool time than elapsed."""
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(seq=1, event_type="tool_use", payload={"id": "a"}, offset=0),
            _event(seq=2, event_type="tool_use", payload={"id": "b"}, offset=1),
            _event(seq=3, event_type="tool_result", payload={"tool_use_id": "a"}, offset=5),
            _event(seq=4, event_type="tool_result", payload={"tool_use_id": "b"}, offset=6),
        ],
    )

    # Summed durations would be 5 + 5 = 10s across a 6s turn; the union is 6s.
    assert stats.tool_seconds == 6.0
    assert stats.tool_seconds <= stats.wall_seconds
    assert stats.model_seconds == 0.0


def test_unfinished_tool_call_contributes_no_window() -> None:
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(seq=1, event_type="tool_use", payload={"id": "a"}, offset=1),
            _event(seq=2, event_type="message", payload={"text": "still going"}, offset=9),
        ],
    )

    assert stats.tool_calls == 1
    assert stats.tool_seconds == 0.0


def test_claude_usage_shape_is_summed() -> None:
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(
                seq=1,
                event_type="tool_use",
                payload={
                    "id": "a",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "cache_read_input_tokens": 300,
                    },
                },
                offset=1,
            ),
            _event(seq=2, event_type="tool_result", payload={"tool_use_id": "a"}, offset=2),
            _event(
                seq=3,
                event_type="message",
                payload={"text": "ok", "usage": {"input_tokens": 100, "output_tokens": 40}},
                offset=4,
            ),
        ],
    )

    assert stats.input_tokens == 200
    assert stats.output_tokens == 50
    assert stats.cache_read_tokens == 300
    assert stats.cache_hit_ratio == 0.6  # 300 cached of 500 total input
    assert stats.tokens_source == "runtime"


def test_pi_usage_event_shape_is_summed() -> None:
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(
                seq=1,
                event_type="usage",
                payload={
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "total_tokens": 11,
                },
                offset=2,
            ),
        ],
    )

    assert stats.input_tokens == 10
    assert stats.output_tokens == 1
    assert stats.tokens_source == "runtime"


def test_runtime_without_usage_reports_none_not_zero() -> None:
    """The distinction the whole module exists for: absent is not zero."""
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(seq=1, event_type="raw", payload={"text": "tier-2 tail"}, offset=1),
            _event(seq=2, event_type="raw", payload={"text": "more tail"}, offset=2),
        ],
    )

    assert stats.input_tokens is None
    assert stats.output_tokens is None
    assert stats.cache_hit_ratio is None
    assert stats.output_tokens_per_second is None
    assert stats.tokens_source == "unavailable"


def test_ttft_measures_from_attempt_start_to_first_output() -> None:
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt(offset=0)],
        events=[
            # Lifecycle noise precedes the model's own first output and must
            # not be mistaken for it.
            _event(seq=1, event_type="lifecycle", payload={"phase": "start"}, offset=1),
            _event(seq=2, event_type="thinking", payload={"text": "hm"}, offset=3),
            _event(seq=3, event_type="message", payload={"text": "hi"}, offset=5),
        ],
    )

    assert stats.ttft_seconds == 3.0


def test_ttft_uses_the_first_attempt_of_a_retried_turn() -> None:
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt(offset=10), _attempt(offset=0)],
        events=[_event(seq=1, event_type="message", payload={"text": "hi"}, offset=12)],
    )

    assert stats.ttft_seconds == 12.0


def test_ttft_is_absent_without_an_attempt_row() -> None:
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[],
        events=[_event(seq=1, event_type="message", payload={"text": "hi"}, offset=2)],
    )

    assert stats.ttft_seconds is None


def test_multiple_turns_aggregate_across_the_thread() -> None:
    stats = summarize_thread(
        job_ids=["job-1", "job-2"],
        attempts=[_attempt(job_id="job-1"), _attempt(job_id="job-2", offset=100)],
        events=[
            _event(job_id="job-1", seq=1, event_type="tool_use", payload={"id": "a"}, offset=1),
            _event(
                job_id="job-1",
                seq=2,
                event_type="tool_result",
                payload={"tool_use_id": "a"},
                offset=3,
            ),
            _event(job_id="job-1", seq=3, event_type="message", payload={"text": "1"}, offset=5),
            _event(
                job_id="job-2",
                seq=1,
                event_type="tool_use",
                payload={"id": "b"},
                offset=101,
                attempt_id=2,
            ),
            _event(
                job_id="job-2",
                seq=2,
                event_type="tool_result",
                payload={"tool_use_id": "b", "is_error": True},
                offset=104,
                attempt_id=2,
            ),
        ],
    )

    assert stats.turns == 2
    assert stats.tool_calls == 2
    assert stats.failed_tool_calls == 1
    assert stats.tool_seconds == 5.0  # 2s in turn 1, 3s in turn 2
    assert stats.wall_seconds == 7.0  # 4s in turn 1, 3s in turn 2


def test_empty_thread_is_all_zero_and_never_raises() -> None:
    stats = summarize_thread(job_ids=["job-1"], attempts=[], events=[])

    assert stats.turns == 1
    assert stats.tool_calls == 0
    assert stats.wall_seconds == 0.0
    assert stats.model_seconds == 0.0
    assert stats.ttft_seconds is None
    assert stats.tokens_source == "unavailable"


def test_tokens_per_second_uses_model_time_only() -> None:
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(seq=1, event_type="tool_use", payload={"id": "a"}, offset=0),
            _event(seq=2, event_type="tool_result", payload={"tool_use_id": "a"}, offset=6),
            _event(
                seq=3,
                event_type="message",
                payload={"text": "done", "usage": {"output_tokens": 200}},
                offset=10,
            ),
        ],
    )

    # 10s elapsed, 6s of it in a tool: 200 tokens over the 4s of model time.
    assert stats.model_seconds == 4.0
    assert stats.output_tokens_per_second == 50.0


def test_events_out_of_clock_order_follow_stored_sequence() -> None:
    """A clock that jitters must not reorder the ledger."""
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(seq=2, event_type="tool_result", payload={"tool_use_id": "a"}, offset=4),
            _event(seq=1, event_type="tool_use", payload={"id": "a"}, offset=1),
        ],
    )

    assert stats.tool_seconds == 3.0


def test_unknown_usage_shape_is_ignored_rather_than_half_counted() -> None:
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(
                seq=1,
                event_type="lifecycle",
                payload={"phase": "result", "usage": {"tokens_we_do_not_know": 999}},
                offset=1,
            ),
        ],
    )

    assert stats.input_tokens is None
    assert stats.output_tokens is None
    assert stats.tokens_source == "unavailable"


def test_codex_lifecycle_usage_is_read_when_its_keys_are_recognizable() -> None:
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(
                seq=1,
                event_type="lifecycle",
                payload={
                    "phase": "result",
                    "usage": {"input_tokens": 42, "output_tokens": 7},
                },
                offset=1,
            ),
        ],
    )

    assert stats.input_tokens == 42
    assert stats.output_tokens == 7
    assert stats.tokens_source == "runtime"


def test_a_zero_output_count_is_withheld_when_the_thread_plainly_produced_text() -> None:
    """A count we can prove wrong is worse than no count.

    Runtimes report their own usage, and a CLI talking to a model whose
    response does not carry usage in the shape it reads reports **zero**
    rather than nothing — which is what happens to every gateway-served
    model this deployment actually runs. A thread that visibly produced
    assistant text did not produce zero output tokens, and rendering that
    figure states something false with the same confidence as a real number.
    """
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(
                seq=1,
                event_type="message",
                payload={
                    "text": "here is the summary",
                    "usage": {"input_tokens": 58_000, "output_tokens": 0},
                },
                offset=1,
            ),
            _event(seq=2, event_type="lifecycle", payload={"phase": "succeeded"}, offset=10),
        ],
    )

    assert stats.input_tokens == 58_000, "the count the runtime does know is still reported"
    assert stats.output_tokens is None, "a provably false zero was rendered as fact"
    assert stats.output_tokens_per_second is None, "a rate derived from a false zero"


def test_a_genuine_zero_output_survives_when_nothing_was_produced() -> None:
    """Silence is for counts we disbelieve, not for every zero.

    A turn that emitted no assistant text and reports zero output tokens is
    reporting the truth, and withholding it would be its own dishonesty.
    """
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(
                seq=1,
                event_type="tool_use",
                payload={
                    "name": "Bash",
                    "id": "t1",
                    "usage": {"input_tokens": 100, "output_tokens": 0},
                },
                offset=1,
            ),
        ],
    )

    assert stats.output_tokens == 0
