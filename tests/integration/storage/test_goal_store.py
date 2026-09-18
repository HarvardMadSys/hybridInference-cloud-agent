"""Real Postgres: the goal's revisions, counters, and the rounds it owns."""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
import pytest

from cloud_agent.goals.models import GoalConflict, GoalInput
from cloud_agent.goals.report import GoalReport
from tests.integration.storage.test_agent_job_store import (  # noqa: F401
    _SHARED_HOST_CAPABILITIES,
    _claim,
    _create_job,
    _shared_claim_attestation,
    pool,
    store,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _goal_job(store, **job_overrides):
    """A conversation whose goal is ``ship it`` for at most five rounds."""
    job = await _create_job(store, **job_overrides)
    goal = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=5),
    )
    assert goal is not None
    return job, goal


#: The host ``_live_goal_job`` reserves its group on. A re-claim has to name
#: the same one: a retained chat is hard-pinned to the machine that owns its
#: writable layer, and a claim from anywhere else is correctly refused.
GOAL_HOST = "goal-runner"


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


async def _live_goal_job(store, *, worker_id: str = "goal-worker", suffix: str = "a"):
    """A goal conversation inside a real retained shared group.

    Round creation and claiming both check the group's live context, so a test
    that exercises either needs one — the same fence production has, not a
    mock of it.
    """
    credential = f"{worker_id}-credential-hash"
    await store.host_pool.register_host(
        host="goal-runner",
        credential_hash=credential,
        capabilities=_SHARED_HOST_CAPABILITIES,
        slots_total=8,
    )
    online = await store.host_pool.record_host_heartbeat(
        host="goal-runner",
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_image_digest="sha256:shared-image",
    )
    assert online is not None and online["status"] == "online"
    job = await _create_job(store)
    claim = await _claim(
        store,
        worker_id=worker_id,
        lease_ttl_seconds=60,
        host="goal-runner",
        host_credential_hash=credential,
        claim_attestation=_shared_claim_attestation(),
    )
    assert claim is not None and claim["id"] == job["id"]
    sandbox = await store.sandbox.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="goal-runner",
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


async def _finish(pool, claim):
    """Make a claimed turn terminal, the way ``transition`` would.

    Written as raw SQL because these tests are about the goal counters, and
    going through ``transition`` would drag the retained-sandbox settlement
    invariants into every one of them.
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
            "WHERE host = 'goal-runner'"
        )


async def _round(
    store,
    *,
    parent: dict,
    kind: str = "work",
    round_number: int = 1,
    goal_revision: int = 1,
    client_request_id: str | None = None,
    client_request_fingerprint: str | None = None,
):
    """A queued automatic round, created through the store's own follow-up path."""
    return await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"],
        user_id=parent["user_id"],
        prompt=f'<goal_round kind="{kind}">',
        turn_source="goal",
        goal_round=round_number,
        goal_round_kind=kind,
        goal_revision=goal_revision,
        client_request_id=client_request_id,
        client_request_fingerprint=client_request_fingerprint,
    )


async def test_set_replace_pause_resume_and_clear_advance_the_revision(store, pool):
    job, goal = await _goal_job(store)
    assert (goal["state"], goal["revision"], goal["rounds_used"]) == ("active", 1, 0)

    replaced = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it safely", max_rounds=7),
        expected_revision=1,
    )
    assert replaced["revision"] == 2
    assert replaced["objective"] == "ship it safely"
    assert replaced["max_rounds"] == 7

    assert await store.goals.pause_goal(thread_id=job["thread_id"], revision=2, reason="user")
    paused = await store.goals.get_goal(thread_id=job["thread_id"])
    assert (paused["state"], paused["pause_reason"], paused["revision"]) == ("paused", "user", 3)

    # A second pause is not a state change, and must not advance the revision.
    assert not await store.goals.pause_goal(thread_id=job["thread_id"], revision=3, reason="user")

    assert await store.goals.resume_goal(thread_id=job["thread_id"], revision=3)
    resumed = await store.goals.get_goal(thread_id=job["thread_id"])
    assert (resumed["state"], resumed["pause_reason"], resumed["revision"]) == ("active", None, 4)

    assert await store.goals.clear_goal(thread_id=job["thread_id"], user_id=job["user_id"])
    assert await store.goals.get_goal(thread_id=job["thread_id"]) is None
    # Removing a goal keeps the counter moving, so a round created against the
    # removed goal can never be mistaken for one created against the next.
    assert await store.goals.clear_goal(thread_id=job["thread_id"], user_id=job["user_id"]) is False


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


