"""Context profiles and owner usage ranges, against a real database.

What only Postgres can settle:

- **Ownership is in the query.** A conversation that is not the caller's, or
  is deleted, reads as absent; the insights list only the caller's runs.
- **The projection reads what the reducer needs from real JSONB**, including
  Codex notifications and the character counts of large tool results, and the
  derived profile is cached and invalidated by the ledger's sequence.
- **All is all.** An owner with more runs than one bounded selection may hold
  is summed in batches, every run counted, and both halves of the page and the
  insights share one interval.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import db as db
from tests.unit.test_ledger_snapshots import report_payload

from cloud_agent import identity
from cloud_agent.admin_usage import MAX_USAGE_RUNS
from cloud_agent.api import context as context_api, owner_usage as owner_usage_api
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.context_insights import ContextInsightsCursor, context_cursor_scope
from cloud_agent.context_profile import CONTEXT_PROFILE_VERSION
from cloud_agent.storage.assembly import Storage
from cloud_agent.usage_batches import owner_usage
from cloud_agent_shared.text_tokens import estimate_text_tokens

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

OWNER = "context-owner"
OTHER = "context-other"
BASE = datetime(2026, 1, 5, 9, tzinfo=UTC)


@pytest_asyncio.fixture
async def store(db):
    _TEMPLATE.clear()
    return Storage(db)


_TEMPLATE: dict[str, str] = {}


async def _template(db, store) -> tuple[str, str]:
    """A conversation the store created, whose rows the seeds below copy.

    Copying a real row keeps every column the schema requires without this
    test restating them; each copy overrides only its identity and history.
    """
    if not _TEMPLATE:
        job = await _create_job(store, user_id="context-template")
        _TEMPLATE.update(thread=job["thread_id"], job=job["id"])
    return _TEMPLATE["thread"], _TEMPLATE["job"]


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
        'task_prompt', $7::text, 'created_at', $8::timestamptz, 'current_attempt_id', NULL,
        'runtime', $9::text, 'model', $10::text))).*
    FROM agent_jobs j WHERE j.id = $1
"""


async def _thread(
    db,
    store,
    thread_id: str,
    *,
    user_id: str = OWNER,
    turns: int = 1,
    at: datetime = BASE,
) -> list[str]:
    """One conversation with ``turns`` turns, each one finished attempt."""
    template_thread, template_job = await _template(db, store)
    job_ids = []
    async with db.acquire() as conn:
        await conn.execute(
            _CLONE_THREAD_SQL, template_thread, thread_id, user_id, f"Title of {thread_id}"
        )
        parent = None
        for turn in range(1, turns + 1):
            job_id = f"{thread_id}-j{turn}"
            await conn.execute(
                _CLONE_JOB_SQL,
                template_job,
                job_id,
                thread_id,
                user_id,
                parent,
                turn,
                "p" * 400,
                at + timedelta(minutes=turn),
                "claude-code",
                "m",
            )
            await conn.execute(
                """
                INSERT INTO agent_attempts (job_id, thread_id, attempt_no, lease_owner,
                    lease_generation, lease_expires_at, status, started_at, finished_at)
                VALUES ($1, $2, 1, 'ctx-test', 1, NOW(), 'finished', $3, $3)
                """,
                job_id,
                thread_id,
                at + timedelta(minutes=turn),
            )
            parent = job_id
            job_ids.append(job_id)
    return job_ids


async def _events(db, job_id: str, rows: list[tuple[str, dict]]) -> None:
    async with db.acquire() as conn:
        attempt_id, start = await conn.fetchrow(
            "SELECT a.id, COALESCE(MAX(e.seq), 0) FROM agent_attempts a"
            " LEFT JOIN agent_job_events e ON e.attempt_id = a.id"
            " WHERE a.job_id = $1 GROUP BY a.id",
            job_id,
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


def _claude(usage_id: str, cached: int, **extra) -> tuple[str, dict]:
    return (
        "message",
        {
            "text": "answer",
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": 0,
                "output_tokens": 5,
            },
            "usage_id": usage_id,
            **extra,
        },
    )


