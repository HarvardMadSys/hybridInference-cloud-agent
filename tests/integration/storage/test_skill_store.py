"""Real Postgres ownership, write fencing, and immutable turn selections."""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
import asyncio

import pytest
from tests.integration.storage.test_agent_job_store import (
    _claim,
    _create_job,
    pool,  # noqa: F401 - pytest fixture
    store,  # noqa: F401 - pytest fixture
)

from cloud_agent.skills.models import SkillConflict, SkillError
from cloud_agent.skills.selection import prompt_with_skills, resolve_skills

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
    assert "Selected skills (JSON):\n[]" in prompt_with_skills(
        after_clear["task_prompt"], after_clear["skills"]
    )
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


@pytest.mark.parametrize("skill_ids", [None, []])
async def test_no_skills_preserves_original_worker_prompt_across_turns(store, skill_ids):
    if skill_ids == []:
        # Explicitly opting out must still bypass an enabled account default.
        await create(store, default_enabled=True)
    selection = await resolve_skills(store.skills, user_id="user-1", skill_ids=skill_ids)
    first = await _create_job(store, skills=selection)
    follow = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="Continue", skills=[]
    )
    inherited = await store.conversation_commands.create_follow_up(
        parent_job_id=first["id"], user_id="user-1", prompt="Continue again"
    )
    claim = await _claim(store, worker_id="no-skills-worker", lease_ttl_seconds=60)
    assert claim["id"] == first["id"]
    for turn in (first, follow, inherited, claim):
        assert turn["skills"] is None
        assert prompt_with_skills(turn["task_prompt"], turn["skills"]) == turn["task_prompt"]


async def test_default_toggle_keeps_content_identity_and_fences_stale_writes(store):
    original = await create(store)
    toggled = await store.skills.update_skill(
        user_id="user-1",
        skill_id=original["id"],
        revision=1,
        values={"default_enabled": True},
    )
    assert toggled["revision"] == 2
    assert toggled["content_revision"] == original["content_revision"] == 1
    assert (await resolve_skills(store.skills, user_id="user-1", skill_ids=None))[0][
        "revision"
    ] == 1
    with pytest.raises(SkillConflict):
        await store.skills.update_skill(
            user_id="user-1",
            skill_id=original["id"],
            revision=1,
            values={"default_enabled": False},
        )
    edited = await store.skills.update_skill(
        user_id="user-1",
        skill_id=original["id"],
        revision=2,
        values={"instructions": "Read more carefully."},
    )
    assert edited["revision"] == 3 and edited["content_revision"] == 2
    assert (await resolve_skills(store.skills, user_id="user-1", skill_ids=None))[0][
        "revision"
    ] == 2


async def test_library_and_job_reads_exclude_bodies_while_inheritance_keeps_them(store):
    chosen = await create(store, instructions="中文", default_enabled=True)
    await create(store, "Unselected", instructions="x" * 65536)
    summaries = await store.skills.list_skills(user_id="user-1")
    assert all("instructions" not in row for row in summaries)
    assert summaries[0]["instruction_bytes"] == 6
    selected = await store.skills.select_skills(user_id="user-1", skill_ids=[chosen["id"]])
    assert [row["id"] for row in selected] == [chosen["id"]]
    assert selected[0]["instructions"] == "中文"
    assert await store.skills.select_skills(user_id="user-2", skill_ids=[chosen["id"]]) == []
    snapshots = await resolve_skills(store.skills, user_id="user-1", skill_ids=None)
    job = await _create_job(store, skills=snapshots)
    listed = await store.conversation_queries.list_jobs(user_id="user-1")
    detail = await store.conversation_queries.get_job(job["id"])
    thread = await store.conversation_queries.get_thread_for_job(job_id=job["id"], user_id="user-1")
    for public in [*listed, detail, *thread["jobs"]]:
        assert public["skills"] == [
            {key: value for key, value in snapshots[0].items() if key != "instructions"}
        ]
    follow = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id="user-1",
        prompt="Continue",
    )
    assert follow["skills"] == snapshots


async def test_github_content_immutability_is_enforced_by_storage(store):
    row = await create(store, source="github", source_revision="a" * 40)
    with pytest.raises(SkillError, match=r"^Imported GitHub skills are read-only\.$"):
        await store.skills.update_skill(
            user_id="user-1",
            skill_id=row["id"],
            revision=1,
            values={"instructions": "Spoofed content"},
        )
    assert (await store.skills.get_skill(user_id="user-1", skill_id=row["id"]))[
        "instructions"
    ] == row["instructions"]
