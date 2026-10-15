"""An MCP request keeps its original attempt and residency through asynchronous waits."""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.servers import test_mcp_approval_resume as approval_fixtures
from tests.servers.test_mcp_approval_resume import (
    upstream as upstream,
    waiting_store as waiting_store,
)
from tests.servers.test_mcp_relay_host import (
    _HOST_URL,
    _RESIDENCY,
    _auth,
    _residency_auth,
    _rpc,
    _secrets as _secrets,
    _snapshot,
    client as client,
    host as host,
    store as store,
)

from cloud_agent.api import mcp_relay

pytestmark = pytest.mark.asyncio
approval_client = approval_fixtures.client


@pytest.mark.parametrize("revoke", [False, True])
async def test_resident_approval_rechecks_the_residency(
    approval_client, waiting_store, upstream, revoke
):
    task = asyncio.create_task(
        approval_client.post(
            _HOST_URL,
            json=_rpc("tools/call", name="github__get_issue", arguments={"id": 7}),
            headers=_residency_auth(),
        )
    )
    await asyncio.wait_for(waiting_store.invocation_created.wait(), 2)
    invocation = waiting_store.created_invocations[0]
    assert invocation["residency_generation"] == _RESIDENCY
    if revoke:
        # The attempt is still live, but the process no longer owns its residency.
        waiting_store.residency_turn = None
    waiting_store.approved = {**invocation, "status": "approved"}
    response = await asyncio.wait_for(task, 3)
    if revoke:
        assert "error" in response.json()
        assert upstream.called == []
        assert waiting_store.claimed == []
        assert invocation["status"] == "expired"
    else:
        assert "result" in response.json()
        assert len(upstream.called) == 1
        assert waiting_store.finished[-1]["status"] == "completed"


@pytest.mark.parametrize("surface", ["merged", "connection"])
async def test_residency_is_rechecked_after_oauth_refresh(
    client, store, host, monkeypatch, surface
):
    refreshing = asyncio.Event()
    proceed = asyncio.Event()
    original = mcp_relay.ensure_fresh
    proxied = []

    async def held_refresh(*args, **kwargs):
        refreshing.set()
        await proceed.wait()
        return await original(*args, **kwargs)

    async def unexpected_proxy(*args, **kwargs):
        proxied.append(kwargs)
        pytest.fail("revoked credentials reached the upstream proxy")

    monkeypatch.setattr(mcp_relay, "ensure_fresh", held_refresh)
    monkeypatch.setattr(mcp_relay, "_proxy", unexpected_proxy)
    task = asyncio.create_task(
        client.post(
            _HOST_URL if surface == "merged" else _HOST_URL + "/docs",
            json=_rpc(
                "tools/call",
                name="docs__search" if surface == "merged" else "search",
                arguments={"q": "review"},
            ),
            headers=_residency_auth(),
        )
    )
    await asyncio.wait_for(refreshing.wait(), 2)
    store.residency_turn = None
    proceed.set()
    response = await asyncio.wait_for(task, 3)
    assert response.status_code == 401 or "error" in response.json()
    assert host.called == []
    assert proxied == []


