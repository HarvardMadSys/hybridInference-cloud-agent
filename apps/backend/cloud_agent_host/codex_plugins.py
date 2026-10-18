"""Install native Codex marketplace plugins in a private member's CODEX_HOME.

Codex 0.145.0 exposes ``plugin marketplace add`` and ``plugin add``. These
commands maintain its own marketplace snapshots, cache, and enabled-plugin
config; the resident app-server reads that same member config on startup.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent_host.plugins import run_plugin_command

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec


_CODEX_INSTALL_SCRIPT = r"""
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
selection = {'state_dir': str(config), 'plugins': plugins}
info = config.lstat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
    raise RuntimeError('Codex plugin home is not member-owned')
os.umask(0o077)

def read_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise RuntimeError('Codex plugin receipt exceeds its bound')
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
    if read_json(marker) == selection:
        raise SystemExit(0)
    progress_path = config / '.freeinference-plugin-progress.json'
    progress = read_json(progress_path)
    if not isinstance(progress, dict) or progress.get('state_dir') != str(config):
        progress = {}
    marketplaces = progress.get('marketplaces', [])
    installed = progress.get('plugins', [])
    if not isinstance(marketplaces, list) or not isinstance(installed, list):
        raise RuntimeError('Codex plugin progress receipt is invalid')
    for plugin in plugins:
        source = plugin['marketplace']
        if source not in marketplaces:
            subprocess.run(['codex', '-c', 'features.plugins=true',
                            'plugin', 'marketplace', 'add', source, '--json'], check=True)
            marketplaces.append(source)
            write_json(progress_path, {'state_dir': str(config),
                                      'marketplaces': marketplaces, 'plugins': installed})
        if plugin not in installed:
            subprocess.run(['codex', '-c', 'features.plugins=true',
                            'plugin', 'add', plugin['plugin'], '--json'], check=True)
            installed.append(plugin)
            write_json(progress_path, {'state_dir': str(config),
                                      'marketplaces': marketplaces, 'plugins': installed})
    write_json(marker, selection)
"""


def install_codex_plugins(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    plugins: tuple[dict[str, str], ...],
    *,
    state_dir: str,
    env: dict[str, str],
    slot_id: int,
) -> None:
    """Install selected plugins before launch, using a credential-free base env.

    The native commands retain Codex's root-owned system requirements. Install
    receipts make successful selections idempotent and retry partial failures
    without reinstalling completed marketplace or plugin operations.
    """
    if not plugins:
        return
    run_plugin_command(
        manager,
        spec,
        ["python3", "-I", "-c", _CODEX_INSTALL_SCRIPT, state_dir, json.dumps(plugins)],
        env={**env, "CODEX_HOME": state_dir, "CI": "1"},
        slot_id=slot_id,
    )


__all__ = ["install_codex_plugins"]
