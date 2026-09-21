"""Owner routes for a conversation's goal, through the real HTTP surface.

A goal belongs to the *thread*, and these cases pin the rules the routes are
responsible for: the identity and the control version every accepted write
leaves behind, what an edit does to the rounds already spent, what pause and
clear cancel, and whose conversation answers at all. The store is the in-memory
fake the other route tests use, so the assertions are about the rows those
routes actually wrote.
"""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from typing import TYPE_CHECKING, Any

import pytest
from tests.servers.test_agent_jobs_api import (  # noqa: F401 - pytest fixtures
    _MCP_ENCRYPTION_KEY,
    _OWNER,
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
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "api-test-secret")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "api-test-broker-secret")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "session-test-secret-32-bytes-minimum!")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name,o/n,private/repo")
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _MCP_ENCRYPTION_KEY)
    yield


GOAL = {"objective": "make `pytest` pass", "max_rounds": 3}


def _path(thread_id: str, suffix: str = "") -> str:
    return f"/v1/agent/threads/{thread_id}/goal{suffix}"


async def _conversation(client: AsyncClient, store: FakeStorage) -> tuple[str, str]:
    """One conversation of the test's owner: its newest job and its thread."""
    job_id = await _create_job(client)
    return job_id, str(store.jobs[job_id]["thread_id"])


async def _set(
    client: AsyncClient,
    thread_id: str,
    *,
    objective: str = GOAL["objective"],
    max_rounds: int = GOAL["max_rounds"],
    revision: int | None = None,
) -> dict[str, Any]:
    """Write the goal and hand back the response body."""
    body: dict[str, Any] = {"goal": {"objective": objective, "max_rounds": max_rounds}}
    if revision is not None:
        body["revision"] = revision
    response = await client.put(_path(thread_id), json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def _goal_round(store: FakeStorage, job_id: str, *, round_number: int = 1) -> dict:
    """One automatic round of the conversation, queued the way the platform does."""
    return await store.conversation_commands.create_follow_up(
        parent_job_id=job_id,
        user_id=_OWNER,
        prompt=f'<goal_round kind="work" round="{round_number}">',
        turn_source="goal",
        goal_round=round_number,
        goal_round_kind="work",
        goal_revision=1,
    )


def _error_type(response) -> str:
    return response.json()["detail"]["error"]["type"]


async def test_a_conversation_with_no_goal_answers_with_its_control_version(client, store):
    """A read that answers ``null`` still has to say what the next write compares against."""
    _, thread_id = await _conversation(client, store)

    response = await client.get(_path(thread_id))

    assert response.status_code == 200, response.text
    assert response.json() == {"thread_id": thread_id, "goal": None, "revision": 0}


async def test_set_creates_the_goal_with_an_identity_and_a_revision(client, store):
    _, thread_id = await _conversation(client, store)

    body = await _set(client, thread_id)

    assert body["revision"] == 1
    goal = body["goal"]
    assert goal["goal_id"]
    assert (goal["state"], goal["objective"], goal["max_rounds"]) == (
        "active",
        GOAL["objective"],
        GOAL["max_rounds"],
    )
    assert goal["rounds_used"] == 0
    assert (goal["pause_reason"], goal["completion_basis"]) == (None, None)


async def test_a_job_created_with_a_goal_carries_it_on_the_conversation(client, store):
    """The composer's path: one submission sets the objective and starts the work."""
    response = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "goal": GOAL,
        },
    )
    assert response.status_code == 201, response.text

    thread_id = response.json()["thread_id"]
    read_back = await client.get(_path(thread_id))
    assert read_back.json()["goal"]["objective"] == GOAL["objective"]
    assert read_back.json()["revision"] == 1
    assert store.thread_owners[thread_id] == _OWNER


async def test_a_stale_revision_is_refused_on_every_write(client, store):
    """Two tabs editing one goal: the second is refused, not silently applied."""
    _, thread_id = await _conversation(client, store)
    await _set(client, thread_id)

    stale_set = await client.put(
        _path(thread_id),
        json={"goal": {"objective": "something else", "max_rounds": 3}, "revision": 0},
    )
    assert stale_set.status_code == 409
    assert _error_type(stale_set) == "goal_revision_conflict"

    stale_pause = await client.post(_path(thread_id, "/pause"), json={"revision": 0})
    assert stale_pause.status_code == 409
    assert _error_type(stale_pause) == "goal_revision_conflict"

    paused = await client.post(_path(thread_id, "/pause"), json={"revision": 1})
    assert paused.status_code == 200, paused.text
    stale_resume = await client.post(_path(thread_id, "/resume"), json={"revision": 1})
    assert stale_resume.status_code == 409
    assert _error_type(stale_resume) == "goal_revision_conflict"

    # None of the refused writes changed anything.
    current = await client.get(_path(thread_id))
    assert current.json()["revision"] == 2
    assert current.json()["goal"]["objective"] == GOAL["objective"]
    assert current.json()["goal"]["state"] == "paused"


