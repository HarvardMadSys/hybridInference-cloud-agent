"""The whole MCP chain, run against a real CLI, a real listener and a real database.

Every other test in this repository mocks one of the seams below, which is what
keeps them fast and deterministic -- and what makes this one worth its cost.
What it crosses, with nothing faked except the two *ends* (the user's MCP server
and the model, both of which are HTTP servers it starts itself):

    pi 0.73.1 -> the shipped bridge -> the relay listener on a real socket
      -> the fence, read from Postgres -> the protocol host
      -> the user's MCP server

The two ends are stubs because they are the parts a deployment does not own: the
platform's job is to carry a call between them correctly. Everything between is
the code that ships.

Three facts this can fail on that no per-layer test can:

- **The era probe against a real server.** A server that predates the
  per-request revision answers ``server/discover`` with the ordinary "no such
  method" code, and the host has to fall back to the handshake. Reasoning about
  that produced a bug once already.
- **Where the namespaced name stops.** The model sees
  ``<alias>__<tool>``; the upstream must be addressed by its own name and the
  model's arguments, untouched.
- **Which credential travels.** The upstream sees the connection's stored
  credential. The sandbox's job token must not appear there, and the connection's
  credential must not appear in what the model is told.

It needs Postgres, npm and the network to fetch the pinned CLI, so it skips
rather than fails wherever those are absent -- the external tier is run
deliberately, never as part of the default gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import asyncpg
import pytest
import uvicorn
from tests.support.migrations import apply_migrations

from cloud_agent.mcp.registry import mcp_cipher
from cloud_agent.settings import initialize_settings
from cloud_agent.storage.assembly import Storage
from cloud_agent.tokens import SCOPE_MCP, mint_worker_token

pytestmark = [pytest.mark.external, pytest.mark.asyncio]

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_OWNER = "e2e-owner"
_ALIAS = "github"
_TOOL = "get_issue"
_TOOL_TEXT = "issue 7: a bug"
_RESOURCE_TEXT = "embedded issue context"
_UPSTREAM_SECRET = "upstream-secret-token"
_MCP_TOKEN_SECRET = "e2e-signing-secret-32-bytes-long"
_ENCRYPTION_KEY = "e2e-encryption-key-at-least-32-bytes"
_MODEL_TOOL_CALL = f"session__{_ALIAS}__{_TOOL}"


def _pinned_pi_version() -> str:
    """The pi version the sandbox image ships, read from its own pin."""
    dockerfile = (_REPO_ROOT / "deploy" / "sandbox" / "Dockerfile.agent-sandbox").read_text()
    match = re.search(r"^ARG PI_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    assert match, "the sandbox Dockerfile no longer pins PI_VERSION"
    return match.group(1)


def _free_port() -> int:
    """One port nothing is listening on, for a server this test starts."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _db_config() -> dict[str, Any]:
    """The per-worker test database, refusing any name without ``_test_``."""
    base = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    name = base if worker == "master" else f"{base}_{worker}"
    if "_test_" not in name:
        pytest.fail(f"SAFETY: TEST_DB_NAME={name!r} is not a test database.")
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


async def _serve(app: Any, port: int, *, lifespan: str = "auto") -> uvicorn.Server:
    """Run one ASGI app on a real socket, in this test's event loop.

    ``lifespan`` is turned off for the two stubs. They are request handlers and
    nothing else, and uvicorn opens a lifespan channel with every application it
    starts -- which a handler that only knows how to read a request body reads
    as one, recording an empty call and never answering it.
    """
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan=lifespan)
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            raise RuntimeError("the test server exited before it started")
        await asyncio.sleep(0.02)
    return server


