"""Scheduling and failure boundaries for shared publication capture."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cloud_agent.workers import snapshots as control_plane


class _SnapshotPort:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls = 0
        self.fail_once = fail_once
        self.called = asyncio.Event()

    async def claim_due_sandbox_snapshot(self) -> dict[str, Any] | None:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise RuntimeError("temporary database failure")
        self.called.set()
        return None


class _ClaimStore:
    """Shaped like the assembled ``Storage``: capabilities hang off named ports.

    The claim used to be reached for with ``getattr(store, ...)`` against the
    top-level store, and a fake carrying the method there kept these tests
    green while the real composition root -- which has it on ``.snapshot`` --
    silently claimed nothing. Keeping the fake's shape honest is what makes a
    coordinator that reaches for the wrong object fail here.
    """

    def __init__(self, *, fail_once: bool = False) -> None:
        self.snapshot = _SnapshotPort(fail_once=fail_once)

    @property
    def calls(self) -> int:
        return self.snapshot.calls

    @property
    def called(self) -> asyncio.Event:
        return self.snapshot.called


@pytest.mark.asyncio
async def test_snapshot_coordinator_runs_on_its_own_short_schedule():
    store = _ClaimStore()
    task = asyncio.create_task(
        control_plane.coordinate_shared_snapshots(store, interval_seconds=0.001)  # type: ignore[arg-type]
    )
    try:
        await asyncio.wait_for(store.called.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.calls >= 1


@pytest.mark.asyncio
async def test_snapshot_coordinator_survives_one_failed_pass():
    store = _ClaimStore(fail_once=True)
    task = asyncio.create_task(
        control_plane.coordinate_shared_snapshots(store, interval_seconds=0.001)  # type: ignore[arg-type]
    )
    try:
        await asyncio.wait_for(store.called.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.calls >= 2


def test_snapshot_coordinator_rejects_a_non_positive_interval():
    async def run() -> None:
        await control_plane.coordinate_shared_snapshots(  # type: ignore[arg-type]
            _ClaimStore(), interval_seconds=0
        )

    with pytest.raises(ValueError, match="positive"):
        asyncio.run(run())


def _claimed_snapshot() -> dict[str, Any]:
    return {
        "snapshot_id": 7,
        "sandbox_id": "asbx_shared",
        "generation": 2,
        "container_id": "container-shared",
        "workspace_epoch": 4,
        "base_sha": "a" * 40,
        "anchor_job_id": "ajob_one",
    }


class _CapturePort:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []
        self.deferred: list[dict[str, Any]] = []

    async def claim_due_sandbox_snapshot(self) -> dict[str, Any] | None:
        return _claimed_snapshot()

    async def record_sandbox_snapshot_capture(self, **kwargs: Any) -> str:
        self.recorded.append(kwargs)
        return "captured"

    async def defer_sandbox_snapshot_capture(self, **kwargs: Any) -> bool:
        self.deferred.append(kwargs)
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", [False, True])
async def test_a_receipt_patch_past_the_capture_limit_is_never_recorded(monkeypatch, oversized):
    """No honest host produces a patch past its own Git output limit."""
    import hashlib

    from cloud_agent import sandbox_manager_routing

    patch = "x" * (control_plane.MAX_SNAPSHOT_PATCH_BYTES + (1 if oversized else 0))
    claimed = _claimed_snapshot()

    class _Broker:
        async def capture_shared_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
            return {
                "snapshot_id": claimed["snapshot_id"],
                "sandbox_id": claimed["sandbox_id"],
                "sandbox_generation": claimed["generation"],
                "container_id": claimed["container_id"],
                "workspace_epoch": claimed["workspace_epoch"],
                "base_sha": claimed["base_sha"],
                "patch": patch,
                "content_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            }

    async def sandbox_manager_for_job(_store: Any, _job_id: str) -> _Broker:
        return _Broker()

    monkeypatch.setattr(sandbox_manager_routing, "sandbox_manager_for_job", sandbox_manager_for_job)
    port = _CapturePort()
    store = type("Store", (), {"snapshot": port, "host_pool": None})()

    assert await control_plane._capture_due_shared_snapshot(store) is True
    if oversized:
        assert port.recorded == []
        [deferred] = port.deferred
        assert deferred["dirty"] is True
        assert "patch size limit" in deferred["detail"]
    else:
        assert [row["patch"] for row in port.recorded] == [patch]
        assert port.deferred == []


class _ReimportPort(_CapturePort):
    """A capture port that can also hand a capture back for a base re-import."""

    def __init__(self, *, importable: bool) -> None:
        super().__init__()
        self.importable = importable
        self.reimports: list[dict[str, Any]] = []

    async def reimport_snapshot_base(self, **kwargs: Any) -> bool:
        self.reimports.append(kwargs)
        return self.importable


def _refusing_sandbox_manager(monkeypatch, status_code: int, detail: str) -> None:
    from cloud_agent import sandbox_manager_routing
    from cloud_agent.sandbox_manager_client import SandboxManagerError

    class _Refusing:
        async def capture_shared_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
            raise SandboxManagerError(status_code, detail)

    async def sandbox_manager_for_job(_store: Any, _job_id: str) -> _Refusing:
        return _Refusing()

    monkeypatch.setattr(sandbox_manager_routing, "sandbox_manager_for_job", sandbox_manager_for_job)


@pytest.mark.asyncio
@pytest.mark.parametrize("importable", [True, False], ids=["published", "never-published"])
async def test_a_capture_whose_base_git_pruned_imports_it_again_instead_of_failing(
    monkeypatch, importable
):
    """The base a capture is cut from is imported again rather than failing the snapshot.

    Git prunes a published base the host imported without a ref, and every
    later capture is cut from that base, so failing it made the workspace
    unpublishable for good. A base no publication made cannot be fetched
    again; that capture still fails, now saying why.
    """
    from cloud_agent_shared.shared_protocol import SNAPSHOT_BASE_MISSING

    _refusing_sandbox_manager(monkeypatch, 409, SNAPSHOT_BASE_MISSING)
    port = _ReimportPort(importable=importable)
    store = type("Store", (), {"snapshot": port, "host_pool": None})()

    assert await control_plane._capture_due_shared_snapshot(store) is True

    claimed = _claimed_snapshot()
    assert port.reimports == [
        {
            "snapshot_id": claimed["snapshot_id"],
            "sandbox_id": claimed["sandbox_id"],
            "sandbox_generation": claimed["generation"],
            "container_id": claimed["container_id"],
            "base_sha": claimed["base_sha"],
        }
    ]
    assert port.recorded == []
    if importable:
        assert port.deferred == []
    else:
        [deferred] = port.deferred
        assert deferred["dirty"] is True
        assert "base commit" in deferred["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "detail", "dirty"),
    [
        (409, "shared workspace still has active mutations", False),
        (502, "fatal: failed to unpack tree object", True),
    ],
    ids=["busy", "refused"],
)
async def test_other_capture_refusals_keep_their_meaning(monkeypatch, status_code, detail, dirty):
    """Only the exact missing-base answer imports again; a busy host still retries."""
    _refusing_sandbox_manager(monkeypatch, status_code, detail)
    port = _ReimportPort(importable=True)
    store = type("Store", (), {"snapshot": port, "host_pool": None})()

    assert await control_plane._capture_due_shared_snapshot(store) is True

    assert port.reimports == []
    [deferred] = port.deferred
    assert deferred["dirty"] is dirty
    assert deferred["detail"] == detail
