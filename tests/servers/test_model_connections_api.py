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

import asyncio
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
from cloud_agent.application import (
    model_connection_check as check,
    model_connection_save as save,
    session_fork,
)
from cloud_agent.application.follow_ups import prepare_follow_up
from cloud_agent.application.model_connections import (
    access_for_job,
    invalidate_connection,
    model_connection_cipher,
    residency_credential,
    translator_token,
)
from cloud_agent.application.pending_messages import prepare_message
from cloud_agent.domain.model_connections import ModelConnectionConflict
from cloud_agent_shared.model_connections import PROTOCOLS

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
        checks = {m: {**r, "revision": 1} for m, r in values["model_checks"].items()}
        row = {
            "user_id": user_id,
            "credential_revision": 1,
            "config_revision": 1,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
            **values,
            "model_checks": checks,
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
        # As the store does: never a new endpoint or an added model unverified.
        if set(changes) - {"name", "models", "enabled"}:
            raise ValueError(f"cannot change {sorted(changes)} unverified")
        row = self.rows.get(connection_id)
        if row is None or row["user_id"] != user_id or row["deleted_at"] is not None:
            return None
        if "models" in changes and not set(changes["models"]) <= set(row["models"]):
            return None
        disabling = row["enabled"] and changes.get("enabled") is False
        row.update(changes)
        if disabling:
            row["credential_revision"] += 1
        if "models" in changes:
            row["model_checks"] = {
                m: r for m, r in row["model_checks"].items() if m in row["models"]
            }
        return copy.deepcopy(row)

    async def commit_verified_update(
        self, *, user_id, connection_id, expected, changes, model_checks
    ):
        row = self.rows.get(connection_id)
        if (
            row is None
            or row["user_id"] != user_id
            or row["deleted_at"] is not None
            or row["credential_revision"] != expected["credential_revision"]
            or row["config_revision"] != expected["config_revision"]
            or row["models"] != list(expected["models"])
            or row["model_checks"] != expected["model_checks"]
        ):
            return None
        endpoint = bool({"base_url", "api_key_ciphertext"} & set(changes))
        row.update(changes)
        if endpoint or changes.get("enabled") is False:
            row["credential_revision"] += 1
        if endpoint:
            row["config_revision"] += 1
        row["model_checks"] = {
            m: {**r, "revision": row["config_revision"]} for m, r in model_checks.items()
        }
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

    async def reserve_model_connection_checks(self, *, user_id, count, limit, seconds):
        if self.checks.count(user_id) + count > limit:
            return False
        self.checks.extend([user_id] * count)
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
    "models": ["gpt-4.1", " openai/gpt-4.1 "],
}


def _facts(
    chat: str = "supported", responses: str = "unsupported", anthropic: str = "unsupported"
) -> dict[str, Any]:
    """What ``verify_model`` returns: one native outcome per API."""
    category = {
        "supported": "supported",
        "unsupported": "endpoint_missing",
        "failed": "rate_limited",
    }
    return {
        "checked_at": "2026-10-28T00:00:00+00:00",
        "protocols": {
            protocol: {"status": status, "category": category[status], "http_status": None}
            for protocol, status in (
                ("chat_completions", chat),
                ("responses", responses),
                ("anthropic_messages", anthropic),
            )
        },
    }


def _supported(*protocols: str) -> dict[str, Any]:
    """A stored verification in which exactly ``protocols`` worked."""
    return _facts(*("supported" if p in protocols else "unsupported" for p in PROTOCOLS))


_UNUSABLE = _facts("unsupported", "unsupported", "unsupported")


