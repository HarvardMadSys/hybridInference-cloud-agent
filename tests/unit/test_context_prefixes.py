"""The request meter's prefixes in the Context dashboard: the profile, the session, a request."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cloud_agent.context_dashboard import session_tokens
from cloud_agent.context_profile import ContextProfileAccumulator, main_prefix
from cloud_agent.context_request import assemble_request, locate_request
from cloud_agent.context_view import assemble_thread_context
from cloud_agent_shared.request_prefix import measure

T0 = datetime(2026, 11, 5, 9, tzinfo=UTC)


def _prefix(system: str, tools: int) -> dict[str, Any]:
    body = {"system": system, "tools": [{"name": f"tool{i}", "x": "y" * 40} for i in range(tools)]}
    record = measure("anthropic_messages", body, mode="passthrough")
    assert record is not None
    return {key: value for key, value in record.items() if key != "v"}


def _usage(usage_id: str) -> dict[str, Any]:
    return {
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 0, "output_tokens": 1},
        "usage_id": usage_id,
        "chars": 4,
        "ascii": 4,
        "cjk": 0,
    }


def _profile(*rows: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    accumulator = ContextProfileAccumulator(attempt_id=1, job_id="j1")
    accumulator.extend(
        {
            "id": 100 + seq,
            "seq": seq,
            "event_type": kind,
            "created_at": T0 + timedelta(seconds=seq),
            "payload": payload,
        }
        for seq, (kind, payload) in enumerate(rows, start=1)
    )
    return accumulator.finish()


def _reported(prefix: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return ("lifecycle", {"phase": "request_prefix", "prefix": prefix})


def test_an_attempt_keeps_each_prefix_once_and_its_agents_is_the_first_with_tools():
    title = _prefix("Write a title", 0)
    agent = _prefix("You are a coding agent." * 10, 3)
    delegated = _prefix("You explore.", 1)
    profile = _profile(
        _reported(title),
        _reported(agent),
        ("message", _usage("m1")),
        _reported(delegated),
        _reported(agent),
    )
    assert [p["fingerprint"] for p in profile["prefixes"]] == [
        title["fp"],
        agent["fp"],
        delegated["fp"],
    ]
    main = main_prefix(profile)
    assert main is not None and main["fingerprint"] == agent["fp"]
    assert main["tool_count"] == 3 and main["system_chars"] == len("You are a coding agent.") * 10
    assert main["system_tokens"] > 0 and main["tool_tokens"] > 0
    assert main["event_id"] == 102


def test_a_malformed_report_is_not_a_prefix():
    bad = {**_prefix("s", 1), "system": {"chars": -1, "ascii": 0, "cjk": 0}}
    profile = _profile(_reported(bad), ("lifecycle", {"phase": "request_prefix", "prefix": "x"}))
    assert profile["prefixes"] == [] and main_prefix(profile) is None


def _job(job_id: str, turn_no: int) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "turn_no": turn_no,
        "created_at": T0 + timedelta(hours=turn_no),
        "runtime": "claude-code",
        "model": "m",
        "state": "succeeded",
        "prompt_chars": 100,
        "prompt_tokens": 25,
    }


def test_the_session_counts_the_latest_measured_turns_prefix_once():
    older = _profile(_reported(_prefix("old", 1)), ("message", _usage("m1")))
    newer = _profile(_reported(_prefix("new system prompt", 2)), ("message", _usage("m2")))
    unmeasured = _profile(("message", _usage("m3")))
    tokens = session_tokens(
        [_job("j1", 1), _job("j2", 2), _job("j3", 3)],
        {
            "j1": [{"attempt_id": 1, "job_id": "j1", "profile": older}],
            "j2": [{"attempt_id": 2, "job_id": "j2", "profile": newer}],
            "j3": [{"attempt_id": 3, "job_id": "j3", "profile": unmeasured}],
        },
    )
    shares = {share.key: share for share in tokens.recorded}
    measured = main_prefix(newer)
    assert measured is not None
    assert shares["system_prompt"].tokens == measured["system_tokens"]
    assert shares["tool_schemas"].tokens == measured["tool_tokens"]
    assert shares["tool_schemas"].items == 2
    assert [share.key for share in tokens.recorded][:2] == ["system_prompt", "tool_schemas"]
    assert tokens.not_recorded == []
    assert tokens.recorded_total == sum(share.tokens for share in tokens.recorded)


def test_a_session_nothing_measured_still_says_so():
    tokens = session_tokens(
        [_job("j1", 1)],
        {"j1": [{"attempt_id": 1, "job_id": "j1", "profile": _profile(("message", _usage("m")))}]},
    )
    assert tokens.not_recorded == ["system_prompt", "tool_schemas"]
    assert {share.key for share in tokens.recorded}.isdisjoint(tokens.not_recorded)


def test_a_request_shows_its_attempts_prefix_with_its_tools_by_size():
    agent = _prefix("You are a coding agent.", 3)
    profile = _profile(
        _reported(agent),
        ("message", _usage("m1")),
        _reported(_prefix("You explore.", 1)),
    )
    jobs = [_job("j1", 1)]
    attempts = [{"attempt_id": 1, "job_id": "j1", "profile": profile}]
    view = assemble_thread_context(thread_id="th", generated_at=T0, jobs=jobs, attempts=attempts)
    located = locate_request(
        view_points=view.requests, jobs=jobs, attempts=attempts, event_id=view.requests[0].event_id
    )
    assert located is not None
    composition = assemble_request(
        thread_id="th",
        located=located,
        usage_id="m1",
        prompts=[],
        events=[],
        superseded={"entries": 0, "chars": 0},
        snapshot=None,
        events_capped=False,
        prefix_record=agent,
    )
    prefix = composition.prefix
    assert prefix is not None
    assert prefix.event_id == 101 and prefix.tool_count == 3 and prefix.other_prefixes == 1
    assert [tool.label for tool in prefix.tools] == ["tool0", "tool1", "tool2"]
    assert all(tool.tokens > 0 for tool in prefix.tools) and prefix.tools_omitted == 0
    # Without the stored record, the sizes still show; the names are not listed.
    unnamed = assemble_request(
        thread_id="th",
        located=located,
        usage_id="m1",
        prompts=[],
        events=[],
        superseded={"entries": 0, "chars": 0},
        snapshot=None,
        events_capped=False,
    ).prefix
    assert unnamed is not None and unnamed.tools == [] and unnamed.tools_omitted == 3
