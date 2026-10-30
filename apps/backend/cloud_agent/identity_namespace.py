"""Pin an Agent database to Cloud Agent's own accounts, and refuse any other.

An empty database is bound to the account system the first time it starts. A
database whose owners are Gateway accounts (one recording a Gateway issuer, or
one holding owner data with no binding, as every Gateway-era database does) is
refused with the cutover instructions: its owner ids name people in another
identity system, and nothing here can prove which account each would become.
There is no switch that adopts such a database.

``python -m cloud_agent.identity_namespace --check`` answers the same question
read-only, on any schema revision, before an operator migrates or starts
anything against a database.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import TYPE_CHECKING

from cloud_agent.accounts.config import CUTOVER_RUNBOOK

if TYPE_CHECKING:
    import asyncpg


class IdentityNamespaceError(RuntimeError):
    """This database's owners are not Cloud Agent accounts."""


GATEWAY_BOUND = (
    "This Agent database's owners are Gateway accounts. Cloud Agent now has its own "
    "accounts only, and does not adopt or map Gateway owners. Leave this database "
    "untouched as the retained archive, point AGENT_DATABASE_URL at a new empty "
    f"database, and create the first administrator there. See {CUTOVER_RUNBOOK}."
)
UNBOUND_OWNER_DATA = (
    "This Agent database has owner data but no account binding, so its owners are "
    "from a Gateway-era deployment. Cloud Agent does not adopt them. Leave this "
    "database untouched, point AGENT_DATABASE_URL at a new empty database, and "
    f"create the first administrator there. See {CUTOVER_RUNBOOK}."
)


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


async def _bound_to_local_accounts(conn: asyncpg.Connection) -> bool:
    # A schema from before local accounts has no marker table, and is not bound.
    if await conn.fetchval("SELECT to_regclass('agent_local_identity_namespace')") is None:
        return False
    return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM agent_local_identity_namespace)"))


async def _gateway_issuer(conn: asyncpg.Connection) -> str | None:
    if await conn.fetchval("SELECT to_regclass('agent_identity_namespace')") is None:
        return None
    return await conn.fetchval("SELECT gateway_issuer FROM agent_identity_namespace")


async def check_namespace(conn: asyncpg.Connection) -> bool:
    """Whether the database is already bound to accounts; never writes.

    Returns:
        ``True`` when bound, ``False`` when empty and so safe to bind.

    Raises:
        IdentityNamespaceError: Gateway-bound, or owner data without a binding.
    """
    if await _gateway_issuer(conn) is not None:
        raise IdentityNamespaceError(GATEWAY_BOUND)
    if await _bound_to_local_accounts(conn):
        return True
    if await _has_owner_data(conn):
        raise IdentityNamespaceError(UNBOUND_OWNER_DATA)
    return False


async def ensure_local_namespace(pool: asyncpg.Pool) -> None:
    """Bind an empty database to accounts exactly once, or check it already is.

    Raises:
        IdentityNamespaceError: The database is bound to a Gateway issuer, or
            holds owner data without any binding.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("LOCK TABLE agent_identity_namespace IN EXCLUSIVE MODE")
        if await check_namespace(conn):
            return
        await conn.execute("INSERT INTO agent_local_identity_namespace (singleton) VALUES (TRUE)")


async def _check(dsn: str) -> str:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(readonly=True):
            bound = await check_namespace(conn)
    finally:
        await conn.close()
    if bound:
        return "Agent database is bound to Cloud Agent accounts; nothing was changed."
    return "Agent database is empty and will be bound to Cloud Agent accounts on first start; nothing was changed."


def main() -> None:
    """Check, read-only, whether this release can serve ``AGENT_DATABASE_URL``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        required=True,
        help="Read-only: refuse a Gateway-era database, never change one",
    )
    parser.parse_args()
    dsn = os.environ.get("AGENT_DATABASE_URL")
    if not dsn:
        raise IdentityNamespaceError("AGENT_DATABASE_URL must be set.")
    print(asyncio.run(_check(dsn)))


if __name__ == "__main__":
    main()
