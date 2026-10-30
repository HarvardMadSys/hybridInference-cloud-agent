"""This control-plane process's report about itself, and the fleet view built from all of them.

Every API and MCP listener process reports, every few seconds, the build it
runs, the capabilities it has, the settings revision it has adopted and which
restart-only settings it is still running with an older value. Its database
connections carry ``application_name = cloud-agent:<role>:<instance>``, so a
session on the database that no reporting process opened can be listed.

What the reports prove is limited and the code says so: a process from an
older release reports nothing and is visible only as an unidentified database
session, and a stale report is a process that may be gone or may be stuck.
Neither is ever counted as adopted or as supporting a capability.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cloud_agent_shared.deployment_contract import key_identifier

if TYPE_CHECKING:
    from cloud_agent.ports import InstanceReports as InstanceRepository

#: What this release can do that a mixed fleet must agree on before it is used.
#: ``instance-report-v1`` is reporting itself; ``model-connections-v1`` runs an
#: owner's own model connection instead of the Gateway.
CONTROL_PLANE_CAPABILITIES: tuple[str, ...] = (
    "instance-report-v1",
    "model-connections-v1",
    # Decrypts with AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS during a root key
    # rotation and reports both keys' identifiers.
    "settings-key-rotation-v1",
    # Decrypts with a credential key's previous key during its rotation and
    # builds lookup digests with the key the rotation names.
    "credential-key-rotation-v1",
    # Accepts a host's renewed credential (promoting it on first use) and the
    # one it replaced for a short grace; issues renewals at heartbeats.
    "host-credential-renewal-v1",
)

#: Saved settings a process reads only when it builds a component, by the
#: roles that build it. A process still running the value it started with
#: reports the key as restart-required.
RESTART_ROLES: dict[str, frozenset[str]] = {
    "mcp_allow_private_targets": frozenset({"mcp"}),
    "mcp_allow_plain_http": frozenset({"mcp"}),
}

REPORT_INTERVAL_S = 5.0


def root_key_state() -> dict[str, str]:
    """Which settings root key this process decrypts with, never the key."""
    dedicated = os.environ.get("AGENT_SETTINGS_ENCRYPTION_KEY") or ""
    fallback = os.environ.get("AGENT_SESSION_SECRET") or ""
    if dedicated:
        source, key = "dedicated", dedicated
    elif fallback:
        source, key = "fallback", fallback
    else:
        source, key = "missing", ""
    state = {"root_key_source": source, "root_key_id": key_identifier(key)}
    previous = os.environ.get("AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS") or ""
    if previous:
        state["root_key_previous_id"] = key_identifier(previous)
    return state


@dataclass
class ProcessIdentity:
    """Who this process is, fixed for its lifetime."""

    instance_id: str
    role: str
    machine: str
    build_sha: str
    started_at: datetime

    @property
    def application_name(self) -> str:
        """The name its database sessions carry (at most 63 bytes)."""
        return f"cloud-agent:{self.role}:{self.instance_id}"[:63]


_identity: ProcessIdentity | None = None


def process_identity(role: str = "api") -> ProcessIdentity:
    """This process's identity, created on first use."""
    global _identity
    if _identity is None or _identity.role != role:
        _identity = ProcessIdentity(
            instance_id=uuid.uuid4().hex,
            role=role,
            machine=(os.environ.get("AGENT_CONTROL_PLANE_MACHINE") or socket.gethostname())[:255],
            build_sha=(os.environ.get("AGENT_DEPLOYMENT_SHA") or "").strip()[:64],
            started_at=datetime.now(UTC),
        )
    return _identity


class RestartTracker:
    """The restart-only settings this process still runs with their startup value."""

    def __init__(self, role: str) -> None:
        """Remember the values this process built its components with."""
        from cloud_agent.settings import get_setting

        self._role = role
        self._startup = {
            key: get_setting(key) for key, roles in RESTART_ROLES.items() if role in roles
        }

    def pending(self) -> list[str]:
        """Keys whose adopted value differs from the one built in at startup."""
        from cloud_agent.settings import get_setting

        return sorted(key for key, value in self._startup.items() if get_setting(key) != value)


