"""Real PostgreSQL invariants for encrypted immutable repository environments."""

from __future__ import annotations

# ruff: noqa: F811 -- imported pytest fixtures remain parameters
import asyncio
import json

import pytest
from tests.integration.storage.test_agent_job_store import pool, store  # noqa: F401

from cloud_agent.environments import EnvironmentConflict, EnvironmentNotFound
from cloud_agent_shared.environment import EnvironmentScripts

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _save(
    store,
    *,
    owner="alice",
    repo="org/repo",
    name="Development",
    revision=0,
    environment_id=None,
    variables=None,
    script="install",
):
    return await store.environments.save_environment(
        owner,
        repo=repo,
        name=name,
        environment_id=environment_id,
        revision=revision,
        document=EnvironmentScripts(setup_script=script if repo else "").model_dump(),
        edits=variables or [],
    )


async def _job(store, env, overrides=None):
    return await store.conversation_commands.create_job(
        user_id="alice",
        repo=env["repo"],
        task_prompt="work",
        runtime="claude-code",
        model="test-model",
        environment_selection={
            "environment_id": env["id"],
            "environment_id_provided": True,
            "environment_revision": env["revision"],
            "overrides": overrides or [],
        },
    )


async def test_ciphertext_and_write_only_values_at_every_version(store, pool, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store, variables=[{"name": "TOKEN", "value": "private-one", "secret": True}])
    assert env["variables"][0]["value"] is None
    async with pool.acquire() as conn:
        encrypted = await conn.fetchval(
            "SELECT value_ciphertext FROM agent_environment_version_variables"
        )
    assert "private-one" not in encrypted
    history = await store.environments.list_environment_versions("alice", env["id"])
    assert "private-one" not in json.dumps(history, default=str)
    with pytest.raises(EnvironmentNotFound):
        await store.environments.get_environment("bob", env["id"])
    with pytest.raises(ValueError):
        await _save(
            store,
            environment_id=env["id"],
            revision=1,
            variables=[{"name": "TOKEN", "operation": "keep", "secret": False}],
        )


