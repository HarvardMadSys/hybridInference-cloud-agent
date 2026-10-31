"""Real Postgres acceptance for usage aggregation over agent-reported tokens.

A turn runs on its owner's model connection, and its agent's own usage report
is what the Usage pages sum. These tests write that report as the agent's
events, read it through the owner and admin routes, and check that the totals
are those events' tokens, counted once per attempt, beside the same thread's
statistics.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import db as db
from tests.support.follow_ups import append_follow_up
from tests.support.owners import CONNECTION_ID, use_session_secret
from tests.unit.test_codex_usage import _completed, _run, _session, _spawn, _usage

from cloud_agent.admin_usage import MAX_USAGE_ATTEMPTS, MAX_USAGE_RUNS
from cloud_agent.api import admin_usage, owner_usage
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.storage import admin_usage_queries
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

START = datetime(2026, 9, 9, 10, tzinfo=UTC)
WINDOW = {"since": "2026-09-09T00:00:00Z", "until": "2026-09-11T00:00:00Z"}
ADMIN = {"Authorization": "Bearer admin-usage-integration-test"}


def report(
    usage_id: str,
    *,
    uncached: int = 20,
    output: int = 10,
    cache_read: int = 80,
    cache_write: int = 0,
) -> dict:
    """One model response's usage, as Claude Code reports it: disjoint counters."""
    return {
        "text": "Working.",
        "usage_id": usage_id,
        "usage": {
            "input_tokens": uncached,
            "output_tokens": output,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
        },
    }


async def _attempt(db, job: dict, *, attempt_no: int = 1, events: list[tuple] = ()) -> int:
    """An attempt of ``job`` and the events its agent emitted, in order."""
    async with db.acquire() as conn:
        attempt_id = await conn.fetchval(
            """
            INSERT INTO agent_attempts (
                job_id, thread_id, attempt_no, lease_owner, lease_generation,
                lease_expires_at, started_at, finished_at, status
            ) VALUES ($1, $3, $2, 'usage-test', $2::integer, NOW() + INTERVAL '1 hour',
                      NOW() - INTERVAL '10 seconds', NOW(), 'finished')
            RETURNING id
            """,
            job["id"],
            attempt_no,
            job["thread_id"],
        )
        await conn.executemany(
            "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)"
            " VALUES ($1, $2, $3, $4, $5::jsonb)",
            [
                (job["id"], attempt_id, seq, event_type, json.dumps(payload))
                for seq, (event_type, payload) in enumerate(events, start=1)
            ],
        )
    return attempt_id


async def _settle(db, job_ids: list[str], created_at: datetime, *, state: str = "succeeded"):
    """Place runs in the past and in ``state``; a succeeded one names its native turn."""
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET created_at = $2, state = $3,"
            " native_turn_no = CASE WHEN $3 = 'succeeded' THEN turn_no ELSE native_turn_no END"
            " WHERE id = ANY($1::text[])",
            job_ids,
            created_at,
            state,
        )


async def _turn(db, store, *, user_id="owner-one", model="model-a", created_at=START, **job):
    created = await _create_job(store, user_id=user_id, model=model, **job)
    await _attempt(db, created, events=[("message", report(f"msg-{created['id']}"))])
    await _settle(db, [created["id"]], created_at)
    return created


def _client(store, *, owner: str | None = None) -> AsyncClient:
    app = FastAPI()
    app.include_router(admin_usage.router)
    app.include_router(owner_usage.router)
    app.dependency_overrides[admin_usage.get_admin_usage_queries] = lambda: store.admin_usage
    app.dependency_overrides[get_storage] = lambda: store
    if owner is not None:
        app.dependency_overrides[require_agent_owner] = lambda: {"user_id": owner}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _selection(store, **changes):
    arguments = {
        "since": datetime(2026, 9, 9, tzinfo=UTC),
        "until": datetime(2026, 9, 11, tzinfo=UTC),
        "user_id": None,
        "model": None,
        "runtime": None,
        "repo": None,
        "max_runs": MAX_USAGE_RUNS,
        "max_attempts": MAX_USAGE_ATTEMPTS,
        **changes,
    }
    return await store.admin_usage.selection(**arguments)


