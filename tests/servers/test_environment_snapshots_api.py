"""The owner and worker surfaces of installation snapshots."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import environments
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_agent_app_credentials, get_gateway_client, get_storage
from cloud_agent_shared.environment import BoundEnvironment

pytestmark = pytest.mark.asyncio


def _app(store) -> FastAPI:
    app = FastAPI()
    app.include_router(environments.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "alice"}
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    app.dependency_overrides[get_gateway_client] = lambda: SimpleNamespace(
        agent_access=AsyncMock(return_value={"user_id": "alice", "allowed": True})
    )
    app.dependency_overrides[environments.verify_claiming_host] = lambda: {
        "host": "runner",
        "credential_hash": "hash",
    }
    return app


def _snapshots(**methods):
    defaults = {
        "enabled": AsyncMock(return_value=True),
        "summaries": AsyncMock(return_value={"env": {"id": "5" * 32, "state": "ready"}}),
        "discard": AsyncMock(return_value=1),
        "queue_prebuild": AsyncMock(return_value="5" * 32),
        "capture_target": AsyncMock(return_value={"snapshot_id": "5" * 32, "owner": "o" * 64}),
        "record_capture": AsyncMock(return_value=True),
        "fail_capture": AsyncMock(),
        "queue_after_test": AsyncMock(return_value="6" * 32),
        "retired_images": AsyncMock(return_value=[]),
    }
    return SimpleNamespace(**{**defaults, **methods})


async def test_the_list_says_which_environments_skip_installation():
    environments_repo = SimpleNamespace(
        list_environments=AsyncMock(return_value=[{"id": "env", "repo": "org/repo"}]),
        get_global_environment=AsyncMock(return_value={"id": None, "revision": 0}),
        current_setup_fingerprints=AsyncMock(return_value={"env": "f" * 64}),
    )
    snapshots = _snapshots()
    store = SimpleNamespace(environments=environments_repo, environment_snapshots=snapshots)
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://t") as http:
        body = (await http.get("/v1/agent/environments")).json()
    assert body["snapshots_enabled"] is True
    assert body["environments"][0]["snapshot"]["state"] == "ready"
    snapshots.summaries.assert_awaited_once_with("alice", {"env": "f" * 64})


async def test_a_rebuild_freezes_the_current_configuration_and_replaces_old_snapshots(
    monkeypatch,
):
    environments_repo = SimpleNamespace(
        get_environment=AsyncMock(return_value={"id": "env", "team": None, "can_edit": True}),
        bind_current_environment=AsyncMock(return_value=("binding", "org/repo")),
    )
    snapshots = _snapshots()
    store = SimpleNamespace(environments=environments_repo, environment_snapshots=snapshots)
    monkeypatch.setattr(environments, "_entitled", AsyncMock())
    monkeypatch.setattr(
        "cloud_agent.environment_source.resolve_test_commit", AsyncMock(return_value="c" * 40)
    )
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://t") as http:
        response = await http.post("/v1/agent/environments/env/snapshot", json={})
        snapshots.enabled.return_value = False
        refused = await http.post("/v1/agent/environments/env/snapshot", json={})
    assert response.status_code == 202, response.text
    snapshots.discard.assert_awaited_once_with("alice", "env", team=False)
    snapshots.queue_prebuild.assert_awaited_once_with(
        user_id="alice", binding_id="binding", repo="org/repo", base_sha="c" * 40, requested=True
    )
    assert refused.status_code == 409


async def test_an_owner_deletes_only_their_own_environments_snapshots():
    from cloud_agent.environments import EnvironmentNotFound

    environments_repo = SimpleNamespace(
        get_environment=AsyncMock(
            side_effect=[{"id": "env", "team": None}, EnvironmentNotFound("missing")]
        )
    )
    snapshots = _snapshots()
    store = SimpleNamespace(environments=environments_repo, environment_snapshots=snapshots)
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://t") as http:
        deleted = await http.delete("/v1/agent/environments/env/snapshot")
        missing = await http.delete("/v1/agent/environments/other/snapshot")
    assert deleted.status_code == 204
    assert missing.status_code == 404
    snapshots.discard.assert_awaited_once_with("alice", "env", team=False)


@pytest.mark.parametrize("can_edit", [True, False])
async def test_a_team_environments_snapshots_are_its_editors_to_throw_away(monkeypatch, can_edit):
    """Every member's snapshot of a team environment goes, so only an editor may decide."""
    team = {"id": "env", "team": {"id": "ateam_1", "name": "Platform"}, "can_edit": can_edit}
    environments_repo = SimpleNamespace(
        get_environment=AsyncMock(return_value=team),
        bind_current_environment=AsyncMock(return_value=("binding", "org/repo")),
    )
    snapshots = _snapshots()
    store = SimpleNamespace(environments=environments_repo, environment_snapshots=snapshots)
    monkeypatch.setattr(environments, "_entitled", AsyncMock())
    monkeypatch.setattr(
        "cloud_agent.environment_source.resolve_test_commit", AsyncMock(return_value="c" * 40)
    )
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://t") as http:
        deleted = await http.delete("/v1/agent/environments/env/snapshot")
        rebuilt = await http.post("/v1/agent/environments/env/snapshot", json={})

    if can_edit:
        assert (deleted.status_code, rebuilt.status_code) == (204, 202)
        assert snapshots.discard.await_args_list[0].kwargs == {"team": True}
    else:
        assert (deleted.status_code, rebuilt.status_code) == (403, 403)
        assert "owner and admins" in deleted.json()["detail"]
        snapshots.discard.assert_not_awaited()
        environments_repo.bind_current_environment.assert_not_awaited()