async def test_the_round_budget_restarts_only_when_the_objective_changes(store, pool):
    job, _ = await _goal_job(store)
    await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=5),
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET goal_rounds_used = 3, goal_blocked_streak = 2 WHERE id = $1",
            job["thread_id"],
        )
    # Same objective, new cap: the rounds already spent stay spent, but the
    # streak does not survive the owner looking at the goal again.
    same = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=9),
    )
    assert (same["rounds_used"], same["blocked_streak"], same["revision"]) == (3, 0, 3)

    changed = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it behind a flag", max_rounds=9),
    )
    assert (changed["rounds_used"], changed["revision"]) == (0, 4)


async def test_a_new_objective_cancels_the_rounds_queued_for_the_old_one(store):
    job, _ = await _goal_job(store)
    queued = await _round(store, parent=job)
    assert queued["state"] == "queued"
    await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="something else entirely"),
    )
    after = await store.conversation_queries.get_job(queued["id"])
    assert after["state"] == "cancelled"


async def test_claiming_a_work_round_charges_it_once(store, pool):
    job, first, _ = await _live_goal_job(store)
    goal_after_first = await store.goals.get_goal(thread_id=job["thread_id"])
    # The conversation-creating turn is a person's, and spends nothing.
    assert goal_after_first["rounds_used"] == 0

    await _finish(pool, first)
    work = await _round(store, parent=job)
    claimed = await _claim_round(store)
    assert claimed["id"] == work["id"]
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["rounds_used"] == 1

    # A second attempt at the same round does not spend the budget twice.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'queued', current_attempt_id = NULL WHERE id = $1",
            work["id"],
        )
        await conn.execute(
            "UPDATE agent_attempts SET status = 'superseded', finished_at = NOW() WHERE id = $1",
            claimed["attempt_id"],
        )
    second = await _claim_round(store)
    assert second["id"] == work["id"]
    assert second["attempt_no"] == 2
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["rounds_used"] == 1


async def test_a_verification_round_is_never_charged(store, pool):
    job, first, _ = await _live_goal_job(store)
    await _finish(pool, first)
    await _round(store, parent=job, kind="verify", round_number=0)
    claimed = await _claim_round(store)
    assert claimed["goal_round_kind"] == "verify"
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["rounds_used"] == 0


async def test_a_round_created_for_a_replaced_goal_does_not_spend_the_new_budget(store, pool):
    """The revision a round carries is the goal it was created for.

    Replacing the objective is what normally disposes of a round made for the
    old one. The guard matters for the round that survives anyway — one whose
    start was requested before the edit landed — and it is checked at claim
    time, where the counter is charged.
    """
    job, first, _ = await _live_goal_job(store)
    await _finish(pool, first)
    stale = await _round(store, parent=job, goal_revision=1)
    await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="a different objective"),
    )
    # Put the round back in the queue carrying the revision it was created for,
    # which is what a claim racing the edit would have found.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'queued', cancel_requested = FALSE WHERE id = $1",
            stale["id"],
        )
    claimed = await _claim_round(store)
    assert claimed["id"] == stale["id"]
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["rounds_used"] == 0


async def test_transition_records_the_report_the_turn_made(store):
    job, _ = await _goal_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="cancelled",
        goal_report=GoalReport(status="continue", reason="two tests still fail"),
    )
    saved = await store.conversation_queries.get_job(job["id"])
    assert (saved["goal_report"], saved["goal_reason"]) == ("continue", "two tests still fail")


async def test_a_report_with_no_reason_is_still_a_report(store):
    job, _ = await _goal_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
        goal_report=GoalReport(status="blocked"),
    )
    saved = await store.conversation_queries.get_job(job["id"])
    assert (saved["goal_report"], saved["goal_reason"]) == ("blocked", None)