class _Recorder:
    """The two ends of the chain, as ordinary threaded HTTP servers.

    Deliberately not ASGI: these are stubs, and the only application that has to
    be the real one is the relay. Running them on ``http.server`` keeps them
    independent of the server library's application introspection, which decides
    how to call something by inspecting it and reads a handler written for a
    different interface as a request with an empty body.
    """

    def __init__(self, *, tool_name: str = _TOOL) -> None:
        """Start with empty transcripts."""
        self.tool_name = tool_name
        self.mcp_calls: list[dict[str, Any]] = []
        self.model_requests: list[dict[str, Any]] = []
        self.tool_results: list[str] = []
        self._servers: list[ThreadingHTTPServer] = []

    def start(self) -> tuple[int, int]:
        """Serve both ends on free ports and return ``(mcp, model)``."""
        self._servers = [
            ThreadingHTTPServer(("127.0.0.1", 0), _handler(self._on_mcp)),
            ThreadingHTTPServer(("127.0.0.1", 0), _handler(self._on_model)),
        ]
        for server in self._servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        return self._servers[0].server_port, self._servers[1].server_port

    def stop(self) -> None:
        """Stop both servers."""
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers = []

    def _on_mcp(self, handler: BaseHTTPRequestHandler, body: bytes) -> None:
        """Answer one JSON-RPC request the way a 2025-era server would."""
        request = _decode(body)
        self.mcp_calls.append({"method": request.get("method"), "params": request.get("params")})
        method = request.get("method")
        if method == "initialize":
            result: Any = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "e2e-fake", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": self.tool_name,
                        "description": "Fetch one issue",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"number": {"type": "integer"}},
                        },
                    }
                ]
            }
        elif method == "resources/read":
            result = {
                "contents": [
                    {
                        "uri": request["params"]["uri"],
                        "text": "template resource body",
                        "mimeType": "text/plain",
                    }
                ]
            }
        elif method == "tools/call":
            result = {
                "content": [
                    {"type": "text", "text": _TOOL_TEXT},
                    {
                        "type": "resource",
                        "resource": {
                            "uri": "issue://7",
                            "text": _RESOURCE_TEXT,
                            "mimeType": "text/plain",
                        },
                    },
                    {"type": "resource_link", "uri": "issue://7", "name": "Issue 7"},
                ]
            }
        else:
            # What a server that predates the stateless revision answers with,
            # and what makes the host fall back to the handshake.
            _reply_json(
                handler,
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {"code": -32601, "message": "method not found"},
                },
            )
            return
        _reply_json(handler, 200, {"jsonrpc": "2.0", "id": request.get("id"), "result": result})

    def _on_model(self, handler: BaseHTTPRequestHandler, body: bytes) -> None:
        """Ask for the MCP tool once, then answer in text, over SSE.

        Streamed because that is what the client reads: a single JSON body
        produces a turn that ends with no content and no tool call, which is
        indistinguishable from a model that chose to say nothing.
        """
        request = _decode(body)
        messages = request.get("messages") or []
        self.model_requests.append(
            {
                "tools": [
                    (tool.get("function") or {}).get("name") for tool in request.get("tools") or []
                ],
                "roles": [message.get("role") for message in messages],
            }
        )
        self.tool_results.extend(
            str(message.get("content")) for message in messages if message.get("role") == "tool"
        )
        answered = any(message.get("role") == "tool" for message in messages)
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.end_headers()

        def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
            payload = json.dumps(
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            ).encode()
            handler.wfile.write(b"data: " + payload + b"\n\n")
            handler.wfile.flush()

        if answered:
            chunk({"role": "assistant", "content": "done"})
            chunk({}, "stop")
        else:
            chunk(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": _MODEL_TOOL_CALL,
                                "arguments": json.dumps({"number": 7}),
                            },
                        }
                    ],
                }
            )
            chunk({}, "tool_calls")
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()