class _Provider:
    """Stands in for ``verify_model``: records each call, answers per model."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: dict[str, dict[str, Any]] = {}
        self.during: Any = None

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({k: kwargs[k] for k in ("base_url", "api_key", "model")})
        if self.during is not None:
            await self.during(kwargs)
        return self.answers.get(kwargs["model"], _facts())

    def models(self) -> list[str]:
        return [call["model"] for call in self.calls]


@pytest.fixture(autouse=True)
def provider(monkeypatch) -> _Provider:
    """Every verification answers Chat Completions working, unless a test says otherwise."""
    fake = _Provider()
    monkeypatch.setattr(check, "verify_model", fake)
    return fake


async def _create(store: _Store, owner: str = "owner-1", **changes: Any) -> dict[str, Any]:
    async with await _client(_settings_app(store, owner)) as client:
        response = await client.post(PATH, json={**_CREATE, **changes})
    assert response.status_code == 201, response.text
    return response.json()


async def _patch(store: _Store, connection_id: str, body: dict[str, Any], owner: str = "owner-1"):
    async with await _client(_settings_app(store, owner)) as client:
        return await client.patch(f"{PATH}/{connection_id}", json=body)


async def test_a_verified_create_stores_its_facts_and_only_the_keys_ciphertext(provider):
    store = _Store()
    view = await _create(store)

    # Verified first, every model, at the exact normalized endpoint it saves.
    assert provider.calls == [
        {"base_url": "https://models.example/v1", "api_key": _KEY, "model": "gpt-4.1"},
        {"base_url": "https://models.example/v1", "api_key": _KEY, "model": "openai/gpt-4.1"},
    ]
    assert store.model_connections.checks == ["owner-1", "owner-1"]
    assert view["base_url"] == "https://models.example/v1"
    assert view["models"] == ["gpt-4.1", "openai/gpt-4.1"]
    assert "protocol_mode" not in view and "manual_protocols" not in view
    every = ["claude-code", "codex", "kilo", "opencode", "pi"]
    assert view["runtimes"] == every
    assert [(m["model"], m["status"], m["runtimes"]) for m in view["model_status"]] == [
        ("gpt-4.1", "ready", every),
        ("openai/gpt-4.1", "ready", every),
    ]
    assert view["has_api_key"] is True
    assert _KEY not in str(view)
    stored = store.model_connections.rows[view["id"]]
    assert _KEY not in str(stored)
    assert "protocols" not in stored
    assert {m: r["revision"] for m, r in stored["model_checks"].items()} == {
        "gpt-4.1": 1,
        "openai/gpt-4.1": 1,
    }
    assert model_connection_cipher().decrypt(stored["api_key_ciphertext"]) == _KEY

    async with await _client(_settings_app(store)) as client:
        listed = await client.get(PATH)
    assert listed.headers["Cache-Control"] == "no-store"
    assert listed.json()["configured"] is True
    assert listed.json()["checks_per_hour"] == check.CHECKS_PER_HOUR
    assert listed.json()["max_models"] == 8
    assert _KEY not in listed.text


async def test_a_create_with_one_unusable_model_stores_nothing_and_names_each_result(provider):
    store = _Store()
    provider.answers["openai/gpt-4.1"] = _UNUSABLE
    async with await _client(_settings_app(store)) as client:
        refused = await client.post(PATH, json=_CREATE)
        listed = await client.get(PATH)

    assert refused.status_code == 422, refused.text
    error = refused.json()["detail"]["error"]
    assert error["type"] == "model_connection_verification_failed"
    # Every model's result, the usable one too, so the owner can fix the form.
    assert [(m["model"], m["status"], m["category"]) for m in error["models"]] == [
        ("gpt-4.1", "ready", None),
        ("openai/gpt-4.1", "unsupported", "endpoint_missing"),
    ]
    assert _KEY not in refused.text
    # No connection and no key: a failing model is never dropped to save the rest.
    assert store.model_connections.rows == {}
    assert listed.json()["connections"] == []
    assert store.model_connections.checks == ["owner-1", "owner-1"]


@pytest.mark.parametrize(
    ("facts", "status", "category"),
    [
        (_facts("failed", "failed", "failed"), "failed", "rate_limited"),
        (_facts("unsupported", "unsupported", "unsupported"), "unsupported", "endpoint_missing"),
        (_facts("failed", "unsupported", "unsupported"), "failed", "rate_limited"),
    ],
)
async def test_failed_and_unsupported_stay_distinct_and_neither_is_saved(
    provider, facts, status, category
):
    store = _Store()
    provider.answers["gpt-4.1"] = facts
    async with await _client(_settings_app(store)) as client:
        refused = await client.post(PATH, json={**_CREATE, "models": ["gpt-4.1"]})

    model = refused.json()["detail"]["error"]["models"][0]
    assert (model["status"], model["category"]) == (status, category)
    assert model["runtimes"] == [] and model["message"]
    assert store.model_connections.rows == {}


async def test_a_model_usable_by_one_agent_is_enough_to_save(provider):
    store = _Store()
    provider.answers["gpt-4.1"] = _facts("unsupported", "supported", "unsupported")
    view = await _create(store, models=["gpt-4.1"])

    status = view["model_status"][0]
    # Responses alone serves Codex, and nothing else; Codex reaches it natively.
    assert (status["status"], status["runtimes"]) == ("ready", ["codex"])
    assert status["paths"] == [
        {
            "runtime": "codex",
            "mode": "direct",
            "client_protocol": "responses",
            "upstream_protocol": "responses",
        }
    ]


async def test_mixed_native_and_translated_paths_are_derived_per_model(provider):
    store = _Store()
    provider.answers["gpt-4.1"] = _facts("supported", "supported", "failed")
    provider.answers["openai/gpt-4.1"] = _facts("unsupported", "unsupported", "supported")
    view = await _create(store)

    by_model = {m["model"]: m for m in view["model_status"]}
    paths = {
        p["runtime"]: (p["mode"], p["upstream_protocol"]) for p in by_model["gpt-4.1"]["paths"]
    }
    # Codex natively on Responses; Claude Code's Messages could not be determined,
    # so it goes through the helper onto verified Chat.
    assert paths["codex"] == ("direct", "responses")
    assert paths["claude-code"] == ("translated", "chat_completions")
    assert by_model["gpt-4.1"]["protocols"]["anthropic_messages"]["status"] == "failed"
    assert by_model["openai/gpt-4.1"]["runtimes"] == ["claude-code"]
    stored = store.model_connections.rows[view["id"]]["model_checks"]
    # Only upstream facts are kept; paths are derived.
    assert "paths" not in stored["gpt-4.1"] and "runtimes" not in stored["gpt-4.1"]


async def test_a_create_past_the_hourly_limit_calls_no_provider_and_stores_nothing(provider):
    store = _Store()
    store.model_connections.checks = ["owner-1"] * (check.CHECKS_PER_HOUR - 1)
    async with await _client(_settings_app(store)) as client:
        refused = await client.post(PATH, json=_CREATE)
    # Two models need two checks; one is left, so none is taken and nothing dials out.
    assert refused.status_code == 429, refused.text
    assert provider.calls == [] and store.model_connections.rows == {}
    assert store.model_connections.checks.count("owner-1") == check.CHECKS_PER_HOUR - 1


async def test_a_taken_name_or_too_many_models_is_refused_before_any_check(provider):
    store = _Store()
    await _create(store)
    provider.calls.clear()
    async with await _client(_settings_app(store)) as client:
        taken = await client.post(PATH, json={**_CREATE, "name": "MY PROVIDER"})
        many = await client.post(PATH, json={**_CREATE, "models": [f"m{i}" for i in range(9)]})
    assert taken.status_code == 409 and many.status_code == 422
    assert provider.calls == []


async def test_a_manual_api_declaration_is_no_longer_accepted(provider):
    store = _Store()
    async with await _client(_settings_app(store)) as client:
        declared = await client.post(PATH, json={**_CREATE, "protocols": ["chat_completions"]})
    view = await _create(store)
    redeclared = await _patch(store, view["id"], {"protocols": ["responses"]})
    assert declared.status_code == 422 and redeclared.status_code == 422
    assert len(store.model_connections.rows) == 1


async def test_verification_is_bounded_to_four_models_at_a_time(provider):
    store = _Store()
    running = 0
    peak = 0

    async def overlap(kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1

    provider.during = overlap
    await _create(store, models=[f"m{i}" for i in range(8)])
    assert len(provider.calls) == 8 and peak == check.VERIFY_CONCURRENCY == 4


async def test_a_rejected_request_names_the_field_and_never_echoes_what_was_sent(provider):
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
    assert provider.calls == []


async def test_another_owner_can_neither_see_nor_change_a_connection(provider):
    store = _Store()
    mine = await _create(store)
    provider.calls.clear()
    async with await _client(_settings_app(store, "owner-2")) as client:
        listed = await client.get(PATH)
        changed = await client.patch(f"{PATH}/{mine['id']}", json={"enabled": False})
        rotated = await client.patch(f"{PATH}/{mine['id']}", json={"api_key": "sk-other-0000"})
        deleted = await client.delete(f"{PATH}/{mine['id']}")
        listed_models = await client.post(f"{PATH}/{mine['id']}/discover-models")

    assert listed.json()["connections"] == []
    assert [changed.status_code, rotated.status_code, deleted.status_code] == [404, 404, 404]
    assert listed_models.status_code == 404
    assert provider.calls == []
    assert store.model_connections.rows[mine["id"]]["enabled"] is True


@pytest.mark.parametrize(
    "change",
    [{"api_key": "sk-rotated-key-0000"}, {"base_url": "https://b.example"}],
)
async def test_a_new_endpoint_is_verified_for_every_model_before_it_replaces_the_old(
    provider, change
):
    store = _Store()
    view = await _create(store)
    provider.calls.clear()
    response = await _patch(store, view["id"], change)

    assert response.status_code == 200, response.text
    assert "sk-rotated" not in response.text
    expected = {
        "base_url": change.get("base_url", "https://models.example/v1"),
        "api_key": change.get("api_key", _KEY),
    }
    assert provider.calls == [
        {**expected, "model": "gpt-4.1"},
        {**expected, "model": "openai/gpt-4.1"},
    ]
    row = store.model_connections.rows[view["id"]]
    assert (row["credential_revision"], row["config_revision"]) == (2, 2)
    assert {r["revision"] for r in row["model_checks"].values()} == {2}
    assert store.model_connections.release_calls == [("owner-1", view["id"])]


@pytest.mark.parametrize(
    "change",
    [
        {"api_key": "sk-rotated-key-0000"},
        {"base_url": "https://b.example"},
        {"models": ["gpt-4.1", "openai/gpt-4.1", "gpt-5"]},
    ],
)
async def test_a_failed_edit_leaves_the_working_connection_exactly_as_it_was(provider, change):
    store = _Store()
    view = await _create(store)
    before = copy.deepcopy(store.model_connections.rows[view["id"]])
    provider.answers = {
        "gpt-4.1": _UNUSABLE,
        "openai/gpt-4.1": _UNUSABLE,
        "gpt-5": _UNUSABLE,
    }
    refused = await _patch(store, view["id"], change)

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["error"]["type"] == "model_connection_verification_failed"
    assert "sk-rotated" not in refused.text
    # The old key, endpoint, results and running processes are all untouched.
    assert store.model_connections.rows[view["id"]] == before
    assert store.model_connections.release_calls == []
    listed = (await (await _client(_settings_app(store))).get(PATH)).json()
    assert listed["connections"][0]["model_status"][0]["status"] == "ready"


async def test_disabling_closes_processes_without_calling_the_provider(provider):
    store = _Store()
    view = await _create(store)
    provider.calls.clear()
    response = await _patch(store, view["id"], {"enabled": False})

    assert response.status_code == 200, response.text
    assert provider.calls == []
    assert store.model_connections.rows[view["id"]]["credential_revision"] == 2
    assert store.model_connections.release_calls == [("owner-1", view["id"])]


async def test_deleting_erases_the_key_and_closes_its_processes(provider):
    store = _Store()
    view = await _create(store)
    async with await _client(_settings_app(store)) as client:
        response = await client.delete(f"{PATH}/{view['id']}")
        again = await client.patch(f"{PATH}/{view['id']}", json={"enabled": True})

    assert response.status_code == 204
    assert store.model_connections.rows[view["id"]]["api_key_ciphertext"] is None
    assert store.model_connections.release_calls == [("owner-1", view["id"])]
    assert again.status_code == 404, "a deleted connection is never brought back"


@pytest.mark.parametrize(
    "same",
    [
        {"name": "Renamed"},
        {
            "name": "Renamed",
            "base_url": "https://models.example/v1/",
            "models": ["gpt-4.1", "openai/gpt-4.1"],
        },
        {"name": "Renamed", "api_key": _KEY},
    ],
)
async def test_a_rename_with_unchanged_fields_keeps_credential_processes_and_checks(
    provider, monkeypatch, same
):
    """F1: only real changes count, and a rename never calls the provider."""
    store = _Store()
    view = await _create(store)
    provider.calls.clear()
    before = copy.deepcopy(store.model_connections.rows[view["id"]])
    renamed = await _patch(store, view["id"], same)
    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "")
    # With the deployment gate off, renaming through the same form still works.
    gate_off = await _patch(store, view["id"], {**same, "name": "Again"})

    assert renamed.status_code == 200, renamed.text
    assert gate_off.status_code == 200, gate_off.text
    assert provider.calls == [] and store.model_connections.checks == ["owner-1"] * 2
    row = store.model_connections.rows[view["id"]]
    assert row["credential_revision"] == before["credential_revision"]
    assert row["config_revision"] == before["config_revision"]
    assert row["model_checks"] == before["model_checks"]
    assert row["api_key_ciphertext"] == before["api_key_ciphertext"]
    assert renamed.json()["model_status"][0]["status"] == "ready"


async def test_adding_a_model_verifies_only_it_and_removing_one_calls_nothing(provider):
    store = _Store()
    view = await _create(store)
    provider.calls.clear()
    added = await _patch(store, view["id"], {"models": ["gpt-4.1", "openai/gpt-4.1", "gpt-5"]})
    assert added.status_code == 200, added.text
    assert provider.calls == [
        {"base_url": "https://models.example/v1", "api_key": _KEY, "model": "gpt-5"}
    ]
    assert {m["model"]: m["status"] for m in added.json()["model_status"]} == {
        "gpt-4.1": "ready",
        "openai/gpt-4.1": "ready",
        "gpt-5": "ready",
    }
    provider.calls.clear()
    removed = await _patch(store, view["id"], {"models": ["gpt-5"]})

    assert provider.calls == []
    row = store.model_connections.rows[view["id"]]
    assert set(row["model_checks"]) == {"gpt-5"}, "a removed model's result goes with it"
    assert row["credential_revision"] == 1 and removed.json()["models"] == ["gpt-5"]


@pytest.mark.parametrize("concurrent", ["rotated", "url", "disabled", "models", "deleted"])
async def test_an_edit_verified_while_the_connection_changed_is_not_saved(provider, concurrent):
    store = _Store()
    view = await _create(store)
    rows = store.model_connections.rows

    async def change_meanwhile(kwargs):
        row = rows[view["id"]]
        if concurrent == "rotated":
            row.update(api_key_ciphertext=model_connection_cipher().encrypt("sk-newer-0000"))
            row["credential_revision"] += 1
            row["config_revision"] += 1
        elif concurrent == "url":
            row.update(base_url="https://newer.example")
            row["credential_revision"] += 1
            row["config_revision"] += 1
        elif concurrent == "disabled":
            row.update(enabled=False)
            row["credential_revision"] += 1
        elif concurrent == "models":
            row["models"] = ["gpt-4.1"]
        else:
            row.update(deleted_at=datetime.now(UTC), enabled=False, api_key_ciphertext=None)
        provider.during = None

    provider.during = change_meanwhile
    meanwhile = None
    response = await _patch(store, view["id"], {"api_key": "sk-mine-0000"})
    meanwhile = copy.deepcopy(rows[view["id"]])

    assert response.status_code == (404 if concurrent == "deleted" else 409), response.text
    # The change made meanwhile stands; the late result undoes nothing.
    assert rows[view["id"]] == meanwhile
    if concurrent != "deleted":
        assert model_connection_cipher().decrypt(rows[view["id"]]["api_key_ciphertext"]) != (
            "sk-mine-0000"
        )
    assert store.model_connections.release_calls == []


async def test_a_rename_made_while_a_new_key_is_verified_does_not_conflict(provider):
    store = _Store()
    view = await _create(store)

    async def rename_meanwhile(kwargs):
        store.model_connections.rows[view["id"]]["name"] = "Renamed meanwhile"
        provider.during = None

    provider.during = rename_meanwhile
    response = await _patch(store, view["id"], {"api_key": "sk-mine-0000"})

    assert response.status_code == 200, response.text
    row = store.model_connections.rows[view["id"]]
    assert row["name"] == "Renamed meanwhile"
    assert model_connection_cipher().decrypt(row["api_key_ciphertext"]) == "sk-mine-0000"


async def test_a_verification_past_the_save_bound_writes_nothing(provider, monkeypatch):
    store = _Store()
    view = await _create(store)
    before = copy.deepcopy(store.model_connections.rows[view["id"]])
    monkeypatch.setattr(save, "SAVE_TIMEOUT_S", 0.01)

    async def hang(kwargs):
        await asyncio.sleep(1)

    provider.during = hang
    late = await _patch(store, view["id"], {"api_key": "sk-mine-0000"})
    async with await _client(_settings_app(store)) as client:
        created = await client.post(PATH, json={**_CREATE, "name": "Second"})

    assert late.status_code == 504, late.text
    assert late.json()["detail"]["error"]["type"] == "model_connection_verification_timeout"
    assert created.status_code == 504
    assert store.model_connections.rows == {view["id"]: before}


async def test_a_model_removed_and_re_added_meanwhile_keeps_its_newer_facts(provider):
    """Review 3: a model edit raises no revision, so the facts fence the late save."""
    store = _Store()
    view = await _create(store)
    rows = store.model_connections.rows

    async def remove_and_re_add(kwargs):
        provider.during = None
        row = rows[view["id"]]
        row["models"] = ["gpt-4.1"]
        row["model_checks"] = {"gpt-4.1": row["model_checks"]["gpt-4.1"]}
        row["models"] = ["gpt-4.1", "openai/gpt-4.1"]
        row["model_checks"]["openai/gpt-4.1"] = {**_facts(), "checked_at": "newer", "revision": 1}

    provider.during = remove_and_re_add
    late = await _patch(store, view["id"], {"models": ["gpt-4.1", "openai/gpt-4.1", "gpt-5"]})

    assert late.status_code == 409, late.text
    assert rows[view["id"]]["model_checks"]["openai/gpt-4.1"]["checked_at"] == "newer"
    assert rows[view["id"]]["models"] == ["gpt-4.1", "openai/gpt-4.1"]


async def test_an_edit_past_the_hourly_limit_changes_nothing(provider):
    store = _Store()
    view = await _create(store)
    before = copy.deepcopy(store.model_connections.rows[view["id"]])
    store.model_connections.checks = ["owner-1"] * check.CHECKS_PER_HOUR
    provider.calls.clear()
    response = await _patch(store, view["id"], {"models": ["gpt-4.1", "openai/gpt-4.1", "gpt-5"]})

    assert response.status_code == 429 and provider.calls == []
    assert store.model_connections.rows[view["id"]] == before


async def test_with_the_gate_off_nothing_new_can_be_made_usable(provider, monkeypatch):
    store = _Store()
    view = await _create(store)
    provider.calls.clear()
    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "")
    async with await _client(_settings_app(store)) as client:
        listed = await client.get(PATH)
        created = await client.post(PATH, json={**_CREATE, "name": "Second"})
        rotated = await client.patch(f"{PATH}/{view['id']}", json={"api_key": "sk-other-0000"})
        added = await client.patch(
            f"{PATH}/{view['id']}", json={"models": ["gpt-4.1", "openai/gpt-4.1", "gpt-5"]}
        )
        disabled = await client.patch(f"{PATH}/{view['id']}", json={"enabled": False})
        enabled = await client.patch(f"{PATH}/{view['id']}", json={"enabled": True})
        deleted = await client.delete(f"{PATH}/{view['id']}")

    assert listed.json()["enabled"] is False and len(listed.json()["connections"]) == 1
    assert [created.status_code, rotated.status_code, added.status_code] == [403, 403, 403]
    assert [disabled.status_code, enabled.status_code, deleted.status_code] == [200, 403, 204]
    assert provider.calls == []


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
        "models": list(models),
        "api_key_ciphertext": model_connection_cipher().encrypt(_KEY),
        "credential_revision": 1,
        "config_revision": 1,
        "enabled": True,
        # Every listed model as a verified save stores it: ``protocols`` worked.
        "model_checks": {model: {**_supported(*protocols), "revision": 1} for model in models},
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
        # Only a Chat Completions upstream is translated onto; Responses serves Codex alone.
        ("claude-code", {}, {"protocols": ["responses"]}, "protocol_unsupported"),
        ("pi", {}, {"protocols": ["responses"]}, "protocol_unsupported"),
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


@pytest.mark.parametrize("runtime", ["codex", "claude-code"])
async def test_codex_and_claude_code_are_admitted_on_a_chat_only_connection(runtime):
    store = _TurnStorage()
    row = _connection_row()
    store.connections.rows[row["id"]] = row
    async with AsyncClient(transport=ASGITransport(app=_turn_app(store)), base_url="http://t") as c:
        created = await _create_job(
            c, runtime=runtime, model="gpt-4.1", model_connection_id=row["id"]
        )

    assert created.status_code == 201, created.text
    stored = store.jobs[created.json()["id"]]
    assert (stored["runtime"], stored["model_connection_id"]) == (runtime, row["id"])
    # The stored facts are the upstream's, untouched by the translated plan.
    assert set(row["model_checks"]["gpt-4.1"]["protocols"]) == set(PROTOCOLS)
    assert row["model_checks"]["gpt-4.1"]["protocols"]["responses"]["status"] == "unsupported"


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

    # The owner later replaces Messages with Responses: nothing serves Claude Code now.
    # A later verification found only Responses: Claude Code has no path now.
    row["model_checks"]["claude-sonnet-4-5"] = {**_supported("responses"), "revision": 1}
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


@pytest.mark.parametrize(
    ("runtime", "client"), [("codex", "responses"), ("claude-code", "anthropic_messages")]
)
async def test_a_translated_claim_names_both_protocols_and_needs_the_compat_host(runtime, client):
    store = _ConnectionClaimStore(_connection_row(), runtime=runtime)
    grants = _Grants()
    response = await _claim(
        store, grants, ("model-connections-v1", "model-protocol-translation-v1")
    )

    assert response.status_code == 200, response.text
    access = response.json()["model_connection"]
    assert (access["protocol"], access["client_protocol"]) == ("chat_completions", client)
    assert access["api_key"] == _KEY and grants.calls == []
    # The member's loopback token: the platform's MAC over this member's
    # process identity, never a function of the provider key it shares.
    identity = {
        "user_id": _OWNER,
        "thread_id": response.json()["thread_id"],
        "source": f"{store.row['id']}@1",
        "client_protocol": client,
        "model": "gpt-4.1",
    }
    assert access["local_token"] == translator_token(model_connection_cipher(), **identity)
    assert access["local_token"] != translator_token(
        model_connection_cipher("another-platform-secret-0123456789"), **identity
    )
    assert access["local_token"] != translator_token(
        model_connection_cipher(), **{**identity, "thread_id": "athr_sibling"}
    )
    assert _KEY not in access["local_token"]

    older = _ConnectionClaimStore(_connection_row(), runtime=runtime)
    refused = await _claim(older, grants, ("model-connections-v1",))
    assert refused.status_code == 503
    assert older.released and older.failed == [] and grants.calls == []


async def test_a_direct_claim_never_asks_for_the_compat_host():
    store = _ConnectionClaimStore(_connection_row(), runtime="pi")
    response = await _claim(store, _Grants(), ("model-connections-v1",))
    assert response.status_code == 200, response.text
    assert "client_protocol" not in response.json()["model_connection"]
    assert "local_token" not in response.json()["model_connection"]


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

    credential, revision, plan = await residency_credential(
        store, _NoGrants(), job=_JOB, row=dict(_ROW), host="h", fence=_FENCE
    )
    assert (credential, revision, plan) == (_KEY, 1, "responses>responses")
    assert connections.bound == [{"thread_id": "athr_1", "generation": 4}]


async def test_a_translated_fork_gives_each_member_its_own_token():
    """Source and target share the key; each launch holds only its own member's token."""
    connections = FakeModelConnections()
    row = _connection_row()
    connections.rows[row["id"]] = row
    source_job = {**_JOB, "thread_id": "athr_source", "model": "gpt-4.1"}
    residencies = {
        "source": {"thread_id": "athr_source", "generation": 3},
        "target": {"thread_id": "athr_target", "generation": 1},
    }
    source, target = await session_fork._fork_credentials(
        _ResidencyStore(connections), None, source_job, residencies
    )
    assert source["credential"] == target["credential"] == _KEY
    assert source["inference_client_protocol"] == target["inference_client_protocol"] == "responses"
    tokens = {}
    for thread_id, fields in (("athr_source", source), ("athr_target", target)):
        # What that member's own later claim carries, so its runner masks it.
        claimed = await access_for_job(
            connections, {**source_job, "thread_id": thread_id}, model_connection_cipher()
        )
        assert claimed is not None
        assert fields["inference_local_token"] == claimed.local_token
        tokens[thread_id] = claimed.local_token
    assert tokens["athr_source"] != tokens["athr_target"]
    assert [b["thread_id"] for b in connections.bound] == ["athr_source", "athr_target"]


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
            # Declared: every agent; the composer is not told which go through the helper.
            "model_runtimes": {"gpt-4.1": ["claude-code", "codex", "kilo", "opencode", "pi"]},
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


