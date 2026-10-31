"""Asking again whether a turn's owner may still use Agent.

``require_agent_owner`` reads the account on every owner request, and for a
turn a person submits that request is what admitted it. Some turns start with
no request behind them at all: a goal's automatic round, a waiting message
sent when the turn in front of it ends, and every turn at the moment a host
claims it. None of those passed that gate, and the account's current
permissions (not an old session, and not a decision made when the work was
queued) govern Agent use. So each of them asks again here.

**Failure is closed, and has two shapes.** The directory answering *no* (an
explicit denial, or an account that is unknown or cannot sign in) is a
verdict, and the caller ends or holds the work with a reason its owner can
read. The directory *not answering* (the account store unreadable, or an
answer that does not parse) is not a verdict on the owner, so
:class:`AgentAccessUnavailable` tells the caller to wait and ask again. Neither
lets the work proceed.

**There is no cache.** A withdrawal that took a cache lifetime to reach the
background would be the gap this closes, and the read is one primary-key
lookup.
"""

from __future__ import annotations

from typing import Any

from cloud_agent.accounts.directory import OwnerDirectoryError
from cloud_agent.agent_access import AgentAccessDecision

#: What the owner reads when the answer is no. The same sentence the request
#: path gives for the same decision, so a held message, a failed turn and a
#: refused request all say one thing.
AGENT_ACCESS_WITHDRAWN = "Agent access has not been granted for this account."


class AgentAccessUnavailable(Exception):
    """The account directory could not confirm the owner's Agent access just now."""


async def owner_may_use_agent(owners: Any, *, user_id: str) -> bool:
    """Whether the account's current permissions grant this owner ``agent.use``.

    Reads the decision the way an owner request does: ``False`` for an explicit
    denial and for an account that is unknown or cannot sign in
    (``subject_unavailable``).

    Args:
        owners: The account directory (``app.state.owners``).
        user_id: The owner of the work about to start.

    Returns:
        ``True`` only when the current decision explicitly includes
        ``agent.use``.

    Raises:
        AgentAccessUnavailable: There is no directory to ask, or it gave no
            usable answer. The caller must not proceed on it.
    """
    if owners is None:
        raise AgentAccessUnavailable("no account directory is available to ask")
    try:
        decision = AgentAccessDecision.model_validate(await owners.agent_access(user_id))
    except OwnerDirectoryError as exc:
        if exc.status_code == 403 and exc.error_type == "subject_unavailable":
            return False
        raise AgentAccessUnavailable(str(exc)) from exc
    except ValueError as exc:
        raise AgentAccessUnavailable("the Agent access decision is invalid") from exc
    if decision.user_id != user_id:
        raise AgentAccessUnavailable("the directory answered for a different account")
    return decision.permits("agent.use")


__all__ = ["AGENT_ACCESS_WITHDRAWN", "AgentAccessUnavailable", "owner_may_use_agent"]
