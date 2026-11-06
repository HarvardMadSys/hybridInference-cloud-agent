"""The plugins a member's CLI starts with."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from cloud_agent_host.launch.facet import LaunchFacet

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class PluginsFacet(LaunchFacet):
    """The validated plugin list, fixed at process start.

    Validation and installation depend on the CLI, so the launch validates
    them and the transport factory installs them; this states only that a
    changed list needs a new process.
    """

    plugins: tuple[dict[str, str], ...]

    @override
    def fingerprint(self) -> Mapping[str, object]:
        return {"plugins": self.plugins}
