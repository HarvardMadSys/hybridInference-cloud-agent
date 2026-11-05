"""What one CLI adds to a member's launch, beyond what every CLI shares.

Each CLI package's ``member`` module defines one :class:`MemberLaunch`, and
:func:`cloud_agent_host.runtimes.live.member_launch` finds it by runtime id.
The transport factory (:mod:`cloud_agent_host.resident_transport`) calls these
hooks at the points it always has and never branches on the runtime itself;
:class:`~cloud_agent_host.launch.runtime.RuntimeFacet` runs the runtime's
helpers at their place in the preparation order.

The defaults are a CLI with nothing of its own: no member config, no private
home, its downloaded release and MCP helpers only, no plugin installer, and
platform instructions it does not take.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from cloud_agent_host.drivers import DriverError
from cloud_agent_host.platform_instructions import platform_instructions

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
    from cloud_agent_host.launch.facet import PrepareContext
    from cloud_agent_host.launch.runtime import RuntimeFacet
    from cloud_agent_host.member_preparation import MemberPreparation
    from cloud_agent_host.resident_transport import ResidentLaunch


class MemberLaunch:
    """One CLI's own steps in a member's launch."""

    #: Whether the slot is reaped before the member's state is prepared even
    #: when the launch installs no plugins (the member's config is written
    #: by its own UID, which an earlier process must not race).
    reaps_before_preparation: ClassVar[bool] = False
    #: Whether the CLI reaches its MCP helpers on every launch, MCP or not.
    always_uses_mcp_helpers: ClassVar[bool] = False

    def prepare_member(
        self,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        launch: ResidentLaunch,
        batch: MemberPreparation,
    ) -> None:
        """Queue the member's own config on ``batch``. Nothing by default."""
        return

    def member_state_argv(
        self, manager: ChatSandboxManager, launch: ResidentLaunch, state_dir: str
    ) -> tuple[str, ...]:
        """The member command that creates its private provider home."""
        raise DriverError(f"runtime {launch.runtime_id!r} has no private member home")

    def install_helpers(self, context: PrepareContext, runtime: RuntimeFacet) -> None:
        """Install the selected runtime and its trusted adapters.

        A version an admin updated to after the image was built is copied in
        the first time; one the image has is not. The MCP helpers are
        refreshed from the host when resuming a retained container, so an
        older image's wrappers' replacements arrive without replacing its
        session state.
        """
        if runtime.runtime_version:
            context.manager.ensure_downloaded_runtime(
                context.spec, runtime.runtime_id, runtime.runtime_version
            )
        if runtime.mcp_server_ids or self.always_uses_mcp_helpers:
            context.manager.ensure_mcp_helpers(context.spec, runtime.runtime_id)

    def install_plugins(
        self,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        launch: ResidentLaunch,
        *,
        state_dir: str,
        env: dict[str, str],
    ) -> dict[str, str]:
        """Install the launch's plugins; return what the CLI's environment gains."""
        return {}

    def platform_instructions(self, launch: ResidentLaunch) -> str:
        """The platform's instructions for this member, asking for the co-author trailer."""
        return platform_instructions(launch, coauthor_trailer=True)

    def with_platform_instructions(
        self, argv: list[str], env: dict[str, str], instructions: str
    ) -> tuple[list[str], dict[str, str]]:
        """Hand ``instructions`` to the CLI the way it takes extra system instructions.

        No instructions change nothing; :meth:`place_instructions` places the rest.
        """
        if not instructions:
            return argv, env
        return self.place_instructions(argv, env, instructions)

    def place_instructions(
        self, argv: list[str], env: dict[str, str], instructions: str
    ) -> tuple[list[str], dict[str, str]]:
        """Where this CLI takes ``instructions``. A CLI that takes none is left as it was."""
        return argv, env

    def tool_hook_settings(self, launch: ResidentLaunch) -> dict[str, dict[str, str]]:
        """Settings the CLI's tool-hook merge adds to the platform's hooks."""
        return {}

    def config_dir(self, launch: ResidentLaunch) -> str | None:
        """The member-private config directory the CLI is started with, if it has one."""
        return None


def append_system_prompt(
    argv: list[str], env: dict[str, str], instructions: str
) -> tuple[list[str], dict[str, str]]:
    """``--append-system-prompt``, appended last so a resident session's slots stay put."""
    return [*argv, "--append-system-prompt", instructions], env


__all__ = ["MemberLaunch", "append_system_prompt"]
