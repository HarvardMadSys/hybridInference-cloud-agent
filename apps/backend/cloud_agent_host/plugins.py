"""Run explicitly selected CLI plugins' installers inside private sandbox members.

The bounded runner and the locked installer preamble every CLI's installer
shares: Claude's (:mod:`cloud_agent_host.runtimes.claude.plugins`), Codex's
(:mod:`cloud_agent_host.runtimes.codex.plugins`), and the npm installer pi,
OpenCode and Kilo share (:mod:`cloud_agent_host.other_plugins`).
"""

from __future__ import annotations

import queue
import secrets
import textwrap
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
        # The installers' CLIs run in the shared workspace, so each installer
        # is isolated Python (-I), which never imports the workspace's modules.
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


#: How a member's installer for Claude or Codex begins. It checks that the
#: state directory ``config`` (argv 1) is the member's own, reads ``plugins``
#: (argv 2), defines bounded ``read_json`` and atomic ``write_json`` receipts,
#: and takes an exclusive lock on the directory. ``@DIRECTORY@`` and
#: ``@RECEIPT@`` name the directory and its receipts in errors.
_INSTALLER_PREAMBLE = r"""
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
    raise RuntimeError('@DIRECTORY@ is not member-owned')
os.umask(0o077)

def read_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise RuntimeError('@RECEIPT@ exceeds its bound')
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
"""


def member_installer(*, directory: str, receipt: str, locked: str) -> str:
    """The installer script: the shared preamble, then ``locked`` under the lock.

    ``locked`` is written at the left margin; it is indented into the lock's
    block here.
    """
    preamble = _INSTALLER_PREAMBLE.replace("@DIRECTORY@", directory)
    return preamble.replace("@RECEIPT@", receipt) + textwrap.indent(locked, "    ")


__all__ = [
    "PLUGIN_INSTALL_TIMEOUT_S",
    "member_installer",
    "run_plugin_command",
]
