"""Erasing a deleted account's Agent data: every owner row, children first.

HybridInference's hard delete removes the user and every row linked to it
(``hard_delete_user``). Here an account's data is spread over every table
that has a ``user_id`` column (the same discovery the identity namespace
guard uses), and over the rows that reference those rows through foreign keys
without a ``user_id`` of their own (a job's attempts and events, a thread's
messages). This module deletes all of it in the caller's transaction, walking
the foreign keys from the catalog so a table added later is covered too:

- A referencing row is deleted before the row it references. A nullable
  reference that would close a cycle (a job pointing at its current
  attempt) is set to NULL first.
- **Another owner's row is never touched.** If a row to delete is in a table
  with a ``user_id`` and belongs to someone else (a fork in another owner's
  sandbox, a team other members still use), the erasure stops with
  :class:`ErasureBlocked` naming the table, and the transaction rolls back.

What runs outside the database is the caller's precondition: the account is
deleted, its termination acknowledged, none of its turns queued, running or
resident, and every sandbox it owns already deleted by the sandbox deletion
path, so no host still holds its workspace. Sandbox deletion archives are
immutable objects in the deployment's bucket, which the control plane may
not delete; their keys are returned for the operator's retention policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Tables erased by the account flow itself, not by this walk.
_SKIP_PREFIX = "agent_local_"
#: Walks over the owner tables before the erasure gives up on a moving target.
_PASSES = 4


class ErasureBlocked(Exception):
    """The account's data cannot be erased without touching another owner's, or yet."""

    def __init__(self, message: str, table: str | None = None) -> None:
        """Keep the table that blocked it."""
        super().__init__(message)
        self.table = table


@dataclass(frozen=True)
class _Reference:
    child: str
    child_cols: tuple[str, ...]
    parent_cols: tuple[str, ...]
    nullable: bool


@dataclass
class _Walk:
    references: dict[str, list[_Reference]]
    owned: set[str]
    deleted: dict[str, int] = field(default_factory=dict)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _cols(names: tuple[str, ...]) -> str:
    return ", ".join(_quote(name) for name in names)


async def _references(conn: Any) -> dict[str, list[_Reference]]:
    """Every foreign key in this schema, keyed by the table it references."""
    rows = await conn.fetch(
        """
        SELECT parent.relname AS parent, child.relname AS child,
               array_agg(ca.attname ORDER BY k.ord) AS child_cols,
               array_agg(pa.attname ORDER BY k.ord) AS parent_cols,
               bool_and(NOT ca.attnotnull) AS nullable
        FROM pg_constraint c
        JOIN pg_class parent ON parent.oid = c.confrelid
        JOIN pg_class child ON child.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = child.relnamespace AND n.nspname = current_schema()
        CROSS JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(ck, pk, ord)
        JOIN pg_attribute ca ON ca.attrelid = c.conrelid AND ca.attnum = k.ck
        JOIN pg_attribute pa ON pa.attrelid = c.confrelid AND pa.attnum = k.pk
        WHERE c.contype = 'f'
        GROUP BY c.oid, parent.relname, child.relname
        """
    )
    graph: dict[str, list[_Reference]] = {}
    for row in rows:
        graph.setdefault(row["parent"], []).append(
            _Reference(
                child=row["child"],
                child_cols=tuple(row["child_cols"]),
                parent_cols=tuple(row["parent_cols"]),
                nullable=bool(row["nullable"]),
            )
        )
    return graph


async def owner_tables(conn: Any) -> set[str]:
    """The ``agent_`` tables with a ``user_id``, as the namespace guard finds owner data."""
    rows = await conn.fetch(
        "SELECT DISTINCT table_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND left(table_name, 6) = 'agent_' "
        "AND column_name = 'user_id'"
    )
    return {row["table_name"] for row in rows if not row["table_name"].startswith(_SKIP_PREFIX)}


async def _delete(
    conn: Any, walk: _Walk, table: str, where: str, args: list[Any], path: tuple[str, ...]
) -> None:
    """Delete ``table`` rows matching ``where``, after everything that references them."""
    if table in walk.owned and await conn.fetchval(
        f"SELECT EXISTS (SELECT 1 FROM {_quote(table)} WHERE ({where}) AND user_id <> $1)", *args
    ):
        raise ErasureBlocked(
            "The account's data is shared with another account and cannot be erased.", table
        )
    for ref in walk.references.get(table, []):
        selected = (
            f"({_cols(ref.child_cols)}) IN (SELECT {_cols(ref.parent_cols)} "
            f"FROM {_quote(table)} WHERE {where})"
        )
        if ref.child in path or ref.child == table:
            if not ref.nullable:
                continue
            assignments = ", ".join(f"{_quote(col)} = NULL" for col in ref.child_cols)
            await conn.execute(
                f"UPDATE {_quote(ref.child)} SET {assignments} WHERE {selected}", *args
            )
            continue
        await _delete(conn, walk, ref.child, selected, args, (*path, table))
    status = await conn.execute(f"DELETE FROM {_quote(table)} WHERE {where}", *args)
    count = int(status.split()[-1])
    if count:
        walk.deleted[table] = walk.deleted.get(table, 0) + count


async def erase_owner_rows(conn: Any, account_id: str) -> dict[str, int]:
    """Delete every row of the account's Agent data; rows deleted per table.

    Raises:
        ErasureBlocked: A row to delete belongs to another owner, or the
            account still holds a sandbox that is not deleted.
    """
    if await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM agent_sandboxes WHERE user_id = $1 "
        "AND deletion_state <> 'deleted')",
        account_id,
    ):
        raise ErasureBlocked(
            "Delete the account's sandboxes first (Admin → Sandboxes); a host still holds them.",
            "agent_sandboxes",
        )
    walk = _Walk(references=await _references(conn), owned=await owner_tables(conn))
    # Deleting rows fires triggers that record the change for the owner (the
    # notification feed), which writes new owner rows. The walk repeats until
    # a pass finds nothing left: the erasure is its own write fence.
    for _ in range(_PASSES):
        for table in sorted(walk.owned):
            await _delete(conn, walk, table, "user_id = $1", [account_id], ())
        if not await _remaining(conn, walk.owned, account_id):
            break
    else:
        raise ErasureBlocked("The account's data kept changing while it was being erased.")
    await conn.execute("DELETE FROM agent_admin_audit_log WHERE target_user_id = $1", account_id)
    return walk.deleted


async def _remaining(conn: Any, tables: set[str], account_id: str) -> bool:
    for table in sorted(tables):
        if await conn.fetchval(
            f"SELECT EXISTS (SELECT 1 FROM {_quote(table)} WHERE user_id = $1)", account_id
        ):
            return True
    return False


async def retained_archives(conn: Any, account_id: str) -> list[str]:
    """The archive object keys of the account's deleted sandboxes, which stay in the bucket."""
    rows = await conn.fetch(
        "SELECT deletion_archive_key FROM agent_sandboxes "
        "WHERE user_id = $1 AND deletion_archive_key IS NOT NULL ORDER BY id",
        account_id,
    )
    return [row["deletion_archive_key"] for row in rows]


__all__ = ["ErasureBlocked", "erase_owner_rows", "owner_tables", "retained_archives"]
