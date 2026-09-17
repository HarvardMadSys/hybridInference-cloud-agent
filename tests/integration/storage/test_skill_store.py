"""Real Postgres ownership, write fencing, and immutable turn selections."""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
import asyncio

import pytest

from cloud_agent.skills.models import SkillConflict, SkillError
from cloud_agent.skills.selection import resolve_skills
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    pool,  # noqa: F401 - pytest fixture
    store,  # noqa: F401 - pytest fixture
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def create(store, name="Review", *, user_id="user-1", **values):
    return await store.skills.create_skill(
        user_id=user_id,
        values={
            "name": name,
            "description": "Review changes",
            "instructions": "Read the diff.",
            "source": "created",
            "default_enabled": False,
            **values,
        },
    )


async def test_owner_scoping_name_uniqueness_and_soft_delete(store):
    row = await create(store)
    await create(store, user_id="user-2")
    assert len(await store.skills.list_skills(user_id="user-1")) == 1
    assert await store.skills.get_skill(user_id="user-2", skill_id=row["id"]) is None
    assert (
        await store.skills.update_skill(
            user_id="user-2", skill_id=row["id"], revision=1, values={"name": "Hijack"}
        )
        is None
    )
    assert not await store.skills.delete_skill(user_id="user-2", skill_id=row["id"], revision=1)
    with pytest.raises(SkillConflict):
        await create(store, "review")
    assert await store.skills.delete_skill(user_id="user-1", skill_id=row["id"], revision=1)
    assert await store.skills.get_skill(user_id="user-1", skill_id=row["id"]) is None
    replacement = await create(store, "REVIEW")
    assert replacement["id"] != row["id"]


async def test_concurrent_edits_have_exactly_one_winner(store):
    row = await create(store)
    results = await asyncio.gather(
        *(
            store.skills.update_skill(
                user_id="user-1", skill_id=row["id"], revision=1, values={"name": name}
            )
            for name in ("First", "Second")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, SkillConflict) for result in results) == 1
    saved = await store.skills.get_skill(user_id="user-1", skill_id=row["id"])
    assert saved["revision"] == 2
    with pytest.raises(SkillConflict):
        await store.skills.delete_skill(user_id="user-1", skill_id=row["id"], revision=1)


async def test_default_count_is_enforced_under_concurrent_writes(store):
    for index in range(15):
        await create(store, f"Skill {index}", default_enabled=True)
    results = await asyncio.gather(
        create(store, "First", default_enabled=True),
        create(store, "Second", default_enabled=True),
        return_exceptions=True,
    )
    assert sum(isinstance(result, SkillError) for result in results) == 1
    assert len(await resolve_skills(store.skills, user_id="user-1", skill_ids=None)) == 16


async def test_default_byte_limit_rolls_back_failed_update(store):
    await create(store, "Large", instructions="a" * 65536, default_enabled=True)
    second = await create(store, "Second", instructions="b" * 65536, default_enabled=True)
    third = await create(store, "Third")
    with pytest.raises(SkillError, match="128 KB"):
        await store.skills.update_skill(
            user_id="user-1",
            skill_id=third["id"],
            revision=1,
            values={"default_enabled": True},
        )
    unchanged = await store.skills.get_skill(user_id="user-1", skill_id=third["id"])
    assert unchanged["revision"] == 1 and not unchanged["default_enabled"]
    await store.skills.delete_skill(user_id="user-1", skill_id=second["id"], revision=1)
    assert (
        await store.skills.update_skill(
            user_id="user-1", skill_id=third["id"], revision=1, values={"default_enabled": True}
        )
    )["default_enabled"]


async def test_saved_turns_survive_edits_deletion_and_inherit_latest_selection(store):
    skill = await create(store, default_enabled=True)
    original = await resolve_skills(store.skills, user_id="user-1", skill_ids=None)
    first = await _create_job(store, skills=original)
    await store.skills.update_skill(
        user_id="user-1",
        skill_id=skill["id"],
        revision=1,
        values={"instructions": "New instructions."},
    )
    latest = await resolve_skills(store.skills, user_id="user-1", skill_ids=[skill["id"]])
    assert latest[0]["revision"] == 2
    follow = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="Continue"
    )
    assert follow["skills"] == original
    updated = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="Use the update", skills=latest
    )
    assert updated["skills"] == latest
    await store.skills.delete_skill(user_id="user-1", skill_id=skill["id"], revision=2)
    inherited = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="Continue after deletion"
    )
    assert inherited["skills"] == latest
    assert inherited["parent_job_id"] == updated["id"]
    cleared = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="Clear", skills=[]
    )
    after_clear = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="Still cleared"
    )
    assert cleared["skills"] == after_clear["skills"] == []
    claim = await _claim(store, worker_id="skills-worker", lease_ttl_seconds=60)
    assert claim["id"] == first["id"]
    assert claim["skills"] == original
    assert claim["task_prompt"] == "fix the flaky test"
    assert original[0]["instructions"] == "Read the diff."


async def test_legacy_jobs_and_followups_keep_null_selection(store):
    first = await _create_job(store)
    follow = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="Continue"
    )
    assert first["skills"] is None and follow["skills"] is None
