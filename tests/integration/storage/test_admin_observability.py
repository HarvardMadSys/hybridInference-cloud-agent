"""Real SQL checks for content boundaries, accounting and cross-attempt pages."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import _seed_attempt, db as db

from cloud_agent.admin_observability import (
    ActivityCursor,
    RunCursor,
    RunFilters,
    cursor_scope,
    decode_cursor,
)
from cloud_agent.api import admin_runs
from cloud_agent.api._job_views import _attempt_resources
from cloud_agent.api.deps import get_storage
from cloud_agent.ledger import summarize_job_usage
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.assembly import Storage
from cloud_agent_host.event_payloads import MAX_EVENT_PAYLOAD_BYTES


@pytest_asyncio.fixture
async def store(db):
    """Everything these tests need, to set a claim up."""
    return Storage(db)


from tests.unit.test_ledger_snapshots import END, START

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _event(db, store, job, attempt_id, seq, event_type="message", payload=None):
    async with db.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)
               VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING id""",
            job["id"],
            attempt_id,
            seq,
            event_type,
            json.dumps(payload or {}),
        )


async def _safe_title(db, store, job, title="Example task"):
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET title = $2, title_source = 'generated' WHERE id = $1",
            job["thread_id"],
            title,
        )


async def test_list_is_cross_user_content_free_and_filters_do_not_duplicate_turns(db, store):
    first = await _create_job(
        store,
        user_id="owner-one",
        task_prompt="PRIVATE-PROMPT",
        metadata={"auth": "PRIVATE-METADATA"},
    )
    second = await _create_job(store, user_id="owner-two", model="other-model")
    _, a1, _ = await _seed_attempt(db, store, job=first, grant_id=None)
    _, a2, _ = await _seed_attempt(db, store, job=first, attempt_no=2, grant_id=None)
    _, b1, _ = await _seed_attempt(db, store, job=second, grant_id=None)
    await _safe_title(db, store, first, "Approved title " + "x" * 500)
    await _safe_title(db, store, second)
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET created_at = $1, detail = 'PRIVATE-ERROR'", START)
        await conn.execute(
            "INSERT INTO agent_runner_hosts (host, first_seen_at, last_seen_at) VALUES ('runner-one', NOW(), NOW())"
        )
        await conn.execute(
            "UPDATE agent_attempts SET host_id = 'runner-one' WHERE id = ANY($1::bigint[])",
            [a1, a2],
        )
    await _event(db, store, first, a1, 1, "tool_use", {"input": "PRIVATE-INPUT"})
    await _event(db, store, first, a2, 1, "tool_use", {"input": "PRIVATE-INPUT"})
    await _event(db, store, second, b1, 1, "message", {"text": "PRIVATE-OUTPUT"})
    queries = store.admin_runs

    async def page(**filters):
        return await queries.list_runs(
            filters=RunFilters(**filters),
            since=START - timedelta(days=1),
            until=END + timedelta(days=1),
            cursor=None,
            limit=20,
        )

    all_runs = await page()
    assert {run.user_id for run in all_runs.runs} == {"owner-one", "owner-two"}
    assert "PRIVATE" not in all_runs.model_dump_json()
    by_id = {run.id: run for run in all_runs.runs}
    assert by_id[first["id"]].tool_calls == 2
    assert "event_count" not in all_runs.model_dump_json()
    assert by_id[first["id"]].hosts == ["runner-one"]
    assert len(by_id[first["id"]].title) == 160
    assert [row.id for row in (await page(host="runner-one")).runs] == [first["id"]]
    assert [row.id for row in (await page(user_id="owner-two", model="other-model")).runs] == [
        second["id"]
    ]
    assert (await page(repo="unrelated/repo")).runs == []
    assert (await page(state="failed")).runs == []
    run = await queries.get_run(first["id"])
    assert "PRIVATE" not in json.dumps(run, default=str)
    assert "event_count" not in run


