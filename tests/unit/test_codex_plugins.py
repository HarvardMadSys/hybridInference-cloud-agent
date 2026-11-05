"""Native Codex plugin setup is private, restartable, and fails closed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from cloud_agent_host import codex_plugins

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def native_cli(tmp_path):
    """Record native command calls and allow one selected operation to fail."""
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir()
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "home = pathlib.Path(os.environ['CODEX_HOME'])\n"
        "with (home / 'calls.jsonl').open('a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if (home / 'fail-add').exists() and sys.argv[3:5] == ['plugin', 'add']:\n"
        "    raise SystemExit(7)\n"
    )
    binary.chmod(0o700)
    return binary.parent


def run_install(home: Path, native_cli: Path, plugins: list[dict[str, str]]):
    return subprocess.run(
        [sys.executable, "-c", codex_plugins._CODEX_INSTALL_SCRIPT, str(home), json.dumps(plugins)],
        env={**os.environ, "PATH": str(native_cli), "CODEX_HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )


def calls(home: Path):
    return [json.loads(line) for line in (home / "calls.jsonl").read_text().splitlines()]


def test_native_install_registers_shared_marketplace_once_and_restarts_without_network(
    tmp_path, native_cli
):
    home = tmp_path / "member"
    home.mkdir()
    plugins = [
        {"plugin": "review@example", "marketplace": "example/plugins"},
        {"plugin": "format@example", "marketplace": "example/plugins"},
    ]
    assert run_install(home, native_cli, plugins).returncode == 0
    assert calls(home) == [
        [
            "-c",
            "features.plugins=true",
            "plugin",
            "marketplace",
            "add",
            "example/plugins",
            "--json",
        ],
        ["-c", "features.plugins=true", "plugin", "add", "review@example", "--json"],
        ["-c", "features.plugins=true", "plugin", "add", "format@example", "--json"],
    ]
    assert json.loads((home / ".freeinference-plugins.json").read_text()) == {
        "state_dir": str(home),
        "plugins": plugins,
    }
    assert run_install(home, native_cli, plugins).returncode == 0
    assert len(calls(home)) == 3


def test_failed_plugin_retries_without_readding_successful_marketplace(tmp_path, native_cli):
    home = tmp_path / "member"
    home.mkdir()
    failure = home / "fail-add"
    failure.touch()
    plugins = [{"plugin": "review@example", "marketplace": "example/plugins"}]
    assert run_install(home, native_cli, plugins).returncode != 0
    assert not (home / ".freeinference-plugins.json").exists()
    assert json.loads((home / ".freeinference-plugin-progress.json").read_text()) == {
        "state_dir": str(home),
        "marketplaces": ["example/plugins"],
        "plugins": [],
    }
    failure.unlink()
    assert run_install(home, native_cli, plugins).returncode == 0
    assert [call[3] for call in calls(home)] == ["marketplace", "add", "add"]


def test_cloned_fork_reinitializes_native_plugins_in_the_child_home(tmp_path, native_cli):
    source = tmp_path / "source-member"
    source.mkdir()
    plugins = [{"plugin": "review@example", "marketplace": "example/plugins"}]
    assert run_install(source, native_cli, plugins).returncode == 0
    child = tmp_path / "child-member"
    shutil.copytree(source, child)
    (child / "calls.jsonl").unlink()
    assert run_install(child, native_cli, plugins).returncode == 0
    assert [call[3] for call in calls(child)] == ["marketplace", "add"]
    assert json.loads((child / ".freeinference-plugins.json").read_text())["state_dir"] == str(
        child
    )
    assert json.loads((source / ".freeinference-plugins.json").read_text())["state_dir"] == str(
        source
    )


@pytest.mark.parametrize("receipt", [".freeinference-plugins.json", ".freeinference-plugins.lock"])
def test_member_cannot_follow_symlinked_install_receipts(tmp_path, native_cli, receipt):
    home = tmp_path / "member"
    home.mkdir()
    external = tmp_path / "other-member-receipt"
    external.write_text("[]")
    (home / receipt).symlink_to(external)
    result = run_install(
        home, native_cli, [{"plugin": "review@example", "marketplace": "example/plugins"}]
    )
    assert result.returncode != 0
    assert external.read_text() == "[]"
    assert not (home / "calls.jsonl").exists()


def test_member_cannot_use_another_homes_symlink(tmp_path, native_cli):
    target = tmp_path / "other-member"
    target.mkdir()
    home = tmp_path / "member"
    home.symlink_to(target, target_is_directory=True)
    result = run_install(
        home, native_cli, [{"plugin": "review@example", "marketplace": "example/plugins"}]
    )
    assert result.returncode != 0
    assert "not member-owned" in result.stderr
    assert not (target / "calls.jsonl").exists()


def test_receipt_reads_are_bounded_before_native_install(tmp_path, native_cli):
    home = tmp_path / "member"
    home.mkdir()
    (home / ".freeinference-plugins.json").write_text(" " * 65537)
    result = run_install(
        home, native_cli, [{"plugin": "review@example", "marketplace": "example/plugins"}]
    )
    assert result.returncode != 0
    assert "exceeds its bound" in result.stderr
    assert not (home / "calls.jsonl").exists()


def test_install_command_uses_member_codex_home_and_supplied_base_environment(monkeypatch):
    captured = []
    monkeypatch.setattr(
        codex_plugins, "run_plugin_command", lambda *args, **kw: captured.append((args, kw))
    )
    selections = ({"plugin": "review@example", "marketplace": "example/plugins"},)
    codex_plugins.install_codex_plugins(
        None,
        None,
        selections,
        state_dir="/private/codex-member",
        env={"HOME": "/home/member", "PATH": "/native/bin"},
        slot_id=4,
    )
    args, kwargs = captured[0]
    assert args[2][:3] == ["python3", "-I", "-c"]
    assert args[2][4] == "/private/codex-member"
    assert json.loads(args[2][5]) == list(selections)
    assert kwargs == {
        "env": {
            "HOME": "/home/member",
            "PATH": "/native/bin",
            "CODEX_HOME": "/private/codex-member",
            "CI": "1",
        },
        "slot_id": 4,
    }
    codex_plugins.install_codex_plugins(
        None, None, (), state_dir="/private/codex-member", env={}, slot_id=4
    )
    assert len(captured) == 1


def test_native_install_starts_in_the_workspace_without_importing_from_it(
    monkeypatch, tmp_path, native_cli
):
    captured = []
    monkeypatch.setattr(
        codex_plugins, "run_plugin_command", lambda *args, **kw: captured.append((args, kw))
    )
    home = tmp_path / "member"
    home.mkdir()
    codex_plugins.install_codex_plugins(
        None,
        None,
        ({"plugin": "review@example", "marketplace": "example/plugins"},),
        state_dir=str(home),
        env={"PATH": f"{native_cli}:/usr/bin:/bin"},
        slot_id=4,
    )
    ((args, kwargs),) = captured
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The project's own module, named like the standard library's.
    (workspace / "types.py").write_text("UserId = int\n")
    # The installer as run_plugin_command runs it, from the workspace.
    result = subprocess.run(
        args[2], cwd=workspace, env=kwargs["env"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert len(calls(home)) == 2