def _handler(callback: Any) -> Any:
    """Build a request handler that hands each POST body to ``callback``."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            callback(self, self.rfile.read(length))

        def log_message(self, *args: Any) -> None:
            """Silence the default per-request line; the transcripts are the record."""

    return _Handler


def _reply_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    """Answer one request with JSON."""
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _decode(raw: bytes) -> dict[str, Any]:
    """Parse a JSON-RPC body, treating anything unreadable as an empty request."""
    try:
        document = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


@pytest.fixture(scope="module")
def pi_install(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """The pinned pi, installed once for this module.

    Skipped rather than failed when npm or the registry is unavailable: this
    tier is run deliberately, and a missing network is not a defect in the code
    under test.
    """
    if shutil.which("npm") is None:
        pytest.skip("npm is not on PATH, so the pinned pi cannot be installed")
    root = tmp_path_factory.mktemp("pi-install")
    version = _pinned_pi_version()
    result = subprocess.run(
        [
            "npm",
            "install",
            "--no-audit",
            "--no-fund",
            f"@mariozechner/pi-coding-agent@{version}",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=900,
    )
    binary = root / "node_modules" / ".bin" / "pi"
    if result.returncode != 0 or not binary.exists():
        pytest.skip(f"could not install pi {version}: {result.stderr[-400:]}")
    return binary


async def test_a_tool_call_travels_the_whole_chain(pi_install: pathlib.Path) -> None:
    """One call from a real CLI, out to a user's server and back to the model."""
    db = _db_config()
    try:
        pool = await asyncpg.create_pool(**db, min_size=1, max_size=4)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    # The listener reads its configuration from the environment the way a deploy
    # does, so the values are set before the application is built.
    os.environ.update(
        {
            "AGENT_DATABASE_URL": (
                f"postgresql://{db['user']}:{db['password']}@{db['host']}:{db['port']}/{db['database']}"
            ),
            "AGENT_CONTROL_TOKEN_SECRET": _MCP_TOKEN_SECRET,
            "AGENT_SETTINGS_ENCRYPTION_KEY": "e2e-admin-settings-secret-32-bytes-long",
            "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY": _ENCRYPTION_KEY,
            "AGENT_MCP_ALLOW_PRIVATE_TARGETS": "1",
            "AGENT_MCP_ALLOW_PLAIN_HTTP": "1",
        }
    )

    upstream = _Recorder()
    upstream_port, model_port = upstream.start()
    relay_port = _free_port()
    servers: list[uvicorn.Server] = []
    try:
        apply_migrations(db)
        # Deployments initialize settings through the main listener first.
        # This isolated relay test uses that same seed path before startup.
        await initialize_settings(Storage(pool).settings)
        token = await _seed(pool, upstream_url=f"http://127.0.0.1:{upstream_port}/mcp")

        from cloud_agent.app import create_mcp_app

        servers.append(await _serve(create_mcp_app(), relay_port))

        # Off the event loop: the three servers this test started are tasks in
        # it, and a blocking ``subprocess.run`` here would leave them unable to
        # answer the CLI they are waiting on.
        result = await asyncio.to_thread(
            _run_pi,
            pi_install,
            upstream,
            model_port=model_port,
            relay_port=relay_port,
            token=token,
        )
    finally:
        for server in servers:
            server.should_exit = True
        upstream.stop()
        await asyncio.sleep(0.2)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_mcp_connections")
        await pool.close()

    # The model was offered exactly the merged catalogue, under the bridge's
    # own namespace, and nothing else.
    assert result["advertised"] == [_MODEL_TOOL_CALL], result
    # The call reached the user's server as its own tool, with the model's
    # arguments, carrying the connection's credential rather than the sandbox's.
    methods = [call["method"] for call in upstream.mcp_calls]
    assert "server/discover" in methods, (upstream.mcp_calls, result, upstream.tool_results)
    assert methods.index("initialize") < methods.index("tools/call"), upstream.mcp_calls
    call = next(entry for entry in upstream.mcp_calls if entry["method"] == "tools/call")
    assert call["params"] == {"name": _TOOL, "arguments": {"number": 7}}
    # And the answer came back to the model as the tool result.
    assert _TOOL_TEXT in " ".join(upstream.tool_results), upstream.tool_results


