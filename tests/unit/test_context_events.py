"""Context events and recorded composition: only what a record shows.

Profiles are built from the stored event shapes the projection in
``storage/_sql/context.py`` hands the reducer; the events and the composition
are then read back the way the Context view and the insights read them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cloud_agent.context_events import assemble_context_events, event_capabilities
from cloud_agent.context_insights import RECORDED_CATEGORIES, compose
from cloud_agent.context_profile import ContextProfileAccumulator
from cloud_agent.prompt_injections import claim_prompt
from cloud_agent_shared.text_tokens import estimate_text_tokens, split_counts

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _events(*rows: tuple[str, dict[str, Any]], start: int = 1) -> list[dict[str, Any]]:
    return [
        {
            "id": 100 + seq,
            "seq": seq,
            "event_type": kind,
            "created_at": T0 + timedelta(seconds=seq),
            "payload": payload,
        }
        for seq, (kind, payload) in enumerate(rows, start=start)
    ]


def _profile(rows: list[dict[str, Any]], attempt_id: int = 1) -> dict[str, Any]:
    accumulator = ContextProfileAccumulator(attempt_id=attempt_id, job_id="job")
    accumulator.extend(rows)
    return accumulator.finish()


def _usage(usage_id: str, cached: int, *, out: int | None = 5, **extra: Any) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input_tokens": 10,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
    }
    if out is not None:
        usage["output_tokens"] = out
    return {"usage": usage, "usage_id": usage_id, **extra}


def _job(job_id: str, turn_no: int, model: str = "m1", **extra: Any) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "turn_no": turn_no,
        "created_at": T0 + timedelta(hours=turn_no),
        "runtime": "claude-code",
        "model": model,
        **extra,
    }


# --- the platform's own injections ------------------------------------------------


def test_claim_records_what_each_wrapper_added_and_never_its_text():
    prompt, injections = claim_prompt(
        "修复登录 bug",
        attachments=[{"path": ".attachments/spec.md", "name": "spec.md", "size": 10}],
        memories=None,
        skills=[{"name": "reviewer", "description": "Review code", "path": "skills/reviewer"}],
        ssh_servers=[],
        goal=None,
        plugins=[{"plugin": "acme/lint"}],
    )
    producers = [item["producer"] for item in injections]
    assert producers == ["attachments", "skills", "plugins"]
    # Every wrapper's characters add up to exactly what the prompt grew by.
    sized = [item for item in injections if item["chars"] is not None]
    assert sum(item["chars"] for item in sized) == len(prompt) - len("修复登录 bug")
    for item in sized:
        assert item["ascii"] + item["cjk"] <= item["chars"]
    assert injections[1]["names"] == ["reviewer"]
    assert injections[2] == {
        "producer": "plugins",
        "items": 1,
        "names": ["acme/lint"],
        "chars": None,
    }
    # Sizes and names only: no injected text travels in the record.
    assert all("text" not in item for item in injections)


def test_a_claim_without_platform_context_records_nothing():
    prompt, injections = claim_prompt(
        "hello",
        attachments=None,
        memories=None,
        skills=None,
        ssh_servers=[],
        goal=None,
    )
    assert prompt == "hello"
    assert injections == []


# --- events in a profile ----------------------------------------------------------


def _started(*records: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return ("lifecycle", {"phase": "started", "context_injections": list(records)})


def test_profile_events_carry_only_their_records_sizes():
    text = "Remember: 用中文回答"
    ascii_chars, cjk, _ = split_counts(text)
    profile = _profile(
        _events(
            _started({"producer": "memory", "items": 2, "chars": 120, "ascii": 120, "cjk": 0}),
            ("lifecycle", {"phase": "init", "model": "m1", "permission_mode": "bypassPermissions"}),
            (
                "lifecycle",
                {
                    "phase": "context_injection",
                    "injection": {"source": "claude_user_line", "label": "isMeta", "chars": 16},
                    "text_chars": len(text),
                    "ascii": ascii_chars,
                    "cjk": cjk,
                },
            ),
            ("message", {**_usage("a", 1000), "chars": 40, "ascii": 40, "cjk": 0}),
            ("tool_use", {"tool_name": "EnterPlanMode", "chars": 20, "ascii": 20, "cjk": 0}),
            (
                "lifecycle",
                {
                    "phase": "context_prune",
                    "prune": {"source": "opencode_prune", "tool": "bash", "call_id": "c1"},
                },
            ),
            # A delegated agent's injection is its own context, not the conversation's.
            (
                "lifecycle",
                {
                    "phase": "context_injection",
                    "parent_tool_use_id": "toolu_task",
                    "injection": {"source": "claude_user_line"},
                    "text_chars": 500,
                },
            ),
        )
    )
    events = profile["events"]
    kinds = [(event["kind"], event["source"]) for event in events]
    assert kinds == [
        ("injection", "platform_memory"),
        ("mode", "claude_init"),
        ("injection", "claude_user_line"),
        ("mode", "claude_plan_tool"),
        ("prune", "opencode_prune"),
    ]
    assert events[0]["tokens"] == 30 and events[0]["items"] == 2
    assert events[2]["tokens"] == estimate_text_tokens(text)
    # A prune is listed with what the record names; no size was recorded.
    assert "chars" not in events[4] and "tokens" not in events[4]
    categories = profile["content"]["categories"]
    assert categories["injected"]["chars"] == len(text)
    assert categories["injected"]["items"] == 1


def test_profile_totals_sum_every_request_and_keep_subagents_apart():
    profile = _profile(
        _events(
            ("message", _usage("a", 1000, out=7)),
            ("message", _usage("a", 1000, out=9)),  # a later block of the same response
            ("message", _usage("b", 3000, out=None)),
            ("message", _usage("s", 500, out=4, parent_tool_use_id="toolu_task")),
        )
    )
    totals = profile["totals"]
    assert totals["main"] == {
        "requests": 2,
        "prompt_tokens": 1010 + 3010,
        "cache_read_tokens": 4000,
        "cache_write_tokens": 0,
        "output_tokens": 9,
        "output_unreported": 1,
    }
    assert totals["subagent"]["requests"] == 1
    assert totals["subagent"]["prompt_tokens"] == 510


def _attempt(attempt_id: int, job_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    return {"attempt_id": attempt_id, "job_id": job_id, "profile": profile}


def test_events_are_ordered_deduplicated_and_retries_are_marked():
    retried = _profile(
        _events(
            _started({"producer": "goal", "items": 1, "chars": 40, "ascii": 40, "cjk": 0}),
            ("lifecycle", {"phase": "init", "model": "m1", "permission_mode": "default"}),
        ),
        attempt_id=1,
    )
    kept = _profile(
        _events(
            _started({"producer": "goal", "items": 1, "chars": 40, "ascii": 40, "cjk": 0}),
            ("lifecycle", {"phase": "init", "model": "m1", "permission_mode": "bypassPermissions"}),
            ("lifecycle", {"phase": "compact_boundary", "compaction": {"trigger": "auto"}}),
        ),
        attempt_id=2,
    )
    second = _profile(
        _events(
            ("lifecycle", {"phase": "init", "model": "m2", "permission_mode": "bypassPermissions"}),
            (
                "lifecycle",
                {
                    "phase": "compact_boundary",
                    "compaction": {"trigger": "manual", "pre_tokens": 90_000, "post_tokens": 9_000},
                },
            ),
            ("lifecycle", {"phase": "init", "permission_mode": "plan"}),
        ),
        attempt_id=3,
    )
    events, counts, omitted = assemble_context_events(
        jobs=[_job("j1", 1), _job("j2", 2, model="m2")],
        by_job={
            "j1": [_attempt(1, "j1", retried), _attempt(2, "j1", kept)],
            "j2": [_attempt(3, "j2", second)],
        },
    )
    listed = [(e.kind, e.source, e.attempt_id, e.superseded) for e in events]
    assert listed == [
        ("injection", "platform_goal", 1, True),
        ("injection", "platform_goal", 2, False),
        ("compaction", "cli_compact_boundary", 2, False),
        ("model_switch", "turn_settings", None, False),
        ("compaction", "cli_compact_boundary", 3, False),
        ("mode", "claude_init", 3, False),
    ]
    # The retried attempt's injection is listed but not counted.
    assert counts == {
        "injection": 1,
        "compaction": 2,
        "prune": 0,
        "model_switch": 1,
        "mode": 1,
    }
    assert omitted == 0
    switch = events[3]
    assert (switch.from_value, switch.to_value) == ("m1", "m2")
    # Sizes only where the CLI reported them, and named as the CLI's.
    auto, manual = events[2], events[4]
    assert auto.before_tokens is None and auto.tokens is None and auto.tokens_basis is None
    assert (manual.before_tokens, manual.after_tokens, manual.tokens) == (90_000, 9_000, -81_000)
    assert manual.tokens_basis == "cli"
    # The permission mode is an event only where it changed.
    mode = events[5]
    assert (mode.from_value, mode.to_value) == ("bypassPermissions", "plan")


def test_a_token_drop_alone_is_not_an_event():
    profile = _profile(
        _events(
            ("message", _usage("a", 90_000)),
            ("message", _usage("b", 1_000)),
        )
    )
    events, counts, _ = assemble_context_events(
        jobs=[_job("j1", 1)], by_job={"j1": [_attempt(1, "j1", profile)]}
    )
    assert events == []
    assert sum(counts.values()) == 0


def test_every_runtime_says_why_an_event_kind_is_not_shown():
    rows = event_capabilities(["claude-code", "codex", "pi", "opencode", "kilo", "other"])
    assert len(rows) == 6 * 5
    for row in rows:
        if row.support != "reported":
            assert row.reason, row
    by_key = {(row.runtime, row.kind): row for row in rows}
    assert by_key[("claude-code", "prune")].support == "not_reported"
    assert by_key[("codex", "mode")].reason == "platform_fixed_mode"
    assert by_key[("other", "injection")].reason == "unknown_runtime"


# --- recorded composition --------------------------------------------------------


def test_compose_sums_rows_and_never_derives_a_remainder():
    rows = [
        {
            "thread_id": "a",
            "turns": 2,
            "turns_profiled": 2,
            "turns_truncated": 1,
            "prompt_tokens": 10,
            "platform_tokens": 30,
            "assistant": 100,
            "tool_results": 400,
            "main_requests": 3,
            "main_prompt_tokens": 9_000,
            "main_cache_read_tokens": 8_000,
            "main_cache_write_tokens": 0,
            "main_output_tokens": 50,
            "main_output_unreported": 0,
            "subagent_requests": 1,
            "subagent_prompt_tokens": 700,
            "subagent_output_tokens": 3,
        },
        {
            "thread_id": "b",
            "turns": 1,
            "turns_profiled": 0,
            "turns_truncated": 0,
            "prompt_tokens": 5,
            # A conversation with no profile yet has no reported sums.
            "main_requests": None,
        },
    ]
    composition = compose(rows)
    recorded = composition.recorded
    assert [(share.key, share.tokens) for share in recorded.categories] == [
        ("turn_prompts", 15),
        ("platform_context", 30),
        ("assistant", 100),
        ("tool_results", 400),
    ]
    assert recorded.total_tokens == 545
    assert (recorded.turns, recorded.turns_profiled, recorded.turns_truncated) == (3, 2, 1)
    assert recorded.basis == "chars_cjk_v1"
    # The provider's numbers are summed apart and never mixed into the estimate.
    provider = composition.provider
    assert (provider.requests, provider.prompt_tokens, provider.cache_read_tokens) == (
        3,
        9_000,
        8_000,
    )
    assert provider.subagent_prompt_tokens == 700
    assert {share.key for share in recorded.categories} <= set(RECORDED_CATEGORIES)
    assert "system_prompt" not in RECORDED_CATEGORIES
