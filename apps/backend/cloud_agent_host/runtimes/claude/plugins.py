"""Install native Claude Code marketplace plugins in a private member's config.

Claude Code's ``plugin marketplace add`` and ``plugin install --scope user``
maintain its own marketplace snapshots and enabled-plugin settings in the
member's ``CLAUDE_CONFIG_DIR``, which the resident session reads on startup.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent_host.plugins import member_installer, run_plugin_command

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec


_CLAUDE_INSTALL_SCRIPT = member_installer(
    directory="plugin config",
    receipt="plugin receipt",
    locked="""\
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
""",
)


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
        ["python3", "-I", "-c", _CLAUDE_INSTALL_SCRIPT, config_dir, json.dumps(plugins)],
        env=install_env,
        slot_id=slot_id,
    )


__all__ = ["install_claude_plugins"]
