"""Run from a previous release's checkout, retaining its pool across migration."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

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


async def main() -> None:
    """Exercise common conversation operations before and after the upgrade."""
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
