"""Owners' model connections, from the settings routes to the turn that runs on one.

The repository is an in-memory stand-in with the port's semantics. The routes
are exercised through ASGI: what is stored (ciphertext, never the key), what
is shown (never the key, nor what a bad request sent), which owner can see
what (only their own), and what a change does to processes still running on
the old state. The turn half -- admission, follow-ups, waiting messages,
claims and residencies -- runs without the Gateway's catalog or grants for a
turn on a connection, and refuses rather than falls back when the connection
cannot serve it.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from tests.servers.test_agent_jobs_api import (  # noqa: F401 -- autouse fixtures
    _OWNER,
    FakeStorage,
    _api_key_secret,
    _build_app,
    _configured_identity_issuer,
    _FakeGateway,
)
from tests.servers.test_grant_at_claim import _app as _claim_app, _ClaimStore, _Grants

from cloud_agent.api import model_connections as api
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_gateway_client, get_grant_client, get_storage
from cloud_agent.application.follow_ups import prepare_follow_up
from cloud_agent.application.model_connection_check import CheckResult
from cloud_agent.application.model_connections import (
    invalidate_connection,
    model_connection_cipher,
    residency_credential,
)
from cloud_agent.application.pending_messages import prepare_message
from cloud_agent.domain.model_connections import ModelConnectionConflict

PATH = "/v1/agent/model-connections"
_KEY = "sk-live-owner-key-ABCDEFGHIJKLMNOP"
_CIPHER_SECRET = "model-connection-test-encryption-key-0123456789"


@pytest.fixture(autouse=True)
def _encryption(monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _CIPHER_SECRET)
    # The deployment gate is on for these cases; the gate itself has its own.
    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "true")


class FakeModelConnections:
    """The port, in memory, with the store's revision and tombstone rules."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.checks: list[str] = []
        self.bound: list[dict[str, Any]] = []
        self.release_calls: list[tuple[str, str]] = []
        self.stale: list[dict[str, Any]] = []
        self.scan_copies: list[tuple[str, str]] = []

    async def create_model_connection(self, *, user_id, values):
        if any(
            row["user_id"] == user_id
            and row["name"].lower() == values["name"].lower()
            and row["deleted_at"] is None
            for row in self.rows.values()
        ):
            raise ModelConnectionConflict(values["name"])
        now = datetime.now(UTC)
        row = {
            "user_id": user_id,
            "credential_revision": 1,
            "config_revision": 1,
            "enabled": True,
            "last_check": None,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
            **values,
        }
        self.rows[values["id"]] = row
        return copy.deepcopy(row)

    async def list_model_connections(self, *, user_id):
        return [
            copy.deepcopy(row)
            for row in self.rows.values()
            if row["user_id"] == user_id and row["deleted_at"] is None
        ]

    async def get_model_connection(self, *, user_id, connection_id):
        row = self.rows.get(connection_id)
        return copy.deepcopy(row) if row and row["user_id"] == user_id else None

    async def update_model_connection(self, *, user_id, connection_id, changes):
        row = self.rows.get(connection_id)
        if row is None or row["user_id"] != user_id or row["deleted_at"] is not None:
            return None
        row.update(changes)
        if {"api_key_ciphertext", "base_url", "protocols"} & set(changes) or (
            changes.get("enabled") is False
        ):
            row["credential_revision"] += 1
        if {"api_key_ciphertext", "base_url", "protocols", "models"} & set(changes):
            row["config_revision"] += 1
            row["last_check"] = None
        return copy.deepcopy(row)

    async def delete_model_connection(self, *, user_id, connection_id):
        row = self.rows.get(connection_id)
        if row is None or row["user_id"] != user_id or row["deleted_at"] is not None:
            return None
        row.update(
            deleted_at=datetime.now(UTC),
            enabled=False,
            api_key_ciphertext=None,
            credential_revision=row["credential_revision"] + 1,
        )
        return copy.deepcopy(row)

    async def reserve_model_connection_check(self, *, user_id, connection_id, limit, seconds):
        row = await self.get_model_connection(user_id=user_id, connection_id=connection_id)
        if row is None or row["deleted_at"] is not None:
            return {"error": "not_found"}
        if sum(self.rows[c]["user_id"] == user_id for c in self.checks) >= limit:
            return {"error": "limited"}
        self.checks.append(connection_id)
        return {"connection": row}

    async def record_model_connection_check(
        self, *, user_id, connection_id, config_revision, result
    ):
        row = self.rows[connection_id]
        if row["config_revision"] != config_revision or row["deleted_at"] is not None:
            return False
        row["last_check"] = result
        return True

    async def scan_key_ciphertexts(self, *, sandbox_id, limit):
        return [c for group, c in self.scan_copies if group == sandbox_id][:limit]

    async def bind_residency(
        self, *, user_id, connection_id, revision, thread_id, generation, scan_key_ciphertext
    ):
        row = self.rows.get(connection_id)
        ok = (
            row is not None
            and row["user_id"] == user_id
            and row["deleted_at"] is None
            and row["enabled"]
            and row["credential_revision"] == revision
        )
        if ok:
            self.bound.append({"thread_id": thread_id, "generation": generation})
            self.scan_copies.append(("asbx_bound", scan_key_ciphertext))
        return ok

    async def release_stale_residencies(self, *, user_id, connection_id):
        self.release_calls.append((user_id, connection_id))
        return list(self.stale)