async def test_new_messages_keep_bound_versions_after_rotation_and_global_edit(store, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    await _save(
        store,
        repo=None,
        name="Global",
        script="",
        variables=[{"name": "GLOBAL", "value": "global-one"}],
    )
    env = await _save(store, variables=[{"name": "TOKEN", "value": "private-one"}])
    job = await _job(store, env, [{"name": "TASK_ONLY", "value": "override-one"}])
    binding = job["environment_binding_id"]
    await _save(
        store,
        environment_id=env["id"],
        revision=1,
        script="new install",
        variables=[{"name": "TOKEN", "value": "private-two"}],
    )
    await _save(
        store,
        repo=None,
        name="Global",
        revision=1,
        variables=[{"name": "GLOBAL", "value": "global-two"}],
    )
    old = await store.environments.get_environment_binding("alice", binding)
    assert old.setup_script == "install"
    assert old.values("runtime") == {
        "GLOBAL": "global-one",
        "TOKEN": "private-one",
        "TASK_ONLY": "override-one",
    }
    new = await _job(store, await store.environments.get_environment("alice", env["id"]))
    assert (
        await store.environments.get_environment_binding("alice", new["environment_binding_id"])
    ).values("runtime") == {"GLOBAL": "global-two", "TOKEN": "private-two"}
    await store.environments.archive_environment("alice", env["id"], 2)
    assert (await store.environments.get_environment_binding("alice", binding)).secrets()[
        "TOKEN"
    ] == "private-one"
    archived_view = await store.environments.workspace_environment_view("alice", job["thread_id"])
    assert archived_view["revision"] == 1
    assert archived_view["environment_name"] == "Development"
    assert archived_view["environment_archived"] is True
    assert all(v["value"] is None for v in archived_view["variables"] if v["secret"])


async def test_two_writers_cas_and_delete_recreate_cannot_hit_new_identity(store, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store)
    results = await asyncio.gather(
        *[
            _save(store, environment_id=env["id"], revision=1, script=f"install-{i}")
            for i in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(r, EnvironmentConflict) for r in results) == 1
    await store.environments.archive_environment("alice", env["id"], 2)
    replacement = await _save(store)
    assert replacement["id"] != env["id"]
    with pytest.raises(EnvironmentNotFound):
        await _save(store, environment_id=env["id"], revision=1)


async def test_restore_and_copy_do_not_mutate_existing_binding_or_share_future_edits(
    store, monkeypatch
):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store, variables=[{"name": "TOKEN", "value": "first-secret"}])
    job = await _job(store, env)
    await _save(
        store,
        environment_id=env["id"],
        revision=1,
        variables=[{"name": "TOKEN", "value": "second-secret"}],
    )
    restored = await store.environments.restore_environment(
        "alice", env["id"], revision=2, source_revision=1
    )
    assert restored["revision"] == 3
    copied = await store.environments.copy_environment(
        "alice", env["id"], repo="org/another", name="Copy", revision=3
    )
    assert copied["id"] != env["id"] and copied["revision"] == 1
    assert (
        await store.environments.get_environment_binding("alice", job["environment_binding_id"])
    ).secrets() == {"TOKEN": "first-secret"}


async def test_effective_limits_are_atomic_across_global_and_environment_writes(store, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(
        store, variables=[{"name": f"LOCAL{i}", "value": "test-value"} for i in range(75)]
    )
    await _save(
        store,
        repo=None,
        name="Global",
        variables=[{"name": f"GLOBAL{i}", "value": "test-value"} for i in range(75)],
    )
    results = await asyncio.gather(
        _save(
            store,
            environment_id=env["id"],
            revision=1,
            variables=[{"name": "EXTRA_LOCAL", "value": "extra-value"}],
        ),
        _save(
            store,
            repo=None,
            name="Global",
            revision=1,
            variables=[{"name": "EXTRA_GLOBAL", "value": "extra-value"}],
        ),
        return_exceptions=True,
    )
    assert all(isinstance(result, ValueError) for result in results)
    assert (await store.environments.get_environment("alice", env["id"]))["revision"] == 1
    assert (await store.environments.get_global_environment("alice"))["revision"] == 1


async def test_cancellation_keeps_host_capacity_until_confirmed_cleanup(store, pool, monkeypatch):
    from tests.integration.storage.test_agent_job_store import (
        _SHARED_HOST_CAPABILITIES,
        _shared_claim_attestation,
    )

    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store)
    run = await store.environments.create_environment_run(
        "alice",
        repo="org/repo",
        base_sha="a" * 40,
        selection={"environment_id": env["id"], "environment_id_provided": True},
        key="first",
    )
    await store.host_pool.register_host(
        host="environment-host",
        credential_hash="credential",
        capabilities=[*_SHARED_HOST_CAPABILITIES, "environment-v1"],
        slots_total=1,
    )
    await store.host_pool.record_host_heartbeat(
        host="environment-host",
        capabilities=[*_SHARED_HOST_CAPABILITIES, "environment-v1"],
        sandbox_image_digest="sha256:shared-image",
    )
    claimed = await store.environments.claim_environment_run(
        "environment-host", "credential", _shared_claim_attestation()
    )
    assert claimed and claimed["id"] == run["id"]
    await store.environments.cancel_environment_run("alice", run["id"])
    second = await store.environments.create_environment_run(
        "alice",
        repo="org/repo",
        base_sha="a" * 40,
        selection={"environment_id": env["id"], "environment_id_provided": True},
        key="second",
    )
    assert (
        await store.environments.claim_environment_run(
            "environment-host", "credential", _shared_claim_attestation()
        )
        is None
    )
    assert not await store.environments.update_environment_run(
        run["id"],
        "environment-host",
        claimed["lease_generation"] + 1,
        {"state": "cancelled", "cleanup_complete": True},
    )
    assert await store.environments.update_environment_run(
        run["id"],
        "environment-host",
        claimed["lease_generation"],
        {"state": "succeeded", "cleanup_complete": True},
    )
    assert (await store.environments.get_environment_run("alice", run["id"]))[
        "state"
    ] == "cancelled"
    assert (
        await store.environments.claim_environment_run(
            "environment-host", "credential", _shared_claim_attestation()
        )
    )["id"] == second["id"]


async def test_revoked_credential_and_missing_replica_attestation_cannot_claim(store, monkeypatch):
    from tests.integration.storage.test_agent_job_store import (
        _SHARED_HOST_CAPABILITIES,
        _shared_claim_attestation,
    )

    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store)
    await store.environments.create_environment_run(
        "alice",
        repo="org/repo",
        base_sha="a" * 40,
        selection={"environment_id": env["id"], "environment_id_provided": True},
        key="test",
    )
    await store.host_pool.register_host(
        host="environment-host",
        credential_hash="new-credential",
        capabilities=_SHARED_HOST_CAPABILITIES,
        slots_total=1,
    )
    assert (
        await store.environments.claim_environment_run(
            "environment-host", "old-credential", _shared_claim_attestation()
        )
        is None
    )
    assert (
        await store.environments.claim_environment_run("environment-host", "new-credential", {})
        is None
    )