async def test_editing_only_the_cap_keeps_the_identity_and_the_rounds_spent(client, store):
    """A cap is not an objective: the same authorization keeps working."""
    _, thread_id = await _conversation(client, store)
    first = await _set(client, thread_id, objective="ship it", max_rounds=2)
    # The rounds a goal has started are derived from them in the real store, so
    # the fake carries the same count the derivation would produce.
    store.thread_goals[thread_id]["rounds_used"] = 1

    edited = await _set(client, thread_id, objective="ship it", max_rounds=4, revision=1)

    assert edited["goal"]["goal_id"] == first["goal"]["goal_id"]
    assert edited["goal"]["max_rounds"] == 4
    assert edited["goal"]["rounds_used"] == 1
    assert edited["revision"] == 2


async def test_replacing_the_objective_mints_an_identity_and_cancels_the_queued_round(
    client, store
):
    """A queued round carries the objective it was written for."""
    job_id, thread_id = await _conversation(client, store)
    first = await _set(client, thread_id, objective="ship it", max_rounds=5)
    queued = await _goal_round(store, job_id)
    store.thread_goals[thread_id]["rounds_used"] = 2

    replaced = await _set(client, thread_id, objective="ship it safely", max_rounds=5, revision=1)

    assert replaced["goal"]["goal_id"] != first["goal"]["goal_id"]
    assert replaced["goal"]["rounds_used"] == 0
    assert replaced["goal"]["state"] == "active"
    assert store.jobs[queued["id"]]["state"] == "cancelled"
    assert store.jobs[queued["id"]]["cancel_requested"] is True


async def test_editing_a_paused_goal_leaves_it_paused(client, store):
    """Changing the words of an objective is not resuming it."""
    _, thread_id = await _conversation(client, store)
    await _set(client, thread_id, objective="ship it")
    paused = await client.post(_path(thread_id, "/pause"), json={"revision": 1})
    assert paused.status_code == 200, paused.text

    edited = await _set(client, thread_id, objective="ship it", max_rounds=7, revision=2)

    assert edited["goal"]["state"] == "paused"
    assert (edited["goal"]["pause_reason"], edited["goal"]["pause_detail"]) == (
        "user",
        "paused by the user",
    )
    assert edited["revision"] == 3


async def test_setting_a_completed_goal_again_mints_a_new_identity(client, store):
    """The last authorization was spent, so identical words are still a new goal."""
    _, thread_id = await _conversation(client, store)
    first = await _set(client, thread_id)
    store.thread_goals[thread_id].update(
        state="complete", completion_basis="model_reported", completed_by_job_id="ajob_0001"
    )
    store.thread_goals[thread_id]["rounds_used"] = 3

    again = await _set(client, thread_id, revision=1)

    assert again["goal"]["goal_id"] != first["goal"]["goal_id"]
    assert again["goal"]["state"] == "active"
    assert again["goal"]["rounds_used"] == 0
    assert again["goal"]["completion_basis"] is None
    assert again["revision"] == 2


async def test_pause_cancels_the_queued_round_and_leaves_the_running_turn_alone(client, store):
    """Stop starting work, never stop work that has started."""
    job_id, thread_id = await _conversation(client, store)
    await _set(client, thread_id)
    queued = await _goal_round(store, job_id, round_number=1)
    running = await _goal_round(store, job_id, round_number=2)
    store.jobs[running["id"]]["state"] = "running"

    paused = await client.post(_path(thread_id, "/pause"), json={"revision": 1})

    assert paused.status_code == 200, paused.text
    assert paused.json()["goal"]["state"] == "paused"
    assert paused.json()["goal"]["pause_reason"] == "user"
    assert store.jobs[queued["id"]]["state"] == "cancelled"
    assert store.jobs[running["id"]]["state"] == "running"


async def test_pause_without_a_goal_is_a_conflict_rather_than_a_creation(client, store):
    """Nothing to stop is not the same request as "make one"."""
    _, thread_id = await _conversation(client, store)

    for suffix in ("/pause", "/resume"):
        response = await client.post(_path(thread_id, suffix))
        assert response.status_code == 409, (suffix, response.text)
        assert _error_type(response) == "goal_missing"
    assert store.thread_goals == {}