# -- automatic detection: one model at a time ---------------------------------------


def _auto_row(**checks: dict[str, Any]) -> dict[str, Any]:
    """An automatic connection with three models, ``checks`` verified at revision 1."""
    return _connection_row(
        protocols=(),
        models=("chat-only", "responses-only", "never-checked"),
        model_checks={model: {**facts, "revision": 1} for model, facts in checks.items()},
    )


_MIXED = {
    "chat-only": _facts(chat="supported"),
    "responses-only": _facts(chat="unsupported", responses="supported"),
}


@pytest.mark.parametrize(
    ("runtime", "model", "status", "reason"),
    [
        ("opencode", "chat-only", 201, None),
        ("codex", "chat-only", 201, None),  # through the helper
        ("claude-code", "chat-only", 201, None),
        ("codex", "responses-only", 201, None),  # natively
        ("claude-code", "responses-only", 400, "protocol_unsupported"),
        ("opencode", "responses-only", 400, "protocol_unsupported"),
        ("opencode", "never-checked", 400, "model_unverified"),
    ],
)
async def test_admission_asks_the_models_own_verification(runtime, model, status, reason):
    store = _TurnStorage()
    row = _auto_row(**_MIXED)
    store.connections.rows[row["id"]] = row
    async with AsyncClient(transport=ASGITransport(app=_turn_app(store)), base_url="http://t") as c:
        created = await _create_job(c, runtime=runtime, model=model, model_connection_id=row["id"])

    assert created.status_code == status, created.text
    if reason:
        assert created.json()["detail"]["error"]["reason"] == reason


