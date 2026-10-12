"""The memory split preserves populated legacy profiles and privacy fences."""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timezone

import asyncpg
import pytest
from alembic import command
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    pool as pool,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import _alembic_config, apply_migrations, downgrade_to

from cloud_agent.storage.repositories.user_memory import _apply_memory_edits
from cloud_agent.user_memory import build_project_memory, build_user_memory

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


def _offline_sql(config: dict, *, downgrade: bool = False) -> str:
    output = io.StringIO()
    alembic_config = _alembic_config(config)
    alembic_config.output_buffer = output
    if downgrade:
        command.downgrade(alembic_config, "0053_project_memory:0052_memory_overrides", sql=True)
    else:
        command.upgrade(alembic_config, "0052_memory_overrides:0053_project_memory", sql=True)
    # The caller supplies the transaction, matching the atomic legacy copy path.
    return re.sub(r"^(?:BEGIN|COMMIT);\s*$", "", output.getvalue(), flags=re.MULTILINE)


def _memory(memory_id: str, repo: str | None, **extra) -> dict:
    return {
        "id": memory_id,
        "kind": "preference",
        "content": "I prefer readable tests.",
        "source_job_id": f"job-{memory_id}",
        "source_repo": repo,
        **extra,
    }


async def test_project_memory_upgrade_preserves_owner_scope_controls_and_round_trip(store, pool):
    await _create_job(store, user_id="alice", repo="org/live")
    await _create_job(store, user_id="alice", repo="org/live")
    deleted = await _create_job(store, user_id="alice", repo="org/deleted-history")
    await _create_job(store, user_id="bob", repo="org/live")
    await _create_job(store, user_id="charlie", repo="org/no-profile")
    config = _db_config()
    fence_time = datetime(2026, 10, 1, tzinfo=timezone.utc)
    legacy_memories = [
        _memory("alice-live", "org/live"),
        _memory("alice-task", "org/live", kind="task"),
        _memory("alice-orphan", "org/orphan"),
        _memory("alice-global", None),
        _memory("alice-empty-repo", ""),
        _memory("alice-explicit-user", "org/live", scope="user"),
        _memory("alice-global-wording", "org/live", content="I prefer tests across all projects."),
    ]
    portrait = {
        "summary": "Aggregate legacy portrait.",
        "projects": ["org/live", "org/portrait-only"],
        "focus_areas": ["Testing"],
        "preferred_runtime": "codex",
        "preferred_model": "gpt-6",
    }
    suggestions = [{"id": "legacy", "title": "Verify", "reason": "Tests", "action": "Run tests"}]
    try:
        downgrade_to(config, "0052_memory_overrides")
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_threads SET deleted_at = $2 WHERE id = $1",
                deleted["thread_id"],
                fence_time,
            )
            fence_id = await conn.fetchval("SELECT max(id) FROM agent_thread_messages")
            await conn.execute(
                "INSERT INTO agent_user_memories "
                "(user_id, enabled, portrait, memories, suggestions, activity_digest, "
                "cleared_message_id, cleared_at, revision, created_at, updated_at) "
                "VALUES ('alice', FALSE, $1::jsonb, $2::jsonb, $3::jsonb, 'legacy-digest', "
                "$4, $5, 7, $5, $5)",
                json.dumps(portrait),
                json.dumps(legacy_memories),
                json.dumps(suggestions),
                fence_id,
                fence_time,
            )
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, portrait, memories, activity_digest) "
                "VALUES ('bob', '{}'::jsonb, $1::jsonb, 'bob-digest')",
                json.dumps([_memory("bob-live", "org/live")]),
            )
            sources = {
                table: [dict(row) for row in await conn.fetch(f"SELECT * FROM {table} ORDER BY id")]
                for table in ("agent_jobs", "agent_threads", "agent_thread_messages")
            }

        for _ in range(2):
            apply_migrations(config, "0053_project_memory")
            async with pool.acquire() as conn:
                versions = await conn.fetch("SELECT version_num FROM alembic_version")
                assert {row["version_num"] for row in versions} == {
                    "0046_admin_sandbox_trajectory",
                    "0053_project_memory",
                }
                rows = await conn.fetch(
                    "SELECT * FROM agent_project_memories ORDER BY user_id, repo"
                )
                projects = {(row["user_id"], row["repo"]): dict(row) for row in rows}
                assert set(projects) == {
                    ("alice", "org/live"),
                    ("alice", "org/deleted-history"),
                    ("alice", "org/orphan"),
                    ("alice", "org/portrait-only"),
                    ("bob", "org/live"),
                    ("charlie", "org/no-profile"),
                }
                for (owner, repo), row in projects.items():
                    assert row["activity_digest"] == ""
                    assert json.loads(row["portrait"])["projects"] == [repo]
                    assert json.loads(row["portrait"])["focus_areas"] == []
                    assert json.loads(row["suggestions"]) == []
                    if owner == "alice":
                        assert row["enabled"] is False
                        assert row["cleared_message_id"] == fence_id
                        assert row["cleared_at"] == fence_time
                        assert row["revision"] == 7
                        assert row["created_at"] == row["updated_at"] == fence_time
                    else:
                        assert row["enabled"] is True
                        assert row["cleared_message_id"] == row["revision"] == 0
                        assert row["cleared_at"] is None
                    for memory in json.loads(row["memories"]):
                        assert memory["scope"] == "repo"
                        assert memory["source_repo"] == repo
                assert {
                    memory["id"] for memory in json.loads(projects["alice", "org/live"]["memories"])
                } == {"alice-live", "alice-task", "alice-global-wording"}
                assert json.loads(projects["alice", "org/orphan"]["memories"]) == [
                    {**legacy_memories[2], "scope": "repo"}
                ]
                assert json.loads(projects["alice", "org/deleted-history"]["memories"]) == []
                assert json.loads(projects["alice", "org/portrait-only"]["memories"]) == []
                assert json.loads(projects["bob", "org/live"]["memories"]) == [
                    {**_memory("bob-live", "org/live"), "scope": "repo"}
                ]
                user = await conn.fetchrow(
                    "SELECT * FROM agent_user_memories WHERE user_id = 'alice'"
                )
                assert user["enabled"] is False
                assert user["cleared_message_id"] == fence_id
                assert user["cleared_at"] == fence_time
                assert user["revision"] == 7
                assert json.loads(user["portrait"]) == portrait
                assert json.loads(user["suggestions"]) == suggestions
                assert user["activity_digest"] == ""
                assert json.loads(user["memories"]) == [
                    {**memory, "scope": "user"} for memory in legacy_memories[3:6]
                ]
                defaults = await conn.fetchrow(
                    "INSERT INTO agent_project_memories (user_id, repo, portrait) "
                    "VALUES ('defaults', 'org/defaults', '{}'::jsonb) RETURNING *"
                )
                assert defaults["enabled"] is True
                assert defaults["cleared_message_id"] == defaults["revision"] == 0
                assert defaults["cleared_at"] is None
                assert defaults["activity_digest"] == ""
                assert json.loads(defaults["overrides"]) == []
                assert json.loads(defaults["memories"]) == json.loads(defaults["suggestions"]) == []
                assert defaults["created_at"] == defaults["updated_at"]
                for column in ("revision", "cleared_message_id"):
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with conn.transaction():
                            await conn.execute(
                                f"UPDATE agent_project_memories SET {column} = -1 "
                                "WHERE user_id = 'defaults' AND repo = 'org/defaults'"
                            )
                with pytest.raises(asyncpg.UniqueViolationError):
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO agent_project_memories (user_id, repo, portrait) "
                            "VALUES ('alice', 'org/live', '{}'::jsonb)"
                        )
                await conn.execute("DELETE FROM agent_project_memories WHERE user_id = 'defaults'")

            downgrade_to(config, "0052_memory_overrides")
            async with pool.acquire() as conn:
                assert await conn.fetchval("SELECT to_regclass('agent_project_memories')") is None
                user = await conn.fetchrow(
                    "SELECT * FROM agent_user_memories WHERE user_id = 'alice'"
                )
                assert {memory["id"] for memory in json.loads(user["memories"])} == {
                    memory["id"] for memory in legacy_memories
                }
                assert user["enabled"] is False
                assert user["cleared_message_id"] == fence_id
                assert user["cleared_at"] == fence_time
                assert json.loads(user["portrait"]) == portrait
                for table, original in sources.items():
                    assert [
                        dict(row) for row in await conn.fetch(f"SELECT * FROM {table} ORDER BY id")
                    ] == original
    finally:
        apply_migrations(config)


