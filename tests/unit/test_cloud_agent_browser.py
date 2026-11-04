"""The sandbox's managed browser launcher and the contract the host hands it."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import stat
import subprocess
import sys
from dataclasses import replace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.support.owners import launch_connection

from cloud_agent_host.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.chat_sandbox_models import MEMBER_TMPFS_ROOT
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransportFactory
from cloud_agent_host.sandbox_browser import member_browser_env

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "deploy" / "sandbox" / "cloud-agent-browser"
DOCKERFILE = REPO_ROOT / "deploy" / "sandbox" / "Dockerfile.agent-sandbox"


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
    # The build proves the browser works as the unprivileged user, after USER.
    user_line = dockerfile.index("USER agent")
    assert dockerfile.index("cloud-agent-browser self-test") > user_line
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


# -- the host's half of the contract -------------------------------------------


def test_member_browser_state_is_private_and_profiles_are_per_chat():
    env = member_browser_env(3, "athr_parent")
    fork = member_browser_env(3, "athr_fork")

    assert env["CLOUD_AGENT_BROWSER_STATE_DIR"] == f"{MEMBER_TMPFS_ROOT}/3/browser"
    assert env["CLOUD_AGENT_BROWSER_PROFILE"].startswith(
        "/home/agent-member-3/.cloud-agent-browser/profiles/"
    )
    # Same slot, new chat: a Fork never starts on its parent's cookies.
    assert fork["CLOUD_AGENT_BROWSER_PROFILE"] != env["CLOUD_AGENT_BROWSER_PROFILE"]
    assert "athr_parent" not in env["CLOUD_AGENT_BROWSER_PROFILE"]
    with pytest.raises(ValueError):
        member_browser_env(-1, "athr_parent")
    with pytest.raises(ValueError):
        member_browser_env(0, "")


@pytest.mark.parametrize("runtime_id", ["claude-code", "codex", "pi", "opencode", "kilo"])
def test_every_runtime_starts_with_the_members_browser_environment(runtime_id):
    manager = SimpleNamespace(backend=SimpleNamespace(gid=10001, base_env=dict))
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
    )
    context = ResidentTransportFactory(manager)._launch_context(
        spec, launch, credential="inference-grant"
    )

    for key, value in member_browser_env(2, "athr_owner").items():
        assert context.base_env[key] == value


# -- paths ----------------------------------------------------------------------


def test_paths_default_under_home_and_follow_the_host(tmp_path):
    defaults = browser.resolve_paths({"HOME": str(tmp_path)})
    assert defaults.state == str(tmp_path / ".cloud-agent-browser" / "run")
    assert defaults.profile == str(tmp_path / ".cloud-agent-browser" / "profile")
    assert defaults.artifacts == "/workspace/.cloud-agent-artifacts"

    given = browser.resolve_paths(
        {
            "HOME": str(tmp_path),
            "CLOUD_AGENT_BROWSER_STATE_DIR": "/run/agent-members/1/browser",
            "CLOUD_AGENT_BROWSER_PROFILE": "/home/agent-member-1/p",
            "CLOUD_AGENT_ARTIFACTS_DIR": "/elsewhere",
        }
    )
    assert given.endpoint == "/run/agent-members/1/browser/endpoint"
    assert given.profile == "/home/agent-member-1/p"
    assert given.artifacts == "/elsewhere"


def test_private_directories_are_tightened_and_symlinks_refused(tmp_path):
    loose = tmp_path / "state"
    loose.mkdir(mode=0o755)
    browser.ensure_private_dir(str(loose))
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(browser.BrowserError):
        browser.ensure_private_dir(str(link))


def test_endpoint_files_are_written_atomically_and_owner_only(tmp_path):
    path = tmp_path / "endpoint"
    browser.write_private(str(path), "ws://127.0.0.1:1/token\n")
    browser.write_private(str(path), "ws://127.0.0.1:2/token\n")

    assert path.read_text() == "ws://127.0.0.1:2/token\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.name for entry in tmp_path.iterdir()] == ["endpoint"]


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


@pytest.mark.skipif(not Path(browser.CHROMIUM).exists(), reason="needs a local Chromium")
def test_self_test_against_a_real_chromium(tmp_path):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "CLOUD_AGENT_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
    }
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(HELPER), "self-test"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("self-test ok")
    finally:
        subprocess.run(
            [sys.executable, "-I", str(HELPER), "stop"], env=env, timeout=30, check=False
        )
