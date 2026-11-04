"""Stand-ins for the runner's background workers, shared by runner service tests."""

from __future__ import annotations

from typing import Any


class NoRuntimeDownloads:
    """A download worker with nothing to hold and no thread."""

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def apply(self, _targets: object) -> None:
        pass

    def report(self) -> list[dict[str, object]]:
        return []

    def targets(self) -> frozenset[tuple[str, str]]:
        return frozenset()

    def registry_report(self) -> dict[str, str]:
        return {"runtime_registry_url": ""}

    def set_registry(self, _registry: str | None) -> bool:
        return False