class _HostPool:
    async def get_host(self, *, host):
        return None


class _Store:
    def __init__(self) -> None:
        self.model_connections = FakeModelConnections()
        self.host_pool = _HostPool()


def _settings_app(store: _Store, owner: str = "owner-1") -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_grant_client] = lambda: None
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": owner}
    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_CREATE = {
    "name": "My provider",
    "base_url": "https://models.example/v1/",
    "api_key": _KEY,
    "protocols": ["chat_completions"],
    "models": ["gpt-4.1", " openai/gpt-4.1 "],
}


async def _create(store: _Store, owner: str = "owner-1", **changes: Any) -> dict[str, Any]:
    async with await _client(_settings_app(store, owner)) as client:
        response = await client.post(PATH, json={**_CREATE, **changes})
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_new_connection_stores_only_ciphertext_and_never_answers_the_key():
    store = _Store()
    view = await _create(store)

    assert view["base_url"] == "https://models.example/v1"
    assert view["models"] == ["gpt-4.1", "openai/gpt-4.1"]
    assert view["runtimes"] == ["kilo", "opencode", "pi"]
    assert view["has_api_key"] is True
    assert _KEY not in str(view)
    stored = store.model_connections.rows[view["id"]]
    assert _KEY not in str(stored)
    assert model_connection_cipher().decrypt(stored["api_key_ciphertext"]) == _KEY

    async with await _client(_settings_app(store)) as client:
        listed = await client.get(PATH)
    assert listed.headers["Cache-Control"] == "no-store"
    assert listed.json()["configured"] is True
    assert _KEY not in listed.text


async def test_a_rejected_request_names_the_field_and_never_echoes_what_was_sent():
    store = _Store()
    async with await _client(_settings_app(store)) as client:
        response = await client.post(PATH, json={**_CREATE, "base_url": "http://models.example/v1"})
        bad_key = await client.post(PATH, json={**_CREATE, "api_key": f"{_KEY} trailing junk"})

    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["field"] == "base_url"
    assert _KEY not in response.text
    assert bad_key.status_code == 422
    assert _KEY not in bad_key.text
    assert response.headers["Cache-Control"] == "no-store"


async def test_another_owner_can_neither_see_nor_change_nor_check_a_connection():
    store = _Store()
    mine = await _create(store)
    async with await _client(_settings_app(store, "owner-2")) as client:
        listed = await client.get(PATH)
        changed = await client.patch(f"{PATH}/{mine['id']}", json={"enabled": False})
        deleted = await client.delete(f"{PATH}/{mine['id']}")
        checked = await client.post(
            f"{PATH}/{mine['id']}/check", json={"model": "gpt-4.1", "protocol": "chat_completions"}
        )

    assert listed.json()["connections"] == []
    assert [changed.status_code, deleted.status_code, checked.status_code] == [404, 404, 404]
    assert store.model_connections.rows[mine["id"]]["enabled"] is True


