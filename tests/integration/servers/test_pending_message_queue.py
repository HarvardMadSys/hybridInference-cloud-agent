"""The owner's pending-message queue, over the real store and the real routes.

Acceptance for the queue (the v2 design, §10.1). Every case drives the HTTP
routes a browser and a runner call — send, edit, delete, continue, cancel, the
worker's claim and finish — against Postgres, with the rest of a turn's protocol
(the retained sandbox's reservation, admission and settlement) written through
the store exactly as the runner's own routes write it.

No workspace broker runs in this tier. The finish route's one call to a host's
terminal broker is answered with "no broker here", which it already treats as
nothing to coordinate; nothing the queue does depends on it.

The database is required. This tier is selected with ``-m dbtest``, so a
database that is not there is a failure to report, never a reason to skip.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

from cloud_agent.api import routes, uploads, worker
from cloud_agent.api._owner_auth import authenticate_agent_owner
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_gateway_client,
    get_grant_client,
    get_log_store,
    get_storage,
)
from cloud_agent.api.worker import verify_claiming_host
from cloud_agent.application.pending_messages import settle_queue, settle_waiting_queues
from cloud_agent.source_control_runtime import SourceControlUnavailable
from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories import pending_messages as pending_message_storage
from cloud_agent.workers.attempts import reap_expired_attempts
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_RUNTIME_PINS,
    shared_deployment_capability,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_OWNER = "user-owner"
_HOST = "runner-test"
#: The sandbox image the test host runs and attests to on every claim.
_IMAGE_DIGEST = "sha256:queue-test-image"
_HOST_CREDENTIAL = "test-credential-hash"
_REPO = "owner/name"


def _db_config() -> dict[str, Any]:
    """The per-worker test database; anything not named ``*_test_*`` is refused."""
    base = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    name = base if worker_id == "master" else f"{base}_{worker_id}"
    if "_test_" not in name:
        pytest.fail(f"SAFETY: TEST_DB_NAME='{name}' is not a test database.")
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


class _Gateway:
    """The gateway's catalog, which a case can change between two sends."""

    def __init__(self) -> None:
        self.models = ["glm-5.1", "glm-4.6"]
        self.efforts = {"glm-5.1": ["low", "medium", "high", "max"], "glm-4.6": ["low", "high"]}

    async def model_catalog(self, user_id: str) -> list[str]:
        return list(self.models)

    async def model_catalog_with_aliases(self, user_id: str) -> tuple[list[str], dict[str, str]]:
        return list(self.models), {}

    async def model_reasoning_efforts(self, user_id: str) -> dict[str, list[str]]:
        return {model: list(levels) for model, levels in self.efforts.items()}

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        return {"user_id": user_id, "allowed": True, "permissions": ["agent.use"]}


class _Grants:
    async def mint(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "grant_id": f"agr_{kwargs['attempt_id']}",
            "token": "agr.token",
            "allowed_models": list(kwargs.get("models") or []),
        }

    async def renew(self, grant_id: str) -> dict[str, Any]:
        return {"grant_id": grant_id}

    async def revoke(self, grant_id: str) -> bool:
        return True


def _claim_body(worker_id: str) -> dict[str, Any]:
    return {
        "worker_id": worker_id,
        "attestation": {
            "sandbox_image_digest": _IMAGE_DIGEST,
            "runner_capabilities": [
                SHARED_SANDBOX_CAPABILITY,
                NATIVE_FORK_AT_ANCHOR_CAPABILITY,
                shared_deployment_capability("test-deployment"),
            ],
            "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
            "helper_versions": dict(SHARED_SANDBOX_HELPER_PINS),
        },
    }


@pytest_asyncio.fixture
async def pool():
    """The per-worker database at head, emptied before and after each case."""
    config = _db_config()
    try:
        created = await asyncpg.create_pool(**config, min_size=1, max_size=8)
    except Exception as exc:  # pragma: no cover - the tier's precondition
        pytest.fail(f"PostgreSQL is required for the dbtest tier: {exc}")
    apply_migrations(config)
    async with created.acquire() as conn:
        await reset_agent_tables(conn)
    try:
        yield created
    finally:
        async with created.acquire() as conn:
            await reset_agent_tables(conn)
        await created.close()


