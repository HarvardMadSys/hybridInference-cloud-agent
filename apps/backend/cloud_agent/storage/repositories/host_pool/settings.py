"""Host-wide settings and the sandbox archive settings."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent.sandbox_archive.sandbox_archive_credentials import (
    archive_settings_payload,
    seal_archive_storage,
    stored_archive_policy,
)
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.runner_policy import archive_settings
from cloud_agent.storage.repositories.host_pool._common import HostPoolBase, _changed_settings
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any


class SettingsMixin(HostPoolBase):
    """Read and change host-wide and archive settings."""

    async def host_settings(self) -> dict[str, Any]:
        """The runtime settings set for every server, as stored.

        A server's own values win over these, and a setting set nowhere falls
        back to the built-in default. Read through
        ``cloud_agent_shared.host_settings.admin_values``, which drops anything
        a release can no longer apply.
        """
        async with self._pool.acquire() as conn:
            stored = await conn.fetchval("SELECT host_settings FROM agent_runner_policy WHERE id")
        return dict(load_json(stored) or {})

    async def set_host_settings(self, *, changes: Mapping[str, int | None]) -> dict[str, Any]:
        """Set or clear runtime settings for every server.

        ``changes`` names only the settings to change, and ``None`` clears one.
        Every host applies the result from its next heartbeat answer, except
        where a value set for that server wins. The policy row is locked for
        the read-modify-write, the same first lock every other policy writer
        takes, so this serializes with them and with a concurrent change to
        another setting. Returns the values before the change, for the audit
        record.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            stored = await conn.fetchval(
                "SELECT host_settings FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            previous = dict(load_json(stored) or {})
            # Upsert for the same reason the pin and the version selection are
            # one: against a database whose singleton row is missing, a bare
            # UPDATE touches nothing and still reports success.
            await conn.execute(
                """
                INSERT INTO agent_runner_policy (id, host_settings, host_settings_import_excluded)
                VALUES (TRUE, $1::jsonb, $2::text[])
                ON CONFLICT (id) DO UPDATE
                SET host_settings = EXCLUDED.host_settings,
                    host_settings_import_excluded = ARRAY(
                        SELECT DISTINCT key FROM unnest(
                            agent_runner_policy.host_settings_import_excluded ||
                            EXCLUDED.host_settings_import_excluded
                        ) AS touched(key)
                    ),
                    updated_at = NOW()
                """,
                json.dumps(_changed_settings(previous, changes)),
                list(changes),
            )
        return previous

    async def sandbox_archive_settings(self) -> dict[str, Any]:
        """Return the normalized deployment-wide archive policy and its timestamp."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT sandbox_archive_settings, sandbox_archive_revision,
                       sandbox_archive_updated_at
                FROM agent_runner_policy WHERE id
                """
            )
        return archive_settings(row)

    async def set_sandbox_archive_settings(
        self,
        *,
        expected_revision: int,
        settings: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Atomically replace the archive policy, or return ``None`` on a stale revision."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO agent_runner_policy (id) VALUES (TRUE) ON CONFLICT (id) DO NOTHING"
            )
            current = await conn.fetchrow(
                """
                SELECT sandbox_archive_settings, sandbox_archive_revision,
                       sandbox_archive_updated_at
                FROM agent_runner_policy WHERE id FOR UPDATE
                """
            )
            assert current is not None
            current_policy = stored_archive_policy(current)
            requested = SandboxArchivePolicy.from_mapping(
                {**dict(settings), "revision": current_policy.revision}
            )
            current_response = archive_settings_payload(
                current_policy,
                current["sandbox_archive_updated_at"],
                _changed=False,
            )
            if expected_revision > current_policy.revision:
                return None
            if requested.as_storage_dict() == current_policy.as_storage_dict():
                # Covers both an ordinary no-op and a retry after a successful
                # response was lost. Neither invents a new rollout revision.
                return current_response
            if current_policy.revision != expected_revision:
                return None
            policy = SandboxArchivePolicy.from_mapping(
                {**dict(settings), "revision": current_policy.revision + 1}
            )
            updated_at = await conn.fetchval(
                """
                UPDATE agent_runner_policy
                SET sandbox_archive_settings = $1::jsonb,
                    sandbox_archive_revision = $2,
                    sandbox_archive_updated_at = NOW(),
                    updated_at = NOW()
                WHERE id
                RETURNING sandbox_archive_updated_at
                """,
                json.dumps(seal_archive_storage(policy.as_storage_dict())),
                policy.revision,
            )
        return archive_settings_payload(policy, updated_at, _changed=True)
