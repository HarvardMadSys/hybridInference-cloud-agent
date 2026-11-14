"""Admin routes for the models a deployment provides: saved verified, key write-only, audited."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_operator_models
from cloud_agent.api.deps import get_storage
from cloud_agent.application.model_connection_save import (
    VerificationRefused,
    VerificationTimedOut,
)
from cloud_agent.application.model_connection_status import verification_status
from cloud_agent.application.model_connections import operator_connection_cipher
from cloud_agent.domain.model_connections import ModelConnectionConflict
from cloud_agent.domain.operator_models import OperatorLimitsMissing

pytestmark = pytest.mark.asyncio

ADMIN = {"Authorization": "Bearer operator-models-admin"}
PATH = "/admin/agent/operator-model-connections"
_KEY = "sk-deployment-key-0123456789"
_NOW = datetime(2026, 11, 12, 9, tzinfo=UTC)
_SUPPORTED = {"protocols": {"chat_completions": {"status": "supported"}}}
_FAILED = {"protocols": {"chat_completions": {"status": "failed", "category": "auth"}}}


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "operator-models-admin")
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "operator-models-settings-root-key")


class _Repository:
    """Provided connections in memory, with the rules the Postgres one enforces."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.limit_rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.user_limits: dict[tuple[str, str, str], int | None] = {}
        self.usage_rows: list[dict[str, Any]] = []
        self.released: list[str] = []
        self.relay_hosts = 1
        self.moved_during_verification = False

    async def list_operator_connections(self):
        return [row for row in self.rows.values() if row["deleted_at"] is None]

    async def get_operator_connection(self, *, connection_id):
        return self.rows.get(connection_id)

    async def create_operator_connection(self, *, values):
        if any(
            row["name"].lower() == values["name"].lower()
            for row in await self.list_operator_connections()
        ):
            raise ModelConnectionConflict(values["name"])
        row = {
            **values,
            "model_checks": {m: {**r, "revision": 1} for m, r in values["model_checks"].items()},
            "is_default": False,
            "enabled": False,
            "config_revision": 1,
            "credential_revision": 1,
            "key_refused_at": None,
            "created_at": _NOW,
            "updated_at": _NOW,
            "deleted_at": None,
        }
        self.rows[row["id"]] = row
        return row

    async def update_operator_connection(self, *, connection_id, changes):
        row = self.rows[connection_id]
        if changes.get("enabled") and not row["enabled"]:
            models = changes.get("models", row["models"])
            missing = [m for m in models if (connection_id, m) not in self.limit_rows]
            if missing:
                raise OperatorLimitsMissing(missing)
        if changes.get("enabled") is False and row["enabled"]:
            row["credential_revision"] += 1
        if changes.get("is_default"):
            for other in self.rows.values():
                other["is_default"] = False
        row.update(changes)
        return row

    async def commit_verified_operator_update(
        self, *, connection_id, expected, changes, model_checks
    ):
        if self.moved_during_verification:
            return None
        row = self.rows[connection_id]
        endpoint = "base_url" in changes or "api_key_ciphertext" in changes
        if endpoint:
            row["config_revision"] += 1
        if "base_url" in changes:
            row["credential_revision"] += 1
        if "api_key_ciphertext" in changes:
            row["key_refused_at"] = None
        row.update(changes)
        row["model_checks"] = {
            m: {**r, "revision": row["config_revision"]} for m, r in model_checks.items()
        }
        return row

    async def delete_operator_connection(self, *, connection_id):
        row = self.rows[connection_id]
        row.update(deleted_at=_NOW, enabled=False, api_key_ciphertext=None)
        return row

    async def model_relay_host_count(self):
        return self.relay_hosts

    async def operator_limits(self, *, connection_ids):
        found: dict[str, dict[str, Any]] = {cid: {} for cid in connection_ids}
        for (cid, model), limit in self.limit_rows.items():
            if cid in found:
                found[cid][model] = limit
        return found

    async def set_operator_limit(
        self, *, connection_id, model, user_daily_requests, global_concurrency
    ):
        if model not in self.rows[connection_id]["models"]:
            return False
        self.limit_rows[(connection_id, model)] = {
            "user_daily_requests": user_daily_requests,
            "global_concurrency": global_concurrency,
        }
        return True

    async def set_operator_user_limit(self, *, connection_id, user_id, model, daily_requests):
        if model not in self.rows[connection_id]["models"]:
            return False
        self.user_limits[(connection_id, user_id, model)] = daily_requests
        return True

    async def clear_operator_user_limit(self, *, connection_id, user_id, model):
        return self.user_limits.pop((connection_id, user_id, model), "absent") != "absent"

    async def list_operator_users(self, *, connection_id, cursor, limit):
        users: dict[str, dict[str, Any]] = {}
        for (cid, user_id, model), value in sorted(self.user_limits.items()):
            if cid == connection_id and (cursor is None or user_id > cursor):
                users.setdefault(user_id, {"user_id": user_id, "limits": {}, "used_today": {}})
                users[user_id]["limits"][model] = value
        return list(users.values())[:limit]

    async def operator_usage(self, *, connection_id, start, end, limit):
        return [row for row in self.usage_rows if start <= row["day"] <= end][:limit]

    async def release_stale_operator_residencies(self, *, connection_id):
        self.released.append(connection_id)
        return []


