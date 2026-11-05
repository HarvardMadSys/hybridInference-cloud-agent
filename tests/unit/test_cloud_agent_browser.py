"""The sandbox's shared browser: its launcher, its service and how the host starts it."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.support.owners import launch_connection

from cloud_agent_host.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.chat_sandbox_models import BROWSER_SERVICE_UID
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransportFactory
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.sandbox_browser import LAUNCHER, ensure_browser_service

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "deploy" / "sandbox" / "cloud-agent-browser"
DOCKERFILE = REPO_ROOT / "deploy" / "sandbox" / "Dockerfile.agent-sandbox"
BROWSER_STATE_DIR = "/run/agent-browser"


def _load_helper():
    loader = SourceFileLoader("cloud_agent_browser", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


browser = _load_helper()


# -- the image ----------------------------------------------------------------


def test_launcher_is_root_owned_read_only_image_code_checked_at_build():
    dockerfile = DOCKERFILE.read_text()

    assert (
        "COPY deploy/sandbox/cloud-agent-browser /usr/local/bin/cloud-agent-browser" in dockerfile
    )
    assert "/usr/local/bin/cloud-agent-browser \\" in dockerfile.split("chmod 0555", 1)[1]
    assert 'org.freeinference.helper.cloud-agent-browser.version="1"' in dockerfile
    assert "        chromium \\\n" in dockerfile
    assert "CLOUD_AGENT_BROWSER_CONTRACT=1" in dockerfile
    assert "CLOUD_AGENT_BROWSER_BIN=/usr/bin/chromium" in dockerfile
    assert f"CLOUD_AGENT_BROWSER_STATE_DIR={BROWSER_STATE_DIR}" in dockerfile
    assert "CLOUD_AGENT_BROWSER_PROFILE=/home/agent-browser/profile" in dockerfile
    # Its own user, in the agent group, and a state directory only it writes.
    assert (
        f"useradd --uid {BROWSER_SERVICE_UID} --gid 10001 --create-home "
        "--shell /usr/sbin/nologin agent-browser"
    ) in dockerfile
    assert f"install -d -m 0750 -o {BROWSER_SERVICE_UID} -g 10001 {BROWSER_STATE_DIR}" in dockerfile
    # The build proves the browser works as the unprivileged user, after USER.
    user_line = dockerfile.index("USER agent")
    started = dockerfile.index("cloud-agent-browser service start")
    assert user_line < started < dockerfile.index("cloud-agent-browser self-test")
    # A browser-download kill switch would change how repositories' own tests run.
    assert "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD" not in dockerfile
    assert "PUPPETEER_SKIP_DOWNLOAD" not in dockerfile


def test_helper_runs_isolated_and_with_the_standard_library_only():
    source = HELPER.read_text()

    assert source.startswith("#!/usr/bin/python3 -I\n")
    imported = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    }
    assert imported <= set(sys.stdlib_module_names)


def test_the_chromium_debug_port_is_never_opened_on_tcp():
    argv = browser.chromium_argv("/profile", sandboxed=True)

    assert "--remote-debugging-pipe" in argv
    assert not any(arg.startswith("--remote-debugging-port") for arg in argv)
    assert "--no-sandbox" not in argv
    assert "--no-sandbox" in browser.chromium_argv("/profile", sandboxed=False)
    assert "--user-data-dir=/profile" in argv


# -- how the host starts the service ---------------------------------------------


class _Manager:
    def __init__(self, outcome):
        self.backend = SimpleNamespace(gid=10001, base_env=dict)
        self.outcome = outcome
        self.calls = []

    def exec_capture(self, spec, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return subprocess.CompletedProcess(argv, self.outcome, b"", b"chromium broke")


def test_the_host_starts_the_service_as_its_own_user():
    manager = _Manager(0)

    ensure_browser_service(manager, SimpleNamespace(container_name="c"))

    [(argv, kwargs)] = manager.calls
    assert argv == [LAUNCHER, "service", "start"]
    assert (kwargs["user_uid"], kwargs["user_gid"]) == (BROWSER_SERVICE_UID, 10001)
    # Not a member UID: no member's turn-end reap reaches the browser.
    assert not 11001 <= BROWSER_SERVICE_UID < 11001 + 128


@pytest.mark.parametrize("outcome", [127, 126])
def test_an_image_without_the_browser_is_left_alone(outcome, caplog):
    with caplog.at_level(logging.WARNING):
        ensure_browser_service(_Manager(outcome), SimpleNamespace(container_name="c"))
    assert not caplog.records


@pytest.mark.parametrize("outcome", [1, SandboxError("exec failed")])
def test_a_service_that_cannot_start_never_blocks_the_member(outcome, caplog):
    with caplog.at_level(logging.WARNING):
        ensure_browser_service(_Manager(outcome), SimpleNamespace(container_name="c"))
    assert "did not start" in caplog.text


@pytest.mark.parametrize("runtime_id", ["claude-code", "codex", "pi", "opencode", "kilo"])
def test_every_runtime_launch_starts_the_service_first(runtime_id):
    events = []
    manager = SimpleNamespace(
        backend=SimpleNamespace(gid=10001, base_env=dict),
        exec_capture=lambda spec, argv, **kwargs: (
            events.append(("exec", argv[0], kwargs["user_uid"]))
            or subprocess.CompletedProcess(argv, 0, b"", b"")
        ),
        ensure_downloaded_runtime=lambda *args: events.append(("runtime",)),
        ensure_mcp_helpers=lambda *args: events.append(("helpers",)),
        ensure_kilo_mcp_runtime=lambda *args: events.append(("kilo",)),
        ensure_codex_mcp_policy=lambda *args: events.append(("codex",)),
    )
    spec = ChatSandboxSpec(
        thread_id="athr_owner",
        sandbox_id="asbx_group",
        generation=1,
        runtime_id=runtime_id,
        shared_live=True,
    )
    launch = replace(
        ResidentLaunch(
            thread_id="athr_owner",
            runtime_id=runtime_id,
            runtime_version=None,
            native_session_id=None,
            model="model",
            reasoning_effort=None,
            credential="inference-grant",
            **launch_connection(runtime_id),
        ),
        slot_id=2,
        inference_client_protocol=None,
    )
    factory = ResidentTransportFactory(manager)

    factory._prepare_runtime_helpers(spec, launch)
    context = factory._launch_context(spec, launch, credential="inference-grant")

    assert events[0] == ("exec", LAUNCHER, BROWSER_SERVICE_UID)
    # The image names the shared browser's paths; a member gets none of its own.
    assert not any(key.startswith("CLOUD_AGENT_BROWSER_") for key in context.base_env)


# -- paths ----------------------------------------------------------------------


def test_paths_default_under_home_and_follow_the_image(tmp_path):
    defaults = browser.resolve_paths({"HOME": str(tmp_path)})
    assert defaults.state == str(tmp_path / ".cloud-agent-browser" / "run")
    assert defaults.profile == str(tmp_path / ".cloud-agent-browser" / "profile")
    assert defaults.artifacts == "/workspace/.cloud-agent-artifacts"

    given = browser.resolve_paths(
        {
            "HOME": str(tmp_path),
            "CLOUD_AGENT_BROWSER_STATE_DIR": "/run/agent-browser",
            "CLOUD_AGENT_BROWSER_PROFILE": "/home/agent-browser/profile",
            "CLOUD_AGENT_ARTIFACTS_DIR": "/elsewhere",
        }
    )
    assert given.endpoint == "/run/agent-browser/endpoint"
    assert given.profile == "/home/agent-browser/profile"
    assert given.artifacts == "/elsewhere"


def test_service_directories_are_tightened_and_symlinks_refused(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o777)
    os.chmod(state, 0o777)
    browser.ensure_service_dir(str(state))
    assert stat.S_IMODE(state.stat().st_mode) == 0o750

    profile = tmp_path / "profile"
    profile.mkdir(mode=0o755)
    browser.ensure_private_dir(str(profile))
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(browser.BrowserError):
        browser.ensure_service_dir(str(link))


def test_endpoint_files_are_written_atomically_for_the_group_to_read(tmp_path):
    path = tmp_path / "endpoint"
    browser.write_shared(str(path), "ws://127.0.0.1:1/token\n")
    browser.write_shared(str(path), "ws://127.0.0.1:2/token\n")

    assert path.read_text() == "ws://127.0.0.1:2/token\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert [entry.name for entry in tmp_path.iterdir()] == ["endpoint"]


def test_a_member_trusts_only_what_the_service_user_alone_could_write(tmp_path):
    paths = browser.resolve_paths({"CLOUD_AGENT_BROWSER_STATE_DIR": str(tmp_path / "run")})
    assert browser.service_owner(paths) is None  # no service directory yet

    browser.ensure_service_dir(paths.state)
    owner = browser.service_owner(paths)
    assert owner == os.getuid()
    browser.write_shared(paths.record, '{"pid": 1}')
    assert browser.read_trusted(paths.record, owner) == '{"pid": 1}'
    assert browser.read_trusted(paths.record, owner + 1) is None

    os.chmod(paths.record, 0o660)  # another group member could have written it
    assert browser.read_trusted(paths.record, owner) is None
    os.chmod(paths.record, 0o640)
    os.symlink(paths.record, paths.endpoint)
    assert browser.read_trusted(paths.endpoint, owner) is None

    os.chmod(paths.state, 0o770)  # another group member could plant an endpoint
    assert browser.service_owner(paths) is None
    assert browser.read_record(paths) is None


def test_artifacts_are_kept_out_of_git_once(tmp_path):
    (tmp_path / ".git").mkdir()
    artifacts = tmp_path / ".cloud-agent-artifacts"

    browser.ensure_artifacts(str(artifacts))
    browser.ensure_artifacts(str(artifacts))

    assert artifacts.is_dir()
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert exclude.splitlines().count("/.cloud-agent-artifacts/") == 1


def test_artifacts_without_git_or_permission_never_fail_the_start(tmp_path):
    browser.ensure_artifacts(str(tmp_path / "plain"))
    assert (tmp_path / "plain").is_dir()
    browser.ensure_artifacts("/proc/cannot-create-here")


# -- WebSocket framing ------------------------------------------------------------


def test_handshake_accept_matches_rfc6455():
    assert browser.websocket_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


@pytest.mark.parametrize("size", [0, 5, 125, 126, 65535, 65536, 200_000])
def test_frames_round_trip_at_every_length_encoding(size):
    payload = bytes(range(256)) * (size // 256) + bytes(size % 256)

    async def run():
        masked = await browser.read_frame(
            _reader(browser.encode_frame(browser.OP_TEXT, payload, mask=True)),
            require_mask=True,
        )
        plain = await browser.read_frame(
            _reader(browser.encode_frame(browser.OP_BINARY, payload)), require_mask=False
        )
        return masked, plain

    masked, plain = asyncio.run(run())
    assert masked == (True, browser.OP_TEXT, payload)
    assert plain == (True, browser.OP_BINARY, payload)


def test_server_refuses_unmasked_client_frames_and_oversized_ones():
    async def run(frame):
        return await browser.read_frame(_reader(frame), require_mask=True)

    with pytest.raises(browser.FrameError, match="not masked"):
        asyncio.run(run(browser.encode_frame(browser.OP_TEXT, b"{}")))
    huge = bytes([0x81, 0xFF]) + (browser.MAX_MESSAGE_BYTES + 1).to_bytes(8, "big")
    with pytest.raises(browser.FrameError, match="too large"):
        asyncio.run(run(huge))


def test_fragmented_messages_are_reassembled_and_pings_answered():
    first = bytearray(browser.encode_frame(browser.OP_TEXT, b'{"id":', mask=True))
    first[0] &= 0x7F  # not final
    ping = browser.encode_frame(browser.OP_PING, b"hi", mask=True)
    last = browser.encode_frame(browser.OP_CONTINUATION, b"1}", mask=True)

    sent: list[bytes] = []
    writer = SimpleNamespace(write=sent.append, drain=_async_none, close=lambda: None)

    async def run():
        socket = browser.WebSocket(_reader(bytes(first) + ping + last), writer)
        return await socket.receive()

    assert asyncio.run(run()) == b'{"id":1}'
    assert sent == [browser.encode_frame(browser.OP_PONG, b"hi")]


async def _async_none():
    return None


# -- request routing ------------------------------------------------------------


def _daemon(tmp_path):
    daemon = browser.Daemon(
        browser.resolve_paths({"HOME": str(tmp_path)}), idle_s=60, ready_fd=None
    )
    daemon.port = 4242
    return daemon


def _upgrade(path, host="127.0.0.1:4242"):
    return browser.parse_request(
        (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            "Connection: keep-alive, Upgrade\r\nSec-WebSocket-Key: a2V5\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )


def test_only_the_tokened_path_on_a_loopback_host_upgrades(tmp_path):
    daemon = _daemon(tmp_path)
    token = daemon.token

    assert daemon._route(_upgrade(f"/{token}/devtools/browser")) == "socket"
    assert daemon._route(_upgrade(f"/{token}/devtools/browser", "localhost:4242")) == "socket"
    assert daemon.ws_url == f"ws://127.0.0.1:4242/{token}/devtools/browser"
    stop = browser.parse_request(
        f"GET /{token}/stop HTTP/1.1\r\nHost: 127.0.0.1:4242\r\n\r\n".encode()
    )
    assert daemon._route(stop) == "stop"
    stop_elsewhere = browser.parse_request(b"GET /stop HTTP/1.1\r\nHost: 127.0.0.1:4242\r\n\r\n")
    assert daemon._route(stop_elsewhere) == "none"
    for refused in (
        _upgrade("/devtools/browser"),
        _upgrade(f"/{token[:-1]}x/devtools/browser"),
        _upgrade(f"/{token}/json/version"),
        _upgrade(f"/{token}/devtools/browser", "evil.example:4242"),
        browser.parse_request(f"GET /{token}/devtools/browser HTTP/1.1\r\n\r\n".encode()),
        browser.parse_request(b"garbage\r\n\r\n"),
    ):
        assert daemon._route(refused) == "none"


# -- CDP multiplexing ---------------------------------------------------------------


class _Pipe:
    def __init__(self):
        self.browser: list[dict] = []
        self.clients: list[tuple[str, dict]] = []
        self.mux = browser.Multiplexer(
            self.browser.append, lambda client, message: self.clients.append((client.name, message))
        )

    def attach(self, name, session):
        client = browser.Client(name=name)
        self.mux.connect(client)
        request = self.browser.pop()
        assert request["method"] == "Target.attachToBrowserTarget"
        assert "sessionId" not in request
        self.mux.from_browser({"id": request["id"], "result": {"sessionId": session}})
        return client


def test_each_client_runs_in_its_own_browser_session_with_its_own_ids():
    pipe = _Pipe()
    a = pipe.attach("a", "ROOT-A")
    b = pipe.attach("b", "ROOT-B")

    pipe.mux.from_client(a, {"id": 1, "method": "Browser.getVersion"})
    pipe.mux.from_client(b, {"id": 1, "method": "Browser.getVersion"})
    to_a, to_b = pipe.browser
    assert to_a["sessionId"] == "ROOT-A" and to_b["sessionId"] == "ROOT-B"
    assert to_a["id"] != to_b["id"]

    pipe.mux.from_browser({"id": to_b["id"], "sessionId": "ROOT-B", "result": {"product": "x"}})
    assert pipe.clients == [("b", {"id": 1, "result": {"product": "x"}})]


def test_commands_sent_before_the_session_exists_are_held_then_sent():
    pipe = _Pipe()
    client = browser.Client(name="early")
    pipe.mux.connect(client)
    attach = pipe.browser.pop()
    pipe.mux.from_client(client, {"id": 7, "method": "Target.getTargets"})
    assert pipe.browser == []

    pipe.mux.from_browser({"id": attach["id"], "result": {"sessionId": "ROOT"}})
    assert pipe.browser[0]["method"] == "Target.getTargets"
    assert pipe.browser[0]["sessionId"] == "ROOT"


def test_child_sessions_belong_to_the_client_that_created_them():
    pipe = _Pipe()
    a = pipe.attach("a", "ROOT-A")
    b = pipe.attach("b", "ROOT-B")

    pipe.mux.from_client(
        a, {"id": 2, "method": "Target.attachToTarget", "params": {"flatten": True}}
    )
    sent = pipe.browser.pop()
    pipe.mux.from_browser(
        {"id": sent["id"], "sessionId": "ROOT-A", "result": {"sessionId": "PAGE"}}
    )
    # Auto-attach announces sessions as events on the owner's browser session.
    pipe.mux.from_browser(
        {
            "method": "Target.attachedToTarget",
            "sessionId": "ROOT-A",
            "params": {"sessionId": "WORKER", "targetInfo": {}},
        }
    )
    pipe.clients.clear()

    pipe.mux.from_client(a, {"id": 3, "method": "Runtime.evaluate", "sessionId": "PAGE"})
    pipe.mux.from_client(a, {"id": 4, "method": "Runtime.enable", "sessionId": "WORKER"})
    assert [message["sessionId"] for message in pipe.browser] == ["PAGE", "WORKER"]

    for foreign in ("PAGE", "WORKER", "ROOT-A", "ROOT-B"):
        pipe.mux.from_client(b, {"id": 9, "method": "Runtime.evaluate", "sessionId": foreign})
    assert len(pipe.browser) == 2
    assert all(
        name == "b" and message["error"]["code"] == browser.SESSION_NOT_FOUND
        for name, message in pipe.clients
    )

    pipe.clients.clear()
    pipe.mux.from_browser({"method": "Runtime.consoleAPICalled", "sessionId": "PAGE", "params": {}})
    pipe.mux.from_browser({"method": "Target.targetCreated", "sessionId": "ROOT-B", "params": {}})
    pipe.mux.from_browser({"method": "Target.targetCreated", "params": {}})
    assert pipe.clients == [
        ("a", {"method": "Runtime.consoleAPICalled", "sessionId": "PAGE", "params": {}}),
        ("b", {"method": "Target.targetCreated", "params": {}}),
    ]


def test_detached_sessions_stop_routing():
    pipe = _Pipe()
    a = pipe.attach("a", "ROOT-A")
    pipe.mux.from_browser(
        {"method": "Target.attachedToTarget", "sessionId": "ROOT-A", "params": {"sessionId": "S"}}
    )
    pipe.mux.from_browser(
        {"method": "Target.detachedFromTarget", "sessionId": "ROOT-A", "params": {"sessionId": "S"}}
    )
    pipe.clients.clear()

    pipe.mux.from_client(a, {"id": 1, "method": "Page.enable", "sessionId": "S"})
    assert pipe.clients[0][1]["error"]["code"] == browser.SESSION_NOT_FOUND


def test_browser_close_from_one_client_only_ends_that_client():
    pipe = _Pipe()
    a = pipe.attach("a", "ROOT-A")
    pipe.mux.from_client(a, {"id": 5, "method": "Browser.close"})

    assert pipe.browser == []
    assert pipe.clients == [("a", {"id": 5, "result": {}})]
    assert a.hang_up


def test_a_departing_client_gives_its_browser_session_back():
    pipe = _Pipe()
    a = pipe.attach("a", "ROOT-A")
    pipe.mux.from_client(a, {"id": 1, "method": "Target.getTargets"})
    in_flight = pipe.browser.pop()
    pipe.mux.disconnect(a)

    assert pipe.browser == [
        {
            "id": pipe.browser[0]["id"],
            "method": "Target.detachFromTarget",
            "params": {"sessionId": "ROOT-A"},
        }
    ]
    # A reply that arrives after the client left goes nowhere.
    pipe.mux.from_browser({"id": in_flight["id"], "sessionId": "ROOT-A", "result": {}})
    pipe.mux.from_browser({"method": "Target.targetCreated", "sessionId": "ROOT-A", "params": {}})
    assert pipe.clients == []


def test_a_client_that_leaves_before_its_session_exists_does_not_leak_it():
    pipe = _Pipe()
    client = browser.Client(name="gone")
    pipe.mux.connect(client)
    attach = pipe.browser.pop()
    pipe.mux.disconnect(client)

    pipe.mux.from_browser({"id": attach["id"], "result": {"sessionId": "ORPHAN"}})
    assert pipe.browser[-1]["method"] == "Target.detachFromTarget"
    assert pipe.browser[-1]["params"] == {"sessionId": "ORPHAN"}


def test_malformed_commands_end_the_connection():
    pipe = _Pipe()
    a = pipe.attach("a", "ROOT-A")
    with pytest.raises(browser.FrameError):
        pipe.mux.from_client(a, {"method": "Page.enable"})
    with pytest.raises(browser.FrameError):
        pipe.mux.from_client(a, {"id": "1", "method": "Page.enable"})


# -- end to end, where a Chromium is installed -------------------------------------


FAKE_CHROMIUM = r"""
import json, os, sys
# Answers CDP on fds 3/4 like Chromium's --remote-debugging-pipe, minimally.
if os.environ.get("FAKE_NEEDS_NO_SANDBOX") and "--no-sandbox" not in sys.argv:
    sys.stderr.write("No usable sandbox! (fake)\n")
    sys.exit(1)
