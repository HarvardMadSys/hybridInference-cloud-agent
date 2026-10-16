"""Team environments on real Postgres: who may change them, use them, and lose them.

A team's environment is changed by the team's owner and admins and used by
every active member for their own tasks, in their own containers. A member's
personal globals join it at run time only, so its installation, and the
snapshot of it, is the same for everyone. A member who leaves loses it: it
drops out of their lists and defaults, and their chats bound to it stop.
"""

from __future__ import annotations

# ruff: noqa: F811 -- imported pytest fixtures remain parameters
import pytest
from tests.integration.storage.test_agent_job_store import pool, store  # noqa: F401
from tests.integration.storage.test_environment_snapshot_store import (
    IMAGE,
    _claim,
    _claim_task,
    _enable_archives,
    _host,
    _receipt,
)
from tests.integration.storage.test_team_store import join, new_team

from cloud_agent.environments import (
    EnvironmentAccessRevoked,
    EnvironmentForbidden,
    EnvironmentNotFound,
)
from cloud_agent_shared.environment import EnvironmentScripts
from cloud_agent_shared.environment_snapshots import owner_hash, snapshot_object_key

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

REPO = "org/app"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "team-environment-test-key")


async def save(store, actor, *, team_id=None, environment_id=None, revision=0, **over):
    return await store.environments.save_environment(
        actor,
        repo=over.pop("repo", REPO),
        name=over.pop("name", "Shared dev"),
        environment_id=environment_id,
        revision=revision,
        document=EnvironmentScripts(setup_script=over.pop("script", "npm ci")).model_dump(),
        edits=over.pop(
            "variables",
            [
                {"name": "SERVICE_TOKEN", "value": "team-service-token", "secret": True},
                {"name": "APP_ENV", "value": "development", "secret": False},
            ],
        ),
        team_id=team_id,
    )


async def globals_for(store, actor, variables):
    return await store.environments.save_environment(
        actor,
        repo=None,
        name="Global",
        environment_id=None,
        revision=0,
        document=EnvironmentScripts().model_dump(),
        edits=variables,
    )


async def bind(store, pool, actor, environment_id=None, *, provided=True):
    selection = {"environment_id": environment_id, "environment_id_provided": provided}
    job = await store.conversation_commands.create_job(
        user_id=actor,
        repo=REPO,
        task_prompt="work",
        runtime="claude-code",
        model="test-model",
        environment_selection=selection,
    )
    async with pool.acquire() as conn:
        binding = await conn.fetchval(
            "SELECT environment_binding_id FROM agent_threads WHERE id=$1", job["thread_id"]
        )
    return job, binding


async def team_with_people(store):
    """alice owns it, carol is an admin, bob is a member; dave is outside."""
    team = await new_team(store)
    await join(store, team.id, "bob")
    await join(store, team.id, "carol")
    revision = (await store.teams.get_team("alice", team.id)).revision
    await store.teams.set_member_role(
        "alice", team.id, "carol", role="admin", expected_revision=revision
    )
    return team


async def test_owners_and_admins_change_it_and_every_member_uses_it(store, pool):
    team = await team_with_people(store)
    env = await save(store, "alice", team_id=team.id)

    assert env["team"] == {"id": team.id, "name": "Platform"}
    by_member = {
        actor: list(await store.environments.list_environments(actor, REPO))
        for actor in ("alice", "bob", "carol", "dave")
    }
    assert [e["id"] for e in by_member["bob"]] == [env["id"]]
    assert by_member["bob"][0]["can_edit"] is False
    assert by_member["carol"][0]["can_edit"] is True
    assert by_member["alice"][0]["can_edit"] is True
    assert by_member["dave"] == []
    # A Secret's value is never read back, by the team or anyone else.
    assert {v["name"]: v["value"] for v in by_member["bob"][0]["variables"]} == {
        "APP_ENV": "development",
        "SERVICE_TOKEN": None,
    }

    with pytest.raises(EnvironmentForbidden):
        await save(store, "bob", environment_id=env["id"], revision=1)
    with pytest.raises(EnvironmentForbidden):
        await save(store, "bob", team_id=team.id, name="Mine")
    with pytest.raises(EnvironmentNotFound):
        await store.environments.get_environment("dave", env["id"])
    with pytest.raises(EnvironmentNotFound):
        await save(store, "dave", team_id=team.id, name="Intruder")
    edited = await save(
        store, "carol", environment_id=env["id"], revision=1, script="npm ci && make"
    )
    assert edited["revision"] == 2 and edited["setup_script"] == "npm ci && make"

    # A personal environment may share the team's name; the team's names are its own.
    personal = await save(store, "bob", name="Shared dev")
    assert personal["team"] is None and personal["can_edit"] is True
    async with pool.acquire() as conn:
        actions = [
            row["action"]
            for row in await conn.fetch(
                "SELECT action FROM agent_team_audit_events WHERE team_id=$1 AND target_id=$2 "
                "ORDER BY id",
                team.id,
                env["id"],
            )
        ]
    assert actions == ["environment_created", "environment_updated"]