class _Accounts:
    emails: ClassVar[dict[str, str]] = {"01HZXACCOUNTAAAAAAAAAAAAAAA": "a@example.edu"}

    async def account_emails(self, account_ids):
        return {aid: self.emails[aid] for aid in account_ids if aid in self.emails}

    async def identifier_owner(self, kind, normalized):
        return next((aid for aid, email in self.emails.items() if email == normalized), None)

    async def get_account(self, account_id):
        return SimpleNamespace(id=account_id) if account_id in self.emails else None


class _Harness:
    def __init__(self, monkeypatch) -> None:
        self.repository = _Repository()
        self.audit: list[dict[str, Any]] = []
        self.verified: list[dict[str, Any]] = []
        self.results: dict[str, dict[str, Any]] = {}
        self.timeout = False
        harness = self

        class Audit:
            async def record_admin_action(self, **row):
                harness.audit.append(row)
                return len(harness.audit)

        self.store = SimpleNamespace(
            operator_models=self.repository,
            audit=Audit(),
            local_accounts=_Accounts(),
            host_pool=SimpleNamespace(),
        )

        async def verify_endpoint(*, base_url, api_key, models):
            harness.verified.append({"base_url": base_url, "api_key": api_key, "models": models})
            if harness.timeout:
                raise VerificationTimedOut
            results = {model: harness.results.get(model, _SUPPORTED) for model in models}
            statuses = [verification_status(model, results[model]) for model in models]
            if any(not status["runtimes"] for status in statuses):
                raise VerificationRefused(statuses)
            return results

        monkeypatch.setattr(admin_operator_models, "verify_endpoint", verify_endpoint)
        self.app = FastAPI()
        self.app.include_router(admin_operator_models.router)
        self.app.dependency_overrides[get_storage] = lambda: self.store

    async def call(self, method: str, path: str = "", *, headers=ADMIN, **kwargs):
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://admin") as client:
            return await client.request(method, PATH + path, headers=headers, **kwargs)

    async def create(self, **changes):
        body = {
            "name": "Provider",
            "base_url": "https://provider.example/v1/",
            "api_key": _KEY,
            "models": ["model-1", "model-2"],
            **changes,
        }
        return await self.call("POST", json=body)

    def actions(self) -> list[str]:
        return [row["action"] for row in self.audit]


@pytest.fixture
def admin(monkeypatch) -> _Harness:
    return _Harness(monkeypatch)


async def _created(admin: _Harness, **changes) -> dict[str, Any]:
    response = await admin.create(**changes)
    assert response.status_code == 201, response.text
    return response.json()


async def _enabled(admin: _Harness) -> dict[str, Any]:
    view = await _created(admin)
    for model in view["models"]:
        await admin.call(
            "PUT", f"/{view['id']}/limits", json={"model": model, "user_daily_requests": 100}
        )
    response = await admin.call("PATCH", f"/{view['id']}", json={"enabled": True})
    assert response.status_code == 200, response.text
    admin.audit.clear()
    admin.verified.clear()
    admin.repository.released.clear()
    return response.json()