sessions = 0
downloads = 0
behavior = ("", "")
buffer = b""

def send(message):
    os.write(4, json.dumps(message).encode() + b"\0")

while True:
    chunk = os.read(3, 65536)
    if not chunk:
        break
    buffer += chunk
    while b"\0" in buffer:
        raw, buffer = buffer.split(b"\0", 1)
        message = json.loads(raw)
        method = message.get("method")
        params = message.get("params") or {}
        session = message.get("sessionId")
        if method == "Target.attachToBrowserTarget":
            sessions += 1
            result = {"sessionId": f"S{sessions}"}
        elif method == "Browser.getVersion":
            result = {"product": "FakeChrome/1", "protocolVersion": "1.3"}
        elif method == "Test.big":
            result = {"data": "x" * params["size"]}
        elif method == "Browser.setDownloadBehavior":
            behavior = (params.get("behavior", ""), params.get("downloadPath", ""))
            result = {}
        elif method == "Test.download":
            downloads += 1
            guid = f"guid-{downloads}"
            name = guid if behavior[0] == "allowAndName" else "suggested.txt"
            with open(os.path.join(behavior[1], name), "w") as handle:
                handle.write(params["body"])
            def event(method, **fields):
                send({"method": method, "params": fields, "sessionId": session})
            event("Browser.downloadWillBegin", guid=guid, suggestedFilename="suggested.txt", url="x")
            for _ in range(params.get("events", 1)):
                event("Browser.downloadProgress", guid=guid, state="completed")
            result = {"guid": guid}
        else:
            result = {}
        reply = {"id": message["id"], "result": result}
        if session is not None:
            reply["sessionId"] = session
        send(reply)