async def test_thread_context_is_the_owners_and_reads_real_payloads(db, store):
    [first, second] = await _thread(db, store, "athr_ctx", turns=2)
    await _events(
        db,
        first,
        [
            ("lifecycle", {"phase": "init", "model": "m"}),
            _claude("msg_a", 1000),
            ("tool_result", {"tool_use_id": "t", "content": "x" * 100_000}),
            _claude("msg_a", 1000),
            _claude("msg_b", 30_000),
            ("lifecycle", {"phase": "result", "context_windows": {"m": 200_000}}),
        ],
    )
    await _events(
        db,
        second,
        [
            (
                "lifecycle",
                {
                    "phase": "compact_boundary",
                    "compaction": {"trigger": "auto", "pre_tokens": 30_020},
                },
            ),
            _claude("msg_c", 4_000),
            (
                "raw",
                {
                    "protocol": "codex-app-server",
                    "text": "{}",
                    "notification": {"method": "item/agentMessage/delta", "params": {"delta": "x"}},
                },
            ),
        ],
    )
    other_thread = await _thread(db, store, "athr_stranger", user_id=OTHER)

    assert await store.context_queries.thread_context(job_id=other_thread[0], user_id=OWNER) is None
    result = await store.context_queries.thread_context(job_id=second, user_id=OWNER)
    assert result is not None and result["thread_id"] == "athr_ctx"
    [p1, p2] = [attempt["profile"] for attempt in result["attempts"]]
    assert [point[3] for point in p1["requests"]] == [1010, 30_010]
    assert p1["summary"]["context_window"] == 200_000
    # The 100,000-character result moved as its length.
    assert p1["content"]["total"]["tool_results"] == 100_000
    assert p2["compactions"][0]["pre_tokens"] == 30_020
    assert [job["prompt_chars"] for job in result["jobs"]] == [400, 400]

    async with db.acquire() as conn:
        stored = await conn.fetch(
            "SELECT attempt_id, event_seq, reducer_version, main_requests, peak_prompt_tokens,"
            " compactions FROM agent_attempt_context_profiles ORDER BY attempt_id"
        )
    assert [(row["main_requests"], row["peak_prompt_tokens"]) for row in stored] == [
        (2, 30_010),
        (1, 4_010),
    ]
    assert {row["reducer_version"] for row in stored} == {CONTEXT_PROFILE_VERSION}

    # A new event invalidates only its attempt's profile.
    await _events(db, second, [_claude("msg_d", 9_000)])
    again = await store.context_queries.thread_context(job_id=first, user_id=OWNER)
    assert again is not None
    assert again["attempts"][1]["profile"]["summary"]["main_requests"] == 2

    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_threads SET deleted_at = NOW() WHERE id = 'athr_ctx'")
    assert await store.context_queries.thread_context(job_id=first, user_id=OWNER) is None


