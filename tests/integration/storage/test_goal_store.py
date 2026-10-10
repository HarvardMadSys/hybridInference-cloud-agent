"""Real Postgres: the goal's identity, its rounds, and the queue that advances it.

The route tests use a fake, so what that fake *means* is pinned here: which
write mints a new ``goal_id`` and which keeps it, how the round budget is
derived from the rounds that actually started, what a pause and a clear cancel,
and how the scheduling obligation survives a crash. The constraints are part of
the contract too — a paused goal that cannot say why is a row Postgres refuses,
not merely a shape nothing writes.
"""

from __future__ import annotations

from typing import Any

import asyncpg

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
import pytest
from tests.integration.storage.test_agent_job_store import (  # noqa: F401
    _SHARED_HOST_CAPABILITIES,
    _claim,
    _create_job,
    _expire_attempt,
    _shared_claim_attestation,
    pool,
    store,
)

from cloud_agent.goals.decision import GoalAction
from cloud_agent.goals.models import (
    MAX_WAKEUP_ATTEMPTS,
    STALLED_AFTER_S,
    WAKEUP_BACKOFF_MAX_S,
    WAKEUP_BACKOFF_S,
    GoalConflict,
    GoalInput,
)
from cloud_agent.goals.report import GoalReportEnvelope
from cloud_agent.storage.repositories.goal_wakeups import is_exhausted, wakeup_backoff

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

#: The host the live-goal helper reserves its group on. A re-claim has to name
#: the same one: a retained chat is hard-pinned to the machine that owns its
#: writable layer, and a claim from anywhere else is correctly refused.
GOAL_HOST = "goal-runner"


async def _goal_job(store, **job_overrides) -> tuple[dict, dict]:
    """A conversation whose goal is ``ship it`` for at most five rounds."""
    job = await _create_job(store, **job_overrides)
    goal = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=5),
    )
    assert goal is not None
    return job, goal


async def _create_job_with_goal(store, *, objective: str = "ship it", **overrides):
    """A conversation created *with* a goal, the way the composer creates one.

    Through ``create_job``'s own ``goal`` parameter rather than a follow-up
    ``set_goal``: that is the path where the thread is created already owning a
    goal, and it is the one whose counters have to line up.
    """
    from cloud_agent.goals.models import GoalInput

    return await _create_job(store, goal=GoalInput(objective=objective, max_rounds=5), **overrides)


async def _claim_round(store, *, worker_id: str = "goal-worker", host_credential_hash=None):
    """Claim the next queued turn of the goal group."""
    return await _claim(
        store,
        worker_id=worker_id,
        lease_ttl_seconds=60,
        host=GOAL_HOST,
        host_credential_hash=host_credential_hash or f"{worker_id}-credential-hash",
        claim_attestation=_shared_claim_attestation(),
    )


async def _live_goal_job(
    store, *, worker_id: str = "goal-worker", suffix: str = "a"
) -> tuple[dict, dict, dict]:
    """A goal conversation inside a real retained shared group.

    Round creation and claiming both check the group's live context, so a test
    that exercises either needs one — the same fence production has, not a
    mock of it.
    """
    credential = f"{worker_id}-credential-hash"
    await store.host_pool.register_host(
        host=GOAL_HOST,
        credential_hash=credential,
        capabilities=_SHARED_HOST_CAPABILITIES,
        slots_total=8,
    )
    online = await store.host_pool.record_host_heartbeat(
        host=GOAL_HOST,
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_image_digest="sha256:shared-image",
    )
    assert online is not None and online["status"] == "online"
    job = await _create_job(store)
    claim = await _claim(
        store,
        worker_id=worker_id,
        lease_ttl_seconds=60,
        host=GOAL_HOST,
        host_credential_hash=credential,
        claim_attestation=_shared_claim_attestation(),
    )
    assert claim is not None and claim["id"] == job["id"]
    sandbox = await store.sandbox.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host=GOAL_HOST,
        sandbox_id=f"asbx_goal_test_{suffix}",
        container_name=f"freeinference-goal-test-{suffix}",
        runtime_id="claude-code",
        runtime_version=claim.get("runtime_version"),
        sandbox_image_digest=claim.get("sandbox_image_digest"),
        execution_profile=job["execution_profile"],
    )
    assert sandbox is not None and sandbox["shared_live"] is True
    goal = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=5),
    )
    assert goal is not None
    return job, claim, goal


async def _finish(pool, claim) -> None:
    """Make a claimed turn terminal, the way ``transition`` would.

    Written as raw SQL because most of these tests are about the goal's
    counters, and going through ``transition`` would drag the retained-sandbox
    settlement invariants into every one of them. The report-and-wakeup commit
    is what :func:`test_a_finished_round_commits_its_report_and_its_wakeup`
    uses the real path for.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = GREATEST(turn_no, 1), "
            "current_attempt_id = NULL WHERE id = $1",
            claim["id"],
        )
        await conn.execute(
            "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() WHERE id = $1",
            claim["attempt_id"],
        )
        # ``transition`` releases the host slot when it closes an attempt, and
        # a raw terminal write has to do the same or the next claim in the same
        # test has nowhere to run.
        await conn.execute(
            "UPDATE agent_runner_hosts SET slots_used = GREATEST(slots_used - 1, 0) "
            f"WHERE host = '{GOAL_HOST}'"
        )


async def _round(
    store,
    *,
    job: dict,
    goal: dict,
    kind: str = "work",
    round_number: int = 1,
    goal_id: str | None = None,
    revision: int | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any] | None:
    """One automatic round, created through the store's own controller path."""
    async with store.goal_wakeups.transaction() as conn:
        return await store.conversation_commands.create_goal_round(
            conn=conn,
            thread_id=str(job["thread_id"]),
            goal_id=goal_id or str(goal["goal_id"]),
            revision=goal["revision"] if revision is None else revision,
            kind=kind,
            round_number=round_number,
            prompt=f'<goal_round kind="{kind}" round="{round_number}">',
            client_request_id=client_request_id
            or f"goal:{job['thread_id']}:{goal['goal_id']}:{kind}:{round_number}",
            client_request_fingerprint=f"goal-round:{kind}:{round_number}",
        )


async def _rounds_used(store, thread_id: str) -> int:
    goal = await store.goals.get_goal(thread_id=thread_id)
    return int(goal["rounds_used"])


async def _arm_test_wakeup(store, *, job: dict, trigger_ref: str = "test:1") -> str:
    """One obligation of a conversation, armed by hand for the queue's own cases."""
    wakeup_id = await store.goal_wakeups.arm_wakeup(
        thread_id=job["thread_id"],
        goal_id="goal-x",
        revision=1,
        trigger="owner",
        trigger_ref=trigger_ref,
    )
    assert wakeup_id is not None
    return wakeup_id


