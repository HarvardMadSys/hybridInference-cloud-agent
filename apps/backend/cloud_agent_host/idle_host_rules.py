"""The sweep's renewal of the host rules while no chat group is running.

The sweep reads the rules back on every pass for each running group. With
none running, a host can still have something behind its packet boundary: a
group that is stopping, which keeps running through its archive and ``docker
stop``, and environment tests and prebuilds, which no group lists. A firewall
reload under them would otherwise stay unrepaired until a chat next starts.
So while anything of this deployment runs, the sweep keeps the rules renewed,
as it did on every idle pass before #944.

A host where nothing of this deployment runs has nothing behind the rules. It
starts no guard helper: the next start or wake renews a lapsed verification
before its group joins a bridge.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_models import CHAT_NAMESPACE_LABEL, CHAT_SANDBOX_LABEL
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_network import ChatNetworkContainers

#: The records keep the supervisor's logger name, where this renewal began.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")


def deployment_running(containers: ChatNetworkContainers) -> bool:
    """Whether Docker shows any container of this deployment running.

    Chat groups, stopping ones included, and environment tests and prebuilds
    all carry the chat sandbox label and this deployment's namespace. Listing
    running containers by label reads only the daemon's records, so a wedged
    Kata shim cannot hold it up. A listing that fails counts as running, so the
    rules stay renewed.
    """
    try:
        result = containers._run(
            [
                containers.backend.docker_binary,
                "container",
                "ls",
                "--quiet",
                "--filter",
                f"label={CHAT_SANDBOX_LABEL}=true",
                "--filter",
                f"label={CHAT_NAMESPACE_LABEL}={containers.backend.sandbox_namespace}",
            ],
            timeout=30,
        )
    except SandboxError:
        return True
    return result.returncode != 0 or bool(result.stdout.strip())


class IdleHostRules:
    """Renew the host rules on a sweep pass that found no running group.

    A failure is logged once until a renewal succeeds again, and stops
    nothing: the next activation repeats the same check and refuses on its own.
    """

    def __init__(self, manager: ChatSandboxManager) -> None:
        self._manager = manager
        self._failing = False

    def renew(self) -> None:
        """Ask the network lifecycle to renew, which it does only while anything runs."""
        try:
            self._manager.network.renew_host_rules()
        except SandboxError as exc:
            if not self._failing:
                logger.warning(
                    "shared_chat_idle_host_rules_unavailable",
                    extra={
                        "event": "shared_chat_idle_host_rules_unavailable",
                        "detail": str(exc)[:300],
                    },
                )
            self._failing = True
        else:
            self._failing = False