@pytest.mark.parametrize(
    "change",
    [{"api_key": "sk-rotated-key-0000"}, {"enabled": False}, {"base_url": "https://b.example"}],
)
async def test_a_credential_change_closes_processes_on_the_old_state(change):
    store = _Store()
    view = await _create(store)
    async with await _client(_settings_app(store)) as client:
        response = await client.patch(f"{PATH}/{view['id']}", json=change)

    assert response.status_code == 200, response.text
    assert "sk-rotated" not in response.text
    assert store.model_connections.rows[view["id"]]["credential_revision"] == 2
    assert store.model_connections.release_calls == [("owner-1", view["id"])]


async def test_deleting_erases_the_key_and_closes_its_processes():
    store = _Store()
    view = await _create(store)
    async with await _client(_settings_app(store)) as client:
        response = await client.delete(f"{PATH}/{view['id']}")
        again = await client.patch(f"{PATH}/{view['id']}", json={"enabled": True})

    assert response.status_code == 204
    assert store.model_connections.rows[view["id"]]["api_key_ciphertext"] is None
    assert store.model_connections.release_calls == [("owner-1", view["id"])]
    assert again.status_code == 404, "a deleted connection is never brought back"


async def test_a_check_runs_only_for_a_listed_model_and_protocol_and_keeps_its_result(
    monkeypatch,
):
    store = _Store()
    view = await _create(store)
    seen: list[dict[str, Any]] = []

    async def fake_check(**kwargs):
        seen.append(kwargs)
        return CheckResult(
            protocol=kwargs["protocol"], model=kwargs["model"], category="auth", http_status=401
        )

    monkeypatch.setattr(api, "check_connection", fake_check)
    async with await _client(_settings_app(store)) as client:
        unlisted = await client.post(
            f"{PATH}/{view['id']}/check", json={"model": "gpt-4o", "protocol": "chat_completions"}
        )
        wrong = await client.post(
            f"{PATH}/{view['id']}/check", json={"model": "gpt-4.1", "protocol": "responses"}
        )
        checked = await client.post(
            f"{PATH}/{view['id']}/check", json={"model": "gpt-4.1", "protocol": "chat_completions"}
        )

    assert [unlisted.status_code, wrong.status_code] == [400, 400]
    assert checked.json()["status"] == "failed" and checked.json()["category"] == "auth"
    assert seen[0]["api_key"] == _KEY and seen[0]["base_url"] == "https://models.example/v1"
    assert store.model_connections.rows[view["id"]]["last_check"]["category"] == "auth"
    assert _KEY not in checked.text


async def test_a_check_that_finishes_after_an_edit_does_not_vouch_for_the_new_config(
    monkeypatch,
):
    store = _Store()
    view = await _create(store)

    async def edit_while_checking(**kwargs):
        # The owner changes the Base URL while the old one is being checked.
        await store.model_connections.update_model_connection(
            user_id="owner-1", connection_id=view["id"], changes={"base_url": "https://b.example"}
        )
        return CheckResult(protocol=kwargs["protocol"], model=kwargs["model"], category="supported")

    monkeypatch.setattr(api, "check_connection", edit_while_checking)
    async with await _client(_settings_app(store)) as client:
        checked = await client.post(
            f"{PATH}/{view['id']}/check", json={"model": "gpt-4.1", "protocol": "chat_completions"}
        )

    assert checked.json()["status"] == "supported"
    assert checked.json()["recorded"] is False
    assert store.model_connections.rows[view["id"]]["last_check"] is None


async def test_with_the_gate_off_nothing_new_can_be_made_usable(monkeypatch):
    store = _Store()
    view = await _create(store)
    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "")
    async with await _client(_settings_app(store)) as client:
        listed = await client.get(PATH)
        created = await client.post(PATH, json={**_CREATE, "name": "Second"})
        rotated = await client.patch(f"{PATH}/{view['id']}", json={"api_key": "sk-other-0000"})
        checked = await client.post(
            f"{PATH}/{view['id']}/check", json={"model": "gpt-4.1", "protocol": "chat_completions"}
        )
        disabled = await client.patch(f"{PATH}/{view['id']}", json={"enabled": False})
        deleted = await client.delete(f"{PATH}/{view['id']}")

    assert listed.json()["enabled"] is False and len(listed.json()["connections"]) == 1
    assert [created.status_code, rotated.status_code, checked.status_code] == [403, 403, 403]
    assert [disabled.status_code, deleted.status_code] == [200, 204]


