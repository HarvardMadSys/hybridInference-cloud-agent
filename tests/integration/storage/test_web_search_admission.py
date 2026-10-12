"""Cross-replica admission for the shared Web Search surface, against Postgres."""

from __future__ import annotations

import asyncio

import pytest
from tests.integration.storage.test_credential_queries_store import (
    _live,
    db as db,
    store as store,
)

from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


def _fence(job, claim) -> dict[str, object]:
    """The authenticated fields one MCP call presents to admission."""
    return {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }


async def test_two_replicas_admit_only_four_concurrent_searches(db, store: Storage) -> None:
    """The Postgres lock makes the concurrency ceiling deployment-wide."""
    job, claim = await _live(store)
    replica = Storage(db)

    decisions = await asyncio.gather(
        *[
            (store if index % 2 else replica).web_search.acquire_web_search(**_fence(job, claim))
            for index in range(8)
        ]
    )

    admitted = [decision for decision in decisions if decision.reason == "admitted"]
    refused = [decision for decision in decisions if decision.reason != "admitted"]
    assert len(admitted) == 4
    assert {decision.reason for decision in refused} == {"concurrency_limited"}
    await asyncio.gather(
        *[
            store.web_search.release_web_search(decision.call_id)
            for decision in admitted
            if decision.call_id is not None
        ]
    )


async def test_release_frees_concurrency_without_refunding_the_user_rate(
    store: Storage,
) -> None:
    """Ten completed calls still consume the sliding-window abuse ceiling."""
    job, claim = await _live(store)
    fence = _fence(job, claim)

    for _ in range(10):
        admitted = await store.web_search.acquire_web_search(**fence)
        assert admitted.reason == "admitted"
        assert admitted.call_id is not None
        assert await store.web_search.release_web_search(admitted.call_id)

    limited = await store.web_search.acquire_web_search(**fence)
    assert limited.call_id is None
    assert limited.reason == "user_rate_limited"


async def test_an_expired_concurrency_lease_frees_capacity(db, store: Storage) -> None:
    """A crashed relay cannot hold one of the four provider slots forever."""
    job, claim = await _live(store)
    fence = _fence(job, claim)
    first = [await store.web_search.acquire_web_search(**fence) for _ in range(4)]
    assert all(decision.reason == "admitted" for decision in first)
    blocked = await store.web_search.acquire_web_search(**fence)
    assert blocked.reason == "concurrency_limited"

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_web_search_admissions "
            "SET admitted_at = admitted_at - INTERVAL '31 seconds', "
            "active_until = active_until - INTERVAL '31 seconds'"
        )

    replacement = await store.web_search.acquire_web_search(**fence)
    assert replacement.reason == "admitted"


async def test_the_global_window_caps_the_shared_web_search_surface(db, store: Storage) -> None:
    """Many individually quiet users cannot exceed the deployment-wide rate."""
    job, claim = await _live(store)
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_web_search_admissions (
                call_id, user_id, job_id, attempt_id, lease_generation,
                admitted_at, active_until
            )
            SELECT md5('global-search-' || sequence::text),
                   'other-user-' || sequence::text,
                   $1, $2, $3, NOW(), NOW()
            FROM generate_series(1, 60) AS series(sequence)
            """,
            job["id"],
            claim["attempt_id"],
            claim["lease_generation"],
        )

    limited = await store.web_search.acquire_web_search(**_fence(job, claim))

    assert limited.call_id is None
    assert limited.reason == "global_rate_limited"


async def test_a_stale_attempt_fence_cannot_consume_search_capacity(store: Storage) -> None:
    """Admission repeats the MCP endpoint's fence inside its own transaction."""
    job, claim = await _live(store)

    refused = await store.web_search.acquire_web_search(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"] + 1,
    )

    assert refused.call_id is None
    assert refused.reason == "stale_fence"
