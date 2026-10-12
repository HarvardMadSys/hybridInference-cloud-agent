"""Real filesystem/process tests for the shared Kata installation fence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from deploy.host import kata_runtime_epoch as epoch

HELPER = Path(epoch.__file__)


def _directory(root: Path) -> Path:
    descriptor = epoch.open_directory(root, create=True)
    assert descriptor is not None
    os.close(descriptor)
    return root.joinpath(*epoch.STATE_PARTS)


def _command(root: Path, action: str, *command: str) -> list[str]:
    return [sys.executable, str(HELPER), action, "--root", str(root), *command]


def _run(root: Path, action: str, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(root, action, *command), check=False, capture_output=True, text=True, timeout=10
    )


def test_initialize_publishes_one_generation_and_preserves_it(tmp_path):
    directory = _directory(tmp_path)
    first = _run(tmp_path, "initialize")
    assert first.returncode == 0, first.stderr
    marker = directory / "epoch.json"
    content = marker.read_bytes()
    value = json.loads(content)
    assert value == {
        "version": 1,
        "state": "ready",
        "generation": str(uuid.UUID(value["generation"])),
    }
    assert marker.stat().st_uid == os.geteuid()
    assert marker.stat().st_mode & 0o022 == 0
    assert _run(tmp_path, "initialize").returncode == 0
    assert marker.read_bytes() == content


@pytest.mark.parametrize("target", ["directory", "ancestor", "marker", "lock"])
def test_epoch_refuses_symlinks_without_touching_the_target(tmp_path, target):
    directory = _directory(tmp_path)
    victim = tmp_path / "victim"
    if target in {"directory", "ancestor"}:
        victim.mkdir()
        redirected = directory if target == "directory" else directory.parent
        if target == "ancestor":
            directory.rmdir()
        redirected.rmdir()
        redirected.symlink_to(victim, target_is_directory=True)
    else:
        victim.write_text("untouched")
        name = "epoch.json" if target == "marker" else epoch.LOCK_NAME
        (directory / name).symlink_to(victim)
    result = _run(tmp_path, "initialize")
    assert result.returncode != 0
    if victim.is_file():
        assert victim.read_text() == "untouched"
    else:
        assert list(victim.iterdir()) == []


@pytest.mark.parametrize("target", ["directory", "ancestor", "marker", "lock"])
def test_epoch_refuses_group_writable_state(tmp_path, target):
    directory = _directory(tmp_path)
    assert _run(tmp_path, "initialize").returncode == 0
    paths = {
        "directory": directory,
        "ancestor": directory.parent,
        "marker": directory / "epoch.json",
        "lock": directory / epoch.LOCK_NAME,
    }
    paths[target].chmod(paths[target].stat().st_mode | 0o020)
    before = (directory / "epoch.json").read_bytes()
    result = _run(tmp_path, "initialize")
    assert result.returncode != 0
    assert (directory / "epoch.json").read_bytes() == before


@pytest.mark.parametrize("kind", ["updating", "malformed", "unknown_version", "oversized", "fifo"])
def test_initialize_never_repairs_an_existing_unusable_marker(tmp_path, kind):
    directory = _directory(tmp_path)
    marker = directory / "epoch.json"
    value = {"version": 1, "state": "updating", "generation": str(uuid.uuid4())}
    if kind == "unknown_version":
        value["version"] = 2
    if kind == "fifo":
        os.mkfifo(marker)
    else:
        content = "{" if kind == "malformed" else json.dumps(value)
        marker.write_text(content + (" " * 4096 if kind == "oversized" else ""))
        before = marker.read_bytes()
    result = _run(tmp_path, "initialize")
    assert result.returncode != 0
    if kind == "fifo":
        assert marker.is_fifo()
    else:
        assert marker.read_bytes() == before


def test_concurrent_initializers_publish_only_one_marker(tmp_path):
    directory = _directory(tmp_path)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: _run(tmp_path, "initialize"), range(6)))
    assert all(result.returncode == 0 for result in results)
    value = json.loads((directory / "epoch.json").read_text())
    assert value["state"] == "ready"
    assert str(uuid.UUID(value["generation"])) == value["generation"]
    assert sorted(path.name for path in directory.iterdir()) == [epoch.LOCK_NAME, "epoch.json"]


@pytest.mark.parametrize("succeeds", [False, True])
def test_initializer_waits_for_install_and_cannot_clear_partial_update(tmp_path, succeeds):
    directory = _directory(tmp_path)
    assert _run(tmp_path, "initialize").returncode == 0
    old = json.loads((directory / "epoch.json").read_text())
    # This stands in for the shell installer's mutation/check body. It uses
    # the actual inherited flock descriptor and marks before its first write.
    body = (
        "import pathlib, subprocess, sys, time; "
        "root = pathlib.Path(sys.argv[1]); "
        "subprocess.run([sys.executable, sys.argv[2], 'begin', '--root', str(root)], "
        "check=True, close_fds=False); "
        "(root / 'mutating').touch(); "
        "\nwhile not (root / 'continue').exists(): time.sleep(.01)\n"
        f"sys.exit({0 if succeeds else 1})"
    )
    installer = subprocess.Popen(
        _command(tmp_path, "install", "--", sys.executable, "-c", body, str(tmp_path), str(HELPER)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    initializer = None
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "mutating").exists() and time.monotonic() < deadline:
            if installer.poll() is not None:
                pytest.fail(f"installer failed before mutation: {installer.communicate()}")
            time.sleep(0.01)
        assert (tmp_path / "mutating").exists()
        updating = json.loads((directory / "epoch.json").read_text())
        assert updating["state"] == "updating"
        assert updating["generation"] != old["generation"]
        initializer = subprocess.Popen(
            _command(tmp_path, "initialize"), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        with pytest.raises(subprocess.TimeoutExpired):
            initializer.wait(timeout=0.1)
        (tmp_path / "continue").touch()
        assert installer.wait(timeout=5) == (0 if succeeds else 1)
        assert initializer.wait(timeout=5) == (0 if succeeds else 1)
        current = json.loads((directory / "epoch.json").read_text())
        assert current["generation"] == updating["generation"]
        assert current["state"] == ("ready" if succeeds else "updating")
    finally:
        (tmp_path / "continue").touch()
        installer.wait(timeout=5)
        installer.communicate()
        if initializer is not None:
            initializer.wait(timeout=5)
            initializer.communicate()


def test_verify_path_is_read_only_and_accepts_missing_state(tmp_path):
    assert _run(tmp_path, "verify-path").returncode == 0
    assert list(tmp_path.iterdir()) == []


def test_state_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    _directory(tmp_path)
    monkeypatch.setattr(epoch.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(RuntimeError, match="owner-controlled"):
        epoch.open_directory(tmp_path)


@pytest.mark.parametrize("artifact", [False, True])
@pytest.mark.parametrize("check_succeeds", [False, True])
def test_deploy_initializes_with_verified_image_and_only_dedicated_state_mount(
    tmp_path, artifact, check_succeeds
):
    """Exercise the real shell function with a recorded Docker boundary."""
    from deploy.release.build_recipe import RECIPE_FILES

    assert "deploy/host/kata_runtime_epoch.py" in RECIPE_FILES
    host_script = HELPER.with_name("sandbox_host.sh").read_text()
    function = host_script[
        host_script.index("initialize_kata_epoch() {") : host_script.index("check_candidate() {")
    ]
    recipe = tmp_path / ("artifact-recipe" if artifact else "source") / "deploy/host"
    recipe.mkdir(parents=True)
    shutil.copyfile(HELPER, recipe / HELPER.name)
    checking = tmp_path / "check-kata"
    checking.write_text(
        '#!/bin/sh\n[ "$1" = "--check" ] || exit 99\n'
        'echo check >> "$EPOCH_TEST_LOG"\n'
        f"exit {0 if check_succeeds else 1}\n"
    )
    checking.chmod(0o755)
    root = tmp_path / "root"
    root.mkdir(mode=0o755)
    commands = tmp_path / "bin"
    commands.mkdir()
    image_id = "sha256:" + "a" * 64
    docker = commands / "docker"
    docker.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "with open(os.environ['EPOCH_TEST_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"if sys.argv[1:3] == ['image', 'inspect']: print({image_id!r})\n"
        "else: sys.stdin.read()\n"
    )
    docker.chmod(0o755)
    log = tmp_path / "commands"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -e; log() { :; }; die() { echo "$*" >&2; exit 1; }; '
            'docker() { "$TEST_DOCKER" "$@"; };\n'
            + function
            + '\ninitialize_kata_epoch "trusted-runner:current" kata\n',
        ],
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "KATA_EXTRACT_ROOT": str(root),
            "KATA_SETUP_SCRIPT": str(checking),
            "SCRIPT_DIR": str(recipe),
            "EPOCH_TEST_LOG": str(log),
            "TEST_DOCKER": str(docker),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    records = log.read_text().splitlines()
    assert records[0] == "check"
    if not check_succeeds:
        assert result.returncode != 0
        assert records == ["check"], "failed host verification must not invoke an initializer"
        return
    assert result.returncode == 0, result.stderr
    inspection, run = [json.loads(record) for record in records[1:]]
    assert inspection == ["image", "inspect", "--format", "{{.Id}}", "trusted-runner:current"]
    assert run[0] == "run" and "--rm" in run and "--read-only" in run
    for option, expected in [
        ("--runtime", "runc"),
        ("--network", "none"),
        ("--user", "0:0"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--pull", "never"),
        ("--entrypoint", "python"),
    ]:
        assert run[run.index(option) + 1] == expected
    assert run.count("-v") == 1
    assert run[run.index("-v") + 1] == (
        f"{root}/var/lib/freeinference/kata-runtime:/run/agent-kata-runtime"
    )
    assert run[-4:] == [image_id, "-", "initialize", "--mounted"]
