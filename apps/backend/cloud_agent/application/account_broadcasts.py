"""Sending administrator mail to accounts, one claimed broadcast at a time.

The pass claims a broadcast that is due (or whose sender stopped), mails its
pending recipients through the configured account mail transport, and
records each recipient's outcome as soon as that one message is handed over,
before the next is sent, so a sender that dies mid-run leaves at most the one
message in flight unrecorded: the pass that takes over resends only that
one. It closes the broadcast once none is pending, and runs in the process
that owns background work, in local mode, every :data:`BROADCAST_INTERVAL_S`;
see :mod:`cloud_agent.storage.repositories.account_broadcasts`.

A message the selected transport refuses by shape (a Tencent SES template
bound, after the transport was changed since the message was created) is
recorded failed for each recipient with that reason, rather than as a
generic delivery failure.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from cloud_agent.accounts.delivery import delivery_refusal
from cloud_agent.accounts.mail import AccountMail
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.accounts.mail import AccountMailer
    from cloud_agent.ports.accounts import AccountBroadcasts

logger = get_logger(__name__)

BROADCAST_INTERVAL_S = 30.0
#: Recipients read per batch, and the pause between batches, as HybridInference.
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
        for recipient in batch:
            outcome = await _deliver(mailer, recipient, details)
            # Recorded before the next message goes: a takeover never resends it.
            await accounts.record_broadcast_results(broadcast_id=broadcast_id, outcomes=[outcome])
        if len(batch) == BATCH_SIZE:
            await asyncio.sleep(BATCH_DELAY_S)
    status = await accounts.finish_broadcast(broadcast_id=broadcast_id)
    logger.info(
        "agent_local_broadcast_sent",
        extra={"event": "agent_local_broadcast_sent", "broadcast_id": broadcast_id},
    )
    return status


async def _deliver(
    mailer: AccountMailer, recipient: dict[str, object], details: dict[str, str]
) -> tuple[str, str, str | None]:
    account_id = str(recipient["account_id"])
    if not recipient["deliverable"]:
        return account_id, "skipped", "address no longer usable"
    mail = AccountMail(kind="broadcast", to=str(recipient["email"]), details=details)
    refused = delivery_refusal(mail)
    if refused is not None:
        return account_id, "failed", refused[:500]
    if await mailer.send(mail):
        return account_id, "sent", None
    return account_id, "failed", "delivery was not accepted"


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