async def _seed(
    pool: Any,
    *,
    upstream_url: str,
    runtime: str = "pi",
    policy: str = "allow",
    tool_name: str = _TOOL,
) -> str:
    """One connection, one bound session, one live attempt, one MCP token."""
    store = Storage(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_runner_policy (id, active_host, runtime_versions) "
            "VALUES (TRUE, NULL, NULL) ON CONFLICT (id) DO UPDATE "
            "SET active_host = NULL, runtime_versions = NULL"
        )
    connection = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="GitHub",
        transport="streamable-http",
        location="remote",
        url=upstream_url,
        launch_spec=None,
        auth_kind="static",
        auth_config={"header_name": "Authorization", "scheme": "Bearer"},
        template="github",
    )
    await store.mcp.set_mcp_credential(
        connection_id=int(connection["id"]),
        expected_authorization_epoch=1,
        access_token_ciphertext=mcp_cipher().encrypt(_UPSTREAM_SECRET),
    )
    await store.mcp.save_mcp_capabilities(
        connection_id=int(connection["id"]),
        config_revision=1,
        protocol_version="2025-11-25",
        server_name="e2e-fake",
        server_version="1",
        tools=[
            {
                "name": tool_name,
                "description": "Fetch one issue",
                "input_schema": {"type": "object", "properties": {}},
                "annotations": {},
            }
        ],
        resources=[],
        resource_templates=[
            {
                "name": "repo://files/{path}",
                "description": "Repository files",
                "input_schema": {"mimeType": "text/plain"},
            },
            {
                "name": "repo://mirrors/{+path}/again/{+path}",
                "description": "Repeated repository paths",
                "input_schema": {"mimeType": "text/plain"},
            },
        ],
        prompts=[],
        support={},
    )
    job = await store.conversation_commands.create_job(
        user_id=_OWNER,
        repo="example-org/example-repo",
        task_prompt="show me issue 7",
        runtime=runtime,
        model="test-model",
        base_sha="deadbeef",
    )
    await store.mcp.replace_mcp_bindings(
        thread_id=str(job["thread_id"]),
        bindings=[
            {
                "connection_id": int(connection["id"]),
                "alias": _ALIAS,
                "enabled": True,
                "config_revision": 1,
                "authorization_epoch": 1,
                "capability_revision": 2,
                "policy_revision": 1,
                "rules": {tool_name: {"policy": policy, "fingerprint": ""}},
                "resource_rules": {
                    "repo://files/{path}": policy,
                    "repo://mirrors/{+path}/again/{+path}": policy,
                },
                "prompt_rules": {},
            }
        ],
    )
    async with pool.acquire() as conn:
        # A running attempt belongs to an enrolled host: the relay fence asks
        # that the machine holding the worker token still has a credential.
        await conn.execute(
            "INSERT INTO agent_runner_hosts (host, status, credential_hash) "
            "VALUES ('e2e-host', 'online', 'e2e-credential-hash') ON CONFLICT (host) DO UPDATE "
            "SET status = 'online', credential_hash = EXCLUDED.credential_hash"
        )
        attempt_id = await conn.fetchval(
            """
            INSERT INTO agent_attempts
                (job_id, thread_id, attempt_no, lease_owner, lease_generation,
                 lease_expires_at, status, host_id)
            VALUES ($1, $2, 1, 'e2e-worker', 3, NOW() + INTERVAL '1 hour', 'running', 'e2e-host')
            RETURNING id
            """,
            str(job["id"]),
            str(job["thread_id"]),
        )
        await conn.execute(
            "UPDATE agent_jobs SET state = 'running', current_attempt_id = $2 WHERE id = $1",
            str(job["id"]),
            attempt_id,
        )
    token = mint_worker_token(
        job_id=str(job["id"]), attempt_id=int(attempt_id), lease_generation=3, scope=SCOPE_MCP
    )
    return token


def _run_pi(
    pi: pathlib.Path,
    upstream: _Recorder,
    *,
    model_port: int,
    relay_port: int,
    token: str,
) -> dict[str, Any]:
    """Drive the pinned CLI at the merged endpoint and report what it saw."""
    home = pi.parent.parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "freeinference": {
                        "baseUrl": f"http://127.0.0.1:{model_port}/v1",
                        "api": "openai-completions",
                        "apiKey": "test-key",
                        "models": [{"id": "test-model", "name": "test-model"}],
                    }
                }
            }
        )
    )
    env = {
        **os.environ,
        "PI_CODING_AGENT_DIR": str(home),
        "AGENT_MCP_SERVERS": json.dumps({"session": f"http://127.0.0.1:{relay_port}/v1/agent/mcp"}),
        "AGENT_MCP_TOKEN": token,
    }
    completed = subprocess.run(
        [
            str(pi),
            "--provider",
            "freeinference",
            "--model",
            "test-model",
            "--mode",
            "text",
            "--no-extensions",
            "-e",
            str(_REPO_ROOT / "deploy" / "sandbox" / "pi-mcp-bridge.ts"),
            "--no-builtin-tools",
            "-p",
            "show me issue 7",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-800:]
    # Deduplicated: the catalogue is re-sent on the turn after a tool result, and
    # what matters is the set the model was ever offered.
    advertised = sorted(
        {name for request in upstream.model_requests for name in request["tools"] if name}
    )
    return {
        "advertised": advertised,
        "stdout": completed.stdout,
        "requests": upstream.model_requests,
    }
