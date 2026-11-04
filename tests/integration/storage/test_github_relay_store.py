"""The GitHub relay's fence and pull request record, against a real database.

The relay exchanges a residency's placeholder for the owner's GitHub token only
while that residency serves a running, leased, unstopped turn on an enrolled
host. Only Postgres can show that this is one fenced read: each predicate
missing is the owner's token handed to a turn that is no longer theirs to run.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import _reserve_shared_chat
from tests.integration.storage.test_credential_queries_store import (  # noqa: F401 — fixtures
    db,
    store,
)

from cloud_agent.storage.repositories.github_relay import PostgresGitHubRelayRepository

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def relay(db):  # noqa: F811 — the imported fixture
    """The capability under test."""
    return PostgresGitHubRelayRepository(db)


async def test_a_residency_resolves_to_its_running_turn_with_owner_and_repository(
    db,  # noqa: F811
    store: Storage,  # noqa: F811
    relay: PostgresGitHubRelayRepository,
) -> None:
    job, claim, sandbox = await _reserve_shared_chat(store, repo="example-org/example-repo")
    lookup = {
        "thread_id": job["thread_id"],
        "residency_generation": sandbox["residency_generation"],
    }

    assert await relay.resolve_github_caller(**lookup) == {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "thread_id": job["thread_id"],
        "user_id": job["user_id"],
        "repo": "example-org/example-repo",
    }
    assert (
        await relay.resolve_github_caller(
            thread_id=job["thread_id"], residency_generation=lookup["residency_generation"] + 1
        )
        is None
    )

    # Stop takes effect on the next request, with nothing to revoke.
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET cancel_requested = TRUE WHERE id = $1", job["id"])
    assert await relay.resolve_github_caller(**lookup) is None


async def test_an_expired_lease_or_a_removed_host_resolves_nothing(
    db,  # noqa: F811
    store: Storage,  # noqa: F811
    relay: PostgresGitHubRelayRepository,
) -> None:
    job, claim, sandbox = await _reserve_shared_chat(store)
    lookup = {
        "thread_id": job["thread_id"],
        "residency_generation": sandbox["residency_generation"],
    }
    assert await relay.resolve_github_caller(**lookup) is not None

    expire = "UPDATE agent_attempts SET lease_expires_at = clock_timestamp() + $2 WHERE id = $1"
    async with db.acquire() as conn:
        await conn.execute(expire, claim["attempt_id"], timedelta(seconds=-1))
    assert await relay.resolve_github_caller(**lookup) is None

    async with db.acquire() as conn:
        await conn.execute(expire, claim["attempt_id"], timedelta(minutes=5))
    assert await relay.resolve_github_caller(**lookup) is not None
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts SET status = 'disabled' WHERE host = "
            "(SELECT host_id FROM agent_attempts WHERE id = $1)",
            claim["attempt_id"],
        )
    assert await relay.resolve_github_caller(**lookup) is None


async def test_a_pull_request_is_recorded_once_and_listed_newest_first(
    db,  # noqa: F811
    store: Storage,  # noqa: F811
    relay: PostgresGitHubRelayRepository,
) -> None:
    job, _claim, _sandbox = await _reserve_shared_chat(store)
    row = {
        "thread_id": job["thread_id"],
        "job_id": job["id"],
        "user_id": job["user_id"],
        "repo": "example-org/example-repo",
        "url": "https://github.com/example-org/example-repo/pull/7",
        "head_ref": "fix/one",
        "base_ref": "dev",
        "draft": True,
    }
    await relay.record_pull_request(**row, number=7)
    await relay.record_pull_request(**{**row, "head_ref": "renamed"}, number=7)
    await relay.record_pull_request(
        **{**row, "url": "https://github.com/example-org/example-repo/pull/9"}, number=9
    )

    listed = await relay.list_pull_requests(thread_id=job["thread_id"])
    assert [(item["number"], item["head_ref"]) for item in listed] == [
        (9, "fix/one"),
        (7, "fix/one"),
    ]
    assert await relay.list_pull_requests(thread_id="athr_other") == []
