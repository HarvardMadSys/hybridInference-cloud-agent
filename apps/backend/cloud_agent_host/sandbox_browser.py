"""The sandbox's shared browser service (browser contract v1).

The sandbox image ships Chromium and ``cloud-agent-browser``
(docs/agents/specs/2026-11-02-computer-use-base-capability.md). One browser
serves a whole sandbox group: the chat and its Forks share its tabs and its
sign-ins, as they share /workspace and as every member belongs to the same
owner.

It runs as its own user, ``agent-browser``, for two reasons. A member's turn
ends with a reap of every process its UID started during the turn, which
would take the browser with it. And the browser outlives any one member, so
it cannot be one member's process at all. A member cannot switch UID inside
the sandbox, so the host starts the service, before each member launch, which
also covers a container that was stopped and started again. The service is a
small loopback server; Chromium itself starts only when a member's tool first
connects, and stops again when no tool has been connected for a while.

Starting it is best effort. A sandbox without the browser keeps working: an
image older than the browser has no launcher, and any other failure is
logged, while the member's ``cloud-agent-browser start`` reports it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_models import BROWSER_SERVICE_UID
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec

logger = logging.getLogger(__name__)

LAUNCHER = "/usr/local/bin/cloud-agent-browser"
#: The service answers once it serves; it does not wait for Chromium.
SERVICE_START_TIMEOUT_S = 30
#: ``docker exec``'s statuses for a command the image cannot run or does not have.
_NO_LAUNCHER = frozenset({126, 127})


def ensure_browser_service(manager: ChatSandboxManager, spec: ChatSandboxSpec) -> None:
    """Start the group's browser service unless it runs; log, not raise, a failure."""
    try:
        result = manager.exec_capture(
            spec,
            [LAUNCHER, "service", "start"],
            workdir="/",
            timeout_s=SERVICE_START_TIMEOUT_S,
            user_uid=BROWSER_SERVICE_UID,
            user_gid=manager.backend.gid,
        )
    except SandboxError as exc:
        logger.warning("sandbox browser service for %s did not start: %s", spec.container_name, exc)
        return
    if result.returncode == 0 or result.returncode in _NO_LAUNCHER:
        return
    logger.warning(
        "sandbox browser service for %s did not start (status %s): %s",
        spec.container_name,
        result.returncode,
        result.stderr.decode("utf-8", "replace")[-300:].strip(),
    )
