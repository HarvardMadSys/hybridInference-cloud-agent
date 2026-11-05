"""The runner's part in sandbox incidents: carry them on beats, confirm them after.

The sandbox manager holds each sandbox's incident rows
(``cloud_agent_host.sandbox_incidents``) and lists them with its groups. The
runner carries them on the heartbeat as it lists them (:func:`carry`). When
the answer says the control plane stored them (``incidents: "accepted"``,
:func:`settle`), the runner tells the sandbox manager to forget them at its
next listing (:func:`confirm`), before it lists again.

A control plane that does not store incidents never says so, and a failed
confirmation is retried: either way the rows are carried again, and the
control plane's unique index makes the replay a no-op.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from cloud_agent_shared.sandbox_incidents import INCIDENTS_ACCEPTED, MAX_INCIDENTS_PER_BEAT

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent_host.sandbox_manager_client import SandboxManagerClient

_lock = threading.Lock()
#: What the beat being built carries, by sandbox.
_carried: dict[str, list[str]] = {}
#: What answers confirmed, not yet passed on to the sandbox manager.
_confirmed: dict[str, list[str]] = {}


def carry(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The incident rows one listed group's beat carries, remembered for :func:`settle`."""
    raw = row.get("incidents")
    rows = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    rows = rows[:MAX_INCIDENTS_PER_BEAT]
    with _lock:
        _carried[str(row.get("sandbox_id"))] = [str(item.get("incident_id")) for item in rows]
    return rows


def settle(answer: object) -> None:
    """Take a heartbeat answer's word on what the beat carried."""
    with _lock:
        if answer == INCIDENTS_ACCEPTED:
            for sandbox_id, ids in _carried.items():
                if ids:
                    _confirmed.setdefault(sandbox_id, []).extend(ids)
        _carried.clear()


def confirm(client: SandboxManagerClient) -> None:
    """Pass confirmations on to the sandbox manager; keep them if it cannot take them."""
    with _lock:
        pending = {key: list(value) for key, value in _confirmed.items()}
        _confirmed.clear()
        _carried.clear()
    if not pending:
        return
    try:
        client.confirm_incidents(pending)
    except Exception:
        with _lock:
            for sandbox_id, ids in pending.items():
                _confirmed.setdefault(sandbox_id, []).extend(ids)


__all__ = ["carry", "confirm", "settle"]
