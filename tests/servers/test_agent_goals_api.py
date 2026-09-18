"""Owner routes and the continuation loop, through the real HTTP surface.

The store is the in-memory fake the other route tests use, which is what makes
these tests about the *decisions* rather than about Postgres: a goal conversation
is created, a turn reports, and what the platform does next is asserted on the
rows the routes actually wrote.
"""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from typing import TYPE_CHECKING, Any

import pytest

from tests.servers.test_agent_jobs_api import (  # noqa: F401 - pytest fixtures
    FakeStorage,
    _build_app,
    _create_job,
    client,
    store,
)

if TYPE_CHECKING:
    from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _deployment_environment(monkeypatch):
    """The environment every one of these cases needs.

    Repeated rather than imported: the route tests' copy is an autouse fixture
    in another module, and a fixture defined there does not travel with an
    import — the failure mode is a 403 from the repository allowlist, which
    would look like a goal bug and is not one.
    """
    from tests.servers.test_agent_jobs_api import _MCP_ENCRYPTION_KEY

    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "api-test-secret")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "api-test-broker-secret")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "session-test-secret-32-bytes-minimum!")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name,o/n,private/repo")
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _MCP_ENCRYPTION_KEY)
    yield


GOAL = {"objective": "make `pytest` pass", "max_rounds": 3}


async def _create_goal_job(client: AsyncClient, *, goal: dict[str, Any] | None = None) -> dict:
    response = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "goal": goal or GOAL,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _claim(client: AsyncClient) -> dict:
    response = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body is not None, "nothing was claimable"
    return body


