"""Root-key protected values outside the settings row (re-encrypted by a rotation)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent.sandbox_archive_credentials import open_archive_secret, seal_archive_storage
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    import asyncpg


class PostgresRootKeyFamilies:
    """The runner policy's archive access key."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Bind to the pool."""
        self._pool = pool

    async def reseal_sandbox_archive_secret(self) -> bool:
        """Re-encrypt the archive access key under the current root key; ``False`` if none.

        The policy's content and revision do not change, so no host is asked to
        apply anything: only the ciphertext at rest does.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            raw = await conn.fetchval(
                "SELECT sandbox_archive_settings FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            values = load_json(raw) or {}
            secret = values.get("aws_secret_access_key")
            if not (isinstance(secret, dict) and "ciphertext" in secret):
                return False
            resealed = seal_archive_storage(open_archive_secret(values))
            await conn.execute(
                "UPDATE agent_runner_policy SET sandbox_archive_settings = $1::jsonb WHERE id",
                json.dumps(resealed),
            )
            # Prove the new ciphertext opens with the current key alone.
            open_archive_secret(resealed, previous_allowed=False)
        return True