def _claimed(monkeypatch, run, snapshots):
    environments_repo = SimpleNamespace(
        claim_environment_run=AsyncMock(return_value=run),
        get_environment_binding=AsyncMock(
            return_value=BoundEnvironment(
                binding_id="bound", setup_script="npm ci", setup_fingerprint="f" * 64
            )
        ),
        fail_environment_preflight=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(environments, "_entitled", AsyncMock())
    monkeypatch.setattr(
        "cloud_agent.environment_source.read_repository_token", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(environments, "require_agent_access", AsyncMock())
    return SimpleNamespace(environments=environments_repo, environment_snapshots=snapshots)


_RUN = {
    "id": "run",
    "user_id": "alice",
    "repo": "org/repo",
    "binding_id": "bound",
    "lease_generation": 2,
    "purpose": "test",
    "snapshot_id": None,
}


@pytest.mark.parametrize("capable", [False, True])
async def test_a_claim_names_a_prebuilds_snapshot_only_to_a_capable_host(monkeypatch, capable):
    # The claim query hands prebuilds only to capable hosts; others get tests.
    run = {**_RUN, "purpose": "prebuild", "snapshot_id": "5" * 32} if capable else _RUN
    snapshots = _snapshots(capture_target=AsyncMock(return_value=None))
    if capable:
        snapshots.capture_target.return_value = {"snapshot_id": "5" * 32, "owner": "o" * 64}
    store = _claimed(monkeypatch, run, snapshots)
    capabilities = ["environment-v1"] + (["environment-snapshot-v1"] if capable else [])
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://t") as http:
        body = (
            await http.post(
                "/v1/agent/worker/environment-runs/claim",
                json={"capabilities": capabilities, "attestation": {}},
            )
        ).json()
    assert store.environments.claim_environment_run.await_args.kwargs == {
        "snapshot_capable": capable
    }
    assert ("setup_fingerprint" in body["environment"]) is capable
    assert (body["snapshot"] is not None) is capable


async def test_a_prebuild_whose_snapshot_was_deleted_is_ended_at_its_claim(monkeypatch):
    snapshots = _snapshots(capture_target=AsyncMock(return_value=None))
    store = _claimed(
        monkeypatch, {**_RUN, "purpose": "prebuild", "snapshot_id": "5" * 32}, snapshots
    )
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://t") as http:
        response = await http.post(
            "/v1/agent/worker/environment-runs/claim",
            json={"capabilities": ["environment-v1", "environment-snapshot-v1"], "attestation": {}},
        )
    assert response.status_code == 200 and response.json() is None
    store.environments.fail_environment_preflight.assert_awaited_once_with(
        "run", "runner", 2, detail="The installation snapshot was deleted"
    )


async def test_a_receipt_that_does_not_match_its_run_is_refused():
    environments_repo = SimpleNamespace(update_environment_run=AsyncMock(return_value=True))
    snapshots = _snapshots(record_capture=AsyncMock(return_value=False))
    store = SimpleNamespace(environments=environments_repo, environment_snapshots=snapshots)
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://t") as http:
        refused = await http.post(
            "/v1/agent/worker/environment-runs/run",
            json={"generation": 2, "snapshot": {"snapshot_id": "5" * 32}},
        )
        snapshots.record_capture.return_value = True
        accepted = await http.post(
            "/v1/agent/worker/environment-runs/run",
            json={"generation": 2, "snapshot": {"snapshot_id": "5" * 32}},
        )
        failed = await http.post(
            "/v1/agent/worker/environment-runs/run",
            json={"generation": 2, "state": "failed", "detail": "npm ci failed"},
        )
    assert refused.status_code == 409
    assert accepted.status_code == 200
    assert failed.status_code == 200
    # The receipt is never stored as run output.
    update = environments_repo.update_environment_run.await_args_list[0].args[3]
    assert "snapshot" not in update
    snapshots.fail_capture.assert_awaited_once_with(run_id="run", detail="npm ci failed")


async def test_only_a_finished_successful_run_asks_for_a_prebuild():
    environments_repo = SimpleNamespace(update_environment_run=AsyncMock(return_value=True))
    snapshots = _snapshots()
    store = SimpleNamespace(environments=environments_repo, environment_snapshots=snapshots)
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://t") as http:
        await http.post("/v1/agent/worker/environment-runs/run", json={"generation": 2})
        snapshots.queue_after_test.assert_not_awaited()
        await http.post(
            "/v1/agent/worker/environment-runs/run",
            json={"generation": 2, "state": "succeeded", "cleanup_complete": True},
        )
        environments_repo.update_environment_run.return_value = False
        refused = await http.post(
            "/v1/agent/worker/environment-runs/run",
            json={"generation": 2, "state": "succeeded", "cleanup_complete": True},
        )
    snapshots.queue_after_test.assert_awaited_once_with("run")
    assert refused.status_code == 409


async def test_a_host_asks_which_of_its_images_are_retired():
    snapshots = _snapshots(
        retired_images=AsyncMock(return_value=[{"snapshot_id": "5" * 32, "owner": "o" * 64}])
    )
    store = SimpleNamespace(environments=SimpleNamespace(), environment_snapshots=snapshots)
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://t") as http:
        answer = await http.post(
            "/v1/agent/worker/environment-snapshots/retired",
            json={"snapshot_ids": ["5" * 32, "6" * 32]},
        )
        malformed = await http.post(
            "/v1/agent/worker/environment-snapshots/retired",
            json={"snapshot_ids": ["../latest"]},
        )
        too_many = await http.post(
            "/v1/agent/worker/environment-snapshots/retired",
            json={"snapshot_ids": ["5" * 32] * 501},
        )
    assert answer.json() == {"snapshots": [{"snapshot_id": "5" * 32, "owner": "o" * 64}]}
    snapshots.retired_images.assert_awaited_once_with(["5" * 32, "6" * 32])
    assert malformed.status_code == 422 and too_many.status_code == 422
