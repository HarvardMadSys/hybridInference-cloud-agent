"""Re-export of :mod:`cloud_agent_shared.shared_protocol`.

The constants and helpers moved to the ``cloud_agent_shared`` package, which
the runner image ships on its own. This module stays so existing importers
keep working: ``from cloud_agent_host.shared_protocol import ...`` resolves to
the very same objects, not copies.

**When this module goes away.** Delete it when nothing imports it. No
``cloud_agent_host`` module does any more. The tests and scripts that still do
are ``SHIM_IMPORT_ALLOWLIST`` in ``tests/unit/test_import_boundary.py``, which
fails on a new importer and on an entry whose importer has moved on, so the
list can only shrink. The control plane is not on it: it imports
``cloud_agent_shared.shared_protocol`` directly, which is the path every
caller is meant to end up on.
"""

from cloud_agent_shared.shared_protocol import (
    ENV_DEPLOYMENT_SHA,
    GOAL_REPORT_CAPABILITY,
    HOST_CONCURRENCY_CAPABILITY,
    HOST_CONCURRENCY_MAX_SLOTS,
    HOST_CONCURRENCY_MIN_SLOTS,
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    PROFILING_CONTROL_CAPABILITY,
    PROFILING_POLICY_HEADER,
    SHARED_DEPLOYMENT_CAPABILITY_PREFIX,
    SHARED_MEMBER_ACCOUNTING_VERSION,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_RUNTIME_PINS,
    has_shared_deployment_capability,
    shared_deployment_capability,
)

#: The surface this re-export was frozen with. A name the shared module gained
#: since is imported from ``cloud_agent_shared.shared_protocol`` directly and is
#: never added here: this module only ever shrinks.
__all__ = [
    "ENV_DEPLOYMENT_SHA",
    "GOAL_REPORT_CAPABILITY",
    "HOST_CONCURRENCY_CAPABILITY",
    "HOST_CONCURRENCY_MAX_SLOTS",
    "HOST_CONCURRENCY_MIN_SLOTS",
    "NATIVE_FORK_AT_ANCHOR_CAPABILITY",
    "PROFILING_CONTROL_CAPABILITY",
    "PROFILING_POLICY_HEADER",
    "SHARED_DEPLOYMENT_CAPABILITY_PREFIX",
    "SHARED_MEMBER_ACCOUNTING_VERSION",
    "SHARED_SANDBOX_CAPABILITY",
    "SHARED_SANDBOX_HELPER_PINS",
    "SHARED_SANDBOX_RUNTIME_PINS",
    "has_shared_deployment_capability",
    "shared_deployment_capability",
]
