"""Real HTTP and Postgres acceptance for reusable environment configuration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from tests.integration.servers.test_pending_message_queue import (
    _claim_body,
    api as api,
    pool as pool,
)

from cloud_agent.api import environments

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_environment_create_test_bind_rotate_reuse_and_archive_via_http(api, monkeypatch):
    monkeypatch.setenv("AGENT_REPOSITORY_ENVIRONMENTS_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "http-environment-key")
    api.app.include_router(environments.router)
    monkeypatch.setattr(
        "cloud_agent.environment_source.resolve_test_commit", AsyncMock(return_value="a" * 40)
    )
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job", AsyncMock(return_value=None)
    )
    http = api.client
    response = await http.patch(
        "/v1/agent/environment-globals",
        json={
            "revision": 0,
            "variables": [{"name": "APP_ENV", "value": "global", "secret": False}],
        },
    )
    assert response.status_code == 200, response.text
    body = {
        "name": "Development",
        "repo": "owner/name",
        "revision": 0,
        "setup_script": "npm ci",
        "start_script": "npm run dev &",
        "verify_script": "npm test",
        "actions": [{"id": "test", "name": "Run tests", "command": "npm test", "cwd": "."}],
        "variables": [
            {"name": "TOKEN", "value": "http-secret-one"},
            {"name": "APP_ENV", "value": "local", "secret": False},
        ],
    }
    response = await http.post("/v1/agent/environments", json=body)
    assert response.status_code == 201, response.text
    env = response.json()
    assert "http-secret-one" not in response.text
    assert env["revision"] == 1
    response = await http.post(
        "/v1/agent/environment-runs",
        headers={"Idempotency-Key": "test-key"},
        json={"environment_id": env["id"], "environment_revision": 1, "global_revision": 1},
    )
    assert response.status_code == 201, response.text
    test = response.json()
    assert test["state"] == "queued"
    attestation = _claim_body("test-worker")["attestation"]
    attestation["runner_capabilities"].append("environment-v1")
    response = await http.post(
        "/v1/agent/worker/environment-runs/claim",
        json={"capabilities": ["environment-v1"], "attestation": attestation},
    )
    assert response.status_code == 200, response.text
    leased = response.json()
    assert leased and leased["id"] == test["id"]
    assert not {"sandbox_token", "mcp_token", "github_token"} & leased.keys()
    response = await http.post(
        f"/v1/agent/worker/environment-runs/{test['id']}",
        json={
            "generation": leased["lease_generation"],
            "state": "succeeded",
            "cleanup_complete": True,
            "steps": [{"phase": "setup", "state": "succeeded", "output": "http-secret-one"}],
        },
    )
    assert response.status_code == 200, response.text
    response = await http.get(f"/v1/agent/environment-runs/{test['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "succeeded"
    assert response.json()["cleanup_complete"] is True
    assert "http-secret-one" not in response.text
    assert "[secret:TOKEN]" in response.text
    first = await api.create(
        "First bound task",
        environment_id=env["id"],
        environment_revision=1,
        global_revision=1,
        environment_overrides=[{"name": "TASK_TOKEN", "value": "temporary-http-secret"}],
    )
    path = f"/v1/agent/jobs/{first['id']}/thread/environment"
    response = await http.get(path)
    assert response.status_code == 200, response.text
    original = response.json()
    assert original["revision"] == 1
    assert {v["name"]: v["source"] for v in original["variables"]} == {
        "APP_ENV": "environment",
        "TOKEN": "environment",
        "TASK_TOKEN": "override",
    }
    assert "temporary-http-secret" not in response.text
    update = {
        **body,
        "revision": 1,
        "setup_script": "npm ci --ignore-scripts",
        "variables": [{"name": "TOKEN", "value": "http-secret-two"}],
    }
    response = await http.patch(f"/v1/agent/environments/{env['id']}", json=update)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2
    stale = await http.patch(f"/v1/agent/environments/{env['id']}", json=update)
    assert stale.status_code == 409
    assert stale.json()["current_revision"] == 2
    bound = (await http.get(path)).json()
    assert bound["binding_id"] == original["binding_id"]
    assert bound["revision"] == 1 and bound["setup_script"] == "npm ci"
    assert bound["has_new_version"] is True
    second = await api.create(
        "New version task", environment_id=env["id"], environment_revision=2, global_revision=1
    )
    newer = await http.get(f"/v1/agent/jobs/{second['id']}/thread/environment")
    assert newer.status_code == 200, newer.text
    assert newer.json()["revision"] == 2
    assert newer.json()["setup_script"] == "npm ci --ignore-scripts"
    assert newer.json()["binding_id"] != original["binding_id"]
    deleted = await http.delete(f"/v1/agent/environments/{env['id']}?revision=2")
    assert deleted.status_code == 204
    assert (await http.get("/v1/agent/environments?repo=owner/name")).json()["environments"] == []
    archived = await http.get(path)
    assert archived.status_code == 200, archived.text
    assert archived.json()["revision"] == 1
    assert archived.json()["environment_archived"] is True
    assert archived.json()["has_new_version"] is False
    assert "http-secret-one" not in archived.text
