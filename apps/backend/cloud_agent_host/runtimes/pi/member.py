"""A pi member's launch steps.

pi keeps no private provider home and no member config of its own: what
:data:`LAUNCH` adds to a member's launch is its MCP bridge on every launch, its
npm plugins, and platform instructions on its system prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from cloud_agent_host.other_plugins import install_other_runtime_plugins
from cloud_agent_host.runtimes.member_launch import MemberLaunch, append_system_prompt

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
    from cloud_agent_host.resident_transport import ResidentLaunch


class PiMember(MemberLaunch):
    """pi's steps: its wrapper and MCP bridge, npm plugins and system prompt."""

    always_uses_mcp_helpers = True

    @override
    def install_plugins(
        self,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        launch: ResidentLaunch,
        *,
        state_dir: str,
        env: dict[str, str],
    ) -> dict[str, str]:
        return install_other_runtime_plugins(
            manager,
            spec,
            runtime_id=launch.runtime_id,
            plugins=launch.plugins,
            env=env,
            slot_id=launch.slot_id,
        )

    @override
    def place_instructions(
        self, argv: list[str], env: dict[str, str], instructions: str
    ) -> tuple[list[str], dict[str, str]]:
        return append_system_prompt(argv, env, instructions)


#: pi's steps in a member's launch.
LAUNCH = PiMember()