@pytest_asyncio.fixture
async def store(db):
    return Storage(db)


@pytest.fixture
def admin_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-usage-integration-test")
    use_session_secret(monkeypatch)
    return ADMIN


async def test_a_finished_connection_turn_shows_its_agents_tokens(db, store, admin_token):
    """The owner's Usage page sums what the agent reported, never a cost.

    Two events carry the same response's usage (Claude Code repeats it on a
    tool call), and the conversation's next turn is its own run: each response
    counts once, each turn once.
    """
    first = await _create_job(store, user_id="owner-one", model_connection_id=CONNECTION_ID)
    await _attempt(
        db,
        first,
        events=[
            ("message", report("msg_1")),
            ("tool_use", {"name": "Edit", "id": "call-1", **report("msg_1")}),
            ("message", report("msg_2", uncached=5, output=9, cache_read=100, cache_write=20)),
        ],
    )
    # The conversation's next turn, written as production writes it.
    follow_up = await append_follow_up(
        db, parent_job_id=first["id"], user_id="owner-one", prompt="and the docs"
    )
    assert follow_up is not None and "error" not in follow_up
    await _attempt(db, follow_up, events=[("message", report("msg_3", output=1))])
    await _settle(db, [first["id"], follow_up["id"]], START)

    async with _client(store, owner="owner-one") as client:
        mine = await client.get("/v1/agent/usage/tokens", params={"all": "true"})
        admin = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["source"] == "runtime_reported"
    totals = body["summary"]["totals"]
    # Cache-inclusive input: 20+80+0 for msg_1, 5+100+20 for msg_2, 20+80 for msg_3.
    assert totals["tokens_in"] == 100 + 125 + 100
    assert totals["tokens_out"] == 10 + 9 + 1
    assert totals["cache_read"] == 80 + 100 + 80
    assert totals["cache_write"] == 20
    # The provider bills the turn; Claude Code does not report reasoning.
    assert (totals["spent_usd"], totals["reasoning"], totals["calls"]) == (None, None, None)
    assert body["summary"]["run_count"] == 2
    assert body["summary"]["attempt_count"] == 2
    assert body["summary"]["runs_with_snapshot"] == 2
    assert body["by_model"]["groups"][0]["usage"]["totals"]["tokens_out"] == 20
    assert admin.json()["summary"]["totals"] == totals

    # The same rollups now serve the thread's statistics, without a rescan.
    stats = await store.execution_queries.thread_stats(job_id=first["id"], user_id="owner-one")
    assert stats is not None
    assert stats["stats"].output_tokens == 20
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_attempt_thread_stats") == 2