async def test_a_result_from_an_earlier_key_or_url_verifies_nothing():
    store = _TurnStorage()
    row = _auto_row(**_MIXED)
    row["config_revision"] = 2  # the key changed after these were verified
    store.connections.rows[row["id"]] = row
    async with AsyncClient(transport=ASGITransport(app=_turn_app(store)), base_url="http://t") as c:
        created = await _create_job(c, model="chat-only", model_connection_id=row["id"])
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://t"
    ) as c:
        config = (await c.get("/v1/agent/config")).json()

    assert created.json()["detail"]["error"]["reason"] == "model_unverified"
    assert config["model_connections"][0]["model_runtimes"] == {}


async def test_the_composer_offers_exactly_what_the_gates_accept():
    store = _TurnStorage()
    row = _auto_row(**_MIXED)
    store.connections.rows[row["id"]] = row
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://t"
    ) as c:
        config = (await c.get("/v1/agent/config")).json()

    option = config["model_connections"][0]
    assert option["models"] == ["chat-only", "responses-only", "never-checked"]
    assert option["model_runtimes"] == {
        "chat-only": ["claude-code", "codex", "kilo", "opencode", "pi"],
        "responses-only": ["codex"],
    }
    assert "chat_completions" not in str(option) and "translated" not in str(option)


