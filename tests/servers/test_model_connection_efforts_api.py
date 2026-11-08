"""Reasoning levels on owner connections, through the API: read, refreshed, admitted, inherited.

What a save stores from the provider's listings, the owner's refresh of a saved
connection (bounded, owner-scoped, guarded like an edit, closing nothing), and
a level's way through admission, follow-ups, claims and ``/config``. The
listings themselves are faked (``listings``); how they are read is
``tests/unit/test_model_efforts.py``.
"""

from __future__ import annotations

import copy
from typing import Any

from httpx import ASGITransport, AsyncClient
from tests.servers.test_agent_jobs_api import (  # noqa: F401 -- autouse fixtures
    _api_key_secret,
    _build_app,
    _FakeOwners,
)
from tests.servers.test_model_connections_api import (  # noqa: F401 -- autouse fixtures
    _KEY,
    PATH,
    FakeModelConnections,
    _claim,
    _client,
    _connection_row,
    _ConnectionClaimStore,
    _create,
    _create_job,
    _encryption,
    _follow_up,
    _parent,
    _settings_app,
    _Store,
    _turn_app,
    _TurnStorage,
    provider,
)
from tests.support.refusals import raises_http

from cloud_agent.api import model_connections as api
from cloud_agent.api.deps import get_owner_directory
from cloud_agent_shared.model_connections import PROTOCOLS

#: What a provider's listings declared for a model, per API, as stored.
_DECLARED = {
    protocol: {
        "status": "supported",
        "levels": ["high", "max"],
        "source": "models_listing_reasoning",
        "reason": "",
    }
    for protocol in PROTOCOLS
}


def _declared_row(**changes: Any) -> dict[str, Any]:
    row = _connection_row(**changes)
    for entry in row["model_checks"].values():
        entry["efforts"] = copy.deepcopy(_DECLARED)
    return row


async def test_a_verified_save_reads_the_declared_levels_once_beside_the_verification(listings):
    listings.answers["gpt-4.1"] = copy.deepcopy(_DECLARED)
    store = _Store()
    view = await _create(store)

    # One read for the save, whatever the number of models, at the saved endpoint.
    assert listings.calls == [
        {
            "base_url": "https://models.example/v1",
            "api_key": _KEY,
            "models": ["gpt-4.1", "openai/gpt-4.1"],
        }
    ]
    # It spends no check of its own: one per verified model, as before.
    assert store.model_connections.checks == ["owner-1", "owner-1"]
    row = store.model_connections.rows[view["id"]]
    assert row["model_checks"]["gpt-4.1"]["efforts"] == _DECLARED
    shown = {m["model"]: m["efforts"] for m in view["model_status"]}
    assert shown["gpt-4.1"]["chat_completions"]["levels"] == ["high", "max"]
    assert shown["openai/gpt-4.1"]["chat_completions"]["status"] == "unknown"


async def test_refreshing_reads_the_levels_of_a_saved_connection_and_changes_nothing_else(
    listings,
):
    store = _Store()
    view = await _create(store)
    before = copy.deepcopy(store.model_connections.rows[view["id"]])
    listings.calls.clear()
    listings.answers["gpt-4.1"] = copy.deepcopy(_DECLARED)

    async with await _client(_settings_app(store)) as client:
        refreshed = await client.post(f"{PATH}/{view['id']}/refresh-efforts")

    assert refreshed.status_code == 200, refreshed.text
    # The saved key at the saved Base URL, for every verified model; one check.
    assert listings.calls == [
        {"base_url": before["base_url"], "api_key": _KEY, "models": before["models"]}
    ]
    assert store.model_connections.checks.count("owner-1") == 3
    row = store.model_connections.rows[view["id"]]
    assert row["model_checks"]["gpt-4.1"]["efforts"] == _DECLARED
    for column in ("credential_revision", "config_revision", "models", "base_url", "enabled"):
        assert row[column] == before[column]
    assert (
        row["model_checks"]["gpt-4.1"]["protocols"]
        == before["model_checks"]["gpt-4.1"]["protocols"]
    )
    # A level is re-checked when a turn is claimed: no process is closed.
    assert store.model_connections.release_calls == []
    assert _KEY not in refreshed.text


async def test_a_refresh_is_bounded_and_owner_scoped(listings, monkeypatch):
    store = _Store()
    view = await _create(store)
    async with await _client(_settings_app(store, owner="owner-2")) as client:
        foreign = await client.post(f"{PATH}/{view['id']}/refresh-efforts")
    assert foreign.status_code == 404

    monkeypatch.setattr(api, "CHECKS_PER_HOUR", 2)
    async with await _client(_settings_app(store)) as client:
        limited = await client.post(f"{PATH}/{view['id']}/refresh-efforts")
    assert limited.status_code == 429
    assert len(listings.calls) == 1  # the save's own read only