async def test_gc_preserves_referenced_old_secrets_and_removes_unreferenced_versions(
    store, pool, monkeypatch
):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store, variables=[{"name": "TOKEN", "value": "original-secret"}])
    job = await _job(store, env)
    await _save(
        store,
        environment_id=env["id"],
        revision=1,
        variables=[{"name": "TOKEN", "value": "unreferenced-secret"}],
    )
    await store.environments.archive_environment("alice", env["id"], 2)
    await pool.execute("UPDATE agent_environments SET archived_at=NOW()-INTERVAL '31 days'")
    await store.environments.maintain_environments()
    assert (
        await store.environments.get_environment_binding("alice", job["environment_binding_id"])
    ).secrets()["TOKEN"] == "original-secret"
    assert (
        await pool.fetchval(
            "SELECT COUNT(*) FROM agent_environment_versions WHERE environment_id=$1", env["id"]
        )
        == 1
    )


async def test_untrusted_test_profile_is_frozen_and_queue_counts_toward_autoscaling(
    store, monkeypatch
):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    monkeypatch.setenv("AGENT_DEFAULT_EXECUTION_PROFILE", "untrusted-kata-v1")
    env = await _save(store)
    run = await store.environments.create_environment_run(
        "alice",
        repo="org/repo",
        base_sha="a" * 40,
        selection={"environment_id": env["id"], "environment_id_provided": True},
        key="untrusted",
    )
    assert run["execution_profile"] == "untrusted-kata-v1"
    monkeypatch.setenv("AGENT_DEFAULT_EXECUTION_PROFILE", "trusted-v1")
    observation = await store.host_pool.autoscaling_observation(expected_deployment_sha="")
    assert observation["runnable_new_group_jobs"] == 1
    assert (await store.environments.get_environment_run("alice", run["id"]))[
        "execution_profile"
    ] == "untrusted-kata-v1"


async def test_copy_retry_and_concurrent_duplicate_create_one_identity(store, pool, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "copy-test-key")
    env = await _save(store, variables=[{"name": "TOKEN", "value": "original-secret"}])
    args = {"repo": "org/repo", "name": "Copied", "revision": 1, "idempotency_key": "copy-key"}
    first, retry = await asyncio.gather(
        *(store.environments.copy_environment("alice", env["id"], **args) for _ in range(2))
    )
    assert first["id"] == retry["id"]
    assert first["variables"][0]["value"] is None
    await _save(
        store,
        environment_id=env["id"],
        revision=1,
        variables=[{"name": "TOKEN", "value": "rotated-secret"}],
    )
    assert (await store.environments.copy_environment("alice", env["id"], **args))["id"] == first[
        "id"
    ]
    with pytest.raises(EnvironmentConflict):
        await store.environments.copy_environment(
            "alice", env["id"], **{**args, "name": "Different"}
        )
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agent_environments WHERE copy_request_key=$1", "copy-key"
            )
            == 1
        )


async def test_action_idempotency_and_terminal_state_are_durable(store, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await store.environments.save_environment(
        "alice",
        repo="org/repo",
        name="Actions",
        environment_id=None,
        revision=0,
        document={"actions": [{"id": "deploy", "name": "Deploy", "command": "deploy"}]},
        edits=[],
    )
    job = await _job(store, env)
    arguments = ("alice", job["thread_id"], job["environment_binding_id"], "deploy", "same-request")
    run = await store.environments.create_environment_action_run(*arguments)
    await store.environments.update_environment_action_run(
        "alice", run["id"], {"state": "succeeded", "exit_code": 0, "output": "deployed"}
    )
    replay = await store.environments.create_environment_action_run(*arguments)
    assert replay["id"] == run["id"] and replay["state"] == "succeeded"
    late = await store.environments.update_environment_action_run(
        "alice", run["id"], {"state": "running", "output": "late acknowledgement"}
    )
    assert late["state"] == "succeeded" and late["output"] == "deployed" and late["exit_code"] == 0


async def test_empty_selection_is_frozen_but_requires_no_environment_protocol(store, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    job = await store.conversation_commands.create_job(
        user_id="alice",
        repo="org/repo",
        task_prompt="work",
        runtime="claude-code",
        model="test-model",
        environment_selection={},
    )
    assert job["environment_binding_id"]
    bound = await store.environments.get_job_environment(job["id"])
    assert bound.required is False
    await _save(
        store, repo=None, name="Global", variables=[{"name": "TOKEN", "value": "global-token"}]
    )
    assert (await store.environments.get_job_environment(job["id"])).required is False


async def test_short_secret_is_rejected_before_any_configuration_is_written(store, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    with pytest.raises(ValueError, match="at least 8"):
        await _save(store, variables=[{"name": "DEBUG", "value": "1"}])
    assert await store.environments.list_environments("alice", "org/repo") == []
    env = await _save(store, variables=[{"name": "DEBUG", "value": "1", "secret": False}])
    assert env["variables"][0]["value"] == "1"