@pytest.mark.parametrize(("model", "client"), [("chat-only", "responses"), ("responses-only", "")])
async def test_a_claim_resolves_codex_natively_or_through_the_helper_per_model(model, client):
    store = _ConnectionClaimStore(_auto_row(**_MIXED), runtime="codex", model=model)
    response = await _claim(
        store, _Grants(), ("model-connections-v1", "model-protocol-translation-v1")
    )

    assert response.status_code == 200, response.text
    access = response.json()["model_connection"]
    assert access.get("client_protocol", "") == client
    assert access["protocol"] == ("chat_completions" if client else "responses")


async def test_a_claim_on_an_unverified_model_ends_the_turn_with_the_reason():
    store = _ConnectionClaimStore(_auto_row(**_MIXED), model="never-checked")
    response = await _claim(store, _Grants())
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["reason"] == "model_unverified"


async def test_a_residency_names_the_plan_its_model_has_now():
    """Codex review 1: a re-verification between claim and residency changes the plan."""
    connections = FakeModelConnections()
    row = _auto_row(**{"gpt-5.1-codex": _facts(chat="supported", responses="supported")})
    row["models"] = ["gpt-5.1-codex"]
    connections.rows[row["id"]] = row
    store = _ResidencyStore(connections)
    *_, native = await residency_credential(
        store, None, job=_JOB, row=dict(_ROW), host="h", fence=_FENCE
    )
    # Verified again meanwhile: Responses is gone, Chat remains.
    row["model_checks"]["gpt-5.1-codex"] = {**_facts(chat="supported"), "revision": 1}
    *_, translated = await residency_credential(
        store, None, job=_JOB, row=dict(_ROW), host="h", fence=_FENCE
    )
    assert (native, translated) == ("responses>responses", "responses>chat_completions")
