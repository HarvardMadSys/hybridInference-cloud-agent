"""Generated setup reservations and activation are fenced by real Postgres locks."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from tests.integration.storage.test_host_pool_store import db as db, store as store
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.compute_secrets import ComputeSecretStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0042_sandbox_archive_settings"
_THIS = "0043_automatic_compute_setup"
_REQUEST = "prepare-hosts"
_FINGERPRINT = "a" * 64
_SOURCE = b"reviewed release source fixture"


async def _save_profile(repo, profile_id, *, request_id=_REQUEST, slot=0, fingerprint=_FINGERPRINT):
    """Persist the same immutable shape used before tunnel authorization begins."""
    configuration = {
        "deployment_sha": "a" * 40,
        "deploy_root": "/opt/cloud-agent-" + profile_id,
        "files": [".compute-secrets/gateway.key"],
        "install_prerequisites": True,
        "timeout_seconds": 3600,
    }
    if request_id is not None:
        configuration["automatic_setup"] = {
            "request_id": request_id,
            "fingerprint": fingerprint,
            "slot": slot,
            "slots": 2,
            "ready": False,
            "endpoint": {
                "main_port": 8000,
                "mcp_port": 8001,
                "gateway_ip": "172.30.0.1",
                "machine_identity": "b" * 64,
            },
            "reverse_port": 20000 + slot,
        }
    ciphertext = ComputeSecretStore(repo, key="x" * 32).encrypt(
        "profile:" + profile_id,
        "deployment_profile",
        json.dumps(
            {
                "environment": "AGENT_SANDBOX_MANAGER_TOKEN='fixture-sandbox-manager-token'\n",
                "files": {".compute-secrets/gateway.key": "fixture-key-for-" + profile_id},
            }
        ),
    )
    return await repo.save_deployment_profile(
        profile_id=profile_id,
        label="Setup " + profile_id,
        configuration=configuration,
        source_bundle=_SOURCE,
        source_sha256=hashlib.sha256(_SOURCE).hexdigest(),
        secret_ciphertext=ciphertext,
    )


async def test_concurrent_same_request_slot_keeps_one_winners_exact_inputs(store):
    """Different route IDs cannot give concurrent retries two sets of tunnel keys."""
    repo = store.compute_resources
    outcomes = await asyncio.gather(
        _save_profile(repo, "route-one"),
        _save_profile(repo, "route-two"),
        return_exceptions=True,
    )
    winners = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(winners) == len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert str(failures[0]) == "deployment_profile_exists"
    winner = winners[0]
    assert await repo.get_deployment_profile(winner["id"]) == winner
    assert (
        await repo.get_deployment_profile(
            "route-two" if winner["id"] == "route-one" else "route-one"
        )
        is None
    )
    assert len(await repo.list_deployment_profiles()) == 1

    # The constraint belongs to request/slot, not to its fingerprint or route.
    with pytest.raises(ValueError, match=r"^deployment_profile_exists$"):
        await _save_profile(repo, "changed-route", fingerprint="c" * 64)
    assert await repo.get_deployment_profile(winner["id"]) == winner


async def test_slot_constraint_allows_other_slots_requests_and_custom_profiles(store):
    """The additive index must leave existing administrator profiles selectable."""
    repo = store.compute_resources
    rows = [
        await _save_profile(repo, "first-slot"),
        await _save_profile(repo, "second-slot", slot=1),
        await _save_profile(repo, "other-request", request_id="another-setup"),
        await _save_profile(repo, "custom-one", request_id=None),
        await _save_profile(repo, "custom-two", request_id=None),
    ]
    assert {row["id"] for row in await repo.list_deployment_profiles()} == {
        row["id"] for row in rows
    }
    for row in rows:
        assert await repo.get_deployment_profile(row["id"]) == row


async def test_activation_is_atomic_idempotent_and_preserves_release_and_secrets(store):
    """Concurrent completion changes readiness once and never replaces saved inputs."""
    repo = store.compute_resources
    originals = [
        await _save_profile(repo, "first-slot"),
        await _save_profile(repo, "second-slot", slot=1),
    ]
    custom = await _save_profile(repo, "custom", request_id=None)
    activation = {
        "profile_ids": [row["id"] for row in originals],
        "request_id": _REQUEST,
        "request_fingerprint": _FINGERPRINT,
    }
    await asyncio.gather(
        repo.activate_automatic_profiles(**activation),
        repo.activate_automatic_profiles(**activation),
    )
    secrets = ComputeSecretStore(repo, key="x" * 32)
    activated = []
    for original in originals:
        row = await repo.get_deployment_profile(original["id"])
        assert row is not None
        assert row["revision"] == original["revision"] + 1
        assert row["configuration"]["automatic_setup"]["ready"] is True
        assert row["updated_at"] >= original["updated_at"]
        normalized = {
            **row,
            "revision": original["revision"],
            "updated_at": original["updated_at"],
            "configuration": {
                **row["configuration"],
                "automatic_setup": {**row["configuration"]["automatic_setup"], "ready": False},
            },
        }
        assert normalized == original
        plaintext = json.loads(
            secrets.decrypt("profile:" + row["id"], "deployment_profile", row["secret_ciphertext"])
        )
        assert plaintext["files"][".compute-secrets/gateway.key"] == "fixture-key-for-" + row["id"]
        activated.append(row)
    assert await repo.get_deployment_profile(custom["id"]) == custom
    await repo.activate_automatic_profiles(**activation)
    for row in activated:
        assert await repo.get_deployment_profile(row["id"]) == row


@pytest.mark.parametrize("conflict", ["request_id", "fingerprint", "missing", "custom"])
async def test_activation_refusal_rolls_back_every_slot_and_releases_locks(store, conflict):
    """One mismatched or missing checkpoint cannot expose the other saved keys."""
    repo = store.compute_resources
    first = await _save_profile(repo, "first-slot")
    if conflict == "missing":
        second = None
        second_id = "missing-slot"
        expected_error = "record_not_found"
    else:
        second = await _save_profile(
            repo,
            "second-slot",
            slot=1,
            request_id=(
                None
                if conflict == "custom"
                else "other-setup"
                if conflict == "request_id"
                else _REQUEST
            ),
            fingerprint="c" * 64 if conflict == "fingerprint" else _FINGERPRINT,
        )
        second_id = second["id"]
        expected_error = "idempotency_conflict"
    with pytest.raises(ValueError, match="^" + expected_error + "$"):
        await repo.activate_automatic_profiles(
            profile_ids=[first["id"], second_id],
            request_id=_REQUEST,
            request_fingerprint=_FINGERPRINT,
        )
    assert await repo.get_deployment_profile(first["id"]) == first
    assert await repo.get_deployment_profile(second_id) == second
    # A refused transaction leaves no lock or aborted connection behind.
    await asyncio.wait_for(
        repo.activate_automatic_profiles(
            profile_ids=[first["id"]],
            request_id=_REQUEST,
            request_fingerprint=_FINGERPRINT,
        ),
        timeout=5,
    )
    assert (await repo.get_deployment_profile(first["id"]))["configuration"]["automatic_setup"][
        "ready"
    ] is True
    assert await repo.get_deployment_profile(second_id) == second


async def test_additive_migration_round_trip_preserves_existing_profiles(db, store):
    """Upgrade the deployed previous schema over custom data, then remove only the index."""
    repo = store.compute_resources
    custom = await _save_profile(repo, "existing-custom", request_id=None)
    config = _db_config()
    try:
        downgrade_to(config, _BEFORE)
        async with db.acquire() as conn:
            assert (
                await conn.fetchval("SELECT to_regclass('idx_compute_automatic_setup_slot')")
                is None
            )
        assert await repo.get_deployment_profile(custom["id"]) == custom

        apply_migrations(config, _THIS)
        async with db.acquire() as conn:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == _THIS
            assert (
                await conn.fetchval("SELECT to_regclass('idx_compute_automatic_setup_slot')")
                is not None
            )
        assert await repo.get_deployment_profile(custom["id"]) == custom
        generated = await _save_profile(repo, "generated")
        with pytest.raises(ValueError, match=r"^deployment_profile_exists$"):
            await _save_profile(repo, "duplicate-slot")

        downgrade_to(config, _BEFORE)
        assert await repo.get_deployment_profile(custom["id"]) == custom
        assert await repo.get_deployment_profile(generated["id"]) == generated
        # The downgrade really removed the slot constraint without deleting data.
        duplicate = await _save_profile(repo, "duplicate-slot")
        async with db.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_compute_deployment_profiles WHERE id=$1", duplicate["id"]
            )
    finally:
        apply_migrations(config)
    assert await repo.get_deployment_profile(custom["id"]) == custom
    assert await repo.get_deployment_profile(generated["id"]) == generated