async def test_a_refresh_overtaken_by_an_edit_writes_nothing(listings):
    store = _Store()
    view = await _create(store)
    listings.answers["gpt-4.1"] = copy.deepcopy(_DECLARED)

    async def edited_meanwhile(kwargs):
        row = store.model_connections.rows[view["id"]]
        row["models"] = ["gpt-4.1"]
        row["model_checks"] = {"gpt-4.1": row["model_checks"]["gpt-4.1"]}

    listings.during = edited_meanwhile
    async with await _client(_settings_app(store)) as client:
        late = await client.post(f"{PATH}/{view['id']}/refresh-efforts")

    assert late.status_code == 409, late.text
    kept = store.model_connections.rows[view["id"]]["model_checks"]["gpt-4.1"]
    assert kept["efforts"]["chat_completions"]["status"] == "unknown"


async def test_a_task_runs_at_a_declared_level_and_stores_it():
    store = _TurnStorage()
    row = _declared_row()
    store.connections.rows[row["id"]] = row
    async with AsyncClient(transport=ASGITransport(app=_turn_app(store)), base_url="http://t") as c:
        chosen = await _create_job(
            c, model="gpt-4.1", model_connection_id=row["id"], reasoning_effort="max"
        )
        auto = await _create_job(
            c, model="gpt-4.1", model_connection_id=row["id"], reasoning_effort="auto"
        )
        omitted = await _create_job(c, model="gpt-4.1", model_connection_id=row["id"])
        refused = await _create_job(
            c, model="gpt-4.1", model_connection_id=row["id"], reasoning_effort="low"
        )

    assert chosen.status_code == 201, chosen.text
    assert chosen.json()["reasoning_effort"] == "max"
    assert store.jobs[chosen.json()["id"]]["reasoning_effort"] == "max"
    assert auto.status_code == omitted.status_code == 201
    assert store.jobs[auto.json()["id"]]["reasoning_effort"] is None
    assert store.jobs[omitted.json()["id"]]["reasoning_effort"] is None
    assert refused.status_code == 400
    error = refused.json()["detail"]["error"]
    assert (error["reason"], error["offered_efforts"]) == (
        "effort_level_unsupported",
        ["high", "max"],
    )
    assert "high, max" in error["message"]


async def test_a_follow_up_inherits_its_level_and_rechecks_it_against_a_new_model():
    connections = FakeModelConnections()
    row = _declared_row(models=("gpt-4.1", "o4-mini"))
    row["model_checks"]["o4-mini"].pop("efforts")
    connections.rows[row["id"]] = row
    parent = _parent(reasoning_effort="max")

    inherited = await _follow_up(connections, parent)
    assert (inherited.reasoning_effort, inherited.reset_reasoning_effort) == (None, False)
    changed = await _follow_up(connections, parent, reasoning_effort="high")
    assert changed.reasoning_effort == "high"

    # o4-mini declares nothing: the inherited ``max`` is refused, not dropped.
    with raises_http() as refused:
        await _follow_up(connections, parent, model="o4-mini", model_connection_id=row["id"])
    assert refused.value.detail["error"]["reason"] == "effort_unknown"
    switched = await _follow_up(
        connections, parent, model="o4-mini", model_connection_id=row["id"], reasoning_effort="auto"
    )
    assert switched.reset_reasoning_effort is True


async def test_a_claim_rechecks_the_level_against_the_current_declaration():
    row = _declared_row()
    store = _ConnectionClaimStore(row, effort="max")
    granted = await _claim(store)
    assert granted.status_code == 200, granted.text
    assert granted.json()["reasoning_effort"] == "max"

    # A later refresh found the provider no longer declares ``max``.
    for entry in row["model_checks"].values():
        for fact in entry["efforts"].values():
            fact["levels"] = ["high"]
    store = _ConnectionClaimStore(row, effort="max")
    refused = await _claim(store)
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"]["reason"] == "effort_level_unsupported"


async def test_config_offers_each_agent_the_declared_levels_it_can_send():
    store = _TurnStorage()
    row = _declared_row()
    for entry in row["model_checks"].values():
        for fact in entry["efforts"].values():
            fact["levels"] = ["minimal", "high", "xhigh", "max"]
    store.connections.rows[row["id"]] = row
    app = _build_app(store)
    app.dependency_overrides[get_owner_directory] = _FakeOwners
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        config = (await client.get("/v1/agent/config")).json()

    offers = config["model_connections"][0]["model_efforts"]["gpt-4.1"]
    levels = {runtime: offer["levels"] for runtime, offer in offers.items()}
    assert levels == {
        "claude-code": ["high", "xhigh", "max"],
        "codex": ["minimal", "high", "xhigh", "max"],
        "kilo": ["minimal", "high", "xhigh", "max"],
        "opencode": ["minimal", "high", "xhigh", "max"],
        "pi": ["minimal", "high"],
    }
    assert {offer["status"] for offer in offers.values()} == {"supported"}
