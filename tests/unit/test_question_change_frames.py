"""The shared-turn stream forwards coalesced native question changes."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from cloud_agent_host.manager_api.turns import _TurnBoundary, _turn_frames


def test_shared_turn_emits_a_question_change_frame_while_native_output_is_silent():
    gate = threading.Event()
    started = threading.Event()

    class Turn:
        pending = True

        def take_question_change(self) -> bool:
            changed, self.pending = self.pending, False
            return changed

        def lines(self):
            started.set()
            gate.wait(2)
            yield SimpleNamespace(event_type="message", payload={"text": "done"}, raw_line="done")

    class Owner:
        def take_reported_checkpoint(self, _thread_id, _turn):
            return None

        def abort_turn(self, _thread_id, _token, _turn):
            gate.set()
            return None

    frames = _turn_frames(Owner(), "thread", "lease", Turn(), _TurnBoundary())
    try:
        frame = json.loads(next(frames))
        assert frame == {"kind": "question_changed"}
        assert started.wait(1)
    finally:
        gate.set()
        frames.close()
