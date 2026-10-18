"""The reaper is what actually collects abandoned staged uploads.

The repository already deletes rows no job claimed and no waiting message
holds. Nothing in production called it, so those rows counted against staging
quota forever. These tests pin the call, the 24-hour window, and that a purge
failure is logged without skipping the reaping that follows it.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from tests.support.storage import AllCapabilities

from cloud_agent.workers import attempts


class _Store(AllCapabilities):
    """Enough of a reaper store to finish one pass and record the purge."""

    def __init__(self, *, purge_error: Exception | None = None) -> None:
        self.events: list[object] = []
        self.purge_error = purge_error

    async def list_terminal_resumes_pending(self) -> list[object]:
        return []

    async def reap_offline_hosts(self, **_kwargs: object) -> list[object]:
        return []

    async def finish_host_removals(self) -> list[str]:
        return []

    async def reap_expired_residencies(self) -> list[object]:
        return []

    async def sweep_queued_follow_ups(self) -> None:
        return None

    async def claim_wakeups(self, *, limit: int, thread_id: str | None = None) -> list[object]:
        return []

    async def sweep_goal_scheduling(self) -> dict[str, list[object]]:
        return {"rearmed": [], "stalled": []}

    async def reap_expired(self, *, max_attempts: int) -> list[object]:
        self.events.append("reap_expired")
        return []

    async def reap_stalled_sandbox_snapshot_captures(self) -> list[object]:
        return []

    async def reap_stalled_snapshot_publishes(self) -> list[object]:
        return []

    async def reap_stalled_session_forks(self) -> list[object]:
        return []

    async def purge_stale_staged(self, *, older_than_hours: int) -> int:
        self.events.append(("purge", older_than_hours))
        if self.purge_error is not None:
            raise self.purge_error
        return 0

    async def threads_to_settle(self, *, limit: int = 50) -> list[str]:
        self.events.append("settle_queues")
        return []


async def _one_pass(store: _Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the loop body once, then stop at the sleep that would start the next."""
    sleeps = 0

    async def _sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(attempts.asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        await attempts.reap_expired_attempts(store, interval_seconds=0, max_attempts=1, grants=None)
    assert sleeps == 2


async def test_one_reaper_pass_purges_abandoned_staged_uploads(monkeypatch: pytest.MonkeyPatch):
    """One pass asks the repository for the 24-hour window, then keeps reaping."""
    store = _Store()

    await _one_pass(store, monkeypatch)

    assert attempts.STAGED_UPLOAD_RETENTION_HOURS == 24
    assert store.events == ["reap_expired", ("purge", 24), "settle_queues"]


async def test_a_purge_failure_is_logged_and_does_not_abort_the_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A purge exception stays in its own try, so the queue sweep still runs."""
    store = _Store(purge_error=RuntimeError("attachment store unavailable"))

    with caplog.at_level(logging.WARNING, logger="cloud_agent.app"):
        await _one_pass(store, monkeypatch)

    assert store.events == ["reap_expired", ("purge", 24), "settle_queues"]
    failures = [
        record for record in caplog.records if record.message == "Staged upload purge failed"
    ]
    assert len(failures) == 1
    assert failures[0].exc_info is not None
    assert isinstance(failures[0].exc_info[1], RuntimeError)
    assert "Agent attempt reaper pass failed" not in caplog.text
