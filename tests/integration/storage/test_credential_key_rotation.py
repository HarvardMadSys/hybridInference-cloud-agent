"""Credential encryption key rotation on a real database: every family, every lookup digest.

Each family table gets a row whose ciphertext columns hold a value encrypted
under the old key (foreign keys are not what this exercises, so rows are
written with replication-role triggers off in the disposable test database).
The workflow then runs against real settings rows and instance reports, and
every value must open with the new key alone, unchanged, before the old key is
dropped.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
from tests.integration.storage.test_schema_upgrade import _db_config, store as store

from cloud_agent import settings
from cloud_agent.application import credential_key_rotation, workflows
from cloud_agent.credential_cipher import CredentialCipher
from cloud_agent.credential_families import FAMILIES, OTHER_KEYS, families
from cloud_agent.environment_fingerprints import setup_material
from cloud_agent.instances import CONTROL_PLANE_CAPABILITIES
from cloud_agent.storage.repositories.instances import InstanceRepository
from cloud_agent_shared.environment import ENVIRONMENT_DOMAIN, EnvironmentVariable

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

OLD_USER = "the-old-user-credential-key-of-32-bytes!"
NEW_USER = "a-new-user-credential-key-of-at-least-32b"
OLD_COMPUTE = "the-old-compute-credential-key-32-bytes!"
NEW_COMPUTE = "a-new-compute-credential-key-of-32-bytes!"

#: Values a generic placeholder would break a CHECK constraint with.
_OVERRIDES: dict[str, dict[str, Any]] = {
    "agent_environment_version_variables": {"phase": "setup", "secret": True},
    "agent_ssh_servers": {
        "name": "build-box",
        "status": "pending",
        "pending_public_key": "ssh-ed25519 AAAA",
    },
    "agent_model_connections": {
        "id": "mconn_" + "0" * 32,
        "base_url": "https://models.example.test/v1",
        "models": ["model-a"],
    },
    "agent_oauth_states": {"provider": "gitlab"},
    "agent_compute_deployment_profiles": {"source_sha256": "0" * 64},
}


def _placeholder(data_type: str, n: int) -> Any:
    if data_type in {"text", "character varying"}:
        return f"value-{n}"
    if data_type in {"bigint", "integer", "smallint"}:
        return n
    if data_type == "boolean":
        return False
    if data_type == "jsonb":
        return "{}"
    if data_type == "bytea":
        return b"\x00"
    if data_type.startswith("timestamp"):
        return datetime(2099, 1, 1, tzinfo=UTC)
    if data_type == "ARRAY":
        return []
    if data_type == "uuid":
        return uuid.uuid4()
    raise AssertionError(f"no placeholder for {data_type}")


async def _insert_family_rows(conn: Any, encrypted: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One row per family table, each family column holding ``encrypted[table][column]``."""
    keys: dict[str, Any] = {}
    await conn.execute("SET session_replication_role = replica")
    for n, (table, columns) in enumerate(encrypted.items(), start=1):
        required = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1 AND is_nullable = 'NO' "
            "AND column_default IS NULL",
            table,
        )
        values: dict[str, Any] = {
            r["column_name"]: _placeholder(r["data_type"], n * 10 + i)
            for i, r in enumerate(required)
        }
        values.update(_OVERRIDES.get(table, {}))
        values.update(columns)
        names = list(values)
        types = {
            r["column_name"]: r["data_type"]
            for r in await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = $1",
                table,
            )
        }
        placeholders = [
            f"${i}::jsonb" if types[name] == "jsonb" else f"${i}"
            for i, name in enumerate(names, start=1)
        ]
        await conn.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(placeholders)})",
            *[
                json.dumps(values[name])
                if types[name] == "jsonb" and not isinstance(values[name], str)
                else values[name]
                for name in names
            ],
        )
        keys[table] = values
    await conn.execute("SET session_replication_role = DEFAULT")
    return keys


