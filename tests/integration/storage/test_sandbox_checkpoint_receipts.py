"""Real Postgres checks for host-reported sandbox checkpoint receipts."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.integration.storage.test_admin_sandbox_queries import seed
from tests.integration.storage.test_attempt_ledger import db as db, store as store

from cloud_agent.storage.admin_sandbox_queries import AdminSandboxQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_CAPTURED = datetime(2026, 10, 13, 12, tzinfo=UTC)


def _checkpoint(sandbox: str, **changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "checkpoint_id": "c" * 32,
        "sandbox_id": sandbox,
        "generation": 1,
        "container_id": f"container-{sandbox}",
        "base_image": "sha256:" + "b" * 64,
        "runtime_id": "claude-code",
        "runtime_version": "2.1.220",
        "object_key": "cloud-agent/sandbox/v2/" + "d" * 64 + "/g1/" + "c" * 32 + ".oci.tar",
        "version_id": "version-1",
        "sha256": "e" * 64,
        "size_bytes": 4096,
        "new_layer_bytes": 2048,
        "layer_count": 3,
        "created_at": _CAPTURED.isoformat(),
        "members": [{"thread_id": "athr", "last_settled_job_id": "ajob"}],
        "durations_ms": {"commit": 10, "export": 20, "upload": 30},
        "format_version": 1,
    }
    values.update(changes)
    return values


async def test_a_receipt_is_recorded_once_for_the_groups_current_host(db, store):
    job = await seed(db, store, "asbx_receipt")

    first = await store.sandbox.record_sandbox_checkpoint(
        host="runner-one", thread_id=job["thread_id"], checkpoint=_checkpoint("asbx_receipt")
    )
    repeated = await store.sandbox.record_sandbox_checkpoint(
        host="runner-one", thread_id=job["thread_id"], checkpoint=_checkpoint("asbx_receipt")
    )

    assert first is True
    assert repeated is False
    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM agent_sandbox_checkpoints")
    assert len(rows) == 1
    assert rows[0]["host"] == "runner-one"
    assert rows[0]["state"] == "reported"
    assert rows[0]["captured_at"] == _CAPTURED
    assert rows[0]["size_bytes"] == 4096


@pytest.mark.parametrize(
    ("host", "changes"),
    [
        ("runner-two", {}),
        ("runner-one", {"generation": 2}),
        ("runner-one", {"container_id": "container-replaced"}),
        ("runner-one", {"sandbox_id": "asbx_other"}),
    ],
)
async def test_a_stale_or_foreign_receipt_is_ignored(db, store, host, changes):
    job = await seed(db, store, "asbx_receipt")

    recorded = await store.sandbox.record_sandbox_checkpoint(
        host=host,
        thread_id=job["thread_id"],
        checkpoint=_checkpoint("asbx_receipt", **changes),
    )

    assert recorded is False
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_sandbox_checkpoints") == 0


async def test_admin_views_show_the_newest_checkpoint(db, store):
    job = await seed(db, store, "asbx_receipt")
    for index, captured in enumerate((_CAPTURED, _CAPTURED + timedelta(hours=1))):
        await store.sandbox.record_sandbox_checkpoint(
            host="runner-one",
            thread_id=job["thread_id"],
            checkpoint=_checkpoint(
                "asbx_receipt",
                checkpoint_id=f"{index}" * 32,
                created_at=captured.isoformat(),
                size_bytes=1000 + index,
            ),
        )
    queries = AdminSandboxQueries(db)

    detail = await queries.detail("asbx_receipt")
    trajectory = await queries.trajectory("asbx_receipt", cursor=None, limit=10)

    assert detail is not None and trajectory is not None
    for latest in (detail.latest_checkpoint, trajectory.latest_checkpoint):
        assert latest is not None
        assert latest.id == "1" * 32
        assert latest.size_bytes == 1001
        assert latest.durations_ms == {"commit": 10, "export": 20, "upload": 30}


async def test_a_sandbox_without_checkpoints_reports_none(db, store):
    await seed(db, store, "asbx_receipt")

    detail = await AdminSandboxQueries(db).detail("asbx_receipt")

    assert detail is not None
    assert detail.latest_checkpoint is None