async def test_set_edit_pause_resume_and_clear_advance_the_revision(store):
    job, goal = await _goal_job(store)
    assert (goal["state"], goal["revision"], goal["rounds_used"]) == ("active", 1, 0)

    # Editing only the cap keeps the identity: it is the same authorization.
    edited = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=9),
        expected_revision=1,
    )
    assert (edited["goal_id"], edited["revision"], edited["max_rounds"]) == (
        goal["goal_id"],
        2,
        9,
    )

    # A new objective is a new authorization, and mints a new identity.
    replaced = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it safely", max_rounds=9),
        expected_revision=2,
    )
    assert replaced["goal_id"] != goal["goal_id"]
    assert (replaced["revision"], replaced["objective"]) == (3, "ship it safely")

    paused = await store.goals.pause_goal(thread_id=job["thread_id"], revision=3, reason="user")
    assert (paused["state"], paused["pause_reason"]) == ("paused", "user")
    assert paused["pause_detail"] == "paused by the user"
    assert paused["revision"] == 4

    # A paused goal is not paused twice: the write is fenced on ``active``.
    assert (
        await store.goals.pause_goal(thread_id=job["thread_id"], revision=4, reason="user") is None
    )

    resumed = await store.goals.resume_goal(
        thread_id=job["thread_id"], user_id=job["user_id"], revision=4
    )
    assert (resumed["state"], resumed["pause_reason"], resumed["revision"]) == ("active", None, 5)

    assert await store.goals.clear_goal(thread_id=job["thread_id"], user_id=job["user_id"])
    assert await store.goals.get_goal(thread_id=job["thread_id"]) is None
    assert await store.goals.clear_goal(thread_id=job["thread_id"], user_id=job["user_id"]) is False


async def test_the_control_version_survives_a_cleared_goal(store):
    """What the next write compares against is the version clear left behind."""
    job, goal = await _goal_job(store)
    state = await store.goals.get_thread_goal_state(
        user_id=job["user_id"], thread_id=job["thread_id"]
    )
    assert (state["goal"]["goal_id"], state["revision"]) == (goal["goal_id"], 1)

    await store.goals.clear_goal(thread_id=job["thread_id"], user_id=job["user_id"])

    after = await store.goals.get_thread_goal_state(
        user_id=job["user_id"], thread_id=job["thread_id"]
    )
    assert after == {"goal": None, "revision": 2}


async def test_reading_a_foreign_conversations_goal_is_not_an_answer(store):
    job, _ = await _goal_job(store)
    assert (
        await store.goals.get_thread_goal_state(user_id="user-2", thread_id=job["thread_id"])
        is None
    )


async def test_only_the_owner_can_write_a_goal(store):
    job, _ = await _goal_job(store)
    assert (
        await store.goals.set_goal(
            thread_id=job["thread_id"],
            user_id="user-2",
            goal=GoalInput(objective="hijack"),
        )
        is None
    )
    assert not await store.goals.clear_goal(thread_id=job["thread_id"], user_id="user-2")
    assert (
        await store.goals.resume_goal(thread_id=job["thread_id"], user_id="user-2", revision=1)
        is None
    )


async def test_a_stale_revision_is_refused_and_nothing_is_written(store):
    job, _ = await _goal_job(store)
    await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it safely", max_rounds=7),
        expected_revision=1,
    )
    with pytest.raises(GoalConflict):
        await store.goals.set_goal(
            thread_id=job["thread_id"],
            user_id=job["user_id"],
            goal=GoalInput(objective="something else"),
            expected_revision=1,
        )
    unchanged = await store.goals.get_goal(thread_id=job["thread_id"])
    assert (unchanged["objective"], unchanged["revision"]) == ("ship it safely", 2)


async def test_editing_a_paused_goal_leaves_it_paused(store):
    """Changing the words is not resuming it, and the reason it stopped stands."""
    job, goal = await _goal_job(store)
    await store.goals.pause_goal(thread_id=job["thread_id"], revision=1, reason="user")

    edited = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=7),
        expected_revision=2,
    )

    assert edited["goal_id"] == goal["goal_id"]
    assert (edited["state"], edited["pause_reason"], edited["revision"]) == ("paused", "user", 3)


async def test_editing_only_the_objective_keeps_a_bound_it_did_not_mention(store):
    """A request that says nothing about the bound leaves the owner's alone.

    This is not a whole-goal replacement: ``max_rounds`` is optional, and a
    caller that only changes the words must not silently delete a limit its
    owner stated. Opening the band's editor and pressing Save without touching
    the objective is exactly that request.
    """
    job, goal = await _goal_job(store)
    assert goal["max_rounds"] == 5

    edited = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it safely"),
        expected_revision=goal["revision"],
    )

    assert edited["max_rounds"] == 5


async def test_stating_no_bound_is_how_a_bound_is_cleared(store):
    """Absence is "unchanged"; an explicit ``null`` is the way to lift one."""
    job, goal = await _goal_job(store)

    cleared = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=None),
        expected_revision=goal["revision"],
    )

    assert cleared["max_rounds"] is None


async def test_a_bound_can_be_stated_on_a_goal_that_had_none(store):
    """The same rule the other way round: an explicit number is written."""
    job, first = await _goal_job(store)
    unbounded = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=None),
        expected_revision=first["revision"],
    )
    assert unbounded["max_rounds"] is None

    bounded = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=5000),
        expected_revision=unbounded["revision"],
    )

    assert bounded["max_rounds"] == 5000


async def test_setting_a_completed_goal_again_mints_a_new_identity(store):
    """Identical words, a new authorization: the last one was spent."""
    job, goal = await _goal_job(store)
    await store.goals.apply_action(
        thread_id=job["thread_id"],
        revision=goal["revision"],
        action=GoalAction(kind="complete", completion_basis="model_reported"),
        completed_by_job_id=job["id"],
    )

    again = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=5),
        expected_revision=2,
    )

    assert again["goal_id"] != goal["goal_id"]
    assert (again["state"], again["rounds_used"], again["revision"]) == ("active", 0, 3)
    assert again["completion_basis"] is None


async def test_an_unstarted_round_costs_nothing(store, pool):
    """The charge is the attempt, not the queue entry."""
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    queued = await _round(store, job=job, goal=goal)

    assert queued is not None and queued["state"] == "queued"
    assert await _rounds_used(store, job["thread_id"]) == 0


