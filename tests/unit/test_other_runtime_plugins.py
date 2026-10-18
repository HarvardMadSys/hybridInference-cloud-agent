"""Private npm installation and explicit loading for the three package CLIs."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from cloud_agent_host.other_plugins import _INSTALL_SCRIPT, install_other_runtime_plugins

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("runtime", ["pi", "opencode", "kilo"])
def test_installer_returns_only_explicit_member_paths(monkeypatch, runtime):
    calls = []
    monkeypatch.setattr(
        "cloud_agent_host.other_plugins.run_plugin_command",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    selected = "@example/plugin@1.2.3"
    result = install_other_runtime_plugins(
        object(),
        object(),
        runtime_id=runtime,
        plugins=({"plugin": selected},),
        env={"PATH": "/opt/pinned/bin:/usr/bin"},
        slot_id=4,
    )
    key = hashlib.sha256(selected.encode()).hexdigest()
    root = f"/home/agent-member-4/.agent-plugins/{runtime}/{key}"
    expected = (
        f"{root}/lib/node_modules/@example/plugin" if runtime == "pi" else f"{root}/entry.mjs"
    )
    assert json.loads(result["AGENT_PLUGIN_PATHS"]) == [expected]
    argv = calls[0][0][2]
    assert argv[:3] == ["python3", "-I", "-c"]
    assert json.loads(argv[5])[0]["name"] == "@example/plugin"
    assert calls[0][1]["env"]["HOME"] == "/home/agent-member-4"
    assert calls[0][1]["env"]["PATH"] == "/opt/pinned/bin:/usr/bin"
    assert calls[0][1]["slot_id"] == 4


def test_installer_starts_in_the_workspace_without_importing_from_it(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "cloud_agent_host.other_plugins.run_plugin_command",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "npm").write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[sys.argv.index('--prefix') + 1])\n"
        "package = root / 'node_modules' / 'example-plugin'\n"
        "package.mkdir(parents=True)\n"
        "(package / 'package.json').write_text(json.dumps({'main': 'index.mjs'}))\n"
        "(package / 'index.mjs').write_text('export default {};')\n"
    )
    (tools / "npm").chmod(0o700)
    install_other_runtime_plugins(
        object(),
        object(),
        runtime_id="opencode",
        plugins=({"plugin": "example-plugin@1.2.3"},),
        env={"PATH": f"{tools}:/usr/bin:/bin"},
        slot_id=4,
    )
    ((args, kwargs),) = calls
    home = tmp_path / "member"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The project's own module, named like the standard library's.
    (workspace / "types.py").write_text("UserId = int\n")
    # The installer as run_plugin_command runs it, from the workspace, with
    # this test's directory as the member's home.
    result = subprocess.run(
        [*args[2][:-1], str(home)],
        cwd=workspace,
        env={**kwargs["env"], "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(list(home.glob(".agent-plugins/opencode/*/.complete.json"))) == 1


def _run_script(monkeypatch, home, runtime, *, spec="example-plugin@1.2.3"):
    selection = [{"spec": spec, "name": "example-plugin", "key": "selection"}]
    monkeypatch.setattr(sys, "argv", ["installer", runtime, json.dumps(selection), str(home)])
    previous_umask = os.umask(0o077)
    try:
        exec(compile(_INSTALL_SCRIPT, "plugin-installer", "exec"), {})
    finally:
        os.umask(previous_umask)


@pytest.mark.parametrize("runtime", ["pi", "opencode", "kilo"])
def test_native_install_uses_private_prefix_and_retains_success(monkeypatch, tmp_path, runtime):
    calls = []

    def install(argv, *, env, cwd, check):
        calls.append(argv)
        assert cwd == str(tmp_path)
        assert check is True
        root = tmp_path / ".agent-plugins" / runtime / "selection"
        if runtime == "pi":
            assert argv == ["pi", "install", "npm:example-plugin@1.2.3"]
            assert env["npm_config_prefix"] == str(root)
            assert env["PI_CODING_AGENT_DIR"] == str(root / "pi-config")
            package = root / "lib/node_modules/example-plugin"
        else:
            assert argv[:4] == ["npm", "install", "--prefix", str(root)]
            package = root / "node_modules/example-plugin"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"main": "index.mjs"}))
        (package / "index.mjs").write_text("export default {server: async () => ({})};")

    monkeypatch.setattr(subprocess, "run", install)
    _run_script(monkeypatch, tmp_path, runtime)
    _run_script(monkeypatch, tmp_path, runtime)
    assert len(calls) == 1
    marker = tmp_path / ".agent-plugins" / runtime / "selection/.complete.json"
    assert json.loads(marker.read_text()) == {"version": 1, "spec": "example-plugin@1.2.3"}


def test_pi_1x_install_is_found_where_the_cli_is_told_to_look(monkeypatch, tmp_path):
    """From 0.75.0 ``pi install`` puts a package in its config directory's npm project.

    The loading path handed to the CLI stays the one 0.73.1 installed to, so
    the installer links it there; a link left dangling by a failed attempt is
    replaced on the next one.
    """
    root = tmp_path / ".agent-plugins" / "pi" / "selection"
    selected = root / "lib/node_modules/example-plugin"
    managed = root / "pi-config/npm/node_modules/example-plugin"

    def install(argv, *, env, cwd, check):
        managed.mkdir(parents=True, exist_ok=True)
        (managed / "package.json").write_text(json.dumps({"name": "example-plugin"}))

    selected.parent.mkdir(parents=True)
    selected.symlink_to(tmp_path / "gone")
    monkeypatch.setattr(subprocess, "run", install)
    _run_script(monkeypatch, tmp_path, "pi")

    assert selected.is_symlink()
    assert selected.resolve() == managed.resolve()
    assert json.loads((selected / "package.json").read_text()) == {"name": "example-plugin"}
    assert (root / ".complete.json").exists()


def test_failed_install_has_no_success_receipt(monkeypatch, tmp_path):
    def fail(argv, **kwargs):
        raise subprocess.CalledProcessError(17, argv)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError) as failed:
        _run_script(monkeypatch, tmp_path, "opencode")
    assert failed.value.returncode == 17
    assert not (tmp_path / ".agent-plugins/opencode/selection/.complete.json").exists()


def test_server_export_is_selected_and_invalid_entry_fails(monkeypatch, tmp_path):
    def install(argv, **kwargs):
        package = tmp_path / ".agent-plugins/kilo/selection/node_modules/example-plugin"
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps({"exports": {"./server": {"import": "../escape.mjs"}}, "main": "index.mjs"})
        )

    monkeypatch.setattr(subprocess, "run", install)
    with pytest.raises(RuntimeError, match="entrypoint is invalid"):
        _run_script(monkeypatch, tmp_path, "kilo")
    assert not (tmp_path / ".agent-plugins/kilo/selection/.complete.json").exists()


def test_server_import_export_precedes_main(monkeypatch, tmp_path):
    def install(argv, **kwargs):
        package = tmp_path / ".agent-plugins/opencode/selection/node_modules/example-plugin"
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps(
                {
                    "exports": {"./server": {"import": "server.mjs", "default": "other.mjs"}},
                    "main": "wrong.mjs",
                }
            )
        )
        (package / "server.mjs").write_text("export default {server: async () => ({})};")

    monkeypatch.setattr(subprocess, "run", install)
    _run_script(monkeypatch, tmp_path, "opencode")
    shim = (tmp_path / ".agent-plugins/opencode/selection/entry.mjs").read_text()
    assert "server.mjs" in shim
    assert "wrong.mjs" not in shim
    assert 'id: value.id === undefined ? "example-plugin" : value.id' in shim


@pytest.mark.parametrize("runtime", ["pi", "opencode", "kilo"])
def test_wrappers_load_selected_paths_and_keep_mcp_guards(monkeypatch, tmp_path, runtime):
    namespace = runpy.run_path(str(REPO_ROOT / "deploy/sandbox" / f"{runtime}-freeinference"))
    captured = []
    monkeypatch.setattr(namespace["os"], "execvp", lambda binary, argv: captured.append(argv))
    monkeypatch.setattr(sys, "argv", ["wrapper", "--no-extensions", "--mode", "rpc"])
    config_dir = tmp_path / "config"
    selected = str(tmp_path / "user plugin" / "entry.mjs")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://relay/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "short-lived")
    monkeypatch.setenv(f"{runtime.upper()}_GATEWAY_MODEL", "model")
    monkeypatch.setenv("AGENT_PLUGIN_PATHS", json.dumps([selected]))
    monkeypatch.setenv("AGENT_MCP_SERVERS", '{"session":"http://mcp/v1/agent/mcp"}')
    monkeypatch.setenv("AGENT_MCP_TOKEN", "scoped-token")
    config_env = (
        "PI_CODING_AGENT_DIR" if runtime == "pi" else f"{runtime.upper()}_FREEINFERENCE_CONFIG_DIR"
    )
    monkeypatch.setenv(config_env, str(config_dir))
    namespace["main"]()
    if runtime == "pi":
        assert captured[0][:3] == ["pi", "-e", selected]
        assert "--no-extensions" in captured[0]
    else:
        config = json.loads((config_dir / "config.json").read_text())
        assert config["plugin"] == [Path(selected).as_uri()]
        assert (
            config["mcp"]["session"]["headers"]["Authorization"] == "Bearer {env:AGENT_MCP_TOKEN}"
        )
        assert namespace["os"].environ[f"{runtime.upper()}_DISABLE_PROJECT_CONFIG"] == "1"
        assert namespace["os"].environ[f"{runtime.upper()}_DISABLE_DEFAULT_PLUGINS"] == "1"


@pytest.mark.parametrize("runtime", ["pi", "opencode", "kilo"])
@pytest.mark.parametrize("raw", ["invalid", "{}", '["relative/path"]', "[17]"])
def test_wrappers_reject_invalid_explicit_paths(monkeypatch, runtime, raw):
    namespace = runpy.run_path(str(REPO_ROOT / "deploy/sandbox" / f"{runtime}-freeinference"))
    monkeypatch.setenv("AGENT_PLUGIN_PATHS", raw)
    with pytest.raises(SystemExit) as rejected:
        namespace["plugin_args" if runtime == "pi" else "plugin_sources"]()
    assert rejected.value.code == 64