@pytest.mark.parametrize("credential_kind", ["attempt", "residency"])
async def test_request_cannot_move_to_the_next_turn(client, store, host, credential_kind):
    reading = asyncio.Event()
    proceed = asyncio.Event()

    async def delayed_body():
        # The endpoint authenticates before it starts reading the request body.
        reading.set()
        await proceed.wait()
        yield json.dumps(
            _rpc("tools/call", name="docs__search", arguments={"q": "old turn"})
        ).encode()

    task = asyncio.create_task(
        client.post(
            _HOST_URL,
            content=delayed_body(),
            headers={
                **(_auth() if credential_kind == "attempt" else _residency_auth()),
                "Content-Type": "application/json",
            },
        )
    )
    await asyncio.wait_for(reading.wait(), 2)
    store.live_turns = {("ajob_next", 9, 1)}
    store.residency_turn = ("ajob_next", 9, 1)
    proceed.set()
    response = await asyncio.wait_for(task, 3)
    assert response.status_code == 401
    assert host.opened == []
    assert host.called == []


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("tools/call", {"name": "docs__search", "arguments": {}}),
        ("resources/read", {"uri": "mcp-resource://8/repo://notes"}),
        ("prompts/get", {"name": "docs__summarize", "arguments": {}}),
    ],
)
async def test_residency_is_rechecked_after_opening_upstream(
    client, store, host, monkeypatch, method, params
):
    store.snapshots[8] = _snapshot(8, "search", resources=("repo://notes",), prompts=("summarize",))
    store.bindings[1]["resource_rules"] = {"repo://notes": "allow"}
    store.bindings[1]["prompt_rules"] = {"summarize": "allow"}
    opening = asyncio.Event()
    proceed = asyncio.Event()
    original = host.open

    async def held_open(*args, **kwargs):
        opening.set()
        await proceed.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(host, "open", held_open)
    task = asyncio.create_task(
        client.post(_HOST_URL, json=_rpc(method, **params), headers=_residency_auth())
    )
    await asyncio.wait_for(opening.wait(), 2)
    store.residency_turn = None
    proceed.set()
    response = await asyncio.wait_for(task, 3)
    assert "error" in response.json()
    assert host.called == host.read == host.prompts == []


async def test_revocation_after_claim_records_an_unsent_approval_as_failed(
    approval_client, waiting_store, upstream, monkeypatch
):
    original = upstream.open

    async def revoke_on_open(*args, **kwargs):
        waiting_store.residency_turn = None
        return await original(*args, **kwargs)

    monkeypatch.setattr(upstream, "open", revoke_on_open)
    task = asyncio.create_task(
        approval_client.post(
            _HOST_URL,
            json=_rpc("tools/call", name="github__get_issue", arguments={"id": 7}),
            headers=_residency_auth(),
        )
    )
    await asyncio.wait_for(waiting_store.invocation_created.wait(), 2)
    invocation = waiting_store.created_invocations[0]
    waiting_store.approved = {**invocation, "status": "approved"}
    response = await asyncio.wait_for(task, 3)
    assert "error" in response.json()
    assert upstream.called == []
    assert waiting_store.claimed == [invocation["id"]]
    assert waiting_store.finished == [
        {"invocation_id": invocation["id"], "dispatch_generation": 1, "status": "failed"}
    ]


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
async def test_connection_proxy_rechecks_after_target_resolution(
    client, store, monkeypatch, method
):
    async def revoke_during_dns(*args, **kwargs):
        store.residency_turn = None

    def unexpected_client(*args, **kwargs):
        pytest.fail("a revoked request constructed an upstream client")

    monkeypatch.setattr(mcp_relay, "guard_target", revoke_during_dns)
    monkeypatch.setattr(mcp_relay, "build_upstream_client", unexpected_client)
    response = await client.request(
        method,
        _HOST_URL + "/docs",
        json=_rpc("tools/call", name="search", arguments={}) if method == "POST" else None,
        headers=_residency_auth(),
    )
    assert response.status_code == 401


@pytest.mark.parametrize("revoke", [False, True])
async def test_web_search_keeps_the_residency_through_admission(client, store, monkeypatch, revoke):
    monkeypatch.setattr(mcp_relay.web_search, "configured", lambda: True)
    original = store.acquire_web_search
    called = []

    async def admission(**kwargs):
        result = await original(**kwargs)
        if revoke:
            store.residency_turn = None
        return result

    async def search(arguments):
        called.append(arguments)
        return {"query": arguments["query"], "organic": []}

    monkeypatch.setattr(store, "acquire_web_search", admission)
    monkeypatch.setattr(mcp_relay.web_search, "search", search)
    response = await client.post(
        _HOST_URL,
        json=_rpc("tools/call", name="web_search", arguments={"query": "package version"}),
        headers=_residency_auth(),
    )
    assert store.search_acquired[0]["residency_generation"] == _RESIDENCY
    assert store.search_released == ["search-call-1"]
    if revoke:
        assert called == []
        assert "error" in response.json()
    else:
        assert len(called) == 1
        assert "result" in response.json()
