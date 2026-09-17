"""Concrete resource reads retain their connection, template and approval scope."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import mcp_relay
from cloud_agent.api.deps import get_storage
from cloud_agent.mcp.host import arguments_digest, decode_arguments
from cloud_agent.mcp.registry import invocation_cipher
from tests.servers.test_mcp_relay_host import (
    FakeStore,
    StubHost,
    _auth,
    _binding,
    _rpc,
    _secrets as _secrets,
    _snapshot,
)

pytestmark = pytest.mark.asyncio
_TEMPLATE = "repo://files/{path}"


@pytest.fixture
def store():
    snapshots = {}
    for connection_id in (7, 8):
        snapshot = _snapshot(connection_id, resources=("repo://notes",))
        snapshot["resource_templates"] = [
            {"name": _TEMPLATE, "input_schema": {"mimeType": "text/plain"}}
        ]
        snapshots[connection_id] = snapshot
    return FakeStore(
        bindings=[
            _binding(index, alias, resource_rules={"repo://notes": "allow", _TEMPLATE: "allow"})
            for index, alias in ((7, "github"), (8, "docs"))
        ],
        snapshots=snapshots,
    )


@pytest.fixture
async def client(store, monkeypatch):
    app = FastAPI()
    app.include_router(mcp_relay.router)
    app.dependency_overrides[get_storage] = lambda: store
    host = StubHost()
    monkeypatch.setattr(mcp_relay, "_HOST_INSTANCE", host)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        yield api, host


async def _read(client, uri):
    api, _ = client
    return await api.post("/v1/agent/mcp", json=_rpc("resources/read", uri=uri), headers=_auth())


async def test_duplicate_upstream_uris_remain_separately_addressable(client, store):
    api, host = client
    listed = await api.post("/v1/agent/mcp", json=_rpc("resources/list"), headers=_auth())
    uris = {item["uri"] for item in listed.json()["result"]["resources"]}
    assert uris == {"mcp-resource://7/repo://notes", "mcp-resource://8/repo://notes"}
    store.bindings.reverse()
    for connection_id in (7, 8):
        uri = f"mcp-resource://{connection_id}/repo://notes"
        response = await _read(client, uri)
        assert response.json()["result"]["contents"][0]["uri"] == uri
        assert host.opened[-1]["connection"].connection_id == str(connection_id)
        assert host.read[-1]["uri"] == "repo://notes"
    store.bindings = [row for row in store.bindings if row["connection_id"] == 8]
    response = await _read(client, "mcp-resource://7/repo://notes")
    assert "error" in response.json()
    assert len(host.read) == 2


async def test_listed_template_can_be_expanded_and_read_verbatim(client):
    api, host = client
    listed = await api.post("/v1/agent/mcp", json=_rpc("resources/templates/list"), headers=_auth())
    templates = listed.json()["result"]["resourceTemplates"]
    assert len(templates) == 2
    assert templates[0]["mimeType"] == "text/plain"
    uri = templates[0]["uriTemplate"].replace("{path}", "a%2Fb%252F")
    response = await _read(client, uri)
    assert response.json()["result"]["contents"][0]["uri"] == uri
    assert host.read == [{"uri": "repo://files/a%2Fb%252F"}]


@pytest.mark.parametrize(
    "uri",
    [
        None,
        7,
        "",
        "repo://notes",
        "mcp-resource://99/repo://notes",
        "mcp-resource://7/repo://files/a/b",
        "mcp-resource://7/repo://missing",
    ],
)
async def test_invalid_or_unbound_resources_never_reach_upstream(client, uri):
    response = await _read(client, uri)
    assert "error" in response.json()
    assert client[1].opened == []


@pytest.mark.parametrize(
    "change", ["deny", "unselected", "disabled", "exact-deny", "template-deny", "ambiguous"]
)
async def test_resource_scopes_are_enforced_before_templates_dispatch(client, store, change):
    rules = store.bindings[0]["resource_rules"]
    if change == "deny":
        rules[_TEMPLATE] = "deny"
    elif change == "unselected":
        del rules[_TEMPLATE]
    elif change == "disabled":
        store.bindings[0]["enabled"] = False
    elif change == "exact-deny":
        store.snapshots[7]["resources"].append({"name": "repo://files/secret"})
        rules["repo://files/secret"] = "deny"
    else:
        other = "repo://files/{+path}"
        store.snapshots[7]["resource_templates"].append({"name": other})
        rules[other] = "deny" if change == "template-deny" else "allow"
    response = await _read(client, "mcp-resource://7/repo://files/secret")
    assert "error" in response.json()
    assert client[1].opened == []


async def test_approval_describes_and_resumes_only_the_concrete_uri(client, store):
    store.bindings[0]["resource_rules"][_TEMPLATE] = "confirm"
    store.wait_action = "pending"
    uri = "repo://files/a%2Fb"
    task = asyncio.create_task(_read(client, "mcp-resource://7/" + uri))
    await asyncio.wait_for(store.invocation_created.wait(), 2)
    invocation = store.created_invocations[0]
    assert client[1].read == []
    assert invocation["capability_kind"] == "resource_template"
    assert invocation["capability_name"] == _TEMPLATE
    args = {"uri": uri, "method": "resources/read"}
    assert invocation["arguments_digest"] == arguments_digest(args)
    assert decode_arguments(invocation_cipher().decrypt(invocation["arguments_ciphertext"])) == args
    store.approved = {**invocation, "status": "approved"}
    response = await asyncio.wait_for(task, 3)
    assert "result" in response.json()
    assert client[1].read == [{"uri": uri}]
    assert store.finished[-1]["status"] == "completed"
    store.approved = None
    store.wait_action = "expired"
    await _read(client, "mcp-resource://7/repo://files/other")
    assert store.created_invocations[1]["arguments_digest"] != invocation["arguments_digest"]
    assert len(client[1].read) == 1


@pytest.mark.parametrize("change", ["policy", "lease", "deny", "cancel"])
async def test_pending_resource_approval_cannot_outlive_authority(client, store, change):
    store.bindings[0]["resource_rules"][_TEMPLATE] = "confirm"
    store.wait_action = "pending"
    task = asyncio.create_task(_read(client, "mcp-resource://7/repo://files/secret"))
    await asyncio.wait_for(store.invocation_created.wait(), 2)
    if change == "policy":
        store.bindings[0]["policy_revision"] += 1
    elif change == "lease":
        store.live = False
    elif change == "deny":
        store.wait_action = "denied"
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client[1].read == []
        return
    response = await asyncio.wait_for(task, 3)
    assert "error" in response.json()
    assert client[1].read == []


async def test_direct_connection_route_lists_and_reads_templates(client, store, monkeypatch):
    import json

    from fastapi.responses import JSONResponse

    from cloud_agent.mcp_proxy import filter_json_body

    forwarded = []

    async def proxy(request, *, server, job_id, method, body=b""):
        payload = json.loads(body)
        forwarded.append(payload)
        if payload["method"] == "resources/templates/list":
            result = {
                "resourceTemplates": [
                    {"uriTemplate": _TEMPLATE},
                    {"uriTemplate": "secret://{path}"},
                ]
            }
        else:
            result = {"contents": [{"uri": payload["params"]["uri"], "text": "body"}]}
        return JSONResponse(
            json.loads(
                filter_json_body(
                    json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode(), server
                )
            )
        )

    monkeypatch.setattr(mcp_relay, "_proxy", proxy)
    api, _ = client
    listed = await api.post(
        "/v1/agent/mcp/github", json=_rpc("resources/templates/list"), headers=_auth()
    )
    assert listed.json()["result"]["resourceTemplates"] == [{"uriTemplate": _TEMPLATE}]
    response = await api.post(
        "/v1/agent/mcp/github",
        json=_rpc("resources/read", uri="repo://files/a%2Fb"),
        headers=_auth(),
    )
    assert response.json()["result"]["contents"][0]["uri"] == "repo://files/a%2Fb"
    assert forwarded[-1]["params"] == {"uri": "repo://files/a%2Fb"}


async def test_resource_operation_is_part_of_the_approval_identity(client, store, monkeypatch):
    store.bindings[0]["resource_rules"][_TEMPLATE] = "confirm"
    looked_up = []

    async def approved(**kwargs):
        looked_up.append(kwargs)
        return None

    monkeypatch.setattr(store, "find_approved_mcp_invocation", approved)
    api, _ = client
    for method in ("resources/read", "resources/subscribe"):
        response = await api.post(
            "/v1/agent/mcp/github", json=_rpc(method, uri="repo://files/one"), headers=_auth()
        )
        assert "error" in response.json()
    assert len(looked_up) == 2
    assert looked_up[0]["capability_name"] == looked_up[1]["capability_name"] == _TEMPLATE
    assert looked_up[0]["arguments_digest"] != looked_up[1]["arguments_digest"]


async def test_indeterminate_denied_template_cannot_fall_through_to_an_allow_rule(
    client, store, monkeypatch
):
    from cloud_agent.mcp import resource_template_values

    denied = "repo://files/{a}{b}{a}{b}"
    store.snapshots[7]["resource_templates"].append({"name": denied})
    store.bindings[0]["resource_rules"][denied] = "deny"
    monkeypatch.setattr(resource_template_values, "MAX_MATCH_STEPS", 10)
    response = await _read(client, "mcp-resource://7/repo://files/different")
    assert "too ambiguous" in response.json()["error"]["message"]
    assert client[1].opened == []


async def test_repeated_variable_denial_cannot_fall_through_to_a_broader_template(client, store):
    allowed = "repo://files/{+path}"
    denied = "repo://files/{+part}/{+part}"
    store.snapshots[7]["resource_templates"] = [{"name": allowed}, {"name": denied}]
    store.bindings[0]["resource_rules"] = {allowed: "allow", denied: "deny"}
    response = await _read(client, "mcp-resource://7/repo://files/a/b/a/b")
    assert "error" in response.json()
    assert client[1].opened == []


async def test_unselected_ambiguous_template_does_not_break_an_allowed_read(
    client, store, monkeypatch
):
    from cloud_agent.mcp import resource_template_values

    store.snapshots[7]["resource_templates"].append({"name": "repo://files/{a}{b}{a}{b}"})
    monkeypatch.setattr(resource_template_values, "MAX_MATCH_STEPS", 10)
    response = await _read(client, "mcp-resource://7/repo://files/different")
    assert "result" in response.json()
    assert client[1].read == [{"uri": "repo://files/different"}]
