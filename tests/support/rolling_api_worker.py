"""Run from a previous release's checkout, retaining its pool across migration.

The previous release's API keeps serving while a deploy migrates the database
under it: owners reading and writing conversations, and runners driving the
turns already in flight. Each check does both, through the old release's own
``Storage`` and over the one connection it held before the migration, so the
old code's prepared statements meet the candidate schema.

The calls below are the previous release's storage API, not this checkout's.
A change to one of their signatures has to keep the old spelling working here
until a release with the new one has been published.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import asyncpg

# The controller explicitly supplies the old checkout, never this repository.
#
# Where that checkout keeps its packages depends on when the release was cut.
# A tree from before the layout move has `cloud_agent/` at its top level; one
# from after it has the same package under `apps/backend/`. Both are handed to
# this worker, and putting the wrong root first is not a harmless miss: the
# import then falls through to the *candidate* the venv has installed, and the
# gate silently stops testing a previous release at all — which is how a
# candidate that writes a column the old schema does not have first shows up
# as "previous release failed before upgrade".
_old_tree = Path(sys.argv[1]).resolve()
for _package_root in (_old_tree, _old_tree / "apps" / "backend"):
    if (_package_root / "cloud_agent").is_dir():
        sys.path.insert(0, str(_package_root))
        break
else:  # pragma: no cover - a checkout with no control plane is not a release
    raise SystemExit(f"no cloud_agent package under {_old_tree}")

from cloud_agent.storage.assembly import Storage
from cloud_agent_shared import shared_protocol

# The same fall-through as above, for the vocabulary a runner attests with: a
# release without this package would hand the claim the candidate's pins.
if not Path(shared_protocol.__file__).resolve().is_relative_to(_old_tree):
    raise SystemExit(f"{shared_protocol.__file__} is not the previous release's")

#: The one runner of the previous release, as its API sees it: an enrolled
#: host that heartbeats and claims, attesting what a new chat group requires.
_HOST = "rolling-upgrade-host"
_CREDENTIAL_HASH = "rolling-upgrade-host-credential-hash"
_IMAGE = "sha256:rolling-upgrade-image"
_CAPABILITIES = [
    shared_protocol.SHARED_SANDBOX_CAPABILITY,
    shared_protocol.NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    shared_protocol.shared_deployment_capability("rolling-upgrade"),
    shared_protocol.GOAL_REPORT_CAPABILITY,
]
_ATTESTATION = {
    "sandbox_image_digest": _IMAGE,
    "runner_capabilities": _CAPABILITIES,
    "runtime_versions": dict(shared_protocol.SHARED_SANDBOX_RUNTIME_PINS),
    "helper_versions": dict(shared_protocol.SHARED_SANDBOX_HELPER_PINS),
}
_LEASE_TTL_S = 120.0
_RESIDENCY_TTL_S = 1800
_RESOLVED_BASE_SHA = "a" * 40


async def _run_turn(store: Storage, claim: dict[str, Any]) -> None:
    """Drive one claimed turn the way a runner drives it through the old API.

    The worker routes in the order a turn calls them: link its grant, renew
    its lease, report an event, reserve the chat's group (a new one, or the
    retained group of a follow-up), start it, establish the member's residency,
    record the CLI process, admit and settle the turn, fence the workspace and
    finish. Then the host's own heartbeat renews that residency.
    """
    thread_id = str(claim["thread_id"])
    lease = {"attempt_id": claim["attempt_id"], "lease_generation": claim["lease_generation"]}
    assert await store.grants.record_attempt_grant(
        **lease, grant_id=f"rolling-attempt-grant-{claim['attempt_id']}"
    )
    beat = await store.attempt_commands.heartbeat(**lease, lease_ttl_seconds=_LEASE_TTL_S)
    assert beat["ok"], beat
    assert (
        await store.attempt_commands.append_event(
            **lease, event_type="message", payload={"text": "rolling upgrade"}, event_seq=1
        )
        is not None
    )

    retained = claim.get("thread_sandbox") or {}
    sandbox_id = str(retained.get("sandbox_id") or f"asbx_rolling_{claim['attempt_id']}")
    sandbox = await store.sandbox.reserve_thread_sandbox(
        thread_id=thread_id,
        **lease,
        host=_HOST,
        sandbox_id=sandbox_id,
        container_name=str(retained.get("container_name") or f"rolling-{sandbox_id}"),
        runtime_id=str(retained.get("runtime_id") or claim["runtime"]),
        runtime_version=(
            retained.get("runtime_version") if retained else claim.get("runtime_version")
        ),
        sandbox_image_digest=(
            retained.get("sandbox_image_digest") if retained else claim.get("sandbox_image_digest")
        ),
        execution_profile=str(claim.get("execution_profile") or "trusted-v1"),
    )
    assert sandbox is not None, "the sandbox reservation lost its lease"
    group = {
        "thread_id": thread_id,
        **lease,
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    context_status = "restored" if sandbox.get("last_settled_job_id") else "new"
    assert await store.sandbox.mark_thread_sandbox_running(
        **group, container_id=f"container-{sandbox_id}", context_status=context_status
    )

    residency = await store.residency.establish_residency(
        thread_id=thread_id,
        **lease,
        host=_HOST,
        sandbox_id=sandbox["sandbox_id"],
        runtime_id=sandbox["runtime_id"],
        runtime_version=sandbox["runtime_version"],
        ttl_seconds=_RESIDENCY_TTL_S,
    )
    assert residency is not None, "the residency lost its lease"
    generation = int(residency["generation"])
    assert await store.grants.link_residency_grant(
        thread_id=thread_id,
        generation=generation,
        grant_id=f"rolling-residency-grant-{claim['attempt_id']}",
    )
    assert await store.sandbox.record_shared_thread_residency(
        thread_id=thread_id,
        **lease,
        sandbox_id=sandbox["sandbox_id"],
        residency_generation=generation,
    )
    assert await store.sandbox.record_thread_sandbox_process(
        **group,
        residency_generation=generation,
        process_generation=int(sandbox.get("process_generation") or 0) + 1,
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **group, residency_generation=generation, job_id=claim["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **group,
        residency_generation=generation,
        job_id=claim["id"],
        context_status=context_status,
        native_session_id=f"rolling-session-{thread_id}",
        # The commit the runner resolved its checkout to.
        base_sha=str(claim.get("base_sha") or _RESOLVED_BASE_SHA),
        idle_timeout_seconds=_RESIDENCY_TTL_S,
    )
    assert await store.sandbox.fence_terminal_workspace(**lease)
    assert await store.attempt_commands.transition(
        job_id=claim["id"], **lease, from_states=("running",), to_state="succeeded"
    )

    assert await store.host_pool.record_host_heartbeat(
        host=_HOST, capabilities=_CAPABILITIES, sandbox_image_digest=_IMAGE
    )
    assert await store.residency.heartbeat_residency(
        thread_id=thread_id, generation=generation, host=_HOST, ttl_seconds=_RESIDENCY_TTL_S
    )


async def _run_queued_turns(store: Storage) -> list[str]:
    """Claim and finish every queued turn this host may take, oldest first."""
    ran: list[str] = []
    for _ in range(20):
        claim = await store.attempt_commands.claim_job(
            worker_id="rolling-upgrade-runner",
            lease_ttl_seconds=_LEASE_TTL_S,
            host=_HOST,
            host_credential_hash=_CREDENTIAL_HASH,
            claim_attestation=_ATTESTATION,
        )
        if claim is None:
            return ran
        await _run_turn(store, claim)
        ran.append(str(claim["id"]))
    raise AssertionError(f"the queue did not drain: {ran}")


async def main() -> None:
    """Exercise conversation and turn operations before and after the upgrade."""
    pool = await asyncpg.create_pool(os.environ["AGENT_DATABASE_URL"], min_size=1, max_size=1)
    store = Storage(pool)
    owner = "rolling-upgrade-test"
    parent = await store.conversation_commands.create_job(
        user_id=owner,
        repo="example/repo",
        task_prompt="Before upgrade",
        runtime="codex",
        model="test-model",
    )
    await store.host_pool.register_host(
        host=_HOST, credential_hash=_CREDENTIAL_HASH, capabilities=_CAPABILITIES, slots_total=4
    )
    online = await store.host_pool.record_host_heartbeat(
        host=_HOST, capabilities=_CAPABILITIES, sandbox_image_digest=_IMAGE
    )
    assert online is not None and online["status"] == "online", online
    async with pool.acquire() as conn:
        backend_pid = await conn.fetchval("SELECT pg_backend_pid()")

    async def exercise() -> None:
        created = await store.conversation_commands.create_job(
            user_id=owner,
            repo="example/repo",
            task_prompt="During upgrade",
            runtime="kilo",
            model="test-model",
        )
        assert created["state"] == "queued"
        assert await store.conversation_queries.list_sessions(user_id=owner)
        assert await store.conversation_queries.list_jobs(user_id=owner)
        assert await store.conversation_queries.list_conversations(user_id=owner)
        assert await store.conversation_queries.get_job(parent["id"])
        assert await store.conversation_queries.get_thread_for_job(
            job_id=parent["id"], user_id=owner
        )
        assert await store.mcp.list_mcp_connections(user_id=owner) == []
        followup = await store.conversation_commands.create_follow_up(
            parent_job_id=parent["id"],
            user_id=owner,
            prompt="Continue",
        )
        assert followup["state"] == "queued"
        # A new chat's first turn and a retained chat's next one both run to
        # the end, so both paths through the worker routes meet this schema.
        ran = await _run_queued_turns(store)
        assert {created["id"], followup["id"]} <= set(ran), ran
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT pg_backend_pid()") == backend_pid

    try:
        await exercise()
        print("READY", flush=True)
        # stdin is a deliberate test barrier: the controller migrates while
        # this exact old process and its prepared statements remain alive.
        for _ in range(2):
            if sys.stdin.readline().strip() != "CHECK":
                raise RuntimeError("rolling upgrade controller disconnected")
            await exercise()
            print("PASS", flush=True)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