async def test_codex_turns_show_what_the_app_server_reported_once_each(
    db, store, admin_token, tmp_path
):
    """Codex's ``thread/tokenUsage/updated``, as its driver stores it, reaches the Usage page.

    Two turns run on one resident thread; the first spawns a subagent and one
    notification repeats. Each model call counts once, in the turn it ran in,
    and the reasoning Codex reports is shown as part of output.
    """
    process, session = _session(tmp_path)
    first_events = _run(
        process,
        session,
        "turn-a",
        _usage("thread-a", "turn-a", last=(1000, 800, 50, 20), total=(1000, 800, 50, 20)),
        _usage("thread-a", "turn-a", last=(1000, 800, 50, 20), total=(1000, 800, 50, 20)),
        _spawn("thread-a", "child-t"),
        _usage("child-t", "child-turn", last=(300, 0, 40, 10), total=(300, 0, 40, 10)),
        _completed("thread-a", "turn-a"),
    )
    second_events = _run(
        process,
        session,
        "turn-b",
        _usage("thread-a", "turn-b", last=(1500, 1400, 70, 30), total=(2500, 2200, 120, 50)),
        _completed("thread-a", "turn-b"),
    )
    first = await _create_job(
        store, user_id="owner-one", runtime="codex", model_connection_id=CONNECTION_ID
    )
    await _attempt(db, first, events=[(e.event_type, e.payload) for e in first_events])
    follow_up = await append_follow_up(
        db, parent_job_id=first["id"], user_id="owner-one", prompt="and the docs"
    )
    assert follow_up is not None and "error" not in follow_up
    await _attempt(db, follow_up, events=[(e.event_type, e.payload) for e in second_events])
    await _settle(db, [first["id"], follow_up["id"]], START)

    async with _client(store, owner="owner-one") as client:
        mine = await client.get("/v1/agent/usage/tokens", params={"all": "true"})
        admin = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["source"] == "runtime_reported"
    totals = body["summary"]["totals"]
    # Codex input already includes cached input.
    assert totals["tokens_in"] == 1000 + 300 + 1500
    assert totals["tokens_out"] == 50 + 40 + 70
    assert totals["cache_read"] == 800 + 0 + 1400
    assert totals["cache_write"] == 0
    assert totals["reasoning"] == 20 + 10 + 30
    assert (totals["spent_usd"], totals["calls"]) == (None, None)
    assert body["summary"]["runs_with_snapshot"] == 2
    assert body["summary"]["unknown_rows"]["reasoning"] == 0
    assert admin.json()["summary"]["totals"] == totals
    stats = await store.execution_queries.thread_stats(job_id=first["id"], user_id="owner-one")
    assert stats is not None and stats["stats"].output_tokens == 160


async def test_another_owners_turns_are_not_in_mine(db, store, admin_token):
    await _turn(db, store, user_id="owner-one")
    await _turn(db, store, user_id="owner-two")
    async with _client(store, owner="owner-two") as client:
        body = (await client.get("/v1/agent/usage/tokens", params={"all": "true"})).json()
    assert body["summary"]["run_count"] == 1
    assert body["summary"]["totals"]["tokens_out"] == 10


async def test_a_turn_that_reported_nothing_is_coverage_not_zero(db, store, admin_token):
    unstarted = await _create_job(store, user_id="owner-one", model="model-a")
    silent = await _create_job(store, user_id="owner-one", model="model-a")
    await _attempt(db, silent, events=[("message", {"text": "no usage here"})])
    running = await _create_job(store, user_id="owner-one", model="model-a")
    await _attempt(db, running)
    await _settle(db, [unstarted["id"], silent["id"]], START)
    await _settle(db, [running["id"]], START, state="running")

    async with _client(store) as client:
        summary = (
            await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
        ).json()["summary"]
    assert summary["run_count"] == 3
    assert summary["runs_with_snapshot"] == 0
    assert summary["totals"] is None
    assert summary["status_counts"]["unavailable"] == 2
    assert summary["status_counts"]["pending"] == 1
    assert summary["missing_attempts"] == 1
    assert summary["active_runs"] == 1


async def test_a_retry_adds_its_own_report_and_the_silent_attempt_is_missing(
    db, store, admin_token
):
    job = await _create_job(store, user_id="owner-one", model="model-a")
    await _attempt(db, job, attempt_no=1, events=[("message", {"text": "crashed"})])
    await _attempt(db, job, attempt_no=2, events=[("message", report("msg_retry"))])
    await _settle(db, [job["id"]], START)
    async with _client(store) as client:
        summary = (
            await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
        ).json()["summary"]
    assert (summary["attempt_count"], summary["missing_attempts"]) == (2, 1)
    assert summary["status_counts"]["partial"] == 1
    assert summary["totals"]["tokens_out"] == 10


async def test_a_forks_copied_turns_are_not_counted_as_runs(db, store, admin_token):
    """A Fork's copies of its source's turns are history to display, not runs."""
    source = await _turn(db, store)
    copy = await _create_job(store, user_id="owner-one", model="model-a")
    await _settle(db, [copy["id"]], START)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET fork_source_job_id = $2 WHERE id = $1", copy["id"], source["id"]
        )
    async with _client(store) as client:
        summary = (
            await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
        ).json()["summary"]
    assert (summary["run_count"], summary["attempt_count"]) == (1, 1)
    assert summary["status_counts"]["unavailable"] == 0
    cohort, _ = await _selection(store, user_id="owner-one")
    assert [row["id"] for row in cohort] == [source["id"]]


