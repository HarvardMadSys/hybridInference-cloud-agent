"""Re-export of :mod:`cloud_agent_shared.turn_resources`.

The whole-turn resource summary moved to the ``cloud_agent_shared``
package, which the runner image ships on its own. This module stays so
existing host-side importers keep working: ``from
cloud_agent_host.turn_resources import ...`` resolves to the very same
objects, not copies.

**When this module goes away.** Delete it when no ``cloud_agent_host`` module
and no test imports ``cloud_agent_host.turn_resources``. The control plane is
already off this path — it imports ``cloud_agent_shared.turn_resources``
directly — so the remaining callers are what the boundary test's
contracts-isolation test keeps honest until they follow.
"""

from cloud_agent_shared.turn_resources import (
    TURN_SUMMARY_SCHEMA_VERSION,
    TURN_SUMMARY_SCOPE,
    summarize_run_resources,
    summarize_turn_resources,
)

#: The moved public surface, so the re-export reads as one.
__all__ = [
    "TURN_SUMMARY_SCHEMA_VERSION",
    "TURN_SUMMARY_SCOPE",
    "summarize_run_resources",
    "summarize_turn_resources",
]
