"""An OpenCode/Kilo member's private XDG home, its launch steps, and its server locks.

What the transport factory (:mod:`cloud_agent_host.resident_transport`) asks
of the two HTTP-bridge CLIs alone: the member script that creates the XDG root
each CLI keeps its state in, the steps :data:`OPENCODE_LAUNCH` and
:data:`KILO_LAUNCH` add to a member's launch (Kilo's MCP runtime, npm plugins,
platform instructions through the wrapper), and the cleanup of the per-project
server lock a closed process leaves behind. The factory passes the sandbox
manager it holds explicitly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from cloud_agent_host.chat_sandbox import MEMBER_UID_BASE
from cloud_agent_host.drivers import DriverError
from cloud_agent_host.member_homes import (
    PRIVATE_MEMBER_HOME_PROLOGUE,
    legacy_member_state_dir,
    member_state_dir,
)
from cloud_agent_host.other_plugins import install_other_runtime_plugins
from cloud_agent_host.runtimes.http_bridge.kilo_mcp import KILO_MCP_VERSION
from cloud_agent_host.runtimes.member_launch import MemberLaunch

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
    from cloud_agent_host.launch.facet import PrepareContext
    from cloud_agent_host.launch.runtime import RuntimeFacet
    from cloud_agent_host.resident_transport import ResidentLaunch


class HttpBridgeMember(MemberLaunch):
    """OpenCode's steps, which Kilo shares: the live bridge, npm plugins, wrapper instructions."""

    always_uses_mcp_helpers = True

    def __init__(self, runtime_id: str) -> None:
        self.runtime_id = runtime_id

    @override
    def member_state_argv(
        self, manager: ChatSandboxManager, launch: ResidentLaunch, state_dir: str
    ) -> tuple[str, ...]:
        return member_state_argv(manager, launch, state_dir)

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
        """Their config's ``instructions`` file list, through the image's wrapper.

        The ``<name>-freeinference`` wrapper writes it from
        ``<NAME>_FREEINFERENCE_INSTRUCTIONS`` beside the config it already writes.
        """
        return argv, {**env, f"{self.runtime_id.upper()}_FREEINFERENCE_INSTRUCTIONS": instructions}


class KiloMember(HttpBridgeMember):
    """Kilo's steps: OpenCode's, and the deployment's isolated Kilo MCP runtime."""

    def __init__(self) -> None:
        super().__init__("kilo")

    @override
    def install_helpers(self, context: PrepareContext, runtime: RuntimeFacet) -> None:
        super().install_helpers(context, runtime)
        context.manager.ensure_kilo_mcp_runtime(
            context.spec, runtime.runtime_version or KILO_MCP_VERSION
        )


def member_state_argv(
    manager: ChatSandboxManager, launch: ResidentLaunch, state_dir: str
) -> tuple[str, ...]:
    """The member command that creates an isolated OpenCode/Kilo XDG root."""
    runtime_id = launch.runtime_id
    if runtime_id not in {"opencode", "kilo"}:
        raise DriverError(f"runtime {runtime_id!r} has no private HTTP state home")
    script = (
        'set -eu; target="$1"; runtime="$2"; legacy="$3"; trusted="$4"; '
        'marker="$target/.freeinference-member-state-v1"; '
        + PRIVATE_MEMBER_HOME_PROLOGUE
        + 'if test ! -e "$marker"; then '
        '  rm -rf -- "$target/$runtime/state/$runtime/locks"; '
        '  : > "$marker"; '
        "fi"
    )
    return (
        "/bin/sh",
        "-c",
        script,
        "sh",
        state_dir,
        runtime_id,
        legacy_member_state_dir(launch),
        str(manager.backend.uid),
    )


def clear_member_locks(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    launch: ResidentLaunch,
) -> None:
    """Remove dead OpenCode/Kilo server locks after closing that process.

    Both HTTP runtimes leave a per-project lock in their XDG state tree.
    The source process is intentionally restarted after native fork, so a
    lock that still names the closed PID must not survive into that new
    process generation.  Session history and databases are left intact.
    """
    runtime_id = launch.runtime_id
    if runtime_id not in {"opencode", "kilo"}:
        raise DriverError(f"runtime {runtime_id!r} has no HTTP member locks")
    state_dir = member_state_dir(launch)
    uid = MEMBER_UID_BASE + launch.slot_id
    result = manager.exec_capture(
        spec,
        [
            "/bin/sh",
            "-c",
            'set -eu; state="$1"; runtime="$2"; rm -rf -- "$state/$runtime/state/$runtime/locks"',
            "sh",
            state_dir,
            runtime_id,
        ],
        workdir="/",
        timeout_s=120,
        user_uid=uid,
        user_gid=manager.backend.gid,
    )
    if result.returncode != 0:
        raise DriverError(
            f"{runtime_id} stale member locks could not be cleared: "
            + result.stderr.decode("utf-8", "replace")[-300:]
        )


#: OpenCode's steps in a member's launch.
OPENCODE_LAUNCH = HttpBridgeMember("opencode")
#: Kilo's steps in a member's launch.
KILO_LAUNCH = KiloMember()
