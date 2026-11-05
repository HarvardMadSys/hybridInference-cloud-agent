"""An empty queue claims back immediately, before the locking transaction.

The advisory pre-check (issue #775) must answer the common empty case without
taking the host-row lock, which conflicts with the same host's heartbeat. The
honest observation: hold the host row FOR UPDATE in another session, then ask
a host with no queued runs to claim. Before the pre-check the claim waits for
the row lock; after it, it answers None without waiting.
"""

import asyncio

import pytest
from tests.integration.storage.test_agent_job_store import (  # noqa: F401
    _SHARED_HOST_CAPABILITIES,
    _shared_claim_attestation,
    pool,
    store,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_an_empty_queue_answers_before_the_host_row_lock(store, pool):  # noqa: F811
    await store.host_pool.register_host(
        host="environment-host",
        credential_hash="credential",
        capabilities=[*_SHARED_HOST_CAPABILITIES, "environment-v1"],
        slots_total=1,
    )
    await store.host_pool.record_host_heartbeat(
        host="environment-host",
        capabilities=[*_SHARED_HOST_CAPABILITIES, "environment-v1"],
        sandbox_image_digest="sha256:shared-image",
    )
    async with (
        pool.acquire() as blocker,
        blocker.transaction(),
    ):
        await blocker.execute(
            "SELECT * FROM agent_runner_hosts WHERE host='environment-host' FOR UPDATE"
        )
        claiming = asyncio.ensure_future(
            store.environments.claim_environment_run(
                "environment-host", "credential", _shared_claim_attestation()
            )
        )
        await asyncio.sleep(0.3)
        assert claiming.done(), "the empty-queue claim must answer before the host-row lock"
        assert await claiming is None
