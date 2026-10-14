"""Re-export of :mod:`cloud_agent_shared.git.patch_gate`.

The patch gate — the four checks an agent-produced diff passes before it may
become a branch — moved to the ``cloud_agent_shared.git`` package, which both images
ship. This module stays so existing importers keep working:
``from cloud_agent_host.patch_gate import ...`` resolves to the very same
objects, not copies.

**When this module goes away.** Delete it when nothing imports it. No
``cloud_agent_host`` module does any more. The tests that still do are
``SHIM_IMPORT_ALLOWLIST`` in ``tests/unit/test_import_boundary.py``, which can
only shrink. The control plane is not on it: the publisher, the publish worker
and the API router already import ``cloud_agent_shared.git.patch_gate``
directly, which is the path every caller is meant to end up on.
"""

from cloud_agent_shared.git.patch_gate import (
    MAX_CHANGED_FILES,  # noqa: F401 — re-exported: the identity the host namespace kept.
    MAX_PATCH_BYTES,  # noqa: F401 — re-exported: the identity the host namespace kept.
    SECRET_PATTERNS,  # noqa: F401 — re-exported: the identity the host namespace kept.
    PatchGateResult,  # noqa: F401 — re-exported: the identity the host namespace kept.
    branch_name_for,  # noqa: F401 — re-exported: the identity the host namespace kept.
    validate_change_set,  # noqa: F401 — re-exported: the identity the host namespace kept.
    validate_patch,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
