"""Replaying settled-terminal recovery markers after a restart.

Ported from the gateway's ``tests/servers/test_bootstrap.py`` at H4, where this
reconciler lived before the control plane became its own service. The sweep
that removed the gateway's agent tests checked each one against this repository
rather than assuming the move had carried it, and this property was not covered
here: a sandbox manager that is *down* must leave the marker set, so the next pass tries
again.

That is the whole point of a durable marker. Marking complete on a failed
resume loses the terminal silently — the job looks recovered, the user opens it,
and there is nothing there. It is worth a test precisely because the failure is
invisible from every side.

The one adaptation: the sandbox manager is resolved **per job** here, not once from the
environment. With a host pool there is no "the" sandbox manager — each job's retained
terminal lives on the machine that ran it — so the double is injected through
``sandbox_manager_for_job`` rather than through the client factory.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tests.support.storage import OneFake

from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.workers.attempts import _reconcile_settled_terminals, reap_expired_attempts

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


async def test_a_settled_terminal_resume_retries_until_the_sandbox_manager_confirms(
    monkeypatch, store: SimpleNamespace
) -> None:
    """A refused resume leaves the marker set; the next pass clears it."""
    sandbox_manager = SimpleNamespace(
        register_shared_sandbox=AsyncMock(return_value={"ok": True}),
        resume_settled_terminals=AsyncMock(
            side_effect=[SandboxManagerError(503, "sandbox manager unavailable"), {"ok": True}]
        ),
    )
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job",
        AsyncMock(return_value=sandbox_manager),
    )

    await _reconcile_settled_terminals(store)
    store.mark_terminal_resume_complete.assert_not_awaited()

    await _reconcile_settled_terminals(store)
    store.mark_terminal_resume_complete.assert_awaited_once_with(job_id="ajob_test")
    assert sandbox_manager.resume_settled_terminals.await_count == 2
    sandbox_manager.resume_settled_terminals.assert_awaited_with(
        "athr_test",
        job_id="ajob_test",
    )


async def test_a_missing_owner_sandbox_manager_keeps_the_marker_for_retry(
    monkeypatch, store: SimpleNamespace
) -> None:
    """A routing/configuration fault must not be reported as recovered."""
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job",
        AsyncMock(return_value=None),
    )

    await _reconcile_settled_terminals(store)

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
    sandbox_manager = AsyncMock()
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job", sandbox_manager
    )

    await _reconcile_settled_terminals(store)

    sandbox_manager.assert_not_awaited()
    store.mark_terminal_resume_complete.assert_awaited_once_with(job_id="ajob_test")


async def test_one_broken_host_does_not_skip_other_terminal_markers(
    monkeypatch, store: SimpleNamespace
) -> None:
    """Recovery is isolated per job because a pool can lose only one sandbox manager."""
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
            side_effect=SandboxManagerError(503, "sandbox manager unavailable")
        )
    )
    ready = SimpleNamespace(
        register_shared_sandbox=AsyncMock(return_value={"ok": True}),
        resume_settled_terminals=AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job",
        AsyncMock(side_effect=[broken, ready]),
    )

    await _reconcile_settled_terminals(store)

    store.mark_terminal_resume_complete.assert_awaited_once_with(job_id="ajob_ready")
    ready.resume_settled_terminals.assert_awaited_once_with(
        "athr_ready",
        job_id="ajob_ready",
    )


async def test_a_sandbox_manager_that_never_answers_does_not_hold_up_the_reaper(
    monkeypatch, store: SimpleNamespace
) -> None:
    """Replay runs beside the reaper's pass, so a silent host delays only its markers.

    A host that drops packets costs each of its markers a full request timeout,
    and its markers stay set, so inside the pass that wait came back on every
    pass -- before any lease was reaped or any grant renewed.
    """
    replay_started = asyncio.Event()
    replay_cancelled = asyncio.Event()

    async def silent_sandbox_manager(_store: Any, _job_id: str | None) -> Any:
        replay_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            replay_cancelled.set()

    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job", silent_sandbox_manager
    )
    store.reap_expired = AsyncMock(return_value=[])
    store.take_revocable_grants = AsyncMock(return_value=[])
    store.list_renewable_grants = AsyncMock(return_value=[])
    grants = SimpleNamespace(renew=AsyncMock(), revoke=AsyncMock(return_value=True))
    task = asyncio.create_task(
        reap_expired_attempts(store, interval_seconds=0.001, grants=grants)  # type: ignore[arg-type]
    )
    try:
        async with asyncio.timeout(10):
            await replay_started.wait()
            while store.list_renewable_grants.await_count < 2:
                await asyncio.sleep(0.005)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert store.reap_expired.await_count >= 2
    store.mark_terminal_resume_complete.assert_not_awaited()
    assert replay_cancelled.is_set(), "stopping the reaper must stop the replay with it"
