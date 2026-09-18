"""The platform's own tools on the merged surface.

They are not connections: no upstream, no credential, no policy and no
approval. What has to hold is that they are always advertised, that they write
the same two columns the text convention writes, and that a superseded turn
cannot report through them.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import mcp_relay as agent_mcp_router
from cloud_agent.api.deps import get_storage
from tests.servers.test_mcp_relay_host import (
    _ATTEMPT,
    _JOB,
    _LEASE,
    FakeStore,
    _auth,
    _result,
    _rpc,
    _tool_names,
)
from tests.support.storage import OneFake

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """The signing secret and credential key the relay module reads.

    Repeated from the relay host tests' own autouse fixture, which is defined in
    that module and therefore does not travel with an import.
    """
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "mcp-host-secret-value-32-bytes-xx")
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "mcp-host-test-encryption-key")
    monkeypatch.setenv("AGENT_MCP_ALLOW_PRIVATE_TARGETS", "1")
    monkeypatch.setenv("AGENT_MCP_ALLOW_PLAIN_HTTP", "1")
    yield


def _app(store: Any) -> FastAPI:
    """The relay router against one fake store."""
    app = FastAPI()
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_storage] = lambda: OneFake(store)
    return app


def _client(store: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://test")


_HOST_URL = "/v1/agent/mcp"

GOAL = {
    "objective": "make `pytest` pass",
    "state": "active",
    "pause_reason": None,
    "blocked_code": None,
    "blocked_message": None,
    "revision": 3,
    "max_rounds": 10,
    "rounds_used": 2,
    "blocked_streak": 0,
}


class GoalStore(FakeStore):
    """The merged endpoint's fake, plus the two things a goal tool needs."""

    def __init__(self, *, goal: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.goal = goal
        self.reports: list[dict[str, Any]] = []

    async def get_goal_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        del job_id
        return None if self.goal is None else dict(self.goal)

    async def record_goal_report(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        status: str,
        reason: str | None,
    ) -> bool:
        if (job_id, attempt_id, lease_generation) != (_JOB, _ATTEMPT, _LEASE):
            return False
        self.reports.append({"status": status, "reason": reason})
        return True


async def _call(api: AsyncClient, name: str, **arguments: Any) -> Any:
    response = await api.post(
        _HOST_URL,
        json=_rpc("tools/call", name=name, arguments=arguments),
        headers=_auth(),
    )
    assert response.status_code == 200, response.text
    return _result(response)


def _structured(reply: dict[str, Any]) -> dict[str, Any]:
    assert reply["isError"] is False, reply
    return reply["structuredContent"]


async def test_the_goal_tools_are_advertised_alongside_the_connections():
    async with _client(GoalStore(goal=GOAL)) as api:
        response = await api.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
    names = _tool_names(response)
    assert "get_goal" in names and "report_goal" in names
    # The owner's own connections are still there, under their namespaces.
    assert "github__get_issue" in names


async def test_a_session_with_no_connections_still_has_the_goal_tools():
    """The platform's tools are not a connection, so an empty catalogue still has them."""
    async with _client(GoalStore(bindings=[], connections={})) as api:
        response = await api.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
    assert _tool_names(response) == ["get_goal", "report_goal"]


async def test_report_goal_is_not_a_namespaced_upstream_tool():
    """An allowlist naming ``report_goal`` on a connection cannot shadow ours."""
    async with _client(GoalStore(goal=GOAL)) as api:
        listing = await api.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
    ours = [tool for tool in _result(listing)["tools"] if tool["name"] == "report_goal"]
    assert len(ours) == 1
    assert set(ours[0]["inputSchema"]["properties"]) == {"status", "reason"}


async def test_get_goal_reports_the_objective_and_its_progress():
    async with _client(GoalStore(goal=GOAL)) as api:
        reply = await _call(api, "get_goal")
    goal = _structured(reply)["goal"]
    assert goal["objective"] == GOAL["objective"]
    assert goal["rounds_used"] == 2 and goal["max_rounds"] == 10
    assert goal["continues_on_its_own"] is True


async def test_get_goal_says_so_when_there_is_no_goal():
    async with _client(GoalStore(goal=None)) as api:
        reply = await _call(api, "get_goal")
    payload = _structured(reply)
    assert payload["goal"] is None
    assert "no completion goal" in payload["note"]


async def test_report_goal_records_the_status_and_reason():
    goals = GoalStore(goal=GOAL)
    async with _client(goals) as api:
        reply = await _call(api, "report_goal", status="complete", reason="pytest: 42 passed")
    assert goals.reports == [{"status": "complete", "reason": "pytest: 42 passed"}]
    assert _structured(reply)["recorded"]["status"] == "complete"


async def test_report_goal_refuses_a_status_the_schema_would_reject():
    """Refused before it is stored, and refused as a tool error, not a crash."""
    goals = GoalStore(goal=GOAL)
    async with _client(goals) as api:
        reply = await _call(api, "report_goal", status="finished")
    assert reply["isError"] is True
    assert "continue, complete, blocked" in reply["structuredContent"]["error"]
    assert goals.reports == []


async def test_report_goal_refuses_a_non_string_reason():
    goals = GoalStore(goal=GOAL)
    async with _client(goals) as api:
        reply = await _call(api, "report_goal", status="continue", reason={"nope": True})
    assert reply["isError"] is True
    assert goals.reports == []


async def test_a_superseded_turn_cannot_report():
    """The fence is the attempt's, and a stale attempt is told nothing was stored."""

    class Stale(GoalStore):
        async def record_goal_report(self, **kwargs: Any) -> bool:
            return False

    async with _client(Stale(goal=GOAL)) as api:
        reply = await _call(api, "report_goal", status="complete")
    assert reply["isError"] is True
    assert "no longer running" in reply["structuredContent"]["error"]


async def test_a_deployment_with_no_goal_storage_refuses_rather_than_crashing():
    """A store assembled without goal storage answers with an error, not a 500."""

    class NoGoals(GoalStore):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            object.__setattr__(self, "goals", None)

    async with _client(NoGoals(goal=GOAL)) as api:
        reply = await _call(api, "get_goal")
    assert reply["isError"] is True
    assert "no goal storage" in reply["structuredContent"]["error"]