async def test_run_physical_hosts_group_runners_without_changing_ids_or_filters(db, store):
    job = await _create_job(store)
    hosts = {
        "gpu-box": {"physical_host": "gpu-box"},
        "gpu-box-kata": {"physical_host": "  gpu-box  "},
        "legacy-kata": {},
        "blank-label": {"physical_host": "   "},
        "invalid-label": {"physical_host": {"unexpected": "object"}},
        "null-label": {"physical_host": None},
    }
    for number, (host, labels) in enumerate(hosts.items(), 1):
        _, attempt, _ = await _seed_attempt(db, store, job=job, attempt_no=number, grant_id=None)
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_runner_hosts (host, labels, first_seen_at, last_seen_at) "
                "VALUES ($1, $2::jsonb, NOW(), NOW())",
                host,
                json.dumps(labels),
            )
            await conn.execute(
                "UPDATE agent_attempts SET host_id = $1 WHERE id = $2", host, attempt
            )
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET created_at = $1 WHERE id = $2", START, job["id"])
    queries = store.admin_runs
    # Filtering still targets the exact runner, even when its display name is shared.
    page = await queries.list_runs(
        filters=RunFilters(host="gpu-box-kata"), since=START, until=END, cursor=None, limit=20
    )
    assert [run.id for run in page.runs] == [job["id"]]
    expected = ["blank-label", "gpu-box", "invalid-label", "legacy-kata", "null-label"]
    assert page.runs[0].physical_hosts == expected
    assert page.runs[0].hosts == sorted(hosts)
    detail = await queries.get_run(job["id"])
    assert detail["physical_hosts"] == expected
    assert detail["hosts"] == sorted(hosts)


@pytest.mark.parametrize("title_source", ["fallback", "generated", "manual"])
async def test_admin_metadata_routes_hide_prompt_fallback_titles(
    db, store, monkeypatch, title_source
):
    job = await _create_job(store, task_prompt="PRIVATE-PROMPT-FIRST-LINE\nMore private content")
    expected = None if title_source == "fallback" else "Approved task title"
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET created_at = $1 WHERE id = $2", START, job["id"])
        if expected is not None:
            await conn.execute(
                "UPDATE agent_threads SET title = $2, title_source = $3 WHERE id = $1",
                job["thread_id"],
                expected,
                title_source,
            )
        else:
            assert (
                await conn.fetchval(
                    "SELECT title FROM agent_threads WHERE id = $1", job["thread_id"]
                )
                == "PRIVATE-PROMPT-FIRST-LINE"
            )
    monkeypatch.setenv("ADMIN_TOKEN", "db-admin-observation-test")
    app = FastAPI()
    app.include_router(admin_runs.router)
    app.dependency_overrides[get_storage] = lambda: store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer db-admin-observation-test"}
        listing = await client.get(
            "/admin/agent/runs",
            params={"since": START.isoformat(), "until": END.isoformat()},
            headers=headers,
        )
        detail = await client.get(f"/admin/agent/runs/{job['id']}", headers=headers)
    for response in (listing, detail):
        assert response.status_code == 200, response.text
        assert "PRIVATE" not in response.text
        assert "event_count" not in response.text
    assert listing.json()["runs"][0]["title"] == expected
    assert detail.json()["run"]["title"] == expected


async def test_run_status_filters_exact_states_before_pagination(db, store, monkeypatch):
    states = ("queued", "running", "succeeded", "failed", "cancelled")
    jobs = {}
    for state in states:
        job = await _create_job(store)
        jobs[state] = job["id"]
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_jobs SET state = $2, created_at = $3, "
                "native_turn_no = CASE WHEN $2 = 'succeeded' THEN 1 END WHERE id = $1",
                job["id"],
                state,
                START,
            )
    monkeypatch.setenv("ADMIN_TOKEN", "db-admin-observation-test")
    app = FastAPI()
    app.include_router(admin_runs.router)
    app.dependency_overrides[get_storage] = lambda: store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for selected in (None, *states):
            expected = states if selected is None else (selected,)
            params = {"since": START.isoformat(), "until": END.isoformat(), "limit": 1}
            if selected is not None:
                params["state"] = selected
            seen = []
            for _ in range(len(states) + 1):
                response = await client.get(
                    "/admin/agent/runs",
                    params=params,
                    headers={"Authorization": "Bearer db-admin-observation-test"},
                )
                assert response.status_code == 200, response.text
                page = response.json()
                seen.extend((run["id"], run["state"]) for run in page["runs"])
                if page["next_cursor"] is None:
                    break
                params["cursor"] = page["next_cursor"]
            else:
                pytest.fail("Run status pagination did not terminate")
            assert seen == sorted(((jobs[state], state) for state in expected), reverse=True)


