"""The Docker CLI as the compute bootstrap's remote scripts call it on a node."""

from __future__ import annotations

import json
from typing import Any


def service(name: str, project: str, role: str) -> dict[str, Any]:
    """A running container of the deployment's Compose project."""
    return {
        "id": name,
        "running": True,
        "labels": {"com.docker.compose.project": project, "com.docker.compose.service": role},
    }


def sandbox(
    name: str, namespace: str, *, runtime: str = "claude-code", **labels: str
) -> dict[str, Any]:
    """A running chat sandbox the deployment's sandbox manager created."""
    return {
        "id": name,
        "running": True,
        "labels": {
            "org.freeinference.chat-sandbox": "true",
            "org.freeinference.sandbox-namespace": namespace,
            "org.freeinference.chat-runtime": runtime,
            **labels,
        },
    }


def probe(name: str, namespace: str) -> dict[str, Any]:
    """A runner's self-test sandbox, as ``_probe_guest_observer_once`` creates it."""
    return sandbox(
        name,
        namespace,
        runtime="preflight",
        **{"org.freeinference.chat-probe-created": "1760000000"},
    )


class Docker:
    """A fixed set of containers behind ``docker ps``, ``stop`` and ``rm --force``.

    ``ps`` honours ``-a``, label filters (``label=key`` and ``label=key=value``)
    and the inventory's label template; anything else lists nothing. With
    ``stops=False`` a stopped container keeps running.
    """

    def __init__(self, *containers: dict[str, Any], stops: bool = True) -> None:
        self.containers = {c["id"]: dict(c) for c in containers}
        self.stops = stops
        self.changes: list[list[str]] = []

    def check_output(self, args: list[str], **_kwargs: Any) -> str:
        if args[:2] != ["docker", "ps"]:
            return ""  # networks and volumes: none
        rest = args[2:]
        filters = [
            rest[i + 1].removeprefix("label=").partition("=")
            for i, arg in enumerate(rest)
            if arg == "--filter"
        ]
        found = [
            c
            for c in self.containers.values()
            if ("-a" in rest or c["running"])
            and all(
                key in c["labels"] and (not value or c["labels"][key] == value)
                for key, _, value in filters
            )
        ]
        if "--format" in rest:
            return "\n".join(
                json.dumps(
                    {
                        "sandbox": c["labels"].get("org.freeinference.chat-sandbox", ""),
                        "project": c["labels"].get("com.docker.compose.project", ""),
                    }
                )
                for c in found
            )
        return "\n".join(c["id"] for c in found)

    def check_call(self, args: list[str], **_kwargs: Any) -> int:
        self.changes.append(list(args))
        if args[1] == "stop":
            for name in args[4:]:
                self.containers[name]["running"] = not self.stops
        elif args[1:3] == ["rm", "--force"]:
            for name in args[3:]:
                del self.containers[name]
        return 0
