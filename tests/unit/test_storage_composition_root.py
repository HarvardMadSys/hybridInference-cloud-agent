"""The assembled ``Storage`` against the protocols the background tasks declare.

Every long-running task in the control plane names what it reaches for as a
``Protocol`` and is handed the one real ``Storage``. Nothing checked that the
two still agree: a capability that moved between assembly and port left the
protocols type-correct, the unit fakes green, and the deployed task reaching
for an attribute that was no longer there. These tests are that check, and
they run against the composition root rather than a stand-in.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from cloud_agent import ports
from cloud_agent.publish_worker import PublishStorage
from cloud_agent.storage.assembly import Storage
from cloud_agent.workers import attempts as attempt_worker, snapshots as snapshot_worker
from cloud_agent.workers.pr_archive import PrArchiveStorage

#: Every protocol a background task is handed the real ``Storage`` for. They
#: live beside the loops that declare them now, not in `app.py`.
TASK_PROTOCOLS = [
    attempt_worker._SettledTerminals,
    attempt_worker._GrantRenewal,
    attempt_worker._Reaping,
    snapshot_worker._SnapshotCapture,
    PublishStorage,
    PrArchiveStorage,
]


def _assembled_storage() -> Storage:
    """Assemble over a sentinel: every repository only stores the pool."""
    return Storage(cast("Any", object()))


def _port_methods(port: type) -> set[str]:
    return {name for name in vars(port) if not name.startswith("_") and callable(vars(port)[name])}


@pytest.mark.parametrize("protocol", TASK_PROTOCOLS, ids=lambda p: p.__name__)
def test_every_declared_port_is_implemented_by_what_assembly_supplies(protocol: type) -> None:
    storage = _assembled_storage()
    gaps: list[str] = []
    for attribute, port_name in protocol.__annotations__.items():
        implementation = getattr(storage, attribute)
        port = getattr(ports, port_name)
        gaps.extend(
            f"{attribute}.{method}"
            for method in sorted(_port_methods(port))
            if not hasattr(implementation, method)
        )
    assert not gaps, f"{protocol.__name__}: assembly supplies implementations missing {gaps}"


def test_snapshot_work_is_reached_for_through_its_port_not_the_store() -> None:
    """The exact shape the capture coordinator and publish worker depend on.

    ``claim_due_sandbox_snapshot`` and its neighbours live on the snapshot
    port. A caller that reaches for them on the top-level store gets ``None``
    from ``getattr`` and returns without claiming anything -- no exception, no
    log line, and a shared workspace that stays unpublished for good.
    """
    storage = _assembled_storage()
    for method in (
        "claim_due_sandbox_snapshot",
        "pending_snapshot_base_import",
        "reap_stalled_sandbox_snapshot_captures",
        "reap_stalled_snapshot_publishes",
    ):
        assert hasattr(storage.snapshot, method)
        assert not hasattr(storage, method), (
            f"{method} is reachable on the top-level store, so a caller using "
            "getattr against it would silently appear to work"
        )