# -- who ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", ""),
        ("POST", ""),
        ("PATCH", "/x"),
        ("DELETE", "/x"),
        ("PUT", "/x/limits"),
        ("GET", "/x/users"),
        ("PUT", "/x/user-limits"),
        ("GET", "/x/usage"),
    ],
)
async def test_every_route_is_an_administrators(admin, method, path) -> None:
    response = await admin.call(method, path, headers={}, json={})
    assert response.status_code in (401, 403)
    assert admin.repository.rows == {} and admin.audit == []


# -- saving ------------------------------------------------------------------------


async def test_a_connection_is_saved_verified_disabled_and_without_its_key(admin) -> None:
    response = await admin.create(default_model="model-1", end_user_header="X-End-User")
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    view = response.json()
    assert view["id"].startswith("oconn_") and view["enabled"] is False
    assert view["base_url"] == "https://provider.example/v1"
    assert (view["default_model"], view["end_user_header"]) == ("model-1", "X-End-User")
    assert view["limits"] == {}
    assert [status["status"] for status in view["model_status"]] == ["ready", "ready"]
    # Verified with exactly what would be saved, before anything was.
    assert admin.verified == [
        {
            "base_url": "https://provider.example/v1",
            "api_key": _KEY,
            "models": ["model-1", "model-2"],
        }
    ]
    # The key is stored encrypted, in the provided connections' own domain, and answered nowhere.
    stored = admin.repository.rows[view["id"]]["api_key_ciphertext"]
    assert _KEY not in stored and operator_connection_cipher().decrypt(stored) == _KEY
    assert _KEY not in response.text and "api_key" not in view
    listed = await admin.call("GET")
    assert _KEY not in listed.text and "ciphertext" not in listed.text
    assert admin.actions() == ["agent.operator_model_connection.create"]
    assert _KEY not in json.dumps(admin.audit, default=str)


async def test_a_model_no_agent_can_use_saves_nothing(admin) -> None:
    admin.results["model-2"] = _FAILED
    response = await admin.create()
    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert error["type"] == "model_connection_verification_failed"
    assert {status["model"]: status["status"] for status in error["models"]} == {
        "model-1": "ready",
        "model-2": "failed",
    }
    assert admin.repository.rows == {} and admin.audit == []


async def test_a_verification_that_outlasts_its_bound_saves_nothing(admin) -> None:
    admin.timeout = True
    assert (await admin.create()).status_code == 504
    assert admin.repository.rows == {}


@pytest.mark.parametrize(
    "fields",
    [
        {"base_url": "http://provider.example/v1"},
        {"base_url": "https://provider.example:8443/v1"},
        {"api_key": "has space"},
        {"models": []},
        {"end_user_header": "Authorization"},
        {"user_concurrency": 0},
        {"unknown": 1},
    ],
)
async def test_what_could_not_be_a_connection_is_refused_before_any_provider_call(
    admin, fields
) -> None:
    response = await admin.create(**fields)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_model_connection"
    # Which field, never what was sent.
    assert _KEY not in response.text and "has space" not in response.text
    assert admin.verified == [] and admin.repository.rows == {}


async def test_the_default_model_must_be_listed(admin) -> None:
    response = await admin.create(default_model="model-9")
    assert response.status_code == 422 and admin.verified == []


async def test_a_taken_name_is_refused_before_any_provider_call(admin) -> None:
    await _created(admin)
    admin.verified.clear()
    response = await admin.create(name="provider")
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "operator_model_connection_name_taken"
    assert admin.verified == []


async def test_the_master_switch_stops_saving_and_enabling(admin, monkeypatch) -> None:
    view = await _created(admin)
    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "false")
    assert (await admin.create(name="Another")).status_code == 403
    assert (await admin.call("PATCH", f"/{view['id']}", json={"enabled": True})).status_code == 403
    listed = (await admin.call("GET")).json()
    assert listed["feature_enabled"] is False
    # Removing it still works.
    assert (await admin.call("DELETE", f"/{view['id']}")).status_code == 204


# -- enabling and limits ----------------------------------------------------------


