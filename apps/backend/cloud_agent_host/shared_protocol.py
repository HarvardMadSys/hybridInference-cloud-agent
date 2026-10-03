"""Re-export of :mod:`cloud_agent_shared.shared_protocol`.

The constants and helpers moved to the ``cloud_agent_shared`` package, which
the runner image ships on its own. This module stays so existing host-side
importers keep working: ``from cloud_agent_host.shared_protocol import ...``
resolves to the very same objects, not copies.

**When this module goes away.** Delete it when no ``cloud_agent_host`` module
and no test imports it. That is exactly the set a scan of
``cloud_agent_host/**`` and ``tests/**`` for
``cloud_agent_host.shared_protocol`` returns today: the host modules
``runner.py``, ``host_runtime.py``, ``resources.py`` and
``candidate_check.py``, plus the eight test files
``tests/unit/test_candidate_check.py``,
``tests/unit/test_cross_artifact_contracts.py``,
``tests/unit/test_deploy_readiness.py``,
``tests/unit/test_execution_profiles.py``,
``tests/unit/test_runtime_version_selection.py``,
``tests/integration/servers/test_agent_jobs_lifecycle.py``,
``tests/integration/storage/test_agent_grant_store.py`` and
``tests/integration/storage/test_agent_job_store.py``. The control plane is
not in that set: it already imports
``cloud_agent_shared.shared_protocol`` directly, which is the path every
one of these callers is meant to end up on. The boundary test's
contracts-isolation test is what keeps the new path honest while the old one
still has callers.
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

#: The whole surface of the moved module, so the re-export reads as one.
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
