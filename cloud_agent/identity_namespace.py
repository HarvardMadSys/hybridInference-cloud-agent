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
                "Stop old Agent processes and explicitly initialize its original issuer with "
                "python -m cloud_agent.identity_namespace --adopt-existing --issuer <original-issuer>."
            )
        await conn.execute(
            "INSERT INTO agent_identity_namespace (singleton, gateway_issuer) VALUES (TRUE, $1)",
            issuer,
        )
        return issuer


async def _adopt(issuer: str) -> None:
    import asyncpg

    from cloud_agent.identity import gateway_issuer

    if issuer != gateway_issuer():
        raise IdentityNamespaceError("The adoption issuer must exactly match GATEWAY_ISSUER.")
    dsn = os.environ.get("AGENT_DATABASE_URL")
    if not dsn:
        raise IdentityNamespaceError("AGENT_DATABASE_URL must be set.")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        await ensure_gateway_namespace(pool, issuer, adopt_existing=True)
    finally:
        await pool.close()


def main() -> None:
    """Explicitly adopt a legacy database into its original Gateway namespace."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adopt-existing", action="store_true", required=True)
    parser.add_argument(
        "--issuer", required=True, help="Original Gateway issuer, exactly as signed"
    )
    args = parser.parse_args()
    asyncio.run(_adopt(args.issuer))
    print("Agent database identity issuer verified and recorded; owner data was not changed.")


if __name__ == "__main__":
    main()