async def test_run_cursor_keeps_tied_timestamps_and_the_original_filter_window(db, store):
    jobs = [await _create_job(store) for _ in range(4)]
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET created_at = $1", START)
    queries = store.admin_runs
    filters = RunFilters()
    cursor = None
    seen = []
    while True:
        page = await queries.list_runs(
            filters=filters, since=START, until=END, cursor=cursor, limit=1
        )
        seen.extend(row.id for row in page.runs)
        if page.next_cursor is None:
            break
        cursor = decode_cursor(page.next_cursor, RunCursor, cursor_scope(filters.model_dump()))
        assert cursor.since == START
        assert cursor.until == END
        with pytest.raises(ValueError, match="Invalid pagination"):
            decode_cursor(
                page.next_cursor, RunCursor, cursor_scope(RunFilters(user_id="other").model_dump())
            )
    assert seen == sorted((job["id"] for job in jobs), reverse=True)
    assert len(set(seen)) == 4


async def test_activity_cursor_orders_per_attempt_and_freezes_late_control_events(db, store):
    job, first, _ = await _seed_attempt(db, store, grant_id=None)
    _, second, _ = await _seed_attempt(db, store, job=job, attempt_no=2, grant_id=None)
    other, other_attempt, _ = await _seed_attempt(db, store, grant_id=None)
    ids = [
        await _event(
            db,
            store,
            job,
            first,
            1,
            "tool_use",
            {"id": "same-id", "name": "Bash", "input": "PRIVATE"},
        ),
        await _event(
            db,
            store,
            job,
            second,
            1,
            "tool_use",
            {"id": "same-id", "name": "Read", "input": "PRIVATE"},
        ),
        await _event(
            db,
            store,
            job,
            first,
            2,
            "tool_result",
            {"tool_use_id": "same-id", "is_error": True, "text": "PRIVATE"},
        ),
        await _event(
            db,
            store,
            job,
            second,
            2,
            "tool_result",
            {"tool_use_id": "same-id", "is_error": False, "text": "PRIVATE"},
        ),
    ]
    await _event(db, store, other, other_attempt, 1, "message", {"text": "PRIVATE-OTHER"})
    queries = store.admin_runs
    page1 = await queries.activity(job["id"], attempt_id=None, cursor=None, limit=2)
    assert [(row.attempt_id, row.seq) for row in page1.events] == [(first, 1), (first, 2)]
    assert page1.events[1].is_error is True
    assert page1.events[0].tool_use_id == "same-id"
    assert "PRIVATE" not in page1.model_dump_json()
    assert "payload" not in page1.model_dump_json()
    cursor = decode_cursor(page1.next_cursor, ActivityCursor, cursor_scope([job["id"], None]))
    late = await _event(db, store, job, first, 3, "attempt_superseded", {"detail": "PRIVATE-LATE"})
    page2 = await queries.activity(job["id"], attempt_id=None, cursor=cursor, limit=2)
    assert [(row.attempt_id, row.seq) for row in page2.events] == [(second, 1), (second, 2)]
    assert page2.next_cursor is None
    assert {row.id for row in page1.events + page2.events} == set(ids)
    refreshed = await queries.activity(job["id"], attempt_id=None, cursor=None, limit=20)
    assert late in {row.id for row in refreshed.events}
    filtered = await queries.activity(job["id"], attempt_id=second, cursor=None, limit=20)
    assert {row.attempt_id for row in filtered.events} == {second}
    with pytest.raises(ValueError, match="Invalid pagination"):
        decode_cursor(page1.next_cursor, ActivityCursor, cursor_scope([other["id"], None]))
    with pytest.raises(ValueError, match="Invalid pagination"):
        decode_cursor(page1.next_cursor, ActivityCursor, cursor_scope([job["id"], first]))


