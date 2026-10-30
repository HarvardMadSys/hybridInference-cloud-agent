"""Re-encrypt one credential family in batches, and rebuild environment fingerprints.

Every rewrite is a compare-and-swap on the value read: a concurrent writer
(a token refresh, an edit) wins, and its value is already under the current
key. Nothing is ever cleared: a value neither key opens is counted and left
as it is, because it was unreadable before the rotation too.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.credential_cipher import CredentialCipherError
from cloud_agent.environment_fingerprints import setup_material
from cloud_agent.environments import EnvironmentEncryptionError
from cloud_agent.storage.repositories.environments import (
    decode_environment_variables,
    environment_installations,
    lock_environment_snapshots,
    runtime_only,
    stored_environment_variables,
)
from cloud_agent_shared.environment import merge_variables

if TYPE_CHECKING:
    import asyncpg

    from cloud_agent.credential_cipher import CredentialCipher
    from cloud_agent.credential_families import Family


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _rewrite(cipher: CredentialCipher, value: str, counts: dict[str, int]) -> str | None:
    """The value under the current key, or ``None`` when it needs no rewrite."""
    if cipher.opens_with_current(value):
        counts["current"] += 1
        return None
    try:
        plaintext = cipher.decrypt(value)
    except CredentialCipherError:
        counts["unreadable"] += 1
        return None
    counts["rewritten"] += 1
    return cipher.encrypt(plaintext)


def _rewrite_values(
    cipher: CredentialCipher, items: list[dict[str, Any]], counts: dict[str, int]
) -> list[dict[str, Any]] | None:
    changed = False
    result = []
    for item in items:
        new = (
            _rewrite(cipher, item["value_ciphertext"], counts)
            if isinstance(item, dict) and isinstance(item.get("value_ciphertext"), str)
            else None
        )
        if new is not None:
            item = {**item, "value_ciphertext": new}
            changed = True
        result.append(item)
    return result if changed else None


class PostgresCredentialRotation:
    """Batches over the families of :mod:`cloud_agent.credential_families`."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Reuse the assembled persistence pool."""
        self._pool = pool

    async def reencrypt_batch(
        self, family: Family, cipher: CredentialCipher, *, after: str, limit: int = 200
    ) -> tuple[str | None, dict[str, int]]:
        """Rewrite up to ``limit`` rows after the cursor; ``None`` cursor when the family is done.

        Table, key expression and column come from the fixed family list,
        never from a request.
        """
        counts = {"rewritten": 0, "current": 0, "unreadable": 0, "raced": 0}
        # Compared as text whatever the key's own type, so one cursor serves every family.
        key = f"({family.row_key})::text"
        async with self._pool.acquire() as conn, conn.transaction():
            # Lets only this transaction's ciphertext-only rewrites past the
            # immutability of environment version values (migration 0112).
            await conn.execute("SET LOCAL cloud_agent.credential_reencryption = 'on'")
            rows = await conn.fetch(
                f"SELECT ({key}) AS k, {family.column} AS v FROM {family.table} "
                f"WHERE ({key}) > $1 AND {family.column} IS NOT NULL ORDER BY ({key}) LIMIT $2",
                after,
                limit,
            )
            for row in rows:
                if family.shape == "text":
                    new: Any = _rewrite(cipher, row["v"], counts)
                    old: Any = row["v"]
                else:
                    items = _json(row["v"]) or []
                    new = _rewrite_values(cipher, items, counts)
                    old = row["v"]
                    new = None if new is None else json.dumps(new)
                if new is None:
                    continue
                cast = "::jsonb" if family.shape == "json_values" else ""
                status = await conn.execute(
                    f"UPDATE {family.table} SET {family.column} = $2{cast} "
                    f"WHERE ({key}) = $1 AND {family.column} IS NOT DISTINCT FROM $3{cast}",
                    row["k"],
                    new,
                    old if family.shape == "text" else json.dumps(_json(old)),
                )
                if status.endswith(" 0"):
                    counts["raced"] += 1
        cursor = str(rows[-1]["k"]) if len(rows) == limit else None
        return cursor, counts

    async def not_current(self, family: Family, cipher: CredentialCipher) -> dict[str, int]:
        """How many stored values do not open with the current key alone, after the batches."""
        result = {"not_current": 0, "unreadable": 0}
        async with self._pool.acquire() as conn, conn.transaction():
            cursor = conn.cursor(
                f"SELECT {family.column} AS v FROM {family.table} WHERE {family.column} IS NOT NULL"
            )
            async for row in cursor:
                values = (
                    [row["v"]]
                    if family.shape == "text"
                    else [
                        item["value_ciphertext"]
                        for item in _json(row["v"]) or []
                        if isinstance(item, dict) and isinstance(item.get("value_ciphertext"), str)
                    ]
                )
                for value in values:
                    if cipher.opens_with_current(value):
                        continue
                    try:
                        cipher.decrypt(value)
                    except CredentialCipherError:
                        result["unreadable"] += 1
                        continue
                    result["not_current"] += 1
        return result

    async def replace_profile_secret(
        self, *, profile_id: str, expected_ciphertext: str, secret_ciphertext: str
    ) -> bool:
        """Replace a deployment profile's encrypted inputs unless they changed since read."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE agent_compute_deployment_profiles SET secret_ciphertext = $3 "
                "WHERE id = $1 AND secret_ciphertext = $2",
                profile_id,
                expected_ciphertext,
                secret_ciphertext,
            )
        return not status.endswith(" 0")

    async def fingerprint_owners(self, *, after: str, limit: int) -> list[str]:
        """Owners with an environment binding or snapshot, in order after ``after``."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id FROM ("
                "SELECT user_id FROM agent_environment_bindings WHERE setup_fingerprint IS NOT NULL "
                "UNION SELECT user_id FROM agent_environment_snapshots) o "
                "WHERE user_id > $1 ORDER BY user_id LIMIT $2",
                after,
                limit,
            )
        return [str(row["user_id"]) for row in rows]

    async def reindex_owner(self, user_id: str, cipher: CredentialCipher) -> dict[str, int]:
        """Move one owner's bindings and snapshots from the previous key's fingerprints to the current one's.

        Under the owner's snapshot lock, in one transaction: a binding and the
        snapshots it would reuse move together. A queued or building snapshot
        whose new fingerprint another pending one already holds is left as it
        is (its unique index forbids two); it is retired as stale after the
        rotation drops the previous key.
        """
        counts = {"bindings": 0, "snapshots": 0, "skipped": 0}
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_environment_snapshots(conn, user_id)
            moves: dict[tuple[str, str], str] = {}
            bindings = await conn.fetch(
                "SELECT b.id, b.setup_fingerprint, b.setup_override, b.has_setup_override, "
                "b.global_version_id, b.environment_version_id, b.team_id, "
                "v.environment_id, v.document FROM agent_environment_bindings b "
                "JOIN agent_environment_versions v ON v.id = b.environment_version_id "
                "WHERE b.user_id = $1 AND b.setup_fingerprint IS NOT NULL FOR UPDATE OF b",
                user_id,
            )
            for row in bindings:
                try:
                    personal = decode_environment_variables(
                        await stored_environment_variables(conn, row["global_version_id"])
                    )
                    merged = merge_variables(
                        runtime_only(personal) if row["team_id"] else personal,
                        decode_environment_variables(
                            await stored_environment_variables(
                                conn, row["environment_version_id"]
                            )
                        ),
                    )
                except (EnvironmentEncryptionError, ValueError):
                    counts["skipped"] += 1
                    continue
                setup = (
                    row["setup_override"]
                    if row["has_setup_override"]
                    else _json(row["document"]).get("setup_script")
                )
                material = setup_material(setup, merged)
                old, new = cipher.mac_with_previous(material), cipher.mac_with_current(material)
                if old is None or row["setup_fingerprint"] != old:
                    continue
                moves[(row["environment_id"], old)] = new
                await conn.execute(
                    "UPDATE agent_environment_bindings SET setup_fingerprint = $2 "
                    "WHERE id = $1 AND setup_fingerprint = $3",
                    row["id"],
                    new,
                    old,
                )
                counts["bindings"] += 1
            snapshots = await conn.fetch(
                "SELECT id, environment_id, setup_fingerprint, state FROM agent_environment_snapshots "
                "WHERE user_id = $1",
                user_id,
            )
            installations = await environment_installations(
                conn, user_id, list({row["environment_id"] for row in snapshots})
            )
            for environment_id, (script, variables) in installations.items():
                if not script:
                    continue
                material = setup_material(script, variables)
                digest = cipher.mac_with_previous(material)
                if digest is not None:
                    moves.setdefault((environment_id, digest), cipher.mac_with_current(material))
            for row in snapshots:
                target = moves.get((row["environment_id"], row["setup_fingerprint"]))
                if target is None:
                    continue
                if row["state"] in {"queued", "building"} and await conn.fetchval(
                    "SELECT 1 FROM agent_environment_snapshots WHERE environment_id = $1 "
                    "AND setup_fingerprint = $2 AND state IN ('queued', 'building')",
                    row["environment_id"],
                    target,
                ):
                    counts["skipped"] += 1
                    continue
                await conn.execute(
                    "UPDATE agent_environment_snapshots SET setup_fingerprint = $2 "
                    "WHERE id = $1 AND setup_fingerprint = $3",
                    row["id"],
                    target,
                    row["setup_fingerprint"],
                )
                counts["snapshots"] += 1
        return counts


__all__ = ["PostgresCredentialRotation"]