async def test_a_started_round_costs_once_however_many_attempts(store, pool):
    """One round, one charge — a retry re-enters the same round."""
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    work = await _round(store, job=job, goal=goal)
    claimed = await _claim_round(store)
    assert claimed["id"] == work["id"]
    assert await _rounds_used(store, job["thread_id"]) == 1

    # A second attempt at the same round: the first expired and was reaped.
    await _expire_attempt(pool, store, claimed["attempt_id"])
    actions = await store.attempt_commands.reap_expired(max_attempts=3)
    assert [action["action"] for action in actions] == ["queued"]
    second = await _claim_round(store)
    assert second["id"] == work["id"] and second["attempt_no"] == 2
    assert await _rounds_used(store, job["thread_id"]) == 1


async def test_a_round_whose_attempt_was_aborted_costs_nothing(store, pool):
    """``aborted`` means the claim was handed back before the turn began."""
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    work = await _round(store, job=job, goal=goal)
    claimed = await _claim_round(store)
    assert claimed["id"] == work["id"]
    assert await _rounds_used(store, job["thread_id"]) == 1

    assert await store.attempt_commands.release_claim(
        job_id=claimed["id"],
        attempt_id=claimed["attempt_id"],
        lease_generation=claimed["lease_generation"],
    )
    assert await _rounds_used(store, job["thread_id"]) == 0

    # And it counts again the moment a model really runs for it.
    again = await _claim_round(store)
    assert again["id"] == work["id"]
    assert await _rounds_used(store, job["thread_id"]) == 1


async def test_a_repair_round_is_charged_like_a_work_round(store, pool):
    """The budget bounds spend, and a repair round is a model round."""
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    repair = await _round(store, job=job, goal=goal, kind="repair", round_number=2)

    claimed = await _claim_round(store)

    assert claimed["id"] == repair["id"]
    assert claimed["goal_round_kind"] == "repair"
    assert await _rounds_used(store, job["thread_id"]) == 1
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["repairs_used"] == 1


async def test_a_round_for_a_replaced_objective_does_not_count(store, pool):
    """Rounds belong to the identity they were created for."""
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    await _round(store, job=job, goal=goal)
    claimed = await _claim_round(store)
    assert claimed is not None
    assert await _rounds_used(store, job["thread_id"]) == 1

    replaced = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="a different objective"),
    )

    assert replaced["goal_id"] != goal["goal_id"]
    assert await _rounds_used(store, job["thread_id"]) == 0


async def test_pause_and_clear_cancel_the_queued_rounds(store, pool):
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    first_round = await _round(store, job=job, goal=goal, round_number=1)
    second_round = await _round(store, job=job, goal=goal, round_number=2)

    paused = await store.goals.pause_goal(thread_id=job["thread_id"], revision=1, reason="user")

    assert paused is not None
    for queued in (first_round, second_round):
        assert (await store.conversation_queries.get_job(queued["id"]))["state"] == "cancelled"

    # The same rule on the way out: clearing the goal takes its rounds with it.
    await store.goals.resume_goal(
        thread_id=job["thread_id"], user_id=job["user_id"], revision=paused["revision"]
    )
    fresh = await store.goals.get_goal(thread_id=job["thread_id"])
    doomed = await _round(store, job=job, goal=fresh, round_number=3)
    assert await store.goals.clear_goal(thread_id=job["thread_id"], user_id=job["user_id"])
    assert (await store.conversation_queries.get_job(doomed["id"]))["state"] == "cancelled"


async def test_resume_arms_the_obligation_to_schedule(store, pool):
    """Resume is the owner's authorization; the wakeup is how it reaches a round."""
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    paused = await store.goals.pause_goal(thread_id=job["thread_id"], revision=1, reason="user")

    resumed = await store.goals.resume_goal(
        thread_id=job["thread_id"], user_id=job["user_id"], revision=paused["revision"]
    )

    wakeup = await store.goal_wakeups.pending_wakeup(thread_id=job["thread_id"])
    assert wakeup is not None
    assert (wakeup["trigger"], wakeup["trigger_ref"]) == ("owner", f"resume:{resumed['revision']}")
    assert (wakeup["goal_id"], wakeup["revision"]) == (goal["goal_id"], resumed["revision"])


async def test_resume_releases_a_bound_the_goal_has_spent(store, pool):
    """The bound is the owner's, so the owner's own Resume is what lifts it.

    There is no other way out: the round bound has no configuration surface at
    all. A Resume that refused here — which is what it used to do — left a
    paused goal with a control that could only fail.
    """
    job, first, _ = await _live_goal_job(store)
    await _finish(pool, first)
    # One round is the whole bound, so the goal is at it the moment it is
    # charged.
    bounded = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=1),
        expected_revision=1,
    )
    work = await _round(store, job=job, goal=bounded, round_number=1)
    claimed = await _claim_round(store)
    assert claimed["id"] == work["id"]
    assert await _rounds_used(store, job["thread_id"]) == 1

    paused = await store.goals.pause_goal(
        thread_id=job["thread_id"], revision=bounded["revision"], reason="round_limit"
    )
    resumed = await store.goals.resume_goal(
        thread_id=job["thread_id"], user_id=job["user_id"], revision=paused["revision"]
    )

    assert resumed["max_rounds"] is None
    # The rounds already spent are history, not something a resume rewrites.
    assert resumed["rounds_used"] == 1
    # And the goal is genuinely unbounded now, not bounded by a large number.
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["max_rounds"] is None


async def test_resume_keeps_a_bound_the_goal_has_not_reached(store, pool):
    """Releasing a spent bound is not a general amnesty.

    "At most five, paused by the owner at none" still means five.
    """
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    paused = await store.goals.pause_goal(thread_id=job["thread_id"], revision=1, reason="user")

    resumed = await store.goals.resume_goal(
        thread_id=job["thread_id"], user_id=job["user_id"], revision=paused["revision"]
    )

    assert resumed["max_rounds"] == goal["max_rounds"] == 5


async def test_lock_goal_state_holds_the_conversations_row(store, pool):
    """The last read before a decision is written is under the same lock."""
    job, _ = await _goal_job(store)

    holder = await pool.acquire()
    try:
        async with holder.transaction():
            locked = await store.goals.lock_goal_state(thread_id=job["thread_id"], conn=holder)
            assert locked is not None and locked["state"] == "active"
            prober = await pool.acquire()
            try:
                with pytest.raises(asyncpg.LockNotAvailableError):
                    async with prober.transaction():
                        await prober.fetchval(
                            "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE NOWAIT",
                            job["thread_id"],
                        )
            finally:
                await pool.release(prober)
    finally:
        await pool.release(holder)


