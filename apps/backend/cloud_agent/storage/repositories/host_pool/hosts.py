"""An administrator's changes to a host: status, drain, slots, removal and forgetting."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent.domain.host_pool import (
    HOST_DISABLED,
    HOST_DRAINING,
    HOST_ENROLLING,
    HOST_OFFLINE,
    HOST_ONLINE,
    transition_allowed,
)
from cloud_agent.storage._sql.compute_admission import (
    host_compute_admission,
    lock_compute_entry,
    lock_compute_topology,
)
from cloud_agent.storage._sql.hosts import (
    COVERED_GROUP_SQL,
    HOST_RETAINED_GROUP,
    lock_revocation_rows,
    release_late_covered_sandboxes,
    revoke_host_credentials,
)
from cloud_agent.storage._sql.move_out import (
    MOVABLE_GROUP_SQL,
    UNMOVABLE_GROUP_SQL,
    UNMOVED_GROUP_SQL,
    move_out_possible,
)
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.repositories.host_pool._common import (
    HostPoolBase,
    _changed_settings,
    logger,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any


#: A removal that moves groups out still waits on host ``h``: for a group that
#: can still move, or for the administrator's word on one that never will.
_REMOVAL_WAITS_SQL = (
    "h.removal_moves_out AND ("
    "EXISTS (SELECT 1 FROM agent_sandboxes g WHERE g.host = h.host AND "
    + MOVABLE_GROUP_SQL
    + ") OR (NOT h.removal_leaves_unmoved AND "
    "EXISTS (SELECT 1 FROM agent_sandboxes g WHERE g.host = h.host AND "
    + UNMOVABLE_GROUP_SQL
    + ")))"
)


class HostAdminMixin(HostPoolBase):
    """Change, drain, remove and forget hosts."""

    async def set_host_status(self, *, host: str, status: str, force: bool = False) -> str:
        """Move a host to ``status``, refusing transitions the table forbids.

        Returns ``"ok"``, ``"unknown"``, ``"invalid"`` or ``"fenced"``. The table is consulted
        inside the transaction that writes, on a locked row: checking it in the
        caller means an admin draining a host and the reaper marking it offline
        can both read ``online`` and both write, and the loser's decision is the
        one that sticks.

        Leaving ``draining`` cancels a pending removal: putting a host back to
        work is the operator taking that request back.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, drain_fence_active FROM agent_runner_hosts "
                "WHERE host = $1 FOR UPDATE",
                host,
            )
            if row is None:
                return "unknown"
            current = row["status"]
            if row["drain_fence_active"]:
                if current == HOST_DRAINING and status == HOST_DRAINING:
                    return "ok"
                if not force:
                    return "fenced"
            if not transition_allowed(current, status):
                return "invalid"
            await conn.execute(
                "UPDATE agent_runner_hosts SET status = $2, drain_fence_token = NULL, "
                "drain_fence_status_before = NULL, drain_fence_active = FALSE, "
                f"removal_requested_at = CASE WHEN $2 = '{HOST_DRAINING}' "
                "THEN removal_requested_at END WHERE host = $1",
                host,
                status,
            )
        return "ok"

    async def acquire_host_drain_fence(
        self,
        *,
        host: str,
        expected_fence_token: Any | None,
        expected_fence_epoch: Any | None,
        fence_token: Any,
    ) -> tuple[str, str | None, Any | None]:
        """CAS one deployment drain under the host row lock."""
        expected_token = None if expected_fence_token is None else str(expected_fence_token)
        expected_epoch = None if expected_fence_epoch is None else str(expected_fence_epoch)
        requested = str(fence_token)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, drain_fence_token, drain_fence_status_before, "
                "drain_fence_active, drain_fence_epoch "
                "FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if row is None:
                return "unknown", None, None
            current_token = (
                None if row["drain_fence_token"] is None else str(row["drain_fence_token"])
            )
            current_epoch = row["drain_fence_epoch"]
            if current_token == requested:
                if row["drain_fence_active"] and row["status"] == HOST_DRAINING:
                    return "ok", row["drain_fence_status_before"], current_epoch
                if not row["drain_fence_active"]:
                    return "released", None, current_epoch
                return "invalid", None, current_epoch
            if current_token != expected_token or str(current_epoch) != expected_epoch:
                return "stale", None, current_epoch
            if row["status"] == HOST_DRAINING and not row["drain_fence_active"]:
                return "manual", None, current_epoch
            if row["status"] not in (HOST_ONLINE, HOST_DRAINING):
                return "invalid", None, current_epoch
            status_before = row["status"]
            next_epoch = await conn.fetchval(
                "UPDATE agent_runner_hosts SET status = $2, drain_fence_token = $3::uuid, "
                "drain_fence_status_before = $4, drain_fence_active = TRUE WHERE host = $1 "
                "RETURNING drain_fence_epoch",
                host,
                HOST_DRAINING,
                requested,
                status_before,
            )
        return "ok", status_before, next_epoch

    async def release_host_drain_fence(
        self, *, host: str, fence_token: Any
    ) -> tuple[str, Any | None]:
        """Release only the exact active owner, retaining its idempotency receipt."""
        requested = str(fence_token)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, drain_fence_token, drain_fence_active, drain_fence_epoch "
                "FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if row is None:
                return "unknown", None
            current_epoch = row["drain_fence_epoch"]
            current_token = (
                None if row["drain_fence_token"] is None else str(row["drain_fence_token"])
            )
            if current_token != requested:
                return "stale", current_epoch
            if row["drain_fence_active"]:
                if row["status"] != HOST_DRAINING:
                    return "invalid", current_epoch
                next_epoch = await conn.fetchval(
                    "UPDATE agent_runner_hosts SET status = $2, drain_fence_active = FALSE, "
                    "removal_requested_at = NULL WHERE host = $1 RETURNING drain_fence_epoch",
                    host,
                    HOST_ONLINE,
                )
                return "ok", next_epoch
            return "ok", current_epoch

    async def set_host_slots_desired(self, *, host: str, slots: int | None) -> str:
        """Ask a host to run ``slots`` turns at once, or with ``None`` its default.

        Returns ``"ok"``, ``"unknown"`` or ``"autoscaling_managed"``. Nothing
        here starts or stops anything: the host reads the value off its next heartbeat answer,
        serves it as its limit, and reports it back as ``slots_total``. The
        claim honours the lower of the two from the next poll on, so a number
        the host has not confirmed admits nothing extra and a lowered one takes
        effect at once. Turns already running are never touched.

        The value survives heartbeats and re-enrollment on purpose. It is a
        decision about the named machine, and neither the heartbeat's
        ``COALESCE`` nor the registration upsert names this column. Bounds are
        the request schema's business; the column refuses only a non-positive
        size, which is not a size.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn)
            current = await conn.fetchrow(
                """SELECT h.host,
                          EXISTS (
                              SELECT 1 FROM agent_compute_nodes n
                              JOIN agent_cloudlab_autoscaling_members m
                                ON m.allocation_id=n.allocation_id
                              WHERE n.id=h.compute_node_id AND m.state<>'released'
                          ) AS autoscaling_managed
                   FROM agent_runner_hosts h WHERE h.host=$1 FOR UPDATE OF h""",
                host,
            )
            if current is None:
                return "unknown"
            if current["autoscaling_managed"]:
                return "autoscaling_managed"
            await conn.execute(
                "UPDATE agent_runner_hosts SET slots_desired=$2 WHERE host=$1",
                host,
                slots,
            )
        return "ok"

    async def set_runner_host_settings(
        self, *, host: str, changes: Mapping[str, int | None]
    ) -> dict[str, Any] | None:
        """Set or clear runtime settings for one server, over the global values.

        ``changes`` names only the settings to change, and ``None`` clears one
        so the global value, or the built-in default, applies again. Settings it does
        not name keep their values. The host applies the result from its next
        heartbeat answer.

        Like ``slots_desired``, the values survive heartbeats and
        re-enrollment: they are a decision about the named machine. Validation
        is the caller's (``cloud_agent_shared.host_settings``). The row is
        locked for the read-modify-write, so two admins changing different
        settings on the same server both land.

        Returns the server's values before the change, or ``None`` for an
        unknown host.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow(
                "SELECT settings_desired FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if current is None:
                return None
            previous = dict(load_json(current["settings_desired"]) or {})
            await conn.execute(
                "UPDATE agent_runner_hosts SET settings_desired = $2::jsonb, "
                "settings_import_excluded = ARRAY(SELECT DISTINCT key FROM "
                "unnest(settings_import_excluded || $3::text[]) AS touched(key)) "
                "WHERE host = $1",
                host,
                json.dumps(_changed_settings(previous, changes)),
                list(changes),
            )
        return previous

    async def remove_host(
        self, *, host: str, force: bool = False, allow_unmoved: bool = False
    ) -> str:
        """Take a host out of the pool, moving its groups out first when they can.

        Returns ``"ok"``, ``"unknown"``, ``"fenced"``, ``"moving_out"``,
        ``"draining"``, ``"no_move_target"``, ``"cannot_move"`` or ``"busy"``.

        A stopped group its checkpoint covers moves on revocation. When other
        groups can move -- checkpoints work, another host would restore them,
        and each can still stop into a checkpoint (``MOVABLE_GROUP_SQL``) --
        the host moves to ``draining`` and moves out (``"moving_out"``): it
        claims nothing, its idle groups stop into checkpoints and are handed
        over one at a time, and :meth:`finish_host_removals` revokes it once
        none is left and its last running turn has ended. Nothing running is
        interrupted. A host still running turns moves out whenever it can,
        even with no group to stop yet: a turn restoring a group, or one that
        claimed a covered group before the removal, leaves that group running
        here once it ends.

        A removal that would leave groups bound to the host needs
        ``allow_unmoved``: ``"no_move_target"`` when groups could move but no
        host or checkpoint is there to take them, ``"cannot_move"`` when some
        never can (``UNMOVABLE_GROUP_SQL``: a stopped group whose checkpoint
        failed, say). With it, whatever can move still moves first; then a
        host still holding slots drains (``"draining"``) and is revoked once
        its running attempts end, and an idle one is revoked now. A group that
        stops moving during the move, as when its checkpoint fails, holds the
        removal until the administrator repeats it with ``allow_unmoved``.
        ``"busy"`` is left for the busy host that cannot drain at all.

        ``force`` ends that work now instead of waiting for it — lease expiry
        *is* the recovery path, and the revocation brings it forward to now
        (see ``revoke_host_credentials``): the attempts' worker tokens name no
        host, so a lease left to run would let the removed machine keep
        renewing it. A retained activation cannot be replayed safely after its
        host disappears, so its expiry marks that chat LOST and fails
        already-queued descendants instead. Covered stopped groups still move.

        Disabling rather than deleting. The row is what
        ``agent_attempts.host_id`` points at, and what an operator reads when
        asking where a job ran; #1158's ``forget`` is still there for the
        machine that is genuinely gone. Removing or forgetting the host row is
        administrative bookkeeping; it is not by itself proof that a retained
        Docker writable layer was deleted.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # The fence a move-out is decided under, before any row: two hosts
            # removed together cannot each count on the other as a target.
            await lock_compute_entry(conn, exclusive=True)
            # The host's running attempts before its row: the order every
            # writer that ends an attempt keeps (move the attempt, then give
            # its slot back). Expiring those leases while holding the host row
            # first would deadlock against an attempt finishing at that moment.
            await conn.execute(
                "SELECT id FROM agent_attempts WHERE host_id = $1 AND status = 'running' "
                "ORDER BY id FOR UPDATE",
                host,
            )
            # Then its groups, which the counts below read and the revocation
            # hands over: a start fenced in between would slip out of both.
            await lock_revocation_rows(conn, [host])
            row = await conn.fetchrow(
                "SELECT status, slots_used, drain_fence_active, removal_requested_at, "
                "removal_moves_out, removal_leaves_unmoved "
                "FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if row is None:
                return "unknown"
            if row["drain_fence_active"] and not force:
                return "fenced"
            if force:
                await revoke_host_credentials(conn, [host])
                return "ok"
            # Covered stopped groups move on revocation; movable ones need
            # stopping into a checkpoint first; unmovable ones would stay here.
            groups = await conn.fetchrow(
                f"""
                SELECT count(*) FILTER (WHERE {MOVABLE_GROUP_SQL} AND NOT {COVERED_GROUP_SQL})
                           AS to_stop,
                       count(*) FILTER (WHERE {UNMOVABLE_GROUP_SQL}) AS unmovable
                FROM agent_sandboxes g WHERE g.host = $1 AND {UNMOVED_GROUP_SQL}
                """,
                host,
            )
            leaves_unmoved = allow_unmoved
            if row["removal_requested_at"] is not None and row["removal_moves_out"]:
                # Already moving out: a repeat may only give the word for the
                # groups that will never move.
                leaves_unmoved = allow_unmoved or row["removal_leaves_unmoved"]
                if groups["unmovable"] and not leaves_unmoved:
                    return "cannot_move"
                await conn.execute(
                    "UPDATE agent_runner_hosts SET removal_leaves_unmoved = $2 WHERE host = $1",
                    host,
                    leaves_unmoved,
                )
                return "moving_out"
            # A running turn may leave a group running here that this count
            # did not see: one it is restoring, or a covered one it claimed
            # before the removal and starts after it. The revocation, which
            # waits for the turn, would leave that group behind, so a host
            # still running turns moves out whenever it can.
            moving = bool(groups["to_stop"] or row["slots_used"]) and await move_out_possible(
                conn, [host], need_target=True
            )
            if not allow_unmoved:
                if groups["to_stop"] and not moving:
                    return "no_move_target"
                if groups["unmovable"]:
                    return "cannot_move"
            if row["slots_used"] == 0 and not moving:
                await revoke_host_credentials(conn, [host])
                return "ok"
            if row["status"] != HOST_DRAINING and not transition_allowed(
                row["status"], HOST_DRAINING
            ):
                return "busy"
            await conn.execute(
                "UPDATE agent_runner_hosts SET status = $2, "
                "removal_requested_at = COALESCE(removal_requested_at, NOW()), "
                "removal_moves_out = $3, removal_leaves_unmoved = $4 WHERE host = $1",
                host,
                HOST_DRAINING,
                moving,
                leaves_unmoved,
            )
            return "moving_out" if moving else "draining"
        return "ok"

    async def finish_host_removals(self) -> list[str]:
        """Revoke every host whose requested removal has no running work left.

        The second half of a deferred :meth:`remove_host`. Each host is
        decided in its own transaction, with the locks taken in the order
        ``remove_host`` takes them, and its count read again under the row
        lock: an attempt the reaper is ending at this moment must either be
        finished first or keep the host for the next pass.

        Each pass then hands on any group of an already revoked host whose
        checkpoint arrived with a beat still in flight at the revocation
        (``release_late_covered_sandboxes``).

        Returns the hosts it revoked.
        """
        removed: list[str] = []
        async with self._pool.acquire() as conn:
            candidates = await conn.fetch(
                "SELECT h.host FROM agent_runner_hosts h "
                "WHERE h.removal_requested_at IS NOT NULL AND h.slots_used = 0 "
                "AND NOT (" + _REMOVAL_WAITS_SQL + ") ORDER BY h.host"
            )
            for candidate in candidates:
                host = candidate["host"]
                async with conn.transaction():
                    await conn.execute(
                        "SELECT id FROM agent_attempts WHERE host_id = $1 AND status = 'running' "
                        "ORDER BY id FOR UPDATE",
                        host,
                    )
                    await lock_revocation_rows(conn, [host])
                    row = await conn.fetchrow(
                        "SELECT h.slots_used, h.removal_requested_at, "
                        "(" + _REMOVAL_WAITS_SQL + ") AS moving "
                        "FROM agent_runner_hosts h WHERE h.host = $1 FOR UPDATE",
                        host,
                    )
                    if (
                        row is None
                        or row["removal_requested_at"] is None
                        or row["slots_used"]
                        or row["moving"]
                    ):
                        continue
                    await revoke_host_credentials(conn, [host])
                removed.append(host)
            async with conn.transaction():
                released = await release_late_covered_sandboxes(conn)
        if released:
            logger.info(
                "agent_sandboxes_released_after_revocation",
                extra={
                    "event": "agent_sandboxes_released_after_revocation",
                    "sandbox_ids": released,
                },
            )
        return removed

    async def reap_offline_hosts(
        self,
        *,
        interval_seconds: float,
        missed_intervals: int,
    ) -> list[str]:
        """Mark enrolled hosts that stopped heartbeating as ``offline``.

        Only *enrolled* hosts — ones holding a credential — are candidates.
        A host that joined by polling never promised to heartbeat, and marking
        it offline would stop the claims of a machine that is working
        perfectly, which is the whole pre-G deployment.

        ``COALESCE(last_heartbeat_at, first_seen_at)`` so a machine that
        registered and then never came up ages out of ``enrolling`` too,
        instead of sitting there looking like it is mid-setup forever.

        **``draining`` is not a candidate, and that is the point.** It was, and
        the result was that an operator's drain undid itself: this pass moved
        ``draining → offline``, and the next heartbeat moved ``offline →
        online``, because a machine returning from a partition is supposed to
        come back without an operator. A drained host that goes quiet and
        returns would silently rejoin the claim pool — quietly taking work the
        operator had just decided it must not take. Drain is an instruction,
        not an observation, so nothing here overwrites it; ``remove`` is what
        ends a drained host's life.
        A released drain receipt survives too: the next rollout finds its
        predecessor by that token, and the status change rotates the epoch.

        Returns the hosts it changed.
        """
        deadline = interval_seconds * missed_intervals
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                UPDATE agent_runner_hosts
                SET status = '{HOST_OFFLINE}'
                WHERE credential_hash IS NOT NULL
                  AND status IN ('{HOST_ENROLLING}', '{HOST_ONLINE}')
                  AND COALESCE(last_heartbeat_at, first_seen_at)
                      < NOW() - make_interval(secs => $1)
                RETURNING host
                """,
                deadline,
            )
        return [row["host"] for row in rows]

    async def set_active_runner_host(self, *, host: str | None) -> bool:
        """Pin new-chat placement to one host, or unpin with ``None``.

        Returns ``False`` when ``host`` names a machine that has never polled —
        pinning to one would park every chat without a sandbox on a host that
        may not exist. Existing groups remain on their owning hosts.

        Takes the policy row exclusively *first*, then the host row it is about
        to point at. Every writer here uses that order, and the claim takes the
        policy row shared, so the paths serialize with no cycle to deadlock on:
        a claim past its own read finishes before this commits, one that has
        not reached it waits and then sees this value, and a concurrent removal
        of the host being pinned cannot slip in between the check and the write.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT active_host FROM agent_runner_policy WHERE id FOR UPDATE")
            await lock_compute_topology(conn)
            # Checked before anything is written — and locked, not merely read:
            # a bare existence check lets a concurrent removal commit before
            # this does, leaving the queue pinned to a host that is gone.
            if host is not None:
                known = await conn.fetchval(
                    "SELECT 1 FROM agent_runner_hosts WHERE host = $1 FOR SHARE", host
                )
                if not known:
                    return False
                admitted, _ = await host_compute_admission(conn, host)
                if not admitted:
                    return False
            # Upsert, not a bare UPDATE: against a database whose policy row is
            # missing, an UPDATE touches nothing and still reports success —
            # the switch would read as applied while the gate kept using the
            # old value.
            await conn.execute(
                """
                INSERT INTO agent_runner_policy (id, active_host) VALUES (TRUE, $1)
                ON CONFLICT (id) DO UPDATE
                SET active_host = EXCLUDED.active_host, updated_at = NOW()
                """,
                host,
            )
            return True

    async def forget_runner_host(self, *, host: str) -> str:
        """Drop a host and its enrollment tokens from the pool.

        The removed credential and old enrollment tokens cannot restore it;
        registering it again requires a freshly issued admin enrollment token.

        Returns ``"deleted"`` or ``"unknown"``; refuses ``"active"``,
        ``"fenced"``,
        ``"retained_groups_present"``, ``"resource_bound"``,
        ``"active_attempts_present"`` and ``"pending_compute_operations"``.
        A sandbox whose deletion has completed is an audit row: it keeps the
        host name and does not block forgetting. Nor does a ``cold`` group,
        which any restore-capable host may claim; it keeps naming this host as
        the last it ran on until a claim moves it (``HOST_RETAINED_GROUP``).
        A live or stopped group, or one whose deletion failed or is still in
        progress, does block. A disabled attached machine without external
        release capability may be deregistered after dependency checks; this
        never shuts it down.

        The active-host refusal lives here rather than in the caller because
        checking there is a read the switch can outrun: the endpoint sees the
        host idle, an admin pins it, and the delete then removes the row the
        new-chat placement gate is pointing at — silently unpinning that
        policy. Same policy row, same order as the switch, so the two serialize.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            active = await conn.fetchval(
                "SELECT active_host FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            await lock_compute_topology(conn, exclusive=True)
            if active == host:
                return "active"
            retained = await conn.fetchval(
                f"SELECT 1 FROM agent_sandboxes g WHERE g.host=$1 AND {HOST_RETAINED_GROUP} LIMIT 1",
                host,
            )
            if retained:
                return "retained_groups_present"
            host_row = await conn.fetchrow(
                "SELECT compute_node_id,status,drain_fence_active "
                "FROM agent_runner_hosts WHERE host=$1 FOR UPDATE",
                host,
            )
            if host_row is not None and host_row["drain_fence_active"]:
                return "fenced"
            bound = None if host_row is None else host_row["compute_node_id"]
            local_cleanup_allocation = None
            if bound is not None:
                resource = await conn.fetchrow(
                    """SELECT a.id,a.observed_state,a.release_supported
                    FROM agent_compute_allocations a JOIN agent_compute_nodes n ON n.allocation_id=a.id
                    WHERE n.id=$1""",
                    bound,
                )
                if resource is None:
                    return "resource_bound"
                local_cleanup = (
                    not resource["release_supported"] and host_row["status"] == HOST_DISABLED
                )
                if resource["observed_state"] not in ("terminated", "absent") and not local_cleanup:
                    return "resource_bound"
                if await conn.fetchval(
                    """SELECT 1 FROM agent_compute_operations
                    WHERE allocation_id=$1 AND state NOT IN ('succeeded','failed','cancelled') LIMIT 1""",
                    resource["id"],
                ):
                    return "pending_compute_operations"
                if local_cleanup:
                    local_cleanup_allocation = resource["id"]
            running = await conn.fetchval(
                "SELECT 1 FROM agent_attempts WHERE host_id=$1 AND status='running' LIMIT 1", host
            )
            if running:
                return "active_attempts_present"
            deleted = await conn.fetchval(
                "DELETE FROM agent_runner_hosts WHERE host = $1 RETURNING host", host
            )
            if deleted is not None:
                await conn.execute(
                    "DELETE FROM agent_host_enroll_tokens WHERE host=$1 OR consumed_host=$1", host
                )
            if deleted is not None and local_cleanup_allocation is not None:
                # This is inventory deregistration for an attached machine,
                # never a provider release, shutdown, or filesystem action.
                await conn.execute(
                    """UPDATE agent_compute_allocations a
                    SET observed_state='absent',desired_state='absent',provider_state='deregistered',
                        revision=revision+1,updated_at=NOW()
                    WHERE a.id=$1 AND NOT EXISTS (
                        SELECT 1 FROM agent_runner_hosts h JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                        WHERE n.allocation_id=a.id)
                """,
                    local_cleanup_allocation,
                )
        return "deleted" if deleted is not None else "unknown"
