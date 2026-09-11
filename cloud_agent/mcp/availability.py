"""The deployment-wide switch, and what turning MCP off actually turns off.

A feature that can only be disabled by deleting rows is not a feature an
operator can withdraw. This is the one lever that closes the *use* of MCP
without touching configuration: authorization, session binding and the relay
all refuse, while everything that lets an owner see and tidy up what they have
stays open. "Off" must never mean "you can no longer find out what is there".

**Environment, not a database row.** A stored switch would have to be read by
every instance on every call and cached with an invalidation story; an
environment variable is read by the process and applied when it starts, which
is the same guarantee a rolling restart already gives. The consequence is the
one the design states: turning it off is not effective until every instance has
restarted, so a deployment that cannot prove that must treat the switch as one
half of the withdrawal and isolate the old listeners as the other.

**Off is a refusal, not a 404.** The relay answers 503 with a message that names
the switch, because an agent that sees "not found" retries the tool and a person
debugging at 3am needs to be told which lever was pulled.
"""

from __future__ import annotations

import os

#: Set to a false value (``0``, ``false``, ``no``, ``off``) to withdraw MCP use
#: across a deployment. Unset means enabled: a service that shipped this feature
#: is a service whose operator wants it, and a kill switch that defaults to off
#: is a feature nobody can turn on by deploying.
ENV_MCP_ENABLED = "AGENT_MCP_ENABLED"

#: The values that read as "off". Anything else, including empty, is on.
_FALSEY = frozenset({"0", "false", "no", "off"})

#: What a caller is told when the switch is off. One sentence, because it is
#: shown in a tool result the model reads and on a page a person reads.
DISABLED_MESSAGE = (
    "MCP is turned off on this deployment. Connected servers and their "
    "capabilities are still listed, and can still be disconnected."
)


def mcp_enabled() -> bool:
    """Whether this instance is serving MCP use."""
    return os.environ.get(ENV_MCP_ENABLED, "").strip().lower() not in _FALSEY


__all__ = ["DISABLED_MESSAGE", "ENV_MCP_ENABLED", "mcp_enabled"]
