"""A runner's batch of events through the worker route.

One fence and one commit for the whole batch, each event stored and bounded as
a single upload stores it. These cases reuse the route suite's in-memory store.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.servers.test_agent_jobs_api import (  # noqa: F401 - shared pytest fixtures
    FakeStorage,
    _api_key_secret,
    _configured_identity_issuer,
    _create_job,
    client,
)

if TYPE_CHECKING:
    from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class _QuestionStorage(FakeStorage):
    def __init__(self):
        super().__init__()
        self.question_hints = []

    async def notify_question_change(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> bool:
        if not self._fenced(attempt_id, lease_generation):
            return False
        job = self.jobs.get(job_id)
        if (
            job is None
            or job.get("current_attempt_id") != attempt_id
            or job.get("state") != "running"
        ):
            return False
        self.question_hints.append((str(job["user_id"]), str(job["thread_id"])))
        return True


@pytest.fixture
def store() -> FakeStorage:
    return _QuestionStorage()


async def _worker_auth(client: AsyncClient) -> tuple[str, dict[str, str]]:
    """Create a job, claim it as a worker, and return its id and the worker's header."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    return job_id, {"Authorization": f"Bearer {claim.json()['worker_token']}"}


async def test_worker_batch_stores_events_in_order_and_answers_a_replay_with_their_ids(
    client: AsyncClient, store: FakeStorage
):
    """A batch whose answer was lost is sent again whole, and duplicates nothing."""
    job_id, auth = await _worker_auth(client)
    batch = {
        "events": [
            {"event_type": "lifecycle", "payload": {"phase": "turn_started"}, "event_seq": 1},
            {"event_type": "message", "payload": {"text": "done"}, "event_seq": 2},
        ]
    }
    path = f"/v1/agent/worker/jobs/{job_id}/events:batch"

    first = await client.post(path, json=batch, headers=auth)
    replay = await client.post(path, json=batch, headers=auth)

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json() == {"event_ids": [event["id"] for event in store.events]}
    assert [(event["client_seq"], event["event_type"]) for event in store.events] == [
        (1, "lifecycle"),
        (2, "message"),
    ]
    assistant = [message["content"] for message in store.messages if message["role"] == "assistant"]
    assert assistant == ["done"]


async def test_worker_question_hint_is_thread_scoped_and_fenced(
    client: AsyncClient, store: FakeStorage
):
    job_id, auth = await _worker_auth(client)

    response = await client.post(f"/v1/agent/worker/jobs/{job_id}/questions:changed", headers=auth)

    assert response.status_code == 204
    expected = (store.jobs[job_id]["user_id"], store.jobs[job_id]["thread_id"])
    assert store.question_hints == [expected]

    store.live_fence = (101, 2)
    stale = await client.post(f"/v1/agent/worker/jobs/{job_id}/questions:changed", headers=auth)
    assert stale.status_code == 409
    assert store.question_hints == [expected]


async def test_worker_batch_is_refused_whole_once_the_lease_is_lost(
    client: AsyncClient, store: FakeStorage
):
    job_id, auth = await _worker_auth(client)
    store.live_fence = (101, 2)

    response = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events:batch",
        json={"events": [{"event_type": "message", "payload": {"text": "late"}, "event_seq": 1}]},
        headers=auth,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "lease_lost"
    assert store.events == []


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"event_type": "message", "payload": {"text": "unnumbered"}}],
        [{"event_type": "message", "event_seq": 2}, {"event_type": "message", "event_seq": 1}],
        [{"event_type": "message", "event_seq": 1}, {"event_type": "message", "event_seq": 1}],
        [{"event_type": "message", "event_seq": seq} for seq in range(1, 102)],
        [{"event_type": "message\nevent: forged", "event_seq": 1}],
    ],
    ids=["empty", "unnumbered", "out_of_order", "repeated", "too_many", "unsafe_type"],
)
async def test_worker_batch_refuses_a_malformed_batch_whole(
    client: AsyncClient, store: FakeStorage, events
):
    job_id, auth = await _worker_auth(client)

    response = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events:batch", json={"events": events}, headers=auth
    )

    assert response.status_code == 422
    assert store.events == []


async def test_worker_batch_bounds_each_event_as_a_single_upload_does(
    client: AsyncClient, store: FakeStorage
):
    job_id, auth = await _worker_auth(client)
    oversized = {"tool_use_id": "tool-1", "content": "image data " * 100_000}

    response = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events:batch",
        json={"events": [{"event_type": "tool_result", "payload": oversized, "event_seq": 1}]},
        headers=auth,
    )

    assert response.status_code == 201
    payload = store.events[0]["payload"]
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 256 * 1024
    assert payload["truncated"] is True


async def test_worker_batch_keeps_each_environment_receipt_after_its_events_in_order(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """The receipt runs after the commit and in the batch's order, as the single append runs it."""
    job_id, auth = await _worker_auth(client)
    receipts: list[tuple[str, int]] = []

    async def record(_job_id, _attempt_id, _generation, receipt):
        receipts.append((receipt["binding_id"], len(store.events)))
        return True

    monkeypatch.setattr(store, "record_worker_environment_preparation", record, raising=False)
    prepared = {"phase": "environment_prepared", "binding_id": "first"}

    response = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events:batch",
        json={
            "events": [
                {"event_type": "lifecycle", "payload": prepared, "event_seq": 1},
                {"event_type": "message", "payload": {"text": "between"}, "event_seq": 2},
                {
                    "event_type": "lifecycle",
                    "payload": {**prepared, "binding_id": "second"},
                    "event_seq": 3,
                },
            ]
        },
        headers=auth,
    )

    assert response.status_code == 201
    assert receipts == [("first", 3), ("second", 3)]