async def _finish(client: AsyncClient, claim: dict, *, state: str = "succeeded") -> dict:
    response = await client.post(
        f"/v1/agent/worker/jobs/{claim['job_id']}/finish",
        json={"state": state},
        headers={"Authorization": f"Bearer {claim['worker_token']}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _say(client: AsyncClient, claim: dict, text: str) -> None:
    """Record one assistant message as the turn's own runtime would."""
    response = await client.post(
        f"/v1/agent/worker/jobs/{claim['job_id']}/events",
        json={"event_type": "message", "payload": {"text": text}},
        headers={"Authorization": f"Bearer {claim['worker_token']}"},
    )
    assert response.status_code == 201, response.text


async def _run_turn(client: AsyncClient, store: FakeStorage, report: str | None) -> dict:
    """Claim the next queued turn, say one thing in it, and finish it."""
    claim = await _claim(client)
    if report is not None:
        await _say(client, claim, report)
    await _finish(client, claim)
    return claim


def _goal_rounds(store: FakeStorage) -> list[dict]:
    return [job for job in store.jobs.values() if job.get("turn_source") == "goal"]


async def test_creating_a_job_with_a_goal_puts_it_on_the_conversation(client, store):
    body = await _create_goal_job(client)
    assert body["goal"]["objective"] == GOAL["objective"]
    assert body["goal"]["state"] == "active"
    assert body["goal"]["rounds_used"] == 0
    assert body["goal"]["revision"] == 1
    assert body["turn_source"] == "human"
    assert body["goal_round"] is None


async def test_the_first_claim_already_carries_the_goal(client, store):
    await _create_goal_job(client)
    claim = await _claim(client)
    prompt = claim["task_prompt"]
    assert prompt.startswith("<conversation_goal>")
    assert '"make `pytest` pass"' in prompt
    assert "Round: 0/3 used" in prompt
    # Every runtime this deployment registers can be given the relay, so the
    # preamble names the tool rather than the text convention — a call is a
    # structured act that lands when it happens, while the two lines are
    # reconstructed from prose at the end of the turn.
    assert "`report_goal`" in prompt
    assert "GOAL_STATUS" not in prompt
    # The owner's own words survive at the end, unaltered.
    assert prompt.rstrip().endswith("fix it")


async def test_continue_queues_the_next_round_and_names_its_parent(client, store):
    body = await _create_goal_job(client)
    await _run_turn(client, store, "GOAL_STATUS: continue\nGOAL_REASON: two tests fail")

    rounds = _goal_rounds(store)
    assert len(rounds) == 1
    queued = rounds[0]
    assert queued["state"] == "queued"
    assert queued["goal_round_kind"] == "work"
    assert queued["goal_round"] == 1
    assert queued["round_goal_revision"] == 1
    assert '<goal_round kind="work" round="1" of="3">' in queued["task_prompt"]
    assert "two tests fail" in queued["task_prompt"]
    assert queued["client_request_id"] == f"goal:{body['thread_id']}:1:work:{body['id']}"


async def test_a_completion_claim_buys_exactly_one_verification_round(client, store):
    await _create_goal_job(client)
    await _run_turn(client, store, "GOAL_STATUS: complete\nGOAL_REASON: pytest: 42 passed")

    rounds = _goal_rounds(store)
    assert len(rounds) == 1
    assert rounds[0]["goal_round_kind"] == "verify"
    assert '<goal_round kind="verify">' in rounds[0]["task_prompt"]
    # The claim did not complete anything: only the verification can.
    goal = await store.goals.get_goal_for_job(job_id=next(iter(store.jobs)))
    assert goal["state"] == "active"


async def test_a_verification_round_completes_the_goal_and_stops(client, store):
    await _create_goal_job(client)
    await _run_turn(client, store, "GOAL_STATUS: complete\nGOAL_REASON: pytest: 42 passed")
    verify = await _run_turn(client, store, "GOAL_STATUS: complete\nGOAL_REASON: pytest: 42 passed")

    goal = await store.goals.get_goal_for_job(job_id=verify["job_id"])
    assert goal["state"] == "complete"
    assert [job.get("goal_round_kind") for job in _goal_rounds(store)] == ["verify"]
    # Nothing further is queued after a completed goal.
    assert not [
        job
        for job in store.jobs.values()
        if job["state"] == "queued" and job.get("turn_source") == "goal"
    ]


async def test_a_failed_verification_round_continues_instead(client, store):
    await _create_goal_job(client)
    await _run_turn(client, store, "GOAL_STATUS: complete\nGOAL_REASON: pytest: 42 passed")
    await _run_turn(client, store, "GOAL_STATUS: continue\nGOAL_REASON: the check never ran")

    goal = await store.goals.get_goal_for_job(job_id=next(iter(store.jobs)))
    assert goal["state"] == "active"
    kinds = [job["goal_round_kind"] for job in _goal_rounds(store)]
    assert kinds == ["verify", "work"]


async def test_the_round_limit_stops_the_goal_without_queuing_another(client, store):
    await _create_goal_job(client, goal={"objective": "never finish", "max_rounds": 1})
    await _run_turn(client, store, "GOAL_STATUS: continue\nGOAL_REASON: still going")
    # The queued work round is claimed, which is what charges it.
    await _run_turn(client, store, "GOAL_STATUS: continue\nGOAL_REASON: still going")

    goal = await store.goals.get_goal_for_job(job_id=next(iter(store.jobs)))
    assert goal["state"] == "blocked"
    assert goal["blocked_code"] == "round-limit"
    assert goal["rounds_used"] == 1
    assert not [
        job
        for job in store.jobs.values()
        if job["state"] == "queued" and job.get("turn_source") == "goal"
    ]


async def test_blocked_is_accepted_only_after_three_consecutive_turns(client, store):
    await _create_goal_job(client, goal={"objective": "never finish", "max_rounds": 9})
    for _ in range(2):
        await _run_turn(client, store, "GOAL_STATUS: blocked\nGOAL_REASON: no network")
        goal = await store.goals.get_goal_for_job(job_id=next(iter(store.jobs)))
        # Not accepted yet, and the next round says so.
        assert goal["state"] == "active"
    pending = [job for job in store.jobs.values() if job["state"] == "queued"]
    assert "has not accepted it yet" in pending[0]["task_prompt"]

    await _run_turn(client, store, "GOAL_STATUS: blocked\nGOAL_REASON: no network")
    goal = await store.goals.get_goal_for_job(job_id=next(iter(store.jobs)))
    assert goal["state"] == "blocked"
    assert goal["blocked_code"] == "model-reported"
    assert goal["blocked_message"] == "no network"


async def test_a_failed_turn_pauses_the_goal_instead_of_continuing(client, store):
    await _create_goal_job(client)
    claim = await _claim(client)
    await _finish(client, claim, state="failed")

    goal = await store.goals.get_goal_for_job(job_id=claim["job_id"])
    assert (goal["state"], goal["pause_reason"]) == ("paused", "turn-failed")
    assert _goal_rounds(store) == []


async def test_a_queued_turn_from_the_owner_makes_the_goal_yield(client, store):
    body = await _create_goal_job(client)
    claim = await _claim(client)
    follow = await client.post(
        f"/v1/agent/jobs/{body['id']}/follow-ups",
        json={"prompt": "actually, also do this"},
    )
    assert follow.status_code == 201, follow.text
    await _say(client, claim, "GOAL_STATUS: continue")
    await _finish(client, claim)

    # The person's turn is next; the goal queued nothing behind it.
    assert [job["id"] for job in store.jobs.values() if job["state"] == "queued"] == [
        follow.json()["id"]
    ]


async def test_a_replayed_finish_does_not_queue_a_second_round(client, store):
    await _create_goal_job(client)
    claim = await _claim(client)
    await _say(client, claim, "GOAL_STATUS: continue")
    await _finish(client, claim)
    assert len(_goal_rounds(store)) == 1

    # The runner resends the same finish — it never saw the ack.
    again = await client.post(
        f"/v1/agent/worker/jobs/{claim['job_id']}/finish",
        json={"state": "succeeded"},
        headers={"Authorization": f"Bearer {claim['worker_token']}"},
    )
    assert again.status_code == 409
    assert len(_goal_rounds(store)) == 1
    assert await store.goals.has_pending_turn(
        thread_id=str(store.jobs[claim["job_id"]]["thread_id"])
    )


async def test_an_idempotent_resend_of_the_goal_creation_runs_nothing_extra(client, store):
    first = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1", "goal": GOAL},
        headers={"Idempotency-Key": "one-submission"},
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1", "goal": GOAL},
        headers={"Idempotency-Key": "one-submission"},
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert len(_goal_rounds(store)) == 0


async def test_pause_cancels_the_queued_round_and_resume_starts_it_again(client, store):
    body = await _create_goal_job(client)
    await _run_turn(client, store, "GOAL_STATUS: continue")
    queued = _goal_rounds(store)[0]

    paused = await client.post(f"/v1/agent/jobs/{body['id']}/goal/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["goal"]["state"] == "paused"
    assert paused.json()["goal"]["pause_reason"] == "user"
    assert store.jobs[queued["id"]]["state"] == "cancelled"

    resumed = await client.post(f"/v1/agent/jobs/{body['id']}/goal/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["goal"]["state"] == "active"
    fresh = [job for job in store.jobs.values() if job["state"] == "queued"]
    assert len(fresh) == 1
    assert fresh[0]["turn_source"] == "goal"
    # The cancelled round's key resolves to the cancelled row, so the new round
    # must be keyed by its parent instead — and it is a different parent now.
    assert fresh[0]["client_request_id"] != queued["client_request_id"]


async def test_resume_refuses_a_goal_that_has_spent_its_rounds(client, store):
    body = await _create_goal_job(client, goal={"objective": "never finish", "max_rounds": 1})
    await _run_turn(client, store, "GOAL_STATUS: continue")
    await _run_turn(client, store, "GOAL_STATUS: continue")
    goal = await store.goals.get_goal_for_job(job_id=body["id"])
    assert goal["state"] == "blocked"

    # A blocked goal is not resumable through the pause/resume path, and one
    # with no budget left is refused with the reason rather than silently
    # resumed into a goal that cannot move.
    resumed = await client.post(f"/v1/agent/jobs/{body['id']}/goal/resume")
    assert resumed.status_code == 409
    assert resumed.json()["detail"]["error"]["type"] == "goal_rounds_exhausted"


async def test_clearing_the_goal_removes_it_and_its_queued_round(client, store):
    body = await _create_goal_job(client)
    await _run_turn(client, store, "GOAL_STATUS: continue")
    queued = _goal_rounds(store)[0]

    cleared = await client.delete(f"/v1/agent/jobs/{body['id']}/goal")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["cleared"] is True
    assert cleared.json()["job"]["goal"] is None
    assert store.jobs[queued["id"]]["state"] == "cancelled"
    assert await store.goals.get_goal_for_job(job_id=body["id"]) is None


async def test_a_goal_write_can_be_fenced_by_the_revision_it_read(client, store):
    body = await _create_goal_job(client)
    stale = await client.put(
        f"/v1/agent/jobs/{body['id']}/goal",
        json={"goal": {"objective": "something else", "max_rounds": 3}, "revision": 7},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"]["type"] == "goal_revision_conflict"

    fresh = await client.put(
        f"/v1/agent/jobs/{body['id']}/goal",
        json={"goal": {"objective": "something else", "max_rounds": 5}, "revision": 1},
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["goal"]["objective"] == "something else"
    assert fresh.json()["goal"]["max_rounds"] == 5
    assert fresh.json()["goal"]["revision"] == 2


async def test_a_follow_up_can_set_the_goal_for_the_conversation(client, store):
    job_id = await _create_job(client)
    response = await client.post(
        f"/v1/agent/jobs/{job_id}/follow-ups",
        json={"prompt": "keep going until it is green", "goal": GOAL},
    )
    assert response.status_code == 201, response.text
    assert response.json()["goal"]["objective"] == GOAL["objective"]
    assert response.json()["goal"]["state"] == "active"
    # The turn that carried it is still the person's own turn.
    assert response.json()["turn_source"] == "human"


async def test_goal_routes_hide_another_owners_conversation(client, store):
    body = await _create_goal_job(client)
    store.jobs[body["id"]]["user_id"] = "someone-else"
    for method, path in (
        ("put", f"/v1/agent/jobs/{body['id']}/goal"),
        ("post", f"/v1/agent/jobs/{body['id']}/goal/pause"),
        ("post", f"/v1/agent/jobs/{body['id']}/goal/resume"),
        ("delete", f"/v1/agent/jobs/{body['id']}/goal"),
    ):
        call = getattr(client, method)
        kwargs = {"json": {"goal": GOAL}} if method == "put" else {}
        response = await call(path, **kwargs)
        assert response.status_code == 404, (method, response.text)


async def test_config_advertises_the_goal_bounds(client):
    response = await client.get("/v1/agent/config")
    assert response.status_code == 200, response.text
    limits = response.json()["goal_limits"]
    assert limits["max_objective_chars"] == 4000
    assert limits["max_rounds"] == 50
    assert limits["default_rounds"] == 10


async def test_an_over_long_objective_is_refused_before_anything_is_created(client, store):
    response = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "goal": {"objective": "x" * 4001},
        },
    )
    assert response.status_code == 422
    assert store.jobs == {}
