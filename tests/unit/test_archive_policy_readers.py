"""Every reader of the stored archive policy, given one it can read and one it cannot.

An administrator's AWS access key is stored sealed, as ``{"ciphertext": ...}``,
and ``stored_archive_policy`` is the one parse that opens it. #984 was a reader
that parsed the stored settings itself, and so refused every policy with a
saved key. Each reader here is the real one, over a connection that answers its
queries with the row ``set_sandbox_archive_settings`` writes.

A policy whose key opens is read as stored. One that does not parse, or whose
key no longer opens, gets each reader's own answer: the claim, the autoscaler,
queue health, move-out and environment snapshots refuse what the policy would
allow; the heartbeat answer reads it again once the beat's own work is done and
fails there, as the admin read and write do.
"""

from __future__ import annotations

import ast
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_autoscaling_archive_policy import (
    _Connection as _AutoscalingConnection,
    _observe,
)

from cloud_agent.sandbox_archive_credentials import seal_archive_storage, stored_archive_policy
from cloud_agent.storage._sql.move_out import move_out_blocker
from cloud_agent.storage._sql.runner_policy import heartbeat_policy
from cloud_agent.storage.repositories import attempt_commands
from cloud_agent.storage.repositories.environment_snapshots import (
    PostgresEnvironmentSnapshotRepository,
)
from cloud_agent.storage.repositories.host_pool import PostgresHostPoolRepository
from cloud_agent_shared.sandbox_archive_settings import (
    SANDBOX_ARCHIVE_CAPABILITY,
    SANDBOX_CHECKPOINT_CAPABILITY,
    SandboxArchivePolicy,
    SandboxArchivePolicyError,
)

CONTROL_PLANE = Path(__file__).resolve().parents[2] / "apps" / "backend" / "cloud_agent"

# AWS's documented example key pair, which no account accepts.
_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_SETTINGS_KEY = "test-settings-root-key-over-thirty-two-bytes"

#: An administrator's request: archives on, uploading with an access key.
_SETTINGS = {
    "enabled": True,
    "region": "us-east-1",
    "aws_access_key_id": _KEY_ID,
    "aws_secret_access_key": _SECRET,
}
#: The policy it stored, at the revision every Host here has applied.
_POLICY = SandboxArchivePolicy.from_mapping({**_SETTINGS, "revision": 3})
#: What a Host and the replica asking for its work declare.
_CLAIMING = ["shared-sandbox-v1", SANDBOX_ARCHIVE_CAPABILITY]


class _Stored(NamedTuple):
    row: dict[str, Any]
    readable: bool


def _row(settings: dict[str, Any]) -> dict[str, Any]:
    """The ``agent_runner_policy`` row as asyncpg returns it, JSONB as its text."""
    return {
        "active_host": None,
        "host_settings": None,
        "runtime_versions": None,
        "runtime_upgrades": None,
        "sandbox_archive_settings": json.dumps(settings),
        "sandbox_archive_revision": _POLICY.revision,
        "sandbox_archive_updated_at": datetime(2026, 10, 23, tzinfo=UTC),
    }


