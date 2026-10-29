"""Administrator mail leaves by the application's account mailer.

Found by the real-browser smoke: the background sender built a mailer of its
own, so a mailer installed on the application (the one preview, test sends
and every account notice use) never carried administrator mail.
"""

from __future__ import annotations

import pytest

from cloud_agent.accounts.delivery import DeliveryMailer
from cloud_agent.bootstrap import identity
from cloud_agent.workers import account_broadcasts

pytestmark = pytest.mark.asyncio


async def test_the_broadcast_loop_uses_the_installed_mailer(monkeypatch):
    seen: list[object] = []

    async def loop(storage, mailer, **_kwargs):
        seen.append(mailer)

    monkeypatch.setattr(account_broadcasts, "run_account_broadcasts", loop)
    installed = object()
    tasks = dict(identity.local_identity_tasks(object(), object(), local=True, mailer=installed))
    await tasks["account_broadcast_task"]
    tasks["account_access_task"].close()
    fallback = dict(identity.local_identity_tasks(object(), object(), local=True))
    await fallback["account_broadcast_task"]
    fallback["account_access_task"].close()
    assert seen[0] is installed
    assert isinstance(seen[1], DeliveryMailer)
    assert identity.local_identity_tasks(object(), object(), local=False, mailer=installed) == []
