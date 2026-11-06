"""Which hosts run a turn, the runner pool, and the queue's health."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent.domain.execution_profiles import KATA_RUNTIME
from cloud_agent.domain.sandbox_group import GroupState
from cloud_agent.sandbox_archive.sandbox_archive_credentials import stored_archive_policy
from cloud_agent.storage._sql.hosts import (
    HOST_CAPACITY_COLUMNS,
    HOST_COLUMNS,
    JOB_HOST_SQL,
    QUALIFIED_HOST_COLUMNS,
)
from cloud_agent.storage._sql.jobs import first_unfinished_turn
from cloud_agent.storage._sql.move_out import MOVABLE_GROUP_SQL, UNMOVABLE_GROUP_SQL
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.sandbox import THREAD_SANDBOX_SOURCE
from cloud_agent.storage.repositories.host_pool._common import HostPoolBase
from cloud_agent_shared.sandbox_archive_settings import (
    SANDBOX_ARCHIVE_CAPABILITY,
    SandboxArchivePolicyError,
)
from cloud_agent_shared.sandbox_restore import SANDBOX_RESTORE_CAPABILITY

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any


class RunnerPoolMixin(HostPoolBase):
    """Read the runner pool and where turns run."""

    async def host_for_attempt(self, *, attempt_id: int) -> dict[str, Any] | None:
        """The host row an attempt ran on, or ``None`` when it had none.

        ``None`` covers both "this attempt predates host reporting" and "the
        host it ran on was forgotten" — the FK is ``ON DELETE SET NULL``, so
        both present as the same absence, and both mean the caller falls back
        to the single-host configuration.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {QUALIFIED_HOST_COLUMNS} FROM agent_runner_hosts h
                JOIN agent_attempts a ON a.host_id = h.host
                WHERE a.id = $1
                """,
                attempt_id,
            )
        return None if row is None else self._host_row_to_dict(row)

    async def host_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        """The host holding the job's workspace, by the rule of ``JOB_HOST_SQL``."""
        owner = JOB_HOST_SQL.format(attempt="a", group="s")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {owner} AS owner_host,
                       {QUALIFIED_HOST_COLUMNS}
                FROM agent_jobs j
                LEFT JOIN agent_attempts a ON a.id = j.current_attempt_id
                LEFT JOIN {THREAD_SANDBOX_SOURCE} ON s.thread_id = j.thread_id
                LEFT JOIN agent_runner_hosts h
                  ON h.host = {owner}
                WHERE j.id = $1
                """,
                job_id,
            )
        if row is None or row["owner_host"] is None:
            return None
        host = self._host_row_to_dict(row)
        # A retained sandbox remains hard-pinned even if its host row was
        # administratively forgotten. Returning a placeholder (with no sandbox manager
        # URL) makes sandbox manager routing fail closed instead of falling back to a
        # different machine's single-host environment setting.
        host["host"] = row["owner_host"]
        return host

    async def active_runner_host(self) -> str | None:
        """The new-chat placement host, or ``None`` when any may materialize."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT active_host FROM agent_runner_policy WHERE id")

    async def runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], datetime]:
        """The pool, the pinned host and the version selection, read together.

        One transaction because all three are shown side by side: read
        separately, a switch landing between them renders a page whose list and
        whose header name different hosts, which reads as a bug in the thing
        the operator is using to fix a bug. The version selection joined that
        for the same reason — a page offering a menu that no longer contains
        the selection it displays is the same failure wearing a different hat.

        **Repeatable read, and read-only.** The pool is the page an operator
        reads while changing things, so a claim, a pin and a drain landing
        between the policy read and the host read used to be able to produce a
        list that never existed: slots already spent shown against a pin not
        yet switched. One snapshot for all of it removes that class of answer,
        and ``observed_at`` is the database's own instant for the same
        snapshot, so the ages the page renders are measured from the moment the
        data was taken rather than from whenever the client's clock says.

        Returns:
            ``(hosts, active_host, selected_versions, observed_at)``.
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            observed_at = await conn.fetchval("SELECT NOW()")
            policy = await conn.fetchrow(
                "SELECT active_host, runtime_versions FROM agent_runner_policy WHERE id"
            )
            rows = await conn.fetch(
                f"""
                SELECT {HOST_COLUMNS}, {HOST_CAPACITY_COLUMNS},
                       (SELECT count(*) FROM agent_sandboxes g
                        WHERE g.host = agent_runner_hosts.host AND {MOVABLE_GROUP_SQL})
                           AS sandboxes_left,
                       (SELECT count(*) FROM agent_sandboxes g
                        WHERE g.host = agent_runner_hosts.host AND {UNMOVABLE_GROUP_SQL})
                           AS sandboxes_unmovable
                FROM agent_runner_hosts
                ORDER BY last_seen_at DESC
                """
            )
        active = None if policy is None else policy["active_host"]
        selected = {} if policy is None else load_json(policy["runtime_versions"]) or {}
        return (
            [{**self._host_row_to_dict(row), "is_active": row["host"] == active} for row in rows],
            active,
            selected,
            observed_at,
        )

    async def deployment_runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], set[str]]:
        """Read placement policy, hosts, and retained owners for rollout fencing.

        The admin pin governs only chats with no sandbox. A control-plane
        rollout must also know which other hosts still own shared-live Docker
        layers so it can reverse-probe their sandbox managers. Read the policy under a
        shared lock and the owners in the same transaction: a concurrent host
        switch cannot make the snapshot describe two different placement
        policies, and any newly claimed group while this lock is held belongs
        to one of the placement hosts this snapshot already includes.

        ``release_follows_control_plane`` marks a Host of a managed compute
        allocation. The control plane rolls such a Host forward itself once it
        runs the new release, so the rollout cannot wait for it first.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            policy = await conn.fetchrow(
                "SELECT active_host, runtime_versions FROM agent_runner_policy WHERE id FOR SHARE"
            )
            rows = await conn.fetch(
                f"""
                SELECT {QUALIFIED_HOST_COLUMNS},
                       COALESCE(a.management_mode = 'manage'
                                AND a.desired_state = 'present'
                                AND a.release_committed_at IS NULL, FALSE)
                           AS release_follows_control_plane
                FROM agent_runner_hosts h
                LEFT JOIN agent_compute_nodes n ON n.id = h.compute_node_id
                LEFT JOIN agent_compute_allocations a ON a.id = n.allocation_id
                ORDER BY h.last_seen_at DESC
                """
            )
            owner_rows = await conn.fetch(
                f"""
                SELECT DISTINCT host
                FROM agent_sandboxes
                WHERE state <> {GroupState.LOST.sql}
                """
            )
        active = None if policy is None else policy["active_host"]
        selected = {} if policy is None else load_json(policy["runtime_versions"]) or {}
        return (
            [{**self._host_row_to_dict(row), "is_active": row["host"] == active} for row in rows],
            active,
            selected,
            {str(row["host"]) for row in owner_rows},
        )

    async def queue_health(
        self, *, expected_deployment_sha: str, queue_alert_after_s: float
    ) -> dict[str, int]:
        """Observe configured queue stalls without claiming or changing jobs.

        Occupied slots are ordinary backpressure. Missing/stale/disabled hosts,
        lost deployment markers and a wrong placement pin are not. Retained
        members use their owner, independent of the pin for new conversations.
        A ``cold`` group lives in its checkpoint instead: any host that
        restores may take it, except the one it left while that one still
        names its container, as the claim's cold branch reads it.
        No user content, job identifiers or credentials leave this projection.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            archive_row = await conn.fetchrow(
                """
                SELECT sandbox_archive_settings, sandbox_archive_revision
                FROM agent_runner_policy WHERE id FOR SHARE
                """
            )
            try:
                archive_policy = stored_archive_policy(archive_row)
                archive_required = archive_policy.enabled
                archive_revision = archive_policy.revision
            except SandboxArchivePolicyError:
                # Claims fail closed on an invalid stored policy. Monitoring
                # must not advertise capacity that the claim path will refuse.
                archive_required = True
                archive_revision = -1
            row = await conn.fetchrow(
                f"""
                WITH waiting AS (
                    SELECT g.host AS owner, t.execution_profile,
                           COALESCE(g.state = {GroupState.COLD.sql}, FALSE) AS cold,
                           g.container_id IS NULL AS uncontained,
                           COALESCE(j.metadata->'requires', '[]'::jsonb) AS requires
                    FROM agent_jobs j
                    JOIN agent_threads t ON t.id = j.thread_id
                    LEFT JOIN agent_thread_sandbox_memberships m ON m.thread_id = t.id
                    LEFT JOIN agent_sandboxes g ON g.id = m.sandbox_id
                    WHERE j.state = 'queued' AND NOT j.cancel_requested
                      AND j.updated_at < NOW() - $3 * INTERVAL '1 second'
                      AND {first_unfinished_turn("j")}
                ), serving AS (
                    SELECT h.* FROM agent_runner_hosts h
                    WHERE h.status = 'online' AND h.credential_hash IS NOT NULL
                      AND h.last_heartbeat_at >= NOW() - INTERVAL '90 seconds'
                      AND h.capabilities @> '["shared-sandbox-v1"]'::jsonb
                      AND COALESCE(h.slots_total, 1) > 0
                      AND EXISTS (
                          SELECT 1 FROM jsonb_array_elements_text(h.capabilities) c
                          WHERE c LIKE 'shared-sandbox-deployment:%'
                      )
                      AND (
                          NOT $4::boolean
                          OR (
                              h.sandbox_archive_settings_applied_revision = $5
                              AND h.capabilities @> $6::jsonb
                          )
                      )
                )
                SELECT COUNT(*) AS queued,
                       COUNT(*) FILTER (WHERE NOT EXISTS (
                           SELECT 1 FROM serving h
                           WHERE waiting.requires <@ h.capabilities
                             AND (waiting.execution_profile = 'trusted-v1' OR h.sandbox_runtime = $2)
                             AND (
                                 (waiting.owner IS NOT NULL AND NOT waiting.cold
                                  AND h.host = waiting.owner)
                                 OR (waiting.cold AND $4::boolean
                                     AND h.capabilities @> $7::jsonb
                                     AND (waiting.uncontained OR h.host <> waiting.owner))
                                 OR (waiting.owner IS NULL
                                     AND h.capabilities @> '["native-fork-at-anchor-v1"]'::jsonb
                                     AND ($1 = '' OR h.capabilities ? ('shared-sandbox-deployment:' || $1))
                                     AND ((SELECT active_host FROM agent_runner_policy WHERE id) IS NULL
                                          OR h.host = (SELECT active_host FROM agent_runner_policy WHERE id)))
                             )
                       )) AS unavailable,
                       (SELECT COUNT(*) FROM agent_jobs WHERE state = 'running') AS running
                FROM waiting
                """,
                expected_deployment_sha,
                KATA_RUNTIME,
                queue_alert_after_s,
                archive_required,
                archive_revision,
                json.dumps([SANDBOX_ARCHIVE_CAPABILITY]),
                json.dumps([SANDBOX_RESTORE_CAPABILITY]),
                timeout=10,
            )
        return {key: int(row[key]) for key in ("queued", "unavailable", "running")}

    async def list_runner_hosts(self) -> list[dict[str, Any]]:
        """Return every host seen polling, most recent poll first."""
        hosts, _, _, _ = await self.runner_pool()
        return hosts