async def test_enabling_waits_for_every_models_default_allowance(admin) -> None:
    view = await _created(admin)
    refused = await admin.call("PATCH", f"/{view['id']}", json={"enabled": True})
    assert refused.status_code == 409
    error = refused.json()["detail"]["error"]
    assert error["type"] == "operator_model_limits_missing"
    assert "model-1" in error["message"] and "model-2" in error["message"]

    limited = await admin.call(
        "PUT",
        f"/{view['id']}/limits",
        json={"model": "model-1", "user_daily_requests": 1500, "global_concurrency": 8},
    )
    assert limited.json()["limits"] == {
        "model-1": {"user_daily_requests": 1500, "global_concurrency": 8}
    }
    assert (await admin.call("PATCH", f"/{view['id']}", json={"enabled": True})).status_code == 409
    # Zero is a choice too: the model is for named accounts only.
    await admin.call(
        "PUT", f"/{view['id']}/limits", json={"model": "model-2", "user_daily_requests": 0}
    )
    enabled = await admin.call("PATCH", f"/{view['id']}", json={"enabled": True})
    assert enabled.status_code == 200 and enabled.json()["enabled"] is True
    assert admin.actions()[-3:] == [
        "agent.operator_model_connection.set_limit",
        "agent.operator_model_connection.set_limit",
        "agent.operator_model_connection.enable",
    ]


@pytest.mark.parametrize(
    "limit",
    [
        {"model": "model-9", "user_daily_requests": 1},
        {"model": "model-1", "user_daily_requests": -1},
        {"model": "model-1", "user_daily_requests": 1, "global_concurrency": 0},
    ],
)
async def test_a_limit_is_for_a_listed_model_and_never_negative(admin, limit) -> None:
    view = await _created(admin)
    response = await admin.call("PUT", f"/{view['id']}/limits", json=limit)
    assert response.status_code in (404, 422)
    assert admin.repository.limit_rows == {}


# -- editing -----------------------------------------------------------------------


async def test_a_rename_calls_no_provider_and_closes_nothing_it_need_not(admin) -> None:
    view = await _enabled(admin)
    response = await admin.call(
        "PATCH", f"/{view['id']}", json={"name": "Renamed", "base_url": view["base_url"]}
    )
    assert response.json()["name"] == "Renamed"
    assert admin.verified == []
    assert admin.repository.rows[view["id"]]["credential_revision"] == 1
    assert admin.audit[-1]["action"] == "agent.operator_model_connection.update"
    assert admin.audit[-1]["details"]["changed"] == ["name"]


async def test_rotating_the_key_verifies_every_model_and_keeps_running_processes(admin) -> None:
    view = await _enabled(admin)
    response = await admin.call(
        "PATCH", f"/{view['id']}", json={"api_key": "sk-new-deployment-key"}
    )
    assert response.status_code == 200
    assert admin.verified == [
        {
            "base_url": "https://provider.example/v1",
            "api_key": "sk-new-deployment-key",
            "models": ["model-1", "model-2"],
        }
    ]
    row = admin.repository.rows[view["id"]]
    assert (
        operator_connection_cipher().decrypt(row["api_key_ciphertext"]) == "sk-new-deployment-key"
    )
    # No process holds the key, so the revision a residency is bound to does not move.
    assert row["credential_revision"] == 1 and row["config_revision"] == 2
    [rotation] = admin.audit
    assert rotation["action"] == "agent.operator_model_connection.rotate_key"
    assert rotation["details"]["api_key"] == "[redacted]"
    assert "sk-new-deployment-key" not in json.dumps(admin.audit, default=str)
    assert "sk-new-deployment-key" not in response.text


async def test_re_entering_the_stored_key_rotates_nothing(admin) -> None:
    view = await _enabled(admin)
    response = await admin.call("PATCH", f"/{view['id']}", json={"api_key": _KEY})
    assert response.status_code == 200
    assert admin.verified == [] and admin.audit == []


async def test_a_new_base_url_is_verified_with_the_stored_key_and_closes_processes(admin) -> None:
    view = await _enabled(admin)
    response = await admin.call(
        "PATCH", f"/{view['id']}", json={"base_url": "https://other.example/v1"}
    )
    assert response.status_code == 200
    assert admin.verified[0]["base_url"] == "https://other.example/v1"
    assert admin.verified[0]["api_key"] == _KEY
    assert admin.repository.rows[view["id"]]["credential_revision"] == 2
    assert admin.repository.released == [view["id"]]


