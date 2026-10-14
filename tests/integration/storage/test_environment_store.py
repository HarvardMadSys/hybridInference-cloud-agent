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


async def test_runtime_rotation_keeps_setup_owner_cas_and_old_secret_scan(store, pool, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store, variables=[{"name": "TOKEN", "value": "original-secret"}])
    job = await _job(store, env)
    binding = job["environment_binding_id"]
    await _save(
        store,
        environment_id=env["id"],
        revision=1,
        script="new-install",
        variables=[{"name": "TOKEN", "value": "rotated-secret"}],
    )
    assert await store.environments.refresh_runtime_environment("alice", binding, 0) == {
        "runtime_revision": 1
    }
    bound = await store.environments.get_environment_binding("alice", binding)
    assert bound.setup_script == "install"
    assert bound.values("setup") == {"TOKEN": "original-secret"}
    assert bound.values("runtime") == {"TOKEN": "rotated-secret"}
    with pytest.raises(EnvironmentConflict):
        await store.environments.refresh_runtime_environment("alice", binding, 0)
    with pytest.raises(EnvironmentNotFound):
        await store.environments.refresh_runtime_environment("bob", binding, 1)
    # Cache the new secret before another rotation: the runtime version must invalidate it.
    steps = [{"output": "rotated-secret"}]
    assert "rotated-secret" not in str(
        await store.environments.redact_environment_steps("alice", binding, steps)
    )
    await _save(
        store,
        environment_id=env["id"],
        revision=2,
        variables=[{"name": "TOKEN", "value": "newest-secret"}],
    )
    await store.environments.refresh_runtime_environment("alice", binding, 1)
    output = await store.environments.redact_environment_steps(
        "alice", binding, [{"output": "original-secret rotated-secret newest-secret"}]
    )
    assert all(
        secret not in str(output)
        for secret in ("original-secret", "rotated-secret", "newest-secret")
    )
    view = await store.environments.workspace_environment_view("alice", job["thread_id"])
    assert all(
        secret not in json.dumps(view)
        for secret in ("original-secret", "rotated-secret", "newest-secret")
    )
    assert view["runtime_revision"] == 2
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "wrong-key")
    from cloud_agent.environments import EnvironmentEncryptionError

    with pytest.raises(EnvironmentEncryptionError):
        await store.environments.redact_environment_steps("alice", binding, steps)


async def test_test_idempotency_keeps_original_sha_after_branch_moves(store, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store)
    selection = {"environment_id": env["id"], "environment_id_provided": True}
    first = await store.environments.create_environment_run(
        "alice", repo="org/repo", base_sha="a" * 40, selection=selection, key="request", ref="dev"
    )
    replay = await store.environments.create_environment_run(
        "alice", repo="org/repo", base_sha="b" * 40, selection=selection, key="request", ref="dev"
    )
    assert replay["id"] == first["id"] and replay["base_sha"] == "a" * 40
    assert (await store.environments.find_environment_run("alice", "request", selection, "dev"))[
        "id"
    ] == first["id"]
    with pytest.raises(EnvironmentConflict):
        await store.environments.find_environment_run("alice", "request", selection, "other")
    assert await store.environments.find_environment_run("bob", "request", selection, "dev") is None


async def test_expired_reservation_releases_capacity_but_fences_returning_host(
    store, pool, monkeypatch
):
    from tests.integration.storage.test_agent_job_store import (
        _SHARED_HOST_CAPABILITIES,
        _shared_claim_attestation,
    )

    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store, variables=[{"name": "TOKEN", "value": "private-example"}])
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
    assert claimed["id"] == run["id"]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_runs SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=$1",
            run["id"],
        )
    await store.environments.maintain_environments()
    expired = await store.environments.get_environment_run("alice", run["id"])
    assert expired["state"] == "failed" and not expired["cleanup_complete"]
    host = next(
        h for h in await store.host_pool.list_runner_hosts() if h["host"] == "environment-host"
    )
    assert host["slots_used"] == 0
    next_run = await store.environments.create_environment_run(
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
    assert any(
        r["id"] == run["id"]
        for r in await store.environments.list_environment_orphans("environment-host")
    )
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "wrong-key")
    assert await store.environments.update_environment_run(
        run["id"],
        "environment-host",
        claimed["lease_generation"],
        {"cleanup_complete": True, "state": "failed"},
    )
    assert (await store.environments.get_environment_run("alice", run["id"]))["cleanup_complete"]
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    assert (
        await store.environments.claim_environment_run(
            "environment-host", "credential", _shared_claim_attestation()
        )
    )["id"] == next_run["id"]


