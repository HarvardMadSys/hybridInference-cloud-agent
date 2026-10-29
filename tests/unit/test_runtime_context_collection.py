"""What the runtimes now keep about context: compaction reports and windows.

Context insights read compaction and capacity only from what the CLI itself
said. These pin the shapes the pinned CLIs send and the payloads stored.
"""

from __future__ import annotations

import json

from cloud_agent_host.runtimes import ClaudeCodeRuntime, PiRuntime
from cloud_agent_host.runtimes.events import (
    MAX_REPORTED_CONTEXT_WINDOWS,
    reported_compaction,
    reported_context_windows,
)


def test_claude_compact_boundary_keeps_the_clis_own_sizes():
    event = ClaudeCodeRuntime().parse_event(
        json.dumps(
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compact_metadata": {"trigger": "auto", "pre_tokens": 151_234},
            }
        )
    )
    assert event is not None and event.event_type == "lifecycle"
    assert event.payload["phase"] == "compact_boundary"
    # post_tokens is absent from this report and stays absent, never 0.
    assert event.payload["compaction"] == {"trigger": "auto", "pre_tokens": 151_234}


def test_claude_compact_boundary_without_metadata_is_still_a_compaction():
    event = ClaudeCodeRuntime().parse_event(
        json.dumps({"type": "system", "subtype": "compact_boundary"})
    )
    assert event is not None and event.payload["phase"] == "compact_boundary"
    assert "compaction" not in event.payload


def test_claude_result_keeps_each_models_context_window():
    event = ClaudeCodeRuntime().parse_event(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "done",
                "usage": {"input_tokens": 3, "output_tokens": 5},
                "modelUsage": {
                    "claude-sonnet-4-5": {"inputTokens": 3, "contextWindow": 200_000},
                    "claude-haiku": {"inputTokens": 1, "contextWindow": "big"},
                },
            }
        )
    )
    assert event is not None and event.payload["phase"] == "result"
    assert event.payload["context_windows"] == {"claude-sonnet-4-5": 200_000}


def test_claude_error_result_keeps_its_window_too():
    event = ClaudeCodeRuntime().parse_event(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "errors": ["boom"],
                "modelUsage": {"m": {"contextWindow": 1_000_000}},
            }
        )
    )
    assert event is not None and event.event_type == "error"
    assert event.payload["context_windows"] == {"m": 1_000_000}


def test_claude_result_without_model_usage_reports_no_window():
    event = ClaudeCodeRuntime().parse_event(
        json.dumps({"type": "result", "subtype": "success", "result": "ok"})
    )
    assert event is not None and "context_windows" not in event.payload


def test_pi_completed_compaction_is_a_compact_boundary_with_its_report():
    runtime = PiRuntime()
    [event] = runtime.parse_events(
        json.dumps(
            {
                "type": "compaction_end",
                "reason": "threshold",
                "result": {
                    "summary": "x" * 20_000,
                    "firstKeptEntryId": "e1",
                    "tokensBefore": 120_500,
                },
                "aborted": False,
                "willRetry": False,
            }
        )
    )
    assert event.event_type == "lifecycle"
    assert event.payload == {
        "phase": "compact_boundary",
        "compaction": {"trigger": "threshold", "pre_tokens": 120_500},
        "completed": True,
        "aborted": False,
        "will_retry": False,
    }
    # The summary text never reaches the stored event.
    assert "x" * 100 not in json.dumps(event.payload)


def test_pi_aborted_compaction_is_a_diagnostic_not_a_boundary():
    [event] = PiRuntime().parse_events(
        json.dumps(
            {
                "type": "compaction_end",
                "reason": "overflow",
                "result": None,
                "aborted": True,
                "willRetry": False,
                "errorMessage": "cancelled",
            }
        )
    )
    assert event.payload["phase"] == "compaction_aborted"
    assert event.payload["completed"] is False
    assert event.payload["aborted"] is True
    assert event.payload["text"] == "cancelled"


def test_pi_compaction_start_names_its_trigger():
    [event] = PiRuntime().parse_events(json.dumps({"type": "compaction_start", "reason": "manual"}))
    assert event.payload == {"phase": "compaction_start", "compaction": {"trigger": "manual"}}


def test_reported_compaction_rejects_non_counts():
    assert reported_compaction(trigger=" auto ", pre_tokens=-1, post_tokens=2.5) == {
        "trigger": "auto"
    }
    assert reported_compaction(trigger=None, pre_tokens=True) == {}
    assert reported_compaction(trigger="manual", pre_tokens=10.0, post_tokens=0) == {
        "trigger": "manual",
        "pre_tokens": 10,
        "post_tokens": 0,
    }


def test_reported_context_windows_is_bounded():
    usage = {f"m{i}": {"contextWindow": 1000 + i} for i in range(20)}
    windows = reported_context_windows(usage)
    assert windows is not None and len(windows) == MAX_REPORTED_CONTEXT_WINDOWS
    assert reported_context_windows({"m": {"contextWindow": 0}}) is None
    assert reported_context_windows([]) is None
