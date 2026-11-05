"""The host beat's view of this host's retained sandboxes, and its residency cleanup.

Both run on the host-heartbeat thread, so a sandbox manager that is slow or
gone costs a beat its observation or its cleanup, never the beat itself.
"""

from __future__ import annotations

import socket
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from cloud_agent_host import host_service
from cloud_agent_host.session_supervisor_client import SessionSupervisorError


def test_reconstructed_running_group_reports_unknown_activity(monkeypatch):
    """A sandbox manager restart must not renew a full idle window on every beat."""

    class Supervisor:
        def list(self):
            return [
                {
                    "thread_id": "athr_reconstructed",
                    "sandbox_id": "asbx_reconstructed",
                    "container_id": "container-reconstructed",
                    "generation": 1,
                    "activation_generation": 0,
                    "running": True,
                    "shared_live": True,
                    "group_busy": False,
                    "terminal_count": 0,
                    "operation_count": 0,
                    "workspace_suspended": False,
                    "idle_deadline_monotonic": None,
                    "members": [],
                }
            ]

        def close(self):
            pass

    monkeypatch.setattr(host_service, "session_supervisor_from_env", lambda **_kwargs: Supervisor())

    rows = host_service._retained_sandbox_heartbeat_rows()

    assert rows is not None
    assert rows[0]["activity_active"] is None
    assert rows[0]["idle_remaining_seconds"] is None


@pytest.mark.parametrize("invalid_usage", [False, True])
@pytest.mark.parametrize("terminal_count", [2, 4096, 4097])
def test_retained_heartbeat_passes_group_resources_and_terminal_count(
    monkeypatch, invalid_usage, terminal_count
):
    sample = {
        "sample_id": "resource-sample",
        "sample_age_ms": 4000,
        "memory_bytes": "invalid" if invalid_usage else 1024,
        "cpu_cores": 1.5,
    }
    supervisor = SimpleNamespace(
        list=lambda: [
            {
                "thread_id": "athr_source",
                "sandbox_id": "asbx_source",
                "container_id": "container-source",
                "generation": 1,
                "activation_generation": 1,
                "running": True,
                "shared_live": True,
                "terminal_count": terminal_count,
                "members": [],
                "resource_usage": sample,
            }
        ],
        close=Mock(),
    )
    monkeypatch.setattr(host_service, "session_supervisor_from_env", lambda **_kwargs: supervisor)
    rows = host_service._retained_sandbox_heartbeat_rows()
    assert rows is not None
    assert rows[0]["container_id"] == "container-source"
    assert rows[0]["activity_active"] is True
    assert rows[0]["terminal_count"] == (terminal_count if terminal_count <= 4096 else None)
    if invalid_usage:
        assert rows[0]["resource_usage"] is None
    else:
        assert rows[0]["resource_usage"]["sample_id"] == "resource-sample"
        assert rows[0]["resource_usage"]["sample_age_ms"] == 4000
        assert rows[0]["resource_usage"]["memory_bytes"] == 1024
        assert rows[0]["resource_usage"]["scope"] == "container"
    supervisor.close.assert_called_once()


def test_a_sandbox_manager_that_never_answers_costs_the_host_beat_only_its_observation(
    monkeypatch,
):
    """The observation runs on the host-heartbeat thread, ahead of the beat itself.

    It used the sandbox manager client's 180-second ceiling, so a sandbox
    manager that accepted the request and never answered held the beat for
    three minutes -- twice the silence after which the control plane counts
    the host offline and refuses its claims.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    # Connections complete in the backlog and are never read or answered.
    listener.listen(8)
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_URL", f"http://127.0.0.1:{listener.getsockname()[1]}")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "sandbox-manager-token")
    monkeypatch.setattr(host_service, "_HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S", 0.2)
    observed: list[Any] = []
    beat = threading.Thread(
        target=lambda: observed.append(host_service._retained_sandbox_heartbeat_rows()),
        daemon=True,
    )
    try:
        beat.start()
        beat.join(timeout=10)
        assert not beat.is_alive(), "the host beat waited on the sandbox manager"
        # "Not observed this beat", never an authoritative empty list.
        assert observed == [None]
    finally:
        listener.close()


def test_residency_cleanup_waits_on_a_silent_sandbox_manager_no_longer_than_the_beat(
    monkeypatch,
):
    """The cleanup a beat's answer asks for runs on the same heartbeat thread.

    It still used the 180-second sandbox manager client, so a sandbox manager
    that never answered held the next beat for three minutes and the host went
    offline.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_URL", f"http://127.0.0.1:{listener.getsockname()[1]}")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "sandbox-manager-token")
    monkeypatch.setattr(host_service, "_HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S", 0.2)
    beat = threading.Thread(
        target=host_service._handle_host_residencies_lost,
        args=([{"thread_id": "athr_1", "generation": 3}],),
        daemon=True,
    )
    try:
        beat.start()
        beat.join(timeout=10)
        assert not beat.is_alive(), "the host beat waited on the sandbox manager"
    finally:
        listener.close()