async def test_a_members_own_variables_join_at_run_time_and_install_is_shared(store, pool):
    team = await team_with_people(store)
    env = await save(store, "alice", team_id=team.id)
    await globals_for(
        store,
        "bob",
        [
            {"name": "MY_KEY", "value": "bob-private-key", "secret": True},
            {"name": "INSTALL_ONLY", "value": "bob-setup", "secret": False, "phase": "setup"},
            {"name": "APP_ENV", "value": "bob-local", "secret": False},
        ],
    )
    await globals_for(
        store, "alice", [{"name": "ALICE_KEY", "value": "alice-private-key", "secret": True}]
    )

    _, bob_binding = await bind(store, pool, "bob", env["id"])
    _, alice_binding = await bind(store, pool, "alice", env["id"])
    bound = await store.environments.get_environment_binding("bob", bob_binding)

    variables = {v.name: (v.value, v.phase, v.source) for v in bound.variables}
    assert variables["MY_KEY"] == ("bob-private-key", "runtime", "global")
    assert "INSTALL_ONLY" not in variables
    # The team's definition of a name wins over the member's.
    assert variables["APP_ENV"] == ("development", "both", "environment")
    assert variables["SERVICE_TOKEN"][0] == "team-service-token"
    # Installation depends on the team's configuration alone.
    alice_bound = await store.environments.get_environment_binding("alice", alice_binding)
    assert bound.setup_fingerprint and bound.setup_fingerprint == alice_bound.setup_fingerprint
    assert (await store.environments.current_setup_fingerprints("bob", [env["id"]])) == {
        env["id"]: bound.setup_fingerprint
    }
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT team_id, team_access_generation FROM agent_environment_bindings WHERE id=$1",
            bob_binding,
        )
    assert row["team_id"] == team.id and row["team_access_generation"] >= 1


