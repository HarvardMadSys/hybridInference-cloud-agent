"""Install native Codex marketplace plugins in a private member's CODEX_HOME.

Codex 0.145.0 exposes ``plugin marketplace add`` and ``plugin add``. These
commands maintain its own marketplace snapshots, cache, and enabled-plugin
config; the resident app-server reads that same member config on startup.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent_host.plugins import member_installer, run_plugin_command

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec


_CODEX_INSTALL_SCRIPT = member_installer(
    directory="Codex plugin home",
    receipt="Codex plugin receipt",
    locked="""\
selection = {'state_dir': str(config), 'plugins': plugins}
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
""",
)


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
