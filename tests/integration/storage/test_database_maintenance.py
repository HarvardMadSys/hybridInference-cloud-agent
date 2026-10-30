"""A database move under maintenance, on real databases, including crash and failure.

The control plane's routes run in-process over a migrated source database; the
real agent code drives the PostgreSQL client programs from the ``postgres:15``
image against that source and an empty target, both disposable. Only Docker
Compose is recorded. The control plane then continues on a store over the
target, as it does when the service restarts there.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
from deploy.agent.client import deliver_results, recover_interrupted
from deploy.agent.database import local_command
from deploy.agent.store import parse_env, write_json
from tests.integration.storage.test_deployment_agents import _app, _enroll, _machine, _sync
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations

from cloud_agent import maintenance
from cloud_agent.application import key_rotation
from cloud_agent.instances import CONTROL_PLANE_CAPABILITIES
from cloud_agent.storage.assembly import Storage
from cloud_agent_shared.deployment_contract import key_identifier, seal, sealed_purpose

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


def _tools() -> dict[str, list[str]]:
    run = ["docker", "run", "--rm", "-i", "--network", "host"]
    for name in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        run += ["-e", name]
    return {name: [*run, "postgres:15", name] for name in ("psql", "pg_dump", "pg_restore")}


def _dsn(database: str) -> str:
    c = _db_config()
    return f"postgresql://{c['user']}:{c['password']}@127.0.0.1:{c['port']}/{database}"


def _config(database: str) -> dict[str, Any]:
    return {**_db_config(), "host": "127.0.0.1", "database": database}


@pytest.fixture()
async def databases():
    suffix = uuid.uuid4().hex[:8]
    base = str(_db_config()["database"])
    names = (f"{base}_mv_src_{suffix}", f"{base}_mv_dst_{suffix}")
    admin = await asyncpg.connect(**_config("postgres"))
    for name in names:
        await admin.execute(f'CREATE DATABASE "{name}"')
    apply_migrations(_config(names[0]))
    pools: list[asyncpg.Pool] = []

    async def open_store(name: str) -> Storage:
        pool = await asyncpg.create_pool(**_config(name), min_size=1, max_size=4)
        pools.append(pool)
        return Storage(pool)

    try:
        yield names[0], names[1], open_store
    finally:
        for pool in pools:
            await pool.close()
        for name in names:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
        maintenance.apply(None)


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    key_rotation.register_advancers()
    monkeypatch.setattr("cloud_agent.api.admin_deployments.log_admin_action", _no_audit)


async def _no_audit(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _report(store: Storage, **details: Any) -> None:
    await store.instances.report(
        instance_id=uuid.uuid4().hex,
        role="api",
        machine="cp-1",
        build_sha="",
        capabilities=list(CONTROL_PLANE_CAPABILITIES),
        adopted_settings_revision=10**6,
        restart_required=[],
        details=details,
        started_at=datetime.now(UTC),
    )


async def _clear(name: str) -> None:
    conn = await asyncpg.connect(**_config(name))
    try:
        await conn.execute("DELETE FROM agent_control_plane_instances")
    finally:
        await conn.close()


async def _prepare(tmp_path, client, source: str, target: str):
    agent_store, executor, machine = _machine(tmp_path, "control-plane")
    write_json(
        agent_store.root / "policy.json",
        {**agent_store.policy(), "database_tools": _tools()},
        0o644,
    )
    (agent_store.deploy_root / ".env").write_text(f"AGENT_DATABASE_URL={_dsn(source)}\n")
    credential = await _enroll(client, agent_store, "control-plane")
    await _sync(client, executor, credential)
    return agent_store, executor, machine, credential


def _sealed_url(agent_store, value: str) -> dict[str, Any]:
    identity = agent_store.identity()
    name = "AGENT_DATABASE_URL"
    return {
        identity["agent_id"]: {
            name: seal(identity["public_key"], value, sealed_purpose(identity["agent_id"], name))
        }
    }


async def _start(client, agent_store, target: str) -> str:
    started = await client.post(
        "/admin/agent/deployments/workflows",
        json={
            "kind": "database_migration",
            "params": {"dsn_id": key_identifier(_dsn(target))},
            "sealed_by_agent": _sealed_url(agent_store, _dsn(target)),
            "confirm": "database_migration",
        },
    )
    assert started.status_code == 200, started.text
    return started.json()["workflow_id"]


async def _enter_maintenance(client, store, source: str, workflow_id: str) -> None:
    """Every process has seen maintenance; the migrate intent is released."""
    state = (await client.get(f"/admin/agent/deployments/workflows/{workflow_id}")).json()
    assert state["step"] == "maintenance" and state["state"] == "waiting", state
    row = await store.maintenance.current()
    assert row["state"] == "migrating"
    maintenance.apply(row)
    await _clear(source)
    await _report(store, maintenance_seen=workflow_id)
    state = (await client.get(f"/admin/agent/deployments/workflows/{workflow_id}")).json()
    assert state["step"] == "migrate", state


async def test_a_move_refuses_writes_first_and_finishes_on_the_target(
    databases, tmp_path, monkeypatch
) -> None:
    source, target, open_store = databases
    store = await open_store(source)
    app = _app(store, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as cp:
        agent_store, executor, _, credential = await _prepare(tmp_path, cp, source, target)
        await _report(store)
        workflow_id = await _start(cp, agent_store, target)
        await _enter_maintenance(cp, store, source, workflow_id)

        # The agent runs the move; progress reaches only this process once fenced.
        headers = {"Authorization": f"Bearer {credential}"}
        intent = (await cp.get("/v1/agent/deploy/intents", headers=headers)).json()["intents"][0]
        await cp.post(
            f"/v1/agent/deploy/intents/{intent['intent_id']}/result",
            json={"state": "running", "detail": "started", "evidence": {}},
            headers=headers,
        )
        outcome = executor.execute(
            intent["operation"],
            intent["params"],
            intent["sealed"],
            intent_id=intent["intent_id"],
            seq=intent["seq"],
            origin="intent",
        )
        assert outcome.state == "succeeded", outcome.detail
        assert parse_env(agent_store.current_env())["AGENT_DATABASE_URL"] == _dsn(target)

        # The source stays read-only; a session on it cannot write, and the
        # agent's routes answer without a 500. The result waits on the agent.
        late = await cp.post(
            f"/v1/agent/deploy/intents/{intent['intent_id']}/result",
            json={"state": "succeeded", "detail": outcome.detail, "evidence": outcome.evidence},
            headers=headers,
        )
        assert late.status_code == 503
        fenced = await asyncpg.connect(**_config(source))
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            await fenced.execute("UPDATE agent_maintenance SET detail = 'late'")
        await fenced.close()
        reported = await cp.post(
            "/v1/agent/deploy/report",
            json={"observed": executor.observed(), "operations": [{"phase": "copy"}]},
            headers=headers,
        )
        assert reported.status_code == 200 and reported.json().get("persisted") is False
        assert (await cp.get("/v1/agent/deploy/intents", headers=headers)).json() == {"intents": []}
        overview = (await cp.get("/admin/agent/deployments")).json()
        assert overview["maintenance"]["active"] is True
        assert overview["maintenance"]["progress"]

    # The control plane restarts on the target: the copy says the move finished.
    moved = await open_store(target)
    row = await moved.maintenance.current()
    assert row["state"] == "completed"
    maintenance.apply(row)
    assert maintenance.current() == {"active": False}
    workflow = await moved.deployments.get_workflow(workflow_id)
    assert workflow is not None and workflow["step"] == "migrate"
    # The evidence the source could not store reaches the target.
    app = _app(moved, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as cp:
        record = agent_store.operation(
            next(
                r["id"]
                for r in agent_store.operations()
                if r.get("operation") == "database_migrate"
            )
        )
        answer = await cp.post(
            f"/v1/agent/deploy/intents/{record['intent_id']}/result",
            json={"state": "succeeded", "detail": record["detail"], "evidence": record["evidence"]},
            headers={"Authorization": f"Bearer {credential}"},
        )
        assert answer.status_code == 200 and answer.json()["accepted"] is True
        await _report(moved, database_url_id=key_identifier(_dsn(target)))
        await _clear(target)
        await _report(moved, database_url_id=key_identifier(_dsn(target)))
        final = (await cp.get(f"/admin/agent/deployments/workflows/{workflow_id}")).json()
        assert final["state"] == "succeeded", final
    journal = json.dumps(agent_store.operations())
    assert "PGPASSWORD" not in journal and _dsn(target) not in journal


async def test_a_failed_copy_reopens_the_source_and_leaves_no_partial_target(
    databases, tmp_path, monkeypatch
) -> None:
    source, target, open_store = databases
    store = await open_store(source)
    app = _app(store, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as cp:
        agent_store, executor, _, credential = await _prepare(tmp_path, cp, source, target)
        write_json(
            agent_store.root / "policy.json",
            {**agent_store.policy(), "database_tools": {**_tools(), "pg_restore": ["false"]}},
            0o644,
        )
        await _report(store)
        workflow_id = await _start(cp, agent_store, target)
        await _enter_maintenance(cp, store, source, workflow_id)
        assert await _sync(cp, executor, credential) == ["failed"]
        failed = (await cp.get(f"/admin/agent/deployments/workflows/{workflow_id}")).json()
        assert failed["state"] == "failed", failed
        assert "writable again" in failed["detail"]
        row = await store.maintenance.current()
        assert row["state"] == "aborted"
    writer = await asyncpg.connect(**_config(source))
    await writer.execute("UPDATE agent_maintenance SET detail = 'writes resumed'")
    await writer.close()
    empty = await asyncpg.connect(**_config(target))
    assert await empty.fetchval("SELECT count(*) FROM pg_tables WHERE schemaname='public'") == 0
    await empty.close()
    assert parse_env(agent_store.current_env())["AGENT_DATABASE_URL"] == _dsn(source)
    assert not list((agent_store.root / "migrations").glob("*.dump"))


class _Crash(BaseException):
    """The agent's process dying: nothing in the handler runs."""