async def test_project_memory_split_preserves_edits_and_source_backed_overrides(store, pool):
    first_repo = "org/first"
    second_repo = "org/second"
    first_sources = [
        "I prefer single-entry corrections.",
        "I prefer forgotten single-entry preferences.",
        "I prefer small tests.",
        "I prefer focused checks.",
        "I prefer clear conversations.",
        "I prefer forgotten bulk preferences.",
    ]
    second_sources = [
        "I prefer direct conversations.",
        "I prefer forgotten second-repo preferences.",
    ]
    first = await _create_job(
        store, user_id="alice", repo=first_repo, task_prompt=" ".join(first_sources)
    )
    second = await _create_job(
        store, user_id="alice", repo=second_repo, task_prompt=" ".join(second_sources)
    )
    other_source = "I prefer another owner's confidential convention."
    other = await _create_job(store, user_id="bob", repo=first_repo, task_prompt=other_source)

    def key(content: str, repo: str) -> str:
        normalized = content.casefold().rstrip(".!?… ")
        return hashlib.sha256(f"{repo}:preference:{normalized}".encode()).hexdigest()[:20]

    first_ids = [key(content, first_repo) for content in first_sources]
    second_ids = [key(content, second_repo) for content in second_sources]
    single = _memory(
        first_ids[0],
        first_repo,
        scope="repo",
        source_job_id=first["id"],
        content="Use readable pytest cases.",
        user_edited=True,
    )
    consolidated = _memory(
        "consolidated-first",
        first_repo,
        scope="repo",
        source_job_id=first["id"],
        content="Use focused tests and checks.",
        edited=True,
        source_ids=first_ids[2:4],
    )
    user_memory = _memory(
        "consolidated-user",
        None,
        scope="user",
        source_job_id=first["id"],
        content="Keep communication clear and direct.",
        edited=True,
        source_ids=[first_ids[4], second_ids[0]],
    )
    wrong_owner = _memory(
        "wrong-owner",
        first_repo,
        scope="repo",
        source_job_id=other["id"],
        source_ids=[key(other_source, first_repo)],
        content="Unowned source must stay isolated.",
    )
    overrides = [
        {"source_ids": first_ids[2:4], "memory": consolidated},
        {"source_ids": user_memory["source_ids"], "memory": user_memory},
        {"source_ids": [first_ids[5]], "memory": None},
        {"source_ids": [second_ids[1]], "memory": None},
        {"source_ids": wrong_owner["source_ids"], "memory": wrong_owner},
    ]
    native_overlay = {
        "source_ids": [first_ids[0]],
        "memory": {
            **single,
            "content": first_sources[0],
            "edited": False,
            "user_edited": False,
            "source_ids": [first_ids[0]],
        },
        "native_source_job_id": first["id"],
    }
    config = _db_config()
    try:
        downgrade_to(config, "0052_memory_overrides")
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", second["thread_id"]
            )
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, enabled, portrait, memories, overrides) "
                "VALUES ('alice', FALSE, '{}'::jsonb, $1::jsonb, $2::jsonb)",
                json.dumps([single, consolidated, user_memory]),
                json.dumps(overrides),
            )
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, portrait) VALUES ('bob', '{}'::jsonb)"
            )
            for memory_id, content in ((first_ids[0], single["content"]), (first_ids[1], None)):
                await conn.execute(
                    "INSERT INTO agent_user_memory_edits (user_id, memory_id, source_job_id, content) "
                    "VALUES ('alice', $1, $2, $3)",
                    memory_id,
                    first["id"],
                    content,
                )
            await conn.execute(
                "INSERT INTO agent_user_memory_edits (user_id, memory_id, source_job_id, content) "
                "VALUES ('alice', $1, $2, $3)",
                user_memory["id"],
                first["id"],
                user_memory["content"],
            )
            await conn.execute(
                "INSERT INTO agent_user_memory_edits (user_id, memory_id, source_job_id, content) "
                "VALUES ('bob', $1, $2, 'Bob correction')",
                first_ids[0],
                other["id"],
            )
            source_snapshot = [
                dict(row)
                for row in await conn.fetch("SELECT * FROM agent_thread_messages ORDER BY id")
            ]

        apply_migrations(config, "0053_project_memory")
        async with pool.acquire() as conn:
            project_rows = await conn.fetch(
                "SELECT repo, memories, overrides, enabled FROM agent_project_memories "
                "WHERE user_id = 'alice' ORDER BY repo"
            )
            projects = {row["repo"]: dict(row) for row in project_rows}
            assert projects[first_repo]["enabled"] is projects[second_repo]["enabled"] is False
            assert json.loads(projects[first_repo]["memories"]) == [single, consolidated]
            assert json.loads(projects[second_repo]["memories"]) == []
            assert json.loads(projects[first_repo]["overrides"]) == [
                overrides[0],
                {"source_ids": [first_ids[4]], "memory": None},
                overrides[2],
                native_overlay,
            ]
            assert json.loads(projects[second_repo]["overrides"]) == [
                {"source_ids": [second_ids[0]], "memory": None},
                overrides[3],
            ]
            user = await conn.fetchrow(
                "SELECT memories, overrides FROM agent_user_memories WHERE user_id = 'alice'"
            )
            assert json.loads(user["memories"]) == [user_memory]
            assert json.loads(user["overrides"]) == [*overrides, native_overlay]
            edits = await conn.fetch(
                "SELECT user_id, repo, memory_id, content FROM agent_project_memory_edits"
            )
            project_edits = {
                (row["user_id"], row["repo"], row["memory_id"]): row["content"] for row in edits
            }
            assert project_edits == {
                ("alice", first_repo, first_ids[0]): single["content"],
                ("alice", first_repo, first_ids[1]): None,
                ("alice", first_repo, user_memory["id"]): None,
                ("bob", first_repo, first_ids[0]): "Bob correction",
            }
            alice_edits = await conn.fetch(
                "SELECT memory_id, content FROM agent_user_memory_edits WHERE user_id = 'alice'"
            )
            assert {row["memory_id"]: row["content"] for row in alice_edits} == {
                first_ids[1]: None,
                user_memory["id"]: user_memory["content"],
            }
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute("UPDATE agent_project_memory_edits SET content = ''")
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO agent_project_memory_edits (user_id, repo, memory_id, source_job_id) "
                        "VALUES ('alice', 'org/not-owned', 'invalid', $1)",
                        first["id"],
                    )

        downgrade_to(config, "0052_memory_overrides")
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT to_regclass('agent_project_memory_edits')") is None
            restored = await conn.fetchrow(
                "SELECT memories, overrides FROM agent_user_memories WHERE user_id = 'alice'"
            )
            assert {memory["id"] for memory in json.loads(restored["memories"])} == {
                single["id"],
                consolidated["id"],
                user_memory["id"],
            }
            assert all(override in json.loads(restored["overrides"]) for override in overrides)
            activity = [
                {"job_id": first["id"], "repo": first_repo, "content": " ".join(first_sources)},
                {"job_id": second["id"], "repo": second_repo, "content": " ".join(second_sources)},
            ]
            restored_overrides = json.loads(restored["overrides"])
            regenerated = build_project_memory(activity, None, first_repo, restored_overrides)
            assert [memory["id"] for memory in regenerated["memories"]].count(
                consolidated["id"]
            ) == 1
            assert not any(memory["id"] == first_ids[5] for memory in regenerated["memories"])
            user_regenerated = build_user_memory(activity, None, restored_overrides)
            assert [memory["id"] for memory in user_regenerated["memories"]].count(
                user_memory["id"]
            ) == 1
            edits = await conn.fetch(
                "SELECT memory_id, content FROM agent_user_memory_edits WHERE user_id = 'alice'"
            )
            assert {row["memory_id"]: row["content"] for row in edits} == {
                first_ids[0]: single["content"],
                first_ids[1]: None,
                user_memory["id"]: user_memory["content"],
            }
            assert [
                dict(row)
                for row in await conn.fetch("SELECT * FROM agent_thread_messages ORDER BY id")
            ] == source_snapshot
        # A second split must preserve one replacement per source decision.
        apply_migrations(config, "0053_project_memory")
        async with pool.acquire() as conn:
            restored_project = json.loads(
                await conn.fetchval(
                    "SELECT overrides FROM agent_project_memories WHERE user_id = 'alice' AND repo = $1",
                    first_repo,
                )
            )
            refreshed = build_project_memory(activity[:1], None, first_repo, restored_project)
            assert [memory["id"] for memory in refreshed["memories"]].count(consolidated["id"]) == 1
    finally:
        apply_migrations(config)


