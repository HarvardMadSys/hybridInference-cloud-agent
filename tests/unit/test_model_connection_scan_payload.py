"""A group's trusted snapshot is scanned for its own connection keys and nobody else's.

The scanner runs inside the group's sandbox, so whatever is handed to it is
handed to that sandbox: a key the owner gave another connection, used in
another group, must never be in the payload. And a scan that cannot cover
every key the group was handed does not run at all.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from cloud_agent import sandbox_manager_routing
from cloud_agent.application.model_connections import (
    MAX_SCAN_KEYS,
    model_connection_scan_cipher,
)
from cloud_agent.workers import snapshots as control_plane

_SECRET = "model-connection-scan-test-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _encryption(monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _SECRET)


def _claimed(sandbox_id: str) -> dict[str, Any]:
    return {
        "snapshot_id": 7,
        "sandbox_id": sandbox_id,
        "generation": 2,
        "container_id": "container-shared",
        "workspace_epoch": 4,
        "base_sha": "a" * 40,
        "anchor_job_id": "ajob_one",
    }


class _Connections:
    """Scan copies by group, as ``bind_residency`` attributes them."""

    def __init__(self, copies: dict[str, list[str]]) -> None:
        cipher = model_connection_scan_cipher()
        self.copies = {
            group: [cipher.encrypt(key) for key in keys] for group, keys in copies.items()
        }
        self.asked: list[str] = []

    async def scan_key_ciphertexts(self, *, sandbox_id: str, limit: int) -> list[str]:
        self.asked.append(sandbox_id)
        return self.copies.get(sandbox_id, [])[:limit]


class _Snapshots:
    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id
        self.recorded: list[dict[str, Any]] = []
        self.deferred: list[dict[str, Any]] = []

    async def claim_due_sandbox_snapshot(self) -> dict[str, Any]:
        return _claimed(self.sandbox_id)

    async def record_sandbox_snapshot_capture(self, **kwargs: Any) -> str:
        self.recorded.append(kwargs)
        return "captured"

    async def defer_sandbox_snapshot_capture(self, **kwargs: Any) -> bool:
        self.deferred.append(kwargs)
        return True


async def _capture(monkeypatch, sandbox_id: str, copies: dict[str, list[str]]):
    sent: list[dict[str, Any]] = []
    claimed = _claimed(sandbox_id)

    class _Broker:
        async def capture_shared_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
            sent.append(snapshot)
            return {
                "snapshot_id": claimed["snapshot_id"],
                "sandbox_id": claimed["sandbox_id"],
                "sandbox_generation": claimed["generation"],
                "container_id": claimed["container_id"],
                "workspace_epoch": claimed["workspace_epoch"],
                "base_sha": claimed["base_sha"],
                "patch": "",
                "content_sha256": hashlib.sha256(b"").hexdigest(),
            }

    async def manager_for_job(_store: Any, _job_id: str) -> _Broker:
        return _Broker()

    monkeypatch.setattr(sandbox_manager_routing, "sandbox_manager_for_job", manager_for_job)
    snapshots = _Snapshots(sandbox_id)
    connections = _Connections(copies)
    store = type(
        "Store",
        (),
        {"snapshot": snapshots, "host_pool": None, "model_connections": connections},
    )()
    assert await control_plane._capture_due_shared_snapshot(store) is True
    return sent, snapshots, connections


async def test_only_the_keys_this_group_was_handed_reach_its_scanner(monkeypatch):
    sent, snapshots, connections = await _capture(
        monkeypatch,
        "asbx_a",
        {"asbx_a": ["group-a-key-1", "group-a-rotated-key-2"], "asbx_b": ["group-b-key"]},
    )

    assert connections.asked == ["asbx_a"]
    assert sorted(sent[0]["known_secrets"].values()) == ["group-a-key-1", "group-a-rotated-key-2"]
    assert "group-b-key" not in str(sent)
    assert snapshots.recorded


async def test_a_group_that_never_ran_on_a_connection_sends_nothing_extra(monkeypatch):
    sent, _snapshots, _connections = await _capture(
        monkeypatch, "asbx_gateway", {"asbx_b": ["group-b-key"]}
    )
    assert "known_secrets" not in sent[0]
    assert "group-b-key" not in str(sent)


async def test_more_keys_than_a_scan_covers_fails_the_capture_closed(monkeypatch):
    sent, snapshots, _connections = await _capture(
        monkeypatch, "asbx_a", {"asbx_a": [f"key-{n}" for n in range(MAX_SCAN_KEYS + 1)]}
    )
    assert sent == [], "nothing reaches the host when the scan would be partial"
    assert snapshots.recorded == []
    assert snapshots.deferred and snapshots.deferred[0]["dirty"] is True