def test_residency_cleanup_leaves_the_rest_to_a_later_beat_once_its_time_is_spent(
    monkeypatch,
):
    """A slow sandbox manager gets one call per beat, not one ceiling per lost member.

    Each verdict is named again by every beat while the sandbox manager still
    reports the member live, so nothing is lost by stopping; waiting out one
    ceiling per member would hold the next beat past the host's offline
    threshold.
    """
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    members = [{"thread_id": f"athr_{index}"} for index in range(3)]
    fenced: list[tuple[str, int]] = []

    class SandboxManager:
        def list(self):
            return [{"members": members}]

        def shared_residency_lost(self, thread_id, *, generation):
            fenced.append((thread_id, generation))
            # A call that used the whole ceiling before it was answered.
            clock.now += host_service._HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S
            raise SessionSupervisorError(503, "shared Session Supervisor is unavailable")

        def close(self):
            pass

    timeouts: list[float | None] = []

    def sandbox_manager(*, timeout=None):
        timeouts.append(timeout)
        return SandboxManager()

    monkeypatch.setattr(host_service, "session_supervisor_from_env", sandbox_manager)

    host_service._handle_host_residencies_lost(
        [{"thread_id": f"athr_{index}", "generation": index + 1} for index in range(3)]
    )

    assert timeouts == [host_service._HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S]
    assert fenced == [("athr_0", 1)]


def test_residency_cleanup_fences_every_named_member_a_prompt_sandbox_manager_holds(
    monkeypatch,
):
    fenced: list[tuple[str, int]] = []
    sandbox_manager = SimpleNamespace(
        list=lambda: [{"members": [{"thread_id": "athr_1"}, {"thread_id": "athr_2"}]}],
        shared_residency_lost=lambda thread_id, *, generation: fenced.append(
            (thread_id, generation)
        ),
        close=Mock(),
    )
    monkeypatch.setattr(
        host_service, "session_supervisor_from_env", lambda **_kwargs: sandbox_manager
    )

    host_service._handle_host_residencies_lost(
        [
            {"thread_id": "athr_1", "generation": 4},
            {"thread_id": "athr_gone", "generation": 1},
            {"thread_id": "athr_2", "generation": 7},
        ]
    )

    assert fenced == [("athr_1", 4), ("athr_2", 7)]
    sandbox_manager.close.assert_called_once()


@pytest.mark.parametrize("checkpoint", [{"checkpoint_id": "c" * 32}, "not-a-receipt", None])
def test_retained_heartbeat_forwards_the_latest_checkpoint_receipt(monkeypatch, checkpoint):
    supervisor = SimpleNamespace(
        list=lambda: [
            {
                "thread_id": "athr_source",
                "sandbox_id": "asbx_source",
                "container_id": "container-source",
                "generation": 1,
                "activation_generation": 1,
                "running": False,
                "shared_live": True,
                "members": [],
                "checkpoint": checkpoint,
            }
        ],
        close=Mock(),
    )
    monkeypatch.setattr(host_service, "session_supervisor_from_env", lambda **_kwargs: supervisor)

    rows = host_service._retained_sandbox_heartbeat_rows()

    assert rows is not None
    assert rows[0]["checkpoint"] == (checkpoint if isinstance(checkpoint, dict) else None)