async def test_cancelling_a_queued_goal_round_pauses_the_goal(store):
    job, _, goal = await _live_goal_job(store)
    queued = await _round(store, parent=job)
    assert await store.attempt_commands.request_cancel(job_id=queued["id"]) == "cancelled"
    after = await store.goals.get_goal(thread_id=job["thread_id"])
    assert (after["state"], after["pause_reason"]) == ("paused", "cancelled")
    assert after["revision"] == goal["revision"] + 1


async def test_cancelling_a_persons_queued_turn_leaves_the_goal_alone(store):
    job, _, _ = await _live_goal_job(store)
    human = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="actually, also do this",
    )
    assert await store.attempt_commands.request_cancel(job_id=human["id"]) == "cancelled"
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["state"] == "active"


async def test_a_replayed_round_creation_makes_one_row(store):
    job, _, _ = await _live_goal_job(store)
    key = f"goal:{job['thread_id']}:1:work:{job['id']}"
    first = await _round(store, parent=job, client_request_id=key, client_request_fingerprint="fp")
    second = await _round(store, parent=job, client_request_id=key, client_request_fingerprint="fp")
    assert first["id"] == second["id"]
    turns = await store.conversation_queries.list_jobs(user_id=job["user_id"])
    assert len([turn for turn in turns if turn["turn_source"] == "goal"]) == 1


async def test_a_report_made_through_the_goal_tool_is_fenced_to_its_attempt(store):
    """The tool writes the same two columns the message parser writes."""
    job, claim, _ = await _live_goal_job(store)
    assert await store.goals.record_goal_report(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        status="complete",
        reason="pytest: 42 passed",
    )
    saved = await store.conversation_queries.get_job(job["id"])
    assert (saved["goal_report"], saved["goal_reason"]) == ("complete", "pytest: 42 passed")

    # A generation that is not the live one is refused: a superseded turn must
    # not relabel what its successor is doing.
    assert not await store.goals.record_goal_report(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"] + 1,
        status="blocked",
        reason="should not land",
    )
    assert (await store.conversation_queries.get_job(job["id"]))["goal_report"] == "complete"


async def test_an_unknown_attempt_is_not_a_report(store):
    job, _, _ = await _live_goal_job(store)
    assert not await store.goals.record_goal_report(
        job_id=job["id"],
        attempt_id=999_999,
        lease_generation=1,
        status="continue",
        reason=None,
    )
    assert (await store.conversation_queries.get_job(job["id"]))["goal_report"] is None


async def test_a_tool_report_outranks_the_text_convention(store):
    """The structured act wins over whatever the closing prose happened to say.

    Both write ``goal_report``. A turn that called ``report_goal`` and then
    narrated a different status must be judged on the call: it is the one the
    model made deliberately, and the one the platform acknowledged at the time.
    """
    job, claim, _ = await _live_goal_job(store)
    await store.goals.record_goal_report(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        status="continue",
        reason="two tests still fail",
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="cancelled",
        goal_report=GoalReport(status="complete", reason="the prose said done"),
    )
    saved = await store.conversation_queries.get_job(job["id"])
    assert (saved["goal_report"], saved["goal_reason"]) == ("continue", "two tests still fail")


async def test_the_text_convention_still_lands_when_no_tool_was_called(store, pool):
    """The text convention is the fallback, and still carries a turn that called nothing."""
    job, claim, _ = await _live_goal_job(store)
    await _finish(pool, claim)
    round_job = await _round(store, parent=job)
    claimed = await _claim_round(store)
    assert claimed["id"] == round_job["id"]
    assert await store.attempt_commands.transition(
        job_id=round_job["id"],
        attempt_id=claimed["attempt_id"],
        lease_generation=claimed["lease_generation"],
        from_states=("running",),
        to_state="cancelled",
        goal_report=GoalReport(status="blocked", reason="no network"),
    )
    saved = await store.conversation_queries.get_job(round_job["id"])
    assert (saved["goal_report"], saved["goal_reason"]) == ("blocked", "no network")


