"""Re-export of :mod:`cloud_agent_shared.git.source_archive`.

The archive builders — the bounded tar of one prepared checkout, and the base
bundle for one resolved commit — moved to the ``cloud_agent_shared.git`` package,
which both images ship. This module stays so existing importers keep working:
``from cloud_agent_host.source_archive import ...`` resolves to the very same
objects, not copies.

``SandboxError`` is re-exported alongside them because this namespace has
always carried it: the builders raise it on every refusal, and the shared
module imports the one class from ``cloud_agent_shared.errors``.

**When this module goes away.** Delete it when nothing imports it. No
``cloud_agent_host`` module does any more. The tests that still do are
``SHIM_IMPORT_ALLOWLIST`` in ``tests/unit/test_import_boundary.py``, which can
only shrink. The control plane is not on it: the publisher already imports
``cloud_agent_shared.git.source_archive`` directly, which is the path every
caller is meant to end up on.
"""

from cloud_agent_shared.git.source_archive import (
    DEFAULT_MAX_BASE_BUNDLE_BYTES,  # noqa: F401 — re-exported: the identity the host namespace kept.
    DEFAULT_MAX_SOURCE_ARCHIVE_BYTES,  # noqa: F401 — re-exported: the identity the host namespace kept.
    SandboxError,  # noqa: F401 — re-exported: the identity the host namespace kept.
    build_base_bundle,  # noqa: F401 — re-exported: the identity the host namespace kept.
    build_source_archive,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
