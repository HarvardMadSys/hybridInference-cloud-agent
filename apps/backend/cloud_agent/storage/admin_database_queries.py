"""Read-only, parameterized access to explicitly reviewed Agent columns."""

from __future__ import annotations

import json
import math
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg

from cloud_agent.admin_database import (
    INTEGER_TYPES,
    MAX_RESULT_BYTES,
    MAX_TEXT_CHARACTERS,
    NUMBER_TYPES,
    REVIEWED_TABLES,
    SUPPORTED_TYPES,
    TEXT_TYPES,
    TIME_TYPES,
    DatabaseColumn,
    DatabaseQuery,
    DatabaseQueryInvalid,
    DatabaseQueryResult,
    DatabaseQueryTimeout,
    DatabaseQueryUnavailable,
    DatabaseTable,
    DatabaseTables,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_SCHEMA_SQL = """
    WITH keys AS (
        SELECT k.table_name, k.column_name, k.ordinal_position
        FROM information_schema.table_constraints t
        JOIN information_schema.key_column_usage k
          ON k.constraint_catalog = t.constraint_catalog
         AND k.constraint_schema = t.constraint_schema
         AND k.constraint_name = t.constraint_name
         AND k.table_name = t.table_name
        WHERE t.table_schema = 'public' AND t.constraint_type = 'PRIMARY KEY'
    )
    SELECT c.table_name, c.column_name, c.data_type, c.is_nullable,
           k.ordinal_position AS key_position
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema = c.table_schema AND t.table_name = c.table_name
    LEFT JOIN keys k ON k.table_name = c.table_name AND k.column_name = c.column_name
    WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
      AND c.table_name = ANY($1::text[])
    ORDER BY c.table_name, c.ordinal_position
"""
_OPERATORS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _identifier(name: str) -> str:
    # Every call receives an allowlisted name already checked against metadata.
    return '"' + name.replace('"', '""') + '"'


def _typed_value(data_type: str, value: str) -> object:
    try:
        if data_type in TEXT_TYPES:
            return value
        if data_type in INTEGER_TYPES:
            number = int(value)
            bits = {"smallint": 16, "integer": 32, "bigint": 64}[data_type]
            if not -(2 ** (bits - 1)) <= number < 2 ** (bits - 1):
                raise ValueError
            return number
        if data_type == "numeric":
            decimal = Decimal(value)
            if not decimal.is_finite() or abs(decimal.adjusted()) > 1000:
                raise ValueError
            return decimal
        if data_type in {"real", "double precision"}:
            floating = float(value)
            if not math.isfinite(floating) or (
                data_type == "real" and abs(floating) > 3.4028235e38
            ):
                raise ValueError
            return floating
        if data_type == "boolean":
            if value.lower() not in {"true", "false"}:
                raise ValueError
            return value.lower() == "true"
        if data_type == "date":
            return date.fromisoformat(value)
        if data_type in TIME_TYPES:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if (instant.utcoffset() is not None) != (data_type == "timestamp with time zone"):
                raise ValueError
            return instant
        if data_type == "uuid":
            return UUID(value)
    except (ValueError, InvalidOperation, OverflowError):
        raise DatabaseQueryInvalid("Filter value does not match the column type.") from None
    raise DatabaseQueryInvalid("Unsupported column type.")


def _cell(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, int) and abs(value) > (2**53 - 1):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


class AdminDatabaseQueries:
    """One reviewed browser capability over the existing connection pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Use the shared pool without exposing a general SQL executor."""
        self._pool = pool

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[asyncpg.Connection]:
        try:
            async with (
                self._pool.acquire() as conn,
                conn.transaction(isolation="repeatable_read", readonly=True),
            ):
                await conn.execute("SET LOCAL statement_timeout = '5s'")
                yield conn
        except (asyncpg.QueryCanceledError, TimeoutError):
            raise DatabaseQueryTimeout() from None
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError):
            # Driver exceptions can include data values and private diagnostics.
            raise DatabaseQueryUnavailable() from None

    async def _schema(self, conn: asyncpg.Connection, names: list[str]) -> DatabaseTables:
        rows = await conn.fetch(_SCHEMA_SQL, names)
        tables: dict[str, DatabaseTable] = {}
        keys: dict[str, list[tuple[int, str]]] = {}
        for row in rows:
            name, column = row["table_name"], row["column_name"]
            if name not in REVIEWED_TABLES:
                continue
            table = tables.setdefault(name, DatabaseTable(name=name, columns=[], primary_key=[]))
            if row["key_position"] is not None:
                keys.setdefault(name, []).append((row["key_position"], column))
            if column in REVIEWED_TABLES[name] and row["data_type"] in SUPPORTED_TYPES:
                table.columns.append(
                    DatabaseColumn(
                        name=column,
                        data_type=row["data_type"],
                        nullable=row["is_nullable"] == "YES",
                    )
                )
        visible = []
        for name, table in sorted(tables.items()):
            table.primary_key = [column for _, column in sorted(keys.get(name, []))]
            columns = {column.name for column in table.columns}
            # A complete reviewed primary key is required for stable pagination.
            if table.primary_key and set(table.primary_key) <= columns:
                visible.append(table)
        return DatabaseTables(tables=visible)

    async def list_tables(self) -> DatabaseTables:
        """Intersect current public base-table metadata with the reviewed policy."""
        async with self._read() as conn:
            return await self._schema(conn, list(REVIEWED_TABLES))

    async def query(self, request: DatabaseQuery) -> DatabaseQueryResult:
        """Build one SELECT from reviewed identifiers and typed bound values."""
        started = perf_counter()
        if request.table not in REVIEWED_TABLES:
            raise DatabaseQueryInvalid("Table is not available in the database browser.")
        async with self._read() as conn:
            schema = await self._schema(conn, [request.table])
            if not schema.tables:
                raise DatabaseQueryInvalid("Table is not available in the database browser.")
            table = schema.tables[0]
            columns = {column.name: column for column in table.columns}
            if request.order_by is not None and request.order_by not in columns:
                raise DatabaseQueryInvalid("Sort column is not available in the database browser.")
            clauses: list[str] = []
            values: list[object] = []
            for condition in request.filters:
                column = columns.get(condition.column)
                if column is None:
                    raise DatabaseQueryInvalid(
                        "Filter column is not available in the database browser."
                    )
                name = _identifier(column.name)
                operator = condition.operator
                if operator in {"is_null", "is_not_null"}:
                    if condition.value is not None:
                        raise DatabaseQueryInvalid("Null filters do not accept a value.")
                    clauses.append(f"{name} IS {'NOT ' if operator == 'is_not_null' else ''}NULL")
                    continue
                if condition.value is None:
                    raise DatabaseQueryInvalid("Provide a value or use a null filter.")
                if operator == "contains":
                    if column.data_type not in TEXT_TYPES:
                        raise DatabaseQueryInvalid("Contains filters require a text column.")
                    values.append(condition.value)
                    clauses.append(f"strpos(lower({name}), lower(${len(values)}::text)) > 0")
                    continue
                if (
                    operator in {"gt", "gte", "lt", "lte"}
                    and column.data_type not in NUMBER_TYPES | TIME_TYPES
                ):
                    raise DatabaseQueryInvalid(
                        "Ordered comparisons require a number or date column."
                    )
                values.append(_typed_value(column.data_type, condition.value))
                clauses.append(f"{name} {_OPERATORS[operator]} ${len(values)}")
            order = list(
                dict.fromkeys(
                    [*([request.order_by] if request.order_by else []), *table.primary_key]
                )
            )
            direction = "DESC" if request.order_direction == "desc" else "ASC"
            order_sql = ", ".join(
                f"source.{_identifier(name)} {direction} NULLS LAST" for name in order
            )
            text_count = sum(column.data_type in TEXT_TYPES for column in table.columns)
            # JSON control characters may occupy six bytes per character. Bound
            # text in Postgres before it crosses the pool, reserving ample room
            # for scalar cells, keys and the response envelope.
            text_limit = min(
                MAX_TEXT_CHARACTERS,
                (MAX_RESULT_BYTES // 2) // ((request.limit + 1) * max(1, text_count) * 6),
            )
            projection = ", ".join(
                f"CASE WHEN char_length({_identifier(column.name)}) > {text_limit} "
                f"THEN LEFT({_identifier(column.name)}, {text_limit - 1}) || '…' "
                f"ELSE {_identifier(column.name)} END AS {_identifier(column.name)}"
                if column.data_type in TEXT_TYPES
                else _identifier(column.name)
                for column in table.columns
            )
            values.extend([request.limit + 1, request.offset])
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            sql = (
                f"SELECT {projection} FROM public.{_identifier(table.name)} AS source{where} "
                f"ORDER BY {order_sql} LIMIT ${len(values) - 1} OFFSET ${len(values)}"
            )
            rows = await conn.fetch(sql, *values)
        result = DatabaseQueryResult(
            table=table.name,
            columns=list(columns),
            rows=[[_cell(row[name]) for name in columns] for row in rows[: request.limit]],
            limit=request.limit,
            offset=request.offset,
            has_more=len(rows) > request.limit,
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
            text_limit=text_limit,
        )
        if len(json.dumps(result.model_dump(), ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
            raise DatabaseQueryInvalid("Result exceeds the database browser response limit.")
        return result
