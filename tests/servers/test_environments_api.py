"""Environment validation never reflects credentials or bypasses owner scope."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import environments
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.environments import EnvironmentNotFound

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    repository = SimpleNamespace(
        get_environment=AsyncMock(return_value={"id": "env", "revision": 1, "variables": []}),
        get_global_environment=AsyncMock(return_value={"id": None, "revision": 0, "variables": []}),
    )
    app = FastAPI()
    app.include_router(environments.router)
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(environments=repository)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "alice"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, repository


async def test_import_preview_exposes_only_names_and_no_store(client):
    http, repo = client
    response = await http.post(
        "/v1/agent/environment-import/preview",
        json={
            "environment_id": "env",
            "revision": 1,
            "text": 'TOKEN="demo-private-token"\nREGION=east',
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "demo-private-token" not in response.text
    assert "east" not in response.text
    assert [v["name"] for v in response.json()["variables"]] == ["TOKEN", "REGION"]
    repo.get_environment.assert_awaited_once_with("alice", "env")


@pytest.mark.parametrize(
    "text",
    [
        "TOKEN=demo-private-token\nOPENAI_API_KEY=secret",
        "TOKEN=demo-private-token\nINVALID",
        "TOKEN=demo-private-token\nTOKEN=second",
    ],
)
async def test_invalid_import_is_atomic_and_errors_do_not_echo_values(client, text):
    http, _ = client
    response = await http.post(
        "/v1/agent/environment-import/preview",
        json={"environment_id": "env", "revision": 1, "text": text},
    )
    assert response.status_code == 422
    assert "demo-private-token" not in response.text
    assert "variables" not in response.json()


async def test_cross_owner_environment_is_not_visible(client):
    http, repo = client
    repo.get_environment.side_effect = EnvironmentNotFound("Environment not found")
    response = await http.get("/v1/agent/environments/foreign")
    assert response.status_code == 404
    repo.get_environment.assert_awaited_once_with("alice", "foreign")


async def test_pydantic_extra_value_errors_do_not_echo_submitted_secret(client):
    http, _ = client
    response = await http.post(
        "/v1/agent/environment-import/preview",
        json={
            "environment_id": "env",
            "revision": 1,
            "text": "TOKEN=value",
            "unexpected": "demo-private-token",
        },
    )
    assert response.status_code == 422
    assert "demo-private-token" not in response.text


async def test_withdrawn_test_owner_gets_no_credentials_or_container(monkeypatch):
    from cloud_agent.api.deps import get_agent_app_credentials, get_gateway_client

    run = {
        "id": "test-run",
        "user_id": "alice",
        "repo": "owner/repo",
        "binding_id": "bound",
        "lease_generation": 2,
    }
    repository = SimpleNamespace(
        claim_environment_run=AsyncMock(return_value=run),
        fail_environment_preflight=AsyncMock(),
        get_environment_binding=AsyncMock(),
    )
    gateway = SimpleNamespace(
        agent_access=AsyncMock(
            return_value={"user_id": "alice", "allowed": False, "permissions": []}
        )
    )
    credentials = SimpleNamespace(token_for=AsyncMock())
    app = FastAPI()
    app.include_router(environments.router)
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(environments=repository)
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    app.dependency_overrides[get_agent_app_credentials] = lambda: credentials
    app.dependency_overrides[environments.verify_claiming_host] = lambda: {
        "host": "runner",
        "credential_hash": "hash",
    }
    entitlement = AsyncMock()
    monkeypatch.setattr(environments, "_entitled", entitlement)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(
            "/v1/agent/worker/environment-runs/claim",
            json={"capabilities": ["environment-v1"], "attestation": {}},
        )
    assert response.status_code == 503
    entitlement.assert_not_awaited()
    credentials.token_for.assert_not_awaited()
    repository.get_environment_binding.assert_not_awaited()
    repository.fail_environment_preflight.assert_awaited_once_with("test-run", "runner", 2)


async def test_unrelated_job_validation_keeps_field_detail_and_internal_errors_stay_internal():
    from cloud_agent.api import jobs

    commands = SimpleNamespace(
        set_thread_title=AsyncMock(side_effect=ValueError("private diagnostic"))
    )
    app = FastAPI()
    app.include_router(jobs.router)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "alice"}
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(conversation_commands=commands)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as http:
        response = await http.patch("/v1/agent/jobs/job/title", json={"title": "x" * 161})
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "title"]
        assert "environment" not in response.text.lower()
        response = await http.patch("/v1/agent/jobs/job/title", json={"title": "Normal title"})
        assert response.status_code == 500
        assert "private diagnostic" not in response.text


async def test_only_environment_encryption_errors_use_the_environment_handler():
    from cloud_agent.app import create_app
    from cloud_agent.environments import EnvironmentEncryptionError
    from cloud_agent.source_control import SourceControlError

    app = create_app()

    @app.get("/test/source-failure")
    async def source_failure():
        raise SourceControlError("GitLab token refresh failed")

    @app.get("/test/environment-failure")
    async def environment_failure():
        raise EnvironmentEncryptionError("private encryption diagnostic")

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as http:
        unrelated = await http.get("/test/source-failure")
        assert unrelated.status_code == 500
        assert "Environment encryption" not in unrelated.text
        own = await http.get("/test/environment-failure")
        assert own.status_code == 503
        assert own.json()["detail"] == "Environment encryption is unavailable"
        assert "private" not in own.text


async def test_invalid_environment_fields_are_located_without_echoing_values(client):
    http, _ = client
    response = await http.post(
        "/v1/agent/environments",
        json={
            "name": "Development",
            "repo": "org/repo",
            "revision": 0,
            "variables": [{"name": "DEBUG", "value": "private", "secret": True}],
        },
    )
    assert response.status_code == 422
    assert "private" not in response.text
    error = response.json()["detail"][0]
    assert error["loc"] == ["body", "variables", 0]
    assert "at least 8" in error["msg"]
    assert "input" not in error and "ctx" not in error


async def test_completed_action_retry_never_dispatches_or_changes_the_result(monkeypatch):
    from cloud_agent.api import _owner_auth, workspace

    job = {"id": "job", "thread_id": "thread", "environment_binding_id": "bound"}
    complete = {"id": "run", "state": "succeeded", "exit_code": 0, "output": "deployed"}
    repo = SimpleNamespace(
        create_environment_action_run=AsyncMock(return_value=complete),
        update_environment_action_run=AsyncMock(),
    )
    store = SimpleNamespace(environments=repo, conversation_queries=object())
    monkeypatch.setattr(_owner_auth, "_owned_job", AsyncMock(return_value=job))
    dispatch = AsyncMock()
    monkeypatch.setattr(workspace, "_terminal_owner_workspace", dispatch)
    assert (
        await environments.start_action(
            "job",
            "deploy",
            idempotency_key="same",
            user={"user_id": "alice"},
            store=store,
            credentials=None,
        )
        == complete
    )
    dispatch.assert_not_awaited()
    repo.update_environment_action_run.assert_not_awaited()


@pytest.mark.parametrize("operation", ["action_status", "stop_action"])
async def test_lost_terminal_becomes_an_explicit_unknown_result_not_an_endless_404(
    monkeypatch, operation
):
    from cloud_agent.sandbox_manager_client import SandboxManagerError

    run = {"id": "run", "state": "running", "terminal_id": "term_run"}
    repo = SimpleNamespace(
        get_environment_action_run=AsyncMock(return_value=run),
        update_environment_action_run=AsyncMock(
            side_effect=lambda user, run_id, update: {**run, **update}
        ),
    )
    broker = SimpleNamespace(
        terminal_output=AsyncMock(side_effect=SandboxManagerError(404, "terminal missing")),
        delete_terminal=AsyncMock(side_effect=SandboxManagerError(404, "terminal missing")),
    )
    monkeypatch.setattr(
        environments,
        "_action_context",
        AsyncMock(return_value=({"id": "job", "thread_id": "thread"}, broker, None)),
    )
    result = await getattr(environments, operation)(
        "run", user={"user_id": "alice"}, store=SimpleNamespace(environments=repo), credentials=None
    )
    assert result["state"] == "unknown"
    assert result["exit_code"] is None
    assert "will not be replayed" in result["output"]
    repo.update_environment_action_run.assert_awaited_once()


async def test_action_status_recovers_the_persisted_terminal_exit(monkeypatch):
    run = {"id": "run", "state": "running", "terminal_id": "term_run"}
    repo = SimpleNamespace(
        get_environment_action_run=AsyncMock(return_value=run),
        update_environment_action_run=AsyncMock(side_effect=lambda u, i, update: {**run, **update}),
    )
    broker = SimpleNamespace(
        terminal_output=AsyncMock(
            return_value={"state": "exited", "exit_code": 0, "output": "done"}
        )
    )
    monkeypatch.setattr(
        environments,
        "_action_context",
        AsyncMock(return_value=({"id": "job", "thread_id": "thread"}, broker, None)),
    )
    result = await environments.action_status(
        "run", user={"user_id": "alice"}, store=SimpleNamespace(environments=repo), credentials=None
    )
    assert result["state"] == "succeeded" and result["exit_code"] == 0


async def test_dotenv_preview_requires_review_of_short_values_without_echoing_them(client):
    http, _ = client
    response = await http.post(
        "/v1/agent/environment-import/preview",
        json={
            "environment_id": "env",
            "revision": 1,
            "text": "DEBUG=1\nTOKEN=example-private-token",
        },
    )
    assert response.status_code == 200
    debug, token = response.json()["variables"]
    assert debug["secret"] is True and debug["secret_allowed"] is False
    assert token["secret"] is True and token["secret_allowed"] is True
    assert "example-private-token" not in response.text


async def test_empty_binding_terminal_does_not_prepare_or_interrupt_a_running_turn(monkeypatch):
    from cloud_agent.api import workspace
    from cloud_agent.sandbox_manager_client import SandboxManagerError
    from cloud_agent.schemas import AgentTerminalRequest
    from cloud_agent_shared.environment import BoundEnvironment

    job = {
        "id": "job",
        "thread_id": "thread",
        "state": "running",
        "environment_binding_id": "empty",
    }
    broker = SimpleNamespace(
        prepare_environment=AsyncMock(side_effect=SandboxManagerError(409, "SessionBusy")),
        terminal=AsyncMock(
            return_value={"output": "1", "stderr": "", "cwd": "/workspace", "exit_code": 0}
        ),
    )
    store = SimpleNamespace(
        conversation_queries=object(),
        sandbox=object(),
        environments=SimpleNamespace(
            get_environment_binding=AsyncMock(return_value=BoundEnvironment(binding_id="empty"))
        ),
    )
    monkeypatch.setattr(workspace, "_owned_job", AsyncMock(return_value=job))
    monkeypatch.setattr(workspace, "_require_workspace_entitlement", AsyncMock())
    monkeypatch.setattr(workspace, "_require_mutable_workspace_ready", AsyncMock())
    monkeypatch.setattr(workspace, "_sandbox_manager_for_job", AsyncMock(return_value=broker))
    result = await workspace.run_agent_job_terminal_command(
        "job",
        AgentTerminalRequest(command="echo 1"),
        user={"user_id": "alice"},
        store=store,
        app_credentials=None,
    )
    assert result.exit_code == 0
    broker.prepare_environment.assert_not_awaited()
    assert "environment" not in broker.terminal.call_args.kwargs


async def test_test_retry_returns_original_before_github_or_latest_config(monkeypatch):
    from cloud_agent.api.deps import get_agent_app_credentials

    monkeypatch.setenv("AGENT_REPOSITORY_ENVIRONMENTS_ENABLED", "false")
    original = {"id": "run", "base_sha": "a" * 40, "state": "queued"}
    repository = SimpleNamespace(
        find_environment_run=AsyncMock(return_value=original), get_environment=AsyncMock()
    )
    app = FastAPI()
    app.include_router(environments.router)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "alice"}
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(environments=repository)
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    entitlement = AsyncMock()
    monkeypatch.setattr(environments, "_entitled", entitlement)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/agent/environment-runs",
            headers={"Idempotency-Key": "same"},
            json={
                "environment_id": "env",
                "environment_revision": 1,
                "global_revision": 0,
                "ref": "dev",
            },
        )
    assert response.status_code == 201 and response.json() == original
    repository.get_environment.assert_not_awaited()
    entitlement.assert_not_awaited()


async def test_workspace_get_only_reads_and_redacts_without_persistence(monkeypatch):
    repository = SimpleNamespace(
        workspace_environment_view=AsyncMock(
            return_value={"binding_id": "bound", "required": True}
        ),
        redact_environment_steps=AsyncMock(return_value=[{"output": "masked"}]),
        record_environment_preparation=AsyncMock(),
        get_environment_binding=AsyncMock(),
    )
    store = SimpleNamespace(environments=repository, conversation_queries=None, host_pool=None)
    monkeypatch.setattr(
        "cloud_agent.api._owner_auth._owned_job", AsyncMock(return_value={"thread_id": "thread"})
    )
    broker = SimpleNamespace(
        environment_status=AsyncMock(
            return_value={"binding_id": "bound", "steps": [{"output": "private"}]}
        )
    )
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job",
        AsyncMock(return_value=broker),
    )
    result = await environments.workspace_environment("job", user={"user_id": "alice"}, store=store)
    assert result["steps"] == [{"output": "masked"}]
    repository.record_environment_preparation.assert_not_awaited()
    repository.get_environment_binding.assert_not_awaited()
