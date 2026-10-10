"""Settings initialization and concurrent patches against actual Postgres."""

from __future__ import annotations

import asyncio

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
