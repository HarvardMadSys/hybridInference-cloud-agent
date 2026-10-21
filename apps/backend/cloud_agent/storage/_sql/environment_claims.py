"""Lease one queued environment run to the host that asked for it.

Extracted from ``environments.py``, which the module-size ratchet (#771) does
not let grow, so the claim path lives in its own module and that one keeps the
headroom it needs for the rest of the repository.

Every idle environment-capable host claims at least every five seconds, and
the maintenance pass repeats the queued scan every thirty. The empty-queue case
is therefore the common one, and it must cost one round trip and take no lock:
migration ``0091`` adds the partial index the scan reads, and the pre-check
below answers an empty queue before the transaction that locks the host row,
which conflicts with that same host's heartbeat (issue #775). The locked path
still re-checks the queued filter itself, so the pre-check is an optimization
and never a correctness dependency.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from cloud_agent.execution_profiles import shared_sandbox_claim_attested
from cloud_agent.storage._sql.compute_admission import (
    host_compute_admission,
    lock_compute_topology,
)

if TYPE_CHECKING:
    from collections.abc import Callable


async def claim_environment_run(
    pool: Any,
    host: str,
    credential_hash: str,
    attestation: dict[str, Any],
    *,
    snapshot_capable: bool = False,
    run_view: Callable[[Any], dict[str, Any]],
) -> dict[str, Any] | None:
    """Lease a test, or a prebuild to a snapshot-capable host, within slot capacity."""
    if not shared_sandbox_claim_attested(
        attestation, expected_deployment_sha=os.environ.get("AGENT_DEPLOYMENT_SHA")
    ):
        return None
    kata = (
        attestation.get("sandbox_runtime") == "io.containerd.kata.v2"
        and bool(attestation.get("guest_kernel"))
        and attestation.get("guest_kernel") != attestation.get("host_kernel")
    )
    # Advisory emptiness check before the locking transaction (issue #775):
    # the common empty case must never take the host-row lock, which conflicts
    # with the same host's heartbeat. The locked path still re-checks the
    # queued filter itself.
    async with pool.acquire() as conn:
        if not await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM agent_environment_runs WHERE state='queued')"
        ):
            return None
    async with pool.acquire() as conn, conn.transaction():
        policy = await conn.fetchrow(
            "SELECT active_host FROM agent_runner_policy WHERE id FOR SHARE"
        )
        await lock_compute_topology(conn)
        resource_new, resource_claims = await host_compute_admission(conn, host)
        if (
            not resource_new
            or not resource_claims
            or (policy and policy["active_host"] not in (None, host))
        ):
            return None
        host_row = await conn.fetchrow(
            "SELECT * FROM agent_runner_hosts WHERE host=$1 FOR UPDATE", host
        )
        if (
            host_row is None
            or host_row["status"] != "online"
            or host_row["credential_hash"] != credential_hash
        ):
            return None
        if await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM agent_environment_runs WHERE host=$1 AND NOT cleanup_complete "
            "AND (lease_expires_at<=NOW() OR started_at<=NOW()-INTERVAL '30 minutes'))",
            host,
        ):
            return None
        tests = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_environment_runs WHERE host=$1 AND NOT cleanup_complete AND lease_expires_at>NOW() AND started_at>NOW()-INTERVAL '30 minutes'",
                host,
            )
        )
        used = int(host_row["slots_used"]) + tests
        if any(
            host_row.get(k) is not None and used >= int(host_row[k])
            for k in ("slots_total", "slots_desired")
        ):
            return None
        # A prebuild whose snapshot was deleted since would install for nothing.
        row = await conn.fetchrow(
            "SELECT * FROM agent_environment_runs WHERE state='queued' AND (execution_profile='trusted-v1' OR $1) "
            "AND (purpose='test' OR ($2 AND EXISTS (SELECT 1 FROM agent_environment_snapshots s "
            "WHERE s.id=agent_environment_runs.snapshot_id AND s.state IN ('queued','building')))) "
            "ORDER BY purpose='prebuild', created_at FOR UPDATE SKIP LOCKED LIMIT 1",
            kata,
            snapshot_capable,
        )
        if row is None:
            return None
        claimed = await conn.fetchrow(
            "UPDATE agent_environment_runs SET state='running',host=$2,lease_generation=lease_generation+1,lease_expires_at=NOW()+INTERVAL '120 seconds',started_at=NOW(),updated_at=NOW() WHERE id=$1 RETURNING *",
            row["id"],
            host,
        )
        return run_view(claimed)
