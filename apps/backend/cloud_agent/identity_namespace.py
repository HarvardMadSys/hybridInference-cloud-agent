"""Pin an Agent database to one Gateway identity issuer."""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class IdentityNamespaceError(RuntimeError):
    """The configured Gateway cannot own this database's existing identities."""


async def _has_owner_data(conn: asyncpg.Connection) -> bool:
    # Discover owner tables so a future connection type cannot escape the
    # legacy-data guard. Only database identifiers enter these quoted queries.
    tables = await conn.fetch(
        "SELECT DISTINCT table_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND left(table_name, 6) = 'agent_' "
        "AND column_name IN ('user_id', 'target_user_id')"
    )
    for row in tables:
        name = '"' + row["table_name"].replace('"', '""') + '"'
        if await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {name})"):
            return True
    return False


async def ensure_gateway_namespace(
    pool: asyncpg.Pool, issuer: str | None, *, adopt_existing: bool = False
) -> str | None:
    """Check the stored issuer, or initialize an empty database exactly once.

    Existing owner data requires an explicit operator adoption. That operation
    can fill an absent binding but can never change a recorded issuer.
    Concurrent initializers serialize on the singleton table.
    """
    async with pool.acquire() as conn, conn.transaction():
        return await check_gateway_namespace(conn, issuer, adopt_existing=adopt_existing)


async def check_gateway_namespace(
    conn: asyncpg.Connection, issuer: str | None, *, adopt_existing: bool = False
) -> str | None:
    """Bind or check the issuer in the same transaction as its settings update."""
    await conn.execute("LOCK TABLE agent_identity_namespace IN EXCLUSIVE MODE")
    stored = await conn.fetchval("SELECT gateway_issuer FROM agent_identity_namespace")
    if stored is not None:
        if issuer != stored:
            raise IdentityNamespaceError(
                "GATEWAY_ISSUER does not match this Agent database's identity issuer. "
                "Restore the original configuration or use a separate database."
            )
        return stored
    if issuer is None:
        return None
    if not adopt_existing and await _has_owner_data(conn):
        raise IdentityNamespaceError(
            "This Agent database has existing owner data but no identity issuer. "
            "Verify its original Gateway issuer and explicitly initialize that binding with "
            "python -m cloud_agent.identity_namespace --adopt-existing --issuer <original-issuer>."
        )
    await conn.execute(
        "INSERT INTO agent_identity_namespace (singleton, gateway_issuer) VALUES (TRUE, $1)", issuer
    )
    return issuer


async def _check_or_adopt(issuer: str | None, *, adopt_existing: bool) -> None:
    import asyncpg

    from cloud_agent.settings import setting_env

    dsn = os.environ.get("AGENT_DATABASE_URL")
    if not dsn:
        raise IdentityNamespaceError("AGENT_DATABASE_URL must be set.")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        from cloud_agent.settings import apply_settings
        from cloud_agent.storage.assembly import Storage

        revision, encoded = await Storage(pool).settings.read_settings()
        if revision >= 0 and "gateway_issuer" in encoded:
            apply_settings(revision, encoded)
        configured_issuer = (setting_env("GATEWAY_ISSUER") or "").strip() or None
        if configured_issuer is None and "gateway_issuer" not in encoded:
            # Before the main listener imports new fields, an existing database's
            # recorded namespace is already the authoritative identity source.
            configured_issuer = await pool.fetchval(
                "SELECT gateway_issuer FROM agent_identity_namespace"
            )
        if configured_issuer is None:
            raise IdentityNamespaceError(
                "Gateway identity issuer is missing. Configure it in Admin Settings before checking this deployment."
            )
        if adopt_existing and issuer != configured_issuer:
            raise IdentityNamespaceError("The adoption issuer must exactly match GATEWAY_ISSUER.")
        await ensure_gateway_namespace(pool, configured_issuer, adopt_existing=adopt_existing)
    finally:
        await pool.close()


def main() -> None:
    """Check deployment readiness or explicitly adopt the original namespace."""
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--check", action="store_true", help="Check the issuer; initialize only an empty database"
    )
    action.add_argument("--adopt-existing", action="store_true")
    parser.add_argument("--issuer", help="Original Gateway issuer, exactly as signed")
    args = parser.parse_args()
    if args.adopt_existing and not args.issuer:
        parser.error("--adopt-existing requires --issuer")
    if args.check and args.issuer is not None:
        parser.error("--issuer is only used with --adopt-existing")
    asyncio.run(_check_or_adopt(args.issuer, adopt_existing=args.adopt_existing))
    print("Agent database identity issuer verified; owner data was not changed.")


if __name__ == "__main__":
    main()
