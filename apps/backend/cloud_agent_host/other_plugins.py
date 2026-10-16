"""Install selected npm plugins for pi, OpenCode, and Kilo before launch."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from cloud_agent_host.plugins import run_plugin_command

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec

PLUGIN_PATHS_ENV = "AGENT_PLUGIN_PATHS"

# Each package has its own member-owned prefix: versions of one package cannot
# overwrite one another, and a failed install is retried without marking it as
# complete. Neither npm nor pi sees the shared repository's settings here.
_INSTALL_SCRIPT = r"""
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import tempfile

runtime, raw_plugins, home = sys.argv[1:]
plugins = json.loads(raw_plugins)
os.umask(0o077)
base = pathlib.Path(home) / '.agent-plugins' / runtime
base.mkdir(parents=True, exist_ok=True)
if base.is_symlink() or base.stat().st_uid != os.getuid():
    raise RuntimeError('plugin prefix is not member-owned')
lock_fd = os.open(base / '.install.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
with os.fdopen(lock_fd, 'r+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    for plugin in plugins:
        root = base / plugin['key']
        root.mkdir(exist_ok=True)
        if root.is_symlink() or root.stat().st_uid != os.getuid():
            raise RuntimeError('plugin package prefix is not member-owned')
        spec, name = plugin['spec'], plugin['name']
        marker = root / '.complete.json'
        package = root / ('lib/node_modules' if runtime == 'pi' else 'node_modules') / name
        selected = package if runtime == 'pi' else root / 'entry.mjs'
        receipt = {'version': 1, 'spec': spec}
        try:
            fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as handle:
                complete = json.load(handle) == receipt and selected.exists()
        except (FileNotFoundError, ValueError):
            complete = False
        if complete:
            continue
        install_env = dict(os.environ)
        install_env.update({
            'npm_config_cache': str(pathlib.Path(home) / '.npm'),
            'npm_config_update_notifier': 'false',
            'npm_config_audit': 'false',
            'npm_config_fund': 'false',
        })
        if runtime == 'pi':
            install_env['npm_config_prefix'] = str(root)
            install_env['PI_CODING_AGENT_DIR'] = str(root / 'pi-config')
            subprocess.run(['pi', 'install', 'npm:' + spec], env=install_env,
                           cwd=home, check=True)
            # pi 0.75.0 moved its own installs from npm's global prefix into
            # the managed project in its config directory. The path handed to
            # the CLI stays the same for every release: link it there.
            managed = root / 'pi-config' / 'npm' / 'node_modules' / name
            if not package.exists() and (managed / 'package.json').is_file():
                if package.is_symlink():
                    package.unlink()
                package.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(os.path.relpath(managed, package.parent), package)
        else:
            subprocess.run(['npm', 'install', '--prefix', str(root), '--no-audit',
                            '--no-fund', '--save-exact', spec], env=install_env,
                           cwd=home, check=True)
        if not (package / 'package.json').is_file():
            raise RuntimeError('installed plugin has no package manifest: ' + spec)
        if runtime != 'pi':
            with (package / 'package.json').open() as handle:
                manifest = json.load(handle)
            # Matches the pinned CLIs' plugin/shared.ts server entry selection:
            # exports["./server"] (import/default), then main, then a local index.
            exports = manifest.get('exports')
            entry = exports.get('./server') if isinstance(exports, dict) else None
            if isinstance(entry, dict):
                entry = next((entry[key] for key in ('import', 'default')
                              if isinstance(entry.get(key), str)), None)
            if not isinstance(entry, str) or not entry:
                entry = manifest.get('main')
            if not isinstance(entry, str) or not entry:
                entry = next((p for p in ('index.ts', 'index.tsx', 'index.js',
                                          'index.mjs', 'index.cjs')
                              if (package / p).is_file()), None)
            if not entry:
                raise RuntimeError('plugin has no server entrypoint: ' + spec)
            entry_path = (package / entry).resolve()
            if not entry_path.is_relative_to(package.resolve()) or not entry_path.is_file():
                raise RuntimeError('plugin server entrypoint is invalid: ' + spec)
            source = json.dumps(entry_path.as_uri())
            # A native v1 npm module may omit id; local modules require it.
            # Preserve every named export and legacy default function while
            # supplying the same package-name identity for a v1 default object.
            # The no-op default supports legacy modules with named exports only.
            selected.write_text(
                'export * from ' + source + ';\n'
                'import * as original from ' + source + ';\n'
                'const value = original.default;\n'
                'export default value && typeof value === "object" && '
                '("server" in value || "tui" in value || "id" in value)\n'
                '  ? {...value, id: value.id === undefined ? ' + json.dumps(name) + ' : value.id}\n'
                '  : value === undefined ? async () => ({}) : value;\n'
            )
        fd, filename = tempfile.mkstemp(prefix='.complete.', dir=root)
        with os.fdopen(fd, 'w') as handle:
            json.dump(receipt, handle)
        os.replace(filename, marker)
"""


def install_other_runtime_plugins(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    runtime_id: str,
    plugins: tuple[dict[str, str], ...],
    env: dict[str, str],
    slot_id: int,
) -> dict[str, str]:
    """Return explicit loading paths after every package installed successfully."""
    if not plugins:
        return {}
    if runtime_id not in {"pi", "opencode", "kilo"}:
        raise ValueError(f"No npm plugin installer for runtime {runtime_id!r}")
    home = f"/home/agent-member-{slot_id}"
    selections = []
    paths = []
    for plugin in plugins:
        package_spec = plugin["plugin"]
        separator = package_spec.rfind("@")
        name = package_spec[:separator] if separator > 0 else package_spec
        key = hashlib.sha256(package_spec.encode()).hexdigest()
        selections.append({"spec": package_spec, "name": name, "key": key})
        root = f"{home}/.agent-plugins/{runtime_id}/{key}"
        paths.append(
            f"{root}/lib/node_modules/{name}" if runtime_id == "pi" else f"{root}/entry.mjs"
        )
    run_plugin_command(
        manager,
        spec,
        ["python3", "-I", "-c", _INSTALL_SCRIPT, runtime_id, json.dumps(selections), home],
        env={**env, "HOME": home, "CI": "1", "GIT_TERMINAL_PROMPT": "0"},
        slot_id=slot_id,
    )
    return {PLUGIN_PATHS_ENV: json.dumps(paths)}


__all__ = ["PLUGIN_PATHS_ENV", "install_other_runtime_plugins"]
