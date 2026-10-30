"""Key rotation and database workflows on real databases (WP9-WP12).

The control plane's routes run in-process over the per-worker test database.
Agents are the real agent code on temporary roots; Docker Compose is recorded,
while the PostgreSQL client programs really run (``psql`` on this machine,
``pg_dump``/``pg_restore`` from the local ``postgres:15`` image), against
disposable databases created for each test and dropped after it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from deploy.agent.database import pg_env, scram_verifier
from deploy.agent.store import parse_env, write_json
from tests.integration.storage.test_deployment_agents import _app, _enroll, _machine, _sync
from tests.integration.storage.test_schema_upgrade import _db_config, store as store

from cloud_agent import settings
from cloud_agent.application import key_rotation
from cloud_agent.instances import CONTROL_PLANE_CAPABILITIES, root_key_state
from cloud_agent.storage.repositories.instances import InstanceRepository
from cloud_agent_shared.deployment_contract import key_identifier, seal, sealed_purpose

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


def _dsn(database: str | None = None, password: str | None = None) -> str:
    config = _db_config()
    return (
        f"postgresql://{config['user']}:{password or config['password']}@{config['host']}:"
        f"{config['port']}/{database or config['database']}"
    )


async def _report_instance(store, **details: Any) -> None:
    await store.instances.report(
        instance_id=uuid.uuid4().hex,
        role="api",
        machine="cp-1",
        build_sha="",
        capabilities=CONTROL_PLANE_CAPABILITIES,
        adopted_settings_revision=10**6,
        restart_required=[],
        details=details,
        started_at=datetime.now(UTC),
    )


async def _clear_instances(store) -> None:
    conn = await asyncpg.connect(**_db_config())
    try:
        await conn.execute("DELETE FROM agent_control_plane_instances")
    finally:
        await conn.close()


def _sealed(agent_store, name: str, value: str) -> dict[str, Any]:
    identity = agent_store.identity()
    return {
        identity["agent_id"]: {
            name: seal(identity["public_key"], value, sealed_purpose(identity["agent_id"], name))
        }
    }


async def test_admin_token_rotation_finishes_only_when_every_process_uses_it(
    store, tmp_path, monkeypatch
) -> None:
    app = _app(store, monkeypatch)
    monkeypatch.setattr(
        "cloud_agent.api.admin_deployments.log_admin_action", _no_audit := (lambda *a, **k: _none())
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        agent_store, executor, _ = _machine(tmp_path, "control-plane")
        credential = await _enroll(client, agent_store, "control-plane")
        await _sync(client, executor, credential)
        new = "a-rotated-admin-token-of-thirty-two-bytes"
        started = await client.post(
            "/admin/agent/deployments/workflows",
            json={
                "kind": "admin_token",
                "params": {"token_id": key_identifier(new)},
                "sealed_by_agent": _sealed(agent_store, "ADMIN_TOKEN", new),
                "confirm": "admin_token",
            },
        )
        assert started.status_code == 200, started.text
        workflow_id = started.json()["workflow_id"]
        assert await _sync(client, executor, credential) == ["succeeded"]
        assert parse_env(agent_store.current_env())["ADMIN_TOKEN"] == new
        # A process still on the old token keeps the workflow waiting.
        await _clear_instances(store)
        await _report_instance(store, admin_token_id=key_identifier("old-token"))
        state = (await client.get(f"/admin/agent/deployments/workflows/{workflow_id}")).json()
        assert state["state"] == "waiting" and "api@cp-1" in state["detail"]
        await _clear_instances(store)
        await _report_instance(store, admin_token_id=key_identifier(new))
        state = (await client.get(f"/admin/agent/deployments/workflows/{workflow_id}")).json()
        assert state["state"] == "succeeded", state
        assert new not in json.dumps(state)


async def _none() -> None:
    return None


async def test_root_key_rotation_reencrypts_every_family_before_retiring(
    store, tmp_path, monkeypatch
) -> None:
    old_root = "the-old-session-fallback-root-key-32b!"
    new_root = "a-brand-new-dedicated-root-key-of-32b!!"
    monkeypatch.delenv("AGENT_SETTINGS_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS", raising=False)
    monkeypatch.setenv("AGENT_SESSION_SECRET", old_root)
    settings.reset_settings()
    await settings.initialize_settings(store.settings)
    revision, _ = await store.settings.read_settings()
    await store.settings.update_settings(
        settings.encode_changes({"github_app_client_secret": "keep-this-secret"}),
        expected_revision=revision,
    )
    app = _app(store, monkeypatch)
    monkeypatch.setattr(
        "cloud_agent.api.admin_deployments.log_admin_action", lambda *a, **k: _none()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        agent_store, executor, _ = _machine(tmp_path, "control-plane")
        (agent_store.deploy_root / ".env").write_text(f"AGENT_SESSION_SECRET={old_root}\n")
        agent_store.activate_version(agent_store.stage_version(agent_store.current_env()))
        credential = await _enroll(client, agent_store, "control-plane")
        await _sync(client, executor, credential)
        await _clear_instances(store)
        await _report_instance(
            store, root_key_source="fallback", root_key_id=key_identifier(old_root)
        )
        # Unidentified sessions block a mixed-version rotation; this test's own pool is unnamed.
        monkeypatch.setattr(InstanceRepository, "unidentified_sessions", lambda self: _empty())
        started = await client.post(
            "/admin/agent/deployments/workflows",
            json={
                "kind": "root_key",
                "params": {"new_key_id": key_identifier(new_root)},
                "sealed_by_agent": _sealed(agent_store, "AGENT_SETTINGS_ENCRYPTION_KEY", new_root),
                "confirm": "root_key",
            },
        )
        assert started.status_code == 200, started.text
        workflow_id = started.json()["workflow_id"]
        assert await _sync(client, executor, credential) == ["succeeded"]  # separate
        assert parse_env(agent_store.current_env())["AGENT_SETTINGS_ENCRYPTION_KEY"] == old_root
        await _clear_instances(store)
        await _report_instance(
            store, root_key_source="dedicated", root_key_id=key_identifier(old_root)
        )
        await client.get(f"/admin/agent/deployments/workflows/{workflow_id}")
        assert await _sync(client, executor, credential) == ["succeeded"]  # stage
        env = parse_env(agent_store.current_env())
        assert env["AGENT_SETTINGS_ENCRYPTION_KEY"] == new_root
        assert env["AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS"] == old_root
        # This process now runs with the staged keys, as a restarted one would.
        monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", new_root)
        monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS", old_root)
        assert root_key_state()["root_key_id"] == key_identifier(new_root)
        await _clear_instances(store)
        await _report_instance(
            store,
            root_key_source="dedicated",
            root_key_id=key_identifier(new_root),
            root_key_previous_id=key_identifier(old_root),
        )
        await client.get(f"/admin/agent/deployments/workflows/{workflow_id}")  # re-encrypts
        assert await _sync(client, executor, credential) == ["succeeded"]  # retire
        assert "AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS" not in parse_env(agent_store.current_env())
        monkeypatch.delenv("AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS")
        await _clear_instances(store)
        await _report_instance(
            store, root_key_source="dedicated", root_key_id=key_identifier(new_root)
        )
        final = (await client.get(f"/admin/agent/deployments/workflows/{workflow_id}")).json()
        assert final["state"] == "succeeded", final
        assert final["progress"]["reencrypted"]["settings"] >= 1
    # With only the new key, every saved secret still opens.
    _, encoded = await store.settings.read_settings()
    assert settings._decode(encoded)["github_app_client_secret"] == "keep-this-secret"
    settings.reset_settings()


async def _empty() -> list[Any]:
    return []


def _postgres_tools() -> dict[str, list[str]] | None:
    if not shutil.which("docker") or not shutil.which("psql"):
        return None
    probe = subprocess.run(
        ["docker", "image", "inspect", "postgres:15"], capture_output=True, check=False
    )
    if probe.returncode != 0:
        return None
    run = ["docker", "run", "--rm", "-i", "--network", "host"]
    for name in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        run += ["-e", name]
    return {
        "psql": ["psql"],
        "pg_dump": [*run, "postgres:15", "pg_dump"],
        "pg_restore": [*run, "postgres:15", "pg_restore"],
    }


async def _admin_conn() -> asyncpg.Connection:
    config = _db_config()
    return await asyncpg.connect(**{**config, "database": "postgres"})


async def test_a_same_database_probe_passes_and_a_clone_fails(store, tmp_path, monkeypatch) -> None:
    tools = _postgres_tools()
    if tools is None:
        pytest.skip("psql and the postgres:15 image are needed")
    clone = f"{_db_config()['database']}_clone_{uuid.uuid4().hex[:6]}"
    admin = await _admin_conn()
    app = _app(store, monkeypatch)
    monkeypatch.setattr(
        "cloud_agent.api.admin_deployments.log_admin_action", lambda *a, **k: _none()
    )
    try:
        await admin.execute(f'CREATE DATABASE "{clone}"')
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cp"
        ) as client:
            agent_store, executor, _ = _machine(tmp_path, "control-plane")
            policy = agent_store.policy()
            write_json(agent_store.root / "policy.json", {**policy, "database_tools": tools}, 0o644)
            (agent_store.deploy_root / ".env").write_text(f"AGENT_DATABASE_URL={_dsn()}\n")
            credential = await _enroll(client, agent_store, "control-plane")
            await _sync(client, executor, credential)
            # A different database (an empty stand-in for a clone) cannot see the probe.
            failing = await client.post(
                "/admin/agent/deployments/workflows",
                json={
                    "kind": "database_connection",
                    "params": {"dsn_id": key_identifier(_dsn(clone))},
                    "sealed_by_agent": _sealed(agent_store, "AGENT_DATABASE_URL", _dsn(clone)),
                    "confirm": "database_connection",
                },
            )
            assert failing.status_code == 200, failing.text
            assert await _sync(client, executor, credential) == ["failed"]
            assert parse_env(agent_store.current_env())["AGENT_DATABASE_URL"] == _dsn()
            failed = (
                await client.get(
                    f"/admin/agent/deployments/workflows/{failing.json()['workflow_id']}"
                )
            ).json()
            assert failed["state"] == "failed"
            # The same database through another spelling of the address passes.
            same = _dsn().replace("@localhost:", "@127.0.0.1:")
            ok = await client.post(
                "/admin/agent/deployments/workflows",
                json={
                    "kind": "database_connection",
                    "params": {"dsn_id": key_identifier(same)},
                    "sealed_by_agent": _sealed(agent_store, "AGENT_DATABASE_URL", same),
                    "confirm": "database_connection",
                },
            )
            assert ok.status_code == 200, ok.text
            probe_states = await _sync(client, executor, credential)
            assert probe_states == ["succeeded"], agent_store.operations()[-1].get("detail")
            assert await _sync(client, executor, credential) == ["succeeded"]  # switch
            assert parse_env(agent_store.current_env())["AGENT_DATABASE_URL"] == same
            await _clear_instances(store)
            await _report_instance(store, database_url_id=key_identifier(same))
            done = (
                await client.get(f"/admin/agent/deployments/workflows/{ok.json()['workflow_id']}")
            ).json()
            assert done["state"] == "succeeded", done
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{clone}" WITH (FORCE)')
        await admin.close()


async def test_migration_fences_the_source_copies_verifies_and_never_overwrites(tmp_path) -> None:
    tools = _postgres_tools()
    if tools is None:
        pytest.skip("psql and the postgres:15 image are needed")
    suffix = uuid.uuid4().hex[:6]
    base = _db_config()["database"]
    source, target, busy = f"{base}_src_{suffix}", f"{base}_dst_{suffix}", f"{base}_busy_{suffix}"
    admin = await _admin_conn()
    try:
        for name in (source, target, busy):
            await admin.execute(f'CREATE DATABASE "{name}"')
        src = await asyncpg.connect(**{**_db_config(), "database": source})
        await src.execute(
            "CREATE TABLE alembic_version (version_num text); INSERT INTO alembic_version VALUES ('0111_database_probes');"
            "CREATE TABLE items (id serial primary key, v text); INSERT INTO items (v) SELECT 'x' FROM generate_series(1, 25);"
        )
        await src.close()
        busy_conn = await asyncpg.connect(**{**_db_config(), "database": busy})
        await busy_conn.execute("CREATE TABLE keep (id int)")
        await busy_conn.close()

        agent_store, executor, _ = _machine(tmp_path, "control-plane")
        write_json(
            agent_store.root / "policy.json",
            {**agent_store.policy(), "database_tools": tools},
            0o644,
        )
        (agent_store.deploy_root / ".env").write_text(f"AGENT_DATABASE_URL={_dsn(source)}\n")
        identity = agent_store.identity()

        def sealed(dsn: str) -> dict[str, Any]:
            return {
                "AGENT_DATABASE_URL": seal(
                    identity["public_key"],
                    dsn,
                    sealed_purpose(executor.agent_id, "AGENT_DATABASE_URL"),
                )
            }

        refused = executor.execute(
            "database_migrate", {"operation_id": "1" * 32}, sealed(_dsn(busy))
        )
        assert refused.state == "failed" and "not empty" in refused.detail
        outcome = executor.execute(
            "database_migrate", {"operation_id": "2" * 32}, sealed(_dsn(target))
        )
        assert outcome.state == "succeeded", outcome.detail
        assert parse_env(agent_store.current_env())["AGENT_DATABASE_URL"] == _dsn(target)
        dst = await asyncpg.connect(**{**_db_config(), "database": target})
        assert await dst.fetchval("SELECT count(*) FROM items") == 25
        await dst.close()
        # The source is fenced: a new session cannot write.
        fenced = await asyncpg.connect(**{**_db_config(), "database": source})
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            await fenced.execute("INSERT INTO items (v) VALUES ('late')")
        await fenced.close()
        journal = next(
            r
            for r in agent_store.operations()
            if (r.get("params") or {}).get("operation_id") == "2" * 32
        )
        assert journal["migration"]["done"] == [
            "preflight",
            "fence",
            "copy",
            "verify",
            "switch",
            "complete",
        ]
        assert not list((agent_store.root / "migrations").glob("*.dump"))
        assert "PGPASSWORD" not in json.dumps(journal)
    finally:
        for name in (source, target, busy):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


def test_connection_details_never_reach_argv_and_passwords_reach_the_server_hashed() -> None:
    env = pg_env("postgresql://user:p%40ss@db.example:6543/agent?sslmode=require")
    assert env == {
        "PGHOST": "db.example",
        "PGPORT": "6543",
        "PGDATABASE": "agent",
        "PGUSER": "user",
        "PGPASSWORD": "p@ss",
        "PGSSLMODE": "require",
    }
    verifier = scram_verifier("a-long-new-password")
    assert verifier.startswith("SCRAM-SHA-256$4096:")
    assert "a-long-new-password" not in verifier
    assert os.environ.get("PGPASSWORD") != "p@ss"
    assert key_rotation.ROTATION_CAPABILITY in CONTROL_PLANE_CAPABILITIES
    assert Path
