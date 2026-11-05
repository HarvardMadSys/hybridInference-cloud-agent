"""An OpenCode/Kilo member's private XDG home, and its server locks.

What the transport factory (:mod:`cloud_agent_host.resident_transport`) asks
of the two HTTP-bridge CLIs alone: the member script that creates the XDG root
each CLI keeps its state in, and the cleanup of the per-project server lock a
closed process leaves behind. The factory passes the sandbox manager it holds
explicitly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox import MEMBER_UID_BASE
from cloud_agent_host.drivers import DriverError
from cloud_agent_host.member_homes import (
    PRIVATE_MEMBER_HOME_PROLOGUE,
    legacy_member_state_dir,
    member_state_dir,
)

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
    from cloud_agent_host.resident_transport import ResidentLaunch


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
