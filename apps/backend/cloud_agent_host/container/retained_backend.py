"""One retained chat container presented through the runner's sandbox contract.

:class:`RetainedChatBackend` lets runtime parsing, setup, patch building and
Agent CLI drivers execute with ``docker exec`` in a container
:class:`~cloud_agent_host.container.chat_sandbox.ChatSandboxManager` owns.
"""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Any

from cloud_agent_host.container.chat_sandbox_models import (
    ACTIVATION_TMPFS,
    CHAT_WORKDIR,
    ChatSandboxSpec,
)
from cloud_agent_host.container.sandbox import (
    DownloadsRead,
    SandboxBackend,
    SandboxError,
    SandboxProcess,
    SandboxSpec,
    TerminalProcess,
)
from cloud_agent_host.network.network_guard import GUARD_RECHECK_S

if TYPE_CHECKING:
    from cloud_agent_host.container.chat_sandbox import ChatSandboxManager
    from cloud_agent_shared.operation_profiling import StageObserver


#: How long a turn's commands rely on a proof that the group bridge is in
#: place before checking again: the minute the host guard already trusts its
#: own last verification for.
TURN_NETWORK_PROOF_S = GUARD_RECHECK_S


class RetainedChatBackend(SandboxBackend):
    """Present one retained container through the runner's sandbox contract.

    This adapter is what lets runtime parsing, setup, patch building and Agent
    CLI drivers execute with ``docker exec``. It never interprets a host
    ``workdir`` or ``state_dir``: both canonical paths already live in the
    container writable layer.
    """

    name = "retained-container"

    def __init__(
        self,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        *,
        network_proved_at: float | None = None,
    ) -> None:
        self.manager = manager
        self.spec = spec
        self.image = spec.sandbox_image_digest or manager.backend.image
        self.runtime = manager.backend.runtime
        # Monotonic time the group bridge was last proved, for a turn's or a
        # workspace operation's backend; ``None`` checks before every command.
        self._network_proved_at = network_proved_at

    @property
    def provides_isolation(self) -> bool:
        """Report the retained container as the outer execution boundary."""
        return True

    @property
    def is_vm_isolated(self) -> bool:
        """Mirror whether the underlying container runtime uses a guest VM."""
        return self.manager.backend.is_vm_isolated

    def sandbox_metadata(self) -> dict[str, str]:
        """Describe the retained lifecycle alongside backend isolation metadata."""
        return {
            **self.manager.backend.sandbox_metadata(),
            "sandbox_lifecycle": "restartable",
            "sandbox_id": self.spec.sandbox_id,
        }

    def version_stamp(self, *, downloads: DownloadsRead = "recent") -> dict[str, Any]:
        """Return a stamp that describes the retained image, not today's image."""
        stamp = self.manager.backend.version_stamp(downloads=downloads)
        if self.spec.sandbox_image_digest:
            stamp = {
                **stamp,
                "sandbox_image_digest": self.spec.sandbox_image_digest,
            }
        if self.spec.runtime_version:
            versions = dict(stamp.get("runtime_versions") or {})
            versions[self.spec.runtime_id] = self.spec.runtime_version
            available = dict(stamp.get("runtime_versions_available") or {})
            available[self.spec.runtime_id] = [self.spec.runtime_version]
            stamp = {
                **stamp,
                "runtime_versions": versions,
                "runtime_versions_available": available,
            }
        return stamp

    def base_env(self) -> dict[str, str]:
        """Return the underlying backend's hermetic sandbox environment."""
        return self.manager.backend.base_env()

    def has_binary(self, name: str) -> bool:
        """Check for one binary inside the exact retained container."""
        if not name or "\x00" in name:
            return False
        result = self.manager.exec_capture(
            self.spec,
            ["/bin/sh", "-c", 'command -v -- "$1" >/dev/null 2>&1', "sh", name],
            timeout_s=30,
        )
        return result.returncode == 0

    def require_network(self, *, observer: StageObserver | None = None) -> None:
        """Prove the container is on its guarded group bridge before running anything.

        A turn's backend starts from the proof its activation made: the sandbox manager
        checked exactly this when it acquired the turn, moments before. A
        workspace operation's backend starts from its admission's proof the
        same way. That proof, and each one this backend makes itself, stands for
        :data:`TURN_NETWORK_PROOF_S`; a command after that, such as one after a
        long setup script or at the end of a long turn, checks again. A backend
        built without a proof checks before every command.
        """
        proved_at = self._network_proved_at
        if proved_at is not None and time.monotonic() - proved_at < TURN_NETWORK_PROOF_S:
            return
        started = time.monotonic()
        self.manager.require_network(self.spec, observer=observer)
        if proved_at is not None:
            self._network_proved_at = started

    def spawn(self, spec: SandboxSpec) -> SandboxProcess:
        """Execute one Agent CLI or bounded command in the retained container."""
        self.require_network()
        pidfile = f"{ACTIVATION_TMPFS}/command-{secrets.token_hex(16)}.pid"
        return self.manager.spawn(
            self.spec,
            spec.argv,
            env=dict(spec.env),
            workdir=CHAT_WORKDIR,
            live_input=spec.live_input,
            user_uid=self.manager.backend.uid,
            user_gid=self.manager.backend.gid,
            process_pidfile=pidfile,
        )

    def spawn_environment_script(
        self, *, phase: str, script: str, env: dict[str, str]
    ) -> SandboxProcess:
        """Give install, services and verification independent non-root identities."""
        from cloud_agent_host.environment_process import EnvironmentProcess

        return EnvironmentProcess(self, phase=phase, script=script, env=env)

    def spawn_terminal(self, spec: SandboxSpec, *, rows: int, cols: int) -> TerminalProcess:
        """Open an interactive PTY in this chat's retained workspace."""
        if not (spec.workdir == CHAT_WORKDIR or spec.workdir.startswith(CHAT_WORKDIR + "/")):
            raise SandboxError("retained terminal must run in the chat workspace")
        return self.manager.spawn_terminal(
            self.spec,
            env=spec.env,
            workdir=spec.workdir,
            rows=rows,
            cols=cols,
            argv=spec.argv if len(spec.argv) > 1 else None,
            user_uid=int(spec.env["AGENT_ACTION_UID"]) if "AGENT_ACTION_UID" in spec.env else None,
        )

    def preflight(self) -> None:
        """Use the container backend checks without probing a host bind mount."""
        self.manager.backend.preflight()


__all__ = ["TURN_NETWORK_PROOF_S", "RetainedChatBackend"]
