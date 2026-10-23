"""The deployment-wide runner policy, as a heartbeat answer reads it.

Every host heartbeat share-locks ``agent_runner_policy`` before it writes its
host row, and the answer it sends back is made of that same row: the settings
set for every server, the archive policy, and what each host must hold of every
agent update. The row is decoded here, for the heartbeat and for each reader
that asks for one of these on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from cloud_agent.sandbox_archive_credentials import (
    archive_settings_payload,
    load_stored_archive_policy,
)
from cloud_agent.storage._sql.rows import load_json, stored_archive_settings
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_T = TypeVar("_T")

#: The policy columns a heartbeat reads, under the share lock its write takes.
HEARTBEAT_POLICY_COLUMNS = (
    "host_settings, host_settings_import_excluded, runtime_versions, runtime_upgrades, "
    "sandbox_archive_settings, sandbox_archive_revision, sandbox_archive_updated_at"
)

#: What reading a stored value of the wrong shape raises: an array or a string
#: where an object belongs, or an archive policy that does not parse.
_UNDECODABLE = (AttributeError, TypeError, ValueError)


def download_of(record: Mapping[str, Any]) -> dict[str, str]:
    """The part of a stored record a heartbeat answer carries."""
    return {key: str(record.get(key) or "") for key in ("version", "before", "nonce")}


def download_targets(policy: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """What a heartbeat answer tells hosts to hold, from the policy's selection and updates.

    See ``HostPoolRepository.runtime_download_targets`` for the rules.
    """
    selection = {} if policy is None else dict(load_json(policy["runtime_versions"]) or {})
    upgrades = {} if policy is None else dict(load_json(policy["runtime_upgrades"]) or {})
    targets: list[dict[str, str]] = []
    for runtime, record in sorted(upgrades.items()):
        if not isinstance(record, dict):
            continue
        own = {"runtime": runtime, **download_of(record)}
        held = record.get("previous_download")
        if not isinstance(held, dict) or held.get("version") == record.get("version"):
            targets.append(own)
        elif not record.get("activated_at"):
            if held.get("version") == selection.get(runtime):
                targets.append({"runtime": runtime, **download_of(held)})
            targets.append(own)
        else:
            targets.append(own)
            if held.get("version") == record.get("previous_version"):
                targets.append({"runtime": runtime, **download_of(held)})
    return targets


def archive_settings(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    """The normalized archive policy and when it was set, from the policy row."""
    if policy is None:
        # The baseline normally creates the singleton. Keep a missing row
        # readable during restore/bootstrap; the first write recreates it.
        return archive_settings_payload(SandboxArchivePolicy(), None)
    return archive_settings_payload(
        load_stored_archive_policy(stored_archive_settings(policy)),
        policy["sandbox_archive_updated_at"],
    )


def _host_settings(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    """The runtime settings set for every server, from the policy row."""
    return {} if policy is None else dict(load_json(policy["host_settings"]) or {})


def _upgrade_waiting(policy: Mapping[str, Any] | None) -> bool:
    """Whether an agent update is still waiting to switch new chats, from the policy row."""
    upgrades = {} if policy is None else load_json(policy["runtime_upgrades"]) or {}
    return any(
        isinstance(target, dict) and not target.get("activated_at") for target in upgrades.values()
    )


def _decoded(
    read: Callable[[Mapping[str, Any] | None], _T], policy: Mapping[str, Any] | None
) -> _T | None:
    """``read(policy)``, or ``None`` when the stored value it reads does not decode."""
    try:
        return read(policy)
    except _UNDECODABLE:
        return None


def heartbeat_policy(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    """What a heartbeat answer needs of the policy row.

    The global ``host_settings``, the ``sandbox_archive_settings``, the
    ``runtime_download_targets``, and whether an agent update is still waiting
    to switch new chats (``runtime_upgrade_waiting``).

    Each is ``None`` when the stored value it is read from does not decode.
    The beat is recorded by then, and its residencies and observations still
    have to be handled, so the answer reads that part again once they are,
    through the store's own read, and fails there, as it did before the beat
    read the policy itself.
    """
    return {
        "host_settings": _decoded(_host_settings, policy),
        "sandbox_archive_settings": _decoded(archive_settings, policy),
        "runtime_download_targets": _decoded(download_targets, policy),
        "runtime_upgrade_waiting": _decoded(_upgrade_waiting, policy),
    }
