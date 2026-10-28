"""Revoking a local account stops its work: real Postgres, real claims and residencies.

The owner is a local account and the Gateway stand-in is the local one, with
the queue fixture's catalog plugged into its model seam. Each case drives the
real worker and residency paths: a revocation must stop queued and running
turns, release residencies, refuse a residency an attempt admitted before it
tries to establish afterwards, hold the waiting queue, kill the group's
terminals, and keep re-approval waiting until all of that is acknowledged —
without a later retry touching work the new approval allows.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
import tests.integration.servers.test_pending_message_queue as queue
from tests.integration.servers.test_pending_message_queue import (
    _claim_body,
    api as api,
    pool as pool,
)
from tests.support.local_accounts import LOCAL_SECRET, act, make_account

from cloud_agent.accounts import config
from cloud_agent.accounts.directory import (
    LocalModeGateway,
    LocalModeGrantClient,
    LocalOwnerDirectory,
)
from cloud_agent.api.deps import get_gateway_client
from cloud_agent.application.owner_termination import (
    enforce_ended_access,
    terminal_freeze_token,
    terminate_owner_access,
)
from cloud_agent.domain.accounts import AccountTransitionRefused
from cloud_agent.domain.execution import CANCELLED, RUNNING

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

OWNER = "01J0000000000000000000WNER"


class _Manager:
    """A sandbox manager that records what termination asks of it."""

    def __init__(self, *, fail_suspend: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_suspend = fail_suspend

    async def shared_residency_lost(self, thread_id: str, *, generation: int) -> None:
        self.calls.append(("residency_lost", thread_id))

    async def suspend_terminals(self, workspace: str, *, lease_generation: int, job_id: str):
        if self.fail_suspend:
            raise ConnectionError("host away")
        self.calls.append(("suspend", workspace, job_id))
        return {"ok": True}

    async def list_terminals(self, workspace: str) -> dict[str, Any]:
        self.calls.append(("list", workspace))
        return {"terminals": [{"id": "term-1"}, {"id": "term-2"}]}

    async def delete_terminal(self, workspace: str, terminal_id: str) -> dict[str, Any]:
        self.calls.append(("delete", terminal_id))
        return {"ok": True}

    async def resume_terminals(self, workspace: str, *, lease_generation: int, job_id: str):
        self.calls.append(("resume", workspace, job_id))
        return {"ok": True}


@pytest_asyncio.fixture(autouse=True)
async def _local_owner(api, monkeypatch):
    """Make the queue fixture's owner a local account, answered by the local directory."""
    monkeypatch.setenv(config.ENV_AUTH_MODE, "local")
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, LOCAL_SECRET)
    # The fixture's owner override reads this name on every request.
    monkeypatch.setattr(queue, "_OWNER", OWNER)
    await make_account(api.store, "owner@example.org", admission="approved", account_id=OWNER)
    local_gateway = LocalModeGateway(
        LocalOwnerDirectory(api.store.local_accounts), catalog=api.gateway
    )
    api.app.dependency_overrides[get_gateway_client] = lambda: local_gateway
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


async def test_a_delayed_pass_never_touches_work_a_new_approval_allows(api):
    revoked = await act(api.store, OWNER, "revoke")
    generation = revoked.account.version
    assert await terminate_owner_access(api.store, LocalModeGrantClient(), OWNER, generation)
    # A second pass is still running somewhere: approval waits for its lock.
    async with api.store.local_accounts.termination_lock(OWNER) as held:
        assert held
        assert not await terminate_owner_access(api.store, LocalModeGrantClient(), OWNER, None)
        with pytest.raises(AccountTransitionRefused):
            await act(api.store, OWNER, "approve")
    await act(api.store, OWNER, "approve")
    job = await api.create()
    # The delayed pass runs only now, and finds access granted again.
    assert await terminate_owner_access(api.store, LocalModeGrantClient(), OWNER, generation)
    assert await _job(api, job["id"]) == {"state": "queued", "cancel_requested": False}
