"""The sandbox's shared browser, which every member's tools reach through its service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from cloud_agent_host.launch.facet import LaunchFacet
from cloud_agent_host.sandbox_browser import ensure_browser_service

if TYPE_CHECKING:
    from cloud_agent_host.launch.facet import PrepareContext


@dataclass(frozen=True)
class BrowserFacet(LaunchFacet):
    """No launch value of its own: only the service, which no member's own exec may start."""

    @override
    def prepare(self, context: PrepareContext) -> None:
        ensure_browser_service(context.manager, context.spec)