async def test_apply_action_pauses_with_the_reason_it_was_given(store, pool):
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    queued = await _round(store, job=job, goal=goal)

    written = await store.goals.apply_action(
        thread_id=job["thread_id"],
        revision=goal["revision"],
        action=GoalAction(kind="pause", reason="round_limit", message="out of rounds"),
    )

    assert written is not None
    assert (written["state"], written["pause_reason"], written["pause_detail"]) == (
        "paused",
        "round_limit",
        "out of rounds",
    )
    assert written["revision"] == goal["revision"] + 1
    assert (await store.conversation_queries.get_job(queued["id"]))["state"] == "cancelled"
    # The same decision applied twice is a no-op the second time: the first
    # application already moved the goal past the revision it was made against.
    assert (
        await store.goals.apply_action(
            thread_id=job["thread_id"],
            revision=goal["revision"],
            action=GoalAction(kind="pause", reason="round_limit"),
        )
        is None
    )


async def test_apply_action_completes_only_with_the_turn_that_earned_it(store):
    job, goal = await _goal_job(store)

    written = await store.goals.apply_action(
        thread_id=job["thread_id"],
        revision=goal["revision"],
        action=GoalAction(kind="complete", completion_basis="model_reported"),
        completed_by_job_id=job["id"],
    )

    assert written is not None
    assert (written["state"], written["completion_basis"]) == ("complete", "model_reported")
    assert written["completed_by_job_id"] == job["id"]

    # "Complete, but nobody knows how" is a row Postgres refuses.
    second_job, second = await _goal_job(store)
    with pytest.raises(asyncpg.CheckViolationError):
        await store.goals.apply_action(
            thread_id=second_job["thread_id"],
            revision=second["revision"],
            action=GoalAction(kind="complete", completion_basis="model_reported"),
        )


async def test_a_decision_that_changes_no_state_leaves_the_reason_alone(store):
    """A ``none`` decision records that a turn was judged, not a new state."""
    job, goal = await _goal_job(store)
    paused = await store.goals.apply_action(
        thread_id=job["thread_id"],
        revision=goal["revision"],
        action=GoalAction(kind="pause", reason="needs_input", message="which region?"),
    )
    assert paused is not None

    assert await store.goals.apply_action(
        thread_id=job["thread_id"],
        revision=paused["revision"],
        action=GoalAction(kind="none"),
    )

    after = await store.goals.get_goal(thread_id=job["thread_id"])
    assert (after["state"], after["pause_reason"], after["pause_detail"]) == (
        "paused",
        "needs_input",
        "which region?",
    )
    assert after["revision"] == paused["revision"]


