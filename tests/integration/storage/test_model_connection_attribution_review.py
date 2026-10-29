"""Usage readers retain the stored provider source, including before any attempt."""

import pytest
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    pool as pool,
    store as store,
)

from cloud_agent.storage.admin_queries import AdminRunQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_admin_and_conversation_usage_queries_keep_personal_source(store, pool):
    connection_id = "mconn_" + "a" * 32
    job = await _create_job(
        store, runtime="opencode", model="same-model", model_connection_id=connection_id
    )
    admin = await AdminRunQueries(pool).get_run(job["id"])
    assert admin is not None
    assert admin.get("model_connection_id") == connection_id
    owner = await store.ledger.job_usage_reads(job_id=job["id"], user_id=job["user_id"])
    assert owner is not None
    assert owner["job"]["model_connection_id"] == connection_id
    assert len(owner["conversation_rows"]) == 1
    assert owner["conversation_rows"][0].get("model_connection_id") == connection_id