async def test_the_window_is_half_open_on_job_creation_time(db, store, admin_token):
    await _turn(db, store, created_at=START)
    await _turn(db, store, created_at=datetime(2026, 9, 11, tzinfo=UTC))
    await _turn(db, store, created_at=datetime(2026, 9, 8, 23, 59, 59, tzinfo=UTC))
    async with _client(store) as client:
        response = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
        offset = await client.get(
            "/admin/agent/usage",
            params={"since": "2026-09-09T09:00:00+09:00", "until": "2026-09-11T09:00:00+09:00"},
            headers=admin_token,
        )
    body = response.json()
    assert body["summary"]["run_count"] == 1
    assert offset.json()["summary"]["run_count"] == 1
    assert [day["date"] for day in body["days"]] == ["2026-09-09", "2026-09-10"]


async def test_a_run_is_counted_once_on_its_creation_day(db, store, admin_token):
    await _turn(db, store, created_at=datetime(2026, 9, 9, 23, 59, tzinfo=UTC))
    async with _client(store) as client:
        body = (await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)).json()
    days = {day["date"]: day for day in body["days"]}
    assert days["2026-09-09"]["usage"]["totals"]["tokens_out"] == 10
    assert days["2026-09-10"]["usage"]["totals"] is None


async def test_every_day_and_both_breakdowns_add_up_to_the_summary(db, store, admin_token):
    users = ["owner-one", "owner-two"]
    models = ["model-a", "model-b", ""]
    for index in range(6):
        await _turn(
            db,
            store,
            user_id=users[index % 2],
            model=models[index % 3],
            created_at=datetime(2026, 9, 9, tzinfo=UTC) + timedelta(days=index % 2, hours=index),
        )
    async with _client(store) as client:
        body = (await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)).json()
    summary = body["summary"]
    assert summary["run_count"] == 6
    assert summary["totals"]["tokens_out"] == 60
    assert sum(day["usage"]["totals"]["tokens_out"] for day in body["days"]) == 60
    for breakdown in (body["by_user"], body["by_model"]):
        shown = sum(group["usage"]["run_count"] for group in breakdown["groups"])
        rest = breakdown["other"]["usage"]["run_count"] if breakdown["other"] else 0
        assert shown + rest == 6
    assert {group["key"] for group in body["by_model"]["groups"]} == {"model-a", "model-b", None}


async def test_an_exact_filter_selects_only_that_group(db, store, admin_token):
    await _turn(db, store, user_id="owner-one", model="model-a")
    await _turn(db, store, user_id="owner-two", model="model-b")
    async with _client(store) as client:
        body = (
            await client.get(
                "/admin/agent/usage", params={**WINDOW, "user_id": "owner-two"}, headers=admin_token
            )
        ).json()
    assert body["summary"]["run_count"] == 1
    assert body["by_user"]["groups"][0]["key"] == "owner-two"


async def test_too_many_runs_or_attempts_is_refused_without_a_partial_total(
    db, store, admin_token, monkeypatch
):
    for minute in range(2):
        await _turn(db, store, created_at=START + timedelta(minutes=minute))
    monkeypatch.setattr(admin_usage, "MAX_USAGE_RUNS", 1)
    async with _client(store) as client:
        runs = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
    monkeypatch.setattr(admin_usage, "MAX_USAGE_RUNS", MAX_USAGE_RUNS)
    monkeypatch.setattr(admin_usage, "MAX_USAGE_ATTEMPTS", 1)
    async with _client(store) as client:
        attempts = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
    for response in (runs, attempts):
        assert response.status_code == 422
        assert response.json()["detail"]["error"]["type"] == "usage_query_too_large"