async def test_preflight_failure_releases_claim_without_decrypting_again(store, pool, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store, variables=[{"name": "TOKEN", "value": "private-example"}])
    run = await store.environments.create_environment_run(
        "alice",
        repo="org/repo",
        base_sha="a" * 40,
        selection={"environment_id": env["id"], "environment_id_provided": True},
        key=None,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_runs SET state='running',host='runner',lease_generation=1,started_at=NOW(),lease_expires_at=NOW()+INTERVAL '2 minutes' WHERE id=$1",
            run["id"],
        )
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "wrong-key")
    assert await store.environments.fail_environment_preflight(run["id"], "runner", 1)
    result = await store.environments.get_environment_run("alice", run["id"])
    assert result["state"] == "failed" and result["cleanup_complete"]
    assert not await store.environments.fail_environment_preflight(run["id"], "runner", 2)


async def test_legacy_disabled_setting_cannot_block_bindings_or_runtime_refresh(
    store, pool, monkeypatch
):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store)
    job = await _job(store, env)
    monkeypatch.setenv("AGENT_REPOSITORY_ENVIRONMENTS_ENABLED", "false")
    created = await _job(store, env)
    assert (await store.environments.get_job_environment(created["id"])).setup_script == "install"
    assert (await store.environments.get_job_environment(job["id"])).setup_script == "install"
    empty = await store.conversation_commands.create_job(
        user_id="bob",
        repo="org/repo",
        task_prompt="work",
        runtime="codex",
        model="test",
        environment_selection={},
    )
    async with pool.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT required FROM agent_environment_bindings WHERE id=$1",
            empty["environment_binding_id"],
        )
        assert await conn.fetchval(
            "SELECT required FROM agent_environment_bindings WHERE id=$1",
            job["environment_binding_id"],
        )
    await _save(
        store,
        owner="bob",
        repo=None,
        name="Global",
        variables=[{"name": "TOKEN", "value": "private-example"}],
    )
    assert (
        await store.environments.refresh_runtime_environment(
            "bob", empty["environment_binding_id"], 0
        )
    )["runtime_revision"] == 1
    refreshed = await store.environments.get_job_environment(empty["id"])
    assert refreshed.required
    assert refreshed.values("runtime")["TOKEN"] == "private-example"
    assert (
        await store.environments.refresh_runtime_environment(
            "alice", job["environment_binding_id"], 0
        )
    )["runtime_revision"] == 1


