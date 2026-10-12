"""Unit tests for the thread stats projection.

The cases below pin the two properties the module exists to protect: that
parallel tool calls cannot inflate tool time past wall time, and that a
measure we do not have stays ``None`` instead of becoming a zero someone
would read as a fact.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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


def test_repeated_response_usage_is_counted_once_with_later_output_updates() -> None:
    """Content blocks share an id; different responses can have equal counts."""
    events = [
        _event(
            seq=seq,
            event_type=kind,
            payload={"usage_id": response_id, "usage": {"input_tokens": 100, "output_tokens": out}},
        )
        for seq, kind, response_id, out in [
            (1, "thinking", "a", 0),
            (2, "message", "a", 5),
            (3, "tool_use", "a", 10),
            (4, "tool_use", "a", 5),
            (5, "message", "b", 10),
        ]
    ]
    stats = summarize_thread(job_ids=["job-1"], attempts=[], events=events)
    assert stats.input_tokens == 200
    assert stats.output_tokens == 20


@pytest.mark.parametrize("kind", ["lifecycle", "error"])
def test_completed_usage_replaces_only_its_own_attempt(kind) -> None:
    """Final counts replace provisional data without erasing retry usage."""
    events = [
        _event(
            seq=1,
            attempt_id=attempt_id,
            event_type="message",
            payload={"usage_id": "same-id", "usage": {"input_tokens": 100, "output_tokens": 0}},
        )
        for attempt_id in [1, 2]
    ]
    events.append(
        _event(
            seq=2,
            attempt_id=2,
            event_type=kind,
            payload={
                "usage_scope": "attempt",
                "usage": {"input_tokens": 40, "output_tokens": 7},
            },
        )
    )
    stats = summarize_thread(job_ids=["job-1"], attempts=[], events=events)
    assert stats.input_tokens == 140
    assert stats.output_tokens == 7


def test_same_response_id_in_separate_turns_is_not_deduplicated() -> None:
    stats = summarize_thread(
        job_ids=["job-1", "job-2"],
        attempts=[],
        events=[
            _event(
                job_id=job_id,
                seq=1,
                event_type="message",
                payload={"usage_id": "same-id", "usage": {"input_tokens": 100}},
            )
            for job_id in ["job-1", "job-2"]
        ],
    )
    assert stats.input_tokens == 200


def test_projected_text_presence_withholds_false_zero_output() -> None:
    """SQL omits full text but retains the fact that an answer was produced."""
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[],
        events=[
            _event(
                seq=1,
                event_type="message",
                payload={"has_text": True, "usage": {"output_tokens": 0}},
            )
        ],
    )
    assert stats.output_tokens is None
    assert stats.output_tokens_per_second is None


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


def test_named_pi_responses_still_add() -> None:
    """pi names each response once; naming them must not merge two of them."""
    usage = {
        "input_tokens": 10,
        "output_tokens": 1,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[_attempt()],
        events=[
            _event(
                seq=1, event_type="usage", payload={**usage, "usage_id": "chatcmpl-a"}, offset=2
            ),
            _event(
                seq=2, event_type="usage", payload={**usage, "usage_id": "chatcmpl-b"}, offset=3
            ),
        ],
    )

    assert stats.input_tokens == 20
    assert stats.output_tokens == 2


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


def test_ttft_across_a_thread_is_the_mean_of_its_turns() -> None:
    """A multi-turn thread reports the average first-token time, not one turn's.

    Every other ttft case here has exactly one sample, which makes the mean
    indistinguishable from the max, the min, or the sum — mutation testing
    found that replacing the mean with `max()` left the suite green. A thread
    is the unit this stat is displayed for, so the aggregation across turns is
    the behaviour worth pinning, and the three samples below are chosen so
    that no other reduction produces the same number.
    """
    stats = summarize_thread(
        job_ids=["job-1", "job-2", "job-3"],
        attempts=[
            _attempt(job_id="job-1", offset=0),
            _attempt(job_id="job-2", offset=100),
            _attempt(job_id="job-3", offset=200),
        ],
        events=[
            _event(job_id="job-1", seq=1, event_type="message", payload={"text": "a"}, offset=2),
            _event(job_id="job-2", seq=1, event_type="message", payload={"text": "b"}, offset=104),
            _event(job_id="job-3", seq=1, event_type="message", payload={"text": "c"}, offset=212),
        ],
    )

    # Samples are 2s, 4s and 12s: mean 6, max 12, min 2, sum 18.
    assert stats.ttft_seconds == 6.0


def test_ttft_ignores_a_turn_that_never_produced_output() -> None:
    """A turn with no output contributes no sample, rather than a zero.

    The same rule the rest of this module follows: a measure we do not have
    stays absent instead of becoming a number someone would average in.
    """
    stats = summarize_thread(
        job_ids=["job-1", "job-2"],
        attempts=[
            _attempt(job_id="job-1", offset=0),
            _attempt(job_id="job-2", offset=100),
        ],
        events=[
            _event(job_id="job-1", seq=1, event_type="message", payload={"text": "a"}, offset=4),
            # job-2 started and only ever emitted lifecycle noise.
            _event(
                job_id="job-2",
                seq=1,
                event_type="lifecycle",
                payload={"phase": "start"},
                offset=101,
            ),
        ],
    )

    assert stats.ttft_seconds == 4.0


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
