"""G7: a host vanishing mid-attempt produces exactly one pull request.

This is the guarantee the whole fencing design exists for, restated for a pool
that can lose a machine: **the work is not lost and the PR is not duplicated.**

A duplicate PR is the worst available outcome. It is not an error anyone sees
in a log — it is two branches, two review threads, and a maintainer wondering
which one is real, on a repository the platform does not own. So the assertion
here is a *count*, taken over the real publish path, with only the two steps
that reach outside the process (git, GitHub) replaced.

The sequence under test, in order:

1. ``host-a`` claims a job and produces a patch.
2. ``host-a`` disappears — no heartbeats, lease lapses.
3. The reaper marks it offline, supersedes its attempt, and requeues the job.
4. ``host-a`` comes back to life *unaware it lost the lease* and tries to finish.
5. ``host-b`` claims the requeued job and finishes it properly.
6. The publisher drains.

Step 4 is the one that matters, and it is not hypothetical: a machine that was
merely unreachable — a partition, a wedged disk, a paused VM — is still running
the agent when it returns, and it has no way to know its lease was taken. It
must not be able to publish behind the replacement's back.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

from cloud_agent import publish_worker
from cloud_agent.host_pool import HOST_OFFLINE, HOST_ONLINE, hash_secret
from cloud_agent.publisher import PublishResult
from cloud_agent.storage.store import AgentJobStore
from tests.support.migrations import apply_migrations

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_REPO = "acme/widgets"
_TABLES_IN_FK_ORDER = (
    "agent_native_session_edges",
    "agent_thread_sandbox_memberships",
    "agent_sandbox_snapshots",
    "agent_session_forks",
    "agent_sandboxes",
    "agent_thread_messages",
    "agent_job_artifacts",
    "agent_job_events",
    "agent_thread_sandboxes",
    "agent_thread_residencies",
    "agent_attempts",
    "agent_jobs",
    "agent_threads",
    "agent_host_enroll_tokens",
    "agent_runner_hosts",
)


@pytest_asyncio.fixture
async def store():
    """A clean store over the shared test database."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }

    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=10)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    apply_migrations(db_config)

    async def _reset() -> None:
        async with pool.acquire() as conn:
            for table in _TABLES_IN_FK_ORDER:
                await conn.execute(f"DELETE FROM {table}")
            await conn.execute("UPDATE agent_runner_policy SET active_host = NULL")

    await _reset()
    try:
        yield AgentJobStore(pool)
    finally:
        await _reset()
        await pool.close()


class PullRequests:
    """Counts what actually reached GitHub.

    Stands in for the two steps that leave the process — pushing a branch and
    opening a PR — and nothing else. ``claim_for_publish`` and
    ``record_publish``, which are where exactly-once is enforced, stay real.
    """

    def __init__(self) -> None:
        self.pushes: list[str] = []
        self.opened: list[str] = []

    def publish_patch(self, *, job_id: str, branch_id: str, **_kwargs) -> PublishResult:
        self.pushes.append(job_id)
        return PublishResult(
            branch=f"agent/{branch_id}",
            commit_sha=f"sha-{len(self.pushes)}",
            changed_files=["README.md"],
        )

    async def create_draft_pull_request(self, *, repo: str, head_branch: str, **_kwargs) -> str:
        self.opened.append(head_branch)
        return f"https://github.test/{repo}/pull/{len(self.opened)}"


@pytest.fixture
def pull_requests(monkeypatch):
    """Install the counting fakes and an allowlist that lets publishing start."""
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", _REPO)
    recorder = PullRequests()
    monkeypatch.setattr(publish_worker, "publish_patch", recorder.publish_patch)
    monkeypatch.setattr(
        publish_worker, "create_draft_pull_request", recorder.create_draft_pull_request
    )
    return recorder


async def _drain_publishes(store: AgentJobStore) -> None:
    """Run the real publisher until the queue is empty."""
    credential = publish_worker.GitHubCredential("test-token")
    while await publish_worker.publish_one(store, credential=credential):
        pass


