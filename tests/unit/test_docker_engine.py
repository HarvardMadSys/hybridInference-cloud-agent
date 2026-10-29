"""The Engine API answers a plain inspect exactly as the Docker CLI would.

A fake daemon serves the API on a unix socket. Each test drives the same code
a deployment does: :func:`run_docker` and the manager's ``_run``, whose callers
parse the answer, so a difference from the CLI's shape fails a real reader.
"""

from __future__ import annotations

import http.server
import json
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_agent_host.chat_sandbox import ChatSandboxManager
from cloud_agent_host.chat_sandbox_inspect import containers_by_ref, inspect_each, inspect_many
from cloud_agent_host.docker_engine import (
    API_VERSION,
    DEFAULT_SOCKET,
    DockerEngine,
    engine_from_env,
    run_docker,
)
from cloud_agent_host.sandbox import ContainerBackend, SandboxError, build_backend_from_env


class _Daemon(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _handler(state: SimpleNamespace) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append(self.path)
            time.sleep(state.delay)
            prefix = f"/v{API_VERSION}/"
            kind, _, rest = self.path.removeprefix(prefix).partition("/")
            ref = rest.removesuffix("/json")
            if state.status is not None:
                self._answer(state.status, {"message": "daemon trouble"})
            elif kind == "containers" and ref in state.containers:
                self._answer(200, state.containers[ref])
            elif kind == "containers":
                self._answer(404, {"message": f"No such container: {ref}"})
            elif kind == "networks" and ref in state.networks:
                self._answer(200, state.networks[ref])
            else:
                self._answer(404, {"message": f"network {ref} not found"})

        def _answer(self, status: int, body: object) -> None:
            data = state.raw if state.raw is not None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: object) -> None:
            return

    return Handler


@pytest.fixture
def daemon():
    # Short: a unix socket path may not exceed about 100 bytes.
    directory = tempfile.mkdtemp(prefix="engine")
    path = str(Path(directory, "docker.sock"))
    state = SimpleNamespace(
        containers={}, networks={}, requests=[], delay=0.0, status=None, raw=None
    )
    server = _Daemon(path, _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(path=path, state=state, engine=DockerEngine(path))
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(directory, ignore_errors=True)


def _container(container_id: str, name: str) -> dict[str, object]:
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "State": {"Status": "running", "Running": True, "StartedAt": "2026-10-28T00:00:00Z"},
        "Config": {"Image": "sha256:image", "Labels": {"owner": "test"}},
    }


def _forbid_cli(monkeypatch):
    def cli(argv, **_kwargs):
        raise AssertionError(f"the CLI ran for {argv!r}")

    monkeypatch.setattr(subprocess, "run", cli)


def test_a_found_and_a_missing_container_read_as_the_cli_prints_them(daemon, monkeypatch):
    daemon.state.containers["chat-1"] = _container("abc123", "chat-1")
    _forbid_cli(monkeypatch)

    result = run_docker(
        ["docker", "container", "inspect", "chat-1", "gone"], timeout=5, engine=daemon.engine
    )

    assert result.returncode == 1
    assert [item["Id"] for item in json.loads(result.stdout)] == ["abc123"]
    assert result.stderr == "Error response from daemon: No such container: gone\n"
    assert daemon.state.requests == [
        f"/v{API_VERSION}/containers/chat-1/json",
        f"/v{API_VERSION}/containers/gone/json",
    ]


def test_a_missing_network_reads_as_the_cli_prints_it(daemon, monkeypatch):
    _forbid_cli(monkeypatch)

    result = run_docker(["docker", "network", "inspect", "net-a"], timeout=5, engine=daemon.engine)

    assert (result.returncode, result.stdout) == (1, "[]")
    assert result.stderr == "Error response from daemon: network net-a not found\n"


def test_the_batch_readers_parse_an_engine_answer(daemon, monkeypatch):
    """The readers that tolerate a vanished object do so for an API answer too."""
    daemon.state.containers["one"] = _container("id-one", "one")
    daemon.state.networks["net-a"] = {"Name": "net-a", "Id": "nid"}
    _forbid_cli(monkeypatch)

    def run(argv, *, timeout):
        return run_docker(argv, timeout=timeout, engine=daemon.engine)

    assert [raw["Id"] for raw in inspect_many(run, "docker", "container", ["one", "two"])] == [
        "id-one"
    ]
    found, failed = inspect_each(run, "docker", "network", ["net-a", "net-b"])
    assert ([raw["Id"] for raw in found], failed) == (["nid"], {})
    assert list(containers_by_ref(run, "docker", ["one", "two"])) == ["one"]


def test_the_manager_reads_through_the_engine_its_backend_carries(daemon, monkeypatch):
    daemon.state.containers["chat-1"] = _container("abc123", "chat-1")
    _forbid_cli(monkeypatch)
    manager = ChatSandboxManager(ContainerBackend(image="image", engine=daemon.engine))

    found = manager.inspect("chat-1", verify_mounts=False)

    assert found is not None and (found.container_id, found.running) == ("abc123", True)
    assert manager.inspect("gone", verify_mounts=False) is None


