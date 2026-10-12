"""CLI plugin installation, private launch state, and host protocol contracts."""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import httpx
import pytest

from cloud_agent_host import resident_transport
from cloud_agent_host.bounded_output import text_chunks
from cloud_agent_host.chat_sandbox import (
    ChatSandboxManager,
    ChatSandboxSpec,
    claude_member_config_dir,
)
from cloud_agent_host.drivers import DriverError
from cloud_agent_host.plugins import install_claude_plugins, run_plugin_command
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransportFactory
from cloud_agent_host.runner import ClaimedJob
from cloud_agent_host.session_supervisor_client import SessionSupervisorClient
from cloud_agent_host.workspace_broker import SharedResidentLaunchConfig

if TYPE_CHECKING:
    from pathlib import Path

_PLUGINS = (
    {"plugin": "review@fixture", "marketplace": "owner/marketplace"},
    {"plugin": "format@fixture", "marketplace": "owner/marketplace"},
)


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_origin",
        sandbox_id="asbx_plugins",
        generation=1,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        resident_slot_capacity=8,
    )


def _launch(**updates) -> ResidentLaunch:
    return replace(
        ResidentLaunch(
            thread_id="athr_origin",
            runtime_id="claude-code",
            runtime_version="2.1.220",
            native_session_id=None,
            model="model",
            reasoning_effort=None,
            gateway_base_url="http://gateway",
            credential="scoped-inference-secret",
            mcp_credential="scoped-mcp-secret",
            slot_id=1,
            plugins=_PLUGINS,
        ),
        **updates,
    )


class _LocalProcess:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process

    def lines(self):
        assert self.process.stdout is not None
        yield from text_chunks(self.process.stdout)

    def wait(self) -> int:
        return self.process.wait()

    def kill(self) -> None:
        self.process.kill()