async def _enrol_online(store: AgentJobStore, host: str, **overrides) -> None:
    await store.register_host(host=host, credential_hash=hash_secret(f"cred-{host}"), **overrides)
    await store.record_host_heartbeat(host=host)


async def _stop_heartbeating(store: AgentJobStore, host: str) -> None:
    """Age a host's last heartbeat past the reaper's patience, without sleeping."""
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts SET last_heartbeat_at = NOW() - INTERVAL '1 hour' "
            "WHERE host = $1",
            host,
        )


async def _expire_lease(store: AgentJobStore, attempt_id: int) -> None:
    """Let the attempt's lease lapse, as it would while its machine is gone."""
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 minute' WHERE id = $1",
            attempt_id,
        )


async def _create_job(store: AgentJobStore) -> dict:
    return await store.create_job(
        user_id="u1",
        repo=_REPO,
        task_prompt="fix the thing",
        runtime="claude-code",
        model="glm-5.1",
        base_sha="a" * 40,
    )


async def test_a_host_lost_mid_attempt_yields_exactly_one_pull_request(store, pull_requests):
    """The headline guarantee, end to end."""
    await _enrol_online(store, "host-a", slots_total=1)
    await _enrol_online(store, "host-b", slots_total=1)
    job = await _create_job(store)

    # 1. host-a takes the job and gets as far as producing a patch.
    first = await store.claim_job(worker_id="w-a", lease_ttl_seconds=120, host="host-a")
    assert first["id"] == job["id"]
    await store.save_artifact(
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
        kind="patch",
        content="diff --git a/README.md b/README.md",
    )

    # 2. The machine vanishes.
    await _stop_heartbeating(store, "host-a")
    await _expire_lease(store, first["attempt_id"])

    # 3. The reaper does what the control plane's loop does, in the same order.
    assert await store.reap_offline_hosts(interval_seconds=30, missed_intervals=3) == ["host-a"]
    assert (await store.get_host(host="host-a"))["status"] == HOST_OFFLINE
    await store.reap_expired(max_attempts=3)
    assert (await store.get_job(job["id"]))["state"] == "queued"
    # The slot went back with the attempt, so the pool's arithmetic survives a
    # machine that never said goodbye.
    assert (await store.get_host(host="host-a"))["slots_used"] == 0

    # 3a. And an offline host claims nothing, so the job cannot bounce back to
    # the machine that just lost it.
    assert await store.claim_job(worker_id="w-a", lease_ttl_seconds=120, host="host-a") is None

    # 4. host-a comes back mid-run, unaware. Every write it attempts is fenced.
    #
    # The artifact write is the sharp one. ``claim_for_publish`` joins any
    # ``patch`` artifact of the job, so a superseded attempt that could still
    # overwrite — or add a second — patch row would decide what gets pushed to
    # the customer's repository from a machine the platform has already
    # replaced.
    assert (
        await store.save_artifact(
            attempt_id=first["attempt_id"],
            lease_generation=first["lease_generation"],
            kind="patch",
            content="ZOMBIE PATCH",
        )
        is None
    )
    assert (
        await store.append_event(
            attempt_id=first["attempt_id"],
            lease_generation=first["lease_generation"],
            event_type="message",
            payload={"text": "still here"},
        )
        is None
    )
    assert not await store.transition(
        job_id=job["id"],
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert (
        await store.heartbeat(
            attempt_id=first["attempt_id"],
            lease_generation=first["lease_generation"],
            lease_ttl_seconds=120,
        )
    ) == {"ok": False}
    async with store._pool.acquire() as conn:
        patches = await conn.fetch(
            "SELECT content FROM agent_job_artifacts WHERE job_id = $1 AND kind = 'patch'",
            job["id"],
        )
    assert [row["content"] for row in patches] == ["diff --git a/README.md b/README.md"]

    # 5. host-b picks the job up and finishes it properly.
    second = await store.claim_job(worker_id="w-b", lease_ttl_seconds=120, host="host-b")
    assert second["id"] == job["id"]
    assert second["attempt_id"] != first["attempt_id"]
    await store.save_artifact(
        attempt_id=second["attempt_id"],
        lease_generation=second["lease_generation"],
        kind="patch",
        content="diff --git a/README.md b/README.md",
    )
    assert await store.transition(
        job_id=job["id"],
        attempt_id=second["attempt_id"],
        lease_generation=second["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    # 6. Publish, then let the zombie try again and publish again.
    await _drain_publishes(store)
    await store.transition(
        job_id=job["id"],
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    await _drain_publishes(store)

    # One branch pushed, one PR opened. This is the assertion the task names.
    assert pull_requests.pushes == [job["id"]]
    assert len(pull_requests.opened) == 1
    finished = await store.get_job(job["id"])
    assert finished["state"] == "succeeded"
    assert finished["published_pr_url"] == "https://github.test/acme/widgets/pull/1"


async def test_two_publishers_racing_the_same_job_open_one_pull_request(store, pull_requests):
    """Exactly-once has to survive more than one control plane.

    ``claim_for_publish`` takes the row ``FOR UPDATE ... SKIP LOCKED`` and moves
    it to ``publishing`` in the same transaction, which is what makes running
    two of these safe. Asserted because a deployment that scales the control
    plane to two replicas is the ordinary way to discover it was not.
    """
    await _enrol_online(store, "host-a", slots_total=2)
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w-a", lease_ttl_seconds=120, host="host-a")
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/README.md b/README.md",
    )
    await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    await asyncio.gather(*(_drain_publishes(store) for _ in range(4)))

    assert len(pull_requests.opened) == 1


async def test_a_forced_removal_requeues_its_work_exactly_once(store, pull_requests):
    """G6's forced path, and the reason it is safe to offer at all.

    Removing a busy host does not stop what it is running — nothing in the
    control plane can reach that machine. It relies on the lease: the attempt
    expires, the reaper supersedes it once, and the job is requeued once. If
    that produced two live attempts, forced removal would be a PR duplicator
    with an admin button on it.
    """
    await _enrol_online(store, "host-a", slots_total=1)
    await _enrol_online(store, "host-b", slots_total=1)
    job = await _create_job(store)
    first = await store.claim_job(worker_id="w-a", lease_ttl_seconds=120, host="host-a")

    assert await store.remove_host(host="host-a") == "busy"
    assert await store.remove_host(host="host-a", force=True) == "ok"

    # Still running until the lease says otherwise — removal is not a kill.
    assert (await store.get_job(job["id"]))["state"] == "running"

    await _expire_lease(store, first["attempt_id"])
    # Several reaper passes, because a real deployment runs them on a timer and
    # a second pass must not produce a second requeue.
    for _ in range(3):
        await store.reap_expired(max_attempts=3)

    async with store._pool.acquire() as conn:
        running = await conn.fetchval(
            "SELECT count(*) FROM agent_attempts WHERE job_id = $1 AND status = 'running'",
            job["id"],
        )
    assert running == 0
    assert (await store.get_job(job["id"]))["state"] == "queued"

    second = await store.claim_job(worker_id="w-b", lease_ttl_seconds=120, host="host-b")
    assert second is not None
    await store.save_artifact(
        attempt_id=second["attempt_id"],
        lease_generation=second["lease_generation"],
        kind="patch",
        content="diff --git a/README.md b/README.md",
    )
    await store.transition(
        job_id=job["id"],
        attempt_id=second["attempt_id"],
        lease_generation=second["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    await _drain_publishes(store)

    assert len(pull_requests.opened) == 1


async def test_a_host_that_comes_back_is_online_again_and_claims(store, pull_requests):
    """The other half of the story: recovery must not need an operator."""
    await _enrol_online(store, "host-a", slots_total=1)
    await _stop_heartbeating(store, "host-a")
    await store.reap_offline_hosts(interval_seconds=30, missed_intervals=3)
    await _create_job(store)

    assert await store.claim_job(worker_id="w-a", lease_ttl_seconds=120, host="host-a") is None

    assert (await store.record_host_heartbeat(host="host-a"))["status"] == HOST_ONLINE

    assert await store.claim_job(worker_id="w-a", lease_ttl_seconds=120, host="host-a") is not None