def test_a_backend_without_an_engine_keeps_every_read_on_the_cli(monkeypatch):
    calls: list[list[str]] = []

    def cli(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "[]", "Error: No such object: gone\n")

    monkeypatch.setattr(subprocess, "run", cli)
    manager = ChatSandboxManager(ContainerBackend(image="image"))

    assert manager.inspect("gone") is None
    assert calls == [["docker", "container", "inspect", "gone"]]


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "container", "inspect", "--format", "{{.Id}}", "chat-1"],
        ["docker", "network", "inspect", "-f", "{{.Id}}", "net-a"],
        ["docker", "image", "inspect", "sha256:image"],
        ["docker", "container", "inspect"],
        ["docker", "start", "chat-1"],
        ["docker", "network", "connect", "net-a", "chat-1"],
    ],
)
def test_anything_but_a_plain_inspect_runs_the_cli(daemon, monkeypatch, argv):
    calls: list[list[str]] = []

    def cli(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", cli)

    run_docker(list(argv), timeout=5, engine=daemon.engine)

    assert calls == [argv]
    assert daemon.state.requests == []


@pytest.mark.parametrize("trouble", ["status", "body", "socket"])
def test_an_answer_the_api_cannot_give_plainly_falls_back_to_the_cli(daemon, monkeypatch, trouble):
    """A server error, a body that is not JSON or no daemon at all: the CLI decides."""
    daemon.state.containers["chat-1"] = _container("abc123", "chat-1")
    engine = daemon.engine
    if trouble == "status":
        daemon.state.status = 500
    elif trouble == "body":
        daemon.state.raw = b"<html>proxy error</html>"
    else:
        engine = DockerEngine(daemon.path + ".missing")
    calls: list[list[str]] = []

    def cli(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(subprocess, "run", cli)

    run_docker(["docker", "container", "inspect", "chat-1"], timeout=5, engine=engine)

    assert calls == [["docker", "container", "inspect", "chat-1"]]


def test_a_daemon_that_does_not_answer_in_time_fails_as_the_cli_would(daemon, monkeypatch):
    """A hung daemon would hang the CLI too, so its read is not tried twice."""
    daemon.state.delay = 2.0
    _forbid_cli(monkeypatch)

    with pytest.raises(SandboxError, match="Docker lifecycle command failed"):
        run_docker(["docker", "container", "inspect", "chat-1"], timeout=0.2, engine=daemon.engine)


def _env(tmp_path, **values: str) -> dict[str, str]:
    return {"HOME": str(tmp_path), **values}


def test_the_default_context_reads_the_default_socket(tmp_path):
    engine = engine_from_env(_env(tmp_path), "docker")
    assert engine is not None and engine.socket_path == DEFAULT_SOCKET
    engine = engine_from_env(_env(tmp_path, DOCKER_CONTEXT="default"), "docker")
    assert engine is not None and engine.socket_path == DEFAULT_SOCKET


def test_docker_host_names_the_socket_and_overrides_any_context(tmp_path):
    """The CLI's precedence: ``DOCKER_HOST`` wins over a context, as it does there."""
    env = _env(tmp_path, DOCKER_HOST="unix:///run/user/docker.sock", DOCKER_CONTEXT="remote")
    engine = engine_from_env(env, "docker")
    assert engine is not None and engine.socket_path == "/run/user/docker.sock"


@pytest.mark.parametrize(
    ("values", "binary"),
    [
        ({}, "/opt/podman/bin/docker"),
        ({"DOCKER_HOST": "tcp://10.0.0.5:2376"}, "docker"),
        ({"DOCKER_HOST": "ssh://builder@host"}, "docker"),
        ({"DOCKER_HOST": "unix://"}, "docker"),
        ({"DOCKER_CONTEXT": "remote"}, "docker"),
    ],
)
def test_a_daemon_the_cli_may_reach_elsewhere_keeps_the_cli(tmp_path, values, binary):
    assert engine_from_env(_env(tmp_path, **values), binary) is None


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"currentContext": "default"}, DEFAULT_SOCKET),
        ({"auths": {}}, DEFAULT_SOCKET),
        ({"currentContext": "remote"}, None),
        ("not json", None),
    ],
)
def test_the_cli_configuration_s_current_context_is_honoured(tmp_path, config, expected):
    directory = tmp_path / "docker-config"
    directory.mkdir()
    text = config if isinstance(config, str) else json.dumps(config)
    (directory / "config.json").write_text(text, encoding="utf-8")

    engine = engine_from_env(_env(tmp_path, DOCKER_CONFIG=str(directory)), "docker")

    assert (engine.socket_path if engine is not None else None) == expected


def test_a_backend_built_from_the_environment_carries_the_engine(tmp_path):
    env = {
        "AGENT_SANDBOX_BACKEND": "container",
        "AGENT_SANDBOX_IMAGE": "sandbox@sha256:abc",
        "HOME": str(tmp_path),
    }
    backend = build_backend_from_env(env, with_network=False)
    assert isinstance(backend, ContainerBackend)
    assert isinstance(backend.engine, DockerEngine)

    other = build_backend_from_env({**env, "AGENT_SANDBOX_DOCKER": "podman"}, with_network=False)
    assert isinstance(other, ContainerBackend)
    assert other.engine is None
    assert ContainerBackend(image="image").engine is None
