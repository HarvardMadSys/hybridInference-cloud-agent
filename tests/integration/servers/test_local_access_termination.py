"""Revoking a local account stops its work: real Postgres, real claims and residencies.

The owner is a local account, the Gateway stand-ins are the local ones, and
every turn runs on the owner's own verified model connection, as a local
account's turns must. Each case drives the real worker and residency paths: a
revocation must stop queued and running turns, release residencies (and the
connection key's binding to them), refuse a residency an attempt admitted
before it tries to establish afterwards, hold the waiting queue, kill the
group's terminals, and keep re-approval waiting until all of that is
acknowledged — without a later retry touching work the new approval allows.
"""

from __future__ import annotations

from tests.support.owners import on_connection

import asyncio
from typing import Any

import pytest
import pytest_asyncio
import tests.integration.servers.test_pending_message_queue as queue
from fastapi import HTTPException
from tests.integration.servers.test_pending_message_queue import (
    _claim_body as _gateway_claim_body,
    api as api,
    pool as pool,
)
from tests.support.local_accounts import LOCAL_SECRET, act, make_account
from tests.support.model_connections import (
    TEST_API_KEY,
    use_test_cipher,
    verified_connection,
    with_connection_capability,
)

from cloud_agent.accounts import config
from cloud_agent.accounts.directory import (
    LocalModeGateway,
    LocalModeGrantClient,
    LocalOwnerDirectory,
)
from cloud_agent.api._local_auth import terminal_open_fence
from cloud_agent.api.deps import get_owner_directory, get_grant_client
from cloud_agent.application.model_connections import connection_residency_credential
from cloud_agent.application.owner_termination import (
    enforce_ended_access,
    terminal_freeze_token,
    terminate_owner_access,
)
from cloud_agent.domain.accounts import AccountTransitionRefused
from cloud_agent.domain.execution import CANCELLED, RUNNING
from cloud_agent.domain.model_connections import ModelConnectionUnusable

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

OWNER = "01J0000000000000000000WNER"
MODEL = "provider-model"


def _claim_body(worker_id: str) -> dict[str, Any]:
    """The queue fixture's claim, from a host that runs members on owner connections."""
    return with_connection_capability(_gateway_claim_body(worker_id))


class _Manager:
    """A sandbox manager that records what termination asks of it."""

    def __init__(self, *, fail_suspend: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_suspend = fail_suspend
        self.fail_delete = False
        #: The terminals the host is running; a kill removes one.
        self.terminals = ["term-1", "term-2"]
        #: Set to hold a pass inside its first host call until released.
        self.hold: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def shared_residency_lost(self, thread_id: str, *, generation: int) -> None:
        self.calls.append(("residency_lost", thread_id))

    async def suspend_terminals(self, workspace: str, *, lease_generation: int, job_id: str):
        if self.fail_suspend:
            raise ConnectionError("host away")
        self.calls.append(("suspend", workspace, job_id))
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        return {"ok": True}

    async def list_terminals(self, workspace: str) -> dict[str, Any]:
        self.calls.append(("list", workspace))
        return {"terminals": [{"id": terminal} for terminal in self.terminals]}

    async def delete_terminal(self, workspace: str, terminal_id: str) -> dict[str, Any]:
        if self.fail_delete:
            raise ConnectionError("host away")
        self.calls.append(("delete", terminal_id))
        if terminal_id in self.terminals:
            self.terminals.remove(terminal_id)
        return {"ok": True}

    async def resume_terminals(self, workspace: str, *, lease_generation: int, job_id: str):
        self.calls.append(("resume", workspace, job_id))
        return {"ok": True}


@pytest_asyncio.fixture(autouse=True)
async def _local_owner(api, monkeypatch):
    """Make the queue fixture's owner a local account whose turns run on its own connection."""
    monkeypatch.setenv(config.ENV_AUTH_MODE, "local")
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, LOCAL_SECRET)
    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "true")
    use_test_cipher(monkeypatch)
    # The fixture's owner override reads this name on every request.
    monkeypatch.setattr(queue, "_OWNER", OWNER)
    monkeypatch.setattr(queue, "_claim_body", _claim_body)
    await make_account(api.store, "owner@example.org", admission="approved", account_id=OWNER)
    local_gateway = LocalModeGateway(LocalOwnerDirectory(api.store.local_accounts))
    api.app.dependency_overrides[get_owner_directory] = lambda: local_gateway
    api.app.dependency_overrides[get_grant_client] = LocalModeGrantClient
    connection = await verified_connection(api.store, OWNER, models=(MODEL,))
    api.connection = connection
    create = api.create

    async def create_on_connection(prompt: str = "fix it", **fields: Any) -> dict[str, Any]:
        return await create(
            prompt, **{"model": MODEL, "model_connection_id": connection["id"], **fields}
        )

    api.create = create_on_connection
    api.manager = _Manager()
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_host",
        lambda host: api.manager,
    )


