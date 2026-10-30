"""Control-plane instance reports and unidentified sessions on a real database (WP4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from tests.integration.storage.test_schema_upgrade import _db_config, store as store

from cloud_agent.instances import fleet_supports
from cloud_agent.storage.repositories.instances import InstanceRepository

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _report(repo: InstanceRepository, instance_id: str, **overrides) -> None:
    values = {
        "instance_id": instance_id,
        "role": "api",
        "machine": "cp-1",
        "build_sha": "a" * 40,
        "capabilities": ["instance-report-v1", "model-connections-v1"],
        "adopted_settings_revision": 4,
        "restart_required": [],
        "details": {"root_key_source": "dedicated", "root_key_id": "0123456789abcdef"},
        "started_at": datetime.now(UTC),
    }
    values.update(overrides)
    await repo.report(**values)


async def test_reports_are_upserted_listed_and_stopped(store) -> None:
    config = _db_config()
    named = await asyncpg.create_pool(
        **config, min_size=1, max_size=1, server_settings={"application_name": "cloud-agent:api:x"}
    )
    try:
        repo = InstanceRepository(named)
        await _report(repo, "1" * 32)
        await _report(repo, "1" * 32, adopted_settings_revision=5, restart_required=["k"])
        await _report(repo, "2" * 32, role="mcp", capabilities=["instance-report-v1"])
        rows = {row["instance_id"]: row for row in await repo.list_instances()}
        assert rows["1" * 32]["adopted_settings_revision"] == 5
        assert rows["1" * 32]["restart_required"] == ["k"]
        assert rows["1" * 32]["current"] is True
        assert rows["1" * 32]["details"]["root_key_id"] == "0123456789abcdef"
        await repo.stop("2" * 32)
        rows = {row["instance_id"]: row for row in await repo.list_instances()}
        assert rows["2" * 32]["current"] is False
        assert rows["2" * 32]["stopped_at"] is not None
    finally:
        await named.close()


async def test_a_stale_report_and_an_unnamed_session_both_fail_the_fleet_check(store) -> None:
    config = _db_config()
    named = await asyncpg.create_pool(
        **config, min_size=1, max_size=1, server_settings={"application_name": "cloud-agent:api:y"}
    )
    try:
        repo = InstanceRepository(named)
        await _report(repo, "3" * 32)
        # An unnamed session, like one an older release opens.
        unnamed = await asyncpg.connect(**config)
        try:
            await unnamed.execute("SELECT 1")
            sessions = await repo.unidentified_sessions()
            assert sessions, "an unnamed session must be listed"
            assert all(not s["application_name"].startswith("cloud-agent:") for s in sessions)
            verdict = await fleet_supports(repo, "model-connections-v1")
            assert verdict.supported is False
            assert "database session" in verdict.reason()
        finally:
            await unnamed.close()

        # A process that stopped reporting without stopping cleanly is stale.
        async with named.acquire() as conn:
            await conn.execute(
                "UPDATE agent_control_plane_instances SET last_seen_at = $2 WHERE instance_id = $1",
                "3" * 32,
                datetime.now(UTC) - timedelta(minutes=2),
            )
        verdict = await fleet_supports(repo, "model-connections-v1")
        assert verdict.supported is False
        assert verdict.stale
    finally:
        await named.close()
