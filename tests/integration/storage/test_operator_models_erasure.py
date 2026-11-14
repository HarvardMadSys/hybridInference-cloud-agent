"""Erasing an account takes its allowances and its request counts on provided models with it."""

from __future__ import annotations

import pytest
from tests.integration.storage.test_local_account_parity_store import (  # noqa: F401 — fixtures
    _secret,
    _stopped,
    pool,
    storage,
)
from tests.support.local_accounts import act, make_account

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ID = "oconn_" + "f" * 32


async def test_an_erased_account_leaves_no_allowance_or_usage_row(storage, pool) -> None:  # noqa: F811
    models = storage.operator_models
    await models.create_operator_connection(
        values={
            "id": _ID,
            "name": "Provider",
            "base_url": "https://provider.example/v1",
            "models": ["m1"],
            "api_key_ciphertext": "ciphertext",
            "user_concurrency": 4,
            "model_checks": {"m1": {"protocols": {"chat_completions": {"status": "supported"}}}},
        }
    )
    await models.set_operator_limit(
        connection_id=_ID, model="m1", user_daily_requests=10, global_concurrency=None
    )
    gone = await make_account(storage, "gone@example.edu")
    stays = await make_account(storage, "stays@example.edu")
    for account in (gone, stays):
        await models.set_operator_user_limit(
            connection_id=_ID, user_id=account, model="m1", daily_requests=50
        )
        counted = await models.count_operator_request(
            connection_id=_ID, user_id=account, model="m1"
        )
        assert counted["outcome"] == "counted"

    await act(storage, gone, "delete")
    await _stopped(storage, gone)
    await storage.local_accounts.erase_account(
        account_id=gone, actor_id="admin-token", actor_account_id=None, admin_ip="t"
    )

    async with pool.acquire() as conn:
        for table in ("agent_operator_model_user_limits", "agent_operator_model_usage"):
            owners = await conn.fetch(f"SELECT user_id FROM {table}")
            assert [row["user_id"] for row in owners] == [stays], table
    # The connection and the model's default are the deployment's, and stay.
    assert [row["id"] for row in await models.list_operator_connections()] == [_ID]
    assert (await models.operator_limits(connection_ids=[_ID]))[_ID]["m1"][
        "user_daily_requests"
    ] == 10