async def test_checks_are_limited_per_owner(monkeypatch):
    store = _Store()
    view = await _create(store)
    store.model_connections.checks = [view["id"]] * api.CHECKS_PER_HOUR

    async with await _client(_settings_app(store)) as client:
        response = await client.post(
            f"{PATH}/{view['id']}/check", json={"model": "gpt-4.1", "protocol": "chat_completions"}
        )
    assert response.status_code == 429


# -- turns ---------------------------------------------------------------------------


class _NoCatalogGateway(_FakeGateway):
    """Agent admission answers; the model catalog must not be asked at all."""

    async def model_catalog(self, user_id: str) -> list[str]:
        raise AssertionError("a turn on an owner connection read the Gateway catalog")

    async def model_catalog_with_reasoning_efforts(self, user_id: str):
        raise AssertionError("a turn on an owner connection read the Gateway catalog")


class _TurnStorage(FakeStorage):
    """The jobs fake, with this owner's connections behind the port."""

    def __init__(self) -> None:
        super().__init__()
        self.connections = FakeModelConnections()

    async def list_model_connections(self, *, user_id):
        return await self.connections.list_model_connections(user_id=user_id)

    async def get_model_connection(self, *, user_id, connection_id):
        return await self.connections.get_model_connection(
            user_id=user_id, connection_id=connection_id
        )


def _connection_row(
    owner: str = _OWNER, *, models=("gpt-4.1",), protocols=("chat_completions",), **changes
):
    row = {
        "id": "mconn_" + ("a" if owner == _OWNER else "b") * 32,
        "user_id": owner,
        "name": "Mine",
        "base_url": "https://models.example/v1",
        "protocols": list(protocols),
        "models": list(models),
        "api_key_ciphertext": model_connection_cipher().encrypt(_KEY),
        "credential_revision": 1,
        "enabled": True,
        "last_check": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "deleted_at": None,
    }
    row.update(changes)
    return row


def _turn_app(store: _TurnStorage) -> FastAPI:
    app = _build_app(store)
    app.dependency_overrides[get_gateway_client] = _NoCatalogGateway
    return app


async def _create_job(client: AsyncClient, **body: Any):
    return await client.post(
        "/v1/agent/jobs",
        json={"repo": None, "task_prompt": "fix it", "runtime": "opencode", **body},
    )


async def test_a_task_on_an_owner_connection_needs_neither_the_gateway_catalog_nor_its_id():
    store = _TurnStorage()
    row = _connection_row()
    store.connections.rows[row["id"]] = row
    async with AsyncClient(transport=ASGITransport(app=_turn_app(store)), base_url="http://t") as c:
        created = await _create_job(c, model="gpt-4.1", model_connection_id=row["id"])

    assert created.status_code == 201, created.text
    assert created.json()["model"] == "gpt-4.1"
    assert created.json()["model_connection_id"] == row["id"]
    assert store.jobs[created.json()["id"]]["model_connection_id"] == row["id"]


