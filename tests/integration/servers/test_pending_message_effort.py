"""A queued message keeps the reasoning level its provider declares, through dispatch.

Against the real store and the same harness as ``test_pending_message_queue``:
a level chosen for a waiting message, and one inherited by the message behind
it, are the levels the turns they become run at.
"""

from __future__ import annotations

from tests.integration.servers import test_pending_message_queue as queue

from cloud_agent_shared.model_connections import PROTOCOLS

pytestmark = queue.pytestmark
#: The queue tests' fixtures, shared rather than copied.
pool = queue.pool
api = queue.api
Api = queue.Api
_OWNER = queue._OWNER


async def _declare_levels(api: Api, levels: list[str]) -> None:
    """As a refresh would store it: every model's provider declares ``levels``."""
    row = await api.store.model_connections.get_model_connection(
        user_id=_OWNER, connection_id=api.connection["id"]
    )
    assert row is not None
    fact = {"status": "supported", "levels": levels, "source": "test", "reason": ""}
    checks = {
        model: {
            **{k: v for k, v in entry.items() if k != "revision"},
            "efforts": {protocol: dict(fact) for protocol in PROTOCOLS},
        }
        for model, entry in row["model_checks"].items()
    }
    assert await api.store.model_connections.commit_verified_update(
        user_id=_OWNER, connection_id=row["id"], expected=row, changes={}, model_checks=checks
    )


async def test_a_queued_message_keeps_its_declared_level_until_it_runs(api: Api):
    await _declare_levels(api, ["high", "max"])
    job = await api.create(reasoning_effort="high")
    thread_id = job["thread_id"]
    running = await api.start(job)
    queued = await api.send(thread_id, "think harder", reasoning_effort="max")
    assert queued.status_code == 201, queued.text
    inherited = await api.send(thread_id, "and keep going")
    assert inherited.status_code == 201, inherited.text

    await api.finish(running)

    turns = await api.turns(thread_id)
    assert [(t["task_prompt"], t["reasoning_effort"]) for t in turns[:2]] == [
        ("fix it", "high"),
        ("think harder", "max"),
    ]