async def test_the_cohort_and_its_usage_come_from_one_read_view(
    db, store, admin_token, monkeypatch
):
    """A run selected as active is never summed with events written after the read began."""
    job = await _create_job(store, user_id="owner-one", model="model-a")
    attempt_id = await _attempt(db, job, events=[("message", report("msg_1"))])
    await _settle(db, [job["id"]], START, state="running")

    queries = store.admin_usage
    original = queries._attempt_usage
    ready = asyncio.Event()
    updated = asyncio.Event()

    async def usage_after_update(conn, **kwargs):
        ready.set()
        await updated.wait()
        return await original(conn, **kwargs)

    monkeypatch.setattr(queries, "_attempt_usage", usage_after_update)
    async with _client(store) as client:
        request = asyncio.create_task(
            client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
        )
        await ready.wait()
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 WHERE id = $1",
                job["id"],
            )
            await conn.execute(
                "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)"
                " VALUES ($1, $2, 2, 'message', $3::jsonb)",
                job["id"],
                attempt_id,
                json.dumps(report("msg_2")),
            )
        updated.set()
        response = await request
    summary = response.json()["summary"]
    pair = (summary["active_runs"], summary["totals"]["tokens_out"])
    assert pair in {(1, 10), (0, 20)}, pair


async def test_a_stale_rollup_is_reduced_again_and_stored(db, store, admin_token):
    job = await _create_job(store, user_id="owner-one", model="model-a")
    attempt_id = await _attempt(db, job, events=[("message", report("msg_1"))])
    await _settle(db, [job["id"]], START)
    async with _client(store) as client:
        await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)"
                " VALUES ($1, $2, 2, 'message', $3::jsonb)",
                job["id"],
                attempt_id,
                json.dumps(report("msg_2", output=5)),
            )
        body = (await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)).json()
    assert body["summary"]["totals"]["tokens_out"] == 15
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT event_seq FROM agent_attempt_thread_stats WHERE attempt_id = $1", attempt_id
            )
            == 2
        )


async def _run_at(db, store, created_at: datetime, *, user_id: str = "owner-one") -> dict:
    job = await _create_job(store, user_id=user_id, model="model-a")
    await _settle(db, [job["id"]], created_at)
    return job


async def test_an_owners_run_bounds_read_the_runs_the_cohort_selects(db, store):
    """The bounds an all-time period starts from count exactly the cohort's runs."""
    day = datetime(2026, 9, 1, tzinfo=UTC)
    until = day + timedelta(days=3, hours=18)
    first = await _run_at(db, store, day + timedelta(hours=10))
    await _run_at(db, store, day + timedelta(days=1, hours=8))
    await _run_at(db, store, day + timedelta(days=1, hours=9))
    await _run_at(db, store, day + timedelta(days=2, hours=12))
    copy = await _run_at(db, store, day - timedelta(days=5))
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET fork_source_job_id = $2 WHERE id = $1", copy["id"], first["id"]
        )
    await _run_at(db, store, day - timedelta(days=9), user_id="owner-two")
    await _run_at(db, store, until)

    bounds = store.admin_usage.owner_run_bounds
    assert await bounds("owner-one", until=until, max_runs=10) == (day + timedelta(hours=10), None)
    assert await bounds("nobody", until=until, max_runs=10) == (None, None)
    earliest, overflow = await bounds("owner-one", until=until, max_runs=2)
    assert earliest == day + timedelta(hours=10)
    assert overflow == day + timedelta(days=1, hours=8)

    async def cohort(since: datetime, max_runs: int) -> list[dict]:
        rows, _ = await _selection(
            store, since=since, until=until, user_id="owner-one", max_runs=max_runs
        )
        return rows

    with pytest.raises(admin_usage_queries.UsageQueryTooLarge):
        await cohort(day + timedelta(days=1), 2)
    assert len(await cohort(day + timedelta(days=2), 2)) == 1
    assert (await bounds("owner-one", until=until, max_runs=3))[1] == day + timedelta(hours=10)
    assert len(await cohort(day + timedelta(days=1), 3)) == 3
