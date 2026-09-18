"""Real Postgres: the goal's revisions, counters, and the rounds it owns."""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
import pytest

from cloud_agent.goals.decision import GoalAction
from cloud_agent.goals.models import MAX_REASON_CHARS, GoalConflict, GoalError, GoalInput
from cloud_agent.goals.report import GoalReport
from tests.integration.storage.test_agent_job_store import (  # noqa: F401
    _SHARED_HOST_CAPABILITIES,
    _claim,
    _create_job,
    _expire_attempt,
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


async def test_editing_only_the_cap_keeps_the_rounds_already_spent(store, pool):
    """Rounds spent are derived from the rounds that started, per revision.

    Two facts, one test, because they are the same fact seen twice: the count is
    not a column anybody writes, so it cannot be reset by an unrelated edit —
    and replacing the objective *is* related, and does reset it.
    """
    job, first, _ = await _live_goal_job(store)
    await _finish(pool, first)
    await _round(store, parent=job)
    await _claim_round(store)
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["rounds_used"] == 1

    # Same objective, new cap: the derived count looks at the *revision*, and
    # editing only the cap would otherwise hand back a fresh budget.
    same = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it", max_rounds=9),
    )
    assert (same["rounds_used"], same["blocked_streak"]) == (1, 0)

    changed = await store.goals.set_goal(
        thread_id=job["thread_id"],
        user_id=job["user_id"],
        goal=GoalInput(objective="ship it behind a flag", max_rounds=9),
    )
    # A different objective mints a revision, and no round carries it yet.
    assert changed["rounds_used"] == 0


async def test_a_round_queued_but_never_started_costs_nothing(store, pool):
    """The charge is the attempt, not the queue entry."""
    job, first, _ = await _live_goal_job(store)
    await _finish(pool, first)
    queued = await _round(store, parent=job)
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["rounds_used"] == 0
    assert queued["state"] == "queued"


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
    # The conversation-creating turn is a person's, and spends nothing.
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["rounds_used"] == 0

    await _finish(pool, first)
    work = await _round(store, parent=job)
    claimed = await _claim_round(store)
    assert claimed["id"] == work["id"]
    assert (await store.goals.get_goal(thread_id=job["thread_id"]))["rounds_used"] == 1

    # A second attempt at the same round does not spend the budget twice: the
    # count is over distinct rounds, not over attempts.
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


