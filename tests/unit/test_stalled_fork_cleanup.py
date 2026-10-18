"""A Fork the reaper failed has its processes and grants stopped, one by one.

``reap_stalled_session_forks`` fails a stalled ``preparing`` operation and
closes its residencies in its own commit, and it never returns that operation
again. The reaper's cleanup of what it hands back is therefore the only one
those processes and grants get, so an error on one operation must not skip the
operations after it -- and a sandbox manager that cannot be found must not skip the
revocations, which do not need it.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tests.support.storage import OneFake

from cloud_agent.workers import attempts as attempts_module
from cloud_agent.workers.attempts import _cleanup_failed_session_fork, reap_expired_attempts

pytestmark = pytest.mark.asyncio


def _failed_fork(number: int) -> dict[str, Any]:
    """A stalled ``preparing`` operation, as the store returns it once failed."""
    return {
        "id": f"afork_{number}",
        "source_job_id": f"ajob_{number}",
        "cleanup_residencies": [
            {"thread_id": f"athr_{number}", "generation": number, "grant_id": f"agr_{number}"}
        ],
    }


async def test_one_failing_fork_cleanup_does_not_strand_the_ones_after_it(monkeypatch) -> None:
    """A finalize that raises and a sandbox manager that cannot be found are each contained."""
    sandbox_manager = SimpleNamespace(shared_residency_lost=AsyncMock(return_value={}))

    async def sandbox_manager_for_job(_store: Any, job_id: str | None) -> Any:
        if job_id == "ajob_2":
            raise RuntimeError("host lookup failed")
        return sandbox_manager

    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job", sandbox_manager_for_job
    )
    handed_out = [
        {"id": "afork_1", "recovery_action": "finalize"},
        _failed_fork(2),
        _failed_fork(3),
    ]
    batches = iter([handed_out])
    inner = SimpleNamespace(
        # The recovery passes that run before the Fork reaper find nothing.
        reap_expired=AsyncMock(return_value=[]),
        list_terminal_resumes_pending=AsyncMock(return_value=[]),
        reap_stalled_sandbox_snapshot_captures=AsyncMock(return_value=[]),
        reap_stalled_snapshot_publishes=AsyncMock(return_value=[]),
        reap_stalled_session_forks=AsyncMock(side_effect=lambda: next(batches, [])),
        finalize_session_fork=AsyncMock(side_effect=RuntimeError("finalize failed")),
    )
    grants = SimpleNamespace(revoke=AsyncMock(return_value=True))
    task = asyncio.create_task(
        reap_expired_attempts(OneFake(inner), interval_seconds=0.001, grants=grants)
    )
    try:
        async with asyncio.timeout(10):
            while inner.reap_stalled_session_forks.await_count < 2:
                await asyncio.sleep(0.005)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    inner.finalize_session_fork.assert_awaited_once_with(operation_id="afork_1")
    # The fork whose sandbox manager could not be found still loses its credential.
    assert sorted(call.args[0] for call in grants.revoke.await_args_list) == ["agr_2", "agr_3"]
    sandbox_manager.shared_residency_lost.assert_awaited_once_with("athr_3", generation=3)


async def test_a_fork_whose_host_never_answers_still_loses_its_grants_promptly(
    monkeypatch,
) -> None:
    """Each member's process cleanup waits for its host a bounded time, then the revocation runs."""
    monkeypatch.setattr(attempts_module, "RESIDENCY_CLEANUP_TIMEOUT_S", 0.05)
    gone = asyncio.Event()

    async def never_answers(_thread_id: str, *, generation: int) -> None:
        await gone.wait()

    sandbox_manager = SimpleNamespace(shared_residency_lost=never_answers)

    async def sandbox_manager_for_job(_store: Any, _job_id: str | None) -> Any:
        return sandbox_manager

    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job", sandbox_manager_for_job
    )
    operation = _failed_fork(4)
    operation["cleanup_residencies"].append(
        {"thread_id": "athr_5", "generation": 5, "grant_id": "agr_5"}
    )
    grants = SimpleNamespace(revoke=AsyncMock(return_value=True))

    await asyncio.wait_for(_cleanup_failed_session_fork(object(), operation, grants), 2)

    assert sorted(call.args[0] for call in grants.revoke.await_args_list) == ["agr_4", "agr_5"]
