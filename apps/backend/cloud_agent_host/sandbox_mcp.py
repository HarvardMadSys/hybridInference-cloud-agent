"""Admit a member's sandbox MCP servers to the sandbox's MCP gateway.

The gateway (``deploy/sandbox/cloud-agent-mcp``) runs the servers a user chose
to run inside the sandbox. Three identities keep an unreviewed server inside
its own member's slot:

- the admission, which the host writes as root, so no server can change it;
- the gateway, ``agent-mcp``, which serves the fixed address and runs nothing;
- the slot's runner, ``agent-mcp-<slot>``, which runs that slot's servers and
  answers only the gateway.

None is a member's UID, so a member's turn-end reap stops none of them, and a
member cannot switch to them: the host does all three, before each member
launch that names sandbox servers. The admission carries the digest of the
member's MCP credential, which the CLI sends to the gateway as it does to the
relay (cloud_agent_shared.sandbox_mcp).

An admission that fails leaves the member without its sandbox servers, not
without its chat: the CLI is still told of the ``sandbox`` server, finds it
refusing, and reports that server as failed, and the host logs why.

A launch that names no sandbox servers removes the slot's admission, so a
server the user turned off is not reachable by any CLI in the slot, even with
the credential an earlier launch was admitted with. The host skips that exec
only for a slot it has itself seen without an admission since it started; a
restarted host knows no slot, and revokes once per slot before it trusts that.
The gateway and the runner also ignore an admission written before the
container last started.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_models import MCP_RUNNER_UID_BASE, MCP_SERVICE_UID
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
    from cloud_agent_shared.sandbox_mcp import SandboxMcpServer

logger = logging.getLogger(__name__)

GATEWAY = "/usr/local/bin/cloud-agent-mcp"
#: Each step writes a file or, at most, starts a listener.
STEP_TIMEOUT_S = 30
_NO_GATEWAY = frozenset({126, 127})
ROOT_UID = 0

#: (container, slot) pairs this host saw left with no admission, by its own
#: revoke or by finding no gateway. Anything else is revoked before trusted.
_known_clean: set[tuple[str, int]] = set()
_known_clean_lock = threading.Lock()


def admission(credential: str, servers: tuple[SandboxMcpServer, ...]) -> bytes:
    """The gateway's admission document for one member."""
    return json.dumps(
        {
            "token_sha256": hashlib.sha256(credential.encode()).hexdigest(),
            "servers": [server.wire() for server in servers],
        },
        separators=(",", ":"),
    ).encode()


class _StepFailed(Exception):
    def __init__(self, step: str, status: int | None, detail: str) -> None:
        super().__init__(
            f"{step} failed" + (f" (status {status})" if status else "") + f": {detail}"
        )
        self.status = status


def _step(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    step: str,
    argv: list[str],
    uid: int,
    input_bytes: bytes | None = None,
) -> None:
    try:
        result = manager.exec_capture(
            spec,
            [GATEWAY, *argv],
            workdir="/",
            timeout_s=STEP_TIMEOUT_S,
            input_bytes=input_bytes,
            user_uid=uid,
            user_gid=0 if uid == ROOT_UID else manager.backend.gid,
        )
    except SandboxError as exc:
        raise _StepFailed(step, None, str(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace")[-300:].strip()
        raise _StepFailed(step, result.returncode, detail)


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
    with _known_clean_lock:
        if not servers and key in _known_clean:
            return False
        _known_clean.discard(key)
    document = admission(credential, servers)
    try:
        _step(manager, spec, "admission", ["admit", str(slot_id)], ROOT_UID, document)
        if not servers:
            with _known_clean_lock:
                _known_clean.add(key)
            return False
        # The gateway first: once it holds the fixed address, no server the
        # runner starts can take that address and stand in for it.
        _step(manager, spec, "gateway", ["service", "start"], MCP_SERVICE_UID)
        uid = MCP_RUNNER_UID_BASE + slot_id
        _step(manager, spec, "server runner", ["runner", "start", str(slot_id)], uid)
    except _StepFailed as exc:
        if exc.status in _NO_GATEWAY:
            # An image older than the gateway: nothing there to admit or revoke.
            with _known_clean_lock:
                _known_clean.add(key)
            if servers:
                logger.warning(
                    "sandbox MCP servers for %s: its image has no MCP gateway", spec.container_name
                )
            return False
        logger.warning("sandbox MCP for %s slot %s: %s", spec.container_name, slot_id, exc)
        return False
    return True