async def test_insights_rank_page_and_report_unprofiled_attempts(db, store):
    for index, peak in enumerate([5_000, 90_000, 40_000]):
        [job] = await _thread(db, store, f"athr_r{index}", at=BASE + timedelta(hours=index))
        await _events(db, job, [_claude(f"m{index}", peak)])
    [stranger] = await _thread(db, store, "athr_x", user_id=OTHER)
    await _events(db, stranger, [_claude("mx", 999_999)])
    since, until = BASE - timedelta(days=1), BASE + timedelta(days=1)

    first = await store.context_queries.insights_page(
        user_id=OWNER,
        since=since,
        until=until,
        sort="peak",
        cursor=None,
        limit=2,
        refresh_attempts=1,
        refresh_events=10_000,
        pending_cap=100,
    )
    # One attempt profiled this read; two still pending, and said so.
    assert first["refreshed"] == 1 and first["pending"] == 2

    drained = None
    for _ in range(3):
        drained = await store.context_queries.insights_page(
            user_id=OWNER,
            since=since,
            until=until,
            sort="peak",
            cursor=None,
            limit=2,
            refresh_attempts=10,
            refresh_events=10_000,
            pending_cap=100,
        )
    assert drained is not None and drained["pending"] == 0
    page = drained["page"]
    assert [row["thread_id"] for row in page] == ["athr_r1", "athr_r2", "athr_r0"]
    assert page[0]["peak_prompt_tokens"] == 90_010
    summary = drained["summary"]
    assert summary["conversations"] == 3 and summary["peak_thread_id"] == "athr_r1"
    assert summary["peak_prompt_tokens"] == 90_010  # the stranger's 999,999 is not here

    last = page[1]
    after = ContextInsightsCursor(
        scope=context_cursor_scope("peak", "7d"),
        since=since,
        until=until,
        number=int(last["sort_number"]),
        at=last["last_turn_at"],
        thread_id=last["thread_id"],
    )
    rest = await store.context_queries.insights_page(
        user_id=OWNER,
        since=since,
        until=until,
        sort="peak",
        cursor=after,
        limit=2,
        refresh_attempts=10,
        refresh_events=10_000,
        pending_cap=100,
    )
    assert [row["thread_id"] for row in rest["page"]] == ["athr_r0"]


async def _bulk_history(db, store, *, user_id: str, runs: int, span_days: int) -> Decimal:
    """``runs`` one-turn conversations spread over ``span_days``; their exact cost."""
    template_thread, template_job = await _template(db, store)
    payload = report_payload()
    payload["totals"] = {**payload["totals"], "spent_usd": "0.00000123"}
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_threads
            SELECT (jsonb_populate_record(NULL::agent_threads, to_jsonb(t) || jsonb_build_object(
                'id', $2 || '-t' || n, 'user_id', $2, 'title', 'bulk'))).*
            FROM agent_threads t, generate_series(1, $3) n WHERE t.id = $1
            """,
            template_thread,
            user_id,
            runs,
        )
        await conn.execute(
            """
            INSERT INTO agent_jobs
            SELECT (jsonb_populate_record(NULL::agent_jobs, to_jsonb(j) || jsonb_build_object(
                'id', $2 || '-j' || n, 'thread_id', $2 || '-t' || n, 'user_id', $2,
                'turn_no', 1, 'native_turn_no', 1, 'parent_job_id', NULL, 'state', 'succeeded',
                'current_attempt_id', NULL, 'runtime', 'codex', 'model', 'model-' || (n % 3),
                'created_at', $4::timestamptz + (n * ($5::double precision / $3)) * INTERVAL '1 day'
            ))).*
            FROM agent_jobs j, generate_series(1, $3) n WHERE j.id = $1
            """,
            template_job,
            user_id,
            runs,
            datetime.now(UTC) - timedelta(days=span_days + 5),
            float(span_days),
        )
        await conn.execute(
            """
            INSERT INTO agent_attempts (job_id, thread_id, attempt_no, lease_owner,
                lease_generation, lease_expires_at, status, started_at, finished_at)
            SELECT j.id, j.thread_id, 1, 'bulk', 1, NOW(), 'finished', j.created_at, j.created_at
            FROM agent_jobs j WHERE j.user_id = $1
            """,
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO agent_attempt_ledger (attempt_id, grant_id, residency_generation,
                binding_id, window_since, snapshot, queried_at, terminal_refresh_count)
            SELECT a.id, 'agr_actual', 1, 'binding', a.started_at, $2::jsonb, NOW(), 1
            FROM agent_attempts a JOIN agent_jobs j ON j.id = a.job_id WHERE j.user_id = $1
            """,
            user_id,
            json.dumps(payload),
        )
    return Decimal("0.00000123") * runs