@pytest.mark.parametrize(
    ("runtime", "body", "row", "reason"),
    [
        # Claude Code needs Anthropic Messages; this connection offers Chat Completions.
        ("claude-code", {}, {}, "protocol_unsupported"),
        ("codex", {}, {}, "protocol_unsupported"),
        ("opencode", {}, {"protocols": ["anthropic_messages"]}, "protocol_unsupported"),
        ("codex", {}, {"protocols": ["anthropic_messages"]}, "protocol_unsupported"),
        (
            "claude-code",
            {"reasoning_effort": "high"},
            {"protocols": ["anthropic_messages"]},
            "effort_unsupported",
        ),
        ("opencode", {"model": "gpt-4o"}, {}, "model_not_listed"),
        ("opencode", {"reasoning_effort": "high"}, {}, "effort_unsupported"),
        ("opencode", {}, {"enabled": False}, "disabled"),
        ("opencode", {}, {"deleted_at": datetime.now(UTC), "enabled": False}, "deleted"),
        ("opencode", {}, {"user_id": "someone-else"}, "not_found"),
    ],
)
async def test_a_task_its_connection_cannot_serve_is_refused_before_it_is_queued(
    runtime, body, row, reason
):
    store = _TurnStorage()
    connection = _connection_row(**row)
    store.connections.rows[connection["id"]] = connection
    async with AsyncClient(transport=ASGITransport(app=_turn_app(store)), base_url="http://t") as c:
        refused = await _create_job(
            c,
            runtime=runtime,
            model_connection_id=connection["id"],
            **{"model": "gpt-4.1", **body},
        )

    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["error"]["reason"] == reason
    assert store.jobs == {}


async def test_claude_code_runs_on_a_connection_that_offers_anthropic_messages():
    store = _TurnStorage()
    row = _connection_row(
        models=("claude-sonnet-4-5",), protocols=("chat_completions", "anthropic_messages")
    )
    store.connections.rows[row["id"]] = row
    async with AsyncClient(transport=ASGITransport(app=_turn_app(store)), base_url="http://t") as c:
        created = await _create_job(
            c, runtime="claude-code", model="claude-sonnet-4-5", model_connection_id=row["id"]
        )

    assert created.status_code == 201, created.text
    stored = store.jobs[created.json()["id"]]
    assert (stored["runtime"], stored["model"], stored["model_connection_id"]) == (
        "claude-code",
        "claude-sonnet-4-5",
        row["id"],
    )
    assert stored.get("reasoning_effort") is None


async def test_a_connection_needs_a_model_and_a_well_formed_id():
    store = _TurnStorage()
    async with AsyncClient(transport=ASGITransport(app=_turn_app(store)), base_url="http://t") as c:
        malformed = await _create_job(c, model="gpt-4.1", model_connection_id="mconn_short")
    assert malformed.status_code == 422


class _FollowUpStore:
    def __init__(self, connections: FakeModelConnections) -> None:
        self.model_connections = connections
        self.attachments = None
        self.skills = None
        self.source_control = None


def _parent(**changes: Any) -> dict[str, Any]:
    parent = {
        "id": "ajob_1",
        "user_id": _OWNER,
        "repo": None,
        "runtime": "opencode",
        "model": "gpt-4.1",
        "model_connection_id": "mconn_" + "a" * 32,
        "reasoning_effort": None,
        "archived_at": None,
        "thread_sandbox": None,
    }
    parent.update(changes)
    return parent


async def _follow_up(connections, parent, **request):
    return await prepare_follow_up(
        parent,
        user={"user_id": _OWNER},
        skill_ids=None,
        attachment_ids=None,
        runtime=None,
        model=request.get("model"),
        model_connection_id=request.get("model_connection_id"),
        reasoning_effort=request.get("reasoning_effort"),
        store=_FollowUpStore(connections),
        app_credentials=None,
        gateway=_NoCatalogGateway(),
    )


async def test_a_follow_up_inherits_its_source_and_rechecks_it_without_the_gateway():
    connections = FakeModelConnections()
    row = _connection_row()
    connections.rows[row["id"]] = row

    inherited = await _follow_up(connections, _parent())
    assert (inherited.model, inherited.model_connection_id) == (None, None)

    row["enabled"] = False
    with pytest.raises(HTTPException) as refused:
        await _follow_up(connections, _parent())
    assert refused.value.status_code == 400
    assert refused.value.detail["error"]["reason"] == "disabled"


