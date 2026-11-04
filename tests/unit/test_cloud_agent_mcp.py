"""The sandbox's MCP gateway: admissions, naming, and serving stdio servers over HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from cloud_agent.mcp.protocol import presented_name
from cloud_agent_shared.sandbox_mcp import (
    ALIAS_PATTERN,
    SANDBOX_MCP_COMMANDS,
    SANDBOX_MCP_URL,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY = REPO_ROOT / "deploy" / "sandbox" / "cloud-agent-mcp"
DOCKERFILE = REPO_ROOT / "deploy" / "sandbox" / "Dockerfile.agent-sandbox"
TOKEN = "member-mcp-credential-" + "7" * 24


def _load_gateway():
    loader = SourceFileLoader("cloud_agent_mcp_gateway", str(GATEWAY))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


gateway = _load_gateway()


def _admission(*servers, token=TOKEN):
    return {"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "servers": list(servers)}


def _server(alias="files", *args, command="npx", env=None):
    return {"alias": alias, "command": command, "args": list(args), "env": env or {}}


# -- the image and the shared contract -------------------------------------------


def test_the_gateway_is_root_owned_image_code_run_as_its_own_user():
    dockerfile = DOCKERFILE.read_text()

    assert "COPY deploy/sandbox/cloud-agent-mcp /usr/local/bin/cloud-agent-mcp" in dockerfile
    assert "/usr/local/bin/cloud-agent-mcp \\" in dockerfile.split("chmod 0555", 1)[1]
    assert (
        "useradd --uid 10003 --gid 10001 --create-home --shell /usr/sbin/nologin agent-mcp"
        in dockerfile
    )
    assert "install -d -m 0700 -o 10003 -g 10001 /run/agent-mcp" in dockerfile
    assert "CLOUD_AGENT_MCP_STATE_DIR=/run/agent-mcp" in dockerfile
    assert 'org.freeinference.helper.cloud-agent-mcp.version="1"' in dockerfile
    # The build proves it refuses an unknown token on its fixed address.
    assert "cloud-agent-mcp service start" in dockerfile


def test_the_gateway_runs_isolated_with_the_standard_library_only():
    source = GATEWAY.read_text()

    assert source.startswith("#!/usr/bin/python3 -I\n")
    imported = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    }
    assert imported <= set(sys.stdlib_module_names)


def test_the_gateway_and_the_platform_agree_on_its_contract():
    assert gateway.ENDPOINT == SANDBOX_MCP_URL
    assert frozenset(SANDBOX_MCP_COMMANDS) == gateway.ALLOWED_COMMANDS
    assert gateway._ALIAS.pattern == ALIAS_PATTERN


@pytest.mark.parametrize(
    ("alias", "tool"),
    [
        ("playwright", "browser_navigate"),
        ("mcp12", "browser_click"),
        ("mcp", "tool"),
        ("a-b", "do.it"),
        ("x" * 40, "y" * 40),
        ("dev", ""),
    ],
)
def test_tools_are_named_as_the_relay_names_them(alias, tool):
    assert gateway.presented_name(alias, tool) == presented_name(alias, tool)


# -- admissions -------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        {"token": TOKEN, "servers": []},  # the credential itself is never accepted
        {"token_sha256": "ABC", "servers": []},
        _admission(_server(command="bash")),
        _admission(_server(command="/usr/local/bin/npx")),
        _admission(_server(alias="a__b")),
        _admission(_server(alias="-x")),
        _admission(_server(env={"A=B": "x"})),
        _admission(_server(), _server()),
        _admission(*[_server(f"s{n}") for n in range(17)]),
        {**_admission(), "extra": 1},
    ],
)
def test_an_admission_outside_the_contract_is_refused_whole(document):
    with pytest.raises(gateway.GatewayError):
        gateway.parse_admission(0, document)


def test_a_slot_out_of_range_is_refused():
    with pytest.raises(gateway.GatewayError):
        gateway.parse_admission(128, _admission())


def test_only_the_digest_of_the_credential_is_kept_and_it_selects_the_member(tmp_path):
    paths = gateway.Paths(str(tmp_path / "state"))
    gateway.ensure_private_dir(paths.state)
    gateway.ensure_private_dir(paths.admissions)
    gateway.write_private(paths.admission(1), json.dumps(_admission(_server("one"))))
    gateway.write_private(
        paths.admission(2), json.dumps(_admission(_server("two"), token="other-" + "8" * 30))
    )

    stored = Path(paths.admission(1)).read_text()
    assert TOKEN not in stored
    assert oct(os.stat(paths.admission(1)).st_mode & 0o777) == "0o600"
    admissions = gateway.Admissions(paths)
    assert admissions.for_token(TOKEN).slot == 1
    assert admissions.for_token("other-" + "8" * 30).slot == 2
    assert admissions.for_token("nobody") is None
    assert admissions.for_token("") is None


def test_no_servers_removes_the_admission(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "service_start", lambda paths, idle_s: {})
    paths = gateway.Paths(str(tmp_path / "state"))

    gateway.admit(paths, 3, _admission(_server()), idle_s=60)
    assert os.path.exists(paths.admission(3))
    gateway.admit(paths, 3, _admission(), idle_s=60)
    assert not os.path.exists(paths.admission(3))


def test_the_gateway_is_started_before_an_admission_is_written(tmp_path, monkeypatch):
    """A gateway that starts forgets the admissions before it, never the new one."""
    order = []
    paths = gateway.Paths(str(tmp_path / "state"))
    monkeypatch.setattr(
        gateway, "service_start", lambda paths, idle_s: order.append(os.listdir(paths.admissions))
    )
    gateway.ensure_private_dir(paths.state)
    gateway.ensure_private_dir(paths.admissions)

    gateway.admit(paths, 0, _admission(_server()), idle_s=60)

    assert order == [[]]
    assert os.listdir(paths.admissions) == ["0.json"]


# -- serving ----------------------------------------------------------------------

FAKE_SERVER = r"""
import json, os, sys
# A stdio MCP server: newline-delimited JSON-RPC. Asks the client for roots
# once, which the gateway must refuse without failing.
asked = False
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if "id" not in message or method is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": os.environ.get("FAKE_NAME", "fake"), "version": "1"}}
        if not asked:
            asked = True
            print(json.dumps({"jsonrpc": "2.0", "id": "r1", "method": "roots/list"}), flush=True)
    elif method == "tools/list":
        if os.environ.get("FAKE_BROKEN"):
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                              "error": {"code": -32000, "message": "broken"}}), flush=True)
            continue
        result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}},
                            {"name": "where", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        name = message["params"]["name"]
        if name == "echo":
            text = json.dumps({"args": sys.argv[1:], "arguments": message["params"]["arguments"],
                               "env": os.environ.get("FAKE_VALUE", "")})
        else:
            text = os.getcwd()
        result = {"content": [{"type": "text", "text": text}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                          "error": {"code": -32601, "message": "no"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A gateway whose ``npx`` is FAKE_SERVER, and a way to send it requests."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ("npx", "uvx"):
        fake = bin_dir / command
        fake.write_text(f"#!{sys.executable}\n{FAKE_SERVER}")
        fake.chmod(0o755)
    monkeypatch.setattr(gateway, "SANDBOX_PATH", str(bin_dir))
    monkeypatch.setattr(gateway, "browser_endpoint", lambda: "ws://127.0.0.1:1/token/devtools")
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = gateway.Paths(str(tmp_path / "state"))
    gateway.ensure_private_dir(paths.state)
    gateway.ensure_private_dir(paths.admissions)
    return paths


async def _rpc(gw, method, params=None, *, token=TOKEN, host="127.0.0.1:47801", verb="POST"):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    request = gateway.HttpRequest(
        verb,
        "/mcp",
        {"host": host, "authorization": f"Bearer {token}"},
        body.encode(),
    )
    response = await gw.handle(request)
    return response, json.loads(response.body) if response.body else None


def _admit(paths, slot, *servers, token=TOKEN):
    gateway.write_private(paths.admission(slot), json.dumps(_admission(*servers, token=token)))


def test_requests_need_the_members_token_on_the_loopback_address(served):
    _admit(served, 0, _server())

    async def scenario():
        gw = gateway.Gateway(served, idle_s=60)
        assert (await _rpc(gw, "ping", token="wrong"))[0].status == "401 Unauthorized"
        assert (await _rpc(gw, "ping", host="evil.example:47801"))[0].status == "403 Forbidden"
        response, _ = await _rpc(gw, "ping", verb="GET")
        assert response.status == "405 Method Not Allowed"
        response, reply = await _rpc(gw, "initialize", {"protocolVersion": "2025-03-26"})
        assert response.status == "200 OK" and "Mcp-Session-Id" in response.headers
        assert reply["result"]["protocolVersion"] == "2025-03-26"
        assert reply["result"]["capabilities"] == {"tools": {"listChanged": False}}

    asyncio.run(scenario())


def test_a_members_servers_are_merged_named_and_routed(served):
    _admit(
        served,
        0,
        _server("files", "-y", "pkg@1.0.0", "--cdp", "${CLOUD_AGENT_BROWSER_CDP}"),
        _server("other", "pkg@2.0.0", env={"FAKE_VALUE": "plain"}),
    )
    _admit(served, 1, _server("someone"), token="other-member-" + "9" * 30)

    async def scenario():
        gw = gateway.Gateway(served, idle_s=60)
        try:
            _, reply = await _rpc(gw, "tools/list")
            names = sorted(tool["name"] for tool in reply["result"]["tools"])
            assert names == ["files__echo", "files__where", "other__echo", "other__where"]

            _, reply = await _rpc(gw, "tools/call", {"name": "files__echo", "arguments": {"a": 1}})
            echoed = json.loads(reply["result"]["content"][0]["text"])
            # The browser placeholder is filled in; the arguments reach the server.
            assert echoed["args"] == ["-y", "pkg@1.0.0", "--cdp", "ws://127.0.0.1:1/token/devtools"]
            assert echoed["arguments"] == {"a": 1}

            _, reply = await _rpc(gw, "tools/call", {"name": "other__echo", "arguments": {}})
            assert json.loads(reply["result"]["content"][0]["text"])["env"] == "plain"

            _, reply = await _rpc(gw, "tools/call", {"name": "someone__echo", "arguments": {}})
            assert reply["error"]["code"] == -32602  # another member's server
            _, reply = await _rpc(gw, "resources/list")
            assert reply["error"]["code"] == -32601
            # One process per member and server.
            assert {key[:2] for key in gw.backends} == {(0, "files"), (0, "other")}
        finally:
            for backend in gw.backends.values():
                backend.stop()

    asyncio.run(scenario())


def test_one_broken_server_keeps_the_others_tools(served):
    _admit(served, 0, _server("good", "pkg@1.0.0"), _server("bad", env={"FAKE_BROKEN": "1"}))

    async def scenario():
        gw = gateway.Gateway(served, idle_s=60)
        try:
            _, reply = await _rpc(gw, "tools/list")
            assert sorted(tool["name"] for tool in reply["result"]["tools"]) == [
                "good__echo",
                "good__where",
            ]
        finally:
            for backend in gw.backends.values():
                backend.stop()

    asyncio.run(scenario())


def test_servers_are_warmed_stopped_when_idle_and_dropped_when_revoked(served):
    _admit(served, 0, _server("files"))

    async def scenario():
        gw = gateway.Gateway(served, idle_s=0.2)
        gw.warm()
        for _ in range(100):
            if any(backend.tools for backend in gw.backends.values()):
                break
            await asyncio.sleep(0.05)
        [backend] = gw.backends.values()
        assert backend.alive and backend.tools

        await asyncio.sleep(0.3)
        gw.retire()
        assert not backend.alive  # idle: stopped, still admitted
        _, reply = await _rpc(gw, "tools/call", {"name": "files__where", "arguments": {}})
        assert "result" in reply and backend.alive  # the next call starts it again

        gateway.revoke(served, 0)
        gw.retire()
        assert not backend.alive and not gw.backends
        _, reply = await _rpc(gw, "tools/list")
        assert reply is None or reply.get("error") or reply["result"]["tools"] == []

    asyncio.run(scenario())


def test_a_server_that_cannot_start_reports_an_error_not_a_hang(served):
    _admit(served, 0, _server("missing", command="uvx"))
    os.unlink(Path(gateway.SANDBOX_PATH) / "uvx")

    async def scenario():
        gw = gateway.Gateway(served, idle_s=60)
        _, reply = await _rpc(gw, "tools/call", {"name": "missing__echo", "arguments": {}})
        assert reply["error"]["code"] in (-32602, -32603)
        _, reply = await _rpc(gw, "tools/list")
        assert reply["result"]["tools"] == []

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_a_new_gateway_forgets_the_admissions_before_it(served, monkeypatch):
    _admit(served, 0, _server())
    started = []

    class Popen:
        def __init__(self, argv, **kwargs):
            started.append(argv)
            os.write(kwargs["pass_fds"][0], b"error stop here\n")

    monkeypatch.setattr(gateway.subprocess, "Popen", Popen)
    with pytest.raises(gateway.GatewayError, match="stop here"):
        gateway._spawn(served, idle_s=60)
    assert started and os.listdir(served.admissions) == []