@pytest.mark.parametrize(
    ("visible", "scope", "kind"),
    [
        (True, "repo", "preference"),
        (False, "repo", "preference"),
        (True, "user", "preference"),
        (False, "repo", "task"),
    ],
)
async def test_project_memory_migrates_native_correction_scope_before_reclassification(
    store, pool, visible, scope, kind
):
    repo = "org/first"
    source = "I prefer concise reviews across all projects."
    job = await _create_job(store, user_id="alice", repo=repo, task_prompt=source)
    async with pool.acquire() as conn:
        title = await conn.fetchval(
            "SELECT title FROM agent_threads WHERE id = $1", job["thread_id"]
        )
    content = source if kind == "preference" else title
    normalized = content.casefold().rstrip(".!?… ")
    memory_id = hashlib.sha256(f"{repo}:{kind}:{normalized}".encode()).hexdigest()[:20]
    correction = "Use short summaries."
    saved = _memory(
        memory_id,
        repo,
        scope=scope,
        kind=kind,
        source_job_id=job["id"],
        content=correction,
        user_edited=True,
    )
    config = _db_config()
    try:
        downgrade_to(config, "0052_memory_overrides")
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, portrait, memories) "
                "VALUES ('alice', '{}'::jsonb, $1::jsonb)",
                json.dumps([saved] if visible else []),
            )
            await conn.execute(
                "INSERT INTO agent_user_memory_edits (user_id, memory_id, source_job_id, content) "
                "VALUES ('alice', $1, $2, $3)",
                memory_id,
                job["id"],
                correction,
            )
        apply_migrations(config, "0053_project_memory")
        user = await store.user_memory.refresh("alice")
        project = await store.user_memory.refresh_project("alice", repo)
        target = user if scope == "user" else project
        matches = [memory for memory in target["memories"] if memory["id"] == memory_id]
        assert len(matches) == 1
        assert matches[0]["content"] == correction
        assert matches[0]["scope"] == scope
        assert matches[0]["kind"] == kind
        other = project if scope == "user" else user
        if kind == "preference":
            assert not any(memory["id"] == memory_id for memory in other["memories"])
            assert not any(
                memory["content"] == source for memory in user["memories"] + project["memories"]
            )
        async with pool.acquire() as conn:
            overlays = json.loads(
                await conn.fetchval(
                    "SELECT overrides FROM agent_user_memories WHERE user_id = 'alice'"
                )
            )
            native = next(item for item in overlays if item["source_ids"] == [memory_id])
            assert native["memory"]["source_job_id"] == job["id"]
            assert native["memory"]["kind"] == kind
            assert native["memory"]["scope"] == scope
            assert native["memory"]["content"] == content
            assert native["native_source_job_id"] == job["id"]
        if scope == "user":
            await store.user_memory.delete_entry(
                "alice", memory_id, expected_revision=target["revision"]
            )
        else:
            await store.user_memory.delete_project_entry(
                "alice", repo, memory_id, expected_revision=target["revision"]
            )
        user = await store.user_memory.refresh("alice")
        project = await store.user_memory.refresh_project("alice", repo)
        assert not any(
            memory["id"] == memory_id for memory in user["memories"] + project["memories"]
        )
    finally:
        apply_migrations(config)