async def test_a_cancelled_round_frees_its_idempotency_key_for_the_next_one(store, pool):
    """The key names the parent turn, so a cancelled round cannot shadow it.

    ``client_request_id`` is what makes a replayed finish produce no second
    round. Built from a round *number* it would be reused after a cancelled
    round was replaced, resolve to the cancelled row, and silently queue
    nothing — the failure this test exists to prevent.
    """
    job, first, _ = await _live_goal_job(store)
    await _finish(pool, first)
    cancelled = await _round(
        store,
        parent=job,
        client_request_id=f"goal:{job['thread_id']}:1:work:{job['id']}",
        client_request_fingerprint="fp",
    )
    assert await store.attempt_commands.request_cancel(job_id=cancelled["id"]) == "cancelled"

    # The owner resumes: the next round hangs from the same parent, and its key
    # must differ from the cancelled one's.
    await store.goals.resume_goal(
        thread_id=job["thread_id"],
        revision=(await store.goals.get_goal(thread_id=job["thread_id"]))["revision"],
    )
    replacement = await _round(
        store,
        parent=job,
        client_request_id=f"goal:{job['thread_id']}:1:work:{job['id']}:again",
        client_request_fingerprint="fp",
    )
    assert replacement["id"] != cancelled["id"]
    assert replacement["state"] == "queued"


async def test_the_decision_is_written_fenced_by_the_revision(store):
    from cloud_agent.goals.decision import GoalAction

    job, goal = await _goal_job(store)
    assert await store.goals.apply_action(
        thread_id=job["thread_id"],
        revision=goal["revision"],
        action=GoalAction(kind="complete"),
    )
    after = await store.goals.get_goal(thread_id=job["thread_id"])
    assert (after["state"], after["revision"]) == ("complete", goal["revision"] + 1)
    # The same decision applied twice is a no-op the second time: the owner's
    # write (or the first application) already moved the goal on.
    assert not await store.goals.apply_action(
        thread_id=job["thread_id"],
        revision=goal["revision"],
        action=GoalAction(kind="complete"),
    )


async def test_archiving_a_conversation_pauses_and_cancels_its_rounds(store):
    job, _, _ = await _live_goal_job(store)
    queued = await _round(store, parent=job)
    assert await store.conversation_commands.set_thread_archived(
        job_id=job["id"], user_id=job["user_id"], archived=True
    )
    goal = await store.goals.get_goal(thread_id=job["thread_id"])
    assert (goal["state"], goal["pause_reason"]) == ("paused", "archived")
    assert (await store.conversation_queries.get_job(queued["id"]))["state"] == "cancelled"


async def test_has_pending_turn_sees_queued_and_running_work(store, pool):
    job, claim, _ = await _live_goal_job(store)
    assert await store.goals.has_pending_turn(thread_id=job["thread_id"])
    await _finish(pool, claim)
    assert not await store.goals.has_pending_turn(thread_id=job["thread_id"])
    await _round(store, parent=job)
    assert await store.goals.has_pending_turn(thread_id=job["thread_id"])


async def test_pause_stalled_goals_only_touches_quiet_conversations(store, pool):
    job, first, _ = await _live_goal_job(store)
    quiet_job, quiet_first, _ = await _live_goal_job(store, worker_id="goal-worker-2", suffix="b")
    # A conversation with a turn still running is not quiet, and neither is one
    # with an automatic round waiting.
    await _finish(pool, first)
    await _finish(pool, quiet_first)
    queued = await _round(store, parent=quiet_job)
    # Settled an hour ago: this one is quiet. The other still has a round
    # waiting, so its age does not matter.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET updated_at = NOW() - interval '1 hour' WHERE id = $1",
            job["id"],
        )
    stalled = await store.goals.pause_stalled_goals()
    assert stalled == [job["thread_id"]]
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["pause_reason"] == "stalled"
    # The one with work still queued is not stalled, whatever its timestamps say.
    assert (await store.goals.get_goal(thread_id=quiet_job["thread_id"]))["state"] == "active"
    assert (await store.conversation_queries.get_job(queued["id"]))["state"] == "queued"