async def test_unchanged_runtime_secrets_do_not_fill_the_history(store, monkeypatch):
    """Each replaced value is kept once; refreshing without a change adds nothing."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store, variables=[{"name": "TOKEN", "value": "original-secret"}])
    job = await _job(store, env)
    binding = job["environment_binding_id"]
    for revision in range(10):
        await store.environments.refresh_runtime_environment("alice", binding, revision)
    bound = await store.environments.get_environment_binding("alice", binding)
    assert bound.previous_secrets == {}
    await _save(
        store,
        environment_id=env["id"],
        revision=1,
        variables=[{"name": "TOKEN", "value": "rotated-secret"}],
    )
    for revision in range(10, 15):
        await store.environments.refresh_runtime_environment("alice", binding, revision)
    bound = await store.environments.get_environment_binding("alice", binding)
    assert list(bound.previous_secrets.values()) == ["original-secret"]
    assert bound.values("runtime") == {"TOKEN": "rotated-secret"}


async def test_start_skip_only_consumes_requesting_chat_next_turn(store, pool, monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(store)
    first = await _job(store, env)
    binding = first["environment_binding_id"]
    with pytest.raises(EnvironmentConflict):
        await store.environments.request_skip_environment_start("alice", binding, first["id"])
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state='failed' WHERE id=$1", first["id"])
    await store.environments.request_skip_environment_start("alice", binding, first["id"])
    assert not (
        await store.environments.environment_for_claim("alice", binding, first["id"])
    ).skip_start
    second = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="alice", prompt="continue"
    )
    assert (
        await store.environments.environment_for_claim("alice", binding, second["id"])
    ).skip_start
    assert (
        await store.environments.environment_for_claim("alice", binding, second["id"])
    ).skip_start
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state='failed' WHERE id=$1", second["id"])
    third = await store.conversation_commands.create_follow_up(
        parent_job_id=second["id"], user_id="alice", prompt="again"
    )
    assert not (
        await store.environments.environment_for_claim("alice", binding, third["id"])
    ).skip_start


async def test_worker_preparation_summary_rejects_expired_or_replaced_attempt(
    store, pool, monkeypatch
):
    from tests.integration.storage.test_agent_job_store import _reserve_shared_chat

    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "environment-test-key")
    env = await _save(
        store,
        owner="user-1",
        repo="example-org/example-repo",
        variables=[{"name": "TOKEN", "value": "private-example"}],
    )
    job, claim, _sandbox = await _reserve_shared_chat(
        store, environment_selection={"environment_id": env["id"], "environment_id_provided": True}
    )
    receipt = {
        "binding_id": job["environment_binding_id"],
        "container_id": "exact-container",
        "boot_id": "boot",
        "steps": [{"phase": "setup", "state": "succeeded", "output": "private-example"}],
    }
    assert await store.environments.record_worker_environment_preparation(
        job["id"], claim["attempt_id"], claim["lease_generation"], receipt
    )
    view = await store.environments.workspace_environment_view("user-1", job["thread_id"])
    assert "private-example" not in json.dumps(view)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=$1",
            claim["attempt_id"],
        )
    assert not await store.environments.record_worker_environment_preparation(
        job["id"],
        claim["attempt_id"],
        claim["lease_generation"],
        {**receipt, "boot_id": "late-boot"},
    )
    assert not await store.environments.record_worker_environment_preparation(
        job["id"], claim["attempt_id"], claim["lease_generation"] + 1, receipt
    )
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT boot_id FROM agent_environment_preparation") == "boot"


async def test_environment_binding_indexes_cover_reverse_reference_cleanup(pool):
    async with pool.acquire() as conn:
        names = {
            row["indexname"]
            for row in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename IN ('agent_environment_bindings','agent_environment_runs','agent_environment_action_runs','agent_environment_preparation','agent_threads')"
            )
        }
    assert {
        "agent_environment_thread_binding",
        "agent_environment_run_binding",
        "agent_environment_action_binding",
        "agent_environment_preparation_binding",
    } <= names


async def test_downgrade_allows_unused_empty_binding_but_guards_execution(store, pool, monkeypatch):
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    import asyncpg

    migration = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "migrations/versions/0065_environments.py")
    )
    statements = []
    migration["downgrade"].__globals__["op"] = SimpleNamespace(execute=statements.append)
    migration["downgrade"]()
    sql = statements[0]
    empty = await store.conversation_commands.create_job(
        user_id="alice",
        repo="org/repo",
        task_prompt="plain",
        runtime="codex",
        model="test",
        environment_selection={},
    )
    assert empty["environment_binding_id"]
    async with pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await conn.execute(sql)
            assert not await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='agent_threads' AND column_name='environment_binding_id')"
            )
        finally:
            await transaction.rollback()
    # Even a verify-only test has a live container obligation and blocks removal.
    env = await _save(store, script="")
    run = await store.environments.create_environment_run(
        "alice",
        repo="org/repo",
        base_sha="a" * 40,
        selection={"environment_id": env["id"], "environment_id_provided": True},
        key=None,
    )
    async with pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            with pytest.raises(asyncpg.PostgresError, match="drained"):
                await conn.execute(sql)
        finally:
            await transaction.rollback()
    await store.environments.cancel_environment_run("alice", run["id"])
    configured = await _save(
        store, environment_id=env["id"], revision=env["revision"], script="install"
    )
    await _job(store, configured)
    async with pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            with pytest.raises(asyncpg.PostgresError, match="drained"):
                await conn.execute(sql)
        finally:
            await transaction.rollback()
