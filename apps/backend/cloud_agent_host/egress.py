"""Re-export of :mod:`cloud_agent_shared.egress`.

The egress policy — its tiers, its environment reading and the allowlist rules
that decide what the sandbox may reach — moved to the ``cloud_agent_shared``
package, which the runner image ships on its own. This module stays so
existing host-side importers keep working: ``from cloud_agent_host.egress
import ...`` resolves to the very same objects, not copies.

The runtime enforcement — pointing a sandbox at the allowlist proxy and
building its environment — is not here and did not move with the policy: see
:mod:`cloud_agent_host.egress_proxy` and the sandbox lifecycle.

**When this module goes away.** Delete it when no ``cloud_agent_host`` module
and no test imports ``cloud_agent_host.egress``. The control plane is already
off this path — it imports ``cloud_agent_shared.egress`` directly — so the
remaining callers are what the boundary test's contracts-isolation test keeps
honest until they follow.
"""

from cloud_agent_shared.egress import (
    DEFAULT_TRUSTED_DOMAINS,
    PHASES,
    VENDOR_TELEMETRY_DOMAINS,
    EgressPolicy,
    EgressPolicyError,
    EgressTier,
    build_policy_from_env,
    check_allowlist,
    normalize_domains,
    trusted_domains,
)

#: The moved public surface, so the re-export reads as one.
__all__ = [
    "DEFAULT_TRUSTED_DOMAINS",
    "PHASES",
    "VENDOR_TELEMETRY_DOMAINS",
    "EgressPolicy",
    "EgressPolicyError",
    "EgressTier",
    "build_policy_from_env",
    "check_allowlist",
    "normalize_domains",
    "trusted_domains",
]
