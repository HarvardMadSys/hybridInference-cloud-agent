"""Making an in-memory fake reachable the way the real storage layer is.

Production hands every caller the assembly: one object per capability, reached
by name (``storage.host_pool.get_host(...)``). A test fake usually implements
several capabilities in one class, because the scenario it stands in for spans
them -- and it would otherwise have to carry sixteen aliases of itself.

``AllCapabilities`` supplies exactly those aliases, and nothing else: a
capability name resolves to the fake itself, and any other missing attribute
still raises ``AttributeError``, so a typo in a fake is still a failure.

The names come from ``cloud_agent.ports`` at import time rather than from a
list kept here, so a seventeenth capability cannot be reachable in production
and silently unreachable in a fake.
"""

from __future__ import annotations

import pathlib
from typing import Any

_PORTS = pathlib.Path(__file__).resolve().parents[2] / "cloud_agent" / "ports"

#: One name per port module, which is also the assembly's attribute name.
CAPABILITIES = frozenset(p.stem for p in _PORTS.glob("*.py") if p.name != "__init__.py") | {
    "admin_runs",
    "admin_usage",
}


class AllCapabilities:
    """Mixin: answer every capability name with the fake itself."""

    def __getattr__(self, name: str) -> Any:
        """Resolve a capability name to this object; anything else is an error."""
        if name in CAPABILITIES:
            return self
        raise AttributeError(name)


class OneFake(AllCapabilities):
    """Wrap one flat fake so that every capability name resolves to it.

    For fakes built inline -- a ``SimpleNamespace`` of ``AsyncMock``s, say --
    where subclassing is not available. Attribute lookup falls through to the
    wrapped object, so the fake's own methods stay reachable under their own
    names as well as under a capability.
    """

    def __init__(self, inner: Any) -> None:
        """Hold the fake every capability will resolve to."""
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        """A capability name is this object; anything else is the fake's own."""
        if name in CAPABILITIES:
            return self
        return getattr(self._inner, name)
