"""Admit a member's sandbox MCP servers to the sandbox's MCP gateway.

The gateway (``deploy/sandbox/cloud-agent-mcp``) runs the servers a user chose
to run inside the sandbox, as its own user, ``agent-mcp``: a member's turn-end
reap does not stop them, and a member cannot switch to that UID, so the host
admits them. It does so before each member launch, with the servers that
launch names and a digest of the member's MCP credential, which the CLI then
sends to the gateway as it does to the relay
(cloud_agent_shared.sandbox_mcp).

An admission that fails leaves the member without its sandbox servers, not
without its chat: the CLI is still told of the ``sandbox`` server, finds it
refusing, and reports that server as failed, and the host logs why.

A launch that names no sandbox servers removes the slot's earlier admission,
so a server the user turned off is not left reachable by the next CLI in the
slot. Only a slot this host admitted needs that exec, so a chat that never
chose a sandbox server costs none. The gateway forgets every admission when
it starts, which covers a stopped container; a host restarted while its
containers kept running keeps the admissions it made before until each slot's
next launch with servers, or the container's next stop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_models import MCP_SERVICE_UID
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
    from cloud_agent_shared.sandbox_mcp import SandboxMcpServer

logger = logging.getLogger(__name__)

GATEWAY = "/usr/local/bin/cloud-agent-mcp"
#: The admission writes a file and, at most, starts the gateway's listener.
ADMIT_TIMEOUT_S = 30
_NO_GATEWAY = frozenset({126, 127})

#: (container, slot) pairs this host admitted servers for and has not revoked.
_admitted: set[tuple[str, int]] = set()
_admitted_lock = threading.Lock()


def admission(credential: str, servers: tuple[SandboxMcpServer, ...]) -> bytes:
    """The gateway's admission document for one member."""
    return json.dumps(
        {
            "token_sha256": hashlib.sha256(credential.encode()).hexdigest(),
            "servers": [server.wire() for server in servers],
        },
        separators=(",", ":"),
    ).encode()


def admit_sandbox_mcp(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    slot_id: int,
    credential: str,
    servers: tuple[SandboxMcpServer, ...],
) -> bool:
    """Replace one member's admission; whether its servers are now served. Never raises."""
    if servers and not credential:
        logger.warning(
            "sandbox MCP servers for %s slot %s need an MCP credential; none was issued",
            spec.container_name,
            slot_id,
        )
        servers = ()
    key = (spec.container_name, slot_id)
    if not servers:
        with _admitted_lock:
            if key not in _admitted:
                return False
    try:
        result = manager.exec_capture(
            spec,
            [GATEWAY, "admit", str(slot_id)],
            workdir="/",
            timeout_s=ADMIT_TIMEOUT_S,
            input_bytes=admission(credential, servers),
            user_uid=MCP_SERVICE_UID,
            user_gid=manager.backend.gid,
        )
    except SandboxError as exc:
        logger.warning(
            "sandbox MCP admission for %s slot %s failed: %s", spec.container_name, slot_id, exc
        )
        return False
    if result.returncode == 0:
        with _admitted_lock:
            if servers:
                _admitted.add(key)
            else:
                _admitted.discard(key)
        return bool(servers)
    if result.returncode in _NO_GATEWAY:
        if servers:
            logger.warning(
                "sandbox MCP servers for %s: its image has no MCP gateway", spec.container_name
            )
        return False
    logger.warning(
        "sandbox MCP admission for %s slot %s failed (status %s): %s",
        spec.container_name,
        slot_id,
        result.returncode,
        result.stderr.decode("utf-8", "replace")[-300:].strip(),
    )
    return False