async def test_a_member_who_leaves_loses_it_and_their_chats_stop(store, pool):
    team = await team_with_people(store)
    env = await save(store, "alice", team_id=team.id)
    default = await store.environments.set_environment_default("bob", REPO, env["id"], 0)
    assert default["environment_id"] == env["id"]
    job, binding = await bind(store, pool, "bob", provided=False)
    assert (await store.environments.get_environment_binding("bob", binding)).environment_id == env[
        "id"
    ]
    assert (await store.environments.environment_for_claim("bob", binding, job["id"])).variables

    await store.teams.leave_team("bob", team.id)

    assert await store.environments.list_environments("bob", REPO) == []
    with pytest.raises(EnvironmentAccessRevoked):
        await store.environments.environment_for_claim("bob", binding, job["id"])
    with pytest.raises(EnvironmentAccessRevoked):
        await store.environments.refresh_runtime_environment("bob", binding, 0)
    # Terminals, actions, Fork and preparation read the values here too.
    with pytest.raises(EnvironmentAccessRevoked):
        await store.environments.get_environment_binding("bob", binding)
    # The masked view still names it, and masking and publication scanning
    # still find its values in what bob's chat already wrote.
    masked = await store.environments.get_environment_binding("bob", binding, include_secrets=False)
    assert masked.environment_id == env["id"]
    assert await store.environments.get_job_environment(job["id"]) is not None
    [step] = await store.environments.redact_environment_steps(
        "bob", binding, [{"phase": "setup", "state": "succeeded", "output": "team-service-token"}]
    )
    assert "team-service-token" not in step["output"]
    # The default it can no longer select is cleared, not silently kept.
    _, fresh = await bind(store, pool, "bob", provided=False)
    assert (await store.environments.get_environment_binding("bob", fresh)).environment_id is None
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT environment_id FROM agent_repository_environment_defaults "
                "WHERE user_id='bob' AND repo=$1",
                REPO,
            )
            is None
        )

    # Rejoining starts a new access generation: the old chat stays stopped.
    await join(store, team.id, "bob", label="link-bob-again")
    with pytest.raises(EnvironmentAccessRevoked):
        await store.environments.environment_for_claim("bob", binding, job["id"])
    rejoined_job, rejoined = await bind(store, pool, "bob", env["id"])
    assert await store.environments.environment_for_claim("bob", rejoined, rejoined_job["id"])

    # A deleted team takes its environments from every member.
    await store.teams.delete_team(
        "alice", team.id, expected_revision=(await store.teams.get_team("alice", team.id)).revision
    )
    with pytest.raises(EnvironmentAccessRevoked):
        await store.environments.environment_for_claim("bob", rejoined, rejoined_job["id"])


async def test_archiving_it_clears_every_members_default(store, pool):
    team = await team_with_people(store)
    env = await save(store, "alice", team_id=team.id)
    for member in ("bob", "carol"):
        await store.environments.set_environment_default(member, REPO, env["id"], 0)

    with pytest.raises(EnvironmentForbidden):
        await store.environments.archive_environment("bob", env["id"], 1)
    await store.environments.archive_environment("carol", env["id"], 1)

    async with pool.acquire() as conn:
        defaults = await conn.fetch(
            "SELECT user_id, environment_id FROM agent_repository_environment_defaults "
            "ORDER BY user_id"
        )
        archived = await conn.fetchval(
            "SELECT action FROM agent_team_audit_events WHERE target_id=$1 ORDER BY id DESC LIMIT 1",
            env["id"],
        )
    assert [(row["user_id"], row["environment_id"]) for row in defaults] == [
        ("bob", None),
        ("carol", None),
    ]
    assert archived == "environment_archived"


async def test_copies_go_to_yourself_or_as_an_editor_to_a_team(store):
    team = await team_with_people(store)
    team_env = await save(store, "alice", team_id=team.id)
    personal = await save(store, "bob", name="Bob's")

    mine = await store.environments.copy_environment(
        "bob", team_env["id"], repo=REPO, name="My copy", revision=1
    )
    assert mine["team"] is None and mine["can_edit"] is True
    with pytest.raises(EnvironmentForbidden):
        await store.environments.copy_environment(
            "bob", personal["id"], repo=REPO, name="Into team", revision=1, team_id=team.id
        )
    shared = await store.environments.copy_environment(
        "carol", team_env["id"], repo=REPO, name="Team copy", revision=1, team_id=team.id
    )
    assert shared["team"] == {"id": team.id, "name": "Platform"}
    assert [e["name"] for e in await store.environments.list_environments("bob", REPO)] == [
        "Bob's",
        "My copy",
        "Shared dev",
        "Team copy",
    ]


