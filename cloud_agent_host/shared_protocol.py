"""Re-export of :mod:`cloud_agent_contracts.shared_protocol`.

The constants and helpers moved to the ``cloud_agent_contracts`` package, which
the runner image ships on its own. This module stays so existing host-side
importers keep working: ``from cloud_agent_host.shared_protocol import ...``
resolves to the very same objects, not copies.
"""

from cloud_agent_contracts.shared_protocol import (
    ENV_DEPLOYMENT_SHA,
    HOST_CONCURRENCY_CAPABILITY,
    HOST_CONCURRENCY_MAX_SLOTS,
    HOST_CONCURRENCY_MIN_SLOTS,
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_DEPLOYMENT_CAPABILITY_PREFIX,
    SHARED_MEMBER_ACCOUNTING_VERSION,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_PROTOCOL,
    SHARED_SANDBOX_RUNTIME_PINS,
    SHARED_SANDBOX_V1_HELPER_PINS,
    has_shared_deployment_capability,
    shared_deployment_capability,
)

#: The whole surface of the moved module, so the re-export reads as one.
__all__ = [
    "ENV_DEPLOYMENT_SHA",
    "HOST_CONCURRENCY_CAPABILITY",
    "HOST_CONCURRENCY_MAX_SLOTS",
    "HOST_CONCURRENCY_MIN_SLOTS",
    "NATIVE_FORK_AT_ANCHOR_CAPABILITY",
    "SHARED_DEPLOYMENT_CAPABILITY_PREFIX",
    "SHARED_MEMBER_ACCOUNTING_VERSION",
    "SHARED_SANDBOX_CAPABILITY",
    "SHARED_SANDBOX_HELPER_PINS",
    "SHARED_SANDBOX_PROTOCOL",
    "SHARED_SANDBOX_RUNTIME_PINS",
    "SHARED_SANDBOX_V1_HELPER_PINS",
    "has_shared_deployment_capability",
    "shared_deployment_capability",
]