async def test_metadata_projection_bounds_names_and_never_fetches_nested_content(db, store):
    job, attempt, _ = await _seed_attempt(db, store, grant_id=None)
    payload = {
        "id": "PRIVATE-ID" * 200,
        "name": "tool-" * 10_000,
        "input": {"command": "PRIVATE-COMMAND"},
        "text": "PRIVATE-TEXT",
        "detail": "PRIVATE-DETAIL",
        "extra": {"nested": "PRIVATE-EXTRA"},
        "usage": {
            "input_tokens": {"content": "PRIVATE-COUNTER"},
            "output_tokens": 20,
            "secret": "PRIVATE-USAGE",
        },
        "usage_id": "PRIVATE-IDENTITY",
        "usage_scope": "PRIVATE-SCOPE",
    }
    await _event(db, store, job, attempt, 1, "tool_use", payload)
    queries = store.admin_runs
    activity = await queries.activity(job["id"], attempt_id=None, cursor=None, limit=10)
    assert len(activity.events[0].tool_name) == 160
    assert activity.events[0].tool_use_id is None  # no ambiguous truncated identity
    assert activity.events[0].has_content is True
    assert "PRIVATE" not in activity.model_dump_json()
    inputs = await queries.usage_inputs(job["id"])
    assert "PRIVATE" not in json.dumps(inputs, default=str)
    projected = inputs["events"][0]["payload"]
    assert set(projected) == {"usage", "usage_id", "usage_scope", "has_text", "id", "tool_use_id"}
    assert projected["usage"] == {"output_tokens": 20}
    assert len(projected["usage_id"]) == 64
    assert len(projected["id"]) == 64
    assert projected["tool_use_id"] is None
    assert inputs["attempts"][0]["attempt_id"] == attempt


async def test_usage_projection_pairs_hashed_tools_without_fetching_their_content(db, store):
    job, attempt, _ = await _seed_attempt(db, store, grant_id=None)
    identity = "PRIVATE-TOOL-IDENTITY"
    await _event(db, store, job, attempt, 1, "tool_use", {"id": identity, "input": "PRIVATE-INPUT"})
    await _event(
        db,
        store,
        job,
        attempt,
        2,
        "tool_result",
        {"tool_use_id": identity, "text": "PRIVATE-RESULT"},
    )
    await _event(db, store, job, attempt, 3, "tool_use", {"id": {"hidden": "PRIVATE-OBJECT"}})
    inputs = await store.admin_runs.usage_inputs(job["id"])
    assert "PRIVATE" not in json.dumps(inputs, default=str)
    start, result, malformed = [event["payload"] for event in inputs["events"]]
    assert start["id"] == result["tool_use_id"]
    assert len(start["id"]) == 64
    assert malformed["id"] is None


async def test_numeric_projection_matches_owner_accounting_for_blocks_and_attempt_totals(db, store):
    job, first, _ = await _seed_attempt(db, store, grant_id=None)
    _, second, _ = await _seed_attempt(db, store, job=job, attempt_no=2, grant_id=None)
    shared = {
        "usage_id": "same-response",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_input_tokens": 8,
            "cache_creation_input_tokens": 2,
        },
    }
    for attempt in (first, second):
        for seq, kind in enumerate(("thinking", "tool_use", "message"), start=1):
            await _event(db, store, job, attempt, seq, kind, {**shared, "text": "PRIVATE"})
    await _event(
        db,
        store,
        job,
        second,
        4,
        "usage",
        {
            "usage_scope": "attempt",
            "usage": {
                "input_tokens": 30,
                "output_tokens": 6,
                "cache_read_input_tokens": 24,
                "cache_creation_input_tokens": 6,
            },
        },
    )
    queries = store.admin_runs
    admin_inputs = await queries.usage_inputs(job["id"])
    owner_inputs = await store.execution_queries.job_usage_inputs(job_id=job["id"])
    admin = summarize_job_usage(
        ledger_rows=[], runtime_inputs=admin_inputs, runtime="claude-code", terminal=True
    )
    owner = summarize_job_usage(
        ledger_rows=[], runtime_inputs=owner_inputs, runtime="claude-code", terminal=True
    )
    assert admin == owner
    assert admin.runtime.input_tokens == 40
    assert admin.runtime.output_tokens == 8
    assert admin.runtime.normalized_input_tokens == 80
    assert admin.ledger.totals is None
    assert "PRIVATE" not in json.dumps(admin_inputs, default=str)


