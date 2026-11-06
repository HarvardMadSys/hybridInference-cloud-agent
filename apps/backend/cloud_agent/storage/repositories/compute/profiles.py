"""Compute deployment profiles and provider defaults."""

from __future__ import annotations

from typing import Any

from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.repositories.compute._common import (
    ComputeRepositoryBase,
    _json,
    _optional_row,
    _row,
)


class ProfilesMixin(ComputeRepositoryBase):
    """Deployment profiles, their host releases, and provider defaults."""

    async def list_deployment_profiles(self) -> list[dict[str, Any]]:
        """List profile metadata without bundles, plaintext inputs or ciphertext."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id,label,configuration,source_sha256,revision,created_at,updated_at
                FROM agent_compute_deployment_profiles ORDER BY created_at,id"""
            )
        return [_row(row) for row in rows]

    async def get_deployment_profile(self, profile_id: str) -> dict[str, Any] | None:
        """Read one trusted profile including its bundle and encrypted inputs."""
        async with self._pool.acquire() as conn:
            return _optional_row(
                await conn.fetchrow(
                    "SELECT * FROM agent_compute_deployment_profiles WHERE id=$1", profile_id
                )
            )

    async def save_deployment_profile(
        self,
        *,
        profile_id: str,
        label: str,
        configuration: dict[str, Any],
        source_bundle: bytes,
        source_sha256: str,
        secret_ciphertext: str,
    ) -> dict[str, Any]:
        """Create one profile. A host release may later advance its commit and bundle."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO agent_compute_deployment_profiles
                (id,label,configuration,source_bundle,source_sha256,secret_ciphertext)
                VALUES ($1,$2,$3::jsonb,$4,$5,$6)
                ON CONFLICT DO NOTHING RETURNING *""",
                profile_id,
                label,
                _json(configuration),
                source_bundle,
                source_sha256,
                secret_ciphertext,
            )
        if row is None:
            raise StorageConflict(ConflictCode.DEPLOYMENT_PROFILE_EXISTS)
        return _row(row)

    async def advance_host_release(
        self,
        *,
        allocation_id: str,
        previous_sha: str,
        previous_digest: str,
        deployment_sha: str,
        source_sha256: str,
        source_bundle: bytes,
        release_bundle_path: str,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Move every stored profile of one release, and the live hosts using it.

        A profile another deployment is currently installing is left unchanged.
        An autoscaling route's snapshot moves with its stored profile. The
        calling allocation must end on the new revision.
        """
        if previous_sha == deployment_sha and previous_digest == source_sha256:
            return
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            await conn.execute(
                """UPDATE agent_compute_deployment_profiles AS profile
                SET source_bundle=$3,
                    source_sha256=$4,
                    configuration=jsonb_set(
                        configuration, '{deployment_sha}', to_jsonb($5::text), true
                    ),
                    revision=revision+1,
                    updated_at=NOW()
                WHERE configuration->>'deployment_sha'=$1
                  AND source_sha256=$2
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_compute_allocations AS allocation
                    JOIN agent_compute_operations AS operation
                      ON operation.allocation_id=allocation.id
                    WHERE allocation.deployment_profile->>'id'=profile.id
                      AND operation.id<>$6
                      AND operation.kind IN ('acquire','upgrade','rotate')
                      AND operation.state IN ('pending','running','waiting','unknown')
                  )""",
                previous_sha,
                previous_digest,
                source_bundle,
                source_sha256,
                deployment_sha,
                operation_id,
            )
            # A reserved route deploys its next pool member from this snapshot.
            # It names the stored bundle, so it moves only with that profile.
            await conn.execute(
                """UPDATE agent_cloudlab_autoscaling_profile_reservations AS reservation
                SET profile_snapshot=reservation.profile_snapshot
                      || jsonb_build_object('deployment_sha',$3::text,'source_sha256',$4::text),
                    updated_at=NOW()
                FROM agent_compute_deployment_profiles AS profile
                WHERE profile.id=reservation.deployment_profile
                  AND reservation.profile_snapshot->>'source_bundle'
                      ='stored-profile:'||profile.id||':bundle'
                  AND reservation.profile_snapshot->>'deployment_sha'=$1
                  AND reservation.profile_snapshot->>'source_sha256'=$2
                  AND profile.configuration->>'deployment_sha'=$3
                  AND profile.source_sha256=$4""",
                previous_sha,
                previous_digest,
                deployment_sha,
                source_sha256,
            )
            await conn.execute(
                """UPDATE agent_compute_allocations AS allocation
                SET deployment_profile=allocation.deployment_profile
                      || jsonb_build_object(
                           'deployment_sha',$3::text,'source_sha256',$4::text
                         )
                      || CASE
                           WHEN allocation.deployment_profile->>'source_bundle'
                                LIKE 'stored-profile:%'
                           THEN '{}'::jsonb
                           ELSE jsonb_build_object('source_bundle',$5::text)
                         END,
                    revision=revision+1,
                    updated_at=NOW()
                WHERE allocation.deployment_profile->>'deployment_sha'=$1
                  AND allocation.deployment_profile->>'source_sha256'=$2
                  AND allocation.release_committed_at IS NULL
                  AND COALESCE(allocation.observed_state,'') NOT IN ('terminated','absent')
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_compute_operations AS operation
                    WHERE operation.allocation_id=allocation.id
                      AND operation.id<>$6
                      AND operation.kind IN ('acquire','upgrade','rotate')
                      AND operation.state IN ('pending','running','waiting','unknown')
                  )""",
                previous_sha,
                previous_digest,
                deployment_sha,
                source_sha256,
                release_bundle_path,
                operation_id,
            )
            recorded = await conn.fetchval(
                """SELECT deployment_profile->>'deployment_sha'=$2
                      AND deployment_profile->>'source_sha256'=$3
                FROM agent_compute_allocations WHERE id=$1""",
                allocation_id,
                deployment_sha,
                source_sha256,
            )
            if recorded is not True:
                raise StorageConflict(ConflictCode.HOST_RELEASE_NOT_RECORDED)

    async def activate_automatic_profiles(
        self,
        *,
        profile_ids: list[str],
        request_id: str,
        request_fingerprint: str,
    ) -> None:
        """Atomically admit provisioned slots while keeping release inputs immutable."""
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT id,configuration FROM agent_compute_deployment_profiles WHERE id=ANY($1::text[]) FOR UPDATE",
                profile_ids,
            )
            if len(rows) != len(profile_ids):
                raise StorageConflict(ConflictCode.RECORD_NOT_FOUND)
            for row in rows:
                configuration = load_json(row["configuration"]) or {}
                metadata = configuration.get("automatic_setup", {})
                if (
                    metadata.get("request_id") != request_id
                    or metadata.get("fingerprint") != request_fingerprint
                ):
                    raise StorageConflict(ConflictCode.IDEMPOTENCY_CONFLICT)
            await conn.execute(
                """UPDATE agent_compute_deployment_profiles
                SET configuration=jsonb_set(configuration,'{automatic_setup,ready}','true'::jsonb),
                    revision=revision+1,updated_at=NOW()
                WHERE id=ANY($1::text[]) AND configuration #>> '{automatic_setup,ready}' = 'false'""",
                profile_ids,
            )

    async def provider_defaults(self, provider_id: str) -> dict[str, Any]:
        """Return the saved non-secret form defaults for one provider."""
        async with self._pool.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT compute_defaults FROM agent_runner_policy WHERE id"
            )
        defaults = load_json(stored) or {}
        value = defaults.get(provider_id)
        return dict(value) if isinstance(value, dict) else {}

    async def set_provider_defaults(
        self, provider_id: str, defaults: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace one provider's defaults without disturbing other providers."""
        async with self._pool.acquire() as conn, conn.transaction():
            stored = await conn.fetchval(
                "SELECT compute_defaults FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            current = load_json(stored) or {}
            current[provider_id] = defaults
            await conn.execute(
                """UPDATE agent_runner_policy
                SET compute_defaults=$1::jsonb,updated_at=NOW() WHERE id""",
                _json(current),
            )
        return dict(defaults)