async def test_a_follow_up_switching_source_is_checked_against_the_new_one():
    connections = FakeModelConnections()
    row = _connection_row(models=("gpt-4.1", "o4-mini"))
    connections.rows[row["id"]] = row
    gateway_parent = _parent(model="glm-5.1", model_connection_id=None, reasoning_effort="high")

    switched = await _follow_up(
        connections, gateway_parent, model="o4-mini", model_connection_id=row["id"]
    )
    assert (switched.model, switched.model_connection_id) == ("o4-mini", row["id"])
    assert switched.reasoning_effort is None

    with pytest.raises(HTTPException) as refused:
        await _follow_up(connections, _parent(), reasoning_effort="high")
    assert refused.value.detail["error"]["reason"] == "effort_unsupported"
    with pytest.raises(HTTPException) as foreign:
        await _follow_up(
            connections, _parent(), model="gpt-4.1", model_connection_id="mconn_" + "f" * 32
        )
    assert foreign.value.detail["error"]["reason"] == "not_found"


async def test_a_claude_follow_up_inherits_its_messages_source_and_rechecks_the_protocol():
    connections = FakeModelConnections()
    row = _connection_row(models=("claude-sonnet-4-5",), protocols=("anthropic_messages",))
    connections.rows[row["id"]] = row
    parent = _parent(runtime="claude-code", model="claude-sonnet-4-5")

    inherited = await _follow_up(connections, parent)
    assert (inherited.model, inherited.model_connection_id) == (None, None)

    # The owner later unticks Messages: the inherited source no longer serves Claude Code.
    row["protocols"] = ["chat_completions"]
    with pytest.raises(HTTPException) as refused:
        await _follow_up(connections, parent)
    assert refused.value.detail["error"]["reason"] == "protocol_unsupported"


async def test_a_waiting_message_on_a_deleted_connection_is_held_with_the_reason():
    connections = FakeModelConnections()
    row = _connection_row(deleted_at=datetime.now(UTC), enabled=False, api_key_ciphertext=None)
    connections.rows[row["id"]] = row

    class _Queue(_FollowUpStore):
        def __init__(self) -> None:
            super().__init__(connections)
            self.conversation_queries = self

        async def get_job(self, job_id):
            return _parent()

    held = await prepare_message(
        _Queue(),
        message={
            "id": "apmsg_1",
            "revision": 1,
            "user_id": _OWNER,
            "thread_id": "athr_1",
            "model": None,
            "model_connection_id": None,
        },
        parent_job_id="ajob_1",
        gateway=_NoCatalogGateway(),
        app_credentials=None,
    )
    assert held is not None
    assert held.error is not None and "deleted" in held.error


# -- claim and residency -----------------------------------------------------------


class _ConnectionClaimStore(_ClaimStore):
    def __init__(
        self,
        row: dict[str, Any],
        *,
        runtime: str = "opencode",
        model: str = "gpt-4.1",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.connections = FakeModelConnections()
        self.connections.rows[row["id"]] = row
        self.row = row
        self.runtime = runtime
        self.model = model

    async def claim_job(self, **kwargs: Any) -> dict[str, Any] | None:
        claim = await super().claim_job(**kwargs)
        if claim is None:
            return None
        return {
            **claim,
            "user_id": _OWNER,
            "runtime": self.runtime,
            "model": self.model,
            "model_connection_id": self.row["id"],
            "reasoning_effort": None,
        }

    async def get_model_connection(self, *, user_id, connection_id):
        return await self.connections.get_model_connection(
            user_id=user_id, connection_id=connection_id
        )


async def _claim(store, grants, capabilities=("model-connections-v1",)):
    app = _claim_app(store, grants)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/v1/agent/worker/claim",
            json={
                "worker_id": "w1",
                "attestation": {"runner_capabilities": list(capabilities)},
            },
        )


async def test_a_claim_on_a_connection_carries_its_key_and_mints_no_gateway_grant():
    store, grants = _ConnectionClaimStore(_connection_row()), _Grants()
    response = await _claim(store, grants)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model_connection"] == {
        "connection_id": store.row["id"],
        "revision": 1,
        "base_url": "https://models.example/v1",
        "protocol": "chat_completions",
        "api_key": _KEY,
    }
    assert body["sandbox_token"] == ""
    assert grants.calls == []


