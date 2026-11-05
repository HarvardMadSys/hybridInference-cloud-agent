"""A Codex member's private home, its launch steps, and recovery of a native Fork.

What the transport factory (:mod:`cloud_agent_host.resident_transport`) asks
of Codex alone: the member script that creates its home from the image's
bootstrap config, the steps :data:`LAUNCH` adds to a member's launch (its MCP
relay policy, plugins and developer instructions), and the bounded rollout
inventory that finds a Fork child whose ``thread/fork`` response was lost. The
factory passes the sandbox manager it holds explicitly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, override

from cloud_agent_host.chat_sandbox import CHAT_STATE_DIR, MEMBER_UID_BASE
from cloud_agent_host.drivers import DriverError
from cloud_agent_host.member_homes import (
    PRIVATE_MEMBER_HOME_PROLOGUE,
    legacy_member_state_dir,
    member_state_dir,
)
from cloud_agent_host.runtimes.codex.plugins import install_codex_plugins
from cloud_agent_host.runtimes.member_launch import MemberLaunch

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
    from cloud_agent_host.launch.facet import PrepareContext
    from cloud_agent_host.launch.runtime import RuntimeFacet
    from cloud_agent_host.resident_transport import ResidentLaunch


class CodexMember(MemberLaunch):
    """Codex's steps: its private home, MCP relay policy, plugins and instructions."""

    @override
    def member_state_argv(
        self, manager: ChatSandboxManager, launch: ResidentLaunch, state_dir: str
    ) -> tuple[str, ...]:
        return member_state_argv(manager, launch, state_dir)

    @override
    def install_helpers(self, context: PrepareContext, runtime: RuntimeFacet) -> None:
        super().install_helpers(context, runtime)
        if runtime.mcp_server_ids:
            context.manager.ensure_codex_mcp_policy(context.spec, runtime.mcp_base_url)

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
        install_codex_plugins(
            manager,
            spec,
            launch.plugins,
            state_dir=state_dir,
            env=env,
            slot_id=launch.slot_id,
        )
        return {}

    @override
    def place_instructions(
        self, argv: list[str], env: dict[str, str], instructions: str
    ) -> tuple[list[str], dict[str, str]]:
        """The ``developer_instructions`` key, as a ``-c`` override before ``app-server``.

        A JSON string is a TOML basic string.
        """
        if "app-server" not in argv:
            return argv, env
        at = argv.index("app-server")
        option = ["-c", f"developer_instructions={json.dumps(instructions)}"]
        return [*argv[:at], *option, *argv[at:]], env


def member_state_argv(
    manager: ChatSandboxManager, launch: ResidentLaunch, state_dir: str
) -> tuple[str, ...]:
    """The member command that creates its Codex home from the image's bootstrap config."""
    script = (
        'set -eu; source="$1"; target="$2"; legacy="$3"; trusted="$4"; '
        'marker="$target/.freeinference-member-state-v1"; '
        + PRIVATE_MEMBER_HOME_PROLOGUE
        + 'if test ! -e "$marker"; then '
        '  if test -f "$source/config.toml"; then '
        '    cp -- "$source/config.toml" "$target/config.toml"; '
        "  fi; "
        '  mkdir -p -- "$target/tmp" "$target/.tmp"; : > "$marker"; '
        "fi"
    )
    return (
        "/bin/sh",
        "-c",
        script,
        "sh",
        CHAT_STATE_DIR,
        state_dir,
        legacy_member_state_dir(launch),
        str(manager.backend.uid),
    )


def recover_fork(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    source_launch: ResidentLaunch,
    *,
    source_id: str,
    baseline: set[str],
) -> str:
    """The one child a preparing journal's lost ``thread/fork`` left, or an error."""
    recovered = new_fork_children(
        manager,
        spec,
        source_launch,
        source_id=source_id,
        baseline=baseline,
    )
    if len(recovered) != 1:
        detail = "no child is visible" if not recovered else "multiple children are visible"
        raise DriverError(f"Codex native fork preparing journal is ambiguous: {detail}")
    return next(iter(recovered))


def new_fork_children(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    source_launch: ResidentLaunch,
    *,
    source_id: str,
    baseline: set[str],
) -> set[str]:
    """Children of ``source_id`` that were not there at ``baseline``."""
    return (
        fork_children(
            manager,
            spec,
            source_launch,
            source_id=source_id,
        )
        - baseline
    )


def fork_children(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    source_launch: ResidentLaunch,
    *,
    source_id: str,
) -> set[str]:
    """Read bounded Codex rollout metadata for crash-safe fork recovery."""
    state_dir = member_state_dir(source_launch)
    script = """
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]) / "sessions"
source_id = sys.argv[2]
children = set()
seen = 0
if root.is_dir():
    for path in root.rglob("*.jsonl"):
        seen += 1
        if seen > 100000:
            raise SystemExit(42)
        try:
            with path.open("r", encoding="utf-8") as handle:
                first = handle.readline(1_000_001)
            if len(first) > 1_000_000:
                raise SystemExit(43)
            item = json.loads(first)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(payload, dict) or payload.get("forked_from_id") != source_id:
            continue
        child_id = payload.get("session_id") or payload.get("id")
        if isinstance(child_id, str) and child_id and len(child_id) <= 256:
            children.add(child_id)
        if len(children) > 4096:
            raise SystemExit(44)
print(json.dumps(sorted(children), separators=(",", ":")))
"""
    result = manager.exec_capture(
        spec,
        ["python3", "-I", "-c", script, state_dir, source_id],
        workdir="/",
        timeout_s=30,
        user_uid=MEMBER_UID_BASE + source_launch.slot_id,
        user_gid=manager.backend.gid,
    )
    if result.returncode != 0:
        raise DriverError(
            "Codex fork recovery inventory failed: "
            + result.stderr.decode("utf-8", "replace")[-300:]
        )
    try:
        value = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriverError("Codex fork recovery inventory returned invalid JSON") from exc
    if (
        not isinstance(value, list)
        or len(value) > 4096
        or any(not isinstance(item, str) or not item or len(item) > 256 for item in value)
    ):
        raise DriverError("Codex fork recovery inventory has an invalid shape")
    return set(value)


def journal_baseline(journal: dict[str, object]) -> set[str]:
    """The children a preparing journal saw before its ``thread/fork`` was sent."""
    value = journal.get("source_child_session_ids")
    if (
        not isinstance(value, list)
        or len(value) > 4096
        or any(not isinstance(item, str) or not item or len(item) > 256 for item in value)
    ):
        raise DriverError("Codex native fork journal has no valid recovery baseline")
    if len(set(value)) != len(value):
        raise DriverError("Codex native fork journal recovery baseline is not canonical")
    return set(value)


#: Codex's steps in a member's launch.
LAUNCH = CodexMember()