async def test_all_sums_every_run_beyond_one_selections_bound(db, store, monkeypatch):
    runs = MAX_USAGE_RUNS + 250
    expected = await _bulk_history(db, store, user_id=OWNER, runs=runs, span_days=400)
    await _bulk_history(db, store, user_id=OTHER, runs=40, span_days=10)
    first = await store.owner_usage.first_record_at(OWNER)
    assert first is not None and first < BASE + timedelta(days=1)

    since = first.replace(hour=0, minute=0, second=0, microsecond=0)
    usage = await owner_usage(
        store.admin_usage, user_id=OWNER, since=since, until=datetime.now(UTC), size="week"
    )
    assert usage.batches >= 2
    assert usage.summary.run_count == runs
    assert usage.summary.totals is not None
    assert Decimal(usage.summary.totals.spent_usd) == expected
    assert sum(bucket.usage.run_count for bucket in usage.buckets) == runs

    # The same through the routes, with the range the page sends.
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "context-integration-session-secret-long")
    app = FastAPI()
    app.include_router(owner_usage_api.router)
    app.include_router(context_api.router)

    class _Gateway:
        async def agent_access(self, owner):
            return {"user_id": owner, "allowed": True, "permissions": ["agent.use"]}

    app.dependency_overrides[get_gateway_client] = lambda: _Gateway()
    app.dependency_overrides[get_storage] = lambda: store
    headers = {"Authorization": f"Bearer {identity.mint_session(user_id=OWNER)}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        tokens = (
            await http.get("/v1/agent/usage/tokens", params={"range": "all"}, headers=headers)
        ).json()
        resources = (
            await http.get("/v1/agent/usage/resources", params={"range": "all"}, headers=headers)
        ).json()
        insights = (
            await http.get("/v1/agent/usage/context", params={"range": "all"}, headers=headers)
        ).json()
    assert tokens["summary"]["run_count"] == runs
    assert Decimal(tokens["summary"]["totals"]["spent_usd"]) == expected
    assert tokens["period"]["bucket"] == "week" and tokens["batches"] >= 2
    assert tokens["period"]["since"] == resources["period"]["since"] == insights["period"]["since"]
    assert insights["summary"]["conversations"] == runs
    assert len(insights["conversations"]) == 25 and insights["next_cursor"]
    # Each listed conversation's cost is the server's own sum of its runs.
    for conversation in insights["conversations"]:
        assert conversation["usage"]["run_count"] == 1
        assert Decimal(conversation["usage"]["totals"]["spent_usd"]) == Decimal("0.00000123")
    # No events: nothing to read, nothing presented as zero.
    assert all(c["peak_prompt_tokens"] is None for c in insights["conversations"])
    assert all(c["attempts_profiled"] == c["attempts"] == 1 for c in insights["conversations"])
    assert insights["profiling"]["complete"] is True


async def _event_id(db, job_id: str, seq: int) -> int:
    async with db.acquire() as conn:
        return await conn.fetchval(
            "SELECT e.id FROM agent_job_events e JOIN agent_attempts a ON a.id = e.attempt_id"
            " WHERE a.job_id = $1 AND e.seq = $2",
            job_id,
            seq,
        )


