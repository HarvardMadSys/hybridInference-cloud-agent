"""A native Fork the retaining host fails is answered like any sandbox manager failure.

The Fork flow hands the host's failure back as ``ForkHostFailed`` rather than
as an HTTP answer, and the route maps it: these pin that the owner still sees
the bounded status and the ``workspace_unavailable`` type.
"""

from __future__ import annotations

from typing import Any

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.servers.test_agent_jobs_api import (  # noqa: F401 - shared pytest fixtures
    ASGITransport,
    AsyncClient,
    FakeStorage,
    _api_key_secret,
    _build_app,
    _create_job,
    pytest,
    store,
)

from cloud_agent import sandbox_manager_routing
from cloud_agent.sandbox_manager_client import SandboxManagerError

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("host_status", "message", "answered", "answered_message"),
    [
        (429, "slow down", 429, "slow down"),
        (500, "host fell over", 503, "host fell over"),
        (
            404,
            "shared chat workspace is not materialized",
            409,
            "This chat has no retained workspace.",
        ),
    ],
)
async def test_a_native_fork_the_host_fails_is_answered_like_any_sandbox_manager_failure(
    store: FakeStorage,
    monkeypatch,
    host_status: int,
    message: str,
    answered: int,
    answered_message: str,
):
    """The host's own failure of the fork request keeps its bounded status and type."""

    class SandboxManager:
        async def fork_shared_session(self, source_thread_id: str, **kwargs: Any):
            raise SandboxManagerError(host_status, message)

    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: SandboxManager())
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        source = store.jobs[job_id]
        source["state"] = "succeeded"
        source["thread_sandbox"] = {
            "thread_id": source["thread_id"],
            "sandbox_id": "asbx_shared",
            "generation": 1,
            "activation_generation": 1,
            "container_id": "container-shared",
            "runtime_id": "claude-code",
            "runtime_version": "2.1.220",
            "native_session_id": "native-session-source",
            "shared_live": True,
            "resident_slot_id": 0,
            "resident_slot_capacity": 8,
        }
        response = await client.post(
            f"/v1/agent/jobs/{job_id}/fork", headers={"Idempotency-Key": "host-fails"}
        )

    assert response.status_code == answered, response.text
    assert response.json() == {
        "detail": {"error": {"type": "workspace_unavailable", "message": answered_message}}
    }
