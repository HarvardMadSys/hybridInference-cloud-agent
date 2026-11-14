"""A turn on a connection the deployment provides: resolved like an owner's, never holding a key."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tests.support.owners import CONNECTION_ID, connection_row

from cloud_agent.application.job_admission import require_connection_source
from cloud_agent.application.model_connections import (
    access_for_job,
    claim_model_connection,
    operator_connection_cipher,
    residency_credential,
    resolve_turn_connection,
)
from cloud_agent.domain.errors import Refused, Unavailable
from cloud_agent.domain.model_connections import ModelConnectionUnusable
from cloud_agent.domain.operator_models import Allowance
from cloud_agent.tokens import ENV_SIGNING_SECRET, parse_residency_model_token
from cloud_agent_shared.model_connections import (
    HOST_MODEL_CONNECTION_CAPABILITIES,
    MODEL_RELAY_CAPABILITY,
)

pytestmark = pytest.mark.asyncio

PROVIDED = "oconn_" + "a" * 32
_KEY = "sk-deployment-key-never-leaves"


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv(ENV_SIGNING_SECRET, "provided-turn-test-secret")
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "provided-turn-settings-root-key")
    # Owner connections are not configured at all on this deployment.
    monkeypatch.delenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", raising=False)


def _row(**changes: Any) -> dict[str, Any]:
    row = connection_row("nobody", connection_id=PROVIDED, models=("model-1", "model-2"))
    row.pop("user_id")
    row["api_key_ciphertext"] = operator_connection_cipher().encrypt(_KEY)
    return {**row, **changes}


class _Connections:
    """The owner-connection store, with one provided connection beside it."""

    def __init__(self, row: dict[str, Any] | None, allowance: Allowance) -> None:
        self.row, self.allowance = row, allowance
        self.bound: list[dict[str, Any]] = []
        self.bind_answer = True
        self.asked: list[tuple[str, str, str]] = []

    async def get_model_connection(self, *, user_id, connection_id):
        raise AssertionError("a provided connection is never read as an owner's")

    async def operator_connection(self, *, connection_id):
        return self.row if connection_id == PROVIDED else None

    async def operator_allowance(self, *, connection_id, user_id, model):
        self.asked.append((connection_id, user_id, model))
        return self.allowance

    async def bind_operator_residency(self, **binding):
        self.bound.append(binding)
        return self.bind_answer

    async def bind_residency(self, **_binding):
        raise AssertionError("no key of a provided connection is kept for scanning")


def _connections(row: dict[str, Any] | None = None, *, limit: int | None = 100, used: int = 0):
    return _Connections(_row() if row is None else row, Allowance(limit=limit, used=used))


async def _resolve(connections, **changes: Any):
    turn = {
        "user_id": "owner",
        "connection_id": PROVIDED,
        "runtime": "codex",
        "model": "model-1",
        **changes,
    }
    return await resolve_turn_connection(connections, **turn)


async def test_any_admitted_owner_runs_on_it_within_their_own_allowance() -> None:
    connections = _connections()
    row, plan = await _resolve(connections, user_id="somebody-else")
    assert row["id"] == PROVIDED and plan.client_protocol == "responses"
    assert connections.asked == [(PROVIDED, "somebody-else", "model-1")]


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (None, "provided_unavailable"),
        ({"enabled": False}, "provided_unavailable"),
        ({"deleted_at": "2026-11-12"}, "provided_unavailable"),
        # What an owner could fix on their own connection is the administrator's here.
        ({"models": ["model-2"]}, "provided_model_unavailable"),
        ({"model_checks": {}}, "provided_model_unavailable"),
        ({"config_revision": 2}, "provided_model_unavailable"),
    ],
)
async def test_what_the_deployment_stopped_providing_reads_as_that(row, reason) -> None:
    connections = _connections()
    connections.row = None if row is None else _row(**row)
    with pytest.raises(ModelConnectionUnusable) as refused:
        await _resolve(connections)
    assert refused.value.reason == reason
    assert "Enable it" not in str(refused.value) and "Settings" not in str(refused.value)


async def test_a_model_withheld_from_the_owner_is_refused_everywhere() -> None:
    for admit in (False, True):
        with pytest.raises(ModelConnectionUnusable) as refused:
            await _resolve(_connections(limit=0), admit=admit)
        assert refused.value.reason == "allowance_none"


async def test_a_used_up_allowance_refuses_a_new_turn_but_not_one_already_starting() -> None:
    spent = _connections(limit=5, used=5)
    with pytest.raises(ModelConnectionUnusable) as refused:
        await _resolve(spent, admit=True)
    assert refused.value.reason == "allowance_exhausted"
    assert "00:00 UTC" in str(refused.value)
    # Its residency and a Fork only ask whether the model is offered at all.
    row, _plan = await _resolve(spent)
    assert row["id"] == PROVIDED


async def test_unlimited_never_runs_out() -> None:
    await _resolve(_connections(limit=None, used=10**6), admit=True)


async def test_admission_refuses_a_spent_allowance_before_anything_is_queued() -> None:
    with pytest.raises(Refused) as refused:
        await require_connection_source(
            _connections(limit=1, used=1),
            user_id="owner",
            connection_id=PROVIDED,
            runtime="codex",
            model="model-1",
            reasoning_effort=None,
        )
    assert refused.value.type == "model_connection_unusable"
    assert refused.value.details == {"reason": "allowance_exhausted"}


async def test_the_master_switch_stops_provided_models_too(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "0")
    with pytest.raises(Refused) as refused:
        await require_connection_source(
            _connections(),
            user_id="owner",
            connection_id=PROVIDED,
            runtime="codex",
            model="model-1",
            reasoning_effort=None,
        )
    assert refused.value.details == {"reason": "feature_disabled"}


def _job(**changes: Any) -> dict[str, Any]:
    return {
        "id": "ajob_1",
        "attempt_id": 7,
        "lease_generation": 1,
        "user_id": "owner",
        "thread_id": "athr_1",
        "runtime": "opencode",
        "model": "model-1",
        "reasoning_effort": None,
        "model_connection_id": PROVIDED,
        **changes,
    }


async def test_the_claim_is_relayed_and_holds_no_key() -> None:
    access = await access_for_job(_connections(), _job())
    assert access is not None and access.relayed
    assert access.api_key == ""
    assert access.base_url == "https://provider.example/v1"
    assert access.source == f"{PROVIDED}@1"
    assert _KEY not in access.model_dump_json()


async def test_a_translated_member_gets_its_own_token_without_the_owner_key_configured() -> None:
    row = _row()
    row["model_checks"]["model-1"]["protocols"] = {"chat_completions": {"status": "supported"}}
    access = await access_for_job(_connections(row), _job(runtime="codex"))
    assert access is not None and access.plan.translated and len(access.local_token) == 64
    sibling = await access_for_job(_connections(row), _job(runtime="codex"), member="athr_2")
    assert sibling is not None and sibling.local_token != access.local_token


def _claim_store(connections) -> SimpleNamespace:
    return SimpleNamespace(
        model_connections=connections,
        attempt_commands=SimpleNamespace(
            release_claim=AsyncMock(return_value=True), transition=AsyncMock(return_value=True)
        ),
    )


async def test_a_host_that_reaches_the_relay_is_handed_the_turn() -> None:
    store = _claim_store(_connections())
    access = await claim_model_connection(
        store, _job(), [*HOST_MODEL_CONNECTION_CAPABILITIES, MODEL_RELAY_CAPABILITY]
    )
    assert access.relayed
    store.attempt_commands.release_claim.assert_not_awaited()


async def test_a_host_without_the_relay_gets_the_claim_back() -> None:
    store = _claim_store(_connections())
    with pytest.raises(Unavailable):
        await claim_model_connection(store, _job(), list(HOST_MODEL_CONNECTION_CAPABILITIES))
    store.attempt_commands.release_claim.assert_awaited_once()
    store.attempt_commands.transition.assert_not_awaited()


async def test_a_spent_allowance_ends_the_turn_at_its_claim() -> None:
    """Where a goal round, which is not admitted through the API, meets the allowance."""
    store = _claim_store(_connections(limit=3, used=3))
    with pytest.raises(Refused) as refused:
        await claim_model_connection(
            store, _job(), [*HOST_MODEL_CONNECTION_CAPABILITIES, MODEL_RELAY_CAPABILITY]
        )
    assert refused.value.details == {"reason": "allowance_exhausted"}
    transition = store.attempt_commands.transition.await_args.kwargs
    assert transition["to_state"] == "failed" and "00:00 UTC" in transition["detail"]
    store.attempt_commands.release_claim.assert_not_awaited()


def _residency_store(connections) -> SimpleNamespace:
    return SimpleNamespace(
        model_connections=connections,
        residency=SimpleNamespace(release_residency=AsyncMock(return_value=True)),
        attempt_commands=SimpleNamespace(transition=AsyncMock(return_value=True)),
    )


_FENCE = {"job_id": "ajob_1", "attempt_id": 7, "lease_generation": 1}


async def test_a_residency_is_handed_a_placeholder_bound_to_the_revision() -> None:
    connections = _connections(_row(credential_revision=6))
    store = _residency_store(connections)
    credential, revision, plan = await residency_credential(
        store, job=_job(), row={**_job(), "generation": 9}, host="runner-a", fence=_FENCE
    )
    assert credential != _KEY and _KEY not in str(credential)
    assert parse_residency_model_token(str(credential)) == {
        "thread_id": "athr_1",
        "residency_generation": 9,
    }
    assert (revision, plan) == (6, "chat_completions>chat_completions")
    assert connections.bound == [
        {
            "user_id": "owner",
            "connection_id": PROVIDED,
            "revision": 6,
            "thread_id": "athr_1",
            "generation": 9,
        }
    ]


async def test_a_connection_changed_while_the_turn_started_releases_the_residency() -> None:
    connections = _connections()
    connections.bind_answer = False
    store = _residency_store(connections)
    with pytest.raises(Refused) as refused:
        await residency_credential(
            store, job=_job(), row={**_job(), "generation": 9}, host="runner-a", fence=_FENCE
        )
    assert refused.value.details == {"reason": "changed"}
    store.residency.release_residency.assert_awaited_once()
    assert store.attempt_commands.transition.await_args.kwargs["to_state"] == "failed"


async def test_nothing_falls_back_between_the_two_kinds() -> None:
    """A turn naming a provided connection never reads the owner's, and the reverse."""
    connections = _connections()
    connections.row = None
    with pytest.raises(ModelConnectionUnusable):
        await _resolve(connections)

    class _OwnerOnly:
        async def get_model_connection(self, *, user_id, connection_id):
            return None

        async def operator_connection(self, **_kwargs):
            raise AssertionError("an owner's connection is never read as a provided one")

    with pytest.raises(ModelConnectionUnusable) as refused:
        await resolve_turn_connection(
            _OwnerOnly(),
            user_id="owner",
            connection_id=CONNECTION_ID,
            runtime="codex",
            model="model-1",
        )
    assert refused.value.reason == "not_found"