async def test_request_composition_reads_carried_content_and_the_snapshot(db, store):
    [first, second] = await _thread(db, store, "athr_req", turns=2)
    big = "def f():\n    return '中文'\n" * 400
    await _events(
        db,
        first,
        [
            _claude("msg_1", 1000),
            ("tool_use", {"name": "Read", "id": "toolu_1", "input": {"file_path": "a.py"}}),
            ("tool_result", {"tool_use_id": "toolu_1", "content": big}),
            _claude("msg_1b", 2000),
        ],
    )
    await _events(
        db,
        second,
        [
            ("lifecycle", {"phase": "compact_boundary", "compaction": {"trigger": "auto"}}),
            ("lifecycle", {"phase": "compaction_summary", "text": "摘要 summary", "chars": 10}),
            _claude("msg_2", 5000),
            ("message", {"text": "helper", "parent_tool_use_id": "toolu_x"}),
            _claude("msg_3", 6000),
            (
                "lifecycle",
                {
                    "phase": "context_snapshot",
                    "snapshot": {
                        "v": 1,
                        "source": "claude_get_context_usage",
                        "status": "ok",
                        "total_tokens": 6100,
                        "categories": [{"key": "system_prompt", "tokens": 4000, "exposed": True}],
                        "entries": [
                            {"category": "instructions", "label": "/state/CLAUDE.md", "tokens": 30}
                        ],
                    },
                },
            ),
        ],
    )
    [stranger] = await _thread(db, store, "athr_req_x", user_id=OTHER)
    await _events(db, stranger, [_claude("msg_x", 1)])

    request_event = await _event_id(db, second, 5)
    composition = await store.context_queries.request_composition(
        job_id=first, user_id=OWNER, event_id=request_event
    )
    assert composition is not None
    rebuilt = composition.reconstructed
    categories = {c.key: c for c in rebuilt.categories}
    # Turn 1 and both prompts went in before the compaction.
    assert rebuilt.excluded["compacted"].entries == 6
    assert categories["compaction_summary"].tokens == 2 + 2  # 摘要 + " summary"
    assert rebuilt.excluded["subagent"].entries == 1
    # The request's own block (seq 5) is not its input; the earlier reply is.
    assert categories["assistant"].entries == 1
    assert composition.native is not None
    assert composition.native["entries"][0]["label"] == "/state/CLAUDE.md"
    assert (
        composition.native_relation is not None and composition.native_relation.requests_after == 0
    )

    earlier = await store.context_queries.request_composition(
        job_id=first, user_id=OWNER, event_id=await _event_id(db, first, 1)
    )
    assert earlier is not None
    assert earlier.native is None and earlier.native_status == "not_collected"
    assert [e.label for e in earlier.reconstructed.entries] == ["prompt"]

    # The CJK-aware estimate is computed from Postgres's own counts.
    middle = await store.context_queries.request_composition(
        job_id=first, user_id=OWNER, event_id=await _event_id(db, second, 3)
    )
    assert middle is not None and middle.native_relation is not None
    assert middle.native_relation.requests_after == 1
    whole = await store.context_queries.request_composition(
        job_id=first, user_id=OWNER, event_id=await _event_id(db, first, 4)
    )
    assert whole is not None
    big_entry = next(e for e in whole.reconstructed.entries if e.category == "tool_results")
    assert big_entry.chars == len(big)
    assert big_entry.tokens == estimate_text_tokens(big)

    # Stranger and wrong-conversation reads answer None.
    assert (
        await store.context_queries.request_composition(
            job_id=stranger, user_id=OWNER, event_id=request_event
        )
        is None
    )
    assert (
        await store.context_queries.request_composition(
            job_id=first, user_id=OTHER, event_id=request_event
        )
        is None
    )


async def test_event_content_is_windowed_and_owner_scoped(db, store):
    [job] = await _thread(db, store, "athr_content")
    text = "中文行 line\n" * 5000
    await _events(db, job, [("tool_result", {"tool_use_id": "t", "content": text})])
    [stranger] = await _thread(db, store, "athr_content_x", user_id=OTHER)
    event_id = await _event_id(db, job, 1)
    first = await store.context_queries.event_content(
        job_id=job, user_id=OWNER, event_id=event_id, offset=0, limit=1000
    )
    assert first is not None and first["total_chars"] == len(text)
    assert first["text"] == text[:1000]
    later = await store.context_queries.event_content(
        job_id=job, user_id=OWNER, event_id=event_id, offset=1000, limit=1000
    )
    assert later is not None and later["text"] == text[1000:2000]
    # Another owner, or the owner through another conversation, reads nothing.
    assert (
        await store.context_queries.event_content(
            job_id=job, user_id=OTHER, event_id=event_id, offset=0, limit=10
        )
        is None
    )
    assert (
        await store.context_queries.event_content(
            job_id=stranger, user_id=OWNER, event_id=event_id, offset=0, limit=10
        )
        is None
    )