async def _job(api, job_id: str) -> dict[str, Any]:
    async with api.pool.acquire() as conn:
        return dict(
            await conn.fetchrow(
                "SELECT state, cancel_requested FROM agent_jobs WHERE id = $1", job_id
            )
        )


async def _residency(api, thread_id: str) -> str:
    async with api.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT residency_status FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
            thread_id,
        )


async def _end_turn(api, turn: dict[str, Any]) -> None:
    assert await api.store.attempt_commands.transition(
        job_id=turn["job_id"],
        attempt_id=turn["attempt_id"],
        lease_generation=turn["lease_generation"],
        from_states=(RUNNING,),
        to_state=CANCELLED,
        detail="stopped",
    )


async def test_revocation_stops_a_running_turn_and_holds_reapproval_until_it_ends(api):
    job = await api.create()
    turn = await api.start(job)
    waiting = await api.send(job["thread_id"], "after this")
    assert waiting.status_code in (200, 201, 202), waiting.text

    revoked = await act(api.store, OWNER, "revoke")
    generation = revoked.account.version
    complete = await terminate_owner_access(api.store, LocalModeGrantClient(), OWNER, generation)

    assert complete is False  # the turn has been asked to stop, not stopped yet
    assert await _job(api, job["id"]) == {"state": "running", "cancel_requested": True}
    assert await _residency(api, job["thread_id"]) == "none"
    assert (await api.queue(job["thread_id"]))["queue_paused_reason"] == "stopped"
    token = terminal_freeze_token(OWNER, generation)
    assert ("suspend", job["thread_id"], token) in api.manager.calls
    assert ("delete", "term-1") in api.manager.calls and ("delete", "term-2") in api.manager.calls
    assert api.manager.calls[-1] == ("resume", job["thread_id"], token)
    with pytest.raises(AccountTransitionRefused) as refused:
        await act(api.store, OWNER, "approve")
    assert refused.value.code == "access_cleanup_pending"

    await _end_turn(api, turn)
    assert await enforce_ended_access(api.store, LocalModeGrantClient()) == 1
    assert await api.store.local_accounts.open_terminations(limit=10) == []
    approved = await act(api.store, OWNER, "approve")
    assert approved.account.admission_status == "approved"


async def test_an_attempt_admitted_before_revocation_cannot_establish_a_residency_after(api):
    job = await api.create()
    claim = await api.claim(job["id"])
    thread_id = claim["thread_id"]
    reserved = await api.store.sandbox.reserve_thread_sandbox(
        thread_id=thread_id,
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host=queue._HOST,
        sandbox_id=f"asbx_{thread_id}",
        container_name=f"chat-{thread_id}",
        runtime_id="claude-code",
        runtime_version=claim.get("runtime_version"),
        sandbox_image_digest=queue._IMAGE_DIGEST,
        execution_profile=claim["execution_profile"],
    )
    assert await api.store.sandbox.mark_thread_sandbox_running(
        thread_id=thread_id,
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        sandbox_generation=reserved["generation"],
        activation_generation=reserved["activation_generation"],
        container_id=f"container-{thread_id}",
        context_status="new",
    )
    # Paused here, between admission and residency: the revocation commits.
    await act(api.store, OWNER, "revoke")
    established = await api.store.residency.establish_residency(
        thread_id=thread_id,
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host=queue._HOST,
        sandbox_id=reserved["sandbox_id"],
        runtime_id=reserved["runtime_id"],
        runtime_version=reserved["runtime_version"],
        ttl_seconds=1800,
    )
    assert established is None


async def test_a_resident_member_is_not_extended_once_access_ends(api):
    job = await api.create()
    turn = await api.start(job)
    key = (job["thread_id"], turn["residency_generation"])
    beat = await api.store.residency.heartbeat_residencies(
        residencies=[key], ttl_seconds=1800, host=queue._HOST
    )
    assert key in beat
    await act(api.store, OWNER, "suspend")
    # Before any termination pass: the beat itself refuses.
    beat = await api.store.residency.heartbeat_residencies(
        residencies=[key], ttl_seconds=1800, host=queue._HOST
    )
    assert beat == {}