"""


@pytest.fixture
def fake_paths(tmp_path, monkeypatch):
    """A service whose Chromium is FAKE_CHROMIUM, its state under a short path.

    A unix socket's path has to fit in 108 bytes, so the state directory is
    not under ``tmp_path``, which pytest nests deeply.
    """
    fake = tmp_path / "chromium"
    fake.write_text(f"#!{sys.executable}\n{FAKE_CHROMIUM}")
    fake.chmod(0o755)
    monkeypatch.setattr(browser, "CHROMIUM", str(fake))
    # In-process, the service has no "_serve" command line to recognise.
    monkeypatch.setattr(browser, "_is_service", lambda pid, owner: pid == os.getpid())
    state = tempfile.mkdtemp(prefix="cab-")
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = browser.resolve_paths(
        {
            "CLOUD_AGENT_BROWSER_STATE_DIR": os.path.join(state, "run"),
            "CLOUD_AGENT_BROWSER_PROFILE": str(tmp_path / "profile"),
            "HOME": str(tmp_path),
        }
    )
    browser.ensure_service_dir(paths.state)
    yield paths
    shutil.rmtree(state, ignore_errors=True)


@contextlib.asynccontextmanager
async def _running(paths, *, idle_s=60.0, sandboxed=False):
    daemon = browser.Daemon(paths, idle_s=idle_s, ready_fd=None)
    daemon.sandboxed = sandboxed
    service = asyncio.create_task(daemon.run())
    for _ in range(200):
        if browser.read_trusted(paths.endpoint, os.getuid()):
            break
        await asyncio.sleep(0.02)
    try:
        yield daemon, browser.read_trusted(paths.endpoint, os.getuid()).strip()
    finally:
        daemon._stop("test over")
        await asyncio.wait_for(service, 10)


def test_the_service_starts_chromium_per_use_for_every_member(fake_paths):
    paths = fake_paths

    async def scenario():
        daemon = browser.Daemon(paths, idle_s=1.0, ready_fd=None)
        daemon.sandboxed = False
        service = asyncio.create_task(daemon.run())
        for _ in range(100):
            if browser.read_trusted(paths.endpoint, os.getuid()):
                break
            await asyncio.sleep(0.02)
        url = browser.read_trusted(paths.endpoint, os.getuid()).strip()
        try:
            # Serving, but no Chromium until a tool connects.
            assert browser.status(paths)["service"] is True
            assert browser.status(paths)["running"] is False
            assert daemon.chromium is None

            # Two members' tools share one Chromium.
            first = await browser.CdpClient.connect(url)
            second = await browser.CdpClient.connect(url)
            assert (await first.call("Browser.getVersion"))["product"] == "FakeChrome/1"
            await second.call("Browser.getVersion")
            pid = daemon.chromium.pid
            status = browser.status(paths)
            assert status["running"] is True
            assert status["browser"] == "FakeChrome/1"

            # A member's stop closes it for everyone; the service stays.
            assert await asyncio.to_thread(browser.stop, paths) is True
            assert await asyncio.wait_for(first.socket.receive(), 5) is None
            assert browser.status(paths)["running"] is False
            assert await asyncio.to_thread(browser.stop, paths) is False

            # The next tool gets a fresh Chromium.
            third = await browser.CdpClient.connect(url)
            await third.call("Browser.getVersion")
            assert daemon.chromium.pid != pid

            # A crash ends its clients, and the next connection recovers.
            daemon.chromium.kill()
            assert await asyncio.wait_for(third.socket.receive(), 5) is None
            for _ in range(100):
                if not browser.status(paths)["running"]:
                    break
                await asyncio.sleep(0.02)
            assert browser.status(paths)["running"] is False
            fourth = await browser.CdpClient.connect(url)
            await fourth.call("Browser.getVersion")
            await fourth.socket.close()

            # With no client for the idle timeout, Chromium stops; the service does not.
            for _ in range(100):
                if daemon.chromium is None:
                    break
                await asyncio.sleep(0.05)
            assert daemon.chromium is None
            assert browser.status(paths)["service"] is True
        finally:
            daemon._stop("test over")
            await asyncio.wait_for(service, 10)
        assert browser.status(paths)["service"] is False
        # A member's stop, a crash, an idle stop: only the crash was nobody's
        # request, so only it is journalled for the host.
        journal = Path(paths.incidents).read_text().splitlines()
        assert [(entry["kind"], entry["signal"]) for entry in map(json.loads, journal)] == [
            ("browser_crashed", 9)
        ]
        assert os.stat(paths.incidents).st_mode & 0o777 == 0o640

    asyncio.run(scenario())


async def _stalled_client(url, request):
    """A client that sends one command and then never reads its socket.

    The small reader limit makes asyncio stop reading the socket once a
    little is buffered, so the kernel's buffers fill and the service's writes
    to it stall, as for a tool that paused.
    """
    rest = url.removeprefix("ws://")
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    reader, writer = await asyncio.open_connection(host, int(port), limit=16 * 1024)
    writer.write(
        (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: c3RhbGxlZA==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 101")
    writer.write(browser.encode_frame(browser.OP_TEXT, json.dumps(request).encode(), mask=True))
    await writer.drain()
    return reader, writer


def test_a_stalled_client_never_holds_up_another(fake_paths):
    """Codex's finding: one paused 8 MiB reply stalled every other client's replies."""

    async def scenario():
        async with _running(fake_paths) as (daemon, url):
            fast = await browser.CdpClient.connect(url)
            await fast.call("Browser.getVersion")
            big = {"id": 1, "method": "Test.big", "params": {"size": 32 * 1024 * 1024}}
            _reader, stalled = await _stalled_client(url, big)
            await asyncio.sleep(0.5)  # the 32 MiB reply is now stuck on its socket
            assert len(daemon.peers) == 2
            for _ in range(5):
                started = time.monotonic()
                reply = await asyncio.wait_for(fast.call("Browser.getVersion"), 5)
                assert reply["product"] == "FakeChrome/1"
                assert time.monotonic() - started < 2

            # The stalled client leaves: its session, channel and task go with it.
            stalled.transport.abort()
            for _ in range(100):
                if len(daemon.peers) == 1:
                    break
                await asyncio.sleep(0.05)
            assert len(daemon.peers) == 1
            assert len(daemon.mux.clients) == 1
            await fast.call("Browser.getVersion")
            await fast.socket.close()

    asyncio.run(scenario())


def test_a_client_too_far_behind_is_disconnected(fake_paths, monkeypatch):
    monkeypatch.setattr(browser, "MAX_CLIENT_BACKLOG", 8 * 1024 * 1024)

    async def scenario():
        async with _running(fake_paths) as (daemon, url):
            fast = await browser.CdpClient.connect(url)
            await fast.call("Browser.getVersion")
            big = {"id": 1, "method": "Test.big", "params": {"size": 6 * 1024 * 1024}}
            _reader, stalled = await _stalled_client(url, big)
            for request_id in (2, 3, 4):
                frame = json.dumps({**big, "id": request_id}).encode()
                stalled.write(browser.encode_frame(browser.OP_TEXT, frame, mask=True))
            await stalled.drain()
            for _ in range(100):
                if len(daemon.peers) == 1:
                    break
                await asyncio.sleep(0.05)
            assert len(daemon.peers) == 1, "a client 24 MiB behind an 8 MiB limit stayed"
            assert (await fast.call("Browser.getVersion"))["product"] == "FakeChrome/1"
            stalled.close()

    asyncio.run(scenario())


def test_chromium_that_cannot_use_its_sandbox_falls_back_once(fake_paths, monkeypatch, capfd):
    """CI's failure: the probe passed under AppArmor, and Chromium aborted sandboxed."""
    monkeypatch.setenv("FAKE_NEEDS_NO_SANDBOX", "1")

    async def scenario():
        async with _running(fake_paths, sandboxed=True) as (daemon, url):
            client = await browser.CdpClient.connect(url)
            assert (await client.call("Browser.getVersion"))["product"] == "FakeChrome/1"
            assert browser.status(fake_paths)["chromium_sandbox"] is False
            assert "--no-sandbox" in daemon.chromium.args
            await client.socket.close()

    asyncio.run(scenario())
    # Chromium's own words in the service log; the service's on its stderr.
    assert "No usable sandbox! (fake)" in Path(fake_paths.log).read_text()
    assert "starting it with --no-sandbox" in capfd.readouterr().err


