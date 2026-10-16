"""The move-out sweep stops idle groups and hands over covered ones, nothing more."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cloud_agent.domain.compute import move_out_window_seconds
from cloud_agent.workers.sandbox_move_out import MOVE_OUT_ACTOR, move_out_sandboxes


class _Sandboxes:
    def __init__(self, *, running: list[dict], results: dict[str, str], handed_over: list[str]):
        self.running = running
        self.results = results
        self.handed_over = handed_over
        self.stops: list[dict] = []

    async def release_moving_out_groups(self) -> list[str]:
        return list(self.handed_over)

    async def sandboxes_moving_out(self, *, limit: int) -> list[dict]:
        return list(self.running)

    async def request_move_out_stop(self, **kwargs) -> str:
        self.stops.append(kwargs)
        return self.results[kwargs["sandbox_id"]]


@pytest.mark.asyncio
async def test_each_idle_group_is_asked_to_stop_and_a_busy_one_waits() -> None:
    sandboxes = _Sandboxes(
        running=[
            {"sandbox_id": "asbx_idle", "host": "h", "generation": 2, "container_id": "c1"},
            {"sandbox_id": "asbx_busy", "host": "h", "generation": 1, "container_id": "c2"},
        ],
        results={"asbx_idle": "accepted", "asbx_busy": "busy"},
        handed_over=["asbx_covered"],
    )

    progress = await move_out_sandboxes(SimpleNamespace(sandbox=sandboxes))

    assert progress == 2
    assert sandboxes.stops == [
        {
            "sandbox_id": "asbx_idle",
            "generation": 2,
            "container_id": "c1",
            "actor_id": MOVE_OUT_ACTOR,
        },
        {
            "sandbox_id": "asbx_busy",
            "generation": 1,
            "container_id": "c2",
            "actor_id": MOVE_OUT_ACTOR,
        },
    ]


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ({}, 7200),
        ({"move_out_before_seconds": 0}, 0),
        ({"rotation_enabled": True}, 0),
        # A granted extension reaches four hours; the window stays inside it.
        ({"enabled": True, "extend_to_horizon_seconds": 14400}, 7200),
        ({"enabled": True, "extend_to_horizon_seconds": 3600}, 3000),
    ],
)
def test_the_move_out_window_never_reopens_after_an_extension(policy, expected) -> None:
    assert move_out_window_seconds(policy) == expected
