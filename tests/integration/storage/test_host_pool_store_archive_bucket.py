"""The archive policy keeps another bucket in PostgreSQL, and stores no default."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.integration.storage.test_host_pool_store import (
    _archive_settings,
    db as db,
    hosts as hosts,
)

from cloud_agent_shared.sandbox_archive_settings import SANDBOX_ARCHIVE_S3_URI

if TYPE_CHECKING:
    from cloud_agent.storage.repositories.host_pool import PostgresHostPoolRepository

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_archive_policy_keeps_another_bucket_and_stores_no_default(
    db, hosts: PostgresHostPoolRepository
):
    """A configured bucket survives the round trip; the default is never stored.

    So a row saved before the bucket was a setting reads the same as one saved
    after it.
    """
    uri = "s3://example-archives/cloud-agent/sandbox"
    changed = await hosts.set_sandbox_archive_settings(
        expected_revision=0, settings=_archive_settings(s3_uri=uri)
    )
    assert changed is not None and changed["s3_uri"] == uri
    assert (await hosts.sandbox_archive_settings())["s3_uri"] == uri

    back = await hosts.set_sandbox_archive_settings(
        expected_revision=1, settings=_archive_settings(s3_uri=SANDBOX_ARCHIVE_S3_URI)
    )
    assert back is not None and back["revision"] == 2
    assert back["s3_uri"] == SANDBOX_ARCHIVE_S3_URI
    async with db.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT sandbox_archive_settings ? 's3_uri' FROM agent_runner_policy WHERE id"
        )
    assert stored is False
