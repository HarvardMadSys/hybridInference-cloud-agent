"""Real Postgres acceptance for cross-task usage aggregation.

The aggregate is compared against the existing single-run ledger, so the two
surfaces cannot drift into different definitions of the same job.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent import identity
from cloud_agent.admin_usage import (
    MAX_USAGE_LEDGER_ROWS,
    MAX_USAGE_RUNS,
    MAX_USAGE_SNAPSHOT_BYTES,
)
from cloud_agent.api import admin_usage
from cloud_agent.api.deps import get_storage
from cloud_agent.storage.assembly import Storage
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import _seed_attempt, db as db
from tests.unit.test_ledger_snapshots import END, START, report_payload

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

WINDOW = {"since": "2026-09-09T00:00:00Z", "until": "2026-09-11T00:00:00Z"}
ADMIN = {"Authorization": "Bearer admin-usage-integration-test"}


def coherent_payload(*, calls: int, amount: str, grant_id: str = "grant-one") -> dict:
    """A report whose nulls and missing-row counts describe the same requests."""
    payload = report_payload(grant_id=grant_id)
    payload["totals"] = {**payload["totals"], "calls": calls, "spent_usd": amount}
    # Every metric is now reported by every row, except reasoning, which the
    # gateway records only when an upstream sends a positive breakdown.
    payload["unknown_rows"] = {
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": calls,
        "spent_usd": 0,
    }
    return payload


async def _seed_snapshot(
    db,
    store,
    ledger_id: int,
    *,
    amount: str = "0.00012345",
    calls: int = 1,
    grant_id: str = "grant-one",
    queried_at: datetime | None = None,
) -> None:
    """Write the totals a Gateway read would have stored, verbatim."""
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempt_ledger SET snapshot = $2::jsonb, queried_at = $3 WHERE id = $1",
            ledger_id,
            json.dumps(coherent_payload(calls=calls, amount=amount, grant_id=grant_id)),
            queried_at or END,
        )


async def _backdate(
    db, store, job_ids: list[str], created_at: datetime, *, state: str | None = None
):
    """Place a run in the past, and optionally in a terminal state.

    A succeeded run must name the native turn it finished, so the state and that
    number are written together rather than the constraint being worked around.
    """
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET created_at = $2 WHERE id = ANY($1::text[])", job_ids, created_at
        )
        if state is not None:
            await conn.execute(
                "UPDATE agent_jobs SET state = $2,"
                " native_turn_no = CASE WHEN $2 = 'succeeded' THEN 1 ELSE native_turn_no END"
                " WHERE id = ANY($1::text[])",
                job_ids,
                state,
            )


def _client(store) -> AsyncClient:
    from cloud_agent.api import admin_runs

    app = FastAPI()
    app.include_router(admin_usage.router)
    app.include_router(admin_runs.router)
    # The assembled projections, not two more built from a loose pool: these
    # routes read what the service reads.
    app.dependency_overrides[admin_usage.get_admin_usage_queries] = lambda: store.admin_usage
    app.dependency_overrides[admin_runs.get_admin_run_queries] = lambda: store.admin_runs
    app.dependency_overrides[get_storage] = lambda: store
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def store(db):
    """Everything these tests need, to set a claim up."""
    return Storage(db)


@pytest.fixture
def admin_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-usage-integration-test")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "admin-usage-integration-long-secret")
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    return ADMIN


async def test_the_aggregate_matches_the_single_run_ledger_field_by_field(db, store, admin_token):
    job = await _create_job(store, user_id="owner-one", model="model-a")
    _, attempt_one, ledger_one = await _seed_attempt(db, store, job=job)
    _, _, ledger_two = await _seed_attempt(db, store, job=job, attempt_no=2, grant_id="grant-two")
    failed = await _create_job(store, user_id="owner-two", model="model-b")
    _, _, ledger_failed = await _seed_attempt(db, store, job=failed, grant_id="grant-three")
    await _backdate(db, store, [job["id"]], START, state="succeeded")
    await _backdate(db, store, [failed["id"]], START, state="failed")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET status = 'running' WHERE id = $1", attempt_one
        )
    await _seed_snapshot(db, store, ledger_one, amount="1.00", calls=2)
    await _seed_snapshot(db, store, ledger_two, amount="0.50", calls=1, grant_id="grant-two")
    await _seed_snapshot(db, store, ledger_failed, amount="0.25", calls=4, grant_id="grant-three")

    async with _client(store) as client:
        response = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
        assert response.status_code == 200
        body = response.json()
        single_response = await client.get(f"/admin/agent/runs/{job['id']}", headers=admin_token)
        assert single_response.status_code == 200, single_response.text
        single = single_response.json()

    assert body["summary"]["run_count"] == 2
    assert body["summary"]["attempt_count"] == 3
    assert body["summary"]["totals"]["calls"] == 7
    assert body["summary"]["totals"]["spent_usd"] == "1.75"
    assert body["summary"]["runs_with_snapshot"] == 2
    assert body["summary"]["missing_attempts"] == 0
    assert body["summary"]["oldest_queried_at"] is not None
    # The aggregation's per-job accounting is the single-run ledger's.
    assert single["usage"]["ledger"]["totals"]["calls"] == 3
    assert single["usage"]["ledger"]["totals"]["spent_usd"] == "1.50"
    assert single["usage"]["ledger"]["status"] == "available"
    assert {group["key"] for group in body["by_user"]["groups"]} == {"owner-one", "owner-two"}
    owner_one = next(g for g in body["by_user"]["groups"] if g["key"] == "owner-one")
    assert owner_one["usage"]["totals"]["calls"] == 3
    assert owner_one["usage"]["totals"]["spent_usd"] == "1.50"


async def test_a_missing_snapshot_keeps_the_run_detail_status(db, store, admin_token):
    """The aggregate's coverage must be the run detail's coverage.

    A turn that never claimed is unavailable; one whose reference simply has no
    snapshot yet is pending. Reading the reference under the wrong column name
    turned the second case into the first, so the two surfaces disagreed about
    the same run.
    """
    unstarted = await _create_job(store, user_id="owner-one", model="model-a")
    untracked = await _create_job(store, user_id="owner-one", model="model-a")
    _, _, ledger_untracked = await _seed_attempt(db, store, job=untracked)
    await _backdate(db, store, [unstarted["id"], untracked["id"]], START, state="succeeded")
    assert ledger_untracked is not None

    async with _client(store) as client:
        response = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
        detail = (
            await client.get(f"/admin/agent/runs/{untracked['id']}", headers=admin_token)
        ).json()
    body = response.json()
    assert detail["usage"]["ledger"]["status"] == "pending"
    assert body["summary"]["run_count"] == 2
    assert body["summary"]["runs_with_snapshot"] == 0
    assert body["summary"]["totals"] is None
    assert body["summary"]["unknown_rows"] is None
    assert body["summary"]["status_counts"]["pending"] == 1
    assert body["summary"]["status_counts"]["unavailable"] == 1
    assert body["summary"]["missing_attempts"] == 1


async def test_the_window_is_half_open_on_job_creation_time(db, store, admin_token):
    at_since = await _create_job(store, user_id="owner-one", model="model-a")
    at_until = await _create_job(store, user_id="owner-one", model="model-a")
    before = await _create_job(store, user_id="owner-one", model="model-a")
    await _backdate(db, store, [at_since["id"]], START)
    await _backdate(db, store, [at_until["id"]], datetime(2026, 9, 11, tzinfo=UTC))
    await _backdate(db, store, [before["id"]], datetime(2026, 9, 8, 23, 59, 59, tzinfo=UTC))

    async with _client(store) as client:
        response = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
        offset = await client.get(
            "/admin/agent/usage",
            params={"since": "2026-09-09T09:00:00+09:00", "until": "2026-09-11T09:00:00+09:00"},
            headers=admin_token,
        )
    body = response.json()
    assert body["summary"]["run_count"] == 1
    assert body["selection"]["since"] == "2026-09-09T00:00:00Z"
    assert body["selection"]["until"] == "2026-09-11T00:00:00Z"
    # An equivalent local boundary selects exactly the same cohort.
    assert offset.json()["summary"]["run_count"] == 1
    assert [day["date"] for day in body["days"]] == ["2026-09-09", "2026-09-10"]
    assert sum(day["usage"]["run_count"] for day in body["days"]) == 1


async def test_a_run_is_counted_once_on_its_creation_day_whatever_its_span(db, store, admin_token):
    """A turn created late on the 9th keeps its whole snapshot on the 9th."""
    job = await _create_job(store, user_id="owner-one", model="model-a")
    _, _, ledger_id = await _seed_attempt(db, store, job=job)
    await _backdate(
        db, store, [job["id"]], datetime(2026, 9, 9, 23, 59, tzinfo=UTC), state="succeeded"
    )
    await _seed_snapshot(db, store, ledger_id, amount="9.99", calls=3)

    async with _client(store) as client:
        response = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
    body = response.json()
    days = {day["date"]: day for day in body["days"]}
    assert days["2026-09-09"]["usage"]["totals"]["spent_usd"] == "9.99"
    assert days["2026-09-10"]["usage"]["totals"] is None
    assert days["2026-09-09"]["since"] == "2026-09-09T00:00:00Z"
    assert days["2026-09-09"]["until"] == "2026-09-10T00:00:00Z"
    assert body["summary"]["totals"]["spent_usd"] == "9.99"


async def test_every_day_and_both_breakdowns_add_up_to_the_summary(db, store, admin_token):
    users = ["owner-one", "owner-two"]
    models = ["model-a", "model-b", ""]
    for index in range(6):
        job = await _create_job(store, user_id=users[index % 2], model=models[index % 3] or "")
        _, _, ledger_id = await _seed_attempt(db, store, job=job)
        await _backdate(
            db,
            store,
            [job["id"]],
            datetime(2026, 9, 9, tzinfo=UTC) + timedelta(days=index % 2, hours=index),
            state="succeeded",
        )
        await _seed_snapshot(db, store, ledger_id, amount=f"{index + 1}.00", calls=index + 1)

    async with _client(store) as client:
        response = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
    body = response.json()
    summary = body["summary"]
    assert summary["run_count"] == 6
    assert summary["totals"]["spent_usd"] == "21.00"
    assert sum(day["usage"]["run_count"] for day in body["days"]) == 6
    assert (
        sum(day["usage"]["totals"]["calls"] for day in body["days"]) == summary["totals"]["calls"]
    )
    for breakdown in (body["by_user"], body["by_model"]):
        shown = sum(group["usage"]["run_count"] for group in breakdown["groups"])
        rest = breakdown["other"]["usage"]["run_count"] if breakdown["other"] else 0
        assert shown + rest == 6
    assert body["by_user"]["group_count"] == 2
    assert body["by_model"]["group_count"] == 3
    # An empty configured model is one unknown group, and it keeps its coverage.
    assert {group["key"] for group in body["by_model"]["groups"]} == {"model-a", "model-b", None}


async def test_long_configured_models_are_not_merged_into_one_group(db, store, admin_token):
    """Grouping uses the stored identity, never a truncated stand-in.

    Two models that share their first 256 characters are two groups, and the
    key that comes back is the real one, so a filter built from it can match.
    """
    first_model = "m" * 256 + "one"
    second_model = "m" * 256 + "two"
    first = await _create_job(store, user_id="owner-one", model=first_model)
    second = await _create_job(store, user_id="owner-one", model=second_model)
    _, _, first_ledger = await _seed_attempt(db, store, job=first)
    _, _, second_ledger = await _seed_attempt(db, store, job=second)
    await _backdate(db, store, [first["id"], second["id"]], START, state="succeeded")
    await _seed_snapshot(db, store, first_ledger, amount="1.00")
    await _seed_snapshot(db, store, second_ledger, amount="2.00")

    async with _client(store) as client:
        response = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
        filtered = await client.get(
            "/admin/agent/usage",
            params={**WINDOW, "model": first_model},
            headers=admin_token,
        )
    body = response.json()
    assert body["by_model"]["group_count"] == 2
    assert {group["key"] for group in body["by_model"]["groups"]} == {
        first_model,
        second_model,
    }
    assert body["summary"]["totals"]["spent_usd"] == "3.00"
    # The stored key is longer than the filter contract allows, so the page
    # disables the exact link rather than building one that must fail. The
    # refusal here is that same contract holding at the boundary.
    assert filtered.status_code == 422
    assert "summary" not in filtered.json()


async def test_an_exact_filter_selects_only_that_group(db, store, admin_token):
    first = await _create_job(store, user_id="owner-one", model="model-a")
    second = await _create_job(store, user_id="owner-two", model="model-b")
    _, _, first_ledger = await _seed_attempt(db, store, job=first)
    _, _, second_ledger = await _seed_attempt(db, store, job=second)
    await _backdate(db, store, [first["id"], second["id"]], START, state="succeeded")
    await _seed_snapshot(db, store, first_ledger, amount="1.00")
    await _seed_snapshot(db, store, second_ledger, amount="2.00")

    async with _client(store) as client:
        response = await client.get(
            "/admin/agent/usage", params={**WINDOW, "user_id": "owner-two"}, headers=admin_token
        )
    body = response.json()
    assert body["summary"]["run_count"] == 1
    assert body["summary"]["totals"]["spent_usd"] == "2.00"
    assert body["by_user"]["groups"][0]["key"] == "owner-two"


async def test_too_many_jobs_is_refused_without_a_partial_total(
    db, store, admin_token, monkeypatch
):
    monkeypatch.setattr(admin_usage, "MAX_USAGE_RUNS", 1)
    for index in range(2):
        job = await _create_job(store, user_id="owner-one", model="model-a")
        await _backdate(db, store, [job["id"]], START + timedelta(minutes=index), state="succeeded")

    async with _client(store) as client:
        response = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["type"] == "usage_query_too_large"
    assert MAX_USAGE_RUNS == 10_000


async def test_the_cohort_and_its_snapshots_come_from_one_read_view(
    db, store, admin_token, monkeypatch
):
    """A run selected as active cannot be priced by a snapshot of its end.

    The route reads twice inside one transaction: the cohort, then the reports
    it sums. Separate transactions returned combinations the database never
    held -- a run counted as ``running`` beside the cost its finished attempt
    had already written. Here the same run and the same snapshot are both
    updated between the two reads, so only a shared view can answer with one of
    the two real states.
    """
    job = await _create_job(store, user_id="owner-one", model="model-a")
    _, _, ledger_id = await _seed_attempt(db, store, job=job)
    await _backdate(db, store, [job["id"]], START, state="running")
    await _seed_snapshot(db, store, ledger_id, amount="1.00", calls=1)

    queries = store.admin_usage
    original_reports = queries._reports
    ready = asyncio.Event()
    updated = asyncio.Event()

    async def reports_after_update(conn, **kwargs):
        # The cohort statement has completed on this connection and the read
        # view is still open; publish the update now, then finish the read.
        ready.set()
        await updated.wait()
        return await original_reports(conn, **kwargs)

    monkeypatch.setattr(queries, "_reports", reports_after_update)
    app = FastAPI()
    app.include_router(admin_usage.router)
    app.dependency_overrides[admin_usage.get_admin_usage_queries] = lambda: queries
    app.dependency_overrides[get_storage] = lambda: store

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        request = asyncio.create_task(
            client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
        )
        await ready.wait()
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 WHERE id = $1",
                job["id"],
            )
        await _seed_snapshot(db, store, ledger_id, amount="2.00", calls=2)
        updated.set()
        response = await request

    assert response.status_code == 200
    body = response.json()
    # Either read view is acceptable; a mixture of the two is not. The first
    # read reports the run as active and its snapshot at 1.00, the second as
    # finished and at 2.00, and nothing in between ever existed.
    active = body["summary"]["active_runs"]
    spent = body["summary"]["totals"]["spent_usd"]
    assert (active, spent) in {(1, "1.00"), (0, "2.00")}, (active, spent)
    assert body["summary"]["status_counts"]["available"] == 1
    assert sum(day["usage"]["run_count"] for day in body["days"]) == 1


async def test_the_projection_preserves_a_damaged_report_instead_of_repairing_it(
    db, store, admin_token
):
    """A missing required key must not arrive as a null that still validates."""
    job = await _create_job(store, user_id="owner-one", model="model-a")
    _, _, ledger_id = await _seed_attempt(db, store, job=job)
    await _backdate(db, store, [job["id"]], START, state="succeeded")
    damaged = coherent_payload(calls=1, amount="1.00")
    del damaged["totals"]["spent_usd"]
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempt_ledger SET snapshot = $2::jsonb, queried_at = $3 WHERE id = $1",
            ledger_id,
            json.dumps(damaged),
            END,
        )

    _, rows = await store.admin_usage.selection(
        since=START,
        until=END,
        user_id=None,
        model=None,
        runtime=None,
        repo=None,
        max_runs=MAX_USAGE_RUNS,
        max_rows=MAX_USAGE_LEDGER_ROWS,
        max_snapshot_bytes=MAX_USAGE_SNAPSHOT_BYTES,
    )
    assert "spent_usd" not in rows[0]["snapshot"]["totals"]
    assert "reasoning" in rows[0]["snapshot"]["totals"]

    async with _client(store) as client:
        response = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
    body = response.json()
    # The damaged snapshot contributes no usage and no zero.
    assert body["summary"]["totals"] is None
    assert body["summary"]["runs_with_snapshot"] == 0
    assert body["summary"]["status_counts"]["unavailable"] == 1


async def test_a_snapshot_carrying_only_allowlisted_keys_is_read(db, store, admin_token):
    """Extra stored keys are dropped by the projection, not by the reader."""
    job = await _create_job(store, user_id="owner-one", model="model-a")
    _, _, ledger_id = await _seed_attempt(db, store, job=job)
    await _backdate(db, store, [job["id"]], START, state="succeeded")
    payload = coherent_payload(calls=2, amount="3.00")
    payload["token"] = "private gateway credential"
    payload["metadata"] = {"authorization": "private"}
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempt_ledger SET snapshot = $2::jsonb, queried_at = $3 WHERE id = $1",
            ledger_id,
            json.dumps(payload),
            END,
        )

    async with _client(store) as client:
        response = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
    assert response.status_code == 200
    assert response.json()["summary"]["totals"]["spent_usd"] == "3.00"
    assert "private" not in response.text


async def test_event_volume_does_not_change_the_result(db, store, admin_token):
    """Aggregation reads jobs and ledger rows, never the event table."""
    job = await _create_job(store, user_id="owner-one", model="model-a")
    _, attempt_id, ledger_id = await _seed_attempt(db, store, job=job)
    await _backdate(db, store, [job["id"]], START, state="succeeded")
    await _seed_snapshot(db, store, ledger_id, amount="1.00")
    async with db.acquire() as conn:
        await conn.executemany(
            "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)"
            " VALUES ($1, $2, $3, $4, $5::jsonb)",
            [
                (job["id"], attempt_id, seq, "message", json.dumps({"text": "noise" * 50}))
                for seq in range(1, 2001)
            ],
        )

    async with _client(store) as client:
        response = await client.get("/admin/agent/usage", params=WINDOW, headers=admin_token)
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["totals"]["spent_usd"] == "1.00"
    assert body["summary"]["attempt_count"] == 1