async def test_resume_returns_the_goal_to_active_and_records_a_wakeup(client, store):
    """The owner's authorization is the trigger; no model report is needed."""
    _, thread_id = await _conversation(client, store)
    created = await _set(client, thread_id)
    await client.post(_path(thread_id, "/pause"), json={"revision": 1})

    resumed = await client.post(_path(thread_id, "/resume"), json={"revision": 2})

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["goal"]["state"] == "active"
    assert resumed.json()["goal"]["goal_id"] == created["goal"]["goal_id"]
    assert resumed.json()["goal"]["pause_reason"] is None
    assert resumed.json()["revision"] == 3
    pending = [row for row in store.goal_wakeups.rows if row["state"] == "pending"]
    assert [(row["trigger"], row["trigger_ref"]) for row in pending] == [("owner", "resume:3")]
    assert pending[0]["revision"] == 3


async def test_resume_refuses_a_goal_that_has_spent_its_rounds(client, store):
    """Resuming would authorize a round the budget cannot pay for."""
    _, thread_id = await _conversation(client, store)
    await _set(client, thread_id, objective="never finish", max_rounds=1)
    store.thread_goals[thread_id]["rounds_used"] = 1
    await client.post(_path(thread_id, "/pause"), json={"revision": 1})

    resumed = await client.post(_path(thread_id, "/resume"), json={"revision": 2})

    assert resumed.status_code == 422, resumed.text
    assert _error_type(resumed) == "goal_rounds_exhausted"
    # Refused means refused: the goal is still paused, at the version it was.
    assert (await client.get(_path(thread_id))).json()["goal"]["state"] == "paused"
    assert store.thread_goals[thread_id]["revision"] == 2


async def test_resume_leaves_an_active_goal_exactly_as_it_was(client, store):
    """There is nothing to resume, and nothing to refuse about it either."""
    _, thread_id = await _conversation(client, store)
    created = await _set(client, thread_id)

    resumed = await client.post(_path(thread_id, "/resume"), json={"revision": 1})

    assert resumed.status_code == 200, resumed.text
    assert resumed.json() == created


async def test_clear_removes_the_goal_and_cancels_its_queued_round(client, store):
    job_id, thread_id = await _conversation(client, store)
    await _set(client, thread_id)
    queued = await _goal_round(store, job_id)

    cleared = await client.delete(_path(thread_id))

    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"thread_id": thread_id, "cleared": True, "revision": 2}
    assert thread_id not in store.thread_goals
    assert store.jobs[queued["id"]]["state"] == "cancelled"
    # The version survives the goal: a report written about the removed
    # objective can never match whatever the owner sets next.
    read_back = await client.get(_path(thread_id))
    assert read_back.json() == {"thread_id": thread_id, "goal": None, "revision": 2}


async def test_clearing_a_conversation_with_no_goal_changes_nothing(client, store):
    _, thread_id = await _conversation(client, store)

    cleared = await client.delete(_path(thread_id))

    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"thread_id": thread_id, "cleared": False, "revision": 0}


async def test_another_owners_conversation_is_a_404(client, store):
    """Not a 403: the goal routes never confirm that somebody else's thread exists."""
    _, thread_id = await _conversation(client, store)
    await _set(client, thread_id)
    store.thread_owners[thread_id] = "someone-else"

    calls = (
        ("get", _path(thread_id), {}),
        ("put", _path(thread_id), {"json": {"goal": GOAL}}),
        ("post", _path(thread_id, "/pause"), {}),
        ("post", _path(thread_id, "/resume"), {}),
        ("delete", _path(thread_id), {}),
    )
    for method, path, kwargs in calls:
        response = await getattr(client, method)(path, **kwargs)
        assert response.status_code == 404, (method, path, response.text)


async def test_the_job_scoped_goal_routes_are_gone(client, store):
    """One goal, one resource: the job-shaped routes were replaced, not shadowed."""
    job_id = await _create_job(client)
    calls = (
        ("get", f"/v1/agent/jobs/{job_id}/goal", {}),
        ("put", f"/v1/agent/jobs/{job_id}/goal", {"json": {"goal": GOAL}}),
        ("post", f"/v1/agent/jobs/{job_id}/goal/pause", {}),
        ("post", f"/v1/agent/jobs/{job_id}/goal/resume", {}),
        ("delete", f"/v1/agent/jobs/{job_id}/goal", {}),
    )
    for method, path, kwargs in calls:
        response = await getattr(client, method)(path, **kwargs)
        assert response.status_code in (404, 405), (method, path, response.status_code)


async def test_config_advertises_the_goal_bounds(client):
    response = await client.get("/v1/agent/config")
    assert response.status_code == 200, response.text
    limits = response.json()["goal_limits"]
    assert limits["max_objective_chars"] == 4000
    assert limits["max_rounds"] == 50
    assert limits["default_rounds"] == 10


async def test_an_over_long_objective_is_refused_before_anything_is_written(client, store):
    _, thread_id = await _conversation(client, store)

    response = await client.put(_path(thread_id), json={"goal": {"objective": "x" * 4001}})

    assert response.status_code == 422
    assert store.thread_goals == {}
