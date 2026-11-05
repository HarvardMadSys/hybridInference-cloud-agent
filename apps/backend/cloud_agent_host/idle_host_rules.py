"""The running containers behind the host rules, and the rules' renewal on an empty host.

The periodic supervisor lists every exact running container, verifies the
host rules, and fail-stops those containers if repair fails. With nothing
running there is nothing to verify, but the next start or wake would find the
verification lapsed and install and read back the rules itself: about a second
of a cold start. So the sweep renews it while the host is empty too, at the
cost of the guard's helper runs about every 45 seconds.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_models import (
    CHAT_NAMESPACE_LABEL,
    CHAT_PROBE_LABEL,
    CHAT_SANDBOX_LABEL,
)
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_network import ChatNetworkContainers

#: The records keep the supervisor's logger name, where this renewal began.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")

_RUNNING_FORMAT = f'{{{{.ID}}}}\t{{{{.Label "{CHAT_PROBE_LABEL}"}}}}'


def running_deployment_container_ids(
    containers: ChatNetworkContainers,
) -> list[str] | None:
    """List exact running container ids, or ``None`` when Docker cannot answer."""
    try:
        result = containers._run(
            [
                containers.backend.docker_binary,
                "container",
                "ls",
                "--no-trunc",
                "--format",
                _RUNNING_FORMAT,
                "--filter",
                f"label={CHAT_SANDBOX_LABEL}=true",
                "--filter",
                f"label={CHAT_NAMESPACE_LABEL}={containers.backend.sandbox_namespace}",
            ],
            timeout=30,
        )
    except SandboxError:
        return None
    if result.returncode != 0:
        return None
    found: list[str] = []
    for line in result.stdout.splitlines():
        container_id, separator, probe = line.partition("\t")
        if not separator or not container_id:
            return None
        if not probe:
            found.append(container_id)
    return found


class IdleHostRules:
    """Renew the host rules' verification while nothing runs behind them.

    A failure is logged once until a renewal succeeds again, and stops
    nothing, as nothing runs. The next activation repeats the same check and
    refuses on its own.
    """

    def __init__(self, manager: ChatSandboxManager) -> None:
        self._manager = manager
        self._failing = False

    def renew(self) -> None:
        """Ask the network lifecycle to renew the verification if it is getting old."""
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
