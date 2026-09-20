"""Re-export of :mod:`cloud_agent_shared.git.source_archive`.

The archive builders — the bounded tar of one prepared checkout, and the base
bundle for one resolved commit — moved to the ``cloud_agent_shared.git`` package,
which both images ship. This module stays so existing host-side importers keep
working: ``from cloud_agent_host.source_archive import ...`` resolves to the
very same objects, not copies.

``SandboxError`` is re-exported alongside them because this namespace has
always carried it: the builders raise it on every refusal, and the shared
module imports the one class from ``cloud_agent_shared.errors``.

**When this module goes away.** Delete it when no ``cloud_agent_host`` module
and no test imports ``cloud_agent_host.source_archive``. That is exactly the
set a scan of ``cloud_agent_host/**`` and ``tests/**`` for
``cloud_agent_host.source_archive`` returns today: the host modules
``runner.py`` (``build_base_bundle``, ``build_source_archive``) and
``workspace_broker.py`` (``DEFAULT_MAX_BASE_BUNDLE_BYTES``), plus the test file
``tests/unit/test_source_archive.py``. The control plane is not in that set:
the publisher already imports ``cloud_agent_shared.git.source_archive`` directly,
which is the path every one of these callers is meant to end up on.
"""

from cloud_agent_shared.git.source_archive import (
    DEFAULT_MAX_BASE_BUNDLE_BYTES,  # noqa: F401 — re-exported: the identity the host namespace kept.
    DEFAULT_MAX_SOURCE_ARCHIVE_BYTES,  # noqa: F401 — re-exported: the identity the host namespace kept.
    SandboxError,  # noqa: F401 — re-exported: the identity the host namespace kept.
    build_base_bundle,  # noqa: F401 — re-exported: the identity the host namespace kept.
    build_source_archive,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
