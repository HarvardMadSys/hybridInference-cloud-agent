"""The lease fence, tested where it is decided: in SQL, against the database clock.

An attempt is the unit of execution, and its lease carries a fencing
generation. Every write a worker makes is refused unless all three hold:

* the attempt row is still ``running``;
* its ``lease_generation`` still equals the one the worker was handed; and
* its lease has not expired, judged by the **database** clock.

The third clause is what this file is mostly about, because the failure it
prevents is invisible in a fast test suite: ``NOW()`` is the *transaction
start* instant in Postgres, so a statement inside a long transaction reads a
lease as live long after it lapsed. ``clock_timestamp()`` is the wall clock at
execution. The difference is a stale worker publishing after its lease was
reaped -- the duplicate-pull-request failure the whole design exists to stop.

These tests drive the repository through a recording fake connection rather
than a database, so they assert the *statement* and its bindings. That is
deliberate: this repository has a ``dbtest`` tier for the kernel's own
behaviour, and what is not covered there is that the compiled predicate is the
one a reader believes it is. A real-database test proves one run of one query;
this proves the predicate is present in every copy of it, which is the property
that actually gets broken by a later edit.

The optional-generation question is pinned at the end. It is a live hazard:
``release_claim`` accepts ``lease_generation=None``, and the SQL compiles that
into ``($2::bigint IS NULL OR lease_generation = $2)`` -- a tautology that
degrades the fence to "attempt id only". No production caller omits it today;
``test_no_production_caller_releases_a_claim_without_a_generation`` is what
keeps that true.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from cloud_agent.storage.repositories import attempt_commands

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "apps" / "backend"
COMMANDS = REPO / "apps" / "backend" / "cloud_agent/storage/repositories/attempt_commands.py"


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _RecordingConnection:
    """Record every statement and parameter list, and answer the ones we need.

    The fence is one CTE statement read with ``fetchrow``: the lock is taken in
    a subquery and the lease is compared in the outer query, so the clock is
    read after any wait for the lock. ``lock_row`` drives whether that inner
    select matches, and ``lease_live`` drives the outer comparison against it.
    """

    def __init__(self, *, lock_row: bool = True, lease_live: bool = True) -> None:
        self.lock_row = lock_row
        self.lease_live = lease_live
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    def _fence_row(self, sql: str) -> Any:
        if "WITH locked AS" not in sql:
            return _MISS
        if not self.lock_row:
            return None
        return {"job_id": "ajob_1", "live": self.lease_live}

    async def fetchval(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((sql, args))
        if "SET lease_expires_at" in sql:
            # A successful extension returns the job the attempt belongs to.
            return "ajob_1"
        return None

    async def fetchrow(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((sql, args))
        fence = self._fence_row(sql)
        if fence is not _MISS:
            return fence
        if "FROM agent_jobs" in sql:
            return {"state": "running", "cancel_requested": False}
        return None

    async def fetch(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((sql, args))
        return []

    async def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((sql, args))
        return None

    def statements(self) -> list[str]:
        return [sql for sql, _ in self.calls]


#: Distinguishes "this statement is not the fence" from "the fence matched
#: nothing", which are both falsy and mean opposite things here.
_MISS = object()


class _Acquire:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _RecordingConnection:
        return self._connection

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)


def _commands(connection: _RecordingConnection) -> attempt_commands.PostgresAttemptCommands:
    return attempt_commands.PostgresAttemptCommands(_Pool(connection))  # type: ignore[arg-type]


# ── the fence predicate itself ────────────────────────────────────────────


async def test_the_lock_tests_generation_in_sql_before_examining_the_lease() -> None:
    """Generation and status are in the locked SELECT, not checked afterward.

    Checking them after the lock would be a different, racier program: the row
    could be superseded between the read and the caller's use of it. The
    predicate belongs in the statement that takes the lock.
    """
    connection = _RecordingConnection()
    assert await attempt_commands.PostgresAttemptCommands._lock_live_attempt(
        connection,
        7,
        3,  # type: ignore[arg-type]
    )
    sql, args = connection.calls[0]
    assert "FOR UPDATE" in sql
    assert "lease_generation = $2" in sql
    assert "status = 'running'" in sql
    assert args == (7, 3)


async def test_the_expiry_check_uses_the_database_clock_after_the_lock() -> None:
    """``clock_timestamp()``, not ``NOW()``: the transaction's start is not the now.

    A worker whose lease lapsed while another held the row must not be able to
    write because its own transaction began earlier. ``NOW()`` would hand it
    that window; ``clock_timestamp()`` does not.
    """
    connection = _RecordingConnection()
    await attempt_commands.PostgresAttemptCommands._lock_live_attempt(
        connection,
        7,
        3,  # type: ignore[arg-type]
    )
    expiry_sql = [sql for sql in connection.statements() if "lease_expires_at >" in sql]
    assert expiry_sql, "the post-lock expiry re-check is missing entirely"
    assert all("clock_timestamp()" in sql for sql in expiry_sql)
    assert not any("NOW()" in sql.upper() for sql in expiry_sql)


async def test_a_superseded_attempt_is_refused_before_its_lease_is_even_read() -> None:
    """When the locked row is gone there is nothing to re-check, and no write."""
    connection = _RecordingConnection(lock_row=False)
    assert not await attempt_commands.PostgresAttemptCommands._lock_live_attempt(
        connection,
        7,
        3,  # type: ignore[arg-type]
    )
    assert len(connection.calls) == 1, "the lease was read for an attempt we do not own"


async def test_an_unexpired_lease_under_the_right_generation_is_accepted() -> None:
    connection = _RecordingConnection(lock_row=True, lease_live=True)
    assert await attempt_commands.PostgresAttemptCommands._lock_live_attempt(
        connection,
        7,
        3,  # type: ignore[arg-type]
    )


async def test_a_live_generation_with_a_lapsed_lease_is_refused() -> None:
    """The mismatch case the whole file exists for: right attempt, dead lease."""
    connection = _RecordingConnection(lock_row=True, lease_live=False)
    assert not await attempt_commands.PostgresAttemptCommands._lock_live_attempt(
        connection,
        7,
        3,  # type: ignore[arg-type]
    )


# ── every fenced writer repeats the predicate ─────────────────────────────


async def test_heartbeat_returns_not_ok_when_the_lease_is_lost() -> None:
    connection = _RecordingConnection(lock_row=False)
    assert await _commands(connection).heartbeat(
        attempt_id=7, lease_generation=3, lease_ttl_seconds=120
    ) == {"ok": False}
    assert not [
        sql for sql in connection.statements() if sql.strip().startswith("UPDATE agent_attempts")
    ], "a heartbeat wrote after losing the lease"


async def test_heartbeat_extends_the_lease_from_the_database_clock() -> None:
    connection = _RecordingConnection(lock_row=True, lease_live=True)
    await _commands(connection).heartbeat(attempt_id=7, lease_generation=3, lease_ttl_seconds=120)
    updates = [sql for sql in connection.statements() if "SET lease_expires_at" in sql]
    assert updates, "a successful heartbeat extended nothing"
    assert all("clock_timestamp()" in sql for sql in updates)
    assert all("lease_generation = $2" in sql for sql in updates)


async def test_heartbeat_also_requires_the_host_to_still_be_enrolled() -> None:
    """A removed host must not keep its leases alive by heartbeating.

    The worker token proves the fence and outlives the host credential it was
    issued under, so the fence alone is not enough here. This is the one
    predicate an "obvious" refactor is most likely to drop.
    """
    connection = _RecordingConnection(lock_row=True, lease_live=True)
    await _commands(connection).heartbeat(attempt_id=7, lease_generation=3, lease_ttl_seconds=120)
    updates = [sql for sql in connection.statements() if "SET lease_expires_at" in sql]
    assert updates
    assert all("LIVE_ATTEMPT_HOST" in sql or "agent_runner_hosts" in sql for sql in updates), (
        "the heartbeat no longer re-checks that the machine is still enrolled"
    )


async def test_recording_continuity_is_refused_after_the_lease_lapses() -> None:
    connection = _RecordingConnection(lock_row=True, lease_live=False)
    assert not await _commands(connection).record_continuity_mode(
        attempt_id=7, lease_generation=3, mode="hot", context_status="live"
    )


async def test_the_resource_rollup_is_fenced_by_both_generation_and_expiry() -> None:
    connection = _RecordingConnection(lock_row=True, lease_live=False)
    assert not await _commands(connection).record_attempt_resources(
        attempt_id=7, lease_generation=3, rollup={}
    )
    assert not [
        sql for sql in connection.statements() if "INSERT INTO agent_attempt_resources" in sql
    ], "resource rows were written for an attempt whose lease had lapsed"


# ── the optional generation is a tautology, and nothing uses it ──────────


async def test_an_omitted_generation_turns_the_predicate_into_a_tautology() -> None:
    """Pin the hazard, so its removal is a deliberate edit and not a surprise.

    ``($2::bigint IS NULL OR lease_generation = $2)`` is true for every row
    when the caller passes ``None``. That is the behaviour today, and this test
    says so out loud: the fence degrades to "this attempt id exists and is
    running". If someone tightens it -- for example by refusing ``None`` -- this
    test fails and the change gets the review it deserves.
    """
    connection = _RecordingConnection()
    await attempt_commands.PostgresAttemptCommands._lock_live_attempt(
        connection,
        7,
        None,  # type: ignore[arg-type]
    )
    sql, args = connection.calls[0]
    assert "($2::bigint IS NULL OR lease_generation = $2)" in sql
    assert args == (7, None)


def test_no_production_caller_releases_a_claim_without_a_generation() -> None:
    """Keep every ``release_claim`` call site fenced.

    The parameter is optional in the port and in the implementation, which is
    exactly why this is checked from source rather than trusted: a future
    caller that omits it silently weakens retry safety, and nothing in the type
    system will complain. ``**fence`` is accepted because the one dict passed
    that way is built with ``lease_generation`` in it -- see
    ``test_the_starred_fence_dicts_do_carry_a_generation``.
    """
    offenders: list[str] = []
    for path in BACKEND.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        source = path.read_text(encoding="utf-8")
        if "release_claim" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "release_claim":
                continue
            keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
            starred = any(keyword.arg is None for keyword in node.keywords)
            if "lease_generation" not in keywords and not starred:
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not offenders, f"release_claim called without a lease generation: {offenders}"


def test_the_starred_fence_dicts_do_carry_a_generation() -> None:
    """The two ``**fence`` call sites pass a dict that names the generation.

    A starred argument defeats the check above, so it needs its own: the dict
    is built once in ``api/worker.py`` and must keep carrying all three fields.
    Dropping ``lease_generation`` from it would fence on attempt id alone.
    """
    source = (BACKEND / "cloud_agent/api/worker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fence_dicts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", None) == "fence"
        and isinstance(node.value, ast.Dict)
    ]
    assert fence_dicts, "the fence dict the release calls spread is gone; re-check those call sites"
    for node in fence_dicts:
        keys = {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        assert "lease_generation" in keys, (
            f"the fence dict at line {node.lineno} no longer carries a generation"
        )


# ── the reaper treats a lapsed lease as the only thing it closes ─────────


def test_the_reaper_selects_expired_attempts_by_the_database_clock() -> None:
    """A reaper on the application clock would close live attempts.

    The sweep statement is the counterpart to the fence: if it read time from
    the process, a control plane whose clock ran ahead would reap healthy
    attempts on every pass.
    """
    connection = _RecordingConnection()
    source = COMMANDS.read_text(encoding="utf-8")
    assert "async def reap_expired" in source
    reap_start = source.index("async def reap_expired")
    sweep = source[reap_start : reap_start + 1200]
    assert "lease_expires_at < clock_timestamp()" in sweep, (
        "the reaper's sweep no longer compares against the database clock"
    )
    assert connection.calls == []


@pytest.mark.parametrize("method", ["heartbeat", "transition", "reap_expired"])
def test_the_lifecycle_writers_exist_where_the_port_says_they_do(method: str) -> None:
    """Cheap shape check: a rename must not silently unhook a fence."""
    assert hasattr(attempt_commands.PostgresAttemptCommands, method)
