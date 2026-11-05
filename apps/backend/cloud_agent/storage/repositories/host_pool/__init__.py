"""Postgres implementation of the runner-host pool port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.

The repository is composed of one mixin per concern. Heartbeats stay in the
class itself: recording a heartbeat's capacity names the class.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent.domain.host_pool import (
    HOST_ENROLLING,
    HOST_OFFLINE,
    HOST_ONLINE,
    normalize_capabilities,
)
from cloud_agent.schemas.capacity_protocol import (
    CAPACITY_HISTORY_MAX_AGE_S,
    history_point_write_from_block,
)
from cloud_agent.storage._sql.hosts import HOST_COLUMNS
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.runner_policy import HEARTBEAT_POLICY_COLUMNS, heartbeat_policy
from cloud_agent.storage.host_settings_import import plan_settings_import
from cloud_agent.storage.repositories.host_capacity import insert_capacity_history_point
from cloud_agent.storage.repositories.host_pool._common import logger
from cloud_agent.storage.repositories.host_pool.autoscaling import AutoscalingMixin
from cloud_agent.storage.repositories.host_pool.enrollment import EnrollmentMixin
from cloud_agent.storage.repositories.host_pool.hosts import HostAdminMixin
from cloud_agent.storage.repositories.host_pool.runner_pool import RunnerPoolMixin
from cloud_agent.storage.repositories.host_pool.runtimes import RuntimesMixin
from cloud_agent.storage.repositories.host_pool.settings import SettingsMixin
from cloud_agent_shared.host_settings import reported_values

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    import asyncpg


_LEGACY_SANDBOX_MANAGER_KEYS = frozenset(
    {"terminal_idle_ttl_s", "terminal_hard_ttl_s", "terminal_max_sessions"}
)


class PostgresHostPoolRepository(
    EnrollmentMixin,
    HostAdminMixin,
    RunnerPoolMixin,
    AutoscalingMixin,
    RuntimesMixin,
    SettingsMixin,
):
    """The runner-host pool: enrollment, heartbeats, and placement."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def record_host_heartbeat(
        self,
        *,
        host: str,
        credential_hash: str | None = None,
        capacity: Mapping[str, Any] | None = None,
        capacity_sample_age_ms: int | None = None,
        physical_host: str | None = None,
        slots_total: int | None = None,
        launch_error: str | None = None,
        runner_version: str | None = None,
        sandbox_manager_url: str | None = None,
        capabilities: list[str] | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: dict[str, list[str]] | None = None,
        settings_applied: Mapping[str, int | float] | None = None,
        settings_local: Mapping[str, int | float] | None = None,
        sandbox_archive_settings_applied_revision: int | None = None,
        sandbox_archive_settings_revision_reported: bool = False,
        runtime_downloads: Sequence[Mapping[str, Any]] | None = None,
        config_reports: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Record a heartbeat, promoting the host to ``online`` when it may.

        ``enrolling → online`` and ``offline → online`` both happen here: a
        machine that comes back after a network partition needs no operator to
        put it back to work. ``draining`` and ``disabled`` deliberately do
        **not** — a heartbeat is the host saying "I am here", not "I should be
        taking work", and letting it re-promote itself would make drain a
        suggestion.

        Returns ``None`` for an unknown host, and for one whose credential is
        no longer the one this request authenticated with. Every field is
        optional and only overwrites when supplied, so a heartbeat that
        carries nothing but liveness does not blank out what enrollment
        declared.

        **The capacity block is written by a second statement, deliberately.**
        Its acceptance conditions are not the heartbeat's: the credential must
        still be the one this request authenticated with, the sample must not
        be one already stored, and an older observation must not overwrite a
        younger one. They are enforced in SQL rather than here because the
        sequence they guard against is a race — credential A authenticates, an
        operator re-enrolls the name under credential B, and A's delayed
        request arrives afterwards. Checking "is this still the right
        credential" outside the write would let exactly that request through.
        That second statement runs in its own savepoint, so a block the
        database refuses costs the host nothing but the block: the liveness
        half of the same call still commits. A third, in a savepoint of its
        own again, keeps an accepted observation in the history the chart
        reads — this request is the only place that sample is known to exist,
        because the row it was just written into holds one observation and the
        next heartbeat replaces it.

        Args:
            host: The authenticated host name.
            credential_hash: Digest the request authenticated with. The
                whole beat — liveness, archive revision, capacity write and
                legacy settings import — is refused unless the row still
                holds it.
            capacity: The validated, normalized capacity block to store.
            capacity_sample_age_ms: How old the observation was when sent.
            physical_host: Admin grouping label; never an identity.
            slots_total: The concurrency limit this host currently applies.
            launch_error: Why it is not claiming, or ``""`` when nothing is
                wrong. ``None`` is a host that does not report this at all and
                leaves whatever is stored in place, the same way an unreported
                runtime does.
            runner_version: Runner build the host is running.
            sandbox_manager_url: Address the control plane reaches this host's sandbox manager.
            capabilities: What this host can run.
            sandbox_runtime: Attested container runtime.
            sandbox_image_digest: Digest of the sandbox image it launches.
            runtime_versions: Default CLI version per runtime.
            runtime_versions_available: Every CLI version its image holds.
            settings_applied: The runtime settings the host is applying now.
            settings_local: Legacy environment observations, imported once as
                per-host overrides. Either ``None`` leaves the report in place.
            sandbox_archive_settings_applied_revision: The archive-policy
                revision the host's sandbox manager confirmed, ``None`` for none.
            sandbox_archive_settings_revision_reported: Whether the heartbeat
                carried that revision at all; when it did not, the stored one
                stays.
            runtime_downloads: This host's progress on each agent version it
                was asked to hold, already validated. ``None`` leaves it.
            config_reports: Non-numeric configuration the host says it uses
                now, such as its npm registry. ``None`` leaves the report.

        Returns:
            The updated host row, or ``None`` when the host no longer exists
            or no longer holds ``credential_hash``. The row also carries
            ``runner_policy``, what the heartbeat answer needs of the policy
            read that share-locks it (``heartbeat_policy``): the global
            ``host_settings``, the ``sandbox_archive_settings``, the
            ``runtime_download_targets`` and ``runtime_upgrade_waiting``, each
            ``None`` when its stored value does not decode.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Match the admin writer's lock order: policy before host. Once
            # defaults are authoritative, a legacy observation is imported at
            # most once and cannot resurrect an explicitly reset setting. The
            # same read carries the policy the heartbeat answer is made of.
            policy = await conn.fetchrow(
                f"SELECT {HEARTBEAT_POLICY_COLUMNS} FROM agent_runner_policy WHERE id FOR SHARE"
            )
            global_settings = (load_json(policy["host_settings"]) or {}) if policy else {}
            global_exclusions = set(policy["host_settings_import_excluded"]) if policy else set()
            row = await conn.fetchrow(
                f"""
                UPDATE agent_runner_hosts
                SET last_heartbeat_at = NOW(),
                    last_seen_at = NOW(),
                    status = CASE
                        WHEN status IN ('{HOST_ENROLLING}', '{HOST_OFFLINE}') THEN '{HOST_ONLINE}'
                        ELSE status
                    END,
                    slots_total = COALESCE($2, slots_total),
                    runner_version = COALESCE($3, runner_version),
                    broker_url = COALESCE($4, broker_url),
                    capabilities = COALESCE($5::jsonb, capabilities),
                    sandbox_runtime = COALESCE($6, sandbox_runtime),
                    sandbox_image_digest = COALESCE($7, sandbox_image_digest),
                    runtime_versions = COALESCE($8::jsonb, runtime_versions),
                    runtime_versions_available =
                        COALESCE($9::jsonb, runtime_versions_available),
                    labels = COALESCE(labels, '{{}}'::jsonb) || $10::jsonb,
                    -- "" survives COALESCE for the same reason an empty
                    -- sandbox runtime does: it is the host saying nothing is
                    -- wrong, which has to be able to clear a stale reason.
                    launch_error = COALESCE($11, launch_error),
                    settings_applied = COALESCE($12::jsonb, settings_applied),
                    settings_local = COALESCE($13::jsonb, settings_local),
                    sandbox_archive_settings_applied_revision = CASE
                        WHEN $15 AND ($16::text IS NULL OR credential_hash = $16) THEN $14
                        ELSE sandbox_archive_settings_applied_revision
                    END,
                    runtime_downloads = COALESCE($17::jsonb, runtime_downloads),
                    config_reports = COALESCE($18::jsonb, config_reports)
                -- The credential the request authenticated with must still be
                -- the row's: a beat delayed past a re-enrollment or a removal
                -- must not mark the new machine online, or a removed one alive.
                WHERE host = $1 AND ($16::text IS NULL OR credential_hash = $16)
                RETURNING {HOST_COLUMNS}, settings_imported, settings_import_excluded,
                    settings_late_imported
                """,
                host,
                slots_total,
                runner_version,
                sandbox_manager_url,
                None if capabilities is None else json.dumps(normalize_capabilities(capabilities)),
                # "" survives COALESCE deliberately: an empty runtime is the
                # host attesting shared-kernel runc, not declining to answer.
                sandbox_runtime,
                sandbox_image_digest,
                None if runtime_versions is None else json.dumps(runtime_versions),
                (
                    None
                    if runtime_versions_available is None
                    else json.dumps(runtime_versions_available)
                ),
                json.dumps({"physical_host": physical_host} if physical_host is not None else {}),
                launch_error,
                None if settings_applied is None else json.dumps(dict(settings_applied)),
                None if settings_local is None else json.dumps(dict(settings_local)),
                sandbox_archive_settings_applied_revision,
                sandbox_archive_settings_revision_reported,
                credential_hash,
                None
                if runtime_downloads is None
                else json.dumps([dict(report) for report in runtime_downloads]),
                None if config_reports is None else json.dumps(dict(config_reports)),
            )
            if row is None:
                return None
            plan = plan_settings_import(
                dict(row),
                reported_values(dict(settings_local)) if settings_local is not None else None,
                global_settings=global_settings,
                global_exclusions=global_exclusions,
                legacy_sandbox_manager_keys=_LEGACY_SANDBOX_MANAGER_KEYS,
            )
            if plan is not None and (
                credential_hash is None
                or await conn.fetchval(
                    "SELECT credential_hash = $2 FROM agent_runner_hosts WHERE host = $1",
                    host,
                    credential_hash,
                )
            ):
                desired, complete, offered = plan
                row = await conn.fetchrow(
                    f"""
                    UPDATE agent_runner_hosts
                    SET settings_desired = $2::jsonb, settings_imported = $3,
                        settings_late_imported = $4::text[]
                    WHERE host = $1
                    RETURNING {HOST_COLUMNS}, settings_imported, settings_import_excluded,
                        settings_late_imported
                    """,
                    host,
                    json.dumps(desired),
                    complete,
                    sorted(offered),
                )
            if (
                capacity is not None
                and capacity_sample_age_ms is not None
                and credential_hash is not None
            ):
                await self._record_host_capacity_isolated(
                    conn,
                    host=host,
                    credential_hash=credential_hash,
                    capacity=capacity,
                    sample_age_ms=capacity_sample_age_ms,
                )
        return {**self._host_row_to_dict(row), "runner_policy": heartbeat_policy(policy)}

    @staticmethod
    async def _record_host_capacity_isolated(
        conn: Any,
        *,
        host: str,
        credential_hash: str,
        capacity: Mapping[str, Any],
        sample_age_ms: int,
    ) -> None:
        """Store a capacity observation without ever risking the heartbeat.

        The two halves of this request share a transaction, and a transaction
        that has raised is finished: catching the exception in Python would not
        un-abort it, so the heartbeat's own write would be rolled back with the
        monitoring field that failed. A nested transaction is a SAVEPOINT,
        which is the only thing that lets the failure be contained here.

        That matters because the failure modes are the database's, not this
        code's: a namespace carrying U+0000 passes any string validation and is
        then refused by JSONB, and a constraint added tomorrow could refuse
        something else. A host that is alive must be recorded as alive whatever
        the control plane thinks of its monitoring data.

        An accepted observation is then kept twice: in the host row, which is
        the current reading the table shows, and in the history table, which is
        the chart. Both from this request, because this is the only moment the
        sample is known to exist — the row holds one observation and the next
        heartbeat overwrites it.
        """
        try:
            async with conn.transaction():
                accepted = await PostgresHostPoolRepository._record_host_capacity(
                    conn,
                    host=host,
                    credential_hash=credential_hash,
                    capacity=capacity,
                    sample_age_ms=sample_age_ms,
                )
        except Exception:
            logger.warning(
                "agent_host_capacity_write_failed",
                exc_info=True,
                extra={"event": "agent_host_capacity_write_failed", "host": host},
            )
            return
        if accepted is None:
            # Refused by one of the write's own conditions: a replay, an
            # observation older than the stored one, or a credential that has
            # since been replaced. None of them is a new measurement, so none
            # of them belongs in the history either.
            return
        await PostgresHostPoolRepository._keep_capacity_history_isolated(
            conn,
            host=host,
            capacity=capacity,
            sample_age_ms=sample_age_ms,
            accepted=accepted,
        )

    @staticmethod
    async def _keep_capacity_history_isolated(
        conn: Any,
        *,
        host: str,
        capacity: Mapping[str, Any],
        sample_age_ms: int,
        accepted: Mapping[str, Any],
    ) -> None:
        """Add this observation to the chart's history, in a savepoint of its own.

        **Why the write is here and not on a timer.** The host row holds one
        observation, and every heartbeat replaces it. A task that re-read that
        row on its own clock could therefore only ever see whichever sample
        happened to be in it at that instant: every other sample was already
        gone. At a 30-second report and a 60-second pass the two clocks drift
        against each other, and each time they crossed, a minute went by whose
        sample had been overwritten before anything read it. That minute was
        then missing from the table for good — nothing backfills, by design —
        and the chart drew a gap no outage had caused. Writing from the request
        that accepts the sample has no clock to drift against.

        **Why a second savepoint.** The reading an operator sees in the table
        was just accepted, and it must not be rolled back because another
        table refused a row. This failure costs one point in a curve; the one
        it is being kept away from costs the current number for a live machine.

        Args:
            conn: The heartbeat's connection, inside its transaction.
            host: The host the observation belongs to.
            capacity: The block that was accepted.
            sample_age_ms: How old the observation was when it was sent.
            accepted: Epoch, instant and identity, as the accepting write
                returned them.
        """
        if sample_age_ms > CAPACITY_HISTORY_MAX_AGE_S * 1000:
            # A sampler that has been stalled for minutes is reporting a
            # measurement, but not this minute's: it is admitted as the current
            # reading, where its age is shown beside it, and kept out of a
            # curve that has no way to say how late it was.
            return
        point = history_point_write_from_block(
            capacity,
            sampled_at=accepted["capacity_sampled_at"],
            sample_id=str(accepted["capacity_sample_id"]),
        )
        if point is None:
            return
        try:
            async with conn.transaction():
                await insert_capacity_history_point(
                    conn,
                    host=host,
                    capacity_epoch=accepted["capacity_epoch"],
                    point=point,
                )
        except Exception:
            logger.warning(
                "agent_host_capacity_history_write_failed",
                exc_info=True,
                extra={"event": "agent_host_capacity_history_write_failed", "host": host},
            )

    @staticmethod
    async def _record_host_capacity(
        conn: Any,
        *,
        host: str,
        credential_hash: str,
        capacity: Mapping[str, Any],
        sample_age_ms: int,
    ) -> Any:
        """Store a capacity observation, iff it is new, newer, and still ours.

        Three conditions, all in the predicate rather than in a prior read:

        * ``credential_hash`` still matches — a re-enrollment between
          authentication and this write has cleared the row, and the delayed
          request must not repopulate it with the previous machine's numbers.
        * ``sample_id`` differs — the same observation arriving again (a
          retried heartbeat, a second replica replaying a cached block) does
          not refresh its timestamps and so cannot be made to look fresh.
        * ``sampled_at`` is newer than what is stored — replicas report
          independently and out of order, and the older report must not
          overwrite the younger one merely because it arrived later.

        ``sampled_at`` is derived from the server's own ``NOW()`` minus the
        reported age. The runner's wall clock is never trusted, and the two
        timestamps therefore come from one clock that cannot jump.

        Returns:
            The epoch, instant and identity the row now holds, or ``None`` when
            one of the three conditions refused the write. The caller keeps the
            history point from this row rather than from the block it sent, so
            the point and the snapshot cannot disagree about which observation
            they are, or about when it was taken.
        """
        return await conn.fetchrow(
            """
            UPDATE agent_runner_hosts
            SET capacity_snapshot = $3::jsonb,
                capacity_received_at = NOW(),
                capacity_sampled_at = NOW() - make_interval(secs => $4::double precision),
                capacity_sample_id = $5::uuid
            WHERE host = $1
              AND credential_hash = $2
              AND capacity_sample_id IS DISTINCT FROM $5::uuid
              AND (
                  capacity_sampled_at IS NULL
                  OR capacity_sampled_at < NOW() - make_interval(secs => $4::double precision)
              )
            RETURNING capacity_epoch, capacity_sampled_at, capacity_sample_id
            """,
            host,
            credential_hash,
            json.dumps(capacity),
            sample_age_ms / 1000.0,
            str(capacity["sample_id"]),
        )