async def test_a_claude_claim_carries_the_messages_protocol_and_mints_no_gateway_grant():
    row = _connection_row(
        models=("claude-sonnet-4-5",),
        protocols=("chat_completions", "anthropic_messages"),
        base_url="https://gateway.example/anthropic/v1",
    )
    store = _ConnectionClaimStore(row, runtime="claude-code", model="claude-sonnet-4-5")
    grants = _Grants()
    response = await _claim(store, grants)

    assert response.status_code == 200, response.text
    access = response.json()["model_connection"]
    # The URL as the owner wrote it; the host derives ANTHROPIC_BASE_URL from it.
    assert access["base_url"] == "https://gateway.example/anthropic/v1"
    assert access["protocol"] == "anthropic_messages"
    assert access["api_key"] == _KEY
    assert response.json()["sandbox_token"] == "" and grants.calls == []


async def test_a_host_that_cannot_launch_on_a_connection_gets_the_claim_back():
    store, grants = _ConnectionClaimStore(_connection_row()), _Grants()
    response = await _claim(store, grants, capabilities=())

    assert response.status_code == 503
    assert store.released and store.failed == []
    assert "sk-" not in response.text and grants.calls == []


@pytest.mark.parametrize(
    "change", [{"enabled": False}, {"deleted_at": "now", "enabled": False}, {"models": ["x"]}]
)
async def test_a_claim_on_a_connection_that_cannot_serve_it_ends_the_turn(change):
    store, grants = _ConnectionClaimStore(_connection_row(**change)), _Grants()
    response = await _claim(store, grants)

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "model_connection_unusable"
    assert store.failed == ["ajob_0001"] and store.released == []
    assert grants.calls == [], "never moved to the Gateway"


class _ResidencyStore:
    def __init__(self, connections: FakeModelConnections) -> None:
        self.model_connections = connections
        self.grants = None
        self.residency = self
        self.attempt_commands = self
        self.released: list[tuple[str, int]] = []
        self.failed: list[str] = []

    async def release_residency(self, *, thread_id, generation, host):
        self.released.append((thread_id, generation))
        return ""

    async def transition(self, *, job_id, detail, **kwargs):
        self.failed.append(detail)
        return True


_JOB = {
    "user_id": _OWNER,
    "runtime": "codex",
    "model": "gpt-5.1-codex",
    "reasoning_effort": None,
    "model_connection_id": "mconn_" + "a" * 32,
}
_ROW = {"thread_id": "athr_1", "generation": 4}
_FENCE = {"job_id": "ajob_1", "attempt_id": 1, "lease_generation": 1}


async def test_a_residency_on_a_connection_gets_its_key_bound_to_the_current_revision():
    connections = FakeModelConnections()
    row = _connection_row(models=("gpt-5.1-codex",), protocols=("responses",))
    connections.rows[row["id"]] = row
    store = _ResidencyStore(connections)

    class _NoGrants:
        async def mint(self, **kwargs):
            raise AssertionError("a residency on an owner connection minted a Gateway grant")

    credential, revision = await residency_credential(
        store, _NoGrants(), job=_JOB, row=dict(_ROW), host="h", fence=_FENCE
    )
    assert (credential, revision) == (_KEY, 1)
    assert connections.bound == [{"thread_id": "athr_1", "generation": 4}]


async def test_a_residency_whose_connection_changed_is_released_and_ends_the_turn():
    connections = FakeModelConnections()
    row = _connection_row(models=("gpt-5.1-codex",), protocols=("responses",))
    connections.rows[row["id"]] = row
    store = _ResidencyStore(connections)

    async def changed_meanwhile(**kwargs):
        return False

    connections.bind_residency = changed_meanwhile  # type: ignore[method-assign]
    with pytest.raises(HTTPException) as refused:
        await residency_credential(store, None, job=_JOB, row=dict(_ROW), host="h", fence=_FENCE)
    assert refused.value.status_code == 403
    assert refused.value.detail["error"]["reason"] == "changed"
    assert store.released == [("athr_1", 4)]
    assert store.failed and _KEY not in store.failed[0]


async def test_invalidation_releases_and_reports_every_stale_residency():
    store = _Store()
    store.model_connections.stale = [
        {"thread_id": "athr_1", "generation": 2, "host": "gone-host", "grant_id": ""}
    ]
    released = await invalidate_connection(
        store, None, user_id="owner-1", connection_id="mconn_" + "a" * 32
    )
    assert released == 1
    assert store.model_connections.release_calls == [("owner-1", "mconn_" + "a" * 32)]