@pytest.mark.parametrize("duplicate_before_migration", [False, True])
async def test_migrated_native_correction_does_not_relink_to_new_duplicate_job(
    store, pool, duplicate_before_migration
):
    repo = "org/first"
    source = "I prefer concise reviews across all projects."
    first = await _create_job(store, user_id="alice", repo=repo, task_prompt=source)
    second = (
        await _create_job(store, user_id="alice", repo=repo, task_prompt=source)
        if duplicate_before_migration
        else None
    )
    memory_id = hashlib.sha256(
        f"{repo}:preference:{source.casefold().rstrip('.')}".encode()
    ).hexdigest()[:20]
    correction = "Use short summaries."
    saved = _memory(
        memory_id,
        repo,
        source_job_id=(second or first)["id"],
        scope="repo",
        content=source if second else correction,
        user_edited=second is None,
    )
    config = _db_config()
    try:
        downgrade_to(config, "0052_memory_overrides")
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, portrait, memories) "
                "VALUES ('alice', '{}'::jsonb, $1::jsonb)",
                json.dumps([saved]),
            )
            await conn.execute(
                "INSERT INTO agent_user_memory_edits (user_id, memory_id, source_job_id, content) "
                "VALUES ('alice', $1, $2, $3)",
                memory_id,
                first["id"],
                correction,
            )
        apply_migrations(config, "0053_project_memory")
        if second is None:
            second = await _create_job(store, user_id="alice", repo=repo, task_prompt=source)
        user = await store.user_memory.refresh("alice")
        project = await store.user_memory.refresh_project("alice", repo)
        combined = user["memories"] + project["memories"]
        assert not any(memory["content"] == correction for memory in combined)
        raw = next(memory for memory in combined if memory["id"] == memory_id)
        assert raw["content"] == source
        assert raw["source_job_id"] == second["id"]
        downgrade_to(config, "0052_memory_overrides")
        async with pool.acquire() as conn:
            overrides = json.loads(
                await conn.fetchval(
                    "SELECT overrides FROM agent_user_memories WHERE user_id = 'alice'"
                )
            )
            assert not any(item.get("native_source_job_id") for item in overrides)
            edits = [
                dict(row)
                for row in await conn.fetch(
                    "SELECT memory_id, source_job_id, content FROM agent_user_memory_edits WHERE user_id = 'alice'"
                )
            ]
            assert edits == [
                {"memory_id": memory_id, "source_job_id": first["id"], "content": correction}
            ]
        assert _apply_memory_edits([raw], edits)[0]["content"] == source
    finally:
        apply_migrations(config)


