"""Settings initialization and concurrent patches against actual Postgres."""

from __future__ import annotations

import asyncio
import os
from urllib.parse import quote

import httpx
import pytest
from tests.integration.storage.test_host_pool_store import db as db, store as store

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_first_import_is_persisted_once_and_later_imports_preserve_admin_values(store):
    assert await store.settings.read_settings() == (-1, {})
    assert await store.settings.seed_settings({"limit": 20, "secret": "ciphertext"}) == (
        1,
        {"limit": 20, "secret": "ciphertext"},
    )
    await store.settings.update_settings({"limit": 7})
    assert await store.settings.seed_settings({"limit": 99, "secret": "other"}) == (
        2,
        {"limit": 7, "secret": "ciphertext"},
    )


async def test_concurrent_patches_preserve_both_changes_and_advance_revision(store):
    await store.settings.seed_settings({"first": 1, "second": 2, "unchanged": 3})
    results = await asyncio.gather(
        store.settings.update_settings({"first": 10}),
        store.settings.update_settings({"second": 20}),
    )
    assert {result[0] for result in results} == {2, 3}
    assert await store.settings.read_settings() == (
        3,
        {"first": 10, "second": 20, "unchanged": 3},
    )
    first, second = sorted(results, key=lambda result: result[0])
    assert first[2] == {"first": 1, "second": 2, "unchanged": 3}
    assert second[2] == first[1]


async def test_an_unchanged_patch_keeps_revision_and_null_is_persisted_for_reset(store):
    await store.settings.seed_settings({"limit": 20, "secret": "ciphertext"})
    assert await store.settings.update_settings({"limit": 20}) == (
        1,
        {"limit": 20, "secret": "ciphertext"},
        {"limit": 20, "secret": "ciphertext"},
    )
    revision, current, previous = await store.settings.update_settings({"secret": None})
    assert revision == 2
    assert current == {"limit": 20, "secret": None}
    assert previous == {"limit": 20, "secret": "ciphertext"}


async def test_concurrent_cross_field_updates_cannot_commit_an_invalid_combination(store):
    await store.settings.seed_settings(
        {"attachments.max_file_bytes": 2, "attachments.max_total_bytes": 4}
    )
    results = await asyncio.gather(
        store.settings.update_settings({"attachments.max_file_bytes": 4}),
        store.settings.update_settings({"attachments.max_total_bytes": 2}),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    revision, encoded = await store.settings.read_settings()
    assert revision == 2
    assert encoded["attachments.max_file_bytes"] <= encoded["attachments.max_total_bytes"]


async def test_new_fields_are_imported_atomically_without_replacing_a_concurrent_admin_edit(store):
    await store.settings.seed_settings({"publish_base_branch": "saved"})
    await asyncio.gather(
        store.settings.seed_settings(
            {"publish_base_branch": "stale", "gateway_issuer": "https://identity.example"}
        ),
        store.settings.update_settings({"publish_base_branch": "admin-edit"}),
    )
    revision, values = await store.settings.read_settings()
    assert revision == 3
    assert values == {
        "publish_base_branch": "admin-edit",
        "gateway_issuer": "https://identity.example",
    }
    assert await store.settings.seed_settings({"gateway_issuer": "https://stale.example"}) == (
        revision,
        values,
    )


async def test_saved_issuer_update_pins_identity_and_a_conflict_rolls_back_the_entire_patch(
    store, db
):
    from cloud_agent.identity_namespace import IdentityNamespaceError

    await store.settings.seed_settings({"gateway_issuer": "", "publish_base_branch": "original"})
    await store.settings.update_settings({"gateway_issuer": "https://identity.example"})
    async with db.acquire() as conn:
        assert (
            await conn.fetchval("SELECT gateway_issuer FROM agent_identity_namespace")
            == "https://identity.example"
        )
    with pytest.raises(IdentityNamespaceError):
        await store.settings.update_settings(
            {"gateway_issuer": "https://foreign.example", "publish_base_branch": "wrong"}
        )
    revision, values = await store.settings.read_settings()
    assert revision == 2
    assert values["publish_base_branch"] == "original"
    assert values["gateway_issuer"] == "https://identity.example"


async def test_upgrade_preserves_the_recorded_issuer_when_legacy_env_is_absent(store, db):
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_identity_namespace (singleton, gateway_issuer) VALUES (TRUE, $1)",
            "https://identity.example",
        )
    revision, values = await store.settings.seed_settings({"gateway_issuer": ""})
    assert revision == 1
    assert values["gateway_issuer"] == "https://identity.example"


async def test_empty_deployment_starts_and_saves_missing_keys_through_recovery_setup(
    db, monkeypatch
):
    from cloud_agent import settings
    from cloud_agent.app import create_app, create_mcp_app

    settings.reset_settings()
    for definition in settings.SETTINGS:
        if definition.env:
            monkeypatch.delenv(definition.env, raising=False)
    monkeypatch.delenv("AGENT_COMPUTE_CONFIG", raising=False)
    monkeypatch.delenv("AGENT_GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setenv(
        "AGENT_SETTINGS_ENCRYPTION_KEY", "test-settings-root-key-over-thirty-two-bytes"
    )
    monkeypatch.setenv("ADMIN_TOKEN", "test-recovery-admin")
    user = quote(os.environ.get("TEST_DB_USER", "postgres"), safe="")
    password = quote(os.environ.get("TEST_DB_PASSWORD", "postgres"), safe="")
    database = quote(os.environ.get("TEST_DB_NAME", "hybridinference_test_db"), safe="")
    monkeypatch.setenv(
        "AGENT_DATABASE_URL",
        f"postgresql://{user}:{password}@{os.environ.get('TEST_DB_HOST', 'localhost')}:"
        f"{os.environ.get('TEST_DB_PORT', '5432')}/{database}",
    )
    # Exercise the real startup and workers while containing public catalog HTTP.
    client_class = httpx.AsyncClient

    def isolated_client(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(lambda _: httpx.Response(503)))
        return client_class(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", isolated_client)
    recovery = {"Authorization": "Bearer test-recovery-admin"}
    signing_key = "test-session-signing-key-over-thirty-two-bytes"
    app = create_app()
    try:
        async with (
            app.router.lifespan_context(app),
            client_class(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            assert (await client.get("/admin/agent/settings")).status_code == 401
            initial = await client.get("/admin/agent/settings", headers=recovery)
            assert initial.status_code == 200
            assert initial.json()["setup"]["required"] is True
            assert initial.json()["secret_configured"]["session_signing_secret"] is False
            saved = await client.put(
                "/admin/agent/settings",
                headers=recovery,
                json={"settings": {"session_signing_secret": signing_key}},
            )
            assert saved.status_code == 200
            assert saved.json()["secret_configured"]["session_signing_secret"] is True
            assert signing_key not in saved.text
            assert settings.setting_env("AGENT_SESSION_SECRET") == signing_key
        # A second listener loads the saved key with no runtime secret in env.
        relay = create_mcp_app()
        async with relay.router.lifespan_context(relay):
            assert settings.setting_env("AGENT_SESSION_SECRET") == signing_key
    finally:
        settings.reset_settings()
