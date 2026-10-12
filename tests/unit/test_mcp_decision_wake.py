"""A tool call held for its owner's approval is dispatched when they decide.

It used to read its row again every second until then. A decision now notifies
the relay process, whose listener wakes the request holding that call; the
clock is only a backstop for what announces nothing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_agent.api import mcp_relay
from cloud_agent.workers.mcp_decision_signal import McpDecisionSignal

INVOCATION = 41
FENCE = {
    "config_revision": 1,
    "authorization_epoch": 1,
    "capability_revision": 1,
    "policy_revision": 1,
    "attempt_id": 7,
    "lease_generation": 1,
}


class Store:
    """One held call, and the lookups a held call makes each time it looks."""

    def __init__(self) -> None:
        self.status = "pending"
        self.looks = 0
        self.credential_queries = SimpleNamespace(
            resolve_model_credential=AsyncMock(return_value={"identity": "the job's"})
        )
        self.mcp = SimpleNamespace(
            get_mcp_invocation=self._get,
            claim_mcp_invocation=AsyncMock(return_value={"dispatch_generation": 3}),
            expire_mcp_invocation=AsyncMock(return_value=True),
        )

    async def _get(self, *, invocation_id: int) -> dict[str, Any]:
        self.looks += 1
        return {"id": invocation_id, "status": self.status}


class Request:
    """The relay request holding the call, in a process that may listen."""

    def __init__(self, signal: McpDecisionSignal | None) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(mcp_decision_signal=signal))

    async def is_disconnected(self) -> bool:
        return False


def _authorized() -> SimpleNamespace:
    return SimpleNamespace(
        job_id="ajob_1",
        alias="github",
        connection_id=5,
        dispatches=[],
        server=None,
        connection=None,
        secret=None,
    )


@pytest.fixture(autouse=True)
def _live_binding(monkeypatch):
    """The connection the call was held for is still the one selected."""
    current = SimpleNamespace(
        binding=SimpleNamespace(enabled=True, policy_revision=1),
        connection_id=5,
        connection=SimpleNamespace(config_revision=1, authorization_epoch=1, capability_revision=1),
        server="server",
        secret="secret",
    )
    monkeypatch.setattr(mcp_relay, "_resolve_live_binding", AsyncMock(return_value=current))


def _hold(store: Store, request: Request | None, *, expires_in_s: float = 600.0) -> asyncio.Task:
    authorized = _authorized()
    task = asyncio.create_task(
        mcp_relay._wait_for_approval(
            authorized,  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
            invocation={
                "id": INVOCATION,
                "expires_at": datetime.now(UTC) + timedelta(seconds=expires_in_s),
            },
            fence=dict(FENCE),
            request_id=1,
            request=request,  # type: ignore[arg-type]
        )
    )
    task.authorized = authorized  # type: ignore[attr-defined]
    return task


async def test_an_approval_dispatches_the_call_at_once(monkeypatch):
    monkeypatch.setattr(mcp_relay, "_APPROVAL_IDLE_S", 30.0)
    signal = McpDecisionSignal()
    signal.reset(listening=True)
    store = Store()
    held = _hold(store, Request(signal))
    await asyncio.sleep(0.05)
    assert store.looks == 1, "waiting for the decision reads nothing"

    store.status = "approved"
    signal.notified(str(INVOCATION))

    assert await asyncio.wait_for(held, 0.5) is None
    assert held.authorized.dispatches == [(INVOCATION, 3)]  # type: ignore[attr-defined]
    assert signal.subscribers(str(INVOCATION)) == 0


async def test_a_refusal_answers_the_caller_at_once(monkeypatch):
    monkeypatch.setattr(mcp_relay, "_APPROVAL_IDLE_S", 30.0)
    signal = McpDecisionSignal()
    signal.reset(listening=True)
    store = Store()
    held = _hold(store, Request(signal))
    await asyncio.sleep(0.05)

    store.status = "denied"
    signal.notified(str(INVOCATION))

    reply = await asyncio.wait_for(held, 0.5)
    assert reply is not None and "denied" in reply["error"]["message"]
    store.mcp.claim_mcp_invocation.assert_not_awaited()


async def test_without_a_listener_it_looks_on_its_clock(monkeypatch):
    monkeypatch.setattr(mcp_relay, "_APPROVAL_POLL_S", 0.01)
    monkeypatch.setattr(mcp_relay, "_APPROVAL_IDLE_S", 30.0)
    signal = McpDecisionSignal()  # not listening: nothing would wake it
    store = Store()
    held = _hold(store, Request(signal))
    await asyncio.sleep(0.05)
    store.status = "approved"
    assert await asyncio.wait_for(held, 0.5) is None

    store = Store()
    held = _hold(store, None)
    await asyncio.sleep(0.05)
    store.status = "approved"
    assert await asyncio.wait_for(held, 0.5) is None


async def test_the_deadline_ends_the_wait_without_a_decision(monkeypatch):
    monkeypatch.setattr(mcp_relay, "_APPROVAL_IDLE_S", 30.0)
    signal = McpDecisionSignal()
    signal.reset(listening=True)
    store = Store()
    held = _hold(store, Request(signal), expires_in_s=0.1)

    reply = await asyncio.wait_for(held, 0.5)
    assert reply is not None and "expired" in reply["error"]["message"]
    store.mcp.expire_mcp_invocation.assert_awaited_once_with(invocation_id=INVOCATION)