async def test_project_memory_downgrade_retains_stricter_divergent_privacy_controls(pool):
    config = _db_config()
    fence_time = datetime(2026, 10, 2, tzinfo=timezone.utc)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, portrait) VALUES ('alice', '{}'::jsonb)"
            )
            await conn.execute(
                "INSERT INTO agent_project_memories "
                "(user_id, repo, enabled, portrait, cleared_message_id, cleared_at, revision) "
                "VALUES ('alice', 'org/private', FALSE, '{}'::jsonb, 42, $1, 3)",
                fence_time,
            )
            await conn.execute(
                "INSERT INTO agent_project_memories "
                "(user_id, repo, enabled, portrait, cleared_message_id, cleared_at) "
                "VALUES ('project-only', 'org/private', FALSE, '{}'::jsonb, 43, $1)",
                fence_time,
            )
        downgrade_to(config, "0052_memory_overrides")
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM agent_user_memories ORDER BY user_id")
            users = {row["user_id"]: dict(row) for row in rows}
            assert users["alice"]["enabled"] is False
            assert users["alice"]["cleared_message_id"] == 42
            assert users["alice"]["cleared_at"] == fence_time
            assert users["alice"]["revision"] == 3
            assert users["project-only"]["enabled"] is False
            assert users["project-only"]["cleared_message_id"] == 43
            assert users["project-only"]["cleared_at"] == fence_time
    finally:
        apply_migrations(config)