async def test_the_shape_constraints_refuse_a_goal_that_cannot_say_what_it_is(store, pool):
    job, _ = await _goal_job(store)

    async def write(sql: str) -> None:
        async with pool.acquire() as conn:
            await conn.execute(sql, job["thread_id"])

    # Paused without a reason, and paused without a detail: both are rows the
    # owner cannot act on.
    with pytest.raises(asyncpg.CheckViolationError):
        await write(
            "UPDATE agent_threads SET goal_state = 'paused', goal_pause_reason = 'user', "
            "goal_pause_detail = NULL WHERE id = $1"
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await write(
            "UPDATE agent_threads SET goal_state = 'paused', goal_pause_reason = NULL, "
            "goal_pause_detail = 'because' WHERE id = $1"
        )
    # A state with no objective, and an objective with no identity.
    with pytest.raises(asyncpg.CheckViolationError):
        await write(
            "UPDATE agent_threads SET goal_objective = NULL, goal_id = NULL, "
            "goal_max_rounds = NULL, goal_state = 'active' WHERE id = $1"
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await write("UPDATE agent_threads SET goal_id = NULL WHERE id = $1")

    # The goal is untouched by every refused write.
    goal = await store.goals.get_goal(thread_id=job["thread_id"])
    assert (goal["state"], goal["revision"]) == ("active", 1)


async def test_arming_the_same_event_twice_makes_one_obligation(store):
    job = await _create_job(store)

    async def arm(trigger_ref: str) -> str | None:
        return await store.goal_wakeups.arm_wakeup(
            thread_id=job["thread_id"],
            goal_id="goal-x",
            revision=1,
            trigger="owner",
            trigger_ref=trigger_ref,
        )

    first = await arm("test:1")

    assert first is not None and await arm("test:1") is None

    # A *processed* row does not block a genuinely new event of the same name.
    await store.goal_wakeups.finish_wakeup(wakeup_id=first, processed=True, detail="done")
    assert await store.goal_wakeups.pending_wakeup(thread_id=job["thread_id"]) is None
    arming_again = await arm("test:1")
    assert arming_again is not None and arming_again != first


async def test_a_claim_leases_a_wakeup_until_its_lease_expires(store, pool):
    job = await _create_job(store)
    wakeup_id = await _arm_test_wakeup(store, job=job)

    claimed = await store.goal_wakeups.claim_wakeups(limit=1)

    assert [row["id"] for row in claimed] == [wakeup_id]
    assert claimed[0]["attempts"] == 1
    assert claimed[0]["lease_expires_at"] is not None
    # A second consumer finds nothing while the lease stands.
    assert await store.goal_wakeups.claim_wakeups(limit=1) == []

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_goal_wakeups SET lease_expires_at = NOW() - interval '1 second' "
            "WHERE id = $1",
            wakeup_id,
        )
    reclaimed = await store.goal_wakeups.claim_wakeups(limit=1)
    assert [row["id"] for row in reclaimed] == [wakeup_id]
    assert reclaimed[0]["attempts"] == 2


async def test_a_released_wakeup_waits_behind_the_turn_it_yielded_to(store):
    job = await _create_job(store)
    wakeup_id = await _arm_test_wakeup(store, job=job)
    await store.goal_wakeups.claim_wakeups(limit=1)

    await store.goal_wakeups.release_wakeup(
        wakeup_id=wakeup_id, waiting_job_id=job["id"], detail="a person's turn arrived first"
    )

    parked = await store.goal_wakeups.pending_wakeup(thread_id=job["thread_id"])
    assert parked["waiting_job_id"] == job["id"]
    # Parked is not due: a consumer would only park it again.
    assert await store.goal_wakeups.claim_wakeups(limit=1) == []


async def test_a_backoff_defers_a_wakeup_without_holding_its_lease(store):
    """Retrying later and "a worker may have died" are different facts."""
    job = await _create_job(store)
    wakeup_id = await _arm_test_wakeup(store, job=job)
    await store.goal_wakeups.claim_wakeups(limit=1)

    await store.goal_wakeups.release_wakeup(wakeup_id=wakeup_id, retry_in_seconds=60)

    row = await store.goal_wakeups.pending_wakeup(thread_id=job["thread_id"])
    assert row["lease_expires_at"] is None and row["lease_owner"] is None
    assert row["not_before"] > row["created_at"]
    assert await store.goal_wakeups.claim_wakeups(limit=1) == []


async def test_finishing_a_wakeup_records_what_was_decided(store):
    job = await _create_job(store)
    wakeup_id = await _arm_test_wakeup(store, job=job)
    await store.goal_wakeups.claim_wakeups(limit=1)

    await store.goal_wakeups.finish_wakeup(wakeup_id=wakeup_id, processed=True, detail="complete")

    assert await store.goal_wakeups.pending_wakeup(thread_id=job["thread_id"]) is None


async def test_the_backoff_and_the_attempt_cap_are_bounded():
    """A broken deployment retries for minutes, never hot and never forever."""
    assert wakeup_backoff(1) == WAKEUP_BACKOFF_S
    assert wakeup_backoff(2) == WAKEUP_BACKOFF_S * 2
    assert wakeup_backoff(3) == WAKEUP_BACKOFF_S * 4
    # Doubling stops at the cap, whatever the attempt count says.
    assert wakeup_backoff(100) == WAKEUP_BACKOFF_MAX_S
    assert wakeup_backoff(MAX_WAKEUP_ATTEMPTS) <= WAKEUP_BACKOFF_MAX_S

    assert is_exhausted(MAX_WAKEUP_ATTEMPTS - 1) is False
    assert is_exhausted(MAX_WAKEUP_ATTEMPTS) is True


async def test_a_finished_round_commits_its_report_and_its_wakeup(store, pool):
    """The crash boundary: the report, the outcome and the obligation are one commit."""
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    round_job = await _round(store, job=job, goal=goal)
    claimed = await _claim_round(store)
    assert claimed["id"] == round_job["id"]

    assert await store.attempt_commands.transition(
        job_id=round_job["id"],
        attempt_id=claimed["attempt_id"],
        lease_generation=claimed["lease_generation"],
        from_states=("running",),
        to_state="cancelled",
        goal_envelope=GoalReportEnvelope(
            parse_result="valid",
            outcome="complete",
            reason="pytest: 42 passed",
            evidence=["pytest -q, exit 0"],
            detail=None,
            source_digest="sha256:abcdef",
        ),
    )

    saved = await store.conversation_queries.get_job(round_job["id"])
    assert (
        saved["goal_report_parse"],
        saved["goal_report_outcome"],
        saved["goal_report_reason"],
    ) == ("valid", "complete", "pytest: 42 passed")
    async with pool.acquire() as conn:
        stored = await conn.fetchrow(
            "SELECT goal_report_evidence, goal_report_digest FROM agent_jobs WHERE id = $1",
            round_job["id"],
        )
    assert stored["goal_report_digest"] == "sha256:abcdef"
    # The obligation the finish owed is in the same commit, keyed by the turn.
    wakeup = await store.goal_wakeups.pending_wakeup(thread_id=job["thread_id"])
    assert (wakeup["trigger"], wakeup["trigger_ref"]) == ("finish", f"job:{round_job['id']}")


async def test_a_report_is_fenced_to_the_live_lease(store, pool):
    """Only the attempt holding the lease may relabel what a turn reported.

    The envelope is written by the terminal transition, so the fence is the
    transition's own: a caller who has lost the lease is refused, and the row
    keeps what the live attempt said.
    """
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    round_job = await _round(store, job=job, goal=goal)
    claimed = await _claim_round(store)
    assert claimed["id"] == round_job["id"]

    envelope = GoalReportEnvelope(
        parse_result="valid",
        outcome="needs_input",
        reason="which region should I deploy to?",
        evidence=None,
        detail=None,
        source_digest="sha256:abcdef",
    )
    # ``cancelled`` rather than ``succeeded``: a success carries a native turn
    # ordinal settled by the shared workspace, which is the sandbox's business
    # and not what this test is about. A cancelled turn is also the sharper
    # case — it is the one that produced a report and must still not complete
    # the goal.
    assert await store.attempt_commands.transition(
        job_id=round_job["id"],
        attempt_id=claimed["attempt_id"],
        lease_generation=claimed["lease_generation"],
        from_states=("running",),
        to_state="cancelled",
        goal_envelope=envelope,
    )
    saved = await store.conversation_queries.get_job(round_job["id"])
    assert (saved["goal_report_parse"], saved["goal_report_reason"]) == (
        "valid",
        "which region should I deploy to?",
    )

    # A generation that is not the live one is refused: a superseded turn must
    # not relabel what its successor is doing. The job is terminal now, so the
    # fence refuses for both reasons and the row is untouched.
    assert not await store.attempt_commands.transition(
        job_id=round_job["id"],
        attempt_id=claimed["attempt_id"],
        lease_generation=claimed["lease_generation"] + 1,
        from_states=("running",),
        to_state="cancelled",
        goal_envelope=GoalReportEnvelope(
            parse_result="valid",
            outcome="complete",
            reason="should not land",
            evidence=None,
            detail=None,
            source_digest=None,
        ),
    )
    after = await store.conversation_queries.get_job(round_job["id"])
    assert (after["goal_report_parse"], after["goal_report_reason"]) == (
        "valid",
        "which region should I deploy to?",
    )


async def test_cancelling_a_queued_goal_round_pauses_the_goal(store, pool):
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    queued = await _round(store, job=job, goal=goal)

    assert await store.attempt_commands.request_cancel(job_id=queued["id"]) == "cancelled"

    after = await store.goals.get_goal(thread_id=job["thread_id"])
    assert (after["state"], after["pause_reason"]) == ("paused", "cancelled")
    assert after["revision"] == goal["revision"] + 1


async def test_cancelling_a_persons_queued_turn_leaves_the_goal_alone(store, pool):
    job, first, _ = await _live_goal_job(store)
    await _finish(pool, first)
    human = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="actually, also do this",
    )

    assert await store.attempt_commands.request_cancel(job_id=human["id"]) == "cancelled"
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["state"] == "active"


async def test_sweep_cancels_a_stale_round_and_rearms_its_live_goal(store, pool):
    """A round that no longer matches its goal can never be claimed."""
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    stale = await _round(store, job=job, goal=goal, round_number=1)
    # The cap moves the revision but keeps the identity, so the queued round is
    # now stale without its goal having been replaced.
    edited = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=9),
        expected_revision=goal["revision"],
    )
    async with pool.acquire() as conn:
        # Put it back in the queue carrying the revision it was created for,
        # which is what a claim racing the edit would have found.
        await conn.execute(
            "UPDATE agent_jobs SET state = 'queued', cancel_requested = FALSE WHERE id = $1",
            stale["id"],
        )

    swept = await store.goals.sweep_goal_scheduling()

    assert job["thread_id"] in swept["rearmed"]
    assert (await store.conversation_queries.get_job(stale["id"]))["state"] == "cancelled"
    wakeup = await store.goal_wakeups.pending_wakeup(thread_id=job["thread_id"])
    assert wakeup["trigger_ref"] == f"stale-round:{edited['revision']}"
    assert wakeup["revision"] == edited["revision"]


