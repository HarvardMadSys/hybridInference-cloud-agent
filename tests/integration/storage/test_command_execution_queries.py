"""Command executions against a real database.

What only Postgres can settle: the JSONB projection reads every adapter's
recorded command shape, ownership is in the query (another owner and a deleted
conversation read as absent), pagination is stable across a page walk, and a
conversation with more command calls than one bounded read covers reports the
total rather than presenting a subset as the whole.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import db as db

from cloud_agent.command_executions import SLOW_COMMAND_MS
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

OWNER = "commands-owner"
OTHER = "commands-other"

_CLONE_THREAD_SQL = """
    INSERT INTO agent_threads
    SELECT (jsonb_populate_record(NULL::agent_threads, to_jsonb(t) || jsonb_build_object(
        'id', $2::text, 'user_id', $3::text, 'title', $4::text))).*
    FROM agent_threads t WHERE t.id = $1
"""

_CLONE_JOB_SQL = """
    INSERT INTO agent_jobs
    SELECT (jsonb_populate_record(NULL::agent_jobs, to_jsonb(j) || jsonb_build_object(
        'id', $2::text, 'thread_id', $3::text, 'user_id', $4::text, 'parent_job_id', $5::text,
        'turn_no', $6::int, 'native_turn_no', $6::int, 'state', 'succeeded',
        'task_prompt', $7::text, 'created_at', NOW(), 'current_attempt_id', NULL,
        'runtime', $8::text, 'model', 'm'))).*
    FROM agent_jobs j WHERE j.id = $1