async def test_resources_preserve_measurement_scope_and_never_read_another_jobs_attempt(db, store):
    job, attempt, _ = await _seed_attempt(db, store, grant_id=None)
    other, other_attempt, _ = await _seed_attempt(db, store, grant_id=None)
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_attempt_resources (job_id, attempt_id, sampler_version, coverage, process_observer, process_observation_complete) VALUES ($1, $2, 'shared-member-v1', 0, 'kata-guest', TRUE)",
            job["id"],
            attempt,
        )
        for seq, timing in ((1, "exact"), (2, "message")):
            await conn.execute(
                "INSERT INTO agent_tool_call_resources (attempt_id, call_seq, tool_use_id, tool_name, timing_source, duration_ms, process_count, process_cpu_usec) VALUES ($1, $2, $3, 'Bash', $4, 35, 1, 1200)",
                attempt,
                seq,
                f"tool-{seq}",
                timing,
            )
        await conn.execute(
            "INSERT INTO agent_process_observations (attempt_id, process_seq, uid, pid, tgid, comm, started_offset_ms, duration_ms, exit_code, observed_exit, tool_call_seqs, cpu_usec, mem_peak_bytes) VALUES ($1, 1, 1000, 42, 42, 'python', 5, 25, 0, TRUE, ARRAY[1], 1200, 4096)",
            attempt,
        )
    queries = store.admin_runs
    row = await queries.resources(job["id"], attempt)
    resources = _attempt_resources(row)
    assert resources.cpu_usec is None
    assert resources.coverage == 0
    assert resources.process_observation_complete is True
    assert [call.timing_source for call in resources.tool_calls] == ["exact", "message"]
    assert resources.tool_calls[0].process_cpu_usec == 1200
    assert resources.tool_calls[0].process_mem_peak_bytes is None
    assert resources.processes[0].cpu_usec == 1200
    assert resources.processes[0].mem_peak_bytes == 4096
    assert await queries.resources(job["id"], other_attempt) is None
    assert await queries.resources(other["id"], attempt) is None
    assert _attempt_resources(await queries.resources(other["id"], other_attempt)) is None


async def test_legacy_large_content_is_bounded_in_sql_and_real_http_reveal_is_audited(
    db, store, monkeypatch
):
    job, attempt, _ = await _seed_attempt(db, store, grant_id=None)
    other, other_attempt, _ = await _seed_attempt(db, store, grant_id=None)
    event = await _event(db, store, job, attempt, 1, "tool_result", {"text": '😀\n"' * 200_000})
    other_event = await _event(
        db, store, other, other_attempt, 1, "message", {"text": "PRIVATE-OTHER"}
    )
    queries = store.admin_runs
    fetched = await queries.event_content(job["id"], event)
    assert (
        len(json.dumps(fetched["payload"], ensure_ascii=False).encode()) <= MAX_EVENT_PAYLOAD_BYTES
    )
    assert fetched["payload"]["truncated"] is True
    assert fetched["payload"]["original_payload_bytes"] > MAX_EVENT_PAYLOAD_BYTES
    assert await queries.event_content(job["id"], other_event) is None

    monkeypatch.setenv("ADMIN_TOKEN", "db-admin-observation-test")
    app = FastAPI()
    app.include_router(admin_runs.router)
    app.dependency_overrides[get_storage] = lambda: store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/admin/agent/runs/{job['id']}/events/{event}/content",
            headers={"Authorization": "Bearer db-admin-observation-test"},
        )
    assert response.status_code == 200, response.text
    assert len(response.content) <= MAX_EVENT_PAYLOAD_BYTES
    assert response.json()["truncated"] is True
    assert response.headers["cache-control"] == "no-store"
    async with db.acquire() as conn:
        record = await conn.fetchrow(
            "SELECT target_user_id, details, success FROM agent_admin_audit_log WHERE details->>'job_id' = $1",
            job["id"],
        )
    assert record["target_user_id"] == job["user_id"]
    assert record["success"] is True
    assert load_json(record["details"]) == {
        "actor": "admin-token",
        "job_id": job["id"],
        "event_id": event,
        "attempt_id": attempt,
        "seq": 1,
    }