class _LocalManager:
    backend = SimpleNamespace(uid=10001, gid=10001)

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def spawn(self, _spec, argv, **kwargs):
        assert (
            ChatSandboxManager._validate_member_pidfile(
                self,  # type: ignore[arg-type]
                _spec,
                kwargs["process_pidfile"],
                user_uid=kwargs["user_uid"],
            )
            == 1
        )
        self.calls.append({"argv": argv, **kwargs})
        return _LocalProcess(
            subprocess.Popen(
                argv,
                env=kwargs["env"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        )


def _native_stub(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    config = tmp_path / "config"
    config.mkdir()
    log = tmp_path / "calls.jsonl"
    binary = tmp_path / "claude"
    binary.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, pathlib, sys\n"
        "config = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])\n"
        "with open(os.environ['PLUGIN_TEST_LOG'], 'a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['plugin', 'install'] and (config / 'fail-once').exists():\n"
        "    (config / 'fail-once').unlink()\n"
        "    print('x' * 10000)\n"
        "    raise SystemExit(7)\n"
    )
    binary.chmod(0o755)
    env = {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "PLUGIN_TEST_LOG": str(log),
    }
    return env, config, log


def test_claude_installs_unique_marketplaces_and_caches_only_complete_success(tmp_path):
    env, config, log = _native_stub(tmp_path)
    manager = _LocalManager()
    for _ in range(2):
        install_claude_plugins(
            manager,  # type: ignore[arg-type]
            _spec(),
            _PLUGINS,
            config_dir=str(config),
            env=env,
            slot_id=1,
        )
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        ["plugin", "marketplace", "add", "owner/marketplace"],
        ["plugin", "install", "review@fixture", "--scope", "user"],
        ["plugin", "install", "format@fixture", "--scope", "user"],
    ]
    assert json.loads((config / ".freeinference-plugins.json").read_text()) == list(_PLUGINS)
    assert (config / ".freeinference-plugins.json").stat().st_mode & 0o777 == 0o600
    assert manager.calls[0]["user_uid"] == 11002
    assert manager.calls[0]["env"]["CLAUDE_CONFIG_DIR"] == str(config)


def test_failed_claude_install_is_retried_with_bounded_diagnostics(tmp_path):
    env, config, log = _native_stub(tmp_path)
    (config / "fail-once").touch()
    manager = _LocalManager()
    kwargs = {"config_dir": str(config), "env": env, "slot_id": 1}
    with pytest.raises(DriverError, match="plugin installation failed") as raised:
        install_claude_plugins(manager, _spec(), _PLUGINS, **kwargs)  # type: ignore[arg-type]
    assert len(str(raised.value)) < 600
    assert not (config / ".freeinference-plugins.json").exists()
    install_claude_plugins(manager, _spec(), _PLUGINS, **kwargs)  # type: ignore[arg-type]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[:2] == calls[2:4]
    assert json.loads((config / ".freeinference-plugins.json").read_text()) == list(_PLUGINS)


def test_marketplace_source_is_passed_as_one_literal_argument(tmp_path):
    env, config, log = _native_stub(tmp_path)
    source = "https://example.com/marketplace.git;touch${IFS}/tmp/should-not-exist"
    install_claude_plugins(
        _LocalManager(),  # type: ignore[arg-type]
        _spec(),
        ({"plugin": "review@fixture", "marketplace": source},),
        config_dir=str(config),
        env=env,
        slot_id=1,
    )
    assert json.loads(log.read_text().splitlines()[0])[-1] == source


def test_plugin_deadline_kills_and_reaps_even_when_output_is_already_closed():
    killed = threading.Event()

    class Process:
        waited = False

        def lines(self):
            return iter(())

        def wait(self):
            killed.wait()
            self.waited = True
            return -9

        def kill(self):
            killed.set()

    process = Process()
    manager = SimpleNamespace(backend=SimpleNamespace(gid=10001), spawn=lambda *_a, **_kw: process)
    with pytest.raises(DriverError, match="plugin installation exceeded"):
        run_plugin_command(
            manager,  # type: ignore[arg-type]
            _spec(),
            ["silent-installer"],
            env={},
            slot_id=1,
            timeout_s=0.01,
        )
    assert killed.is_set()
    assert process.waited is True


def test_plugin_failures_include_the_retained_process_stderr_tail():
    process = SimpleNamespace(
        lines=lambda: iter(()),
        wait=lambda: 1,
        stderr_text=lambda: "x" * 10000 + "registry download failed",
    )
    manager = SimpleNamespace(backend=SimpleNamespace(gid=10001), spawn=lambda *_a, **_kw: process)
    with pytest.raises(DriverError, match="registry download failed") as raised:
        run_plugin_command(manager, _spec(), ["installer"], env={}, slot_id=1)  # type: ignore[arg-type]
    assert len(str(raised.value)) < 600


@pytest.mark.parametrize("fails", [False, True])
def test_installation_precedes_the_first_private_claude_launch(monkeypatch, fails):
    events: list[str] = []
    manager = Mock()
    manager.backend.gid = 10001
    manager.backend.base_env.return_value = {"HOME": "/home/agent", "PATH": "/usr/bin"}
    manager.reap_member_processes.side_effect = lambda *_args, **_kwargs: events.append("reap")
    factory = ResidentTransportFactory(manager)
    monkeypatch.setattr(
        factory, "_prepare_claude_member_state", lambda *_a: events.append("prepare")
    )

    def install(_manager, _spec, plugins, *, env, config_dir, slot_id):
        events.append("install")
        assert plugins == _PLUGINS
        assert slot_id == 1
        assert env["HOME"] == "/home/agent-member-1"
        assert env["PATH"].startswith("/opt/runtimes/claude-code/2.1.220/bin:")
        assert config_dir == claude_member_config_dir(1, "athr_origin")
        assert not any("secret" in value for value in env.values())
        if fails:
            raise DriverError("native install failed")

    def launch_live(context):
        events.append("launch")
        return SimpleNamespace(ensure_started=lambda **_kw: None, process_alive=lambda: True)

    monkeypatch.setattr(resident_transport, "install_claude_plugins", install)
    monkeypatch.setattr(
        resident_transport, "get_driver", lambda _r: SimpleNamespace(launch_live=launch_live)
    )
    if fails:
        with pytest.raises(DriverError, match="native install failed"):
            factory.open(_spec(), _launch())
        assert events == ["reap", "prepare", "install"]
    else:
        factory.open(_spec(), _launch())
        assert events == ["reap", "prepare", "install", "launch"]


@pytest.mark.parametrize("runtime_id", ["codex", "pi", "opencode", "kilo"])
def test_other_runtime_installers_receive_private_env_before_launch(monkeypatch, runtime_id):
    versions = {"codex": "0.145.0", "pi": "0.73.1", "opencode": "1.18.9", "kilo": "7.4.17"}
    plugins = _PLUGINS if runtime_id == "codex" else ({"plugin": "@owner/plugin@1.2.3"},)
    launch = _launch(runtime_id=runtime_id, runtime_version=versions[runtime_id], plugins=plugins)
    events: list[str] = []
    manager = Mock()
    manager.backend.gid = 10001
    manager.backend.base_env.return_value = {"HOME": "/home/agent", "PATH": "/usr/bin"}
    factory = ResidentTransportFactory(manager)
    monkeypatch.setattr(factory, "_prepare_member_state", lambda *_a: None)

    def install(_manager, _spec, *selected, env, slot_id, **kwargs):
        events.append("install")
        assert env["HOME"] == "/home/agent-member-1"
        assert env["PATH"].startswith(f"/opt/runtimes/{runtime_id}/{versions[runtime_id]}/bin:")
        assert slot_id == 1
        assert not any("secret" in value for value in env.values())
        if runtime_id == "codex":
            assert selected == (plugins,)
            assert "/codex-members/" in kwargs["state_dir"]
            return None
        assert kwargs["runtime_id"] == runtime_id
        assert kwargs["plugins"] == plugins
        return {"AGENT_PLUGIN_PATHS": '["/private/plugin"]'}

    def launch_live(context):
        events.append("launch")
        if runtime_id != "codex":
            assert context.base_env["AGENT_PLUGIN_PATHS"] == '["/private/plugin"]'
        return SimpleNamespace(ensure_started=lambda **_kw: None, process_alive=lambda: True)

    monkeypatch.setattr(resident_transport, "install_codex_plugins", install)
    monkeypatch.setattr(resident_transport, "install_other_runtime_plugins", install)
    monkeypatch.setattr(
        resident_transport,
        "get_driver",
        lambda _r: SimpleNamespace(launch_live=launch_live),
    )
    factory.open(_spec(), launch)
    assert events == ["install", "launch"]


def test_claim_validates_runtime_plugin_specs_and_preserves_legacy_claims():
    body = {
        "job_id": "ajob_plugins",
        "thread_id": "athr_origin",
        "attempt_id": 1,
        "attempt_no": 1,
        "repo": "owner/repo",
        "task_prompt": "review",
        "runtime": "claude-code",
        "model": "model",
        "worker_token": "worker-secret",
        "sandbox_token": "scoped-inference-secret",
    }
    assert ClaimedJob.from_response(body).plugins == []
    assert ClaimedJob.from_response({**body, "plugins": list(_PLUGINS)}).plugins == list(_PLUGINS)
    with pytest.raises(ValueError):
        ClaimedJob.from_response({**body, "plugins": {}})
    with pytest.raises(ValueError):
        ClaimedJob.from_response({**body, "plugins": [{"plugin": "npm-package"}]})
    with pytest.raises(ValueError):
        ClaimedJob.from_response({**body, "plugins": [*_PLUGINS] * 5})
    assert ClaimedJob.from_response(
        {**body, "runtime": "pi", "plugins": [{"plugin": "npm:@owner/package@1.2.3"}]}
    ).plugins == [{"plugin": "@owner/package@1.2.3"}]


def test_plugin_selections_survive_the_client_and_broker_launch_protocol():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"resident_state": "live"})

    client = SessionSupervisorClient("http://broker", "broker-secret")
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        client.ensure_shared_resident("athr_origin", token="lease-token", launch=_launch())
    finally:
        client.close()
    [request] = seen
    payload = json.loads(request.content)
    assert payload["launch"]["plugins"] == list(_PLUGINS)
    broker = SharedResidentLaunchConfig.model_validate(payload["launch"])
    assert [plugin.model_dump() for plugin in broker.plugins] == list(_PLUGINS)
    assert request.extensions["timeout"]["read"] == 960.0
    assert json.loads(json.dumps(asdict(_launch())))["plugins"] == list(_PLUGINS)


def test_native_fork_rejects_different_plugin_selections_before_mutating():
    factory = ResidentTransportFactory(Mock())
    source = SimpleNamespace(native_session_id="native-source", launch=_launch())
    child = _launch(thread_id="athr_child", slot_id=2, plugins=())
    with pytest.raises(DriverError, match="cannot change plugins"):
        factory.fork_native(_spec(), source, "fork-operation", child, 1)