@pytest.mark.parametrize("newer_scope", ["user", "repo"])
@pytest.mark.parametrize("clear_target", [False, True])
async def test_project_memory_downgrade_keeps_canonical_decision_after_other_scope_refresh(
    store, pool, newer_scope, clear_target
):
    source = "I prefer readable tests."
    repo = "org/first"
    job = await _create_job(store, user_id="alice", repo=repo, task_prompt=source)
    source_id = hashlib.sha256(
        f"{repo}:preference:{source.casefold().rstrip('.')}".encode()
    ).hexdigest()[:20]
    user_memory = _memory(
        "user-correction",
        repo,
        scope="user" if newer_scope == "user" else "repo",
        edited=True,
        source_job_id=job["id"],
        content="Keep updates concise.",
        source_ids=[source_id],
    )
    project_memory = _memory(
        "project-correction",
        repo,
        scope="repo" if newer_scope == "repo" else "user",
        edited=True,
        source_job_id=job["id"],
        content="Use readable pytest cases.",
        source_ids=[source_id],
    )
    old_time = datetime(2026, 10, 1, tzinfo=timezone.utc)
    config = _db_config()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, portrait, overrides, updated_at) "
                "VALUES ('alice', '{}'::jsonb, $1::jsonb, $2)",
                json.dumps([{"source_ids": [source_id], "memory": user_memory}]),
                old_time,
            )
            await conn.execute(
                "INSERT INTO agent_project_memories (user_id, repo, portrait, overrides, updated_at) "
                "VALUES ('alice', $1, '{}'::jsonb, $2::jsonb, $3)",
                repo,
                json.dumps([{"source_ids": [source_id], "memory": project_memory}]),
                old_time,
            )
        # A background refresh advances updated_at without changing its hidden
        # overlay. The canonical edit in the other scope must still win.
        if newer_scope == "repo":
            await store.user_memory.refresh("alice")
        else:
            await store.user_memory.refresh_project("alice", repo)
        async with pool.acquire() as conn:
            hidden_updated_at = await conn.fetchval(
                "SELECT updated_at FROM agent_user_memories WHERE user_id = 'alice'"
                if newer_scope == "repo"
                else "SELECT updated_at FROM agent_project_memories WHERE user_id = 'alice' AND repo = $1",
                *([] if newer_scope == "repo" else [repo]),
            )
            assert hidden_updated_at > old_time
        if clear_target:
            if newer_scope == "repo":
                await store.user_memory.clear_project("alice", repo)
            else:
                await store.user_memory.clear("alice")
        downgrade_to(config, "0052_memory_overrides")
        async with pool.acquire() as conn:
            overlays = json.loads(
                await conn.fetchval(
                    "SELECT overrides FROM agent_user_memories WHERE user_id = 'alice'"
                )
            )
        expected = user_memory if newer_scope == "user" else project_memory
        assert overlays == [
            {"source_ids": [source_id], "memory": None if clear_target else expected}
        ]
        activity = [{"job_id": job["id"], "repo": repo, "content": source}]
        regenerated_user = build_user_memory(activity, None, overlays)
        regenerated_project = build_project_memory(activity, None, repo, overlays)
        combined = regenerated_user["memories"] + regenerated_project["memories"]
        assert [memory["id"] for memory in combined] == ([] if clear_target else [expected["id"]])
        if not clear_target:
            assert combined[0]["content"] == expected["content"]
    finally:
        apply_migrations(config)


