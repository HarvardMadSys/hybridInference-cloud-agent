"""Re-export of :mod:`cloud_agent_shared.workspace_browser`.

The path rules the control plane validates owner-supplied paths with moved to
the ``cloud_agent_shared`` package, which the runner image ships on its
own. This module stays so existing host-side importers keep working: ``from
cloud_agent_host.workspace_browser import ...`` resolves to the very same
objects, not copies.

**When this module goes away.** Delete it when no ``cloud_agent_host`` module
and no test imports ``cloud_agent_host.workspace_browser``. The control plane
is already off this path — it imports ``cloud_agent_shared.workspace_browser``
directly — so the remaining callers are what the boundary test's
contracts-isolation test keeps honest until they follow.
"""

from cloud_agent_shared.workspace_browser import (
    MAX_FILE_PREVIEW_BYTES,
    WorkspacePathError,
    normalize_workspace_path,
)

#: The moved public surface, so the re-export reads as one.
__all__ = [
    "MAX_FILE_PREVIEW_BYTES",
    "WorkspacePathError",
    "normalize_workspace_path",
]
