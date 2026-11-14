"""Provided model connections, allowances and the model relay's fence, against a real database.

Only Postgres can show the two things that matter here: that an allowance
holds when requests race for its last unit, and that a provided connection's
placeholder resolves only for the residency bound to it while its turn runs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import _reserve_shared_chat
from tests.integration.storage.test_credential_queries_store import (  # noqa: F401 — fixtures
    db,
    store,
)

from cloud_agent.domain.model_connections import ModelConnectionConflict
from cloud_agent.domain.operator_models import OperatorLimitsMissing
from cloud_agent.storage.repositories.operator_models import PostgresOperatorModelRepository

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ID = "oconn_" + "a" * 32
_SUPPORTED = {"protocols": {"chat_completions": {"status": "supported"}}}


@pytest_asyncio.fixture
async def models(db):  # noqa: F811 — the imported fixture
    """The capability under test."""
    return PostgresOperatorModelRepository(db)


async def _create(
    models: PostgresOperatorModelRepository, *, connection_id: str = _ID, name: str = "Provider"
) -> dict[str, Any]:
    return await models.create_operator_connection(
        values={
            "id": connection_id,
            "name": name,
            "base_url": "https://provider.example/v1",
            "models": ["m1", "m2"],
            "default_model": "m1",
            "api_key_ciphertext": "ciphertext",
            "end_user_header": None,
            "user_concurrency": 4,
            "model_checks": {"m1": _SUPPORTED, "m2": _SUPPORTED},
        }
    )


async def _enabled(models: PostgresOperatorModelRepository, **limits: int | None) -> dict[str, Any]:
    await _create(models)
    for model in ("m1", "m2"):
        await models.set_operator_limit(
            connection_id=_ID,
            model=model,
            user_daily_requests=limits.get(model, 100),
            global_concurrency=None,
        )
    row = await models.update_operator_connection(connection_id=_ID, changes={"enabled": True})
    assert row is not None and row["enabled"]
    return row


# -- connections -------------------------------------------------------------------


async def test_a_connection_is_created_disabled_with_its_facts_at_revision_one(models) -> None:
    row = await _create(models)
    assert (row["enabled"], row["is_default"]) == (False, False)
    assert (row["config_revision"], row["credential_revision"]) == (1, 1)
    assert row["model_checks"]["m1"]["revision"] == 1
    assert await models.list_operator_connections() == [row]
    with pytest.raises(ModelConnectionConflict):
        await _create(models, connection_id="oconn_" + "b" * 32, name="PROVIDER")


async def test_enabling_needs_a_default_allowance_for_every_listed_model(models) -> None:
    await _create(models)
    with pytest.raises(OperatorLimitsMissing) as missing:
        await models.update_operator_connection(connection_id=_ID, changes={"enabled": True})
    assert missing.value.models == ["m1", "m2"]
    await models.set_operator_limit(
        connection_id=_ID, model="m1", user_daily_requests=10, global_concurrency=2
    )
    with pytest.raises(OperatorLimitsMissing) as missing:
        await models.update_operator_connection(connection_id=_ID, changes={"enabled": True})
    assert missing.value.models == ["m2"]
    # A model that is not listed takes no limit, so none can be set ahead of it.
    assert not await models.set_operator_limit(
        connection_id=_ID, model="m9", user_daily_requests=1, global_concurrency=None
    )
    await models.set_operator_limit(
        connection_id=_ID, model="m2", user_daily_requests=0, global_concurrency=None
    )
    enabled = await models.update_operator_connection(connection_id=_ID, changes={"enabled": True})
    assert enabled is not None and enabled["enabled"]
    assert (await models.operator_limits(connection_ids=[_ID]))[_ID] == {
        "m1": {"user_daily_requests": 10, "global_concurrency": 2},
        "m2": {"user_daily_requests": 0, "global_concurrency": None},
    }


async def test_only_disabling_and_a_new_base_url_move_the_revision_processes_are_bound_to(
    models,
) -> None:
    row = await _enabled(models)
    renamed = await models.update_operator_connection(
        connection_id=_ID, changes={"name": "Renamed"}
    )
    assert renamed is not None and renamed["credential_revision"] == 1

    rotated = await models.commit_verified_operator_update(
        connection_id=_ID,
        expected=renamed,
        changes={"api_key_ciphertext": "new-ciphertext"},
        model_checks={"m1": _SUPPORTED, "m2": _SUPPORTED},
    )
    # A new key: every model's facts are replaced, and no process needs closing.
    assert rotated is not None
    assert (rotated["credential_revision"], rotated["config_revision"]) == (1, 2)
    assert rotated["model_checks"]["m1"]["revision"] == 2

    moved = await models.commit_verified_operator_update(
        connection_id=_ID,
        expected=rotated,
        changes={"base_url": "https://other.example/v1"},
        model_checks={"m1": _SUPPORTED, "m2": _SUPPORTED},
    )
    assert moved is not None
    assert (moved["credential_revision"], moved["config_revision"]) == (2, 3)

    # An edit verified against the row as it was before is not applied on top.
    assert (
        await models.commit_verified_operator_update(
            connection_id=_ID,
            expected=row,
            changes={"api_key_ciphertext": "late"},
            model_checks={"m1": _SUPPORTED, "m2": _SUPPORTED},
        )
        is None
    )
    disabled = await models.update_operator_connection(
        connection_id=_ID, changes={"enabled": False}
    )
    assert disabled is not None and disabled["credential_revision"] == 3


async def test_a_removed_model_takes_its_facts_its_allowances_and_the_default_choice(
    models,
) -> None:
    await _enabled(models)
    await models.set_operator_user_limit(
        connection_id=_ID, user_id="user-1", model="m1", daily_requests=5
    )
    await models.set_operator_user_limit(
        connection_id=_ID, user_id="user-1", model="m2", daily_requests=5
    )
    row = await models.update_operator_connection(connection_id=_ID, changes={"models": ["m2"]})
    assert row is not None
    assert (row["models"], row["default_model"]) == (["m2"], None)
    assert list(row["model_checks"]) == ["m2"]
    assert list((await models.operator_limits(connection_ids=[_ID]))[_ID]) == ["m2"]
    [user] = await models.list_operator_users(connection_id=_ID, cursor=None, limit=10)
    assert user["limits"] == {"m2": 5}
    # A list that adds a model needs a verification, so this path refuses it.
    assert (
        await models.update_operator_connection(connection_id=_ID, changes={"models": ["m2", "m3"]})
        is None
    )


async def test_at_most_one_connection_is_where_a_new_owner_starts(models) -> None:
    await _create(models)
    other = "oconn_" + "b" * 32
    await _create(models, connection_id=other, name="Other")
    await models.update_operator_connection(connection_id=_ID, changes={"is_default": True})
    await models.update_operator_connection(connection_id=other, changes={"is_default": True})
    flags = {row["id"]: row["is_default"] for row in await models.list_operator_connections()}
    assert flags == {_ID: False, other: True}


async def test_deleting_erases_the_key_and_keeps_a_tombstone(models) -> None:
    await _enabled(models)
    deleted = await models.delete_operator_connection(connection_id=_ID)
    assert deleted is not None
    assert deleted["api_key_ciphertext"] is None and not deleted["enabled"]
    assert deleted["deleted_at"] is not None and deleted["credential_revision"] == 2
    assert await models.list_operator_connections() == []
    # A turn that names it still finds a row to be refused by.
    tombstone = await models.get_operator_connection(connection_id=_ID)
    assert tombstone is not None and tombstone["deleted_at"] is not None
    assert await models.delete_operator_connection(connection_id=_ID) is None
    # Its name is free again.
    await _create(models, connection_id="oconn_" + "b" * 32)


# -- allowances --------------------------------------------------------------------


async def test_an_allowance_is_the_accounts_own_or_the_models_default(models, store) -> None:  # noqa: F811
    await _enabled(models, m1=10, m2=0)
    connections = store.model_connections

    async def allowance(user: str, model: str):
        return await connections.operator_allowance(connection_id=_ID, user_id=user, model=model)

    assert (await allowance("user-1", "m1")).limit == 10
    assert not (await allowance("user-1", "m2")).offered
    # A model with no default is offered to nobody, whatever an account was given.
    assert not (await allowance("user-1", "m9")).offered

    await models.set_operator_user_limit(
        connection_id=_ID, user_id="user-1", model="m2", daily_requests=3
    )
    await models.set_operator_user_limit(
        connection_id=_ID, user_id="user-2", model="m1", daily_requests=None
    )
    assert (await allowance("user-1", "m2")).limit == 3
    assert (await allowance("user-2", "m1")).limit is None
    assert not (await allowance("user-2", "m2")).offered

    assert await models.clear_operator_user_limit(connection_id=_ID, user_id="user-1", model="m2")
    assert not await models.clear_operator_user_limit(
        connection_id=_ID, user_id="user-1", model="m2"
    )
    assert not (await allowance("user-1", "m2")).offered


async def test_requests_are_counted_up_to_the_allowance_and_no_further(models, store) -> None:  # noqa: F811
    await _enabled(models, m1=3, m2=0)
    count = {"connection_id": _ID, "user_id": "user-1", "model": "m1"}
    outcomes = [(await models.count_operator_request(**count))["outcome"] for _ in range(5)]
    assert outcomes == ["counted", "counted", "counted", "exhausted", "exhausted"]
    assert (await models.count_operator_request(connection_id=_ID, user_id="user-1", model="m2"))[
        "outcome"
    ] == "not_offered"
    assert (await models.count_operator_request(connection_id=_ID, user_id="user-1", model="m9"))[
        "outcome"
    ] == "not_offered"
    # Another account's count is its own.
    other = await models.count_operator_request(connection_id=_ID, user_id="user-2", model="m1")
    assert other["outcome"] == "counted"

    allowance = await store.model_connections.operator_allowance(**count)
    assert (allowance.limit, allowance.used, allowance.exhausted) == (3, 3, True)


async def test_racing_requests_cannot_pass_the_allowance_together(models) -> None:
    await _enabled(models, m1=7)
    count = {"connection_id": _ID, "user_id": "user-1", "model": "m1"}
    outcomes = await asyncio.gather(*(models.count_operator_request(**count) for _ in range(40)))
    assert sorted(outcome["outcome"] for outcome in outcomes).count("counted") == 7
    assert [o["outcome"] for o in outcomes].count("exhausted") == 33


async def test_an_unlimited_allowance_still_counts(models) -> None:
    await _enabled(models, m1=None)
    count = {"connection_id": _ID, "user_id": "user-1", "model": "m1"}
    for _ in range(4):
        assert (await models.count_operator_request(**count))["outcome"] == "counted"
    [user] = await models.list_operator_users(connection_id=_ID, cursor=None, limit=10)
    assert user == {"user_id": "user-1", "limits": {}, "used_today": {"m1": 4}}


async def test_a_request_the_provider_never_served_is_taken_back(models) -> None:
    await _enabled(models, m1=1)
    count = {"connection_id": _ID, "user_id": "user-1", "model": "m1"}
    counted = await models.count_operator_request(**count)
    assert (await models.count_operator_request(**count))["outcome"] == "exhausted"
    await models.refund_operator_request(**count, day=counted["day"])
    assert (await models.count_operator_request(**count))["outcome"] == "counted"
    # Never below zero, however often it is taken back.
    for _ in range(3):
        await models.refund_operator_request(**count, day=counted["day"])
    assert (await models.count_operator_request(**count))["outcome"] == "counted"


async def test_counts_belong_to_their_utc_day(db, models) -> None:  # noqa: F811
    await _enabled(models, m1=1)
    count = {"connection_id": _ID, "user_id": "user-1", "model": "m1"}
    assert (await models.count_operator_request(**count))["outcome"] == "counted"
    # Yesterday's requests leave today's allowance whole.
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_operator_model_usage SET day = day - 1")
    assert (await models.count_operator_request(**count))["outcome"] == "counted"
    today = datetime.now(UTC).date()
    rows = await models.operator_usage(
        connection_id=_ID, start=today - timedelta(days=7), end=today, limit=10
    )
    assert [(row["day"], row["requests"]) for row in rows] == [
        (today, 1),
        (today - timedelta(days=1), 1),
    ]
    assert (
        await models.operator_usage(connection_id=_ID, start=today, end=today, limit=1) == rows[:1]
    )


async def test_an_owner_is_shown_every_enabled_source_with_their_own_numbers(models) -> None:
    await _enabled(models, m1=10, m2=0)
    await _create(models, connection_id="oconn_" + "b" * 32, name="Disabled")
    await models.set_operator_user_limit(
        connection_id=_ID, user_id="user-1", model="m2", daily_requests=5
    )
    await models.count_operator_request(connection_id=_ID, user_id="user-1", model="m1")

    [source] = await models.operator_sources_for_user(user_id="user-1")
    assert source["id"] == _ID
    assert {m: (a.limit, a.used) for m, a in source["allowances"].items()} == {
        "m1": (10, 1),
        "m2": (5, 0),
    }
    [stranger] = await models.operator_sources_for_user(user_id="user-2")
    assert {m: (a.limit, a.used) for m, a in stranger["allowances"].items()} == {
        "m1": (10, 0),
        "m2": (0, 0),
    }


async def test_accounts_are_paged_by_id(models) -> None:
    await _enabled(models)
    for user in ("user-3", "user-1", "user-2"):
        await models.set_operator_user_limit(
            connection_id=_ID, user_id=user, model="m1", daily_requests=1
        )
    first = await models.list_operator_users(connection_id=_ID, cursor=None, limit=2)
    assert [row["user_id"] for row in first] == ["user-1", "user-2"]
    rest = await models.list_operator_users(connection_id=_ID, cursor="user-2", limit=2)
    assert [row["user_id"] for row in rest] == ["user-3"]


# -- residencies and the relay's fence ---------------------------------------------


async def _turn_on_provided(db, store, models) -> tuple[dict, dict, dict]:  # noqa: F811
    """A running shared-live turn whose job names the provided connection."""
    await _enabled(models, m1=10, m2=0)
    job, claim, sandbox = await _reserve_shared_chat(store)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET model_connection_id = $2 WHERE id = $1", job["id"], _ID
        )
    return job, claim, sandbox


async def _bind(store, job, sandbox, *, revision: int = 1) -> bool:  # noqa: F811
    return await store.model_connections.bind_operator_residency(
        user_id=job["user_id"],
        connection_id=_ID,
        revision=revision,
        thread_id=job["thread_id"],
        generation=sandbox["residency_generation"],
    )


async def test_the_placeholder_resolves_only_for_the_residency_bound_to_the_connection(
    db,  # noqa: F811
    store: Storage,  # noqa: F811
    models,
) -> None:
    job, claim, sandbox = await _turn_on_provided(db, store, models)
    lookup = {
        "thread_id": job["thread_id"],
        "residency_generation": sandbox["residency_generation"],
    }

    unbound = await models.resolve_model_relay_caller(**lookup, model="m1")
    assert unbound is not None and unbound["bound_revision"] is None

    assert await _bind(store, job, sandbox)
    caller = await models.resolve_model_relay_caller(**lookup, model="m1")
    assert caller is not None
    assert (caller["job_id"], caller["attempt_id"], caller["user_id"]) == (
        job["id"],
        claim["attempt_id"],
        job["user_id"],
    )
    assert caller["connection"]["id"] == _ID and caller["bound_revision"] == 1
    assert (caller["allowance"].limit, caller["allowance"].used) == (10, 0)
    # A model withheld from this owner, and a request that names none.
    withheld = await models.resolve_model_relay_caller(**lookup, model="m2")
    assert withheld is not None and not withheld["allowance"].offered
    listing = await models.resolve_model_relay_caller(**lookup, model=None)
    assert listing is not None and listing["allowance"] is None

    # Another residency generation, and Stop, buy nothing.
    assert (
        await models.resolve_model_relay_caller(
            thread_id=job["thread_id"],
            residency_generation=sandbox["residency_generation"] + 1,
            model="m1",
        )
        is None
    )
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET cancel_requested = TRUE WHERE id = $1", job["id"])
    assert await models.resolve_model_relay_caller(**lookup, model="m1") is None


async def test_a_turn_on_an_owner_connection_resolves_to_no_provided_connection(
    db,  # noqa: F811
    store: Storage,  # noqa: F811
    models,
) -> None:
    await _enabled(models)
    job, _claim, sandbox = await _reserve_shared_chat(store)
    caller = await models.resolve_model_relay_caller(
        thread_id=job["thread_id"],
        residency_generation=sandbox["residency_generation"],
        model="m1",
    )
    # A live turn, so not a 401; but nothing of the deployment's to use.
    assert caller is not None and caller["connection"] is None
    assert caller["allowance"] is None


async def test_a_binding_and_a_change_cannot_both_win(
    db,  # noqa: F811
    store: Storage,  # noqa: F811
    models,
) -> None:
    job, _claim, sandbox = await _turn_on_provided(db, store, models)

    # The change first: a binding at the old revision is refused.
    await models.update_operator_connection(connection_id=_ID, changes={"enabled": False})
    assert not await _bind(store, job, sandbox)
    await models.update_operator_connection(connection_id=_ID, changes={"enabled": True})
    assert not await _bind(store, job, sandbox, revision=1)

    # The binding first: the change's release finds it.
    assert await _bind(store, job, sandbox, revision=2)
    assert await models.release_stale_operator_residencies(connection_id=_ID) == []
    await models.update_operator_connection(connection_id=_ID, changes={"enabled": False})
    [released] = await models.release_stale_operator_residencies(connection_id=_ID)
    assert (released["thread_id"], released["generation"]) == (
        job["thread_id"],
        sandbox["residency_generation"],
    )
    # Released once.
    assert await models.release_stale_operator_residencies(connection_id=_ID) == []


async def test_a_rotated_key_leaves_bound_residencies_running(
    db,  # noqa: F811
    store: Storage,  # noqa: F811
    models,
) -> None:
    job, _claim, sandbox = await _turn_on_provided(db, store, models)
    assert await _bind(store, job, sandbox)
    row = await models.get_operator_connection(connection_id=_ID)
    assert row is not None
    await models.commit_verified_operator_update(
        connection_id=_ID,
        expected=row,
        changes={"api_key_ciphertext": "rotated"},
        model_checks={"m1": _SUPPORTED, "m2": _SUPPORTED},
    )
    assert await models.release_stale_operator_residencies(connection_id=_ID) == []
    caller = await models.resolve_model_relay_caller(
        thread_id=job["thread_id"],
        residency_generation=sandbox["residency_generation"],
        model="m1",
    )
    assert caller is not None
    assert caller["bound_revision"] == caller["connection"]["credential_revision"] == 1
    assert caller["connection"]["api_key_ciphertext"] == "rotated"


async def test_another_owners_residency_cannot_be_bound(
    db,  # noqa: F811
    store: Storage,  # noqa: F811
    models,
) -> None:
    job, _claim, sandbox = await _turn_on_provided(db, store, models)
    assert not await store.model_connections.bind_operator_residency(
        user_id="somebody-else",
        connection_id=_ID,
        revision=1,
        thread_id=job["thread_id"],
        generation=sandbox["residency_generation"],
    )


async def test_the_key_health_mark_keeps_when_it_started(models) -> None:
    await _enabled(models)
    await models.mark_operator_key_refused(connection_id=_ID, refused=True)
    first = (await models.get_operator_connection(connection_id=_ID))["key_refused_at"]
    assert first is not None
    await models.mark_operator_key_refused(connection_id=_ID, refused=True)
    assert (await models.get_operator_connection(connection_id=_ID))["key_refused_at"] == first
    await models.mark_operator_key_refused(connection_id=_ID, refused=False)
    assert (await models.get_operator_connection(connection_id=_ID))["key_refused_at"] is None


async def test_hosts_that_reach_the_relay_are_counted(store: Storage, models) -> None:  # noqa: F811
    assert await models.model_relay_host_count() == 0
    await store.host_pool.register_host(
        host="relay-host",
        credential_hash="hash",
        capabilities=["shared-sandbox-v1", "model-relay-v1"],
        slots_total=1,
    )
    await store.host_pool.register_host(
        host="plain-host",
        credential_hash="hash-2",
        capabilities=["shared-sandbox-v1"],
        slots_total=1,
    )
    assert await models.model_relay_host_count() == 1