@pytest.mark.parametrize("populated", ["native", "override", "malformed"])
async def test_project_memory_offline_upgrade_refuses_populated_overlay_data(pool, populated):
    config = _db_config()
    rendered = _offline_sql(config)
    try:
        downgrade_to(config, "0052_memory_overrides")
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, portrait, overrides) "
                "VALUES ('alice', '{}'::jsonb, $1::jsonb)",
                "[]"
                if populated == "native"
                else "{}"
                if populated == "malformed"
                else '[{"source_ids":["legacy-source"],"memory":null}]',
            )
            if populated == "native":
                await conn.execute(
                    "INSERT INTO agent_user_memory_edits (user_id, memory_id, source_job_id, content) "
                    "VALUES ('alice', 'legacy-source', 'legacy-job', NULL)"
                )
            with pytest.raises(asyncpg.RaiseError, match="requires online Alembic upgrade"):
                async with conn.transaction():
                    await conn.execute(rendered)
            assert await conn.fetchval("SELECT to_regclass('agent_project_memories')") is None
            versions = await conn.fetch("SELECT version_num FROM alembic_version")
            assert {row["version_num"] for row in versions} == {
                "0046_admin_sandbox_trajectory",
                "0052_memory_overrides",
            }
            # Leave a schema-valid profile for the normal online cleanup.
            await conn.execute("UPDATE agent_user_memories SET overrides = '[]'::jsonb")
    finally:
        apply_migrations(config)