def report_details() -> dict[str, Any]:
    """Evidence beside the revision: one-way identifiers of the keys this process holds."""
    return {
        **root_key_state(),
        "admin_token_id": key_identifier((os.environ.get("ADMIN_TOKEN") or "").strip()),
        "database_url_id": key_identifier(os.environ.get("AGENT_DATABASE_URL") or ""),
    }


async def report_forever(repository: InstanceRepository, role: str) -> None:
    """Report this process every few seconds until cancelled; failures retry."""
    from cloud_agent.logging_utils import get_logger
    from cloud_agent.settings import adopted_revision

    logger = get_logger(__name__)
    identity = process_identity(role)
    tracker = RestartTracker(role)
    try:
        while True:
            try:
                await repository.report(
                    instance_id=identity.instance_id,
                    role=identity.role,
                    machine=identity.machine,
                    build_sha=identity.build_sha,
                    capabilities=CONTROL_PLANE_CAPABILITIES,
                    adopted_settings_revision=adopted_revision(),
                    restart_required=tracker.pending(),
                    details=report_details(),
                    started_at=identity.started_at,
                )
            except Exception as exc:
                logger.warning("instance report failed (%s)", type(exc).__name__)
            await asyncio.sleep(REPORT_INTERVAL_S)
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(repository.stop(identity.instance_id), timeout=2)
        except Exception:
            logger.debug("instance stop report failed", exc_info=True)
        raise


@dataclass(frozen=True)
class FleetVerdict:
    """Whether every participant is shown to support something, and why not."""

    supported: bool
    current: list[dict[str, Any]]
    missing: list[str]
    stale: list[str]
    unidentified_sessions: list[dict[str, Any]]

    def reason(self) -> str:
        """One sentence an administrator can act on; empty when supported."""
        parts = []
        if not self.current:
            parts.append("no control-plane process has reported")
        if self.missing:
            parts.append("these processes do not support it: " + ", ".join(self.missing[:10]))
        if self.stale:
            parts.append(
                "these processes stopped reporting without a clean stop: "
                + ", ".join(self.stale[:10])
            )
        if self.unidentified_sessions:
            parts.append(
                f"{len(self.unidentified_sessions)} database session(s) were not opened by a "
                "reporting control-plane process (an older release or a tool); close them "
                "or upgrade them first"
            )
        return "; ".join(parts)


def _label(instance: dict[str, Any]) -> str:
    return f"{instance['role']}@{instance['machine']} ({str(instance['instance_id'])[:8]})"


async def fleet_supports(repository: InstanceRepository, capability: str) -> FleetVerdict:
    """Whether every control-plane participant is shown to support ``capability``.

    Fails closed: a current process without the capability, a process whose
    report went stale without a clean stop, or any database session no
    reporting process opened all make the answer "no".
    """
    instances = await repository.list_instances()
    current = [instance for instance in instances if instance["current"]]
    missing = [_label(i) for i in current if capability not in i["capabilities"]]
    stale = [
        _label(i)
        for i in instances
        if not i["current"] and i["stopped_at"] is None and _recent(i["last_seen_at"])
    ]
    unidentified = await repository.unidentified_sessions()
    return FleetVerdict(
        supported=bool(current) and not missing and not stale and not unidentified,
        current=current,
        missing=missing,
        stale=stale,
        unidentified_sessions=unidentified,
    )


def _recent(seen: datetime | None, window_s: float = 600.0) -> bool:
    """Stale within the last few minutes: possibly still running somewhere."""
    if seen is None:
        return False
    return (datetime.now(UTC) - seen).total_seconds() < window_s


def reset_identity() -> None:
    """Forget this process's identity (tests)."""
    global _identity
    _identity = None


__all__ = [
    "CONTROL_PLANE_CAPABILITIES",
    "RESTART_ROLES",
    "FleetVerdict",
    "ProcessIdentity",
    "RestartTracker",
    "fleet_supports",
    "key_identifier",
    "process_identity",
    "report_forever",
    "reset_identity",
    "root_key_state",
]
