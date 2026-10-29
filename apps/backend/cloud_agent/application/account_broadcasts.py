"""Sending administrator mail to accounts, one claimed broadcast at a time.

The pass claims a broadcast that is due (or whose sender stopped), mails its
pending recipients in batches through the configured account mail transport,
records each outcome, and closes it once none is pending. It runs in the
process that owns background work, in local mode, every
:data:`BROADCAST_INTERVAL_S`; see
:mod:`cloud_agent.storage.repositories.account_broadcasts` for why a message
is never sent twice.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from cloud_agent.accounts.mail import AccountMail
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.accounts.mail import AccountMailer
    from cloud_agent.ports.accounts import AccountBroadcasts

logger = get_logger(__name__)

BROADCAST_INTERVAL_S = 30.0
#: Recipients per batch, and the pause between batches, as HybridInference.
BATCH_SIZE = 50
BATCH_DELAY_S = 1.0


async def send_broadcast(
    accounts: AccountBroadcasts, mailer: AccountMailer, broadcast: dict[str, object]
) -> str:
    """Mail every pending recipient of a claimed broadcast; its final status."""
    broadcast_id = str(broadcast["id"])
    details = {
        "subject": str(broadcast["subject"]),
        "html": str(broadcast["body_html"]),
        "text": str(broadcast["body_text"]),
    }
    while batch := await accounts.pending_broadcast_recipients(
        broadcast_id=broadcast_id, limit=BATCH_SIZE
    ):
        outcomes: list[tuple[str, str, str | None]] = []
        for recipient in batch:
            if not recipient["deliverable"]:
                outcomes.append((recipient["account_id"], "skipped", "address no longer usable"))
                continue
            sent = await mailer.send(
                AccountMail(kind="broadcast", to=recipient["email"], details=details)
            )
            outcomes.append(
                (recipient["account_id"], "sent", None)
                if sent
                else (recipient["account_id"], "failed", "delivery was not accepted")
            )
        await accounts.record_broadcast_results(broadcast_id=broadcast_id, outcomes=outcomes)
        if len(batch) == BATCH_SIZE:
            await asyncio.sleep(BATCH_DELAY_S)
    status = await accounts.finish_broadcast(broadcast_id=broadcast_id)
    logger.info(
        "agent_local_broadcast_sent",
        extra={"event": "agent_local_broadcast_sent", "broadcast_id": broadcast_id},
    )
    return status


async def send_due_broadcasts(accounts: AccountBroadcasts, mailer: AccountMailer) -> int:
    """One pass: send every broadcast that is due; how many were sent."""
    if not mailer.configured:
        return 0
    sent = 0
    while broadcast := await accounts.claim_due_broadcast():
        await send_broadcast(accounts, mailer, broadcast)
        sent += 1
    return sent


__all__ = ["BATCH_SIZE", "BROADCAST_INTERVAL_S", "send_broadcast", "send_due_broadcasts"]