"""


@pytest_asyncio.fixture
async def store(db):
    return Storage(db)


async def _conversation(
    db,
    store,
    thread_id: str,
    *,
    user_id: str = OWNER,
    turns: int = 1,
    runtime: str = "claude-code",
    state: str = "succeeded",
) -> list[dict]:
    job = await _create_job(store, user_id="commands-template")
    async with db.acquire() as conn:
        await conn.execute(
            _CLONE_THREAD_SQL, job["thread_id"], thread_id, user_id, f"Title {thread_id}"
        )
        parent = None
        created = []
        for turn in range(1, turns + 1):
            job_id = f"{thread_id}-j{turn}"
            await conn.execute(
                _CLONE_JOB_SQL,
                job["id"],
                job_id,
                thread_id,
                user_id,
                parent,
                turn,
                "prompt",
                runtime,
            )
            await conn.execute("UPDATE agent_jobs SET state = $2 WHERE id = $1", job_id, state)
            attempt_id = await conn.fetchval(
                "INSERT INTO agent_attempts (job_id, thread_id, attempt_no, lease_owner,"
                " lease_generation, lease_expires_at, status, started_at, finished_at)"
                " VALUES ($1, $2, 1, 'cmd-test', 1, NOW(), 'finished', NOW(), NOW())"
                " RETURNING id",
                job_id,
                thread_id,
            )
            created.append({"job_id": job_id, "attempt_id": attempt_id})
            parent = job_id
    return created


async def _events(db, job_id: str, attempt_id: int, rows: list[tuple[str, dict]]) -> None:
    async with db.acquire() as conn:
        start = await conn.fetchval(
            "SELECT COALESCE(MAX(seq), 0) FROM agent_job_events WHERE attempt_id = $1",
            attempt_id,
        )
        for offset, (kind, payload) in enumerate(rows, start=1):
            await conn.execute(
                "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)"
                " VALUES ($1, $2, $3, $4, $5::jsonb)",
                job_id,
                attempt_id,
                start + offset,
                kind,
                json.dumps(payload),
            )


def _bash(call_id: str, command: str, **input_extra) -> tuple[str, dict]:
    return (
        "tool_use",
        {"name": "Bash", "id": call_id, "input": {"command": command, **input_extra}},
    )


def _done(call_id: str, content: str = "ok", **extra) -> tuple[str, dict]:
    return ("tool_result", {"tool_use_id": call_id, "content": content, "is_error": False, **extra})


async def test_the_owners_commands_are_assembled_with_summary_and_programs(db, store):
    turns = await _conversation(db, store, "athr_cmd", turns=2)
    await _events(
        db,
        "athr_cmd-j1",
        turns[0]["attempt_id"],
        [
            _bash("c1", "cd /work && git status"),
            ("tool_result", {"tool_use_id": "c1", "content": "clean", "exit_code": 0}),
            _bash("c2", "npm test"),
            ("tool_result", {"tool_use_id": "c2", "content": "1 failed", "exit_code": 1}),
        ],
    )
    result = await store.command_executions.command_executions(job_id="athr_cmd-j1", user_id=OWNER)
    assert result is not None
    assert result["summary"]["total"] == 2
    assert result["summary"]["failed"] == 1
    assert result["summary"]["settled"] == 2
    assert result["coverage"]["turns"] == 2
    assert [program["name"] for program in result["programs"]] == ["git", "npm"]
    assert result["slow_cutoff_ms"] == SLOW_COMMAND_MS


async def test_another_owner_and_a_deleted_conversation_read_as_absent(db, store):
    await _conversation(db, store, "athr_cmd_priv")
    assert (
        await store.command_executions.command_executions(job_id="athr_cmd_priv-j1", user_id=OTHER)
        is None
    )
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_threads SET deleted_at = NOW() WHERE id = 'athr_cmd_priv'")
    assert (
        await store.command_executions.command_executions(job_id="athr_cmd_priv-j1", user_id=OWNER)
        is None
    )


async def test_each_runtimes_recorded_command_shape_is_read(db, store):
    for runtime, top in [
        ("codex", {"name": "commandExecution", "id": "x1", "command": "ls -la"}),
        ("pi", {"name": "bash", "id": "x2", "input": {"command": "pwd"}}),
        ("opencode", {"name": "bash", "id": "x3", "input": {"command": "make"}}),
        ("kilo", {"name": "bash", "id": "x4", "input": {"command": "go test"}}),
    ]:
        [turn] = await _conversation(db, store, f"athr_{runtime}", runtime=runtime)
        await _events(
            db,
            turn["job_id"],
            turn["attempt_id"],
            [
                ("tool_use", top),
                ("tool_result", {"tool_use_id": top["id"], "content": "x", "exit_code": 0}),
            ],
        )
        result = await store.command_executions.command_executions(
            job_id=turn["job_id"], user_id=OWNER
        )
        assert result is not None, runtime
        assert result["summary"]["total"] == 1, runtime
        assert result["executions"][0]["status"] == "completed", runtime


async def test_a_delayed_result_and_polls_are_one_execution(db, store):
    [turn] = await _conversation(db, store, "athr_poll")
    await _events(
        db,
        turn["job_id"],
        turn["attempt_id"],
        [
            (
                "tool_use",
                {
                    "name": "Bash",
                    "id": "c1",
                    "input": {"command": "npm run dev", "run_in_background": True},
                },
            ),
            (
                "tool_result",
                {"tool_use_id": "c1", "content": "started", "status": "running", "shellId": "s1"},
            ),
            ("tool_use", {"name": "BashOutput", "id": "p1", "input": {"session_id": "s1"}}),
            ("tool_result", {"tool_use_id": "p1", "content": "ready", "session_id": "s1"}),
            ("tool_use", {"name": "BashOutput", "id": "p2", "input": {"session_id": "s1"}}),
            ("tool_result", {"tool_use_id": "p2", "content": "listening", "session_id": "s1"}),
        ],
    )
    result = await store.command_executions.command_executions(job_id=turn["job_id"], user_id=OWNER)
    assert result["summary"]["total"] == 1
    assert result["executions"][0]["background"] is True
    assert result["executions"][0]["output_preview"] == "listening"


async def test_a_duplicate_event_row_is_one_execution_and_a_repeat_is_not(db, store):
    [turn] = await _conversation(db, store, "athr_dup")
    await _events(
        db,
        turn["job_id"],
        turn["attempt_id"],
        [
            _bash("c1", "pytest -q"),
            _bash("c1", "pytest -q"),
            _done("c1"),
            _bash("c2", "pytest -q"),
            _done("c2"),
        ],
    )
    result = await store.command_executions.command_executions(job_id=turn["job_id"], user_id=OWNER)
    assert result["summary"]["total"] == 2
    assert result["attention"]["repeats"] == {"command": "pytest -q", "count": 2}


async def test_long_command_and_output_move_bounded(db, store):
    [turn] = await _conversation(db, store, "athr_long")
    command = "grep -n " + "z" * 9_000
    await _events(
        db,
        turn["job_id"],
        turn["attempt_id"],
        [
            _bash("c1", command),
            ("tool_result", {"tool_use_id": "c1", "content": "y" * 9_000, "exit_code": 0}),
        ],
    )
    result = await store.command_executions.command_executions(job_id=turn["job_id"], user_id=OWNER)
    execution = result["executions"][0]
    assert execution["command_chars"] > 8_000
    assert execution["command_truncated"] is True
    assert len(execution["output_preview"]) <= 2_000
    assert execution["output_truncated"] is True


async def test_multiline_heredoc_command_keeps_its_subject(db, store):
    [turn] = await _conversation(db, store, "athr_heredoc")
    await _events(
        db,
        turn["job_id"],
        turn["attempt_id"],
        [
            _bash("c1", "cd /w && cat <<'EOF' > probe.txt\none\ntwo\nEOF"),
            _done("c1"),
        ],
    )
    result = await store.command_executions.command_executions(job_id=turn["job_id"], user_id=OWNER)
    assert result["executions"][0]["program"] == "cat"
    assert "\n" in result["executions"][0]["command"]


async def test_filters_sort_and_search_agree_across_a_page_walk(db, store):
    [turn] = await _conversation(db, store, "athr_page")
    rows: list[tuple[str, dict]] = []
    for index in range(7):
        rows.append(_bash(f"c{index}", f"git log -{index}"))
        rows.append(
            (
                "tool_result",
                {"tool_use_id": f"c{index}", "content": "x", "exit_code": 0 if index != 3 else 1},
            )
        )
    await _events(db, turn["job_id"], turn["attempt_id"], rows)

    asked = await store.command_executions.command_executions(
        job_id=turn["job_id"], user_id=OWNER, sort="latest", limit=3
    )
    seen = [execution["id"] for execution in asked["executions"]]
    cursor = asked["page"]["next_cursor"]
    while cursor:
        nxt = await store.command_executions.command_executions(
            job_id=turn["job_id"], user_id=OWNER, sort="latest", limit=3, cursor=cursor
        )
        seen.extend(execution["id"] for execution in nxt["executions"])
        cursor = nxt["page"]["next_cursor"]
    assert len(seen) == 7 and len(set(seen)) == 7

    failed = await store.command_executions.command_executions(
        job_id=turn["job_id"], user_id=OWNER, filter="failed"
    )
    assert failed["counts"] == {"all": 7, "failed": 1, "slow": 0, "background": 0}
    assert failed["page"]["total"] == 1
    assert failed["summary"]["total"] == 7

    searched = await store.command_executions.command_executions(
        job_id=turn["job_id"], user_id=OWNER, q="log -5"
    )
    assert searched["counts"]["all"] == 1


async def test_a_conversation_over_the_bound_reports_its_true_total(db, store):
    [turn] = await _conversation(db, store, "athr_big")
    rows: list[tuple[str, dict]] = []
    for index in range(60):
        rows.append(_bash(f"c{index}", f"true {index}"))
        rows.append(("tool_result", {"tool_use_id": f"c{index}", "content": "", "exit_code": 0}))
    await _events(db, turn["job_id"], turn["attempt_id"], rows)
    page = await store.command_executions.command_executions(
        job_id=turn["job_id"], user_id=OWNER, limit=10
    )
    assert page["summary"]["total"] == 60
    assert page["coverage"]["executions_total"] == 60
    assert page["coverage"]["truncated"] is False
    assert page["page"]["returned"] == 10