async def test_config_offers_the_owners_connections_and_their_last_use_without_the_catalog():
    store = _TurnStorage()
    row = _connection_row(protocols=("chat_completions", "responses"))
    store.connections.rows[row["id"]] = row
    store.connections.rows["mconn_" + "d" * 32] = _connection_row(
        id="mconn_" + "d" * 32, name="Off", enabled=False
    )

    from cloud_agent.model_preference import ModelPreferenceView

    async def last_on_connection(user_id: str) -> ModelPreferenceView:
        return ModelPreferenceView.of(None, "gpt-4.1", row["id"])

    store.get_model_preference = last_on_connection  # type: ignore[method-assign]

    class _DownGateway(_FakeGateway):
        async def model_catalog_with_reasoning_efforts(self, user_id: str):
            from cloud_agent.gateway import GatewayError

            raise GatewayError("catalog down")

    app = _build_app(store)
    app.dependency_overrides[get_gateway_client] = _DownGateway
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        config = (await client.get("/v1/agent/config")).json()

    assert config["models"] == []
    assert config["default_model"] == "gpt-4.1"
    assert config["default_model_connection_id"] == row["id"]
    assert config["model_connections"] == [
        {
            "id": row["id"],
            "name": "Mine",
            "models": ["gpt-4.1"],
            "runtimes": ["codex", "kilo", "opencode", "pi"],
        }
    ]
    assert _KEY not in str(config)


async def test_with_the_gate_off_a_task_on_a_connection_is_refused_not_moved(monkeypatch):
    store = _TurnStorage()
    row = _connection_row()
    store.connections.rows[row["id"]] = row
    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "")
    async with AsyncClient(transport=ASGITransport(app=_turn_app(store)), base_url="http://t") as c:
        refused = await _create_job(c, model="gpt-4.1", model_connection_id=row["id"])

    assert refused.status_code == 400
    assert refused.json()["detail"]["error"]["reason"] == "feature_disabled"
    assert store.jobs == {}


def _remembering(store: _TurnStorage, connection_id: str, *, pinned: str | None = None):
    from cloud_agent.model_preference import ModelPreferenceView

    async def preference(user_id: str) -> ModelPreferenceView:
        return ModelPreferenceView.of(pinned, "glm-5.1", connection_id)

    store.get_model_preference = preference  # type: ignore[method-assign]


@pytest.mark.parametrize(
    "unavailable",
    [{"enabled": False}, {"models": ["other"]}, {"deleted_at": "now", "enabled": False}, "gate"],
)
async def test_an_unavailable_remembered_connection_leaves_the_default_empty(
    monkeypatch, unavailable
):
    """Never the Gateway's ``glm-5.1``, nor the deployment default: the owner chooses."""
    from tests.servers.test_agent_jobs_api import _with_deployment_default

    store = _TurnStorage()
    changes = {"models": ["glm-5.1"]}
    if unavailable != "gate":
        changes.update(unavailable)
    row = _connection_row(**changes)
    store.connections.rows[row["id"]] = row
    _remembering(store, row["id"])
    _with_deployment_default(monkeypatch, "qwen-next")
    if unavailable == "gate":
        monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "")
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://t"
    ) as c:
        config = (await c.get("/v1/agent/config")).json()

    assert "glm-5.1" in config["models"] and "qwen-next" in config["models"]
    assert config["default_model"] is None
    assert config["default_model_connection_id"] is None
    assert config["model_connections_enabled"] is (unavailable != "gate")


async def test_a_gateway_pin_still_outranks_a_remembered_connection(monkeypatch):
    store = _TurnStorage()
    row = _connection_row(models=("glm-5.1",))
    store.connections.rows[row["id"]] = row
    _remembering(store, row["id"], pinned="qwen-next")
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://t"
    ) as c:
        config = (await c.get("/v1/agent/config")).json()

    assert (config["default_model"], config["default_model_connection_id"]) == ("qwen-next", None)