@pytest.fixture(params=["key-opens", "does-not-parse", "key-does-not-open"])
def stored(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> _Stored:
    """The stored policy a reader is given, and whether it can be read."""
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", _SETTINGS_KEY)
    if request.param == "does-not-parse":
        # Archives on without the region they need.
        return _Stored(_row({"enabled": True}), readable=False)
    row = _row(seal_archive_storage(_POLICY.as_storage_dict()))
    if request.param == "key-does-not-open":
        # The settings key changed without re-encrypting what it had sealed.
        monkeypatch.setenv(
            "AGENT_SETTINGS_ENCRYPTION_KEY", "another-test-settings-key-over-32-bytes"
        )
    return _Stored(row, readable=request.param == "key-opens")


def _pool(connection: Any) -> Any:
    @asynccontextmanager
    async def acquire():
        yield connection

    return SimpleNamespace(acquire=acquire)


@asynccontextmanager
async def _transaction(**_options: Any):
    yield


def test_the_stored_key_parses_only_once_it_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """#984's trap: parsed as stored, the sealed key refuses the whole policy.

    So a reader below that reads this policy has opened its key.
    """
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", _SETTINGS_KEY)
    row = _row(seal_archive_storage(_POLICY.as_storage_dict()))

    with pytest.raises(SandboxArchivePolicyError, match="aws_secret_access_key must be a trimmed"):
        SandboxArchivePolicy.from_mapping(json.loads(row["sandbox_archive_settings"]))
    assert stored_archive_policy(row).equivalent(_POLICY)


class _ClaimConnection:
    """A Host on the policy's revision asking for work, and an empty queue."""

    def __init__(self, policy: dict[str, Any]) -> None:
        self.policy = policy
        self.read_the_queue = False

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, sql: str, *_args: Any) -> dict[str, Any] | None:
        # First: the queue's query names the other two tables in its fragments.
        if "FROM agent_jobs j" in sql:
            self.read_the_queue = True
            return None
        if "FROM agent_runner_policy" in sql:
            return self.policy
        if "FROM agent_runner_hosts" in sql:
            return {
                "status": "online",
                "capabilities": json.dumps(_CLAIMING),
                "slots_total": 1,
                "slots_used": 0,
                "slots_desired": None,
                "sandbox_archive_settings_applied_revision": _POLICY.revision,
                "credential_matches": True,
            }
        raise AssertionError(f"unexpected query: {sql}")


async def test_the_claim_reads_the_queue_only_under_a_policy_it_can_read(
    stored: _Stored, monkeypatch: pytest.MonkeyPatch
) -> None:
    for attested in (
        "shared_sandbox_claim_attested",
        "compatible_shared_sandbox_followup_attested",
    ):
        monkeypatch.setattr(attempt_commands, attested, lambda *_args, **_kwargs: True)
    monkeypatch.setattr(attempt_commands, "lock_compute_topology", AsyncMock())
    monkeypatch.setattr(
        attempt_commands, "host_compute_admission", AsyncMock(return_value=(True, True))
    )
    connection = _ClaimConnection(stored.row)

    claim = await attempt_commands.PostgresAttemptCommands(_pool(connection)).claim_job(
        worker_id="worker-1",
        lease_ttl_seconds=30,
        host="runner-a",
        host_credential_hash="credential-hash",
        claim_attestation={
            "runner_capabilities": _CLAIMING,
            "sandbox_archive_settings_applied_revision": _POLICY.revision,
        },
    )

    # Nothing is queued either way: a policy it cannot read refuses the Host first.
    assert claim is None
    assert connection.read_the_queue is stored.readable


def test_the_heartbeat_answer_carries_only_a_policy_it_can_read(stored: _Stored) -> None:
    archive = heartbeat_policy(stored.row)["sandbox_archive_settings"]

    if stored.readable:
        # The Host's sandbox manager receives the key opened.
        assert (archive["revision"], archive["aws_secret_access_key"]) == (3, _SECRET)
    else:
        # Read again once the beat's own work is done, where it fails the answer.
        assert archive is None


async def test_the_admin_read_fails_on_a_policy_it_cannot_read(stored: _Stored) -> None:
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value=stored.row))
    read = PostgresHostPoolRepository(_pool(connection)).sandbox_archive_settings()

    if stored.readable:
        values = await read
        assert (values["revision"], values["aws_secret_access_key_configured"]) == (3, True)
    else:
        with pytest.raises(SandboxArchivePolicyError):
            await read


async def test_the_admin_write_fails_over_a_policy_it_cannot_read(stored: _Stored) -> None:
    connection = SimpleNamespace(
        transaction=_transaction,
        execute=AsyncMock(),
        fetchrow=AsyncMock(return_value=stored.row),
        fetchval=AsyncMock(),
    )
    write = PostgresHostPoolRepository(_pool(connection)).set_sandbox_archive_settings(
        expected_revision=_POLICY.revision, settings=_SETTINGS
    )

    if stored.readable:
        # The same request again: the stored key opened, and matches it.
        assert (await write)["_changed"] is False
    else:
        with pytest.raises(SandboxArchivePolicyError):
            await write
    connection.fetchval.assert_not_awaited()