async def test_a_queued_turn_is_refused_at_claim_and_cancelled_by_the_pass(api):
    job = await api.create()
    revoked = await act(api.store, OWNER, "revoke")
    response = await api.client.post("/v1/agent/worker/claim", json=_claim_body("w"))
    # The claim reads the denial and ends the turn instead of running it.
    assert response.status_code in (200, 403), response.text
    if response.status_code == 200:
        assert response.json() is None
    assert await terminate_owner_access(
        api.store, LocalModeGrantClient(), OWNER, revoked.account.version
    )
    assert (await _job(api, job["id"]))["state"] in ("cancelled", "failed")


async def test_an_unacknowledged_terminal_cleanup_keeps_the_record_open(api):
    job = await api.create()
    turn = await api.start(job)
    api.manager.fail_suspend = True
    revoked = await act(api.store, OWNER, "revoke")
    generation = revoked.account.version
    assert not await terminate_owner_access(api.store, LocalModeGrantClient(), OWNER, generation)
    async with api.pool.acquire() as conn:
        outcome = await conn.fetchval(
            "SELECT last_outcome FROM agent_local_access_terminations WHERE account_id = $1",
            OWNER,
        )
    assert outcome == "terminals_pending"
    assert not any(call[0] == "delete" for call in api.manager.calls)
    api.manager.fail_suspend = False
    await _end_turn(api, turn)
    assert await terminate_owner_access(api.store, LocalModeGrantClient(), OWNER, generation)
    assert await api.store.local_accounts.open_terminations(limit=10) == []


async def _record_row(api) -> dict[str, Any]:
    async with api.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT revision, completed_at, last_outcome FROM agent_local_access_terminations "
            "WHERE account_id = $1",
            OWNER,
        )
    return dict(row)


async def test_a_terminal_reopened_while_a_pass_finishes_keeps_the_record_open(api, monkeypatch):
    """Pass A cleans up, pauses before closing; a late terminal reopens; A must not close it."""
    job = await api.create()
    await api.run(job)
    revoked = await act(api.store, OWNER, "revoke")
    generation = revoked.account.version

    accounts = api.store.local_accounts
    real_record = accounts.record_termination_attempt
    reached, release = asyncio.Event(), asyncio.Event()

    async def paused_record(**kwargs: Any) -> bool:
        reached.set()
        await release.wait()
        return await real_record(**kwargs)

    monkeypatch.setattr(accounts, "record_termination_attempt", paused_record)
    pass_a = asyncio.create_task(
        terminate_owner_access(api.store, LocalModeGrantClient(), OWNER, generation)
    )
    await asyncio.wait_for(reached.wait(), 10)
    # A has frozen, killed and released the group's terminals and found no turn.
    assert api.manager.terminals == []
    assert ("resume", job["thread_id"], terminal_freeze_token(OWNER, generation)) in (
        api.manager.calls
    )

    # B: a request admitted before the revocation opens a terminal now, and
    # its own kill fails.
    api.manager.terminals.append("late")
    api.manager.fail_delete = True
    with pytest.raises(HTTPException) as refused:
        async with terminal_open_fence(api.store, OWNER, api.manager, job["thread_id"]) as opened:
            opened["terminal"] = {"id": "late"}
    assert refused.value.status_code == 403
    assert (await _record_row(api))["revision"] == 2

    release.set()
    assert await asyncio.wait_for(pass_a, 10) is False
    row = await _record_row(api)
    assert row["completed_at"] is None and row["last_outcome"] == "reopened"
    with pytest.raises(AccountTransitionRefused) as blocked:
        await act(api.store, OWNER, "approve")
    assert blocked.value.code == "access_cleanup_pending"

    # The next pass sees the open record, kills the late terminal and closes it.
    monkeypatch.setattr(accounts, "record_termination_attempt", real_record)
    api.manager.fail_delete = False
    assert await enforce_ended_access(api.store, LocalModeGrantClient()) == 1
    assert api.manager.terminals == []
    assert ("delete", "late") in api.manager.calls
    assert (await _record_row(api))["completed_at"] is not None
    assert (await act(api.store, OWNER, "approve")).account.admission_status == "approved"