class Api:
    """The app, its store, and the host that runs its turns."""

    def __init__(
        self, client: AsyncClient, store: Storage, pool: Any, gateway: _Gateway, app: FastAPI
    ) -> None:
        self.client = client
        self.store = store
        self.pool = pool
        self.gateway = gateway
        self.app = app
        #: The retained sandbox of each conversation, once its first turn made it.
        self._sandboxes: dict[str, dict[str, Any]] = {}

    # -- the owner's side ----------------------------------------------------

    async def create(self, prompt: str = "fix it", **fields: Any) -> dict[str, Any]:
        response = await self.client.post(
            "/v1/agent/jobs",
            # A pinned base, as a resolved branch would give: settling a
            # retained turn records the commit it worked from.
            json={
                "repo": _REPO,
                "task_prompt": prompt,
                "model": "glm-5.1",
                "base_sha": "a" * 40,
                **fields,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def send(
        self, thread_id: str, prompt: str, *, key: str | None = None, **fields: Any
    ) -> Any:
        return await self.client.post(
            f"/v1/agent/threads/{thread_id}/messages",
            json={"prompt": prompt, **fields},
            headers={"Idempotency-Key": key} if key else None,
        )

    async def edit(self, thread_id: str, message: dict[str, Any], **changes: Any) -> Any:
        return await self.client.patch(
            f"/v1/agent/threads/{thread_id}/messages/{message['id']}",
            json={"revision": message["revision"], **changes},
        )

    async def delete(self, thread_id: str, message: dict[str, Any]) -> Any:
        return await self.client.delete(
            f"/v1/agent/threads/{thread_id}/messages/{message['id']}",
            params={"revision": message["revision"]},
        )

    async def resume(self, thread_id: str) -> Any:
        return await self.client.post(f"/v1/agent/threads/{thread_id}/queue/resume")

    async def thread(self, job_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/v1/agent/jobs/{job_id}/thread")
        assert response.status_code == 200, response.text
        return response.json()

    async def turns(self, thread_id: str) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, turn_no, state, task_prompt, model, reasoning_effort, "
                "turn_source, parent_job_id, client_request_id "
                "FROM agent_jobs WHERE thread_id = $1 ORDER BY turn_no",
                thread_id,
            )
        return [dict(row) for row in rows]

    async def queue(self, thread_id: str) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT queue_paused_reason, queue_settled_job_id FROM agent_threads WHERE id = $1",
                thread_id,
            )
            waiting = await conn.fetch(
                "SELECT id, state, prompt, revision, last_error FROM agent_pending_messages "
                "WHERE thread_id = $1 ORDER BY position",
                thread_id,
            )
        return {**dict(row), "messages": [dict(message) for message in waiting]}

    async def fork(self, source_job: dict[str, Any]) -> dict[str, Any]:
        """Materialize a native sibling in the source's retained sandbox."""
        source_sandbox = await self.store.sandbox.thread_sandbox(thread_id=source_job["thread_id"])
        assert source_sandbox is not None
        operation = await self.store.session_fork.prepare_session_fork(
            source_job_id=source_job["id"],
            user_id=_OWNER,
            idempotency_key=f"queue-test-fork:{source_job['id']}:{len(self._sandboxes)}",
        )
        assert operation is not None and operation.get("error") is None
        coordinator = f"queue-test-coordinator:{operation['id']}"
        assert await self.store.session_fork.claim_session_fork_materialization(
            operation_id=operation["id"],
            coordinator_token=coordinator,
            lease_seconds=210,
        )
        residencies = await self.store.session_fork.establish_session_fork_residencies(
            operation_id=operation["id"],
            coordinator_token=coordinator,
            ttl_seconds=120,
        )
        assert residencies is not None
        receipt = {
            "operation_id": operation["id"],
            "sandbox_id": operation["sandbox_id"],
            "sandbox_generation": operation["sandbox_generation"],
            "container_id": operation["container_id"],
            "runtime_id": operation["runtime_id"],
            "runtime_version": operation["runtime_version"],
            "source_thread_id": operation["source_thread_id"],
            "source_native_session_id": operation["source_native_session_id"],
            "target_thread_id": operation["target_thread_id"],
            "target_native_session_id": f"native-{operation['target_thread_id']}",
            "target_resident_slot_id": operation["target_resident_slot_id"],
            "source_residency_generation": residencies["source"]["generation"],
            "target_residency_generation": residencies["target"]["generation"],
            "source_process_generation": source_sandbox["process_generation"] + 1,
            "target_process_generation": 1,
        }
        materialized = await self.store.session_fork.record_session_fork_materialized(
            operation_id=operation["id"],
            coordinator_token=coordinator,
            receipt=receipt,
        )
        assert materialized is not None and materialized["state"] == "materialized"
        child = await self.store.session_fork.finalize_session_fork(operation_id=operation["id"])
        assert child is not None
        child_sandbox = await self.store.sandbox.thread_sandbox(thread_id=child["thread_id"])
        assert child_sandbox is not None and child_sandbox["native_state"] == "ready"
        self._sandboxes[child["thread_id"]] = child_sandbox
        return child

    # -- the host's side -----------------------------------------------------

    async def claim(self, expected_job_id: str) -> dict[str, Any]:
        response = await self.client.post("/v1/agent/worker/claim", json=_claim_body("w"))
        assert response.status_code == 200, response.text
        claim = response.json()
        assert claim is not None and claim["job_id"] == expected_job_id, claim
        async with self.pool.acquire() as conn:
            claim["lease_generation"] = await conn.fetchval(
                "SELECT lease_generation FROM agent_attempts WHERE id = $1", claim["attempt_id"]
            )
        return claim

    async def start(self, job: dict[str, Any]) -> dict[str, Any]:
        """Claim a turn and deliver its prompt, as the runner does before the CLI runs."""
        claim = await self.claim(job["id"])
        thread_id = claim["thread_id"]
        known = self._sandboxes.get(thread_id)
        reserved = await self.store.sandbox.reserve_thread_sandbox(
            thread_id=thread_id,
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            host=_HOST,
            sandbox_id=known["sandbox_id"] if known else f"asbx_{thread_id}",
            container_name=known["container_name"] if known else f"chat-{thread_id}",
            runtime_id="claude-code",
            runtime_version=claim.get("runtime_version"),
            # The runner reserves with its own image, the one it attested.
            sandbox_image_digest=_IMAGE_DIGEST,
            execution_profile=claim["execution_profile"],
        )
        assert reserved is not None
        self._sandboxes[thread_id] = reserved
        fence = {
            "thread_id": thread_id,
            "attempt_id": claim["attempt_id"],
            "lease_generation": claim["lease_generation"],
            "sandbox_generation": reserved["generation"],
            "activation_generation": reserved["activation_generation"],
        }
        assert await self.store.sandbox.mark_thread_sandbox_running(
            **fence,
            container_id=reserved.get("container_id") or f"container-{thread_id}",
            context_status="live" if known else "new",
        )
        residency = await self.store.residency.establish_residency(
            thread_id=thread_id,
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            host=_HOST,
            sandbox_id=reserved["sandbox_id"],
            runtime_id=reserved["runtime_id"],
            runtime_version=reserved["runtime_version"],
            ttl_seconds=1800,
        )
        assert residency is not None
        assert await self.store.sandbox.admit_thread_sandbox_turn(
            **fence,
            residency_generation=residency["generation"],
            job_id=job["id"],
        )
        return {**claim, "fence": fence, "residency_generation": residency["generation"]}

    async def finish(self, turn: dict[str, Any], state: str = "succeeded") -> None:
        """End a started turn through the worker's finish route.

        The native context is settled first, as the runner does for a turn that
        completed, was stopped, or failed after the CLI answered.
        """
        assert await self.store.sandbox.settle_thread_sandbox(
            **turn["fence"],
            residency_generation=turn["residency_generation"],
            job_id=turn["job_id"],
            context_status="live",
            native_session_id=f"native-{turn['thread_id']}",
            idle_timeout_seconds=1800,
            base_sha=turn.get("base_sha"),
        )
        response = await self.client.post(
            f"/v1/agent/worker/jobs/{turn['job_id']}/finish",
            json={"state": state, "detail": None if state == "succeeded" else f"ended {state}"},
            headers={"Authorization": f"Bearer {turn['worker_token']}"},
        )
        assert response.status_code == 200, response.text

    async def run(self, job: dict[str, Any], state: str = "succeeded") -> None:
        await self.finish(await self.start(job), state)


@pytest_asyncio.fixture
async def api(pool, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "queue-test-secret")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "queue-broker-secret")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", _REPO)
    store = Storage(pool)
    await store.host_pool.register_host(
        host=_HOST,
        credential_hash=_HOST_CREDENTIAL,
        capabilities=[SHARED_SANDBOX_CAPABILITY],
        slots_total=4,
    )
    await store.host_pool.record_host_heartbeat(
        host=_HOST,
        capabilities=[SHARED_SANDBOX_CAPABILITY, NATIVE_FORK_AT_ANCHOR_CAPABILITY],
        sandbox_image_digest=_IMAGE_DIGEST,
    )

    async def no_broker(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(worker, "_worker_workspace_broker", no_broker)
    gateway = _Gateway()
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(uploads.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    app.dependency_overrides[get_grant_client] = _Grants
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    app.dependency_overrides[get_log_store] = lambda: None
    app.dependency_overrides[authenticate_agent_owner] = lambda: {
        "user_id": _OWNER,
        "role": "free",
        "authenticated": True,
    }
    app.dependency_overrides[verify_claiming_host] = lambda: {
        "host": _HOST,
        "credential_hash": _HOST_CREDENTIAL,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield Api(client, store, pool, gateway, app)


# -- 10.1: a held message holds the line, FIFO resumes after save --------------


async def test_an_edited_message_holds_the_line_until_it_is_saved(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    first = await api.start(job)

    # Two messages while the turn runs: both wait, neither is a turn yet.
    sent_a = await api.send(thread_id, "A: add a regression test")
    sent_b = await api.send(thread_id, "B: then update the changelog")
    assert sent_a.status_code == sent_b.status_code == 201
    assert sent_a.json()["job"] is None and sent_b.json()["job"] is None
    a, b = sent_a.json()["pending"], sent_b.json()["pending"]
    assert a["position"] < b["position"]

    # The owner opens A for editing: it is held.
    held = await api.edit(thread_id, a, state="held")
    assert held.status_code == 200, held.text
    a = held.json()["pending"]
    assert (a["state"], a["revision"]) == ("held", 2)

    # The running turn ends. The queue does not go past the held message.
    await api.finish(first)
    turns = await api.turns(thread_id)
    assert [turn["task_prompt"] for turn in turns] == ["fix it"]
    queue = await api.queue(thread_id)
    assert [(row["id"], row["state"]) for row in queue["messages"]] == [
        (a["id"], "held"),
        (b["id"], "queued"),
    ]
    assert queue["queue_paused_reason"] is None

    # Saving A puts it back in line, and — nothing running — it is sent now.
    saved = await api.edit(thread_id, a, prompt="A: add a regression test first", state="queued")
    assert saved.status_code == 200, saved.text
    assert saved.json()["pending"] is None
    a_job = saved.json()["job"]
    assert a_job["task_prompt"] == "A: add a regression test first"
    assert a_job["turn_no"] == 2
    queue = await api.queue(thread_id)
    assert [row["id"] for row in queue["messages"]] == [b["id"]]

    # A's turn ends; B is sent by the finish, without anyone asking.
    await api.run(a_job)
    turns = await api.turns(thread_id)
    assert [turn["task_prompt"] for turn in turns] == [
        "fix it",
        "A: add a regression test first",
        "B: then update the changelog",
    ]
    assert turns[2]["parent_job_id"] == a_job["id"]
    assert turns[2]["state"] == "queued"
    assert (await api.queue(thread_id))["messages"] == []
    # The transcript shows each message once, in the turn it became.
    transcript = [
        (message["role"], message["content"])
        for message in (await api.thread(job["id"]))["messages"]
    ]
    assert [content for role, content in transcript if role == "user"] == [
        "fix it",
        "A: add a regression test first",
        "B: then update the changelog",
    ]


# -- 10.1: two tabs, one stale revision -----------------------------------------


async def test_two_tabs_editing_one_message_one_wins_and_the_other_sees_it(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.start(job)
    message = (await api.send(thread_id, "original")).json()["pending"]

    # Both tabs read revision 1.
    first_tab = await api.edit(thread_id, message, prompt="from the first tab")
    second_tab = await api.edit(thread_id, message, prompt="from the second tab")

    assert first_tab.status_code == 200, first_tab.text
    assert first_tab.json()["pending"]["revision"] == 2
    assert second_tab.status_code == 409
    error = second_tab.json()["detail"]["error"]
    assert error["type"] == "revision_conflict"
    assert error["current"]["prompt"] == "from the first tab"
    assert error["current"]["revision"] == 2
    # A delete from the stale tab is refused the same way, and changes nothing.
    stale_delete = await api.delete(thread_id, message)
    assert stale_delete.status_code == 409
    assert stale_delete.json()["detail"]["error"]["current"]["revision"] == 2
    queue = await api.queue(thread_id)
    assert [(row["prompt"], row["revision"]) for row in queue["messages"]] == [
        ("from the first tab", 2)
    ]


# -- 10.1: two replicas settling at once ----------------------------------------


async def test_two_replicas_settling_at_once_create_one_turn(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    first = await api.start(job)
    await api.send(thread_id, "only once, please")
    # The turn ends without the finish route's own settlement, so the two
    # replicas below are the first to see it.
    assert await api.store.sandbox.settle_thread_sandbox(
        **first["fence"],
        residency_generation=first["residency_generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id=f"native-{thread_id}",
        idle_timeout_seconds=1800,
    )
    assert await api.store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    config = _db_config()
    pools = [await asyncpg.create_pool(**config, min_size=1, max_size=4) for _ in range(2)]
    try:
        replicas = [Storage(replica) for replica in pools]
        outcomes = await asyncio.gather(
            *(
                settle_queue(
                    replica,
                    thread_id=thread_id,
                    gateway=api.gateway,
                    app_credentials=None,
                )
                for replica in replicas
                for _ in range(4)
            )
        )
    finally:
        for replica in pools:
            await replica.close()

    assert sum(outcome.job is not None for outcome in outcomes) == 1
    turns = await api.turns(thread_id)
    assert [turn["task_prompt"] for turn in turns] == ["fix it", "only once, please"]
    assert (await api.queue(thread_id))["messages"] == []


# -- 10.1: Stop through the real cancel route -----------------------------------


async def test_stop_on_a_running_turn_pauses_the_queue_and_continue_sends_it(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    running = await api.start(job)
    await api.send(thread_id, "after the stop")

    stopped = await api.client.post(f"/v1/agent/jobs/{job['id']}/cancel")

    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["cancel_requested"] is True
    # Paused in the cancel's own commit, before the turn has even ended.
    assert (await api.queue(thread_id))["queue_paused_reason"] == "stopped"
    assert (await api.thread(job["id"]))["queue_paused_reason"] == "stopped"

    # The runner ends the turn cancelled; its finish settles, and sends nothing.
    await api.finish(running, "cancelled")
    assert [turn["state"] for turn in await api.turns(thread_id)] == ["cancelled"]
    queue = await api.queue(thread_id)
    assert queue["queue_paused_reason"] == "stopped"
    assert queue["queue_settled_job_id"] == job["id"]
    assert [row["state"] for row in queue["messages"]] == ["queued"]

    resumed = await api.resume(thread_id)

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["queue_paused_reason"] is None
    sent = resumed.json()["job"]
    assert sent["task_prompt"] == "after the stop"
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == [
        "fix it",
        "after the stop",
    ]
    assert (await api.queue(thread_id))["messages"] == []


async def test_stop_on_a_queued_turn_settles_at_once_without_sending(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    # Idle: this message is the next turn at once, and it has not started.
    second = (await api.send(thread_id, "second")).json()["job"]
    assert second["state"] == "queued"
    await api.send(thread_id, "third, waiting behind the second")

    stopped = await api.client.post(f"/v1/agent/jobs/{second['id']}/cancel")

    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "cancelled"
    # The cancel route settled right after the cancel committed: the ending is
    # recorded, the queue is paused, and no turn was created for the third.
    queue = await api.queue(thread_id)
    assert queue["queue_paused_reason"] == "stopped"
    assert queue["queue_settled_job_id"] == second["id"]
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it", "second"]

    resumed = await api.resume(thread_id)

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["job"]["task_prompt"] == "third, waiting behind the second"
    assert [turn["state"] for turn in await api.turns(thread_id)] == [
        "succeeded",
        "cancelled",
        "queued",
    ]


# -- 10.1: a failed turn, and a turn the reaper fails -----------------------------


async def test_a_failed_turn_pauses_the_queue(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    running = await api.start(job)
    await api.send(thread_id, "waits for a decision")

    await api.finish(running, "failed")

    queue = await api.queue(thread_id)
    assert queue["queue_paused_reason"] == "failed"
    assert [row["state"] for row in queue["messages"]] == ["queued"]
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it"]


async def test_a_turn_the_reaper_fails_is_paused_by_the_periodic_sweep(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    claim = await api.claim(job["id"])
    await api.send(thread_id, "waits for a decision")
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            claim["attempt_id"],
        )

    # The reaper's verdict reaches no finish route.
    actions = await api.store.attempt_commands.reap_expired(max_attempts=1)
    assert [action["action"] for action in actions] == ["failed"]
    assert (await api.queue(thread_id))["queue_paused_reason"] is None

    settled = await settle_waiting_queues(api.store, gateway=api.gateway, github_app=None)

    assert settled == 1
    queue = await api.queue(thread_id)
    assert queue["queue_paused_reason"] == "failed"
    assert queue["queue_settled_job_id"] == job["id"]
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it"]
    # Nothing is left for the next sweep to do with this thread.
    assert await api.store.pending_messages.threads_to_settle() == []


async def test_the_reaper_loop_runs_queue_and_staged_attachment_sweeps(api: Api, monkeypatch):
    """The sweep is wired into the loop that runs in production, not only callable."""
    claimed_upload = await api.store.attachments.create_attachment(
        user_id=_OWNER, path="claimed.txt", content=b"claimed", content_type="text/plain"
    )
    job = await api.create(attachment_ids=[claimed_upload["id"]])
    thread_id = job["thread_id"]
    claim = await api.claim(job["id"])
    pending_upload = await api.store.attachments.create_attachment(
        user_id=_OWNER, path="pending.txt", content=b"pending", content_type="text/plain"
    )
    sent = await api.send(thread_id, "waits for a decision", attachment_ids=[pending_upload["id"]])
    assert sent.status_code == 201, sent.text
    pending_message = sent.json()["pending"]
    abandoned_upload = await api.store.attachments.create_attachment(
        user_id=_OWNER, path="abandoned.txt", content=b"abandoned", content_type="text/plain"
    )
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            claim["attempt_id"],
        )
        await conn.execute(
            "UPDATE agent_job_attachments SET created_at = NOW() - INTERVAL '2 hours'"
        )

    # Prove the loop reads the saved-setting seam rather than baking in the
    # 24-hour default: these two-hour-old rows are eligible at one hour.
    monkeypatch.setattr(
        "cloud_agent.workers.attempts.integer_setting",
        lambda key, fallback: 1 if key == "attachments.staged_retention_hours" else fallback,
    )

    loop = asyncio.create_task(
        reap_expired_attempts(api.store, interval_seconds=0.05, max_attempts=1, gateway=api.gateway)
    )
    try:
        # Generous: one loop pass also reaps hosts, residencies and goals, and
        # a slow disk can make that pass take seconds.
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            if (await api.queue(thread_id))["queue_paused_reason"] == "failed":
                break
            await asyncio.sleep(0.05)
    finally:
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop

    assert (await api.queue(thread_id))["queue_paused_reason"] == "failed"
    assert [turn["state"] for turn in await api.turns(thread_id)] == ["failed"]
    async with api.pool.acquire() as conn:
        attachments = await conn.fetch(
            "SELECT id, job_id, pending_message_id FROM agent_job_attachments ORDER BY id"
        )
    assert [dict(row) for row in attachments] == [
        {
            "id": claimed_upload["id"],
            "job_id": job["id"],
            "pending_message_id": None,
        },
        {
            "id": pending_upload["id"],
            "job_id": None,
            "pending_message_id": pending_message["id"],
        },
    ]
    assert abandoned_upload["id"] not in {row["id"] for row in attachments}


async def test_a_github_app_that_cannot_load_holds_a_message_back_without_refusing_it(api: Api):
    """Issue #47 on the queue: an outage is not a verdict on the owner's message.

    Without the App the repository check would see only the allowlist, so the
    finish records the ending but sends nothing, and holds nothing either. The
    sweep sends the message once the App loads.
    """

    class RecoveringApp:
        loaded = False

        def get(self) -> object:
            if not self.loaded:
                raise SourceControlUnavailable("github")
            return object()  # the repository is allowlisted; the minter is not asked

    app = RecoveringApp()
    api.app.state.source_control = SimpleNamespace(github=app)
    job = await api.create()
    thread_id = job["thread_id"]
    running = await api.start(job)
    await api.send(thread_id, "next step")

    await api.finish(running)

    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it"]
    queue = await api.queue(thread_id)
    assert [(row["state"], row["last_error"]) for row in queue["messages"]] == [("queued", None)]
    assert (queue["queue_paused_reason"], queue["queue_settled_job_id"]) == (None, job["id"])
    assert thread_id in await api.store.pending_messages.threads_to_settle()
    await settle_waiting_queues(api.store, gateway=api.gateway, github_app=app)
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it"]

    app.loaded = True
    await settle_waiting_queues(api.store, gateway=api.gateway, github_app=app)

    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it", "next step"]
    assert (await api.queue(thread_id))["messages"] == []


# -- 10.1: idle send, idempotent resends ------------------------------------------


async def test_an_idle_send_returns_its_turn_and_a_resend_returns_the_same_one(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)

    first = await api.send(thread_id, "next step", key="turn-2")

    assert first.status_code == 201, first.text
    assert first.json()["pending"] is None
    made = first.json()["job"]
    assert (made["turn_no"], made["task_prompt"]) == (2, "next step")
    # The response was lost and the browser sends again: the same turn.
    again = await api.send(thread_id, "next step", key="turn-2")
    assert again.status_code == 201, again.text
    assert again.json()["job"]["id"] == made["id"]
    assert len(await api.turns(thread_id)) == 2
    # The same key for different content is a different submission.
    conflict = await api.send(thread_id, "something else", key="turn-2")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"]["type"] == "idempotency_conflict"
    assert len(await api.turns(thread_id)) == 2


async def test_a_resend_finds_the_message_while_it_waits_and_its_turn_after(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    running = await api.start(job)

    first = await api.send(thread_id, "queued once", key="queued-key")
    waiting = first.json()["pending"]
    again = await api.send(thread_id, "queued once", key="queued-key")

    assert again.json()["pending"]["id"] == waiting["id"]
    assert len((await api.queue(thread_id))["messages"]) == 1

    # The message becomes a turn; its key goes with it.
    await api.finish(running)
    after = await api.send(thread_id, "queued once", key="queued-key")
    assert after.status_code == 201, after.text
    assert after.json()["pending"] is None
    assert after.json()["job"]["task_prompt"] == "queued once"
    turns = await api.turns(thread_id)
    assert [turn["task_prompt"] for turn in turns] == ["fix it", "queued once"]
    assert turns[1]["client_request_id"] == "queued-key"
    assert (await api.queue(thread_id))["messages"] == []


async def _publish_workspace(api: Api, thread_id: str) -> None:
    """Complete the latest snapshot without a publisher in this queue-only tier."""
    async with api.pool.acquire() as conn:
        snapshot_id = await conn.fetchval(
            "UPDATE agent_sandbox_snapshots SET state = 'published', "
            "published_commit_sha = $2, published_at = NOW() WHERE id = ("
            "SELECT snapshot.id FROM agent_sandbox_snapshots snapshot "
            "JOIN agent_thread_sandbox_memberships membership "
            "ON membership.sandbox_id = snapshot.sandbox_id "
            "WHERE membership.thread_id = $1 "
            "ORDER BY snapshot.workspace_epoch DESC, snapshot.id DESC LIMIT 1) RETURNING id",
            thread_id,
            "b" * 40,
        )
    assert snapshot_id is not None


async def test_two_tabs_guarded_submit_creates_one_turn_under_the_thread_lock(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)

    fields = {
        "thread_id": thread_id,
        "user_id": _OWNER,
        "prompt": "Watch the PR and merge",
        "skill_ids": None,
        "attachment_ids": None,
        "model": None,
        "reasoning_effort": None,
        "client_request_id": "two-tabs-watch",
        "client_request_fingerprint": "same-guarded-request",
        "require_idle": True,
        "expected_latest_job_id": job["id"],
    }
    first, second = await asyncio.gather(
        api.store.pending_messages.submit_message(**fields),
        api.store.pending_messages.submit_message(**fields),
    )

    assert first["job_id"] == second["job_id"]
    assert (await api.queue(thread_id))["messages"] == []
    assert [turn["id"] for turn in await api.turns(thread_id)] == [job["id"], first["job_id"]]


async def test_guarded_submit_sees_a_message_committed_while_it_waits_for_the_thread_lock(
    api: Api,
):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    task = None
    try:
        async with api.pool.acquire() as writer, writer.transaction():
            writer_pid = await writer.fetchval("SELECT pg_backend_pid()")
            await writer.execute("SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE", thread_id)
            task = asyncio.create_task(
                api.store.pending_messages.submit_message(
                    thread_id=thread_id,
                    user_id=_OWNER,
                    prompt="Watch the PR and merge",
                    skill_ids=None,
                    attachment_ids=None,
                    model=None,
                    reasoning_effort=None,
                    require_idle=True,
                    expected_latest_job_id=job["id"],
                )
            )
            async with asyncio.timeout(10), api.pool.acquire() as observer:
                while not await observer.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                    writer_pid,
                ):
                    await asyncio.sleep(0.01)
            await writer.execute(
                "INSERT INTO agent_pending_messages (id, thread_id, user_id, state, prompt) "
                "VALUES ('pending-lock-race', $1, $2, 'queued', 'Ordinary message wins')",
                thread_id,
                _OWNER,
            )
            await writer.execute(
                "UPDATE agent_threads SET queue_paused_reason = 'failed', "
                "queue_settled_job_id = NULL WHERE id = $1",
                thread_id,
            )
        assert await asyncio.wait_for(task, timeout=10) == {"error": "conversation_changed"}
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    queue = await api.queue(thread_id)
    assert (queue["queue_paused_reason"], queue["queue_settled_job_id"]) == ("failed", None)
    assert [(row["id"], row["prompt"]) for row in queue["messages"]] == [
        ("pending-lock-race", "Ordinary message wins")
    ]
    assert len(await api.turns(thread_id)) == 1


async def test_guarded_submit_replays_its_turn_after_the_chat_changes(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    fields = {"require_idle": True, "expected_latest_job_id": job["id"]}

    first = await api.send(thread_id, "Watch the PR and merge", key="watch-once", **fields)
    assert first.status_code == 201, first.text
    made = first.json()["job"]
    assert made["task_prompt"] == "Watch the PR and merge"
    running = await api.start(made)
    await api.send(thread_id, "another ordinary message")
    queue_before = await api.queue(thread_id)

    again = await api.send(thread_id, "Watch the PR and merge", key="watch-once", **fields)

    assert again.status_code == 201, again.text
    assert again.json()["job"]["id"] == made["id"]
    assert await api.queue(thread_id) == queue_before
    assert len(await api.turns(thread_id)) == 2
    async with api.pool.acquire() as conn:
        fingerprint = await conn.fetchval(
            "SELECT client_request_fingerprint FROM agent_jobs WHERE id = $1", made["id"]
        )
    # The locked write must replay too: concurrent requests can both pass the
    # route's earlier replay read before either has committed its submission.
    replayed_at_write = await api.store.pending_messages.submit_message(
        thread_id=thread_id,
        user_id=_OWNER,
        prompt="Watch the PR and merge",
        skill_ids=None,
        attachment_ids=None,
        model=None,
        reasoning_effort=None,
        client_request_id="watch-once",
        client_request_fingerprint=fingerprint,
        **fields,
    )
    assert replayed_at_write == {"job_id": made["id"]}
    assert await api.queue(thread_id) == queue_before
    await api.finish(running)


async def test_guarded_submit_refuses_an_ordinary_message_winning_after_the_queue_read(
    api: Api, monkeypatch
):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    before_locked_write = asyncio.Event()
    ordinary_won = asyncio.Event()
    original = api.store.pending_messages.submit_message

    async def intercept(**fields: Any) -> dict[str, Any]:
        if fields["require_idle"]:
            before_locked_write.set()
            await ordinary_won.wait()
        return await original(**fields)

    monkeypatch.setattr(api.store.pending_messages, "submit_message", intercept)
    watch = asyncio.create_task(
        api.send(
            thread_id,
            "Watch the PR and merge",
            key="racing-watch",
            require_idle=True,
            expected_latest_job_id=job["id"],
        )
    )
    try:
        await asyncio.wait_for(before_locked_write.wait(), timeout=10)
        ordinary = await api.send(thread_id, "ordinary message wins")
        assert ordinary.status_code == 201, ordinary.text
        ordinary_won.set()
        refused = await asyncio.wait_for(watch, timeout=10)
    finally:
        ordinary_won.set()
        if not watch.done():
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == [
        "fix it",
        "ordinary message wins",
    ]
    assert (await api.queue(thread_id))["messages"] == []


@pytest.mark.parametrize("state", ["queued", "held", "sending"])
async def test_guarded_submit_preserves_every_waiting_message_and_a_paused_queue(
    api: Api, state: str
):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    waiting = await api.store.pending_messages.submit_message(
        thread_id=thread_id,
        user_id=_OWNER,
        prompt="unrelated waiting message",
        skill_ids=None,
        attachment_ids=None,
        model=None,
        reasoning_effort=None,
    )
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_pending_messages SET state = $2 WHERE id = $1",
            waiting["message"]["id"],
            state,
        )
        await conn.execute(
            "UPDATE agent_threads SET queue_paused_reason = 'failed', "
            "queue_settled_job_id = NULL WHERE id = $1",
            thread_id,
        )
    before = await api.queue(thread_id)

    refused = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=job["id"],
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert await api.queue(thread_id) == before
    assert len(await api.turns(thread_id)) == 1


async def test_guarded_submit_refuses_a_newer_completed_turn(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    ordinary = (await api.send(thread_id, "another completed turn")).json()["job"]
    await api.run(ordinary)
    await _publish_workspace(api, thread_id)

    refused = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=job["id"],
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert len(await api.turns(thread_id)) == 2
    assert (await api.queue(thread_id))["messages"] == []


@pytest.mark.parametrize("state", ["queued", "running"])
async def test_guarded_submit_refuses_any_live_turn_even_when_its_id_matches(api: Api, state: str):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    ordinary = (await api.send(thread_id, "ordinary live turn")).json()["job"]
    async with api.pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = $2 WHERE id = $1", ordinary["id"], state)

    refused = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=ordinary["id"],
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert len(await api.turns(thread_id)) == 2
    assert (await api.queue(thread_id))["messages"] == []


@pytest.mark.parametrize("state", ["pending", "capturing", "captured", "publishing", "failed"])
async def test_guarded_submit_waits_for_the_current_workspace_publication(api: Api, state: str):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    async with api.pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandbox_snapshots SET state = $1", state)

    refused = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=job["id"],
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert len(await api.turns(thread_id)) == 1
    assert (await api.queue(thread_id))["messages"] == []
    # Normal messages retain their existing ability to start before publication.
    ordinary = await api.send(thread_id, "ordinary follow-up")
    assert ordinary.status_code == 201, ordinary.text
    assert ordinary.json()["job"]["task_prompt"] == "ordinary follow-up"


async def test_guarded_submit_refuses_a_newer_unpublished_workspace_after_an_older_publication(
    api: Api,
):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    ordinary = (await api.send(thread_id, "new changes to publish")).json()["job"]
    await api.run(ordinary)
    async with api.pool.acquire() as conn:
        assert [
            row["state"]
            for row in await conn.fetch(
                "SELECT state FROM agent_sandbox_snapshots ORDER BY workspace_epoch"
            )
        ] == ["published", "pending"]

    refused = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=ordinary["id"],
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert len(await api.turns(thread_id)) == 2
    assert (await api.queue(thread_id))["messages"] == []


@pytest.mark.parametrize("missing", ["snapshot", "membership"])
async def test_guarded_submit_requires_a_known_published_workspace(api: Api, missing: str):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    async with api.pool.acquire() as conn:
        if missing == "snapshot":
            await conn.execute("DELETE FROM agent_sandbox_snapshots")
        else:
            await conn.execute(
                "DELETE FROM agent_thread_sandbox_memberships WHERE thread_id = $1", thread_id
            )

    refused = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=job["id"],
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert len(await api.turns(thread_id)) == 1
    assert (await api.queue(thread_id))["messages"] == []


async def test_guarded_submit_creates_its_turn_atomically_with_idle_native_siblings(
    api: Api, monkeypatch
):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    sibling = await api.fork(job)
    settlement = AsyncMock()
    monkeypatch.setattr(api.store.pending_messages, "settle_queue", settlement)

    accepted = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=job["id"],
    )

    assert accepted.status_code == 201, accepted.text
    made = accepted.json()["job"]
    assert made["task_prompt"] == "Watch the PR and merge"
    assert accepted.json()["pending"] is None
    settlement.assert_not_awaited()
    assert (await api.queue(thread_id))["messages"] == []
    assert len(await api.turns(thread_id)) == 2
    assert len(await api.turns(sibling["thread_id"])) == 1


@pytest.mark.parametrize("state", ["queued", "running"])
async def test_guarded_submit_refuses_a_native_sibling_with_a_live_turn(api: Api, state: str):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    sibling = await api.fork(job)
    ordinary = (await api.send(sibling["thread_id"], "sibling changes")).json()["job"]
    if state == "running":
        await api.start(ordinary)
    before = await api.queue(thread_id)

    refused = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=job["id"],
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert "sharing this workspace" in refused.json()["detail"]["error"]["message"]
    assert await api.queue(thread_id) == before
    assert len(await api.turns(thread_id)) == 1
    assert len(await api.turns(sibling["thread_id"])) == 2


@pytest.mark.parametrize("state", ["queued", "held", "sending"])
async def test_guarded_submit_refuses_a_native_siblings_waiting_messages(api: Api, state: str):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    sibling = await api.fork(job)
    message = await api.store.pending_messages.submit_message(
        thread_id=sibling["thread_id"],
        user_id=_OWNER,
        prompt="sibling's waiting changes",
        skill_ids=None,
        attachment_ids=None,
        model=None,
        reasoning_effort=None,
    )
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_pending_messages SET state = $2 WHERE id = $1",
            message["message"]["id"],
            state,
        )
    own_before = await api.queue(thread_id)
    sibling_before = await api.queue(sibling["thread_id"])

    refused = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=job["id"],
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert await api.queue(thread_id) == own_before
    assert await api.queue(sibling["thread_id"]) == sibling_before
    assert len(await api.turns(thread_id)) == len(await api.turns(sibling["thread_id"])) == 1


async def test_guarded_submit_waits_for_a_siblings_active_attempt_after_its_job_ends(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    sibling = await api.fork(job)
    ordinary = (await api.send(sibling["thread_id"], "sibling finishing")).json()["job"]
    await api.start(ordinary)
    async with api.pool.acquire() as conn:
        # A terminal job alone does not say the host finished its workspace
        # mutation: the member's active-attempt fence must settle as well.
        await conn.execute("UPDATE agent_jobs SET state = 'failed' WHERE id = $1", ordinary["id"])

    refused = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=job["id"],
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert (await api.queue(thread_id))["messages"] == []
    assert len(await api.turns(thread_id)) == 1


@pytest.mark.parametrize("operation", ["fork", "group_operation"])
async def test_guarded_submit_refuses_live_shared_workspace_operations(api: Api, operation: str):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    if operation == "fork":
        prepared = await api.store.session_fork.prepare_session_fork(
            source_job_id=job["id"], user_id=_OWNER, idempotency_key="fork-before-watch"
        )
        assert prepared is not None and prepared.get("error") is None
    else:
        async with api.pool.acquire() as conn:
            await conn.execute("UPDATE agent_sandboxes SET active_operation_count = 1")
    before = await api.queue(thread_id)

    refused = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=job["id"],
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert await api.queue(thread_id) == before
    assert len(await api.turns(thread_id)) == 1


async def test_guarded_submit_ignores_live_work_in_an_unrelated_sandbox(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    unrelated = await api.create("unrelated work")
    await api.start(unrelated)

    accepted = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=job["id"],
    )

    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["job"]["task_prompt"] == "Watch the PR and merge"
    assert (await api.queue(thread_id))["messages"] == []
    assert len(await api.turns(thread_id)) == 2
    assert len(await api.turns(unrelated["thread_id"])) == 1


async def test_guarded_submit_replays_before_group_checks_when_a_sibling_becomes_busy(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    sibling = await api.fork(job)
    guard = {"require_idle": True, "expected_latest_job_id": job["id"]}
    first = await api.send(thread_id, "Watch the PR and merge", key="group-watch-once", **guard)
    assert first.status_code == 201, first.text
    made = first.json()["job"]
    ordinary = await api.send(sibling["thread_id"], "sibling's next turn")
    assert ordinary.status_code == 201, ordinary.text
    async with api.pool.acquire() as conn:
        fingerprint = await conn.fetchval(
            "SELECT client_request_fingerprint FROM agent_jobs WHERE id = $1", made["id"]
        )

    replayed = await api.store.pending_messages.submit_message(
        thread_id=thread_id,
        user_id=_OWNER,
        prompt="Watch the PR and merge",
        skill_ids=None,
        attachment_ids=None,
        model=None,
        reasoning_effort=None,
        client_request_id="group-watch-once",
        client_request_fingerprint=fingerprint,
        **guard,
    )

    assert replayed == {"job_id": made["id"]}
    assert (await api.queue(thread_id))["messages"] == []
    assert len(await api.turns(thread_id)) == len(await api.turns(sibling["thread_id"])) == 2


async def test_guarded_submit_waits_for_actual_sibling_admission_at_the_group_lock(
    api: Api, monkeypatch
):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    sibling = await api.fork(job)
    ordinary = (await api.send(sibling["thread_id"], "sibling's admitted work")).json()["job"]
    admission_holds_group = asyncio.Event()
    release_admission = asyncio.Event()
    admission_pid = None
    group = await api.store.sandbox.thread_sandbox(thread_id=thread_id)
    assert group is not None
    original_capture = api.store.sandbox._capture_attempt_ledger

    async def hold_admission(conn: Any, **fields: Any) -> None:
        nonlocal admission_pid
        await original_capture(conn, **fields)
        if fields["thread_id"] == sibling["thread_id"]:
            # Hold the admission's next write boundary in its own transaction.
            # Its attempt and membership are already locked in normal order.
            await conn.fetchval(
                "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE", group["sandbox_id"]
            )
            admission_pid = await conn.fetchval("SELECT pg_backend_pid()")
            admission_holds_group.set()
            await release_admission.wait()

    monkeypatch.setattr(api.store.sandbox, "_capture_attempt_ledger", hold_admission)
    admission = asyncio.create_task(api.start(ordinary))
    watch = None
    before = await api.queue(thread_id)
    try:
        await asyncio.wait_for(admission_holds_group.wait(), timeout=10)
        watch = asyncio.create_task(
            api.send(
                thread_id,
                "Watch the PR and merge",
                require_idle=True,
                expected_latest_job_id=job["id"],
            )
        )
        async with asyncio.timeout(10), api.pool.acquire() as observer:
            while not await observer.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                admission_pid,
            ):
                await asyncio.sleep(0.01)
        assert not watch.done()
        release_admission.set()
        admitted, refused = await asyncio.wait_for(asyncio.gather(admission, watch), timeout=10)
    finally:
        release_admission.set()
        for task in (admission, watch):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    assert admitted["job_id"] == ordinary["id"]
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["type"] == "conversation_changed"
    assert await api.queue(thread_id) == before
    assert len(await api.turns(thread_id)) == 1


async def test_ordinary_sibling_submit_waits_for_watch_to_commit_after_its_empty_group_check(
    api: Api, monkeypatch
):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    sibling = await api.fork(job)
    checked_empty_group = asyncio.Event()
    release_watch = asyncio.Event()
    watch_pid = None
    original_insert = pending_message_storage.insert_follow_up

    async def hold_watch(conn: Any, **fields: Any) -> dict[str, Any] | None:
        nonlocal watch_pid
        if fields["prompt"] == "Watch the PR and merge":
            # The guarded transaction has read the group's empty queue and
            # still owns its thread, membership and group locks.
            watch_pid = await conn.fetchval("SELECT pg_backend_pid()")
            checked_empty_group.set()
            await release_watch.wait()
        return await original_insert(conn, **fields)

    monkeypatch.setattr(pending_message_storage, "insert_follow_up", hold_watch)
    watch = asyncio.create_task(
        api.send(
            thread_id,
            "Watch the PR and merge",
            require_idle=True,
            expected_latest_job_id=job["id"],
        )
    )
    ordinary = None
    try:
        await asyncio.wait_for(checked_empty_group.wait(), timeout=10)
        ordinary = asyncio.create_task(api.send(sibling["thread_id"], "ordinary sibling follow-up"))
        async with asyncio.timeout(10), api.pool.acquire() as observer:
            while not await observer.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                watch_pid,
            ):
                await asyncio.sleep(0.01)
            assert (
                await observer.fetchval(
                    "SELECT count(*) FROM agent_pending_messages WHERE thread_id = $1",
                    sibling["thread_id"],
                )
                == 0
            )
            assert (
                await observer.fetchval(
                    "SELECT count(*) FROM agent_jobs WHERE thread_id = $1", sibling["thread_id"]
                )
                == 1
            )
        assert not ordinary.done()
        release_watch.set()
        accepted_watch, accepted_ordinary = await asyncio.wait_for(
            asyncio.gather(watch, ordinary), timeout=10
        )
    finally:
        release_watch.set()
        for task in (watch, ordinary):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    assert accepted_watch.status_code == accepted_ordinary.status_code == 201
    assert accepted_watch.json()["job"]["task_prompt"] == "Watch the PR and merge"
    assert accepted_ordinary.json()["job"]["task_prompt"] == "ordinary sibling follow-up"
    assert accepted_watch.json()["pending"] is accepted_ordinary.json()["pending"] is None
    assert len(await api.turns(thread_id)) == len(await api.turns(sibling["thread_id"])) == 2
    assert (await api.queue(thread_id))["messages"] == []
    assert (await api.queue(sibling["thread_id"]))["messages"] == []


async def test_ordinary_submit_without_a_retained_membership_still_queues_and_replays(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    # The first turn has not reached a host, so no membership or group exists.
    assert await api.store.sandbox.thread_sandbox(thread_id=thread_id) is None

    first = await api.send(thread_id, "ordinary legacy queue", key="no-membership-queue")
    again = await api.send(thread_id, "ordinary legacy queue", key="no-membership-queue")

    assert first.status_code == again.status_code == 201
    assert first.json()["job"] is again.json()["job"] is None
    assert first.json()["pending"]["id"] == again.json()["pending"]["id"]
    assert [row["prompt"] for row in (await api.queue(thread_id))["messages"]] == [
        "ordinary legacy queue"
    ]
    assert len(await api.turns(thread_id)) == 1


async def test_guarded_submit_does_not_hold_an_included_job_while_waiting_for_publication(
    api: Api,
):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    group = await api.store.sandbox.thread_sandbox(thread_id=thread_id)
    assert group is not None
    watch = None
    try:
        async with api.pool.acquire() as publisher:
            snapshot_id = await publisher.fetchval(
                "UPDATE agent_sandbox_snapshots SET state = 'publishing' "
                "WHERE sandbox_id = $1 RETURNING id",
                group["sandbox_id"],
            )
            assert snapshot_id is not None
            async with publisher.transaction():
                # record_snapshot_publish locks snapshot -> group -> included
                # jobs. Pause at its group boundary before the included write.
                await publisher.execute(
                    "UPDATE agent_sandbox_snapshots SET state = 'published', "
                    "published_commit_sha = $2, published_at = NOW() WHERE id = $1",
                    snapshot_id,
                    "a" * 40,
                )
                await publisher.fetchval(
                    "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE",
                    group["sandbox_id"],
                )
                publisher_pid = await publisher.fetchval("SELECT pg_backend_pid()")
                watch = asyncio.create_task(
                    api.send(
                        thread_id,
                        "Watch the PR and merge",
                        require_idle=True,
                        expected_latest_job_id=job["id"],
                    )
                )
                async with asyncio.timeout(10), api.pool.acquire() as observer:
                    while not await observer.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                        publisher_pid,
                    ):
                        await asyncio.sleep(0.01)
                # If Watch locked its parent before waiting for the group,
                # this real included-job update would form a PostgreSQL cycle.
                async with asyncio.timeout(10):
                    updated = await publisher.execute(
                        "UPDATE agent_jobs SET published_pr_url = $2, "
                        "published_commit_sha = $3 WHERE id IN ("
                        "SELECT job_id FROM agent_snapshot_jobs WHERE snapshot_id = $1)",
                        snapshot_id,
                        "https://github.com/owner/name/pull/1",
                        "a" * 40,
                    )
                    assert updated == "UPDATE 1"
                assert not watch.done()
        accepted = await asyncio.wait_for(watch, timeout=10)
    finally:
        if watch is not None and not watch.done():
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)

    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["job"]["task_prompt"] == "Watch the PR and merge"
    assert accepted.json()["pending"] is None
    assert (await api.queue(thread_id))["messages"] == []
    assert len(await api.turns(thread_id)) == 2


async def test_guarded_submit_rolls_back_the_turn_if_a_prepared_upload_disappears(
    api: Api, monkeypatch
):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    upload = await api.store.attachments.create_attachment(
        user_id=_OWNER, path="notes.txt", content=b"notes", content_type="text/plain"
    )
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET queue_paused_reason = 'failed', "
            "queue_settled_job_id = NULL WHERE id = $1",
            thread_id,
        )
    before = await api.queue(thread_id)
    original = api.store.pending_messages.submit_message

    async def lose_prepared_upload(**fields: Any) -> dict[str, Any]:
        assert fields["attachments"][0]["id"] == upload["id"]
        assert await api.store.attachments.delete_attachment(
            user_id=_OWNER, attachment_id=upload["id"]
        )
        return await original(**fields)

    monkeypatch.setattr(api.store.pending_messages, "submit_message", lose_prepared_upload)
    refused = await api.send(
        thread_id,
        "Watch the PR and merge",
        key="watch-with-missing-upload",
        require_idle=True,
        expected_latest_job_id=job["id"],
        attachment_ids=[upload["id"]],
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["error"]["type"] == "invalid_attachment_selection"
    assert await api.queue(thread_id) == before
    assert len(await api.turns(thread_id)) == 1
    assert [message["role"] for message in (await api.thread(job["id"]))["messages"]].count(
        "user"
    ) == 1


@pytest.mark.parametrize("override", [False, True], ids=["inherit", "prepared-overrides"])
async def test_guarded_submit_preserves_resolved_turn_choices_and_attachment_ownership(
    api: Api, override: bool
):
    skill = await api.store.skills.create_skill(
        user_id=_OWNER,
        values={
            "name": "Review",
            "description": "Review changes",
            "instructions": "Inspect the original tests.",
            "source": "created",
        },
    )
    original_upload = await api.store.attachments.create_attachment(
        user_id=_OWNER, path="original.txt", content=b"original", content_type="text/plain"
    )
    job = await api.create(
        reasoning_effort="high", skill_ids=[skill["id"]], attachment_ids=[original_upload["id"]]
    )
    thread_id = job["thread_id"]
    await api.run(job)
    await _publish_workspace(api, thread_id)
    previous = await api.store.conversation_queries.get_job(job["id"])
    assert previous is not None
    async with api.pool.acquire() as conn:
        previous_skills = json.loads(
            await conn.fetchval("SELECT skills FROM agent_jobs WHERE id = $1", job["id"])
        )
    await api.store.skills.update_skill(
        user_id=_OWNER,
        skill_id=skill["id"],
        revision=skill["revision"],
        values={"instructions": "Inspect the newly selected tests."},
    )
    fields: dict[str, Any] = {}
    upload = None
    if override:
        upload = await api.store.attachments.create_attachment(
            user_id=_OWNER, path="next.txt", content=b"next", content_type="text/plain"
        )
        fields = {
            "model": "glm-4.6",
            "reasoning_effort": "auto",
            "skill_ids": [skill["id"]],
            "attachment_ids": [upload["id"]],
        }

    accepted = await api.send(
        thread_id,
        "Watch the PR and merge",
        require_idle=True,
        expected_latest_job_id=job["id"],
        **fields,
    )

    assert accepted.status_code == 201, accepted.text
    made = await api.store.conversation_queries.get_job(accepted.json()["job"]["id"])
    assert made is not None
    async with api.pool.acquire() as conn:
        made_skills = json.loads(
            await conn.fetchval("SELECT skills FROM agent_jobs WHERE id = $1", made["id"])
        )
    assert made["parent_job_id"] == job["id"]
    if override:
        assert (made["model"], made["reasoning_effort"]) == ("glm-4.6", None)
        assert made_skills[0]["instructions"] == "Inspect the newly selected tests."
        assert made_skills[0]["revision"] == previous_skills[0]["revision"] + 1
        assert upload is not None
        assert made["attachments"] == [
            {"id": upload["id"], "path": "next.txt", "content_type": "text/plain", "size_bytes": 4}
        ]
        async with api.pool.acquire() as conn:
            bound = await conn.fetchrow(
                "SELECT job_id, thread_id, pending_message_id FROM agent_job_attachments WHERE id = $1",
                upload["id"],
            )
        assert dict(bound) == {
            "job_id": made["id"],
            "thread_id": thread_id,
            "pending_message_id": None,
        }
    else:
        assert (made["model"], made["reasoning_effort"]) == ("glm-5.1", "high")
        assert made_skills == previous_skills
        assert made_skills[0]["instructions"] == "Inspect the original tests."
        assert made["attachments"] is None
    async with api.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT job_id FROM agent_job_attachments WHERE id = $1", original_upload["id"]
            )
            == job["id"]
        )
    assert (await api.queue(thread_id))["messages"] == []


# -- 10.1: uploads a waiting message holds -----------------------------------------


async def test_uploads_a_waiting_message_holds_survive_the_sweep_until_it_is_deleted(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.start(job)
    upload = await api.store.attachments.create_attachment(
        user_id=_OWNER, path="notes.txt", content=b"notes", content_type="text/plain"
    )
    stray = await api.store.attachments.create_attachment(
        user_id=_OWNER, path="stray.txt", content=b"stray", content_type="text/plain"
    )
    sent = await api.send(thread_id, "read the notes", attachment_ids=[upload["id"]])
    assert sent.status_code == 201, sent.text
    message = sent.json()["pending"]
    assert [item["path"] for item in message["attachments"]] == ["notes.txt"]
    # Both uploads are old enough for the staged sweep.
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_job_attachments SET created_at = NOW() - INTERVAL '48 hours'"
        )

    assert await api.store.attachments.purge_stale_staged(older_than_hours=24) == 1
    async with api.pool.acquire() as conn:
        left = await conn.fetch("SELECT id, pending_message_id FROM agent_job_attachments")
    assert [(row["id"], row["pending_message_id"]) for row in left] == [
        (upload["id"], message["id"])
    ]
    assert stray["id"] not in {row["id"] for row in left}
    # The composer cannot remove a file a waiting message will send.
    refused = await api.client.delete(f"/v1/agent/uploads/{upload['id']}")
    assert refused.status_code == 404
    assert refused.json()["detail"]["error"]["type"] == "upload_not_found"
    async with api.pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM agent_job_attachments WHERE id = $1", upload["id"]
        )

    deleted = await api.delete(thread_id, message)

    assert deleted.status_code == 200, deleted.text
    async with api.pool.acquire() as conn:
        released = await conn.fetchrow(
            "SELECT job_id, thread_id, pending_message_id FROM agent_job_attachments WHERE id = $1",
            upload["id"],
        )
    assert dict(released) == {"job_id": None, "thread_id": None, "pending_message_id": None}
    assert await api.store.attachments.purge_stale_staged(older_than_hours=24) == 1


async def test_a_waiting_message_carries_its_uploads_into_its_turn(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    running = await api.start(job)
    upload = await api.store.attachments.create_attachment(
        user_id=_OWNER, path="spec.md", content=b"# spec", content_type="text/markdown"
    )
    await api.send(thread_id, "follow the spec", attachment_ids=[upload["id"]])

    await api.finish(running)

    turns = await api.turns(thread_id)
    assert [turn["task_prompt"] for turn in turns] == ["fix it", "follow the spec"]
    async with api.pool.acquire() as conn:
        claimed = await conn.fetchrow(
            "SELECT job_id, thread_id, pending_message_id FROM agent_job_attachments WHERE id = $1",
            upload["id"],
        )
    assert dict(claimed) == {
        "job_id": turns[1]["id"],
        "thread_id": thread_id,
        "pending_message_id": None,
    }


# -- 10.1: each message keeps its own model and effort -----------------------------


async def test_each_message_runs_with_its_own_model_and_effort(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    running = await api.start(job)
    a = await api.send(thread_id, "A on the smaller model", model="glm-4.6", reasoning_effort="low")
    b = await api.send(thread_id, "B back on the default", model="glm-5.1", reasoning_effort="max")
    assert a.status_code == b.status_code == 201, (a.text, b.text)

    await api.finish(running)
    a_turn = (await api.turns(thread_id))[1]
    assert (a_turn["task_prompt"], a_turn["model"], a_turn["reasoning_effort"]) == (
        "A on the smaller model",
        "glm-4.6",
        "low",
    )
    await api.run({"id": a_turn["id"]})
    b_turn = (await api.turns(thread_id))[2]
    assert (b_turn["task_prompt"], b_turn["model"], b_turn["reasoning_effort"]) == (
        "B back on the default",
        "glm-5.1",
        "max",
    )


async def test_a_message_the_checks_refuse_when_it_is_sent_is_held_with_the_reason(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    running = await api.start(job)
    await api.send(thread_id, "use the smaller model", model="glm-4.6")
    behind = (await api.send(thread_id, "and then this")).json()["pending"]
    # The model is withdrawn while the message waits.
    api.gateway.models = ["glm-5.1"]

    await api.finish(running)

    queue = await api.queue(thread_id)
    assert [row["state"] for row in queue["messages"]] == ["held", "queued"]
    assert "glm-4.6" in queue["messages"][0]["last_error"]
    assert queue["messages"][1]["id"] == behind["id"]
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it"]
    thread = await api.thread(job["id"])
    assert thread["pending_messages"][0]["last_error"] == queue["messages"][0]["last_error"]


# -- 10.1: the owner's message before the goal's next round -------------------------


async def test_the_owners_waiting_message_runs_before_the_goal_continues(api: Api):
    job = await api.create(prompt="ship the export", goal={"objective": "the export works"})
    thread_id = job["thread_id"]
    running = await api.start(job)
    await api.send(thread_id, "use CSV, not JSON")

    await api.finish(running)

    turns = await api.turns(thread_id)
    assert [(turn["turn_source"], turn["task_prompt"]) for turn in turns] == [
        ("human", "ship the export"),
        ("human", "use CSV, not JSON"),
    ]
    async with api.pool.acquire() as conn:
        parked = await conn.fetch(
            "SELECT waiting_job_id FROM agent_goal_wakeups "
            "WHERE thread_id = $1 AND state = 'pending'",
            thread_id,
        )
    assert parked and {row["waiting_job_id"] for row in parked} == {turns[1]["id"]}

    # Only once the owner's turn has ended does the goal add its round.
    await api.run({"id": turns[1]["id"]})

    turns = await api.turns(thread_id)
    assert [turn["turn_source"] for turn in turns] == ["human", "human", "goal"]
    assert turns[2]["parent_job_id"] == turns[1]["id"]


# -- the rest of the design's behaviour ---------------------------------------------


async def test_an_idle_send_on_a_paused_queue_continues_it_oldest_first(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    running = await api.start(job)
    await api.send(thread_id, "older, waiting")
    await api.finish(running, "failed")
    assert (await api.queue(thread_id))["queue_paused_reason"] == "failed"

    # Enter on the idle, paused conversation is the owner continuing.
    newer = await api.send(thread_id, "newer")

    assert newer.status_code == 201, newer.text
    assert newer.json()["job"]["task_prompt"] == "older, waiting"
    assert newer.json()["pending"]["prompt"] == "newer"
    assert newer.json()["queue_paused_reason"] is None
    assert [row["prompt"] for row in (await api.queue(thread_id))["messages"]] == ["newer"]


async def test_archiving_pauses_a_waiting_queue_and_restoring_does_not_continue_it(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    running = await api.start(job)
    await api.send(thread_id, "after the archive")

    assert (await api.client.post(f"/v1/agent/jobs/{job['id']}/archive")).status_code == 200
    assert (await api.queue(thread_id))["queue_paused_reason"] == "archived"
    refused = await api.send(thread_id, "while archived")
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "session_archived"
    assert (await api.resume(thread_id)).status_code == 409

    await api.finish(running)
    assert (await api.client.delete(f"/v1/agent/jobs/{job['id']}/archive")).status_code == 200

    assert (await api.queue(thread_id))["queue_paused_reason"] == "archived"
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it"]
    resumed = await api.resume(thread_id)
    assert resumed.json()["job"]["task_prompt"] == "after the archive"


async def test_the_queue_route_answers_what_a_page_polls(api: Api):
    job = await api.create()
    thread_id = job["thread_id"]
    await api.start(job)
    message = (await api.send(thread_id, "poll me")).json()["pending"]

    response = await api.client.get(f"/v1/agent/threads/{thread_id}/messages")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["id"] for row in body["pending_messages"]] == [message["id"]]
    assert (body["latest_job_id"], body["latest_turn_no"], body["latest_job_state"]) == (
        job["id"],
        1,
        "running",
    )
    assert body["queue_paused_reason"] is None
    other = await api.client.get("/v1/agent/threads/athr_not_theirs/messages")
    assert other.status_code == 404


async def test_deleting_the_conversation_removes_its_waiting_messages_and_their_uploads(
    api: Api,
):
    job = await api.create()
    thread_id = job["thread_id"]
    upload = await api.store.attachments.create_attachment(
        user_id=_OWNER, path="draft.txt", content=b"draft", content_type="text/plain"
    )
    # The first turn has not started, so the message waits behind it.
    sent = await api.send(thread_id, "never sent", attachment_ids=[upload["id"]])
    assert sent.json()["pending"] is not None
    assert (await api.client.post(f"/v1/agent/jobs/{job['id']}/cancel")).status_code == 200
    assert (await api.client.post(f"/v1/agent/jobs/{job['id']}/archive")).status_code == 200

    deleted = await api.store.conversation_commands.delete_sessions(
        thread_ids=[thread_id], user_id=_OWNER
    )

    assert deleted == {"thread_ids": [thread_id]}
    async with api.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agent_pending_messages WHERE thread_id = $1", thread_id
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agent_job_attachments WHERE id = $1", upload["id"]
            )
            == 0
        )
