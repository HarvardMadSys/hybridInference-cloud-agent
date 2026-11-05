"""The named attempt-lease fence variants render the statements their callers used to spell.

Each variant in :mod:`cloud_agent.storage._sql.fences` replaced copies that
were written out at their call sites. These snapshots pin the rendered SQL of
every variant, lock clause and clock source included, to the text those copies
had, so a later edit to a variant is a visible change to every caller at once
rather than a silent one. ``dbtest`` covers what each predicate does against a
real database; ``test_lease_fence`` covers :func:`~cloud_agent.storage._sql.fences.lock_live_attempt`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from cloud_agent.storage._sql import fences

STORAGE = Path(__file__).resolve().parents[2] / "apps/backend/cloud_agent/storage"


class _RecordingConnection:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return self.answer


def _flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_the_leased_attempt_predicate_is_the_inline_one_it_replaced() -> None:
    assert fences.leased_attempt_sql("$2", "$3") == (
        "a.id = $2 AND a.lease_generation = $3"
        " AND a.status = 'running' AND a.lease_expires_at > clock_timestamp()"
    )


def test_the_leased_attempt_predicate_names_bare_columns_without_an_alias() -> None:
    assert fences.leased_attempt_sql("$1", "$2", alias="") == (
        "id = $1 AND lease_generation = $2"
        " AND status = 'running' AND lease_expires_at > clock_timestamp()"
    )


@pytest.mark.parametrize(
    ("mode", "statement"),
    [
        (
            fences.LockMode.SHARE,
            "SELECT id FROM agent_attempts "
            "WHERE id = $1 AND lease_generation = $2 AND status = 'running' FOR SHARE",
        ),
        (
            fences.LockMode.UPDATE,
            "SELECT id FROM agent_attempts "
            "WHERE id = $1 AND lease_generation = $2 AND status = 'running' FOR UPDATE",
        ),
    ],
)
async def test_the_running_attempt_lock_takes_the_callers_mode_and_reads_no_clock(
    mode: fences.LockMode, statement: str
) -> None:
    connection = _RecordingConnection(answer=7)
    assert await fences.lock_running_attempt(connection, 7, 3, mode)
    assert connection.calls == [(statement, (7, 3))]


async def test_the_running_attempt_lock_refuses_when_no_row_is_locked() -> None:
    assert not await fences.lock_running_attempt(
        _RecordingConnection(answer=None), 7, 3, fences.LockMode.SHARE
    )


async def test_the_lease_is_read_by_the_wall_clock_after_the_lock() -> None:
    connection = _RecordingConnection(answer=False)
    assert not await fences.attempt_lease_is_live(connection, 7)
    assert connection.calls == [
        ("SELECT lease_expires_at > clock_timestamp() FROM agent_attempts WHERE id = $1", (7,))
    ]


async def test_the_attempt_and_residency_leases_are_read_together() -> None:
    connection = _RecordingConnection(answer=True)
    assert await fences.attempt_and_residency_are_live(connection, 7, "athr_1")
    [(sql, args)] = connection.calls
    assert _flat(sql) == (
        "SELECT a.lease_expires_at > clock_timestamp()"
        " AND m.residency_lease_expires_at > clock_timestamp()"
        " FROM agent_attempts a, agent_thread_sandbox_memberships m"
        " WHERE a.id = $1 AND m.thread_id = $2"
    )
    assert args == (7, "athr_1")


def test_no_repository_spells_the_two_step_fence_itself() -> None:
    """The two-statement fence's clock reads live in one place; a copy elsewhere can drift."""
    clock_reads = (
        "SELECT lease_expires_at > clock_timestamp() FROM agent_attempts WHERE id = $1",
        "SELECT a.lease_expires_at > clock_timestamp()"
        " AND m.residency_lease_expires_at > clock_timestamp()"
        " FROM agent_attempts a, agent_thread_sandbox_memberships m",
    )
    copies = [
        path.name
        for path in STORAGE.rglob("*.py")
        if path.name != "fences.py"
        and any(read in _flat(path.read_text(encoding="utf-8")) for read in clock_reads)
    ]
    assert copies == []