async def test_every_ciphertext_column_of_the_schema_belongs_to_a_family(store) -> None:
    """A new ciphertext column fails here until the manifest says which key it is under."""
    conn = await asyncpg.connect(**_db_config())
    try:
        columns = {
            f"{r['table_name']}.{r['column_name']}"
            for r in await conn.fetch(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND (column_name LIKE '%ciphertext%' "
                "OR column_name LIKE '%sealed%')"
            )
        }
    finally:
        await conn.close()
    listed = {f"{f.table}.{f.column}" for f in FAMILIES if f.shape == "text"} | set(OTHER_KEYS)
    assert columns - listed == set()
    assert {f"{f.table}.{f.column}" for f in FAMILIES if f.shape == "text"} <= columns


async def _report(store, revision: int, capabilities=CONTROL_PLANE_CAPABILITIES) -> None:
    await store.instances.report(
        instance_id=uuid.uuid4().hex,
        role="api",
        machine="cp-1",
        build_sha="",
        capabilities=list(capabilities),
        adopted_settings_revision=revision,
        restart_required=[],
        details={},
        started_at=datetime.now(UTC),
    )


async def _clear_instances() -> None:
    conn = await asyncpg.connect(**_db_config())
    try:
        await conn.execute("DELETE FROM agent_control_plane_instances")
        await conn.execute("DELETE FROM agent_deploy_workflows")
    finally:
        await conn.close()


async def _adopt(store) -> int:
    """This process and one reported process adopt the latest settings."""
    revision, encoded = await store.settings.read_settings()
    settings.apply_settings(revision, encoded)
    await _clear_instances_only()
    await _report(store, revision)
    return revision


async def _clear_instances_only() -> None:
    conn = await asyncpg.connect(**_db_config())
    try:
        await conn.execute("DELETE FROM agent_control_plane_instances")
    finally:
        await conn.close()


async def _run_to_end(store, workflow_id: str, rounds: int = 40) -> dict[str, Any]:
    for _ in range(rounds):
        await _adopt(store)
        workflow = await store.deployments.get_workflow(workflow_id)
        if workflow["state"] not in {"running", "waiting"}:
            return workflow
        await workflows.advance_workflow(store, workflow)
    return await store.deployments.get_workflow(workflow_id)


async def _seed_settings(store) -> None:
    settings.reset_settings()
    await settings.initialize_settings(store.settings)
    revision, _ = await store.settings.read_settings()
    await store.settings.update_settings(
        settings.encode_changes(
            {"source_control_encryption_key": OLD_USER, "compute_encryption_key": OLD_COMPUTE}
        ),
        expected_revision=revision,
    )


@pytest.fixture(autouse=True)
def _workflows_registered(monkeypatch):
    from cloud_agent.application.key_rotation import register_advancers

    register_advancers()

    async def _no_sessions(self):
        return []

    # This test's own pool is the only other session; it is not a control-plane process.
    monkeypatch.setattr(InstanceRepository, "unidentified_sessions", _no_sessions)
    monkeypatch.setenv("AGENT_SESSION_SECRET", "a-session-secret-of-at-least-32-bytes!!")
    yield
    settings.reset_settings()


async def test_a_user_key_rotation_reencrypts_every_family_and_moves_every_fingerprint(
    store,
) -> None:
    await _clear_instances()
    await _seed_settings(store)
    old_ciphers = {f.domain: CredentialCipher(OLD_USER, domain=f.domain) for f in FAMILIES}
    plaintexts: dict[tuple[str, str], str] = {}
    encrypted: dict[str, dict[str, Any]] = {}
    for family in families("user"):
        value = f"secret of {family.name}"
        plaintexts[(family.table, family.column)] = value
        ciphertext = old_ciphers[family.domain].encrypt(value)
        encrypted.setdefault(family.table, {})[family.column] = (
            ciphertext
            if family.shape == "text"
            else [{"name": "TOKEN", "value_ciphertext": ciphertext, "secret": True}]
        )
    # An environment whose binding and two snapshots carry the old key's fingerprint.
    env_cipher = CredentialCipher(OLD_USER, domain=ENVIRONMENT_DOMAIN)
    setup_value = "build-time-token"
    variable_ciphertext = env_cipher.encrypt(setup_value)
    encrypted["agent_environment_version_variables"].update(
        version_id="v-1", name="NPM_TOKEN", value_ciphertext=variable_ciphertext
    )
    plaintexts[("agent_environment_version_variables", "value_ciphertext")] = setup_value
    material = setup_material(
        "npm ci", [EnvironmentVariable(name="NPM_TOKEN", value=setup_value, phase="setup")]
    )
    old_fp = env_cipher.mac(material)
    new_fp = CredentialCipher(NEW_USER, domain=ENVIRONMENT_DOMAIN).mac(material)
    encrypted["agent_environment_bindings"].update(
        id="b-1",
        user_id="owner-1",
        environment_version_id="v-1",
        global_version_id=None,
        has_setup_override=False,
        setup_fingerprint=old_fp,
    )
    encrypted["agent_environments"] = {
        "id": "e-1",
        "user_id": "owner-1",
        "repo": "group/repo",
        "revision": 1,
    }
    encrypted["agent_environment_versions"] = {
        "id": "v-1",
        "environment_id": "e-1",
        "revision": 1,
        "document": json.dumps({"setup_script": "npm ci"}),
    }
    conn = await asyncpg.connect(**_db_config())
    try:
        await _insert_family_rows(conn, encrypted)
        await conn.execute("SET session_replication_role = replica")
        for snapshot_id, state in (("a" * 32, "ready"), ("b" * 32, "building")):
            await conn.execute(
                "INSERT INTO agent_environment_snapshots "
                "(id, user_id, environment_id, revision, setup_fingerprint, state, object_key, "
                "sha256, size_bytes, base_image, source_sha, ready_at) "
                "VALUES ($1, 'owner-1', 'e-1', 1, $2, $3, 'k', $4, 1, 'img', 'sha', NOW())",
                snapshot_id,
                old_fp,
                state,
                "0" * 64,
            )
        await conn.execute("SET session_replication_role = DEFAULT")
    finally:
        await conn.close()
    await _adopt(store)

    workflow = await credential_key_rotation.start_credential_key_rotation(
        store, key="user", secret=NEW_USER, admin_id="admin-token"
    )
    # During the window, an old-only process is named and nothing is retired.
    final = await _run_to_end(store, workflow["workflow_id"])
    assert final["state"] == "succeeded", final
    dumped = json.dumps(final, default=str)
    assert NEW_USER not in dumped and OLD_USER not in dumped

    _revision, encoded = await store.settings.read_settings()
    values = settings._decode(encoded)
    assert values["source_control_encryption_key"] == NEW_USER
    assert values["source_control_encryption_key_previous"] == ""
    assert values["credential_index_uses_previous"] is False

    conn = await asyncpg.connect(**_db_config())
    try:
        for family in families("user"):
            new_only = CredentialCipher(NEW_USER, domain=family.domain)
            stored = await conn.fetchval(f"SELECT {family.column} FROM {family.table} LIMIT 1")
            values_ = (
                [stored]
                if family.shape == "text"
                else [item["value_ciphertext"] for item in json.loads(stored)]
            )
            for value in values_:
                assert new_only.decrypt(value) == plaintexts[(family.table, family.column)], (
                    family.name
                )
        assert (
            await conn.fetchval(
                "SELECT setup_fingerprint FROM agent_environment_bindings WHERE id = 'b-1'"
            )
            == new_fp
        )
        assert {
            r["id"]: r["setup_fingerprint"]
            for r in await conn.fetch(
                "SELECT id, setup_fingerprint FROM agent_environment_snapshots"
            )
        } == {"a" * 32: new_fp, "b" * 32: new_fp}
    finally:
        await conn.close()


async def test_a_compute_key_rotation_keeps_the_identity_binding_of_each_credential(store) -> None:
    await _clear_instances()
    await _seed_settings(store)
    from cloud_agent.compute_secrets import ComputeSecretStore

    old_store = ComputeSecretStore(None, key=OLD_COMPUTE)  # type: ignore[arg-type]
    encrypted = {
        "agent_compute_connections": {
            "id": "conn-1",
            "provider_id": "cloudlab",
            "secret_ciphertext": old_store.encrypt("conn-1", "cloudlab", "private key"),
        },
        "agent_compute_deployment_profiles": {
            "id": "profile-1",
            "secret_ciphertext": old_store.encrypt("profile-1", "profile", "inputs"),
        },
    }
    conn = await asyncpg.connect(**_db_config())
    try:
        await _insert_family_rows(conn, encrypted)
    finally:
        await conn.close()
    await _adopt(store)
    workflow = await credential_key_rotation.start_credential_key_rotation(
        store, key="compute", secret=NEW_COMPUTE, admin_id="admin-token"
    )
    final = await _run_to_end(store, workflow["workflow_id"])
    assert final["state"] == "succeeded", final
    assert final["step"] == "retire"  # no lookup digests to rebuild for this key
    new_store = ComputeSecretStore(None, key=NEW_COMPUTE)  # type: ignore[arg-type]
    conn = await asyncpg.connect(**_db_config())
    try:
        stored = await conn.fetchval(
            "SELECT secret_ciphertext FROM agent_compute_connections WHERE id = 'conn-1'"
        )
    finally:
        await conn.close()
    assert new_store.decrypt("conn-1", "cloudlab", stored) == "private key"


async def test_the_rotation_waits_for_a_process_without_the_capability(store) -> None:
    await _clear_instances()
    await _seed_settings(store)
    revision = await _adopt(store)
    workflow = await credential_key_rotation.start_credential_key_rotation(
        store, key="compute", secret=NEW_COMPUTE, admin_id="admin-token"
    )
    workflow = await store.deployments.get_workflow(workflow["workflow_id"])
    revision, encoded = await store.settings.read_settings()
    settings.apply_settings(revision, encoded)
    await _clear_instances_only()
    await _report(store, revision, capabilities=["instance-report-v1"])
    await workflows.advance_workflow(store, workflow)
    workflow = await store.deployments.get_workflow(workflow["workflow_id"])
    assert workflow["state"] == "waiting"
    assert "api@cp-1" in workflow["detail"]
    # The old key still decrypts: it is kept, not dropped, while the fleet lags.
    assert settings._decode(encoded)["compute_encryption_key_previous"] == OLD_COMPUTE


async def test_an_unconfigured_or_unsupported_key_is_refused(store) -> None:
    await _clear_instances()
    settings.reset_settings()
    await settings.initialize_settings(store.settings)
    await _adopt(store)
    with pytest.raises(workflows.WorkflowRefused, match="not configured"):
        await credential_key_rotation.start_credential_key_rotation(
            store, key="user", secret=NEW_USER, admin_id="admin-token"
        )
    await _seed_settings(store)
    await _clear_instances_only()
    await _report(store, 10**6, capabilities=["instance-report-v1"])
    with pytest.raises(workflows.WorkflowRefused, match="cannot be rotated yet"):
        await credential_key_rotation.start_credential_key_rotation(
            store, key="user", secret=NEW_USER, admin_id="admin-token"
        )


async def test_environment_values_stay_immutable_to_every_other_write(store) -> None:
    """Only a flagged, ciphertext-only rewrite passes; the flag alone does not license edits."""
    cipher = CredentialCipher(OLD_USER, domain=ENVIRONMENT_DOMAIN)
    conn = await asyncpg.connect(**_db_config())
    try:
        await _insert_family_rows(
            conn,
            {
                "agent_environment_version_variables": {
                    "version_id": "v-imm",
                    "name": "TOKEN",
                    "value_ciphertext": cipher.encrypt("x"),
                }
            },
        )
        update = "UPDATE agent_environment_version_variables SET {} WHERE version_id = 'v-imm'"
        with pytest.raises(asyncpg.RaiseError, match="immutable"):
            await conn.execute(update.format("value_ciphertext = 'other'"))
        async with conn.transaction():
            await conn.execute("SET LOCAL cloud_agent.credential_reencryption = 'on'")
            with pytest.raises(asyncpg.RaiseError, match="immutable"):
                await conn.execute(update.format("phase = 'runtime'"))
        async with conn.transaction():
            await conn.execute("SET LOCAL cloud_agent.credential_reencryption = 'on'")
            await conn.execute(update.format(f"value_ciphertext = '{cipher.encrypt('x')}'"))
    finally:
        await conn.close()
