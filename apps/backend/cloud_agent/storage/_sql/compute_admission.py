"""Compute admission fences shared by claim, Fork, binding, and retirement.

Lock order is policy, shared/exclusive topology fence, then domain rows. Shared
readers remain concurrent; rare binding/retirement changes take the exclusive
fence. A global fence deliberately avoids racing a Host's allocation lookup
against rebinding and does not hold any lock over a provider or SSH call.
"""

from __future__ import annotations

from typing import Any

from cloud_agent.storage._sql.rows import load_json

_COMPUTE_FENCE = 597366831493327721


async def lock_compute_topology(conn: Any, *, exclusive: bool = False) -> None:
    """Call before domain row locks; the policy lock must already be held."""
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await conn.execute(f"SELECT {function}($1::bigint)", _COMPUTE_FENCE)


async def lock_compute_entry(conn: Any, *, exclusive: bool = False) -> None:
    """Enter a resource-aware transaction that does not already lock policy."""
    await conn.execute("SELECT id FROM agent_runner_policy WHERE id FOR SHARE")
    await lock_compute_topology(conn, exclusive=exclusive)


async def host_compute_admission(conn: Any, host: str) -> tuple[bool, bool]:
    """Return (may_create_group, may_claim) using DB time and durable holds.

    Hard deadlines work when the background worker is stopped. Unknown/stale
    resource observations pause new groups; existing retained turns are allowed
    only until a known deadline/explicit all-claims restriction. No host status
    is changed, so renewal cannot undo an administrator's Drain.
    """
    row = await conn.fetchrow(
        """
        SELECT a.*, clock_timestamp() AS database_now,
               EXISTS (SELECT 1 FROM agent_host_admission_holds x WHERE x.host=h.host) AS held,
               EXISTS (SELECT 1 FROM agent_host_admission_holds x WHERE x.host=h.host
                       AND x.scope='all_claims') AS all_held
        FROM agent_runner_hosts h
        LEFT JOIN agent_compute_nodes n ON n.id=h.compute_node_id
        LEFT JOIN agent_compute_allocations a ON a.id=n.allocation_id
        WHERE h.host=$1
    """,
        host,
    )
    if row is None:
        return False, False
    if row["id"] is None:
        return not row["held"], not row["all_held"]
    policy = load_json(row["policy"]) or {}
    now = row["database_now"]
    new = (
        not row["held"]
        and row["desired_state"] == "present"
        and row["scope_complete"]
        and row["observed_state"] in ("ready", "active", "running")
    )
    claims = not row["all_held"] and row["release_committed_at"] is None
    if row["observed_state"] in ("terminated", "absent", "failed"):
        return False, False
    if row["observation_error"]:
        new = False
    if (row["lease_managed"] and not row["expiration_known"]) or row["observed_at"] is None:
        new = False
    if row["observed_at"] is not None and (now - row["observed_at"]).total_seconds() > 300:
        new = False
    if row["expires_at"] is not None:
        remaining = (row["expires_at"] - now).total_seconds()
        if remaining <= max(0, float(policy.get("stop_new_groups_before_seconds", 1800))):
            new = False
        if remaining <= max(0, float(policy.get("stop_claims_before_seconds", 300))):
            claims = False
    return new and claims, claims
