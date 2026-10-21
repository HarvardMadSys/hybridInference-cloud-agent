"""A pinned default model is owner-scoped, catalog-checked, and survives reload."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import identity
from cloud_agent.api import model_preferences
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.app import create_app, create_mcp_app, install_admin_no_store
from cloud_agent.model_preference import ModelPreferenceView

PATH = "/v1/agent/model-preference"
MODELS = ["glm-5.1", "qwen-next"]
ALIASES = {"glm-latest": "glm-5.1"}


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(
        identity.ENV_SESSION_SECRET, "model-preference-test-session-secret-long-enough"
    )
    application = FastAPI()
    application.include_router(model_preferences.router)
    install_admin_no_store(application, prefixes=(PATH,))
    #: What the store holds, keyed by owner: the pin, and the last-used model
    #: the repository derives from the newest turn.
    pins: dict[str, str | None] = {}
    last: dict[str, str | None] = {"owner-1": "qwen-next"}

    repository = AsyncMock()
    repository.get_model_preference.side_effect = lambda user_id: ModelPreferenceView.of(
        pins.get(user_id), last.get(user_id)
    )

    def save(user_id: str, model: str | None) -> ModelPreferenceView:
        pins[user_id] = model
        return ModelPreferenceView.of(model, last.get(user_id))

    repository.set_default_model.side_effect = save
    gateway = AsyncMock()
    gateway.agent_access.side_effect = lambda owner: {
        "user_id": owner,
        "allowed": True,
        "permissions": ["agent.use"],
    }
    gateway.model_catalog.return_value = list(MODELS)
    gateway.model_catalog_with_aliases.return_value = (list(MODELS), dict(ALIASES))
    application.dependency_overrides[get_gateway_client] = lambda: gateway
    application.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        model_preferences=repository
    )
    application.state.repository = repository
    application.state.gateway = gateway
    application.state.pins = pins
    application.state.last = last
    return application


def headers(owner="owner-1"):
    return {"Authorization": f"Bearer {identity.mint_session(user_id=owner)}"}


async def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    )


async def test_a_new_owner_follows_their_last_used_model(app):
    async with await client_for(app) as client:
        response = await client.get(PATH, headers=headers())
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    # No pin yet, so the newest turn is what a new task would start on.
    assert response.json() == {
        "default_model": None,
        "last_model": "qwen-next",
        "effective_model": "qwen-next",
        # This test sets no deployment default, so nothing is reported.
        "deployment_default": None,
    }


async def test_the_pin_outranks_the_last_used_model_and_clearing_restores_it(app):
    async with await client_for(app) as client:
        pinned = await client.put(PATH, headers=headers(), json={"default_model": "glm-5.1"})
        cleared = await client.put(PATH, headers=headers(), json={"default_model": None})
    assert pinned.status_code == 200
    assert pinned.json()["effective_model"] == "glm-5.1"
    assert cleared.status_code == 200
    # Clearing is not a reset to the deployment default: the last used model
    # takes over again.
    assert cleared.json()["effective_model"] == "qwen-next"
    assert app.state.pins["owner-1"] is None


async def test_an_alias_is_stored_as_the_canonical_id(app):
    async with await client_for(app) as client:
        saved = await client.put(PATH, headers=headers(), json={"default_model": "glm-latest"})
    assert saved.status_code == 200
    # The picker's options are canonical ids, so an alias stored verbatim would
    # pre-select nothing.
    assert app.state.pins["owner-1"] == "glm-5.1"


async def test_a_model_outside_the_catalog_is_refused_and_nothing_is_written(app):
    async with await client_for(app) as client:
        response = await client.put(PATH, headers=headers(), json={"default_model": "retired"})
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "model_not_available"
    assert "glm-5.1" in response.json()["detail"]["error"]["message"]
    app.state.repository.set_default_model.assert_not_awaited()


async def test_a_retired_pin_falls_back_to_the_last_used_model(app):
    app.state.pins["owner-1"] = "retired-model"
    async with await client_for(app) as client:
        response = await client.get(PATH, headers=headers())
    body = response.json()
    # The pin is still reported — it is what the owner saved, and the card has
    # to explain why it is not being applied — but a new task starts on the
    # last used model instead of on something the catalog no longer offers.
    assert body["default_model"] == "retired-model"
    assert body["last_model"] == "qwen-next"
    assert body["effective_model"] == "qwen-next"


async def test_a_catalog_outage_withholds_every_choice(app):
    app.state.gateway.model_catalog.return_value = []
    app.state.gateway.model_catalog_with_aliases.return_value = ([], {})
    async with await client_for(app) as client:
        read = await client.get(PATH, headers=headers())
        write = await client.put(PATH, headers=headers(), json={"default_model": "glm-5.1"})
    assert read.json()["effective_model"] is None
    assert read.json()["last_model"] == "qwen-next"
    # Nothing can be verified against an unreadable catalog, so nothing is pinned.
    assert write.status_code == 400


async def test_one_owner_never_sees_or_sets_another_owners_choice(app):
    async with await client_for(app) as client:
        await client.put(PATH, headers=headers("owner-1"), json={"default_model": "glm-5.1"})
        other = await client.get(PATH, headers=headers("owner-2"))
        mine = await client.get(PATH, headers=headers("owner-1"))
    assert other.json() == {
        "default_model": None,
        "last_model": None,
        "effective_model": None,
        "deployment_default": None,
    }
    assert mine.json()["effective_model"] == "glm-5.1"
    assert app.state.repository.set_default_model.await_args.args[0] == "owner-1"


@pytest.mark.parametrize(
    "body",
    [{}, {"default_model": "x" * 201}, {"default_model": "glm-5.1", "user_id": "other"}],
)
async def test_malformed_updates_are_refused_without_writing(app, body):
    async with await client_for(app) as client:
        response = await client.put(PATH, headers=headers(), json=body)
    assert response.status_code == 422
    app.state.repository.set_default_model.assert_not_awaited()


async def test_unauthenticated_callers_cannot_read_or_write(app):
    async with await client_for(app) as client:
        assert (await client.get(PATH)).status_code == 401
        assert (await client.put(PATH, json={"default_model": None})).status_code == 401
    app.state.repository.get_model_preference.assert_not_awaited()


def test_model_preference_routes_are_only_on_the_main_listener():
    assert PATH in create_app().openapi()["paths"]
    assert PATH not in create_mcp_app().openapi()["paths"]