async def test_a_pass_in_flight_excludes_other_passes_and_approval(api):
    """Hold pass A inside a host call: nothing else may run, and approval cannot commit."""
    first = await api.create()
    await api.run(first)
    revoked = await act(api.store, OWNER, "revoke")
    generation = revoked.account.version
    api.manager.hold = asyncio.Event()
    pass_a = asyncio.create_task(
        terminate_owner_access(api.store, LocalModeGrantClient(), OWNER, generation)
    )
    await asyncio.wait_for(api.manager.entered.wait(), 10)
    suspends = sum(call[0] == "suspend" for call in api.manager.calls)

    # Another pass, from the record or from the sweep, finds the lock taken.
    for other in (generation, None):
        assert (
            await asyncio.wait_for(
                terminate_owner_access(api.store, LocalModeGrantClient(), OWNER, other), 5
            )
            is False
        )
    assert sum(call[0] == "suspend" for call in api.manager.calls) == suspends
    # Approval cannot commit while A is in flight, even once the record looks done.
    with pytest.raises(AccountTransitionRefused) as refused:
        await act(api.store, OWNER, "approve")
    assert refused.value.code == "access_cleanup_pending"
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_local_access_terminations SET completed_at = NOW() WHERE account_id = $1",
            OWNER,
        )
    with pytest.raises(AccountTransitionRefused) as refused:
        await act(api.store, OWNER, "approve")
    assert refused.value.code == "access_cleanup_pending"
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_local_access_terminations SET completed_at = NULL WHERE account_id = $1",
            OWNER,
        )

    api.manager.hold.set()
    assert await asyncio.wait_for(pass_a, 10) is True
    assert api.manager.terminals == []
    await act(api.store, OWNER, "approve")

    # New work under the new approval: a running turn with its residency.
    second = await api.create()
    turn = await api.start(second)
    suspends = sum(call[0] == "suspend" for call in api.manager.calls)
    # The old generation's pass runs late and must touch none of it.
    assert await terminate_owner_access(api.store, LocalModeGrantClient(), OWNER, generation)
    assert await _job(api, second["id"]) == {"state": "running", "cancel_requested": False}
    assert await _residency(api, turn["thread_id"]) != "none"
    assert sum(call[0] == "suspend" for call in api.manager.calls) == suspends


async def _job_row(api, job_id: str) -> dict[str, Any]:
    async with api.pool.acquire() as conn:
        return dict(await conn.fetchrow("SELECT * FROM agent_jobs WHERE id = $1", job_id))


async def test_revocation_closes_a_member_running_on_the_owner_connection(api):
    """The member's process holds the owner's provider key; revocation takes it away."""
    job = await api.create()
    turn = await api.start(job)
    row = await _job_row(api, job["id"])
    assert (row["model"], row["model_connection_id"]) == (MODEL, api.connection["id"])
    residency = {"thread_id": job["thread_id"], "generation": turn["residency_generation"]}
    access = await connection_residency_credential(api.store, row, row=residency)
    assert access.api_key == TEST_API_KEY and access.connection_id == api.connection["id"]

    revoked = await act(api.store, OWNER, "revoke")
    # From the commit, before any pass: the key is bound to no residency again.
    with pytest.raises(ModelConnectionUnusable) as unusable:
        await connection_residency_credential(api.store, row, row=residency)
    assert unusable.value.reason == "changed"
    key = (job["thread_id"], turn["residency_generation"])
    assert (
        await api.store.residency.heartbeat_residencies(
            residencies=[key], ttl_seconds=1800, host=queue._HOST
        )
        == {}
    )

    generation = revoked.account.version
    assert not await terminate_owner_access(api.store, LocalModeGrantClient(), OWNER, generation)
    assert await _residency(api, job["thread_id"]) == "none"
    assert ("residency_lost", job["thread_id"]) in api.manager.calls
    assert await _job(api, job["id"]) == {"state": "running", "cancel_requested": True}

    await _end_turn(api, turn)
    assert await enforce_ended_access(api.store, LocalModeGrantClient()) == 1
    await act(api.store, OWNER, "approve")
    # Approval resumes nothing: the stopped turn is not run again on the key.
    assert (await _job(api, job["id"]))["state"] == "cancelled"
    response = await api.client.post("/v1/agent/worker/claim", json=_claim_body("w"))
    assert response.status_code == 200 and response.json() is None, response.text


async def test_a_turn_without_a_connection_is_refused_and_never_reaches_a_gateway(api):
    """A local account has no Gateway models: the source is never silently swapped."""
    response = await api.client.post(
        "/v1/agent/jobs",
        json=on_connection({"repo": queue._REPO, "task_prompt": "fix it", "model": MODEL, "base_sha": "a" * 40}),
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["type"] == "model_connection_required"

    job = await api.create()
    claim = await api.claim(job["id"])
    # The claim carries the owner's own key and no Gateway grant.
    assert claim["model_connection"]["api_key"] == TEST_API_KEY
    assert claim["model_connection"]["connection_id"] == api.connection["id"]
    assert not claim.get("sandbox_token")
