"""The sandbox's MCP gateway: admissions, slot runners, and serving stdio servers over HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from cloud_agent.mcp.protocol import presented_name
from cloud_agent_host.chat_sandbox_models import MCP_RUNNER_UID_BASE, MCP_SERVICE_UID
from cloud_agent_shared.sandbox_mcp import (
    ALIAS_PATTERN,
    SANDBOX_MCP_COMMANDS,
    SANDBOX_MCP_URL,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY = REPO_ROOT / "deploy" / "sandbox" / "cloud-agent-mcp"
DOCKERFILE = REPO_ROOT / "deploy" / "sandbox" / "Dockerfile.agent-sandbox"
TOKEN = "member-mcp-credential-" + "7" * 24
OTHER = "other-member-credential-" + "9" * 24


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


def test_the_gateway_and_each_slots_runner_have_their_own_users():
    dockerfile = DOCKERFILE.read_text()

    assert "COPY deploy/sandbox/cloud-agent-mcp /usr/local/bin/cloud-agent-mcp" in dockerfile
    assert "/usr/local/bin/cloud-agent-mcp \\" in dockerfile.split("chmod 0555", 1)[1]
    assert "useradd --uid 10003 --gid 10001 --create-home --shell /usr/sbin/nologin agent-mcp" in (
        dockerfile
    )
    assert "uid=$((12001 + slot))" in dockerfile
    assert '--home-dir "/home/agent-mcp-$slot" --shell /usr/sbin/nologin "agent-mcp-$slot"' in (
        dockerfile
    )
    assert 'install -d -m 0710 -o "$uid" -g 10001 "/run/agent-mcp-slots/$slot"' in dockerfile
    # Admissions are root's: no server, runner or gateway can change one.
    assert "install -d -m 0755 -o 0 -g 0 /run/agent-mcp-admissions" in dockerfile
    for name in ("STATE_DIR=/run/agent-mcp", "ADMISSIONS_DIR=/run/agent-mcp-admissions"):
        assert f"CLOUD_AGENT_MCP_{name}" in dockerfile
    assert "CLOUD_AGENT_MCP_SLOTS_DIR=/run/agent-mcp-slots" in dockerfile
    assert 'org.freeinference.helper.cloud-agent-mcp.version="1"' in dockerfile
    assert "cloud-agent-mcp runner start 0" in dockerfile


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
    assert gateway.GATEWAY_UID == MCP_SERVICE_UID
    assert gateway.RUNNER_UID_BASE == MCP_RUNNER_UID_BASE
    assert gateway.ADMISSION_OWNER_UID == 0


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


@pytest.fixture
def paths(monkeypatch):
    """Short paths (a unix socket's must fit in 108 bytes), owned by this test's user."""
    root = tempfile.mkdtemp(prefix="cam-")
    resolved = gateway.Paths(
        state=os.path.join(root, "state"),
        admissions=os.path.join(root, "adm"),
        slots=os.path.join(root, "slots"),
    )
    os.makedirs(resolved.admissions, mode=0o755)
    os.makedirs(resolved.slots, mode=0o755)
    for slot in (0, 1):
        os.makedirs(resolved.slot_dir(slot), mode=0o710)
    # This test's user plays root (the admission owner) and the gateway.
    monkeypatch.setattr(gateway, "ADMISSION_OWNER_UID", os.getuid())
    monkeypatch.setattr(gateway, "GATEWAY_UID", os.getuid())
    yield resolved
    shutil.rmtree(root, ignore_errors=True)


def _admit(paths, slot, *servers, token=TOKEN):
    gateway.admit(paths, slot, _admission(*servers, token=token))


def test_only_the_digest_is_kept_and_it_selects_the_slot(paths):
    _admit(paths, 0, _server("one"))
    _admit(paths, 1, _server("two"), token=OTHER)

    stored = Path(paths.admission(0)).read_text()
    assert TOKEN not in stored
    assert oct(os.stat(paths.admission(0)).st_mode & 0o777) == "0o644"
    admissions = gateway.Admissions(paths)
    assert admissions.for_token(TOKEN).slot == 0
    assert admissions.for_token(OTHER).slot == 1
    assert admissions.for_token("nobody") is None
    assert admissions.for_token("") is None


def test_an_admission_is_written_only_as_its_owner(paths, monkeypatch):
    monkeypatch.setattr(gateway, "ADMISSION_OWNER_UID", os.getuid() + 1)
    with pytest.raises(gateway.GatewayError, match="written as uid"):
        gateway.admit(paths, 0, _admission(_server()))
    assert not os.path.exists(paths.admission(0))


def test_an_admission_anyone_else_could_have_written_is_ignored(paths, monkeypatch):
    """Codex's P1: a server rewrote another slot's admission. Only root's file counts."""
    _admit(paths, 0, _server("approved"))
    assert gateway.Admissions(paths).get(0) is not None

    monkeypatch.setattr(gateway, "ADMISSION_OWNER_UID", os.getuid() + 1)
    assert gateway.Admissions(paths).get(0) is None  # not root's file
    monkeypatch.setattr(gateway, "ADMISSION_OWNER_UID", os.getuid())
    os.chmod(paths.admission(0), 0o666)
    assert gateway.Admissions(paths).get(0) is None  # writable by others


def test_an_admission_from_before_the_container_started_is_ignored(paths, monkeypatch):
    _admit(paths, 0, _server())
    monkeypatch.setattr(gateway, "container_started_at", lambda: time.time() + 60)
    assert gateway.Admissions(paths).get(0) is None


def test_no_servers_removes_the_admission(paths):
    _admit(paths, 3, _server())
    assert os.path.exists(paths.admission(3))
    gateway.admit(paths, 3, _admission())
    assert not os.path.exists(paths.admission(3))
    gateway.admit(paths, 3, _admission())  # idempotent


def test_each_process_starts_only_as_its_own_user(paths, monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_UID", os.getuid() + 1)
    with pytest.raises(gateway.GatewayError, match="runs as uid"):
        gateway.service_start(paths)
    with pytest.raises(gateway.GatewayError, match="runs as uid"):
        gateway.runner_start(paths, 0, idle_s=60)


# -- a slot's runner ----------------------------------------------------------------

FAKE_SERVER = r"""
import json, os, sys, time
# A stdio MCP server: newline-delimited JSON-RPC. Asks the client for roots
# once, which the runner must refuse without failing.
asked = False
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if "id" not in message or method is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "1"}}
        if not asked:
            asked = True
            print(json.dumps({"jsonrpc": "2.0", "id": "r1", "method": "roots/list"}), flush=True)
    elif method == "tools/list":
        if os.environ.get("FAKE_BROKEN"):
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                              "error": {"code": -32000, "message": "broken"}}), flush=True)
            continue
        result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}},
                            {"name": "slow", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        name = message["params"]["name"]
        if name == "slow":
            time.sleep(float(message["params"]["arguments"].get("s", 1)))
        text = json.dumps({"args": sys.argv[1:], "arguments": message["params"]["arguments"],
                           "env": os.environ.get("FAKE_VALUE", ""), "tool": name})
        result = {"content": [{"type": "text", "text": text}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                          "error": {"code": -32601, "message": "no"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.fixture
def served(paths, tmp_path, monkeypatch):
    """Paths whose ``npx`` and ``uvx`` are FAKE_SERVER."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ("npx", "uvx"):
        fake = bin_dir / command
        fake.write_text(f"#!{sys.executable}\n{FAKE_SERVER}")
        fake.chmod(0o755)
    monkeypatch.setattr(gateway, "SANDBOX_PATH", str(bin_dir))
    monkeypatch.setattr(gateway, "browser_endpoint", lambda: "ws://127.0.0.1:1/token/devtools")
    monkeypatch.setenv("HOME", str(tmp_path))
    return paths


def _text(result):
    return json.loads(result["content"][0]["text"])


def _stop_all(runner):
    for backend in runner.backends.values():
        backend.stop()


def test_a_runner_merges_names_and_routes_its_slots_servers(served):
    _admit(
        served,
        0,
        _server("files", "-y", "pkg@1.0.0", "--cdp", "${CLOUD_AGENT_BROWSER_CDP}"),
        _server("other", "pkg@2.0.0", env={"FAKE_VALUE": "plain"}),
    )

    async def scenario():
        runner = gateway.Runner(served, 0, idle_s=60)
        try:
            names = sorted(tool["name"] for tool in await runner.list_tools())
            assert names == ["files__echo", "files__slow", "other__echo", "other__slow"]
            echoed = _text(await runner.call_tool("files__echo", {"a": 1}))
            # The browser placeholder is filled in; the arguments reach the server.
            assert echoed["args"] == ["-y", "pkg@1.0.0", "--cdp", "ws://127.0.0.1:1/token/devtools"]
            assert echoed["arguments"] == {"a": 1}
            assert _text(await runner.call_tool("other__echo", {}))["env"] == "plain"
            with pytest.raises(gateway.RemoteError):
                await runner.call_tool("nobody__echo", {})
        finally:
            _stop_all(runner)

    asyncio.run(scenario())


def test_a_broken_server_listed_first_cannot_answer_for_a_good_one(served):
    """Codex's P2: the call walked servers in order, and the broken one answered."""
    _admit(served, 0, _server("bad", env={"FAKE_BROKEN": "1"}), _server("good", "pkg@1.0.0"))

    async def scenario():
        runner = gateway.Runner(served, 0, idle_s=60)
        try:
            assert sorted(tool["name"] for tool in await runner.list_tools()) == [
                "good__echo",
                "good__slow",
            ]
            assert _text(await runner.call_tool("good__echo", {}))["tool"] == "echo"
            # Even without a listing first, the call finds its own server.
            fresh = gateway.Runner(served, 0, idle_s=60)
            try:
                assert _text(await fresh.call_tool("good__echo", {}))["tool"] == "echo"
            finally:
                _stop_all(fresh)
        finally:
            _stop_all(runner)

    asyncio.run(scenario())


def test_a_running_call_is_never_retired_as_idle(served):
    """Codex's P2: a 25-minute call was killed after the 15-minute idle timeout."""
    _admit(served, 0, _server("files"))

    async def scenario():
        runner = gateway.Runner(served, 0, idle_s=0.2)
        try:
            await runner.list_tools()
            call = asyncio.create_task(runner.call_tool("files__slow", {"s": 1.5}))
            for _ in range(6):
                await asyncio.sleep(0.25)
                runner.retire()  # well past idle_s, with the call still running
            assert _text(await call)["tool"] == "slow"
            [backend] = runner.backends.values()
            assert backend.alive and backend.active == 0
            await asyncio.sleep(0.3)
            runner.retire()
            assert not backend.alive  # idle now, and stopped
        finally:
            _stop_all(runner)

    asyncio.run(scenario())


def test_servers_are_warmed_and_dropped_when_withdrawn(served):
    _admit(served, 0, _server("files"))

    async def scenario():
        runner = gateway.Runner(served, 0, idle_s=60)
        runner.warm()
        for _ in range(100):
            if any(backend.tools for backend in runner.backends.values()):
                break
            await asyncio.sleep(0.05)
        [backend] = runner.backends.values()
        assert backend.alive and backend.tools

        gateway.admit(served, 0, _admission())
        runner.retire()
        assert not backend.alive and not runner.backends
        assert await runner.list_tools() == []

    asyncio.run(scenario())


def test_a_server_that_cannot_start_reports_an_error_not_a_hang(served):
    _admit(served, 0, _server("missing", command="uvx"))
    os.unlink(Path(gateway.SANDBOX_PATH) / "uvx")

    async def scenario():
        runner = gateway.Runner(served, 0, idle_s=60)
        assert await runner.list_tools() == []
        with pytest.raises(gateway.RemoteError):
            await runner.call_tool("missing__echo", {})

    asyncio.run(asyncio.wait_for(scenario(), 30))


# -- the gateway, through real sockets ------------------------------------------------


async def _rpc(gw, method, params=None, *, token=TOKEN, host="127.0.0.1:47801", verb="POST"):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    request = gateway.HttpRequest(
        verb, "/mcp", {"host": host, "authorization": f"Bearer {token}"}, body.encode()
    )
    response = await gw.handle(request)
    return response, json.loads(response.body) if response.body else None


@contextlib.asynccontextmanager
async def _runners(paths, monkeypatch, *slots):
    """Each slot's runner listening, as if this user were that slot's runner user."""
    tasks = []
    for slot in slots:
        runner = gateway.Runner(paths, slot, idle_s=60)
        tasks.append((runner, asyncio.create_task(runner.run(None))))
    for _ in range(100):
        if all(os.path.exists(paths.runner_socket(slot)) for slot in slots):
            break
        await asyncio.sleep(0.02)
    # The gateway checks a socket is its slot's runner's; here that is us.
    monkeypatch.setattr(gateway, "RUNNER_UID_BASE", os.getuid() - slots[0])
    try:
        yield
    finally:
        for runner, task in tasks:
            runner.stopping.set()
            await asyncio.wait_for(task, 10)


def test_requests_need_a_members_token_on_the_loopback_address(served):
    _admit(served, 0, _server())

    async def scenario():
        gw = gateway.Gateway(served)
        assert (await _rpc(gw, "ping", token="wrong"))[0].status == "401 Unauthorized"
        assert (await _rpc(gw, "ping", host="evil.example:47801"))[0].status == "403 Forbidden"
        assert (await _rpc(gw, "ping", verb="GET"))[0].status == "405 Method Not Allowed"
        response, reply = await _rpc(gw, "initialize", {"protocolVersion": "2025-03-26"})
        assert response.status == "200 OK" and "Mcp-Session-Id" in response.headers
        assert reply["result"]["protocolVersion"] == "2025-03-26"
        assert reply["result"]["capabilities"] == {"tools": {"listChanged": False}}

    asyncio.run(scenario())


def test_the_gateway_reaches_only_the_tokens_slot(served, monkeypatch):
    _admit(served, 0, _server("files", "pkg@1.0.0"))
    _admit(served, 1, _server("theirs", "pkg@2.0.0"), token=OTHER)
    # Both runners are this test's user; only slot 0's socket is checked as owned.
    monkeypatch.setattr(gateway, "RUNNER_UID_BASE", os.getuid())

    async def scenario():
        async with _runners(served, monkeypatch, 0):
            gw = gateway.Gateway(served)
            _, reply = await _rpc(gw, "tools/list")
            assert sorted(tool["name"] for tool in reply["result"]["tools"]) == [
                "files__echo",
                "files__slow",
            ]
            _, reply = await _rpc(gw, "tools/call", {"name": "files__echo", "arguments": {"x": 1}})
            assert _text(reply["result"])["arguments"] == {"x": 1}
            _, reply = await _rpc(gw, "tools/call", {"name": "theirs__echo", "arguments": {}})
            assert reply["error"]["code"] == -32602  # another member's server
            # Slot 1's token: its runner is not running, so nothing is served.
            _, reply = await _rpc(gw, "tools/list", token=OTHER)
            assert "not running" in reply["error"]["message"]
            _, reply = await _rpc(gw, "resources/list")
            assert reply["error"]["code"] == -32601

    asyncio.run(scenario())


def test_a_runner_answers_only_the_gateway(served, monkeypatch):
    _admit(served, 0, _server("files"))

    async def scenario():
        async with _runners(served, monkeypatch, 0):
            monkeypatch.setattr(gateway, "GATEWAY_UID", os.getuid() + 1)
            with pytest.raises(gateway.RemoteError, match="closed without answering"):
                await gateway.ask_runner(served, 0, {"op": "list"}, 10)

    asyncio.run(scenario())


def test_the_gateway_trusts_only_the_slots_own_runner_socket(served, monkeypatch):
    _admit(served, 0, _server("files"))

    async def scenario():
        async with _runners(served, monkeypatch, 0):
            monkeypatch.setattr(gateway, "RUNNER_UID_BASE", os.getuid() + 1)
            with pytest.raises(gateway.RemoteError, match="not its runner's"):
                await gateway.ask_runner(served, 0, {"op": "list"}, 10)

    asyncio.run(scenario())
