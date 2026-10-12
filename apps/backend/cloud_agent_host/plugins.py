"""Install explicitly selected CLI plugins inside private sandbox members."""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from concurrent.futures import Future
from typing import TYPE_CHECKING

from cloud_agent_host.bounded_output import OutputPump, TextTail
from cloud_agent_host.chat_sandbox import CHAT_WORKDIR, MEMBER_TMPFS_ROOT, MEMBER_UID_BASE
from cloud_agent_host.drivers import DriverError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec

PLUGIN_INSTALL_TIMEOUT_S = 900.0


def run_plugin_command(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    argv: list[str],
    *,
    env: dict[str, str],
    slot_id: int,
    timeout_s: float = PLUGIN_INSTALL_TIMEOUT_S,
) -> None:
    """Run bounded installation without granting the process agent credentials."""
    process = manager.spawn(
        spec,
        argv,
        env=env,
        workdir=CHAT_WORKDIR,
        user_uid=MEMBER_UID_BASE + slot_id,
        user_gid=manager.backend.gid,
        process_pidfile=f"{MEMBER_TMPFS_ROOT}/{slot_id}/resident-{secrets.token_hex(16)}.pid",
    )
    output = OutputPump(getattr(process, "chunks", process.lines)(), name="plugin-install-output")
    receipt: Future[int] = Future()

    def wait_for_exit() -> None:
        try:
            receipt.set_result(process.wait())
        except Exception as exc:
            receipt.set_exception(exc)

    threading.Thread(target=wait_for_exit, daemon=True, name="plugin-install-wait").start()
    deadline = time.monotonic() + timeout_s
    tail = TextTail(limit=4096)
    completed = False
    output_finished = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DriverError(f"plugin installation exceeded {timeout_s:.0f}s")
            if output_finished:
                try:
                    exit_code = receipt.result(timeout=min(0.25, remaining))
                except TimeoutError:
                    continue
                break
            try:
                chunk = output.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if chunk is None:
                output_finished = True
            else:
                tail.append(chunk)
        completed = True
        if exit_code != 0:
            stderr = str(getattr(process, "stderr_text", lambda: "")())
            detail = (tail.text() + "\n" + stderr).strip()[-500:]
            raise DriverError(f"plugin installation failed ({exit_code}): {detail}")
    finally:
        if not completed:
            output.close(drain=True)
            process.kill()
            process.wait()
        output.close()


_CLAUDE_INSTALL_SCRIPT = r"""
import fcntl
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

config = pathlib.Path(sys.argv[1])
plugins = json.loads(sys.argv[2])
info = config.lstat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
    raise RuntimeError('plugin config is not member-owned')
os.umask(0o077)

def read_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise RuntimeError('plugin receipt exceeds its bound')
    try:
        return json.loads(raw)
    except ValueError:
        return None

def write_json(path, value):
    fd, filename = tempfile.mkstemp(prefix='.plugins.', dir=config)
    temporary = pathlib.Path(filename)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, separators=(',', ':'))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

lock_fd = os.open(config / '.freeinference-plugins.lock',
                  os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
with os.fdopen(lock_fd, 'r+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    marker = config / '.freeinference-plugins.json'
    if read_json(marker) == plugins:
        raise SystemExit(0)
    marketplaces = set()
    for plugin in plugins:
        source = plugin['marketplace']
        if source not in marketplaces:
            subprocess.run(['claude', 'plugin', 'marketplace', 'add', source], check=True)
            marketplaces.add(source)
        subprocess.run(['claude', 'plugin', 'install', plugin['plugin'],
                        '--scope', 'user'], check=True)
    write_json(marker, plugins)
"""


def install_claude_plugins(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    plugins: tuple[dict[str, str], ...],
    *,
    config_dir: str,
    env: dict[str, str],
    slot_id: int,
) -> None:
    """Install Claude plugins once per successful private member selection."""
    if not plugins:
        return
    install_env = {
        **env,
        "CLAUDE_CONFIG_DIR": config_dir,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CI": "1",
    }
    run_plugin_command(
        manager,
        spec,
        ["python3", "-c", _CLAUDE_INSTALL_SCRIPT, config_dir, json.dumps(plugins)],
        env=install_env,
        slot_id=slot_id,
    )


__all__ = ["PLUGIN_INSTALL_TIMEOUT_S", "install_claude_plugins", "run_plugin_command"]