async def test_a_members_installation_snapshot_serves_the_whole_team(store, pool):
    await _enable_archives(store)
    await _host(store)
    team = await team_with_people(store)
    env = await save(store, "alice", team_id=team.id)
    _, alice_binding = await bind(store, pool, "alice", env["id"])
    snapshot_id = await store.environment_snapshots.queue_prebuild(
        user_id="alice", binding_id=alice_binding, repo=REPO, base_sha="c" * 40
    )
    run = await _claim(store, capable=True)
    assert await store.environment_snapshots.capture_target(run, "environment-host")
    assert await store.environment_snapshots.record_capture(
        run_id=run["id"],
        host="environment-host",
        generation=run["lease_generation"],
        receipt=_receipt(snapshot_id),
    )

    bob_job, bob_binding = await bind(store, pool, "bob", env["id"])
    used = await store.environment_snapshots.snapshot_for_claim(
        user_id="bob", binding_id=bob_binding, base_image=IMAGE
    )
    assert used is not None and used["snapshot_id"] == snapshot_id
    # bob's workspace reports on alice's snapshot; someone else's personal
    # binding cannot reach it.
    dave_env = await save(store, "dave", name="Dave's")
    dave_job, _ = await bind(store, pool, "dave", dave_env["id"])
    # Each from the attempt holding its task's lease; alice's task is queued first.
    claims = {}
    for worker in ("w1", "w2", "w3"):
        claim = await _claim_task(store, pool, worker)
        claims[claim["id"]] = claim
    for job in (bob_job, dave_job):
        assert await store.environment_snapshots.record_use(
            job_id=job["id"],
            attempt_id=claims[job["id"]]["attempt_id"],
            lease_generation=claims[job["id"]]["lease_generation"],
            snapshot_id=snapshot_id,
            outcome="used",
            base_sha="c" * 40,
        )
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT use_count FROM agent_environment_snapshots WHERE id=$1", snapshot_id
            )
            == 1
        )
    fingerprints = await store.environments.current_setup_fingerprints("bob", [env["id"]])
    summary = await store.environment_snapshots.summaries("bob", fingerprints)
    assert summary[env["id"]]["id"] == snapshot_id

    # Only an owner or admin decides to throw the team's snapshots away.
    assert await store.environment_snapshots.discard("carol", env["id"], team=True) == 1
    assert (
        await store.environment_snapshots.snapshot_for_claim(
            user_id="bob", binding_id=bob_binding, base_image=IMAGE
        )
        is None
    )


async def test_a_team_snapshot_is_retired_by_the_teams_changes_alone(store, pool):
    """A member's own install-time globals never retire it; an editor's change does."""
    await _enable_archives(store)
    await _host(store)
    team = await team_with_people(store)
    setup_only = [
        {"name": "NPM_TOKEN", "value": "personal-npm-token", "secret": True, "phase": "setup"}
    ]
    alice_globals = await globals_for(store, "alice", setup_only)
    bob_globals = await globals_for(store, "bob", setup_only)
    env = await save(store, "alice", team_id=team.id)
    _, bob_binding = await bind(store, pool, "bob", env["id"])
    snapshot_id = await store.environment_snapshots.queue_prebuild(
        user_id="bob", binding_id=bob_binding, repo=REPO, base_sha="c" * 40
    )
    run = await _claim(store, capable=True)
    assert await store.environment_snapshots.capture_target(run, "environment-host")
    assert await store.environment_snapshots.record_capture(
        run_id=run["id"],
        host="environment-host",
        generation=run["lease_generation"],
        receipt=_receipt(
            snapshot_id, object_key=snapshot_object_key(owner_hash("bob"), snapshot_id)
        ),
    )

    async def state():
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, detail FROM agent_environment_snapshots WHERE id=$1", snapshot_id
            )
        return row["state"], row["detail"]

    # The receipt checked it against the team's installation, not the
    # creator's or the builder's globals.
    assert await state() == ("ready", None)
    for actor, current in (("alice", alice_globals), ("bob", bob_globals)):
        await store.environments.save_environment(
            actor,
            repo=None,
            name="Global",
            environment_id=current["id"],
            revision=current["revision"],
            document=EnvironmentScripts().model_dump(),
            edits=[
                {
                    "name": "NPM_TOKEN",
                    "value": "rotated-npm-token",
                    "secret": True,
                    "phase": "setup",
                }
            ],
        )
    assert await state() == ("ready", None)

    # carol, an admin, changes what it installs with: bob's snapshot goes.
    await save(
        store,
        "carol",
        team_id=team.id,
        environment_id=env["id"],
        revision=env["revision"],
        variables=[{"name": "SERVICE_TOKEN", "value": "rotated-team-token", "secret": True}],
    )
    assert await state() == ("superseded", "The installation changed")