async def test_sweep_pauses_a_goal_it_cannot_explain(store, pool):
    """An active goal that nothing is waiting on and nothing can explain.

    This is the backstop, not a mechanism: the goal has no queued or running
    turn, no pending obligation, and has not moved for longer than a consumer
    could still be about to act. It cannot be allowed to show a green badge for
    ever, so it is paused with a reason the owner can act on.
    """
    job, _ = await _goal_job(store)
    # The conversation's first turn has to have happened: a *queued* turn is a
    # reason to wait, not a lost obligation, and the sweep is only about goals
    # that nothing explains.
    await _finish(pool, await _claim_round(store))
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET goal_updated_at = NOW() - make_interval(secs => $2) "
            "WHERE id = $1",
            job["thread_id"],
            STALLED_AFTER_S + 60,
        )
        await conn.execute(
            "UPDATE agent_goal_wakeups SET state = 'processed' WHERE thread_id = $1",
            job["thread_id"],
        )

    swept = await store.goals.sweep_goal_scheduling()

    assert swept["stalled"] == [job["thread_id"]]
    goal = await store.goals.get_goal(thread_id=job["thread_id"])
    assert (goal["state"], goal["pause_reason"]) == ("paused", "scheduling_failed")


async def test_get_goal_for_job_reads_across_the_join(store):
    """The one joined goal read. Unqualified, ``goal_revision`` is ambiguous here.

    ``0019`` puts that column on ``agent_jobs`` as well — the revision a turn
    was created for — so Postgres refuses the statement outright (42702) rather
    than picking one. This read is on the path of *every* turn's continuation,
    and the exception was swallowed into a warning, so a missing test here meant
    no automatic round was ever queued and nothing said why.
    """
    job, goal = await _goal_job(store)
    found = await store.goals.get_goal_for_job(job_id=job["id"])
    assert found is not None
    assert found["objective"] == goal["objective"]
    assert found["revision"] == goal["revision"]


async def test_has_pending_turn_sees_queued_and_running_work(store, pool):
    job, claim, goal = await _live_goal_job(store)
    assert await store.goals.has_pending_turn(thread_id=job["thread_id"])
    await _finish(pool, claim)
    assert not await store.goals.has_pending_turn(thread_id=job["thread_id"])
    await _round(store, job=job, goal=goal)
    assert await store.goals.has_pending_turn(thread_id=job["thread_id"])


async def test_a_replayed_round_creation_makes_one_row(store, pool):
    """The client request id is the event's identity, not the round's."""
    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    key = f"goal:{job['thread_id']}:{goal['goal_id']}:work:job:{first['id']}"
    created = await _round(store, job=job, goal=goal, client_request_id=key)
    replayed = await _round(store, job=job, goal=goal, client_request_id=key)

    assert created is not None and replayed is None
    turns = await store.conversation_queries.list_jobs(user_id=job["user_id"])
    assert len([turn for turn in turns if turn["turn_source"] == "goal"]) == 1


async def test_a_goal_created_with_the_first_message_queues_its_first_round(
    store, pool, monkeypatch: pytest.MonkeyPatch
):
    """The whole first-round path, on the rows a real conversation writes.

    A goal created alongside the opening message is the ordinary way a goal
    starts, and it is the case where three separate counters have to agree: the
    conversation's input count, the count its first turn was stamped with, and
    the count an automatic round inherits. A zero anywhere in that chain makes
    the platform read "the owner has spoken since" on every round it ever
    queues — so the goal stays active for ever and no completion is ever
    accepted — which is what this walks on real rows.
    """
    from cloud_agent.goals.controller import consume_wakeup, continue_after_finish
    from cloud_agent.goals.models import GOAL_REPORT_PROTOCOL

    # The consumer checks the repository's entitlement before it schedules, so
    # this deployment has to allow the repository the case runs against.
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "example-org/example-repo")
    job = await _create_job_with_goal(store, objective="ship it")
    thread_id = str(job["thread_id"])
    assert await store.goals.get_goal(thread_id=thread_id) is not None

    # The wakeup the creation owed is there, and the first turn carries the
    # conversation's own count — not zero, which would make it older than the
    # message that created it.
    armed = await store.goal_wakeups.pending_wakeup(thread_id=thread_id)
    assert armed is not None and armed["trigger"] == "owner"
    first = await store.conversation_queries.get_job(str(job["id"]))
    assert first["input_seq_at_create"] == first["thread_user_input_seq"] == 1

    # The owner's own turn is live, so the obligation waits behind it rather
    # than queueing a second turn on top of it.
    claim = await _claim_round(store)
    assert claim["id"] == job["id"]
    claimed = await store.goal_wakeups.claim_wakeups(limit=1)
    assert claimed and claimed[0]["id"] == armed["id"]
    assert await consume_wakeup(store=store, wakeup=claimed[0]) == "wait"

    # It finishes without reporting anything, which is the ordinary shape of a
    # first turn. That finish releases the wait behind it and records the
    # decision the turn owes. Through ``transition``, not the raw helper, for
    # exactly that reason: the release and the record live in its transaction.
    # Cancelled rather than succeeded so the turn needs no native settlement —
    # the arithmetic below is the same either way.
    assert await store.attempt_commands.transition(
        job_id=str(job["id"]),
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="cancelled",
    )
    assert await continue_after_finish(store=store, job_id=str(job["id"])) >= 1

    turns = await _thread_turns(store, pool, thread_id)
    rounds = [row for row in turns if row["turn_source"] == "goal"]
    assert len(rounds) == 1, "round one was not queued"
    assert rounds[0]["goal_round"] == 1
    # The round inherits the count the owner's message left, which is what lets
    # a completion it reports be accepted later.
    assert rounds[0]["input_seq_at_create"] == 1
    # And it asks for a host that can deliver that report.
    assert GOAL_REPORT_PROTOCOL in (rounds[0]["metadata"] or {}).get("requires", [])


