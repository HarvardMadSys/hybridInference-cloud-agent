"""Gateway-owned recovery for terminal sessions left behind by settled jobs.

The broker is passed in rather than read from the environment here.
This runs for jobs that have already settled — including from the reaper's
startup reconciliation, where there is no request and no attempt in hand — so
"which broker" is a question only the caller can answer, by looking up the host
that ran the attempt. See ``cloud_agent.broker_routing``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.logging_utils import get_logger
from cloud_agent.workspace_broker_client import WorkspaceBrokerError

if TYPE_CHECKING:
    from cloud_agent.workspace_broker_client import WorkspaceBrokerClient

logger = get_logger(__name__)


async def resume_settled_terminal(
    workspace_id: str,
    *,
    job_id: str,
    broker: WorkspaceBrokerClient | None,
) -> bool:
    """Authoritatively resume one settled workspace, reporting confirmation.

    ``broker=None`` reports failure. Every runnable chat has a retained
    broker-owned workspace; clearing the durable recovery marker without
    reaching that owner would turn a routing/configuration fault into silent
    state loss.
    """
    if broker is None:
        return False
    try:
        await broker.resume_settled_terminals(workspace_id, job_id=job_id)
    except WorkspaceBrokerError:
        logger.warning(
            "agent_terminal_settled_resume_failed",
            exc_info=True,
            extra={
                "event": "agent_terminal_settled_resume_failed",
                "job_id": job_id,
                "workspace_id": workspace_id,
            },
        )
        return False
    return True
