"""Installation snapshots: which one a new workspace may start from, and their lifecycle.

A snapshot row is created when a build is wanted (``queued``), claimed with
the environment run that builds it (``building``), and becomes ``ready`` only
from that run's host receipt. Nothing here touches S3: hosts upload, download
and delete, and the control plane only records what they report.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import TYPE_CHECKING, Any

from cloud_agent.execution_profiles import default_execution_profile
from cloud_agent.sandbox_archive_credentials import load_stored_archive_policy
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.repositories.environments import (
    lock_environment_snapshots,
    lock_team_environment_snapshots,
    retire_changed_snapshots,
)
from cloud_agent_shared.environment_snapshots import (
    SNAPSHOT_MAX_AGE_DAYS,
    SNAPSHOT_OWNER_QUOTA_BYTES,
    SNAPSHOT_SUPERSEDED_GRACE_HOURS,
    SNAPSHOT_UNUSED_DAYS,
    owner_hash,
    snapshot_object_key,
    valid_lockfiles,
)
from cloud_agent_shared.sandbox_restore import pullable_image

if TYPE_CHECKING:
    import builtins
    from collections.abc import Iterable, Mapping

    import asyncpg

#: What a runner reports after it was handed a snapshot for a new workspace.
SNAPSHOT_OUTCOMES = frozenset(
    {
        # The workspace started from it.
        "used",
        # None was handed out, so the workspace installed in full; a build for
        # its commit is wanted.
        "absent",
        # The task's lockfiles differ: it installed in full, and a build for
        # its commit is wanted.
        "lockfiles_changed",
        # The host's sandbox image differs: as above.
        "image_changed",
        # The object could not be fetched or loaded; nothing about it is known
        # to be wrong.
        "unavailable",
        # The object did not match its receipt or failed the archive checks.
        "invalid",
    }
)
_SUMMARY_STATES = ("queued", "building", "ready", "failed")
#: Earlier releases also saved a passing test's container, which a start
#: script may have written runtime values into. Such a snapshot is retired.
_SAVED_BY_TEST = (
    "EXISTS (SELECT 1 FROM agent_environment_runs r WHERE r.id = s.run_id AND r.purpose = 'test')"
)
#: A snapshot a job (user ``$2``, binding ``$3``) may report on: its own, or
#: any member's snapshot of the team environment its binding names, as
#: :meth:`PostgresEnvironmentSnapshotRepository.snapshot_for_claim` serves.
_JOB_MAY_USE = (
    "(user_id = $2 OR environment_id = ("
    "SELECT v.environment_id FROM agent_environment_bindings b "
    "JOIN agent_environment_versions v ON v.id = b.environment_version_id "
    "WHERE b.id = $3 AND b.team_id IS NOT NULL))"
)
#: After a failed build, tasks stop queueing new ones for this long; the owner
#: can still ask for one. A setup that fails would otherwise rerun in the
#: background beside every task.
FAILED_BUILD_BACKOFF_HOURS = 6


def _ref(row: Mapping[str, Any]) -> dict[str, Any]:
    """The value-free description a claim hands to a host."""
    return {
        "snapshot_id": row["id"],
        "owner": owner_hash(row["user_id"]),
        "object_key": row["object_key"],
        "version_id": row["object_version_id"],
        "sha256": row["sha256"],
        "size_bytes": int(row["size_bytes"]),
        "base_image": row["base_image"],
        "source_sha": row["source_sha"],
        "lockfiles": load_json(row["lockfiles"]) or {},
        "built_at": row["ready_at"].isoformat(),
    }


class PostgresEnvironmentSnapshotRepository:
    """Snapshot matching, build queueing, receipts and cleanup over one pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Reuse the assembled persistence pool."""
        self._pool = pool

    @staticmethod
    async def _enabled(conn: Any) -> bool:
        """Snapshots ride on checkpoints: both need the archive policy and its bucket."""
        row = await conn.fetchrow(
            "SELECT sandbox_archive_settings, sandbox_archive_revision FROM agent_runner_policy WHERE id"
        )
        if row is None:
            return False
        raw = dict(load_json(row["sandbox_archive_settings"]) or {})
        raw["revision"] = row["sandbox_archive_revision"]
        try:
            policy = load_stored_archive_policy(raw)
        except ValueError:
            return False
        return bool(policy.enabled and policy.checkpoints_enabled)

    async def enabled(self) -> bool:
        """Whether this deployment stores snapshots at all."""
        async with self._pool.acquire() as conn:
            return await self._enabled(conn)

    async def snapshot_for_claim(
        self, *, user_id: str, binding_id: str, base_image: str | None
    ) -> dict[str, Any] | None:
        """The newest usable snapshot for a binding on a host with ``base_image``."""
        if not pullable_image(base_image):
            return None
        async with self._pool.acquire() as conn:
            if not await self._enabled(conn):
                return None
            row = await conn.fetchrow(
                f"""
                SELECT s.* FROM agent_environment_bindings b
                JOIN agent_environment_versions v ON v.id = b.environment_version_id
                JOIN agent_environment_snapshots s
                  ON s.environment_id = v.environment_id
                 AND s.setup_fingerprint = b.setup_fingerprint
                 -- A personal environment's snapshot is its owner's alone. A
                 -- team's installation does not depend on who runs it, and its
                 -- members trust each other with it, so any member's serves.
                 AND (s.user_id = b.user_id OR b.team_id IS NOT NULL)
                -- Only a prebuild's container is an installation alone; see
                -- capture_target.
                JOIN agent_environment_runs r ON r.id = s.run_id AND r.purpose = 'prebuild'
                WHERE b.id = $1 AND b.user_id = $2 AND s.state = 'ready'
                  AND s.base_image = $3
                  AND s.ready_at > NOW() - INTERVAL '{SNAPSHOT_MAX_AGE_DAYS} days'
                ORDER BY s.ready_at DESC LIMIT 1
                """,
                binding_id,
                user_id,
                base_image,
            )
        return _ref(row) if row is not None else None

    async def queue_prebuild(
        self,
        *,
        user_id: str,
        binding_id: str,
        repo: str,
        base_sha: str,
        requested: bool = False,
    ) -> str | None:
        """Queue one build of a binding's installation, unless one is already pending.

        Returns the snapshot id, or ``None`` when nothing was queued: snapshots
        are off, the binding has no setup to snapshot, its environment is
        archived, a build of the same installation is already waiting, or one
        failed recently and the owner did not ask (``requested``) for another.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._enabled(conn):
                return None
            binding = await conn.fetchrow(
                "SELECT b.setup_fingerprint, v.environment_id, v.revision FROM agent_environment_bindings b "
                "JOIN agent_environment_versions v ON v.id = b.environment_version_id "
                "JOIN agent_environments e ON e.id = v.environment_id "
                "WHERE b.id = $1 AND b.user_id = $2 AND e.archived_at IS NULL",
                binding_id,
                user_id,
            )
            if binding is None or not binding["setup_fingerprint"]:
                return None
            if not requested and await conn.fetchval(
                f"""
                SELECT EXISTS(SELECT 1 FROM agent_environment_snapshots
                    WHERE environment_id=$1 AND setup_fingerprint=$2 AND state='failed'
                      AND updated_at > NOW() - INTERVAL '{FAILED_BUILD_BACKOFF_HOURS} hours')
                """,
                binding["environment_id"],
                binding["setup_fingerprint"],
            ):
                return None
            snapshot_id = secrets.token_hex(16)
            inserted = await conn.fetchval(
                """
                INSERT INTO agent_environment_snapshots
                    (id, user_id, environment_id, revision, setup_fingerprint)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (environment_id, setup_fingerprint)
                    WHERE state IN ('queued', 'building') DO NOTHING
                RETURNING id
                """,
                snapshot_id,
                user_id,
                binding["environment_id"],
                binding["revision"],
                binding["setup_fingerprint"],
            )
            if inserted is None:
                return None
            run_id = secrets.token_hex(16)
            await conn.execute(
                "INSERT INTO agent_environment_runs"
                "(id,user_id,repo,base_sha,binding_id,execution_profile,purpose,snapshot_id) "
                "VALUES($1,$2,$3,$4,$5,$6,'prebuild',$7)",
                run_id,
                user_id,
                repo,
                base_sha,
                binding_id,
                default_execution_profile().value,
                snapshot_id,
            )
            await conn.execute(
                "UPDATE agent_environment_snapshots SET run_id = $2 WHERE id = $1",
                snapshot_id,
                run_id,
            )
            return snapshot_id

    async def capture_target(self, run: Mapping[str, Any], host: str) -> dict[str, Any] | None:
        """What a host that just claimed ``run`` should save, if anything.

        Only a prebuild saves a snapshot: the one it was queued for, unless
        that was deleted since. A test runs the start and verify scripts with
        runtime values, so its container is never saved; a passing one queues
        a prebuild instead (:meth:`queue_after_test`).
        """
        if run.get("purpose") != "prebuild":
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE agent_environment_snapshots SET state='building', host=$3, updated_at=NOW() "
                "WHERE id=$1 AND run_id=$2 AND state IN ('queued','building') RETURNING user_id",
                run.get("snapshot_id"),
                run["id"],
                host,
            )
        if row is None:
            return None
        return {"snapshot_id": run["snapshot_id"], "owner": owner_hash(row["user_id"])}

    async def queue_after_test(self, run_id: str) -> str | None:
        """Queue a prebuild of the installation a test just passed with.

        Nothing is queued when the installation already has a ready snapshot
        of the tested commit. The owner asked for the test, so a recent failed
        build does not hold this one back.
        """
        async with self._pool.acquire() as conn:
            run = await conn.fetchrow(
                "SELECT r.user_id, r.repo, r.base_sha, r.binding_id FROM agent_environment_runs r "
                "JOIN agent_environment_bindings b ON b.id = r.binding_id "
                "JOIN agent_environment_versions v ON v.id = b.environment_version_id "
                "WHERE r.id=$1 AND r.purpose='test' AND r.state='succeeded' AND NOT EXISTS ("
                "SELECT 1 FROM agent_environment_snapshots s WHERE s.environment_id=v.environment_id "
                "AND s.setup_fingerprint=b.setup_fingerprint AND s.state='ready' AND s.source_sha=r.base_sha)",
                run_id,
            )
        if run is None or not run["repo"]:
            return None
        return await self.queue_prebuild(
            user_id=run["user_id"],
            binding_id=run["binding_id"],
            repo=run["repo"],
            base_sha=run["base_sha"],
            requested=True,
        )

    async def record_capture(
        self, *, run_id: str, host: str, generation: int, receipt: Mapping[str, Any]
    ) -> bool:
        """Make a building snapshot ready from its own run's host receipt.

        The receipt is checked against what the control plane decided: the
        run, its live lease, the snapshot it builds, and the one object key
        that snapshot may have. A host can only report the snapshot it was
        handed, and only while its run may still report at all: the run row is
        locked with the snapshot, so a cancel or an expired lease cannot slip
        between this check and the snapshot becoming ready. The owner's
        snapshot lock comes first, as for a deletion of the same snapshots;
        for a team environment, every owner's of its snapshots.
        """
        snapshot_id = str(receipt.get("snapshot_id") or "")
        lockfiles = receipt.get("lockfiles")
        if not valid_lockfiles(lockfiles) or not isinstance(lockfiles, dict):
            return False
        if not pullable_image(receipt.get("base_image")):
            return False
        if not isinstance(receipt.get("source_sha"), str) or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", receipt["source_sha"]
        ):
            return False
        async with self._pool.acquire() as conn, conn.transaction():
            built = await conn.fetchrow(
                "SELECT s.user_id, s.environment_id, e.team_id FROM agent_environment_snapshots s "
                "JOIN agent_environments e ON e.id = s.environment_id WHERE s.id=$1",
                snapshot_id,
            )
            if built is None:
                return False
            # The builds it supersedes may be other members' of a team environment.
            if built["team_id"]:
                await lock_team_environment_snapshots(conn, built["environment_id"])
            else:
                await lock_environment_snapshots(conn, built["user_id"])
            row = await conn.fetchrow(
                "SELECT s.* FROM agent_environment_snapshots s JOIN agent_environment_runs r ON r.id = s.run_id "
                "WHERE s.id=$1 AND r.id=$2 AND r.host=$3 AND r.lease_generation=$4 AND s.state='building' "
                "AND r.purpose='prebuild' AND r.state='running' AND NOT r.cancel_requested "
                "AND NOT r.cleanup_complete AND r.lease_expires_at>NOW() "
                "AND r.started_at>NOW()-INTERVAL '30 minutes' "
                "FOR UPDATE OF s, r",
                snapshot_id,
                run_id,
                host,
                generation,
            )
            if row is None or receipt.get("object_key") != snapshot_object_key(
                owner_hash(row["user_id"]), snapshot_id
            ):
                return False
            await conn.execute(
                """
                UPDATE agent_environment_snapshots SET
                    state='ready', base_image=$2, source_sha=$3, lockfiles=$4::jsonb,
                    object_key=$5, object_version_id=$6, sha256=$7, size_bytes=$8,
                    new_layer_bytes=$9, durations_ms=$10::jsonb, host=$11,
                    ready_at=NOW(), updated_at=NOW()
                WHERE id=$1
                """,
                snapshot_id,
                receipt["base_image"],
                receipt["source_sha"],
                json.dumps(lockfiles),
                receipt["object_key"],
                receipt.get("version_id"),
                receipt["sha256"],
                int(receipt["size_bytes"]),
                int(receipt.get("new_layer_bytes") or 0),
                json.dumps(
                    {str(k): int(v) for k, v in (receipt.get("durations_ms") or {}).items()}
                ),
                host,
            )
            # The newest build of an installation replaces older ones on the
            # same image; workspaces starting from them get a grace period.
            await conn.execute(
                "UPDATE agent_environment_snapshots SET state='superseded', updated_at=NOW() "
                "WHERE environment_id=$1 AND setup_fingerprint=$2 AND base_image=$3 AND state='ready' AND id<>$4",
                row["environment_id"],
                row["setup_fingerprint"],
                receipt["base_image"],
                snapshot_id,
            )
            # A build queued before the owner changed the installation, or for
            # a task or test bound before it, installed with the old values:
            # it is recorded, so it gets deleted, but never handed out.
            await retire_changed_snapshots(conn, row["user_id"], [row["environment_id"]])
            await self._enforce_quota(conn, row["user_id"])
            return True

    @staticmethod
    async def _enforce_quota(conn: Any, user_id: str) -> None:
        """Keep an owner's ready snapshots within quota, least recently used out first."""
        rows = await conn.fetch(
            "SELECT id, size_bytes FROM agent_environment_snapshots WHERE user_id=$1 AND state='ready' "
            "ORDER BY COALESCE(last_used_at, ready_at) DESC",
            user_id,
        )
        total = 0
        evicted = []
        for row in rows:
            total += int(row["size_bytes"])
            if total > SNAPSHOT_OWNER_QUOTA_BYTES:
                evicted.append(row["id"])
        if evicted:
            await conn.execute(
                "UPDATE agent_environment_snapshots SET state='deleting', detail='Owner snapshot quota reached', "
                "updated_at=NOW() WHERE id=ANY($1::text[])",
                evicted,
            )

    async def fail_capture(self, *, run_id: str, detail: str) -> None:
        """A run that ended without a ready snapshot leaves it failed."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_environment_snapshots SET state=CASE WHEN object_key IS NULL THEN 'failed' ELSE 'deleting' END, "
                "detail=$2, updated_at=NOW() WHERE run_id=$1 AND state IN ('queued','building')",
                run_id,
                detail[:500],
            )

    async def record_use(
        self, *, job_id: str, snapshot_id: str | None, outcome: str, base_sha: str | None
    ) -> bool:
        """Account for a new workspace's snapshot, and queue a build when it had none to use.

        ``base_sha`` is the commit the workspace checked out; a build is queued
        for it, so the next task on that commit finds a snapshot.
        """
        if outcome not in SNAPSHOT_OUTCOMES:
            raise ValueError("unknown snapshot outcome")
        if (snapshot_id is None) != (outcome == "absent"):
            raise ValueError("only an absent snapshot has no id")
        async with self._pool.acquire() as conn:
            job = await conn.fetchrow(
                "SELECT t.user_id, t.environment_binding_id, t.repo FROM agent_jobs j "
                "JOIN agent_threads t ON t.id = j.thread_id WHERE j.id = $1",
                job_id,
            )
            if job is None or not job["environment_binding_id"]:
                return False
            if outcome == "used":
                await conn.execute(
                    "UPDATE agent_environment_snapshots SET last_used_at=NOW(), use_count=use_count+1 "
                    f"WHERE id=$1 AND {_JOB_MAY_USE} AND state IN ('ready','superseded')",
                    snapshot_id,
                    job["user_id"],
                    job["environment_binding_id"],
                )
                return True
            if outcome == "invalid":
                await conn.execute(
                    "UPDATE agent_environment_snapshots SET state='deleting', "
                    "detail='The stored snapshot did not match its record', updated_at=NOW() "
                    f"WHERE id=$1 AND {_JOB_MAY_USE} AND state IN ('ready','superseded')",
                    snapshot_id,
                    job["user_id"],
                    job["environment_binding_id"],
                )
        if outcome in {"absent", "lockfiles_changed", "image_changed"} and job["repo"] and base_sha:
            await self.queue_prebuild(
                user_id=job["user_id"],
                binding_id=job["environment_binding_id"],
                repo=job["repo"],
                base_sha=base_sha,
            )
        return True

    async def summaries(
        self, user_id: str, fingerprints: Mapping[str, str | None]
    ) -> dict[str, dict[str, Any] | None]:
        """The latest snapshot of each environment's current installation.

        Each environment is matched with its own fingerprint: a copy installs
        what the original did, so one environment's earlier snapshot can carry
        another's current fingerprint. A team environment's is whichever
        member's built it; a personal one's, the owner's own. ``fingerprints``
        names only environments the caller may use.
        """
        wanted = {env: fp for env, fp in fingerprints.items() if fp}
        result: dict[str, dict[str, Any] | None] = dict.fromkeys(fingerprints)
        if not wanted:
            return result
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (s.environment_id) s.environment_id, s.id, s.state,
                       s.created_at, s.ready_at, s.last_used_at, s.size_bytes, s.detail
                FROM agent_environment_snapshots s
                JOIN agent_environments e ON e.id = s.environment_id
                WHERE (s.user_id = $1 OR e.team_id IS NOT NULL)
                  AND (s.environment_id, s.setup_fingerprint) IN (
                      SELECT * FROM unnest($2::text[], $3::text[]))
                  AND s.state = ANY($4::text[])
                ORDER BY s.environment_id, s.created_at DESC
                """,
                user_id,
                list(wanted),
                list(wanted.values()),
                list(_SUMMARY_STATES),
            )
        for row in rows:
            if wanted.get(row["environment_id"]) is None:
                continue
            result[row["environment_id"]] = {
                "id": row["id"],
                "state": row["state"],
                "created_at": row["created_at"],
                "ready_at": row["ready_at"],
                "last_used_at": row["last_used_at"],
                "size_bytes": row["size_bytes"],
                "detail": row["detail"],
            }
        return result

    async def discard(self, user_id: str, environment_id: str, *, team: bool = False) -> int:
        """Delete an environment's snapshots, and cancel the prebuilds of pending ones.

        A prebuild is cancelled the way an owner cancels a test: a queued one
        is never claimed, and a running one's host is refused at its next
        report and stops. ``team`` deletes every member's snapshots of a team
        environment; the caller has checked that ``user_id`` may change it.
        The snapshot locks of their owners come first: the statement below
        locks the snapshots in whatever order it scans them, and a receipt
        holding one of them may be waiting for another.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if team:
                owners = await lock_team_environment_snapshots(conn, environment_id)
            else:
                await lock_environment_snapshots(conn, user_id)
                owners = [user_id]
            rows = await conn.fetch(
                "UPDATE agent_environment_snapshots SET state=CASE WHEN object_key IS NULL THEN 'deleted' ELSE 'deleting' END, "
                "detail='Deleted by the owner', updated_at=NOW(), "
                "deleted_at=CASE WHEN object_key IS NULL THEN NOW() ELSE NULL END "
                "WHERE user_id=ANY($1::text[]) AND environment_id=$2 AND state IN ('queued','building','ready','superseded','failed') "
                "RETURNING run_id",
                owners,
                environment_id,
            )
            await conn.execute(
                "UPDATE agent_environment_runs SET cancel_requested=TRUE, cleanup_complete=(host IS NULL), "
                "state='cancelled', detail='The installation snapshot was deleted', "
                "finished_at=NOW(), updated_at=NOW() "
                "WHERE id=ANY($1::text[]) AND user_id=ANY($2::text[]) AND purpose='prebuild' AND state IN ('queued','running')",
                [row["run_id"] for row in rows if row["run_id"]],
                owners,
            )
        return len(rows)

    async def maintain(self) -> None:
        """Expire, retire and abandon snapshots; hosts then delete their objects."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE agent_environment_snapshots s SET state='deleting', updated_at=NOW(),
                    detail=CASE
                        WHEN e.archived_at IS NOT NULL THEN 'Environment archived'
                        WHEN {_SAVED_BY_TEST} THEN 'Saved by an environment test'
                        WHEN s.state='superseded' THEN COALESCE(s.detail, 'Replaced by a newer snapshot')
                        WHEN s.ready_at <= NOW() - INTERVAL '{SNAPSHOT_MAX_AGE_DAYS} days' THEN 'Snapshot expired'
                        ELSE 'Not used for {SNAPSHOT_UNUSED_DAYS} days' END
                FROM agent_environments e
                WHERE e.id = s.environment_id AND (
                    (s.state IN ('ready','superseded') AND e.archived_at IS NOT NULL)
                    OR (s.state IN ('ready','superseded') AND {_SAVED_BY_TEST})
                    OR (s.state='superseded' AND s.updated_at <= NOW() - INTERVAL '{SNAPSHOT_SUPERSEDED_GRACE_HOURS} hours')
                    OR (s.state='ready' AND s.ready_at <= NOW() - INTERVAL '{SNAPSHOT_MAX_AGE_DAYS} days')
                    OR (s.state='ready' AND COALESCE(s.last_used_at, s.ready_at)
                        <= NOW() - INTERVAL '{SNAPSHOT_UNUSED_DAYS} days'))
                """
            )
            # A build whose run ended, or that never started within a day,
            # will not finish.
            await conn.execute(
                """
                UPDATE agent_environment_snapshots s SET
                    state=CASE WHEN s.object_key IS NULL THEN 'failed' ELSE 'deleting' END,
                    detail=COALESCE(s.detail, 'Snapshot build did not finish'), updated_at=NOW()
                WHERE s.state IN ('queued','building') AND (
                    s.created_at <= NOW() - INTERVAL '1 day'
                    OR EXISTS (SELECT 1 FROM agent_environment_runs r WHERE r.id = s.run_id
                               AND r.state IN ('succeeded','failed','cancelled')
                               AND r.finished_at <= NOW() - INTERVAL '5 minutes'))
                """
            )

    async def deletions(self, limit: int = 20) -> builtins.list[dict[str, Any]]:
        """Stored objects a host should delete, those waiting longest first.

        Handing one out counts as an attempt and moves it to the back, so
        objects a bucket refuses to delete (the permission is optional) never
        hold back the rest.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE agent_environment_snapshots SET updated_at=NOW()
                WHERE id IN (
                    SELECT id FROM agent_environment_snapshots WHERE state='deleting'
                    ORDER BY updated_at, id LIMIT $1 FOR UPDATE SKIP LOCKED)
                RETURNING id, user_id, object_key, object_version_id
                """,
                limit,
            )
        return [
            {
                "snapshot_id": row["id"],
                "owner": owner_hash(row["user_id"]),
                "object_key": row["object_key"],
                "version_id": row["object_version_id"],
            }
            for row in rows
        ]

    async def retired_images(self, snapshot_ids: Iterable[str]) -> builtins.list[dict[str, Any]]:
        """Which of the snapshot images a host holds it should drop.

        The host names the images it has, so each host clears all of its own
        however many snapshots were retired elsewhere. An image this
        deployment has no record of is left alone.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, user_id FROM agent_environment_snapshots "
                "WHERE id = ANY($1::text[]) AND state IN ('deleting','deleted','failed')",
                list(snapshot_ids),
            )
        return [{"snapshot_id": row["id"], "owner": owner_hash(row["user_id"])} for row in rows]

    async def confirm_deleted(self, snapshot_id: str) -> bool:
        """Record that a host deleted a snapshot's stored object."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE agent_environment_snapshots SET state='deleted', deleted_at=NOW(), updated_at=NOW() "
                "WHERE id=$1 AND state='deleting'",
                snapshot_id,
            )
        return result == "UPDATE 1"
