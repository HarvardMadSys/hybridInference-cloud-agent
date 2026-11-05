"""One request's composition: what was carried, what was not, and why.

Profiles are built by the real reducer from stored event shapes; event rows
are shaped like the composition read's projection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cloud_agent.context_profile import ContextProfileAccumulator
from cloud_agent.context_request import assemble_request, locate_request
from cloud_agent.context_view import assemble_thread_context
from cloud_agent_shared.text_tokens import estimate_text_tokens, split_counts

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _profile(attempt_id: int, job_id: str, rows: list[tuple[int, str, dict[str, Any]]]) -> dict:
    accumulator = ContextProfileAccumulator(attempt_id=attempt_id, job_id=job_id)
    accumulator.extend(
        {
            "id": event_id,
            "seq": seq,
            "event_type": kind,
            "created_at": T0 + timedelta(seconds=seq),
            "payload": payload,
        }
        for seq, (event_id, kind, payload) in enumerate(rows, start=1)
    )
    return accumulator.finish()


def _usage(usage_id: str, cached: int) -> dict:
    return {
        "usage": {
            "input_tokens": 5,
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0,
            "output_tokens": 9,
        },
        "usage_id": usage_id,
    }


def _row(event_id: int, attempt_id: int, seq: int, kind: str, text: str, **extra) -> dict:
    ascii_chars, cjk, other = split_counts(text)
    return {
        "id": event_id,
        "attempt_id": attempt_id,
        "seq": seq,
        "event_type": kind,
        "chars": len(text),
        "ascii": ascii_chars,
        "cjk": cjk,
        "other": other,
        "preview": text[:200],
        "truncated": False,
        **extra,
    }


def _job(job_id: str, turn_no: int, runtime: str = "claude-code", **extra) -> dict:
    return {"job_id": job_id, "turn_no": turn_no, "runtime": runtime, "state": "succeeded", **extra}


def _prompt(job_id: str, turn_no: int, text: str) -> dict:
    ascii_chars, cjk, other = split_counts(text)
    return {
        "job_id": job_id,
        "turn_no": turn_no,
        "chars": len(text),
        "ascii": ascii_chars,
        "cjk": cjk,
        "other": other,
        "preview": text,
    }


def _compose(
    jobs, attempts, event_id, *, events, prompts, usage_id=None, superseded=None, snapshot=None
):
    view = assemble_thread_context(thread_id="th", generated_at=T0, jobs=jobs, attempts=attempts)
    located = locate_request(
        view_points=view.requests, jobs=jobs, attempts=attempts, event_id=event_id
    )
    assert located is not None
    return located, assemble_request(
        thread_id="th",
        located=located,
        usage_id=usage_id,
        prompts=[p for p in prompts if p["job_id"] in located.carried_jobs],
        events=[e for e in events if e["attempt_id"] in located.carried_attempts],
        superseded=superseded or {"entries": 0, "chars": 0},
        snapshot=snapshot,
        events_capped=False,
    )


def _claude_conversation():
    """Turn 1 retried (attempt 1 superseded), turn 2 with a compaction and two requests."""
    turn1_failed = _profile(1, "j1", [(10, "message", {**_usage("m0", 100), "text": "lost"})])
    turn1 = _profile(
        2,
        "j1",
        [
            (20, "thinking", {**_usage("m1", 1000), "text": "思考 code"}),
            (21, "tool_use", {**_usage("m1", 1000), "name": "Read", "id": "toolu_1"}),
            (22, "tool_result", {"tool_use_id": "toolu_1"}),
            (23, "message", {**_usage("m2", 3000), "text": "done"}),
        ],
    )
    turn2 = _profile(
        3,
        "j2",
        [
            # The turn's start-up line: the tool menu Claude offers, no sizes.
            (
                29,
                "lifecycle",
                {"phase": "init", "snapshot": {"source": "claude_init", "status": "ok"}},
            ),
            (
                30,
                "lifecycle",
                {
                    "phase": "compact_boundary",
                    "compaction": {"trigger": "auto", "pre_tokens": 3100},
                },
            ),
            (31, "lifecycle", {"phase": "compaction_summary", "text": "摘要", "chars": 2}),
            (32, "message", {**_usage("m3", 800), "text": "next"}),
            (33, "tool_use", {**_usage("m3", 800), "name": "Bash", "id": "toolu_2"}),
            (
                34,
                "message",
                {"text": "subagent says", "parent_tool_use_id": "toolu_2", **_usage("ms", 99)},
            ),
            (35, "tool_result", {"tool_use_id": "toolu_2"}),
            (36, "message", {**_usage("m4", 1200), "text": "final"}),
        ],
    )
    jobs = [_job("j1", 1), _job("j2", 2)]
    attempts = [
        {"attempt_id": 1, "job_id": "j1", "profile": turn1_failed},
        {"attempt_id": 2, "job_id": "j1", "profile": turn1},
        {"attempt_id": 3, "job_id": "j2", "profile": turn2},
    ]
    events = [
        _row(10, 1, 1, "message", "lost"),
        _row(20, 2, 1, "thinking", "思考 code", usage_id="m1"),
        _row(21, 2, 2, "tool_use", "Read {}", name="Read", call_id="toolu_1", usage_id="m1"),
        _row(
            22,
            2,
            3,
            "tool_result",
            "def f():\n    return 1\n" * 30,
            tool_use_id="toolu_1",
            truncated=True,
        ),
        _row(23, 2, 4, "message", "done", usage_id="m2"),
        _row(30, 3, 1, "lifecycle", "", phase="compact_boundary"),
        _row(31, 3, 2, "lifecycle", "摘要", phase="compaction_summary"),
        _row(32, 3, 3, "message", "next", usage_id="m3"),
        _row(33, 3, 4, "tool_use", "Bash ls", name="Bash", call_id="toolu_2", usage_id="m3"),
        _row(34, 3, 5, "message", "subagent says", subagent=True, usage_id="ms"),
        _row(35, 3, 6, "tool_result", "ls output 中文", tool_use_id="toolu_2"),
    ]
    prompts = [_prompt("j1", 1, "修复 bug"), _prompt("j2", 2, "继续 continue")]
    return jobs, attempts, events, prompts


def test_a_request_after_a_compaction_carries_only_what_followed_it():
    jobs, attempts, events, prompts = _claude_conversation()
    # The request reported on event 36 (its first block), last of turn 2.
    located, composition = _compose(
        jobs,
        attempts,
        36,
        events=[e for e in events if not (e["attempt_id"] == 3 and e["seq"] >= 7)],
        prompts=prompts,
        usage_id="m4",
        superseded={"entries": 1, "chars": 4},
    )
    rebuilt = composition.reconstructed
    categories = {c.key: c for c in rebuilt.categories}
    # Turn 1 and turn 2's prompt went in before the compaction.
    assert rebuilt.excluded["compacted"].entries == 6  # 2 prompts + 4 turn-1 events
    assert rebuilt.boundary is not None and rebuilt.boundary.turn_no == 2
    assert rebuilt.boundary.may_preserve_recent is True
    assert categories["compaction_summary"].entries == 1
    assert categories["tool_results"].entries == 1
    assert rebuilt.excluded["subagent"].entries == 1
    assert rebuilt.excluded["superseded_attempts"].entries == 1
    labels = {(e.category, e.label) for e in rebuilt.entries}
    assert ("tool_results", "Bash") in labels and ("tool_calls", "Bash") in labels
    chinese = next(e for e in rebuilt.entries if e.category == "tool_results")
    assert chinese.tokens == estimate_text_tokens("ls output 中文")
    # The turn's start-up read (event 29) came before this request, before
    # the turn's earlier request on event 32 and before its compaction.
    assert composition.native_status == "ok"
    assert located.snapshot_event_id == 29 and located.requests_before == 1
    assert located.compacted_since is True


def test_an_earlier_request_carries_the_retried_turn_once_and_says_snapshot_came_later():
    jobs, attempts, events, prompts = _claude_conversation()
    # The request reported on event 20: turn 1's first request, on attempt 2.
    located, composition = _compose(
        jobs,
        attempts,
        20,
        events=[e for e in events if e["attempt_id"] != 2 or e["seq"] < 1],
        prompts=prompts,
        usage_id="m1",
        superseded={"entries": 1, "chars": 4},
    )
    rebuilt = composition.reconstructed
    assert [e.label for e in rebuilt.entries] == ["prompt"]
    assert rebuilt.entries[0].tokens == estimate_text_tokens("修复 bug")
    assert located.carried_jobs == ["j1"] and 1 not in located.carried_attempts
    assert rebuilt.excluded["superseded_attempts"].entries == 1
    assert composition.native_status == "not_collected"


def test_a_missing_snapshot_says_why_the_cli_was_not_asked():
    jobs, attempts, events, prompts = _claude_conversation()
    # Event 10 is the retried attempt's own request: it never settled.
    located, composition = _compose(jobs, attempts, 10, events=events, prompts=prompts)
    assert located.retried is True
    assert located.carried_attempts == {1: ("j1", 1, 0)}
    assert composition.native_status == "not_settled"
    # A running turn has not been asked yet; a failed one never will be.
    for state, expected in (("running", "pending"), ("failed", "not_settled")):
        changed = [_job("j1", 1, state=state), jobs[1]]
        _located, composition = _compose(changed, attempts, 20, events=events, prompts=prompts)
        assert composition.native_status == expected


def test_claude_offers_names_from_its_start_up_line_and_no_sizes():
    jobs, attempts, events, prompts = _claude_conversation()
    _located, composition = _compose(
        jobs,
        attempts,
        32,
        events=[e for e in events if not (e["attempt_id"] == 3 and e["seq"] >= 3)],
        prompts=prompts,
        usage_id="m3",
        snapshot={
            "source": "claude_init",
            "harness": {
                "tools": ["Bash", "Read", "mcp__github__search_code"],
                "mcp_servers": [{"name": "github", "status": "connected"}],
                "permission_mode": "bypassPermissions",
            },
        },
    )
    native = composition.native
    assert native is not None and composition.native_status == "ok"
    assert native["token_basis"] == "names_only" and native["total_tokens"] is None
    categories = {c["key"]: c for c in native["categories"]}
    assert categories["tool_definitions"] == {
        "key": "tool_definitions",
        "tokens": None,
        "entries": 2,
        "exposed": True,
    }
    assert categories["mcp_tools"]["entries"] == 1
    # Never a zero: what the line does not report is not exposed.
    assert categories["system_prompt"]["exposed"] is False
    mcp = next(e for e in native["entries"] if e["category"] == "mcp_tools")
    assert mcp == {
        "category": "mcp_tools",
        "label": "mcp__github__search_code",
        "tokens": None,
        "token_basis": "names_only",
        "source": "github",
    }
    assert native["mcp_servers"] == [{"name": "github", "status": "connected"}]
    relation = composition.native_relation
    assert relation is not None
    assert (relation.event_id, relation.requests_before, relation.compacted_since) == (29, 0, True)


def test_no_read_is_made_of_pi_and_a_bridge_read_belongs_to_the_turns_first_request():
    jobs, attempts, events, prompts = _claude_conversation()
    pi_jobs = [_job("j1", 1, runtime="pi"), _job("j2", 2, runtime="pi")]
    _located, composition = _compose(pi_jobs, attempts, 20, events=events, prompts=prompts)
    assert composition.native_status == "not_offered"

    profile = _profile(
        1,
        "j1",
        [
            (
                9,
                "lifecycle",
                {
                    "phase": "context_snapshot",
                    "snapshot": {"source": "opencode_session_messages", "status": "ok"},
                },
            ),
            (10, "message", {**_usage("s1", 400), "text": "a"}),
            (11, "message", {**_usage("s2", 600), "text": "b"}),
        ],
    )
    jobs = [_job("j1", 1, runtime="opencode")]
    attempts = [{"attempt_id": 1, "job_id": "j1", "profile": profile}]
    first, _composition = _compose(jobs, attempts, 10, events=[], prompts=[])
    later, _composition = _compose(jobs, attempts, 11, events=[], prompts=[])
    assert (first.snapshot_event_id, first.requests_before) == (9, 0)
    assert (later.snapshot_event_id, later.requests_before) == (9, 1)
    assert first.compacted_since is later.compacted_since is False
    # Without a read, a running turn's is still to come; an ended one has none.
    bare = _profile(1, "j1", [(10, "message", {**_usage("s1", 400), "text": "a"})])
    attempts = [{"attempt_id": 1, "job_id": "j1", "profile": bare}]
    for state, expected in (("running", "pending"), ("succeeded", "not_collected")):
        _located, composition = _compose(
            [_job("j1", 1, runtime="opencode", state=state)], attempts, 10, events=[], prompts=[]
        )
        assert composition.native_status == expected


def test_pi_request_drops_its_own_reply_matched_by_response_id():
    profile = _profile(
        1,
        "j1",
        [
            (
                10,
                "usage",
                {
                    "input_tokens": 50,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "output_tokens": 5,
                },
            ),
            (11, "message", {"text": "a1", "usage_id": "r2"}),
            (
                12,
                "usage",
                {
                    "input_tokens": 90,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "output_tokens": 5,
                    "usage_id": "r2",
                },
            ),
        ],
    )
    jobs = [_job("j1", 1, runtime="pi")]
    attempts = [{"attempt_id": 1, "job_id": "j1", "profile": profile}]
    events = [_row(11, 1, 2, "message", "a1", usage_id="r2")]
    _located, composition = _compose(
        jobs, attempts, 12, events=events, prompts=[_prompt("j1", 1, "hi")], usage_id="r2"
    )
    rebuilt = composition.reconstructed
    assert rebuilt.excluded["this_request_output"].entries == 1
    assert [e.category for e in rebuilt.entries] == ["user_messages"]


def _codex_usage(total: int, size: int) -> dict:
    return {
        "notification": {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "t",
                "tokenUsage": {
                    "total": {"totalTokens": total},
                    "last": {"inputTokens": size, "cachedInputTokens": 0, "outputTokens": 1},
                },
            },
        }
    }


def test_codex_output_between_reports_is_the_requests_own_but_tool_results_are_input():
    profile = _profile(
        1,
        "j1",
        [
            (10, "message", {"text": "first reply"}),
            (11, "raw", _codex_usage(100, 90)),
            (12, "tool_result", {"tool_use_id": "c1"}),
            (13, "message", {"text": "second reply"}),
            (14, "raw", _codex_usage(300, 190)),
        ],
    )
    jobs = [_job("j1", 1, runtime="codex")]
    attempts = [{"attempt_id": 1, "job_id": "j1", "profile": profile}]
    events = [
        _row(10, 1, 1, "message", "first reply"),
        _row(12, 1, 3, "tool_result", "result body", tool_use_id="c1"),
        _row(13, 1, 4, "message", "second reply"),
    ]
    _located, composition = _compose(
        jobs, attempts, 14, events=events, prompts=[_prompt("j1", 1, "go")]
    )
    rebuilt = composition.reconstructed
    categories = {c.key: c.entries for c in rebuilt.categories}
    assert categories == {"user_messages": 1, "assistant": 1, "tool_results": 1}
    assert rebuilt.excluded["this_request_output"].entries == 1
    assert composition.native_status == "not_offered"


def test_copied_fork_turns_are_counted_not_reconstructed_and_clipped_previews_are_marked():
    profile = _profile(5, "j2", [(50, "message", {**_usage("m9", 10), "text": "x"})])
    jobs = [_job("j1", 1, copied=True), _job("j2", 2)]
    attempts = [{"attempt_id": 5, "job_id": "j2", "profile": profile}]
    clipped = _row(49, 5, 0, "tool_result", "partial", tool_use_id="t", stored_chars=10_000)
    _located, composition = _compose(
        jobs, attempts, 50, events=[clipped], prompts=[_prompt("j2", 2, "go")], usage_id="m9"
    )
    rebuilt = composition.reconstructed
    assert rebuilt.copied_turns == 1
    result = next(e for e in rebuilt.entries if e.category == "tool_results")
    assert result.truncated is True
    assert {c.key: c.truncated_entries for c in rebuilt.categories}["tool_results"] == 1


def test_an_unknown_request_is_not_found():
    jobs, attempts, _events, _prompts = _claude_conversation()
    view = assemble_thread_context(thread_id="th", generated_at=T0, jobs=jobs, attempts=attempts)
    assert (
        locate_request(view_points=view.requests, jobs=jobs, attempts=attempts, event_id=99_999)
        is None
    )
