"""Docker reads for retained chat sandboxes and their group networks.

``docker container inspect`` and ``docker network inspect`` take any number of
references and answer for all of them in one process; an extra reference costs
a few hundred microseconds against roughly twelve milliseconds for the process
itself. Reading one object per process made the host heartbeat's listing and a
reconcile's relay checks scale with the number of objects for no reason.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent_host.chat_sandbox_models import ChatSandboxInspection, ChatSandboxStatus
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    import builtins
    import subprocess
    from collections.abc import Callable, Sequence

    Run = Callable[..., subprocess.CompletedProcess[str]]

#: References per Docker process, which bounds its argv and its output.
INSPECT_BATCH = 100

#: How Docker says it found no object for one reference.
_NOT_FOUND = ("no such container", "no such object", "no such network", "not found")


def inspect_many(
    run: Run, docker_binary: str, kind: str, refs: Sequence[str]
) -> builtins.list[dict[str, Any]]:
    """Inspect each ``container`` or ``network`` in ``refs``; return what Docker found.

    One that went away since it was listed is only absent: Docker answers for
    the rest and exits non-zero, saying which it did not find. Any other
    failure raises, as a failed inspection of a single object does.
    """
    unique = list(dict.fromkeys(refs))
    found: builtins.list[dict[str, Any]] = []
    for first in range(0, len(unique), INSPECT_BATCH):
        batch = unique[first : first + INSPECT_BATCH]
        result = run([docker_binary, kind, "inspect", *batch], timeout=30)
        errors = [line.lower() for line in result.stderr.splitlines() if line.strip()]
        only_missing = errors and all(
            any(marker in line for marker in _NOT_FOUND)
            and any(ref.lower() in line for ref in batch)
            for line in errors
        )
        if result.returncode != 0 and not only_missing:
            raise SandboxError(f"Docker {kind} inspect failed: " + result.stderr.strip()[:300])
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise SandboxError(f"Docker returned an invalid {kind} inspection") from exc
        if not isinstance(payload, list) or not all(isinstance(raw, dict) for raw in payload):
            raise SandboxError(f"Docker returned an invalid {kind} inspection")
        found.extend(payload)
    return found


def containers_by_ref(
    run: Run, docker_binary: str, container_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Inspect many containers in one call, keyed by the id asked for.

    An id that does not resolve is simply absent from the result. Docker
    exits non-zero when *any* id is missing while still answering for the
    rest, so the output is parsed regardless of the exit code: a partial
    answer is the normal case here, since Compose replacement can remove
    an endpoint between the network inspection and this one.
    """
    wanted = list(dict.fromkeys(str(container_id) for container_id in container_ids))
    if not wanted:
        return {}
    result = run([docker_binary, "container", "inspect", *wanted], timeout=30)
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, list):
        return {}

    found: dict[str, dict[str, Any]] = {}
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        identifier = str(raw.get("Id") or "")
        name = str(raw.get("Name") or "").removeprefix("/")
        for container_id in wanted:
            if container_id in found:
                continue
            # A caller may hold a full id, a short id, or a name; Docker
            # always answers with the full id and the name.
            if container_id in (identifier, name) or identifier.startswith(container_id):
                found[container_id] = raw
                break
    return found


def container_status(state: dict[str, Any]) -> ChatSandboxStatus:
    """Read Docker's container state as a lifecycle status."""
    if bool(state.get("Paused")):
        return ChatSandboxStatus.PAUSED
    raw = str(state.get("Status") or "").lower()
    if raw == "exited":
        return ChatSandboxStatus.STOPPED
    try:
        return ChatSandboxStatus(raw)
    except ValueError:
        return ChatSandboxStatus.UNKNOWN


def container_inspection(raw: Any) -> ChatSandboxInspection:
    """Read one ``docker container inspect`` object; a malformed one raises."""
    try:
        config = raw.get("Config") or {}
        labels = config.get("Labels") or {}
        mounts = raw.get("Mounts") or []
        state = raw.get("State") or {}
        network_settings = (raw.get("NetworkSettings") or {}).get("Networks") or {}
        networks = network_settings.keys()
        return ChatSandboxInspection(
            container_id=str(raw["Id"]),
            name=str(raw.get("Name") or "").removeprefix("/"),
            status=container_status(state),
            running=bool(state.get("Running")),
            # Config.Image is the exact reference passed to ``docker
            # create``. ``Image`` is Docker's resolved local layer id and
            # cannot be compared directly with a registry repo digest.
            image=str(config.get("Image") or raw.get("Image") or ""),
            labels={str(key): str(value) for key, value in labels.items()},
            mounts=tuple(dict(mount) for mount in mounts),
            networks=tuple(sorted(str(network) for network in networks)),
            network_ids=tuple(
                sorted(
                    (
                        str(network),
                        str((endpoint or {}).get("NetworkID") or ""),
                    )
                    for network, endpoint in network_settings.items()
                )
            ),
            sandbox_runtime=str((raw.get("HostConfig") or {}).get("Runtime") or ""),
            tmpfs=tuple(
                sorted(str(path) for path in (raw.get("HostConfig") or {}).get("Tmpfs") or {})
            ),
            started_at=str(state.get("StartedAt") or ""),
        )
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise SandboxError("Docker returned an invalid chat sandbox inspection") from exc