async def test_an_unloaded_github_app_leaves_the_goal_waiting_on_its_rows(
    store, pool, monkeypatch: pytest.MonkeyPatch
):
    """Issue #47 on real rows: an outage spends nothing and pauses nothing.

    The finish that armed the wakeup drains it through the consumer, and the
    reaper asks again every pass. While the GitHub App cannot be loaded, both
    stop before the claim — more times than a real failure is allowed — so the
    wakeup keeps its five attempts, the goal stays active, and the stall sweep
    leaves it alone because the obligation is still pending. The same wakeup
    becomes the next round once the App loads.
    """
    from cloud_agent.goals.controller import consume_wakeup, continue_after_finish, run_goal_wakeups
    from cloud_agent.goals.models import MAX_WAKEUP_ATTEMPTS
    from cloud_agent.source_control_runtime import SourceControlUnavailable

    class RecoveringApp:
        loaded = False

        def get(self) -> object:
            if not self.loaded:
                raise SourceControlUnavailable("github")
            return object()  # the repository is allowlisted; the minter is not asked

    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "example-org/example-repo")
    job = await _create_job_with_goal(store, objective="ship it")
    thread_id = str(job["thread_id"])
    armed = await store.goal_wakeups.pending_wakeup(thread_id=thread_id)
    claim = await _claim_round(store)
    claimed = await store.goal_wakeups.claim_wakeups(limit=1)
    assert claimed and claimed[0]["id"] == armed["id"]
    assert await consume_wakeup(store=store, wakeup=claimed[0]) == "wait"
    assert await store.attempt_commands.transition(
        job_id=str(job["id"]),
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="cancelled",
    )
    due = await store.goal_wakeups.pending_wakeup(thread_id=thread_id)
    attempts_before = due["attempts"]

    app = RecoveringApp()
    assert await continue_after_finish(store=store, job_id=str(job["id"]), github_app=app) == 0
    for _ in range(MAX_WAKEUP_ATTEMPTS + 1):
        assert await run_goal_wakeups(store=store, github_app=app) == 0
    # Old enough to be stalled, were it not for the obligation still pending.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET goal_updated_at = NOW() - INTERVAL '1 day' WHERE id = $1",
            thread_id,
        )
    swept = await store.goals.sweep_goal_scheduling()

    waiting = await store.goal_wakeups.pending_wakeup(thread_id=thread_id)
    assert waiting is not None and waiting["id"] == due["id"]
    assert waiting["attempts"] == attempts_before
    assert waiting["lease_expires_at"] is None
    assert thread_id not in swept["stalled"]
    goal = await store.goals.get_goal(thread_id=thread_id)
    assert goal["state"] == "active"
    assert [
        row for row in await _thread_turns(store, pool, thread_id) if row["turn_source"] == "goal"
    ] == []

    app.loaded = True
    assert await run_goal_wakeups(store=store, github_app=app) == 1
    assert await store.goal_wakeups.pending_wakeup(thread_id=thread_id) is None
    rounds = [
        row for row in await _thread_turns(store, pool, thread_id) if row["turn_source"] == "goal"
    ]
    assert len(rounds) == 1