def test_the_probe_asks_for_a_user_mapping_not_just_a_namespace(monkeypatch):
    seen = []

    def run(argv, **_kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(browser.subprocess, "run", run)
    assert browser.probe_user_namespaces() is False
    assert seen == [["unshare", "--user", "--map-root-user", "true"]]


async def _download(client, target, *, behavior="allowAndName", events=1):
    await client.call(
        "Browser.setDownloadBehavior",
        {"behavior": behavior, "downloadPath": str(target), "eventsEnabled": True},
    )
    guid = (await client.call("Test.download", {"body": "payload", "events": events}))["guid"]
    progress = [
        await client.wait_event(
            "Browser.downloadProgress", lambda params: params.get("guid") == guid, timeout=15
        )
        for _ in range(events)
    ]
    return guid, progress


def test_a_download_reaches_a_directory_chromium_cannot_write(fake_paths, monkeypatch, tmp_path):
    """Codex's finding: a member's 0700 download directory is not the browser's to write."""
    monkeypatch.setattr(browser, "browser_can_write", lambda path: False)
    target = tmp_path / "member-downloads"
    target.mkdir(mode=0o700)

    async def scenario():
        async with _running(fake_paths) as (daemon, url):
            courier = asyncio.create_task(asyncio.to_thread(browser.run_courier, fake_paths))
            client = await browser.CdpClient.connect(url)
            guid, [progress] = await _download(client, target)
            assert progress["state"] == "completed"
            assert (target / guid).read_text() == "payload"
            [route] = daemon.peers[next(iter(daemon.peers))].routes
            assert route.target == str(target) and not os.listdir(route.staging)

            # One download heard on two sessions: copied once, completed twice.
            guid, progress = await _download(client, target, events=2)
            assert [event["state"] for event in progress] == ["completed", "completed"]
            assert (target / guid).read_text() == "payload"

            # Puppeteer's "allow" names the file as the page suggested.
            _guid, [progress] = await _download(client, target, behavior="allow")
            assert progress["state"] == "completed"
            assert (target / "suggested.txt").read_text() == "payload"
            await client.socket.close()
        await asyncio.wait_for(courier, 10)  # it leaves with the service

    asyncio.run(scenario())


def test_a_download_with_no_courier_is_reported_canceled(fake_paths, monkeypatch, tmp_path, capfd):
    monkeypatch.setattr(browser, "browser_can_write", lambda path: False)
    monkeypatch.setattr(browser, "COURIER_WAIT_S", 0.2)
    target = tmp_path / "member-downloads"
    target.mkdir(mode=0o700)

    async def scenario():
        async with _running(fake_paths) as (daemon, url):
            client = await browser.CdpClient.connect(url)
            guid, [progress] = await _download(client, target)
            assert progress["state"] == "canceled"
            assert not (target / guid).exists()
            [route] = daemon.peers[next(iter(daemon.peers))].routes
            assert not os.listdir(route.staging)
            await client.socket.close()

    asyncio.run(scenario())
    assert "no courier for uid" in capfd.readouterr().err


def test_a_download_chromium_can_write_is_left_alone(fake_paths, tmp_path):
    target = tmp_path / "shared-downloads"
    target.mkdir()

    async def scenario():
        async with _running(fake_paths) as (daemon, url):
            client = await browser.CdpClient.connect(url)
            guid, [progress] = await _download(client, target)
            assert progress["state"] == "completed"
            assert (target / guid).read_text() == "payload"
            assert not daemon.peers[next(iter(daemon.peers))].routes
            await client.socket.close()

    asyncio.run(scenario())


def test_the_courier_copies_only_a_staged_file_into_a_new_name_of_its_own(tmp_path):
    paths = browser.resolve_paths({"CLOUD_AGENT_BROWSER_STATE_DIR": str(tmp_path / "run")})
    staging = Path(paths.downloads) / "client-1"
    staging.mkdir(parents=True)
    (staging / "file").write_text("staged")
    target = tmp_path / "target"
    target.mkdir()
    owner = os.getuid()

    browser.deliver(paths, owner, str(staging / "file"), str(target / "file"))
    assert (target / "file").read_text() == "staged"

    elsewhere = tmp_path / "secret"
    elsewhere.write_text("not a download")
    (staging / "link").symlink_to(elsewhere)
    refusals = [
        (str(elsewhere), str(target / "a")),  # not in the staging directory
        (str(staging / "link"), str(target / "b")),  # a symlink out of it
        (str(staging / "file"), str(target / "file")),  # an existing file
        (str(staging / "file"), str(target)),  # no file name
        (str(staging / "file"), "relative/c"),
    ]
    for src, dst in refusals:
        with pytest.raises((browser.BrowserError, OSError)):
            browser.deliver(paths, owner, src, dst)
    with pytest.raises(browser.BrowserError):
        browser.deliver(paths, owner + 1, str(staging / "file"), str(target / "d"))
    assert sorted(os.listdir(target)) == ["file"]


def test_a_clients_uid_comes_from_the_kernels_socket_table():
    async def scenario():
        accepted = asyncio.get_running_loop().create_future()

        async def handle(reader, writer):
            accepted.set_result(writer.get_extra_info("peername"))

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        peer = await asyncio.wait_for(accepted, 5)
        assert browser.tcp_peer_uid(port, peer[1]) == os.getuid()
        assert browser.tcp_peer_uid(port, 1) is None
        writer.close()
        server.close()

    asyncio.run(scenario())


@pytest.mark.skipif(not Path(browser.CHROMIUM).exists(), reason="needs a local Chromium")
def test_self_test_against_a_real_chromium(tmp_path):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "CLOUD_AGENT_BROWSER_STATE_DIR": str(tmp_path / "run"),
        "CLOUD_AGENT_BROWSER_PROFILE": str(tmp_path / "profile"),
        "CLOUD_AGENT_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
    }

    def helper(*args, timeout=120):
        return subprocess.run(
            [sys.executable, "-I", str(HELPER), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def log_tail():
        # What Chromium itself said: CI keeps none of tmp_path.
        log = tmp_path / "run" / "daemon.log"
        return log.read_text(errors="replace")[-6000:] if log.exists() else "(no daemon log)"

    try:
        started = helper("service", "start")
        assert started.returncode == 0, f"{started.stderr}\n--- daemon.log\n{log_tail()}"
        result = helper("self-test")
        assert result.returncode == 0, f"{result.stderr}\n--- daemon.log\n{log_tail()}"
        assert result.stdout.startswith("self-test ok")
    finally:
        helper("service", "stop", timeout=30)
