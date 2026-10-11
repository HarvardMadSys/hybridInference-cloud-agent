"""Database browser SQL boundaries, typing and response precision."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import asyncpg
import pytest

from cloud_agent.admin_database import (
    DatabaseQuery,
    DatabaseQueryInvalid,
    DatabaseQueryTimeout,
    DatabaseQueryUnavailable,
)
from cloud_agent.storage.admin_database_queries import AdminDatabaseQueries, _cell, _typed_value


class Context:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *exc: object) -> bool:
        return False


class Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.transactions: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.rows: list[dict[str, Any]] = []
        self.schema = [
            {
                "table_name": "agent_jobs",
                "column_name": "id",
                "data_type": "text",
                "is_nullable": "NO",
                "key_position": 1,
            },
            {
                "table_name": "agent_jobs",
                "column_name": "turn_no",
                "data_type": "integer",
                "is_nullable": "NO",
                "key_position": None,
            },
            {
                "table_name": "agent_jobs",
                "column_name": "created_at",
                "data_type": "timestamp with time zone",
                "is_nullable": "NO",
                "key_position": None,
            },
            {
                "table_name": "agent_jobs",
                "column_name": "task_prompt",
                "data_type": "text",
                "is_nullable": "NO",
                "key_position": None,
            },
            {
                "table_name": "agent_jobs",
                "column_name": "metadata",
                "data_type": "jsonb",
                "is_nullable": "YES",
                "key_position": None,
            },
        ]

    def transaction(self, **kwargs: Any) -> Context:
        self.transactions.append(kwargs)
        return Context()

    async def execute(self, sql: str) -> None:
        self.calls.append((sql, ()))

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if self.error:
            raise self.error
        return self.schema if "information_schema.columns" in sql else self.rows


class Pool:
    def __init__(self) -> None:
        self.connection = Connection()

    def acquire(self) -> Context:
        return Context(self.connection)


def reader() -> tuple[AdminDatabaseQueries, Connection]:
    pool = Pool()
    return AdminDatabaseQueries(cast("Any", pool)), pool.connection


async def test_metadata_excludes_unreviewed_columns_and_requires_complete_safe_keys():
    queries, conn = reader()
    tables = await queries.list_tables()
    assert [column.name for column in tables.tables[0].columns] == ["id", "turn_no", "created_at"]
    assert tables.tables[0].primary_key == ["id"]
    conn.schema[3]["key_position"] = 2
    assert (await queries.list_tables()).tables == []


async def test_values_are_bound_and_order_is_stable_and_transaction_is_readonly():
    queries, conn = reader()
    attack = "x' OR true; DROP TABLE agent_jobs; --_%"
    result = await queries.query(
        DatabaseQuery.model_validate(
            {
                "table": "agent_jobs",
                "filters": [{"column": "id", "operator": "contains", "value": attack}],
                "order_by": "turn_no",
                "order_direction": "desc",
                "limit": 1,
                "offset": 17,
            }
        )
    )
    sql, args = conn.calls[-1]
    assert attack not in sql
    assert args == (attack, 2, 17)
    assert 'strpos(lower("id"), lower($1::text)) > 0' in sql
    assert 'ORDER BY source."turn_no" DESC NULLS LAST, source."id" DESC NULLS LAST' in sql
    assert "task_prompt" not in sql and "metadata" not in sql and "SELECT *" not in sql
    assert 'LEFT("id",' in sql and "|| '…'" in sql
    assert conn.transactions == [{"isolation": "repeatable_read", "readonly": True}]
    assert conn.calls[0][0] == "SET LOCAL statement_timeout = '5s'"
    assert result.rows == [] and result.columns == ["id", "turn_no", "created_at"]
    assert result.limit == 1 and result.offset == 17 and not result.has_more


@pytest.mark.parametrize(
    "body",
    [
        {"table": "agent_jobs; DELETE FROM agent_jobs"},
        {"table": "agent_settings"},
        {"table": "agent_jobs", "order_by": "task_prompt"},
        {"table": "agent_jobs", "filters": [{"column": "metadata", "operator": "is_null"}]},
        {
            "table": "agent_jobs",
            "filters": [{"column": "turn_no", "operator": "contains", "value": "1"}],
        },
        {
            "table": "agent_jobs",
            "filters": [
                {"column": "turn_no", "operator": "eq", "value": "secret-in-invalid-number"}
            ],
        },
        {"table": "agent_jobs", "filters": [{"column": "id", "operator": "gt", "value": "1"}]},
        {"table": "agent_jobs", "filters": [{"column": "id", "operator": "eq"}]},
        {
            "table": "agent_jobs",
            "filters": [{"column": "id", "operator": "is_null", "value": "secret"}],
        },
        {
            "table": "agent_jobs",
            "filters": [{"column": "created_at", "operator": "gt", "value": "2026-10-01T00:00:00"}],
        },
    ],
)
async def test_invalid_or_sensitive_queries_never_execute_data_sql_or_echo_values(body):
    queries, conn = reader()
    with pytest.raises(DatabaseQueryInvalid) as caught:
        await queries.query(DatabaseQuery.model_validate(body))
    assert "secret" not in str(caught.value)
    assert not any(sql.startswith("SELECT ") for sql, _ in conn.calls)


async def test_typed_filters_bound_numbers_and_dates_with_null_predicates():
    queries, conn = reader()
    instant = "2026-10-11T04:00:00Z"
    await queries.query(
        DatabaseQuery.model_validate(
            {
                "table": "agent_jobs",
                "filters": [
                    {"column": "turn_no", "operator": "gte", "value": "123"},
                    {"column": "created_at", "operator": "lt", "value": instant},
                    {"column": "created_at", "operator": "is_not_null"},
                ],
            }
        )
    )
    sql, args = conn.calls[-1]
    assert args == (123, datetime(2026, 10, 11, 4, tzinfo=UTC), 51, 0)
    assert '"created_at" IS NOT NULL' in sql


async def test_pagination_uses_extra_row_and_encodes_cells_without_precision_loss():
    queries, conn = reader()
    conn.rows = [
        {"id": "first", "turn_no": 2**53 + 1, "created_at": datetime(2026, 10, 11, tzinfo=UTC)},
        {"id": "second", "turn_no": 2, "created_at": None},
    ]
    result = await queries.query(DatabaseQuery(table="agent_jobs", limit=1))
    assert result.rows == [["first", "9007199254740993", "2026-10-11T00:00:00+00:00"]]
    assert result.has_more
    assert _cell(Decimal("123.4500")) == "123.4500"
    assert _cell(float("inf")) == "inf"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (asyncpg.QueryCanceledError("private data"), DatabaseQueryTimeout),
        (asyncpg.PostgresError("private credentials"), DatabaseQueryUnavailable),
        (OSError("private host"), DatabaseQueryUnavailable),
    ],
)
async def test_database_failures_are_sanitized(error, expected):
    queries, conn = reader()
    conn.error = error
    with pytest.raises(expected) as caught:
        await queries.list_tables()
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("bigint", "9223372036854775808"),
        ("integer", "2147483648"),
        ("numeric", "NaN"),
        ("double precision", "Infinity"),
        ("boolean", "secret"),
    ],
)
def test_invalid_scalar_types_do_not_echo_values(kind, value):
    with pytest.raises(DatabaseQueryInvalid) as caught:
        _typed_value(kind, value)
    assert value not in str(caught.value)