async def _thread_turns(store, pool, thread_id: str) -> list[dict[str, Any]]:
    """Every turn of one conversation, oldest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM agent_jobs WHERE thread_id = $1 ORDER BY turn_no", thread_id
        )
    return [await store.conversation_queries.get_job(str(row["id"])) for row in rows]


async def test_a_clipped_assistant_message_is_recorded_as_incomplete(store, pool):
    """The completeness marker travels from the event to the row and back.

    A runtime that binds its text drops the *end* of a message, which is where
    the report block lives. That has to survive the trip: the event carries it,
    the stored message keeps it, and the read a report is taken from returns it,
    so the reader can tell "the model said no more" from "the transport stopped
    carrying what it said".
    """
    # While the attempt still holds its lease — which is when a runtime emits
    # the message in the first place.
    job, first, _ = await _live_goal_job(store)
    assert await store.attempt_commands.append_event(
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
        event_type="message",
        payload={"text": "a long answer that was cut off", "truncated": True},
    )
    # A runtime that did not clip says nothing, and the default is complete.
    assert await store.attempt_commands.append_event(
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
        event_type="message",
        payload={"text": "the whole answer"},
    )

    state = await store.conversation_queries.latest_assistant_message_state(job_id=str(job["id"]))
    assert state == {"content": "the whole answer", "truncated": False}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT content, truncated FROM agent_thread_messages "
            "WHERE job_id = $1 AND role = 'assistant' ORDER BY id",
            job["id"],
        )
    assert [(row["content"], row["truncated"]) for row in rows] == [
        ("a long answer that was cut off", True),
        ("the whole answer", False),
    ]


async def test_a_resume_on_a_conversation_with_no_repository_access_pauses(
    store, pool, monkeypatch: pytest.MonkeyPatch
):
    """An owner trigger ignores the last turn's result, not the conversation's state.

    Resume is authorized by the owner, so the round that paused the goal is not
    judged again — but the conversation still has to be one work can run in. A
    repository this deployment does not allow is the case a real deployment hits
    first, and leaving the goal silently active would show a green badge over
    work that nothing will ever run.
    """
    from cloud_agent.goals.controller import consume_wakeup

    job, first, goal = await _live_goal_job(store)
    await _finish(pool, first)
    assert await store.goals.pause_goal(
        thread_id=str(job["thread_id"]), revision=goal["revision"], reason="user"
    )

    # A repository outside the allowlist, and no App installation to fall back
    # on: entitlement is the one eligibility read that leaves the database.
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "someone-else/other-repo")
    resumed = await store.goals.resume_goal(
        thread_id=str(job["thread_id"]), user_id=job["user_id"], revision=goal["revision"] + 1
    )
    assert resumed is not None and resumed["state"] == "active"

    claimed = await store.goal_wakeups.claim_wakeups(limit=1)
    assert claimed, "resume did not record its obligation"
    assert await consume_wakeup(store=store, wakeup=claimed[0]) == "pause"

    after = await store.goals.get_goal(thread_id=str(job["thread_id"]))
    assert (after["state"], after["pause_reason"]) == ("paused", "access_denied")
    assert not [
        row
        for row in await _thread_turns(store, pool, str(job["thread_id"]))
        if row["turn_source"] == "goal" and row["state"] == "queued"
    ]


async def test_latest_turn_reports_the_conversations_real_state(
    store, pool, monkeypatch: pytest.MonkeyPatch
):
    """The eligibility read has to see archiving and a lost session.

    ``latest_turn`` is what a decision reads to ask "may work start here", and
    those answers live on the conversation and its sandbox rather than on the
    turn — so a projection that carries neither makes an archived conversation
    look idle, and a resume queues a round into a place nothing will run. This
    pins the two reads agreeing, which is the property the check depends on.
    """
    from cloud_agent.goals.controller import consume_wakeup

    job, first, goal = await _live_goal_job(store)
    thread_id = str(job["thread_id"])

    # A live conversation: both reads agree, and the sandbox is readable.
    latest = await store.conversation_queries.latest_turn(thread_id=thread_id)
    full = await store.conversation_queries.get_job(str(first["id"]))
    assert latest["id"] == full["id"]
    assert latest["archived_at"] == full["archived_at"] is None
    assert latest["thread_sandbox"] is not None

    await _finish(pool, first)
    assert await store.goals.pause_goal(
        thread_id=thread_id, revision=goal["revision"], reason="user"
    )

    # Archiving the conversation is visible to both reads.
    await store.conversation_commands.set_thread_archived(
        job_id=str(first["id"]), user_id=job["user_id"], archived=True
    )
    latest = await store.conversation_queries.latest_turn(thread_id=thread_id)
    full = await store.conversation_queries.get_job(str(first["id"]))
    assert latest["archived_at"] is not None
    assert latest["archived_at"] == full["archived_at"]

    # And a resume on it pauses with the reason, rather than queueing a round
    # into an archived conversation.
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "example-org/example-repo")
    resumed = await store.goals.resume_goal(
        thread_id=thread_id, user_id=job["user_id"], revision=goal["revision"] + 1
    )
    assert resumed is not None and resumed["state"] == "active"
    claimed = await store.goal_wakeups.claim_wakeups(limit=1)
    assert claimed, "resume did not record its obligation"
    assert await consume_wakeup(store=store, wakeup=claimed[0]) == "pause"
    after = await store.goals.get_goal(thread_id=thread_id)
    assert (after["state"], after["pause_reason"]) == ("paused", "archived")


async def test_a_resume_on_a_lost_session_pauses(store, pool, monkeypatch: pytest.MonkeyPatch):
    """A session that is gone stops a resume, and says which condition stopped it.

    The sandbox state is on the sandbox rather than the turn, so the eligibility
    read has to join it: a conversation whose retained session was lost cannot be
    resumed into one, and a round queued there would either never run or run
    without the history the owner resumed for.
    """
    from cloud_agent.goals.controller import consume_wakeup

    job, first, goal = await _live_goal_job(store)
    thread_id = str(job["thread_id"])
    await _finish(pool, first)
    assert await store.goals.pause_goal(
        thread_id=thread_id, revision=goal["revision"], reason="user"
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships SET context_status = 'lost', "
            "native_state = 'lost' WHERE thread_id = $1",
            thread_id,
        )

    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "example-org/example-repo")
    resumed = await store.goals.resume_goal(
        thread_id=thread_id, user_id=job["user_id"], revision=goal["revision"] + 1
    )
    assert resumed is not None and resumed["state"] == "active"
    claimed = await store.goal_wakeups.claim_wakeups(limit=1)
    assert claimed, "resume did not record its obligation"
    assert await consume_wakeup(store=store, wakeup=claimed[0]) == "pause"
    after = await store.goals.get_goal(thread_id=thread_id)
    assert (after["state"], after["pause_reason"]) == ("paused", "context_lost")


async def test_an_unbounded_goal_queues_round_fifty_one(store, pool, monkeypatch):
    """Bounded scheduling on real rows: the decision runs once, and queues one.

    Fifty is the ceiling the platform used to impose on every goal, and ten was
    its default. Neither exists any more, and this walks the whole path that
    used to enforce them — the SQL that counts a goal's charged rounds, the
    context built from it, and the rule that reads it — with fifty already
    spent and no bound stated. One wakeup is consumed and exactly one round is
    created: no agent is started, and nothing loops.
    """
    from cloud_agent.goals.controller import consume_wakeup

    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "example-org/example-repo")
    job, first, _ = await _live_goal_job(store)
    thread_id = str(job["thread_id"])
    await _finish(pool, first)
    # The bound this fixture's goal carries is lifted explicitly. Leaving the
    # field out would *keep* it, which is the rule the three cases above pin:
    # only a stated ``null`` removes a limit, so an ordinary edit cannot.
    goal = await store.goals.set_goal(
        thread_id=thread_id,
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=None),
        expected_revision=1,
    )
    assert goal["max_rounds"] is None

    last = None
    for number in range(1, 51):
        queued = await _round(store, job=job, goal=goal, round_number=number)
        claimed = await _claim_round(store)
        assert claimed["id"] == queued["id"]
        await _finish(pool, claimed)
        last = claimed
    assert await _rounds_used(store, thread_id) == 50

    # The obligation round fifty's finish owed, judged in the context that
    # obligation belongs to.
    wakeup_id = await store.goal_wakeups.arm_wakeup(
        thread_id=thread_id,
        goal_id=str(goal["goal_id"]),
        revision=goal["revision"],
        trigger="finish",
        trigger_ref=f"job:{last['id']}",
    )
    assert wakeup_id is not None
    # The newest obligation of this conversation is the one just armed: setting
    # the goal armed an owner wakeup of its own, and it is older.
    armed = await store.goal_wakeups.pending_wakeup(thread_id=thread_id)
    assert armed is not None and armed["id"] == wakeup_id

    assert await consume_wakeup(store=store, wakeup=armed) == "queue_work_round"

    rounds = [
        row for row in await _thread_turns(store, pool, thread_id) if row["turn_source"] == "goal"
    ]
    assert len(rounds) == 51
    assert rounds[-1]["goal_round"] == 51
    # The round says which one it is and no total: there is no total.
    assert '<goal_round kind="work" round="51">' in rounds[-1]["task_prompt"]
    after = await store.goals.get_goal(thread_id=thread_id)
    assert (after["state"], after["max_rounds"]) == ("active", None)


async def test_a_goal_at_its_own_bound_pauses_instead_of_queueing(store, pool, monkeypatch):
    """Removing the platform's ceiling is not removing the owner's own number."""
    from cloud_agent.goals.controller import consume_wakeup

    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "example-org/example-repo")
    job, first, _ = await _live_goal_job(store)
    thread_id = str(job["thread_id"])
    await _finish(pool, first)
    goal = await store.goals.set_goal(
        thread_id=thread_id,
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=1),
        expected_revision=1,
    )
    queued = await _round(store, job=job, goal=goal, round_number=1)
    claimed = await _claim_round(store)
    assert claimed["id"] == queued["id"]
    await _finish(pool, claimed)
    assert await _rounds_used(store, thread_id) == 1

    wakeup_id = await store.goal_wakeups.arm_wakeup(
        thread_id=thread_id,
        goal_id=str(goal["goal_id"]),
        revision=goal["revision"],
        trigger="finish",
        trigger_ref=f"job:{claimed['id']}",
    )
    assert wakeup_id is not None
    armed = await store.goal_wakeups.pending_wakeup(thread_id=thread_id)
    assert armed is not None and armed["id"] == wakeup_id

    assert await consume_wakeup(store=store, wakeup=armed) == "pause"

    after = await store.goals.get_goal(thread_id=thread_id)
    assert (after["state"], after["pause_reason"]) == ("paused", "round_limit")
    turns = await _thread_turns(store, pool, thread_id)
    assert len([row for row in turns if row["turn_source"] == "goal"]) == 1