async def test_only_an_added_model_is_verified(admin) -> None:
    view = await _enabled(admin)
    response = await admin.call(
        "PATCH", f"/{view['id']}", json={"models": ["model-1", "model-2", "model-3"]}
    )
    assert response.status_code == 200
    assert admin.verified[0]["models"] == ["model-3"]
    statuses = {status["model"]: status["status"] for status in response.json()["model_status"]}
    assert statuses == {"model-1": "ready", "model-2": "ready", "model-3": "ready"}
    # The new model has no default allowance yet, so it is offered to nobody.
    assert "model-3" not in response.json()["limits"]


async def test_enabling_with_a_new_model_is_refused_before_anything_is_verified_or_saved(
    admin,
) -> None:
    view = await _created(admin)
    for model in view["models"]:
        await admin.call(
            "PUT", f"/{view['id']}/limits", json={"model": model, "user_daily_requests": 1}
        )
    admin.verified.clear()
    before = json.dumps(admin.repository.rows[view["id"]], default=str, sort_keys=True)
    # The added model has no default allowance, so the connection cannot be enabled with it.
    response = await admin.call(
        "PATCH",
        f"/{view['id']}",
        json={"models": ["model-1", "model-2", "model-3"], "enabled": True},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "operator_model_limits_missing"
    assert "model-3" in response.json()["detail"]["error"]["message"]
    assert admin.verified == []
    assert json.dumps(admin.repository.rows[view["id"]], default=str, sort_keys=True) == before


async def test_a_refused_verification_leaves_the_connection_as_it_was(admin) -> None:
    view = await _enabled(admin)
    before = json.dumps(admin.repository.rows[view["id"]], default=str, sort_keys=True)
    admin.results["model-3"] = _FAILED
    response = await admin.call(
        "PATCH", f"/{view['id']}", json={"models": ["model-1", "model-3"], "name": "X"}
    )
    assert response.status_code == 422
    assert json.dumps(admin.repository.rows[view["id"]], default=str, sort_keys=True) == before
    assert admin.audit == []


async def test_an_edit_overtaken_by_another_saves_nothing(admin) -> None:
    view = await _enabled(admin)
    admin.repository.moved_during_verification = True
    response = await admin.call(
        "PATCH", f"/{view['id']}", json={"api_key": "sk-new-deployment-key"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "operator_model_connection_changed"
    assert admin.audit == []


async def test_removing_the_default_model_clears_the_choice(admin) -> None:
    view = await _created(admin, default_model="model-2")
    response = await admin.call("PATCH", f"/{view['id']}", json={"models": ["model-1"]})
    assert response.status_code == 200
    assert response.json()["default_model"] is None and admin.verified[1:] == []


async def test_disabling_and_deleting_close_running_processes(admin) -> None:
    view = await _enabled(admin)
    disabled = await admin.call("PATCH", f"/{view['id']}", json={"enabled": False})
    assert disabled.json()["enabled"] is False
    assert admin.repository.rows[view["id"]]["credential_revision"] == 2
    deleted = await admin.call("DELETE", f"/{view['id']}")
    assert deleted.status_code == 204
    assert admin.repository.released == [view["id"], view["id"]]
    # The key is erased with it, and the id reads as gone from then on.
    assert admin.repository.rows[view["id"]]["api_key_ciphertext"] is None
    assert (await admin.call("PATCH", f"/{view['id']}", json={"name": "Back"})).status_code == 404
    assert (await admin.call("GET")).json()["connections"] == []
    assert admin.actions() == [
        "agent.operator_model_connection.disable",
        "agent.operator_model_connection.delete",
    ]


async def test_one_connection_is_where_a_new_owner_starts(admin) -> None:
    first = await _created(admin)
    second = await _created(admin, name="Second")
    await admin.call("PATCH", f"/{first['id']}", json={"is_default": True})
    await admin.call("PATCH", f"/{second['id']}", json={"is_default": True})
    listed = {c["name"]: c["is_default"] for c in (await admin.call("GET")).json()["connections"]}
    assert listed == {"Provider": False, "Second": True}


@pytest.mark.parametrize("connection_id", ["mconn_" + "0" * 32, "oconn_" + "9" * 32, "x"])
async def test_an_unknown_connection_is_not_found(admin, connection_id) -> None:
    for method, path, body in (
        ("PATCH", "", {"name": "X"}),
        ("DELETE", "", None),
        ("PUT", "/limits", {"model": "m", "user_daily_requests": 1}),
        ("GET", "/users", None),
        ("GET", "/usage", None),
    ):
        response = await admin.call(method, f"/{connection_id}{path}", json=body)
        assert response.status_code == 404, (method, path)


async def test_the_list_says_whether_any_host_reaches_the_relay(admin) -> None:
    await _created(admin)
    assert (await admin.call("GET")).json()["relay_hosts"] == 1
    admin.repository.relay_hosts = 0
    assert (await admin.call("GET")).json()["relay_hosts"] == 0


# -- one account's own allowance --------------------------------------------------

_ACCOUNT = "01HZXACCOUNTAAAAAAAAAAAAAAA"


@pytest.mark.parametrize("account", [_ACCOUNT, "a@example.edu", " A@Example.edu "])
async def test_an_account_is_named_by_id_or_by_email(admin, account) -> None:
    view = await _created(admin)
    response = await admin.call(
        "PUT",
        f"/{view['id']}/user-limits",
        json={"account": account, "model": "model-2", "daily_requests": 300},
    )
    assert response.status_code == 200
    assert response.json() == {
        "account_id": _ACCOUNT,
        "email": "a@example.edu",
        "limits": {"model-2": 300},
        "used_today": {},
    }
    [row] = admin.audit[-1:]
    assert row["action"] == "agent.operator_model_user_limit.set"
    assert row["target_user_id"] == _ACCOUNT
    assert row["details"]["daily_requests"] == 300

    listed = await admin.call("GET", f"/{view['id']}/users")
    assert listed.json()["users"][0]["limits"] == {"model-2": 300}

    cleared = await admin.call(
        "DELETE", f"/{view['id']}/user-limits", params={"account_id": _ACCOUNT, "model": "model-2"}
    )
    assert cleared.status_code == 204
    assert admin.audit[-1]["action"] == "agent.operator_model_user_limit.clear"
    again = await admin.call(
        "DELETE", f"/{view['id']}/user-limits", params={"account_id": _ACCOUNT, "model": "model-2"}
    )
    assert again.status_code == 404


async def test_unlimited_and_withheld_are_both_an_accounts_own_allowance(admin) -> None:
    view = await _created(admin)
    for value in (None, 0):
        response = await admin.call(
            "PUT",
            f"/{view['id']}/user-limits",
            json={"account": _ACCOUNT, "model": "model-1", "daily_requests": value},
        )
        assert response.json()["limits"] == {"model-1": value}


@pytest.mark.parametrize("account", ["nobody@example.edu", "01HZXNOBODY", "not an email@"])
async def test_an_unknown_account_gets_no_allowance(admin, account) -> None:
    view = await _created(admin)
    response = await admin.call(
        "PUT",
        f"/{view['id']}/user-limits",
        json={"account": account, "model": "model-1", "daily_requests": 5},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["type"] == "account_not_found"
    assert admin.repository.user_limits == {}


# -- usage -------------------------------------------------------------------------


async def test_usage_is_requests_per_day_account_and_model(admin) -> None:
    view = await _created(admin)
    admin.repository.usage_rows = [
        {"day": date(2026, 11, 11), "user_id": _ACCOUNT, "model": "model-1", "requests": 42},
        {"day": date(2026, 10, 1), "user_id": _ACCOUNT, "model": "model-1", "requests": 7},
    ]
    response = await admin.call(
        "GET", f"/{view['id']}/usage", params={"from": "2026-11-10", "to": "2026-11-12"}
    )
    assert response.json() == {
        "rows": [
            {
                "day": "2026-11-11",
                "account_id": _ACCOUNT,
                "email": "a@example.edu",
                "model": "model-1",
                "requests": 42,
            }
        ],
        "truncated": False,
    }


@pytest.mark.parametrize(
    "span",
    [{"from": "2026-11-12", "to": "2026-11-01"}, {"from": "2026-01-01", "to": "2026-11-12"}],
)
async def test_a_usage_range_is_bounded(admin, span) -> None:
    view = await _created(admin)
    assert (await admin.call("GET", f"/{view['id']}/usage", params=span)).status_code == 422