async def test_the_autoscaler_counts_a_host_only_under_a_policy_it_can_read(
    stored: _Stored,
) -> None:
    connection = _AutoscalingConnection(_POLICY, applied_revision=_POLICY.revision)
    connection.policy_row = stored.row

    observation = await _observe(connection)
    assert observation["fresh_effective_free_slots"] == (2 if stored.readable else 0)


async def test_queue_health_counts_a_host_only_under_a_policy_it_can_read(
    stored: _Stored,
) -> None:
    connection = SimpleNamespace(
        transaction=_transaction,
        fetchrow=AsyncMock(side_effect=[stored.row, {"queued": 1, "unavailable": 1, "running": 0}]),
    )

    await PostgresHostPoolRepository(_pool(connection)).queue_health(
        expected_deployment_sha="test-deployment"
    )
    # Archives required, at the revision a serving Host must have applied. A
    # policy it cannot read asks for one no Host has, as the claim refuses them.
    required, revision = connection.fetchrow.await_args.args[4:6]
    assert (required, revision) == (True, 3 if stored.readable else -1)


async def test_move_out_starts_only_under_a_policy_it_can_read(stored: _Stored) -> None:
    leaving = {
        "capabilities": json.dumps([SANDBOX_CHECKPOINT_CAPABILITY]),
        "sandbox_archive_settings_applied_revision": _POLICY.revision,
        "enrolled": True,
        "alive": True,
    }
    connection = SimpleNamespace(
        fetchrow=AsyncMock(return_value=stored.row), fetch=AsyncMock(return_value=[leaving])
    )

    blocker = await move_out_blocker(connection, ["runner-a"], need_target=False)
    assert blocker == (None if stored.readable else "archives_disabled")


async def test_environment_snapshots_are_on_only_under_a_policy_they_can_read(
    stored: _Stored,
) -> None:
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value=stored.row))

    snapshots = PostgresEnvironmentSnapshotRepository(_pool(connection))
    assert await snapshots.enabled() is stored.readable


#: Where the control plane parses a policy mapping, and why each may.
PARSES = {
    # Opens a stored key before it parses: every reader's way in.
    "sandbox_archive_credentials.py:stored_archive_policy",
    # The administrator's request, which holds the key as they typed it.
    "storage/repositories/host_pool.py:PostgresHostPoolRepository.set_sandbox_archive_settings",
}


class _Parses(ast.NodeVisitor):
    """Each scope that names ``from_mapping``, dotted as ``Class.function``."""

    def __init__(self) -> None:
        self.scope: list[str] = []
        self.found: set[str] = set()

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _visit_scope

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "from_mapping":
            self.found.add(".".join(self.scope))
        self.generic_visit(node)


def _parses(source: str) -> set[str]:
    visitor = _Parses()
    visitor.visit(ast.parse(source))
    return visitor.found


def test_no_other_control_plane_code_parses_a_policy_mapping() -> None:
    """A stored policy reaches ``from_mapping`` only through ``stored_archive_policy``.

    A new parse is either one of these, or added with the reason its mapping
    holds no sealed key; one that is gone leaves the list.
    """
    found = {
        f"{path.relative_to(CONTROL_PLANE).as_posix()}:{scope}"
        for path in sorted(CONTROL_PLANE.rglob("*.py"))
        for scope in _parses(path.read_text(encoding="utf-8"))
    }
    assert found == PARSES


def test_the_scan_sees_a_parse_in_any_scope_or_form() -> None:
    """Negative controls: a method, a function inside it, and a parse kept for later."""
    source = (
        "class Store:\n"
        "    async def read(self, row):\n"
        "        def parse():\n"
        "            return SandboxArchivePolicy.from_mapping(row)\n"
        "        later = SandboxArchivePolicy.from_mapping\n"
    )
    assert _parses(source) == {"Store.read", "Store.read.parse"}
