"""User-facing usage keeps each agent's input definition, and counts nothing it cannot."""

from __future__ import annotations

import pytest

from cloud_agent.thread_stats import normalized_runtime_input_tokens


def _event(usage, *, attempt_id=1, seq=1, **payload):
    return {
        "attempt_id": attempt_id,
        "seq": seq,
        "event_type": "usage",
        "payload": {"usage": usage, **payload},
    }


@pytest.mark.parametrize("runtime", ["claude-code", "pi"])
def test_cache_counters_are_added_once_per_response_and_per_attempt(runtime):
    counters = {"input_tokens": 100, "cache_read_tokens": 200, "cache_write_tokens": 30}
    events = [
        _event(counters, usage_id="same", seq=1),
        _event(counters, usage_id="same", seq=2),
        _event(counters, usage_id="same", seq=3, attempt_id=2),
    ]
    assert normalized_runtime_input_tokens(runtime=runtime, events=events) == 660


def test_attempt_total_replaces_incremental_responses_and_input_zero_is_valid():
    events = [
        _event({"input_tokens": 100}, usage_id="response"),
        _event(
            {"input_tokens": 0, "cache_read_tokens": 200, "cache_write_tokens": 30},
            seq=2,
            usage_scope="attempt",
        ),
    ]
    assert normalized_runtime_input_tokens(runtime="claude-code", events=events) == 230


def test_codex_input_already_includes_cached_tokens():
    # The Codex driver's usage event: ``inputTokens`` includes ``cachedInputTokens``.
    events = [_event({"input_tokens": 400, "cache_read_tokens": 250, "cache_write_tokens": 0})]
    assert normalized_runtime_input_tokens(runtime="codex", events=events) == 400


@pytest.mark.parametrize("runtime", ["claude-code", "pi", "kilo", "opencode", None])
def test_missing_cache_or_unknown_runtime_cannot_establish_inclusive_input(runtime):
    events = [_event({"input_tokens": 100, "cache_read_tokens": 200})]
    assert normalized_runtime_input_tokens(runtime=runtime, events=events) is None


def test_missing_counter_in_one_response_does_not_create_a_partial_input_total():
    events = [
        _event({"input_tokens": 100, "cache_read_tokens": 200, "cache_write_tokens": 30}),
        _event({"output_tokens": 20}, seq=2),
    ]
    assert normalized_runtime_input_tokens(runtime="claude-code", events=events) is None
    assert normalized_runtime_input_tokens(runtime="codex", events=events) is None