async def test_project_memory_offline_empty_upgrade_and_downgrade_preserve_privacy(pool):
    config = _db_config()
    upgrade_sql = _offline_sql(config)
    downgrade_sql = _offline_sql(config, downgrade=True)
    try:
        downgrade_to(config, "0052_memory_overrides")
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_user_memories (user_id, enabled, portrait, cleared_message_id) "
                "VALUES ('alice', FALSE, '{\"projects\":[\"org/legacy\"]}'::jsonb, 42)"
            )
            async with conn.transaction():
                await conn.execute(upgrade_sql)
            project = await conn.fetchrow(
                "SELECT enabled, cleared_message_id FROM agent_project_memories WHERE user_id = 'alice'"
            )
            assert project["enabled"] is False
            assert project["cleared_message_id"] == 42
            await conn.execute(
                'UPDATE agent_project_memories SET overrides = \'[{"source_ids":["legacy"],"memory":null}]\'::jsonb'
            )
            with pytest.raises(asyncpg.RaiseError, match="requires online Alembic downgrade"):
                async with conn.transaction():
                    await conn.execute(downgrade_sql)
            assert await conn.fetchval("SELECT to_regclass('agent_project_memories')") is not None
            await conn.execute("UPDATE agent_project_memories SET overrides = '[]'::jsonb")
            async with conn.transaction():
                await conn.execute(downgrade_sql)
            assert await conn.fetchval("SELECT to_regclass('agent_project_memories')") is None
            user = await conn.fetchrow(
                "SELECT enabled, cleared_message_id FROM agent_user_memories WHERE user_id = 'alice'"
            )
            assert user["enabled"] is False
            assert user["cleared_message_id"] == 42
    finally:
        apply_migrations(config)
