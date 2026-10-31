"""The local-mode pass that sends administrator mail to accounts when it is due.

Runs only in the process that owns background work, and only in local mode.
See :mod:`cloud_agent.application.account_broadcasts`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.application.account_broadcasts import (
    BROADCAST_INTERVAL_S,
    send_due_broadcasts,
)
from cloud_agent.workers.periodic import run_every, run_pass

if TYPE_CHECKING:
    from cloud_agent.accounts.mail import AccountMailer
    from cloud_agent.storage.assembly import Storage


async def run_account_broadcasts(
    storage: Storage, mailer: AccountMailer, *, interval_seconds: float = BROADCAST_INTERVAL_S
) -> None:
    """Repeat the sending pass until cancelled."""

    async def one_pass() -> None:
        await run_pass(
            lambda: send_due_broadcasts(storage.local_accounts, mailer),
            deadline_seconds=3600.0,
            description="Local account mail",
            event="agent_account_broadcasts",
        )

    await run_every(interval_seconds, one_pass)


__all__ = ["run_account_broadcasts"]
