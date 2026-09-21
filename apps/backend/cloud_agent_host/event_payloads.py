"""Re-export of :mod:`cloud_agent_shared.event_payloads`.

The event bounding helpers moved to the ``cloud_agent_shared`` package,
which the runner image ships on its own. This module stays so existing
host-side importers keep working: ``from cloud_agent_host.event_payloads
import ...`` resolves to the very same objects, not copies.

**When this module goes away.** Delete it when no ``cloud_agent_host`` module
and no test imports ``cloud_agent_host.event_payloads``. The control plane is
already off this path — it imports ``cloud_agent_shared.event_payloads``
directly — so the remaining callers are what the boundary test's
contracts-isolation test keeps honest until they follow.
"""

from cloud_agent_shared.event_payloads import (
    MAX_EVENT_PAYLOAD_BYTES,
    TRUNCATION_NOTICE,
    bound_event_payload,
)

#: The moved public surface, so the re-export reads as one.
__all__ = [
    "MAX_EVENT_PAYLOAD_BYTES",
    "TRUNCATION_NOTICE",
    "bound_event_payload",
]
