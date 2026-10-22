"""Each job event stores its usage projection, and the usage reads use it.

A job's usage is read again each time a model call is counted, by every tab its
owner has open, and a thread's stats on every state change. Both project every
event's payload down to a few keys, so an event stores that projection when it
is inserted and those reads do not open the payload again. Rows stored before
the column existed, or under a projection changed since, are projected when
read, and both ways must give the same answer.
"""

from __future__ import annotations

import json

import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    pool as pool,
    store as store,
)

from cloud_agent.storage._sql.ledger import USAGE_SUMMARY_VERSION

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_OUTPUT = "x = 1\n" * 5000
_EVENTS = [
    (
        "message",
        {
            "text": "Looking.",
            "usage_id": "msg_1",
            "usage": {"input_tokens": 100, "output_tokens": 0, "cache_read_input_tokens": 50},
        },
    ),
    (
        "tool_use",
        {
            "name": "Edit",
            "id": "call-1",
            "input": {"file_path": "a.py", "new_string": _OUTPUT},
            "usage_id": "msg_1",
            "usage": {"input_tokens": 100, "output_tokens": 40},
        },
    ),
    ("tool_result", {"tool_use_id": "call-1", "is_error": False, "content": _OUTPUT}),
    ("thinking", {"text": "  \n ", "signature": "s" * 10_000}),
    ("usage", {"input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 1}),
    ("raw", {"text": "partial answer"}),
    ("lifecycle", None),
    (
        "message",
        {"text": "Done.", "usage_id": "msg_2", "usage": {"input_tokens": 5, "output_tokens": 9}},
    ),
]


async def _turn_with_events(store) -> dict:
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    for n, (event_type, payload) in enumerate(_EVENTS):
        assert (
            await store.attempt_commands.append_event(
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                event_type=event_type,
                payload=payload,
                event_seq=n + 1,
            )
            is not None
        )
    return job


async def _reads(store, pool, job: dict) -> tuple[dict, dict]:
    usage = await store.ledger.job_usage_reads(job_id=job["id"], user_id=job["user_id"])
    # A thread's stats reuse an attempt's stored rollup until its events change,
    # and these tests change only how the events are stored: drop the rollups,
    # so that each read reduces the events again.
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_attempt_thread_stats "
            "WHERE attempt_id IN (SELECT id FROM agent_attempts WHERE job_id = $1)",
            job["id"],
        )
    thread = await store.execution_queries.thread_stats(job_id=job["id"], user_id=job["user_id"])
    assert usage is not None and thread is not None
    return usage["inputs"], thread


async def test_an_appended_event_stores_what_a_read_would_project(store, pool):
    job = await _turn_with_events(store)
    async with pool.acquire() as conn:
        stored = await conn.fetch(
            "SELECT event_type, usage_summary FROM agent_job_events WHERE job_id = $1 ORDER BY id",
            job["id"],
        )
    assert len(stored) == len(_EVENTS)
    for row in stored:
        summary = json.loads(row["usage_summary"])
        assert summary.pop("_usage_projection") == USAGE_SUMMARY_VERSION
        # Only what the summaries read: never the bulky fields.
        assert not {"text", "input", "content", "signature", "name"} & summary.keys(), row
    from_summaries = await _reads(store, pool, job)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_job_events SET usage_summary = NULL WHERE job_id = $1", job["id"]
        )
    from_payloads = await _reads(store, pool, job)

    assert from_summaries == from_payloads
    usage_events = from_summaries[0]["events"]
    assert [event["payload"]["has_text"] for event in usage_events] == [
        True,
        False,
        False,
        False,
        False,
        True,
        False,
        True,
    ]


async def test_a_summary_stored_under_another_projection_is_not_trusted(store, pool):
    job = await _turn_with_events(store)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_job_events SET usage_summary = NULL WHERE job_id = $1", job["id"]
        )
    projected = await _reads(store, pool, job)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_job_events
            SET usage_summary = jsonb_build_object(
                '_usage_projection', 'an-older-projection',
                'usage', jsonb_build_object('input_tokens', 999999)
            )
            WHERE job_id = $1
            """,
            job["id"],
        )

    assert await _reads(store, pool, job) == projected
