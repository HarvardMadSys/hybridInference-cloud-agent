"""Reconciling the Docker observations a host reports for its groups."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.domain.sandbox_group import DELETED_GROUP_STATES
from cloud_agent.storage._sql.sandbox_observations import (
    MEMBERS_STOPPED_SQL,
    observation_changes_nothing,
    observed_groups,
    record_observed_measurements,
    record_running_container,
    record_stopped_container,
    stored_terminal_count,
    write_observed_container,
    write_reported_members,
)
from cloud_agent.storage._sql.sandbox_transitions import (
    CLEAR_RESOURCE_SAMPLE_SQL,
    RECORD_RESOURCE_SAMPLE_SQL,
    RECORD_TERMINAL_COUNT_SQL,
    STOP_LOST_ONLY_GROUP_SQL,
    STOP_ORPHANED_PROVISIONING_GROUP_SQL,
)
from cloud_agent.storage.repositories.sandbox._common import (
    SandboxRepositoryBase,
    logger,
)
from cloud_agent.usage.owner_usage import measured_interval, utc_day_shares
from cloud_agent_shared.sandbox_resources import parse_sandbox_resource_sample

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import asyncpg


class ObservationMixin(SandboxRepositoryBase):
    """Join a host's group observations with the durable group and members."""

    async def reconcile_thread_sandbox_observation(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
        running: bool,
        activity_active: bool | None = None,
        idle_remaining_seconds: float | None = None,
        members: list[dict[str, Any]] | None = None,
        resource_usage: dict[str, Any] | None = None,
        terminal_count: int | None = None,
    ) -> str | None:
        """Reconcile one authenticated shared-group Docker observation."""
        return await self._reconcile_shared_sandbox_observation(
            thread_id=thread_id,
            host=host,
            sandbox_id=sandbox_id,
            container_id=container_id,
            sandbox_generation=sandbox_generation,
            running=running,
            activity_active=activity_active,
            idle_remaining_seconds=idle_remaining_seconds,
            members=members or [],
            resource_usage=resource_usage,
            terminal_count=terminal_count,
        )

    async def reconcile_thread_sandbox_observations(
        self, *, host: str, observations: Sequence[Mapping[str, Any]]
    ) -> list[str | None]:
        """Reconcile every group one heartbeat reports, writing only what changed.

        Each observation carries the arguments of
        :meth:`reconcile_thread_sandbox_observation` but the host, and gets its
        answer, in order. One that changes nothing, as one unlocked read finds
        the groups, takes no lock and is answered ``None``
        (:mod:`cloud_agent.storage._sql.sandbox_observations`). The rest are
        reconciled one by one in fenced transactions of their own, and so is
        any later sighting of a group in the same beat, which the read no
        longer describes. A group the host did not report is not read.
        """
        if not observations:
            return []
        async with self._pool.acquire() as conn:
            groups, members = await observed_groups(
                conn, [str(observation["sandbox_id"]) for observation in observations]
            )
        results: list[str | None] = []
        fenced: set[str] = set()
        for observation in observations:
            sandbox_id = str(observation["sandbox_id"])
            if sandbox_id not in fenced and observation_changes_nothing(
                observation,
                host=host,
                group=groups.get(sandbox_id),
                members=members.get(sandbox_id, {}),
            ):
                results.append(None)
                continue
            fenced.add(sandbox_id)
            results.append(
                await self.reconcile_thread_sandbox_observation(host=host, **observation)
            )
        return results

    async def _reconcile_shared_sandbox_observation(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
        running: bool,
        activity_active: bool | None,
        idle_remaining_seconds: float | None,
        members: list[dict[str, Any]],
        resource_usage: dict[str, Any] | None,
        terminal_count: int | None,
    ) -> str | None:
        """Join one Docker group observation with independently fenced members."""
        async with self._pool.acquire() as conn, conn.transaction():
            # The stopped path updates every durable member, including one a
            # partial host census omitted. Lock the complete bounded group in
            # deterministic order before its sandbox row. Settlement,
            # admission, fork, and terminal writers use the same membership
            # -> sandbox order; taking only reported members would recreate
            # the inverse order when the bulk stopped-state update runs.
            await conn.fetch(
                """
                SELECT thread_id
                FROM agent_thread_sandbox_memberships
                WHERE sandbox_id = $1
                ORDER BY thread_id
                FOR UPDATE
                """,
                sandbox_id,
            )
            group = await conn.fetchrow(
                """
                SELECT * FROM agent_sandboxes
                WHERE id = $1 AND origin_thread_id = $2 AND host = $3
                  AND generation = $4
                FOR UPDATE
                """,
                sandbox_id,
                thread_id,
                host,
                sandbox_generation,
            )
            if group is None:
                return None
            if group["container_id"] not in {None, container_id}:
                return None
            if group["deletion_state"] in DELETED_GROUP_STATES:
                # Host samples predate this transaction. They cannot reopen
                # admission or resurrect a container fenced for deletion.
                return None
            if group["state"] in {"cold", "restoring"}:
                # Only a checkpoint holds this group until the restore that
                # claimed it confirms the recreated container. A container the
                # restoring host reports before then is not yet the group's.
                return None
            # Each step writes only what changes the row locked here: a beat
            # that repeats what is recorded rewrites nothing.
            await write_observed_container(conn, group, container_id=container_id, running=running)
            await record_observed_measurements(
                conn,
                group,
                running=running,
                terminal_count=terminal_count,
                resource_usage=resource_usage,
                terminals=self._record_terminal_observation,
                resources=self._record_resource_observation,
            )
            await write_reported_members(conn, sandbox_id, members)

            active_member = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM agent_thread_sandbox_memberships
                    WHERE sandbox_id = $1 AND active_attempt_id IS NOT NULL
                )
                """,
                sandbox_id,
            )
            # A host heartbeat is an observation sampled before this database
            # transaction.  It may say "locally idle" just before a native
            # fork/snapshot coordinator increments its durable operation
            # fence.  Never let that stale sample re-arm an idle deadline in
            # the middle of the externally visible mutation.
            durable_activity = bool(active_member) or int(group["active_operation_count"]) > 0
            # A runner may have died after every durable member context became
            # LOST while the sandbox manager still holds a stale local lease.
            # That lease must not keep the shared container RUNNING forever.
            lost_only_group = False
            if not durable_activity and (
                running or (group["state"] == "running" and group["idle_deadline"] is None)
            ):
                lost_only_group = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM agent_thread_sandbox_memberships
                        WHERE sandbox_id = $1
                    ) AND NOT EXISTS (
                        SELECT 1 FROM agent_thread_sandbox_memberships
                        WHERE sandbox_id = $1
                          AND COALESCE(context_status, '') <> 'lost'
                          AND COALESCE(native_state, '') <> 'lost'
                    )
                    """,
                    sandbox_id,
                )
            # Docker creation can outlive the runner that requested it. An
            # explicit pre-admission abandon leaves the group PROVISIONING
            # with no active member; if Docker reports that exact labelled
            # object afterwards, it is an orphan to stop, not a fresh active
            # group to adopt. This recovery fence prevents a sandbox-manager-local
            # lease from keeping an unowned container resident forever.
            if group["state"] == "provisioning" and not durable_activity:
                recovered = await conn.fetchval(
                    STOP_ORPHANED_PROVISIONING_GROUP_SQL,
                    sandbox_id,
                    running,
                )
                if recovered == "stopped":
                    await conn.execute(MEMBERS_STOPPED_SQL, sandbox_id)
                return None if recovered is None else str(recovered)
            if running and lost_only_group and not durable_activity:
                row = await conn.fetchrow(
                    STOP_LOST_ONLY_GROUP_SQL,
                    sandbox_id,
                )
                # Unwritten, the group already stands where this would put it.
                return str(group["state"] if row is None else row["state"])
            if running:
                return await record_running_container(
                    conn,
                    group,
                    activity_active=activity_active,
                    idle_remaining_seconds=idle_remaining_seconds,
                    durable_activity=durable_activity,
                )
            if active_member:
                return None
            return await record_stopped_container(conn, group)

    @staticmethod
    async def _record_terminal_observation(
        conn: asyncpg.Connection,
        *,
        sandbox_id: str,
        running: bool,
        terminal_count: int | None,
    ) -> None:
        """Persist current terminal safety data for an already fenced container."""
        count = stored_terminal_count(running, terminal_count)
        await conn.execute(
            RECORD_TERMINAL_COUNT_SQL,
            sandbox_id,
            count,
        )

    @staticmethod
    async def _record_resource_observation(
        conn: asyncpg.Connection,
        *,
        sandbox_id: str,
        running: bool,
        resource_usage: dict[str, Any] | None,
    ) -> None:
        """Store only new, valid counters for an already fenced container."""
        if not running:
            await conn.execute(
                CLEAR_RESOURCE_SAMPLE_SQL,
                sandbox_id,
            )
            return
        sample = parse_sandbox_resource_sample(resource_usage)
        if sample is None:
            return
        # The CTE reads the row as this statement found it, so the previous
        # sample's time comes back beside the one replacing it.
        accepted = await conn.fetchrow(
            RECORD_RESOURCE_SAMPLE_SQL,
            sandbox_id,
            json.dumps(sample.model_dump(mode="json")),
            sample.sample_age_ms / 1000.0,
            sample.sample_id,
        )
        if accepted is None or sample.cpu_cores is None or sample.memory_bytes is None:
            return
        seconds = measured_interval(
            sampled_at=accepted["resource_sampled_at"],
            previous_sampled_at=accepted["previous_sampled_at"],
            cpu_window_seconds=sample.cpu_window_seconds,
        )
        if seconds <= 0:
            return
        # The owner's history is optional too: losing one day's addition must
        # not lose the sample the admin inventory shows. The time a sample
        # stands for can begin before midnight, so each UTC day it covers gets
        # its own share, earliest day first.
        try:
            async with conn.transaction():
                for day, share in utc_day_shares(
                    ended_at=accepted["resource_sampled_at"], seconds=seconds
                ):
                    await conn.execute(
                        """
                        INSERT INTO agent_sandbox_usage_days AS d (
                            sandbox_id, user_id, day,
                            measured_seconds, cpu_core_seconds, memory_byte_seconds
                        )
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (sandbox_id, day) DO UPDATE SET
                            measured_seconds = d.measured_seconds + EXCLUDED.measured_seconds,
                            cpu_core_seconds = d.cpu_core_seconds + EXCLUDED.cpu_core_seconds,
                            memory_byte_seconds = d.memory_byte_seconds + EXCLUDED.memory_byte_seconds,
                            updated_at = NOW()
                        """,
                        sandbox_id,
                        accepted["user_id"],
                        day,
                        share,
                        sample.cpu_cores * share,
                        float(sample.memory_bytes) * share,
                    )
        except Exception:
            logger.warning("sandbox_usage_day_unavailable", exc_info=True)
