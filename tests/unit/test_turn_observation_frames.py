"""Question hints and checkpoints stay available during silent turns."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from tests.unit.test_agent_sandbox_manager import _app, _SharedSupervisor

from cloud_agent_host.sandbox_manager import SharedTurnRequest


@pytest.mark.asyncio
async def test_question_hint_and_checkpoint_precede_any_native_event():
    """Question hints and turn-fenced checkpoints both precede native output."""
    gate = threading.Event()
    turn_started = threading.Event()

    class QuietTurn:
        def __init__(self):
            self.question_pending = True

        def take_question_change(self):
            pending, self.question_pending = self.question_pending, False
            return pending

        @staticmethod
        def lines():
            turn_started.set()
            gate.wait(2)
            yield SimpleNamespace(event_type="message", payload={"text": "done"}, raw_line="done")

    supervisor = _SharedSupervisor()
    supervisor.start_turn = lambda *_args, **_kw: QuietTurn()  # type: ignore[attr-defined]
    pending = [{"measurement_id": "sample", "state": "collecting", "report_seq": 1}]
    supervisor.take_reported_checkpoint = (  # type: ignore[attr-defined]
        lambda _thread_id, _turn: pending.pop() if pending else None
    )
    supervisor.abort_turn = lambda *_args: gate.set()  # type: ignore[attr-defined]
    app = _app(supervisor)
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/shared-sandboxes/{thread_id}/turn"
    )
    response = await endpoint(
        "athr_source", SharedTurnRequest(token="lease", prompt="work", model="model")
    )
    try:
        question = json.loads(await asyncio.wait_for(anext(response.body_iterator), timeout=1))
        checkpoint = json.loads(await asyncio.wait_for(anext(response.body_iterator), timeout=1))
        assert question == {"kind": "question_changed"}
        assert checkpoint["kind"] == "resource_checkpoint"
        assert checkpoint["turn_resources"]["state"] == "collecting"
        assert turn_started.wait(1)
        assert not gate.is_set()
    finally:
        gate.set()
        await response.body_iterator.aclose()
