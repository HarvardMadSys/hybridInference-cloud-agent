"""Asking again whether a turn's owner may still use Agent.

``require_agent_owner`` asks the Gateway on every owner request, and for a turn
a person submits that request is what admitted it. Some turns start with no
request behind them at all: a goal's automatic round, a waiting message sent
when the turn in front of it ends, and every turn at the moment a host claims
it. None of those passed that gate, and the lookups contract is explicit that
the Gateway's current decision — not an old session, and not a decision made
when the work was queued — governs Agent use. So each of them asks again here.

The grant mint does not make this redundant. The Gateway refuses a grant only
for an account that is unknown or inactive; an active account whose
``agent.use`` was withdrawn is still issued a working capability.

**Failure is closed, and has two shapes.** The Gateway answering *no* — an
explicit denial, or an account it reports unknown or inactive — is a verdict,
and the caller ends or holds the work with a reason its owner can read. The
Gateway *not answering* — unreachable, unconfigured, or answering with
something that does not parse — is not a verdict on the owner, so
:class:`AgentAccessUnavailable` tells the caller to wait and ask again. Neither
lets the work proceed.

**There is no cache.** A withdrawal that took a cache lifetime to reach the
background would be the gap this closes, and every caller here is about to make
a Gateway or GitHub call of its own — a grant mint, a model check, an
entitlement lookup — so one more lookup per turn does not change the load.
"""

from __future__ import annotations

from typing import Any

from cloud_agent.agent_access import AgentAccessDecision
from cloud_agent.gateway import GatewayError

#: What the owner reads when the answer is no. The same sentence the request
#: path gives for the same decision, so a held message, a failed turn and a
#: refused request all say one thing.
AGENT_ACCESS_WITHDRAWN = "Agent access has not been granted for this account."


class AgentAccessUnavailable(Exception):
    """The Gateway could not confirm the owner's Agent access just now."""


async def owner_may_use_agent(gateway: Any, *, user_id: str) -> bool:
    """Whether the Gateway's current decision grants this owner ``agent.use``.

    Reads the decision the way an owner request does: ``False`` for an explicit
    denial and for an account the Gateway reports unknown or no longer active
    (``subject_unavailable``).

    Args:
        gateway: Client for the Gateway's internal contracts.
        user_id: The owner of the work about to start.

    Returns:
        ``True`` only when the current decision explicitly includes
        ``agent.use``.

    Raises:
        AgentAccessUnavailable: There is no Gateway client to ask, or it gave no
            usable answer. The caller must not proceed on it.
    """
    if gateway is None:
        raise AgentAccessUnavailable("no Gateway client is available to ask")
    try:
        decision = AgentAccessDecision.model_validate(await gateway.agent_access(user_id))
    except GatewayError as exc:
        if exc.status_code == 403 and exc.error_type == "subject_unavailable":
            return False
        raise AgentAccessUnavailable(str(exc)) from exc
    except ValueError as exc:
        raise AgentAccessUnavailable("the Gateway's Agent access decision is invalid") from exc
    if decision.user_id != user_id:
        raise AgentAccessUnavailable("the Gateway answered for a different account")
    return decision.permits("agent.use")


__all__ = ["AGENT_ACCESS_WITHDRAWN", "AgentAccessUnavailable", "owner_may_use_agent"]
