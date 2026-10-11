"""Error types both sides of the deployment have to agree on.

``SandboxError`` is the one error that crosses the boundary: the host's sandbox
raises it, and the shared source-archive builders in ``cloud_agent_shared.git`` raise
the same class when a prepared checkout cannot be archived. It is an ordinary
``Exception`` subclass with no behaviour of its own, so it carries no execution
dependency and belongs with the rest of the shared vocabulary rather than in
the host package that gave it its name.

The class is defined here and nowhere else. ``cloud_agent_host.sandbox``
imports this object and re-exports it, so the host's ``except SandboxError``
sites and ``TerminalNotSupportedError``'s inheritance keep working unchanged.
The only thing that changed for a caller is the module path in a traceback.
"""

from __future__ import annotations


class SandboxError(Exception):
    """Raised when a sandbox cannot be created or configured."""


class SandboxArchiveBlocked(SandboxError):
    """Raised when unflushed native history permanently forbids final archival."""
