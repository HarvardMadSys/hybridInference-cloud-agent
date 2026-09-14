"""A slow OAuth refresh cannot block unrelated MCP capabilities or outlive authority."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from cloud_agent.api import mcp_relay
from cloud_agent.mcp.refresh import RefreshRefused
from cloud_agent.mcp.registry import mcp_cipher
from tests.servers.test_mcp_relay_host import (
    _auth,
    _rpc,
    _secrets as _secrets,
    client as client,
    host as host,
    store as store,
)

pytestmark = pytest.mark.asyncio


def _oauth(store):
    store.connections[7].update(
        auth_kind="oauth",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        refresh_token_ciphertext=mcp_cipher().encrypt("refresh-fixture"),
        issuer="http://127.0.0.1:9",
        resource="http://127.0.0.1:9/mcp",
    )


@pytest.mark.parametrize(
    "method", ["tools/list", "resources/list", "resources/templates/list", "prompts/list"]
)
async def test_catalogue_does_not_contact_an_oauth_endpoint(
    client, store, host, monkeypatch, method
):
    _oauth(store)
    refreshed = []

    async def refresh(repository, *, row):
        refreshed.append(row["id"])
        await asyncio.Event().wait()

    monkeypatch.setattr(mcp_relay, "ensure_fresh", refresh)
    response = await asyncio.wait_for(
        client.post("/v1/agent/mcp", json=_rpc(method), headers=_auth()), 0.5
    )
    assert "result" in response.json()
    assert refreshed == []
    assert host.opened == []


async def test_a_healthy_tool_does_not_wait_for_another_connections_refresh(
    client, store, host, monkeypatch
):
    _oauth(store)
    refreshed = []

    async def refresh(repository, *, row):
        refreshed.append(row["id"])
        if row["id"] == 7:
            await asyncio.Event().wait()
        return row

    monkeypatch.setattr(mcp_relay, "ensure_fresh", refresh)
    response = await asyncio.wait_for(
        client.post(
            "/v1/agent/mcp",
            json=_rpc("tools/call", name="docs__search", arguments={}),
            headers=_auth(),
        ),
        0.5,
    )
    assert "result" in response.json()
    assert refreshed == [8]
    assert host.called[-1]["name"] == "search"


@pytest.mark.parametrize(
    "change", ["lease", "binding", "policy", "connection", "capability", "alias"]
)
@pytest.mark.parametrize("surface", ["tool", "prompt", "resource", "direct"])
async def test_changes_during_refresh_prevent_dispatch(
    client, store, host, monkeypatch, change, surface
):
    _oauth(store)
    store.snapshots[7]["resources"] = [{"name": "repo://notes"}]
    store.snapshots[7]["prompts"] = [{"name": "summarize"}]
    store.bindings[0]["resource_rules"] = {"repo://notes": "allow"}
    store.bindings[0]["prompt_rules"] = {"summarize": "allow"}
    request = {
        "tool": _rpc("tools/call", name="github__get_issue", arguments={}),
        "prompt": _rpc("prompts/get", name="github__summarize", arguments={}),
        "resource": _rpc("resources/read", uri="mcp-resource://7/repo://notes"),
        "direct": _rpc("tools/call", name="get_issue", arguments={}),
    }[surface]
    started = asyncio.Event()
    finish = asyncio.Event()

    async def refresh(repository, *, row):
        if row["id"] == 7:
            started.set()
            await finish.wait()
        return row

    monkeypatch.setattr(mcp_relay, "ensure_fresh", refresh)
    task = asyncio.create_task(
        client.post(
            "/v1/agent/mcp/github" if surface == "direct" else "/v1/agent/mcp",
            json=request,
            headers=_auth(),
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    if change == "lease":
        store.live = False
    elif change == "binding":
        store.bindings[0]["enabled"] = False
    elif change == "policy":
        store.bindings[0]["policy_revision"] += 1
        store.bindings[0]["rules"]["get_issue"]["policy"] = "deny"
    elif change == "connection":
        store.connections[7]["authorization_epoch"] += 1
    elif change == "capability":
        store.connections[7]["capability_revision"] += 1
        store.snapshots[7]["tools"] = []
    else:
        store.connections[9] = {**store.connections[7], "id": 9}
        store.snapshots[9] = {**store.snapshots[7], "connection_id": 9}
        store.bindings[0]["connection_id"] = 9
    finish.set()
    response = await asyncio.wait_for(task, 1)
    assert "error" in response.json() or response.status_code in {401, 403, 503}
    assert host.called == []
    assert host.opened == []
    assert host.read == []
    assert host.prompts == []


async def test_a_refresh_failure_is_local_to_the_selected_connection(
    client, store, host, monkeypatch
):
    _oauth(store)
    refreshed = []

    async def refresh(repository, *, row):
        refreshed.append(row["id"])
        if row["id"] == 7:
            raise RefreshRefused("fixture issuer unavailable")
        return row

    monkeypatch.setattr(mcp_relay, "ensure_fresh", refresh)
    failed = await client.post(
        "/v1/agent/mcp", json=_rpc("tools/call", name="github__get_issue"), headers=_auth()
    )
    assert failed.status_code == 200
    assert "reconnected" in failed.json()["error"]["message"]
    healthy = await client.post(
        "/v1/agent/mcp", json=_rpc("tools/call", name="docs__search"), headers=_auth()
    )
    assert "result" in healthy.json()
    assert refreshed == [7, 8]
    assert len(host.called) == 1


async def test_the_selected_call_uses_its_fresh_credential(client, store, host, monkeypatch):
    _oauth(store)

    async def refresh(repository, *, row):
        assert row["id"] == 7
        store.connections[7]["access_token_ciphertext"] = mcp_cipher().encrypt("fresh-fixture")
        return dict(store.connections[7])

    monkeypatch.setattr(mcp_relay, "ensure_fresh", refresh)
    response = await client.post(
        "/v1/agent/mcp", json=_rpc("tools/call", name="github__get_issue"), headers=_auth()
    )
    assert "result" in response.json()
    assert host.opened[-1]["secret"] == "fresh-fixture"


async def test_approval_cannot_dispatch_after_its_refresh_outlives_the_lease(
    client, store, host, monkeypatch
):
    _oauth(store)
    store.bindings[0]["rules"]["get_issue"]["policy"] = "confirm"
    store.wait_action = "pending"
    started, finish = asyncio.Event(), asyncio.Event()

    async def refresh(repository, *, row):
        if store.created_invocations:
            started.set()
            await finish.wait()
        return row

    monkeypatch.setattr(mcp_relay, "ensure_fresh", refresh)
    task = asyncio.create_task(
        client.post(
            "/v1/agent/mcp", json=_rpc("tools/call", name="github__get_issue"), headers=_auth()
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    store.approved = {**store.created_invocations[0], "status": "approved"}
    store.live = False
    finish.set()
    response = await asyncio.wait_for(task, 1)
    assert "error" in response.json()
    assert store.created_invocations[0]["status"] == "expired"
    assert store.claimed == []
    assert host.opened == []
