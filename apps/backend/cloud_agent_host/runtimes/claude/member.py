"""A Claude member's private config, its launch steps, and its native Fork.

What the transport factory (:mod:`cloud_agent_host.resident_transport`) asks
of Claude alone: the member-private config that links only the group's native
project history, the steps :data:`LAUNCH` adds to a member's launch (plugins,
platform instructions, commit attribution), and the image's
``claude-session-fork`` helper that writes a child session at a historical
turn.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, override

from cloud_agent_host.container.chat_sandbox import (
    CHAT_WORKDIR,
    CLAUDE_SHARED_PROJECTS_DIR,
    MEMBER_UID_BASE,
    claude_member_config_dir,
)
from cloud_agent_host.container.sandbox import SANDBOX_PATH
from cloud_agent_host.drivers import DriverError
from cloud_agent_host.member_setup.member_preparation import MemberStep, run_member_steps
from cloud_agent_host.platform_instructions import platform_instructions
from cloud_agent_host.runtimes.claude.plugins import install_claude_plugins
from cloud_agent_host.runtimes.member_launch import MemberLaunch, append_system_prompt

if TYPE_CHECKING:
    from cloud_agent_host.container.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
    from cloud_agent_host.member_setup.member_preparation import MemberPreparation
    from cloud_agent_host.resident_transport import ResidentLaunch


class ClaudeMember(MemberLaunch):
    """Claude Code's steps: its private config, plugins, prompt and attribution."""

    reaps_before_preparation = True

    @override
    def prepare_member(
        self,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        launch: ResidentLaunch,
        batch: MemberPreparation,
    ) -> None:
        prepare_member_state(manager, spec, launch, batch)

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
        install_claude_plugins(
            manager,
            spec,
            launch.plugins,
            config_dir=claude_member_config_dir(launch.slot_id, launch.thread_id),
            env=env,
            slot_id=launch.slot_id,
        )
        return {}

    @override
    def platform_instructions(self, launch: ResidentLaunch) -> str:
        # Claude Code adds the trailer itself (``claude_attribution``); asked
        # to as well, it would write it twice.
        return platform_instructions(launch, coauthor_trailer=False)

    @override
    def place_instructions(
        self, argv: list[str], env: dict[str, str], instructions: str
    ) -> tuple[list[str], dict[str, str]]:
        return append_system_prompt(argv, env, instructions)

    @override
    def tool_hook_settings(self, launch: ResidentLaunch) -> dict[str, dict[str, str]]:
        # Claude's own commit attribution names the platform's co-author.
        return claude_attribution(launch)

    @override
    def config_dir(self, launch: ResidentLaunch) -> str | None:
        return claude_member_config_dir(launch.slot_id, launch.thread_id)


def claude_attribution(launch: ResidentLaunch) -> dict[str, dict[str, str]]:
    """Claude Code's own attribution settings, naming the platform's co-author.

    Claude Code adds its attribution to the commits and pull requests it
    writes; here the co-author is the deployment's, and the pull request
    description carries none. Empty without a co-author: Claude's defaults stand.
    """
    if not launch.git_coauthor:
        return {}
    return {"attribution": {"commit": f"Co-Authored-By: {launch.git_coauthor}", "pr": ""}}


def prepare_member_state(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    launch: ResidentLaunch,
    batch: MemberPreparation | None = None,
) -> None:
    """Share Claude native history while keeping launch input slot-private.

    Old retained containers already have projects under the shared Claude
    root. Link only that credential-free native history; never import its
    settings, environment overrides, credentials, or process registry.
    Member homes are image-created beneath root-owned /home, outside every
    shared-state permission-normalization traversal. Queued on ``batch``
    when one is given, and run now otherwise.
    """
    script = (
        "import os, pathlib, stat, sys\n"
        "config = pathlib.Path(sys.argv[1])\n"
        "shared = pathlib.Path(sys.argv[2])\n"
        "uid = os.getuid()\n"
        "for path in (config.parent.parent,):\n"
        "    info = path.lstat()\n"
        "    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:\n"
        "        raise RuntimeError('Claude member home is not private')\n"
        "    path.chmod(0o700)\n"
        "os.umask(0o077)\n"
        "for path in (config.parent, config):\n"
        "    path.mkdir(exist_ok=True)\n"
        "    info = path.lstat()\n"
        "    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:\n"
        "        raise RuntimeError('Claude config is not member-owned')\n"
        "    path.chmod(0o700)\n"
        "for path in (shared.parent, shared):\n"
        "    path.mkdir(mode=0o2770, exist_ok=True)\n"
        "    if not stat.S_ISDIR(path.lstat().st_mode):\n"
        "        raise RuntimeError('Claude history path is not a directory')\n"
        "    if path.stat().st_uid == uid:\n"
        "        path.chmod(0o2770)\n"
        "projects = config / 'projects'\n"
        "try:\n"
        "    projects.symlink_to(shared, target_is_directory=True)\n"
        "except FileExistsError:\n"
        "    if not projects.is_symlink() or os.readlink(projects) != str(shared):\n"
        "        raise RuntimeError('Claude member history link is invalid')\n"
    )
    step = MemberStep(
        (
            "python3",
            "-I",
            "-c",
            script,
            claude_member_config_dir(launch.slot_id, launch.thread_id),
            CLAUDE_SHARED_PROJECTS_DIR,
        ),
        timeout_s=30,
        failure=lambda stderr: DriverError(
            "Claude member state could not be prepared: " + stderr.decode("utf-8", "replace")[-300:]
        ),
    )
    run_member_steps(
        manager,
        spec,
        step,
        user_uid=MEMBER_UID_BASE + launch.slot_id,
        user_gid=manager.backend.gid,
        batch=batch,
    )


def fork_session(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    source_id: str,
    source_launch: ResidentLaunch,
    source_native_turn_no: int,
) -> str:
    """Write a child of ``source_id`` at its turn, as the source member; return its id."""
    uid = MEMBER_UID_BASE + source_launch.slot_id
    result = manager.exec_capture(
        spec,
        ["claude-session-fork", source_id, str(source_native_turn_no)],
        env={
            "HOME": f"/home/agent-member-{source_launch.slot_id}",
            "CLAUDE_CONFIG_DIR": claude_member_config_dir(
                source_launch.slot_id, source_launch.thread_id
            ),
            "PATH": SANDBOX_PATH,
            # Its interpreter is the image's Claude SDK venv, so the flag is
            # passed here: nothing in the member's home may load into it.
            "PYTHONNOUSERSITE": "1",
        },
        workdir=CHAT_WORKDIR,
        timeout_s=60,
        user_uid=uid,
        user_gid=manager.backend.gid,
    )
    if result.returncode != 0:
        raise DriverError(
            "Claude native fork failed: " + result.stderr.decode("utf-8", "replace")[:500]
        )
    try:
        receipt = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriverError("Claude native fork returned invalid JSON") from exc
    if not isinstance(receipt, dict) or receipt.get("source_session_id") != source_id:
        raise DriverError("Claude native fork returned the wrong source session")
    return str(receipt.get("session_id") or "")


#: Claude Code's steps in a member's launch.
LAUNCH = ClaudeMember()
