"""Carry GitHub-shaped results through the relay into the pinned Claude CLI.

Set MCP_TEST_CLAUDE_BINARY to the sandbox's Claude Code version. Only persistence,
the upstream server and inference are synthetic; the relay, protocol transport,
host normalization and native client's schema validation run unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import subprocess

import pytest
import uvicorn
from fastapi import FastAPI
from tests.external.test_agent_mcp_end_to_end import _decode, _free_port, _reply_json
from tests.external.test_resident_mcp_parity import Gateway
from tests.servers.test_mcp_relay_host import FakeStore, _binding, _connection, _snapshot, _token

from cloud_agent.api import mcp_relay
from cloud_agent.api.deps import get_storage
from cloud_agent.mcp import runtime as mcp_runtime
from cloud_agent.mcp.host import McpHost
from cloud_agent.mcp.net_policy import policy_from_environment
from cloud_agent_host.runtimes import ClaudeCodeRuntime, RuntimeMCPConfig

pytestmark = [pytest.mark.external, pytest.mark.asyncio]
_ROOT = pathlib.Path(__file__).resolve().parents[2]


class ResultGateway(Gateway):
    """Return a supplied upstream result without normalizing it in the fixture."""

    def __init__(self, transcript, *, tool_name, result):
        super().__init__(transcript, tool_name=tool_name)
        self.result = result

    def _on_mcp(self, handler, body):
        request = _decode(body)
        if request.get("method") != "tools/call":
            super()._on_mcp(handler, body)
            return
        self.upstream_authorizations.append(handler.headers.get("Authorization", ""))
        self.mcp_calls.append(request)
        _reply_json(
            handler, 200, {"jsonrpc": "2.0", "id": request.get("id"), "result": self.result}
        )


@pytest.mark.parametrize(
    "tool,value,with_text,policy",
    [
        ("list_branches", [{"name": "dev"}], True, "allow"),
        ("get_file_contents", [{"name": "README.md"}], True, "allow"),
        ("create_pull_request", {"number": 7, "draft": True}, False, "confirm"),
        ("pull_request_read", [{"name": "CI", "conclusion": "success"}], False, "allow"),
        ("list_pull_requests", [], False, "allow"),
    ],
)
@pytest.mark.timeout(420)
async def test_claude_receives_github_results_after_relay_and_owner_approval(
    tmp_path, monkeypatch, tool, value, with_text, policy
):
    binary = os.environ.get("MCP_TEST_CLAUDE_BINARY")
    if not binary:
        pytest.skip("set MCP_TEST_CLAUDE_BINARY to the sandbox's pinned Claude CLI")
    pin = re.search(
        r"^ARG CLAUDE_CODE_VERSION=(\S+)$",
        (_ROOT / "deploy/sandbox/Dockerfile.agent-sandbox").read_text(),
        re.MULTILINE,
    )
    assert pin
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True)
    assert version.stdout.startswith(pin.group(1) + " "), version.stdout

    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "mcp-host-secret-value-32-bytes-xx")
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "mcp-host-test-encryption-key")
    monkeypatch.setenv("AGENT_MCP_ALLOW_PRIVATE_TARGETS", "1")
    monkeypatch.setenv("AGENT_MCP_ALLOW_PLAIN_HTTP", "1")
    monkeypatch.setattr(mcp_relay.web_search, "configured", lambda: False)
    actual_host = McpHost(policy=policy_from_environment())
    monkeypatch.setattr(mcp_runtime, "_HOST_INSTANCE", actual_host)
    result = {"structuredContent": value}
    if with_text:
        result["content"] = [{"type": "text", "text": json.dumps(value)}]
    upstream = ResultGateway(tmp_path / "model.jsonl", tool_name=tool, result=result)
    mcp_port, model_port = upstream.start()
    relay = None
    relay_task = None
    approver = None
    approval_delay = float(os.environ.get("MCP_TEST_APPROVAL_DELAY", "0.2"))
    try:
        store = FakeStore(
            connections={7: _connection(7, "GitHub", url=f"http://127.0.0.1:{mcp_port}/mcp")},
            bindings=[_binding(7, "github", rules={tool: {"policy": policy, "fingerprint": ""}})],
            snapshots={7: _snapshot(7, tool)},
        )
        store.wait_action = "pending"
        app = FastAPI()
        app.include_router(mcp_relay.router)
        app.dependency_overrides[get_storage] = lambda: store
        relay_port = _free_port()
        relay = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=relay_port, log_level="warning")
        )
        relay_task = asyncio.create_task(relay.serve())
        while not relay.started:
            if relay_task.done():
                await relay_task
                raise RuntimeError("relay stopped before startup")
            await asyncio.sleep(0.02)

        if policy == "confirm":

            async def approve():
                await asyncio.wait_for(store.invocation_created.wait(), timeout=30)
                await asyncio.sleep(approval_delay)
                assert not [call for call in upstream.mcp_calls if call["method"] == "tools/call"]
                store.approved = {**store.created_invocations[0], "status": "approved"}

            approver = asyncio.create_task(approve())

        runtime = ClaudeCodeRuntime()
        runtime.binary = binary
        argv, env = runtime.prepare(
            workdir=str(tmp_path),
            task_prompt="Call the supplied GitHub MCP tool once, then report its result.",
            model="test-model",
            gateway_base_url=f"http://127.0.0.1:{model_port}",
            credential="synthetic-model-key",
            config_dir=str(tmp_path / "claude-config"),
            mcp_config=RuntimeMCPConfig(
                server_ids=("github",),
                base_url=f"http://127.0.0.1:{relay_port}",
                credential=_token(),
            ),
        )
        # Use print mode with the production launch options. No account tokens
        # or developer settings are inherited into the synthetic model request.
        env = {**env, "PATH": os.environ.get("PATH", ""), "ENABLE_TOOL_SEARCH": "false"}
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=tmp_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=approval_delay + 60
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, stderr.decode()
        events = [json.loads(line) for line in stdout.decode().splitlines() if line.startswith("{")]
        tool_results = [
            block
            for event in events
            if event.get("type") == "user"
            for block in event.get("message", {}).get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert tool_results, stdout.decode()
        assert not any(block.get("is_error") for block in tool_results), tool_results
        rendered = json.dumps(tool_results)
        assert "failed schema validation" not in rendered
        assert upstream.tool_results, "the model did not receive the native tool result"
        calls = [call for call in upstream.mcp_calls if call["method"] == "tools/call"]
        assert len(calls) == 1
        assert calls[0]["params"]["name"] == tool
        assert "Bearer GitHub-secret" in upstream.upstream_authorizations
        expected = value if isinstance(value, dict) else {"result": value}
        assert any(
            json.loads(block["content"]) == expected
            for block in tool_results
            if isinstance(block.get("content"), str)
        ), tool_results
        if approver:
            await approver
            assert len(store.claimed) == 1
            assert store.finished[0]["status"] == "completed"
    finally:
        if approver and not approver.done():
            approver.cancel()
            await asyncio.gather(approver, return_exceptions=True)
        if relay:
            relay.should_exit = True
        if relay_task:
            await asyncio.wait_for(relay_task, timeout=10)
        await actual_host.aclose()
        await asyncio.to_thread(upstream.stop)