async def test_get_goal_for_job_reads_across_the_join(store):
    """The one joined goal read. Unqualified, `goal_revision` is ambiguous here.

    ``0017`` puts that column on ``agent_jobs`` as well — the revision a turn
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


async def test_the_third_consecutive_blocked_report_blocks_the_goal(store):
    """The block action carries the code it is required to carry.

    Applied through the real repository, because the fake used to skip this
    check: a ``block`` with no ``blocked_code`` passes a lenient fake and raises
    ``GoalError`` in Postgres, which is exactly how "blocked three turns
    running" was green in CI and dead in production.
    """
    job, goal = await _goal_job(store)
    action = GoalAction(
        kind="block", reason_code="model-reported", message="no network", blocked_streak=3
    )
    written = await store.goals.apply_action(
        thread_id=job["thread_id"],
        revision=goal["revision"],
        action=action,
        blocked_code="model-reported",
    )
    assert written is not None
    assert (written["state"], written["blocked_code"]) == ("blocked", "model-reported")
    assert (written["blocked_message"], written["blocked_streak"]) == ("no network", 3)


async def test_a_terminal_action_without_its_reason_is_refused(store):
    """``_action_columns`` validates, and that validation is load-bearing.

    A pause that cannot say why it paused is a goal the owner cannot act on;
    refusing the write is how that stays impossible rather than merely unusual.
    """
    job, goal = await _goal_job(store)
    with pytest.raises(GoalError):
        await store.goals.apply_action(
            thread_id=job["thread_id"],
            revision=goal["revision"],
            action=GoalAction(kind="pause"),
        )
    with pytest.raises(GoalError):
        await store.goals.apply_action(
            thread_id=job["thread_id"],
            revision=goal["revision"],
            action=GoalAction(kind="block", blocked_streak=3),
        )


async def test_a_streak_write_leaves_the_reason_it_is_not_changing(store):
    """A write that changes no state does not clear why the state is what it is.

    Recording a blocked streak means applying a decision whose kind is ``none``.
    That used to null the code and the message on the way past — silently, since
    the revision does not move either — and the band was left saying "blocked"
    with no way to say why.
    """
    job, goal = await _goal_job(store)
    await store.goals.apply_action(
        thread_id=job["thread_id"],
        revision=goal["revision"],
        action=GoalAction(
            kind="block", reason_code="model-reported", message="no network", blocked_streak=3
        ),
        blocked_code="model-reported",
    )
    blocked = await store.goals.get_goal(thread_id=job["thread_id"])
    assert await store.goals.apply_action(
        thread_id=job["thread_id"],
        revision=blocked["revision"],
        action=GoalAction(kind="none", blocked_streak=0),
    )
    after = await store.goals.get_goal(thread_id=job["thread_id"])
    assert (after["state"], after["blocked_code"], after["blocked_message"]) == (
        "blocked",
        "model-reported",
        "no network",
    )


async def test_an_overlong_reason_is_bounded_at_the_write(store):
    """Both report paths pass through here, so the bound lives here."""
    job, claim, _ = await _live_goal_job(store)
    assert await store.goals.record_goal_report(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        status="continue",
        reason="x" * (MAX_REASON_CHARS + 500),
    )
    saved = await store.conversation_queries.get_job(job["id"])
    assert len(saved["goal_reason"]) == MAX_REASON_CHARS


async def test_a_follow_up_that_replaces_the_objective_cancels_the_old_round(store):
    """The UI's main path for setting a goal, and it has to obey the same rule.

    A queued round carries a ``<goal_round>`` body naming the objective *it* was
    created for. Leaving it queued runs a turn toward an objective its owner
    just replaced — with the new objective prefixed to it besides — and pays
    for it.
    """
    job, _, _ = await _live_goal_job(store)
    queued = await _round(store, parent=job)
    follow = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="actually, do something else",
        goal=GoalInput(objective="roll the change back", max_rounds=5),
    )
    assert follow is not None and follow.get("error") is None
    assert (await store.conversation_queries.get_job(queued["id"]))["state"] == "cancelled"


async def test_a_round_reports_for_itself_after_a_retry(store, pool):
    """A claim handed back clears the answer the abandoned attempt left.

    Otherwise a retry inherits a ``complete`` from a turn whose work was thrown
    away, and a verification round that *failed* on its second attempt would
    complete the goal on the strength of its first.
    """
    job, first, _ = await _live_goal_job(store)
    await _finish(pool, first)
    await _round(store, parent=job)
    claimed = await _claim_round(store)
    assert claimed is not None
    assert await store.goals.record_goal_report(
        job_id=claimed["id"],
        attempt_id=claimed["attempt_id"],
        lease_generation=claimed["lease_generation"],
        status="complete",
        reason="prose said done",
    )
    assert await store.attempt_commands.release_claim(
        job_id=claimed["id"],
        attempt_id=claimed["attempt_id"],
        lease_generation=claimed["lease_generation"],
    )
    saved = await store.conversation_queries.get_job(claimed["id"])
    assert (saved["state"], saved["goal_report"], saved["goal_reason"]) == (
        "queued",
        None,
        None,
    )


async def test_a_reaped_round_reports_for_itself_too(store, pool):
    """The same rule on the reaper's requeue branch."""
    job, first, _ = await _live_goal_job(store)
    await _finish(pool, first)
    await _round(store, parent=job)
    claimed = await _claim_round(store)
    assert claimed is not None
    assert await store.goals.record_goal_report(
        job_id=claimed["id"],
        attempt_id=claimed["attempt_id"],
        lease_generation=claimed["lease_generation"],
        status="complete",
        reason=None,
    )
    await _expire_attempt(pool, store, claimed["attempt_id"])
    actions = await store.attempt_commands.reap_expired(max_attempts=3)
    assert [action["action"] for action in actions] == ["queued"]
    assert (await store.conversation_queries.get_job(claimed["id"]))["goal_report"] is None


async def test_the_newest_turn_that_ran_is_the_one_resumed_from(store, pool):
    """A cancelled turn is not somewhere new work can hang from.

    The boolean leads and ``turn_no`` breaks the tie, and it has to sort
    *ascending*: ``TRUE > FALSE`` in Postgres, so ``DESC`` returns the cancelled
    turn first — which made "Resume" re-pause the goal it had just resumed, from
    inside the resume request, because the decision read the cancelled turn's
    state as the outcome being continued.

    Built out of ordinary turns rather than automatic rounds, because cancelling
    a round pauses the goal and a paused goal queues nothing after it.
    """
    job, first, _ = await _live_goal_job(store)
    await _finish(pool, first)
    doomed = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id=job["user_id"], prompt="never mind"
    )
    survivor = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id=job["user_id"], prompt="this one"
    )
    assert await store.attempt_commands.request_cancel(job_id=doomed["id"]) == "cancelled"
    await _finish(pool, await _claim_round(store))

    newest = await store.conversation_queries.latest_terminal_turn(thread_id=job["thread_id"])
    assert newest is not None
    assert newest["id"] == survivor["id"]
    assert newest["state"] == "succeeded"


async def test_a_queued_round_is_what_makes_a_round_yield(store):
    """The yield rule, asked inside the transaction that would append the round.

    A caller-side check leaves a window: it reads "nothing pending", somebody
    types, and the automatic round lands *after* their message. Asking under the
    thread lock is the only way the answer cannot go stale.
    """
    job, _, _ = await _live_goal_job(store)
    await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"], user_id=job["user_id"], prompt="wait for me"
    )
    yielded = await store.conversation_commands.create_follow_up(
        parent_job_id=job["id"],
        user_id=job["user_id"],
        prompt="round",
        turn_source="goal",
        goal_round=1,
        goal_round_kind="work",
        goal_revision=1,
        yield_to_queued=True,
    )
    assert yielded == {"error": "yielded"}


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
