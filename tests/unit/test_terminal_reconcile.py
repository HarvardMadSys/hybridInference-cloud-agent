"""Replaying settled-terminal recovery markers after a restart.

Ported from the gateway's ``tests/servers/test_bootstrap.py`` at H4, where this
reconciler lived before the control plane became its own service. The sweep
that removed the gateway's agent tests checked each one against this repository
rather than assuming the move had carried it, and this property was not covered
here: a broker that is *down* must leave the marker set, so the next pass tries
again.

That is the whole point of a durable marker. Marking complete on a failed
resume loses the terminal silently — the job looks recovered, the user opens it,
and there is nothing there. It is worth a test precisely because the failure is
invisible from every side.

The one adaptation: the broker is resolved **per job** here, not once from the
environment. With a host pool there is no "the" broker — each job's retained
terminal lives on the machine that ran it — so the double is injected through
``broker_for_job`` rather than through the client factory.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent import app as control_plane
from cloud_agent.workspace_broker_client import WorkspaceBrokerError
from tests.support.storage import OneFake

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store() -> OneFake:
    return OneFake(
        SimpleNamespace(
            list_terminal_resumes_pending=AsyncMock(return_value=["ajob_test"]),
            mark_terminal_resume_complete=AsyncMock(return_value=True),
            get_job=AsyncMock(
                return_value={
                    "id": "ajob_test",
                    "thread_id": "athr_test",
                    "thread_sandbox": {
                        "shared_live": True,
                        "thread_id": "athr_test",
                        "origin_thread_id": "athr_test",
                    },
                }
            ),
        )
    )


async def test_a_settled_terminal_resume_retries_until_the_broker_confirms(
    monkeypatch, store: SimpleNamespace
) -> None:
    """A refused resume leaves the marker set; the next pass clears it."""
    broker = SimpleNamespace(
        register_shared_sandbox=AsyncMock(return_value={"ok": True}),
        resume_settled_terminals=AsyncMock(
            side_effect=[WorkspaceBrokerError(503, "broker unavailable"), {"ok": True}]
        ),
    )
    monkeypatch.setattr(
        "cloud_agent.broker_routing.broker_for_job",
        AsyncMock(return_value=broker),
    )

    await control_plane._reconcile_settled_terminals(store)
    store.mark_terminal_resume_complete.assert_not_awaited()

    await control_plane._reconcile_settled_terminals(store)
    store.mark_terminal_resume_complete.assert_awaited_once_with(job_id="ajob_test")
    assert broker.resume_settled_terminals.await_count == 2
    broker.resume_settled_terminals.assert_awaited_with(
        "athr_test",
        job_id="ajob_test",
    )


async def test_a_missing_owner_broker_keeps_the_marker_for_retry(
    monkeypatch, store: SimpleNamespace
) -> None:
    """A routing/configuration fault must not be reported as recovered."""
    monkeypatch.setattr(
        "cloud_agent.broker_routing.broker_for_job",
        AsyncMock(return_value=None),
    )

    await control_plane._reconcile_settled_terminals(store)

    store.mark_terminal_resume_complete.assert_not_awaited()


async def test_a_cancelled_pre_claim_job_has_no_workspace_to_resume(
    monkeypatch, store: SimpleNamespace
) -> None:
    """A queued cancellation can retire its marker without contacting Docker."""
    store.get_job.return_value = {
        "id": "ajob_test",
        "thread_id": "athr_test",
        "thread_sandbox": None,
    }
    broker = AsyncMock()
    monkeypatch.setattr("cloud_agent.broker_routing.broker_for_job", broker)

    await control_plane._reconcile_settled_terminals(store)

    broker.assert_not_awaited()
    store.mark_terminal_resume_complete.assert_awaited_once_with(job_id="ajob_test")


async def test_one_broken_host_does_not_skip_other_terminal_markers(
    monkeypatch, store: SimpleNamespace
) -> None:
    """Recovery is isolated per job because a pool can lose only one broker."""
    store.list_terminal_resumes_pending.return_value = ["ajob_broken", "ajob_ready"]
    store.get_job.side_effect = [
        {
            "id": "ajob_broken",
            "thread_id": "athr_broken",
            "thread_sandbox": {"shared_live": True},
        },
        {
            "id": "ajob_ready",
            "thread_id": "athr_ready",
            "thread_sandbox": {"shared_live": True},
        },
    ]
    broken = SimpleNamespace(
        register_shared_sandbox=AsyncMock(
            side_effect=WorkspaceBrokerError(503, "broker unavailable")
        )
    )
    ready = SimpleNamespace(
        register_shared_sandbox=AsyncMock(return_value={"ok": True}),
        resume_settled_terminals=AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        "cloud_agent.broker_routing.broker_for_job",
        AsyncMock(side_effect=[broken, ready]),
    )

    await control_plane._reconcile_settled_terminals(store)

    store.mark_terminal_resume_complete.assert_awaited_once_with(job_id="ajob_ready")
    ready.resume_settled_terminals.assert_awaited_once_with(
        "athr_ready",
        job_id="ajob_ready",
    )
