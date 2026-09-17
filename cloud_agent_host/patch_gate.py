"""Re-export of :mod:`cloud_agent_git.patch_gate`.

The patch gate — the four checks an agent-produced diff passes before it may
become a branch — moved to the ``cloud_agent_git`` package, which both images
ship. This module stays so existing host-side importers keep working:
``from cloud_agent_host.patch_gate import ...`` resolves to the very same
objects, not copies.

**When this module goes away.** Delete it when no ``cloud_agent_host`` module
and no test imports ``cloud_agent_host.patch_gate``. That is exactly the set a
scan of ``cloud_agent_host/**`` and ``tests/**`` for
``cloud_agent_host.patch_gate`` returns today: the host module ``runner.py``,
which takes ``branch_name_for``, and the two test files
``tests/unit/test_agent_patch_gate.py`` and
``tests/integration/test_agent_publisher.py``. The control plane is not in that
set: the publisher, the publish worker and the API router already import
``cloud_agent_git.patch_gate`` directly, which is the path every one of these
callers is meant to end up on.
"""

from cloud_agent_git.patch_gate import (
    MAX_CHANGED_FILES,  # noqa: F401 — re-exported: the identity the host namespace kept.
    MAX_PATCH_BYTES,  # noqa: F401 — re-exported: the identity the host namespace kept.
    SECRET_PATTERNS,  # noqa: F401 — re-exported: the identity the host namespace kept.
    PatchGateResult,  # noqa: F401 — re-exported: the identity the host namespace kept.
    branch_name_for,  # noqa: F401 — re-exported: the identity the host namespace kept.
    validate_change_set,  # noqa: F401 — re-exported: the identity the host namespace kept.
    validate_patch,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