async def test_an_interrupted_move_resumes_after_a_restart_without_the_target_being_entered_again(
    databases, tmp_path, monkeypatch
) -> None:
    source, target, open_store = databases
    store = await open_store(source)
    app = _app(store, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as cp:
        agent_store, executor, machine, credential = await _prepare(tmp_path, cp, source, target)
        await _report(store)
        workflow_id = await _start(cp, agent_store, target)
        await _enter_maintenance(cp, store, source, workflow_id)

        real_stream = machine.stream

        def dying_stream(argv, **kwargs):
            if "pg_restore" in argv:
                real_stream(argv, **{**kwargs, "stdin_path": kwargs["stdin_path"]})
                raise _Crash
            return real_stream(argv, **kwargs)

        machine.stream = dying_stream
        with pytest.raises(_Crash):
            await _sync(cp, executor, credential)
        machine.stream = real_stream
        interrupted = next(
            r for r in agent_store.operations() if r.get("operation") == "database_migrate"
        )
        assert interrupted["state"] == "running"
        assert "fence" in interrupted["migration"]["done"]
        assert interrupted["migration"]["copying"] is True  # the target holds this copy

        # A status read on the machine shows the step and no sealed value.
        args = type("Args", (), {"operation_id": None, "database_command": "status"})()
        status = json.dumps(local_command(executor, args).evidence)
        assert "copying" in status and "sealed_target" not in status

        # The agent restarts: the journal alone is enough.
        settled = recover_interrupted(executor)
        assert settled == [interrupted["id"]]
        finished = agent_store.operation(interrupted["id"])
        assert finished["state"] == "succeeded", finished.get("detail")
        assert finished["migration"]["done"][-1] == "complete"
        assert parse_env(agent_store.current_env())["AGENT_DATABASE_URL"] == _dsn(target)
        assert finished.get("intent_id") == interrupted["intent_id"]
        assert not finished.get("delivered")


async def test_unfence_on_the_machine_works_without_the_control_plane(
    databases, tmp_path, monkeypatch
) -> None:
    source, target, open_store = databases
    store = await open_store(source)
    app = _app(store, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as cp:
        agent_store, executor, machine, credential = await _prepare(tmp_path, cp, source, target)
        await _report(store)
        workflow_id = await _start(cp, agent_store, target)
        await _enter_maintenance(cp, store, source, workflow_id)
        real_stream = machine.stream

        def dying_stream(argv, **kwargs):
            raise _Crash

        machine.stream = dying_stream
        with pytest.raises(_Crash):
            await _sync(cp, executor, credential)
        machine.stream = real_stream
    record = next(r for r in agent_store.operations() if r.get("operation") == "database_migrate")
    fenced = await asyncpg.connect(**_config(source))
    with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
        await fenced.execute("UPDATE agent_maintenance SET detail = 'late'")
    await fenced.close()
    args = type("Args", (), {"operation_id": record["id"], "database_command": "unfence"})()
    outcome = local_command(executor, args)
    assert outcome.state == "succeeded", outcome.detail
    writer = await asyncpg.connect(**_config(source))
    await writer.execute("UPDATE agent_maintenance SET detail = 'writes resumed'")
    assert await writer.fetchval("SELECT state FROM agent_maintenance") == "aborted"
    await writer.close()
    # It is settled: a restart does not fence the source again.
    assert recover_interrupted(executor) == []


async def test_a_result_the_control_plane_missed_is_sent_again(tmp_path) -> None:
    agent_store, _, _ = _machine(tmp_path, "control-plane")
    agent_store.save_operation(
        {"id": "a" * 32, "intent_id": "a" * 32, "state": "succeeded", "detail": "d", "evidence": {}}
    )
    sent: list[tuple[str, str]] = []

    class Client:
        def result(self, intent_id, state, detail, evidence):
            sent.append((intent_id, state))

    deliver_results(Client(), agent_store)  # type: ignore[arg-type]
    deliver_results(Client(), agent_store)  # type: ignore[arg-type]
    assert sent == [("a" * 32, "succeeded")]
