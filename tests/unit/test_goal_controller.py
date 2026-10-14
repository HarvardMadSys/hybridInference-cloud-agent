"""The wakeup consumer: what the platform writes after it judges a goal.

The decision itself is pure and tested on its own. This module is everything
around it — reading the facts a decision needs, running it, and writing the
answer *together with* the wakeup's completion. The store below is a small,
stateful model of the four repositories the consumer reaches, so each case can
assert on the rows it actually wrote: the round that was queued, the goal that
was paused, the wakeup that was finished or parked.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from cloud_agent.domain.pending_messages import SettleOutcome
from cloud_agent.goals.controller import (
    client_request_id_for,
    consume_wakeup,
    continue_after_finish,
    run_goal_wakeups,
)
from cloud_agent.goals.models import MAX_WAKEUP_ATTEMPTS
from cloud_agent.goals.retry_policy import wakeup_backoff
from cloud_agent.storage.repositories.goals import _action_columns

pytestmark = pytest.mark.asyncio

THREAD_ID = "athr_1"
GOAL_ID = "goal-1"
REPO = "owner/name"


def goal_row(
    *,
    state: str = "active",
    goal_id: str = GOAL_ID,
    revision: int = 1,
    objective: str = "ship it",
    # No bound by default: the goal the product creates is unbounded, and a test
    # about a stated bound passes one explicitly.
    max_rounds: int | None = None,
    rounds_used: int = 0,
    repairs_used: int = 0,
    pause_reason: str | None = None,
    pause_detail: str | None = None,
) -> dict[str, Any]:
    """One conversation's goal, as the store reads it back."""
    return {
        "goal_id": goal_id,
        "objective": objective,
        "state": state,
        "revision": revision,
        "max_rounds": max_rounds,
        "rounds_used": rounds_used,
        "repairs_used": repairs_used,
        "pause_reason": pause_reason,
        "pause_detail": pause_detail,
        "completion_basis": None,
        "completed_by_job_id": None,
    }


def turn(
    *,
    id: str,
    state: str = "succeeded",
    turn_source: str = "human",
    goal_id: str | None = None,
    round_kind: str | None = None,
    report_parse: str | None = None,
    report_outcome: str | None = None,
    report_reason: str | None = None,
    report_detail: str | None = None,
    detail: str | None = None,
    input_seq_at_create: int = 0,
    turn_no: int = 1,
    archived_at: str | None = None,
    runtime: str = "claude-code",
) -> dict[str, Any]:
    """One turn of the conversation, with only the fields a decision reads.

    ``input_seq_at_create`` is the person-message counter this turn was created
    at, which is what ``_has_newer_input`` compares against the thread's. A
    person's turn lands on the value it produced; an automatic round carries
    the value it saw when the platform created it.
    """
    return {
        "id": id,
        "thread_id": THREAD_ID,
        "user_id": "user-1",
        "repo": REPO,
        "state": state,
        "turn_no": turn_no,
        "turn_source": turn_source,
        "goal_id": goal_id,
        "round_goal_revision": 1 if round_kind else None,
        "goal_round_kind": round_kind,
        "goal_report_parse": report_parse,
        "goal_report_outcome": report_outcome,
        # The model's own sentence and the parser's reason it refused a block
        # are two different columns, and only the first is a report's content.
        "goal_report_reason": report_reason,
        "goal_report_detail": report_detail,
        "detail": detail,
        "runtime": runtime,
        "input_seq_at_create": input_seq_at_create,
        "archived_at": archived_at,
        "thread_sandbox": None,
    }


class _Transaction:
    """One fake transaction, over the single thread these cases run on."""

    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class FakeWakeups:
    """The durable queue, modelled with a clock the test advances."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.rows: list[dict[str, Any]] = []

    def arm(
        self,
        *,
        trigger: str,
        trigger_ref: str,
        goal_id: str | None = GOAL_ID,
        revision: int = 1,
        thread_id: str = THREAD_ID,
    ) -> dict[str, Any]:
        """Record one obligation, idempotent on the event while it is pending."""
        for row in self.rows:
            if (
                row["state"] == "pending"
                and row["thread_id"] == thread_id
                and row["trigger"] == trigger
                and row["trigger_ref"] == trigger_ref
            ):
                return row
        row = {
            "id": f"wakeup_{len(self.rows) + 1}",
            "thread_id": thread_id,
            "goal_id": goal_id,
            "revision": revision,
            "trigger": trigger,
            "trigger_ref": trigger_ref,
            "state": "pending",
            "waiting_job_id": None,
            "attempts": 0,
            "detail": None,
            "lease_expires_at": None,
            "lease_owner": None,
            "not_before": 0.0,
        }
        self.rows.append(row)
        return row

    async def claim_wakeups(
        self, *, limit: int = 1, thread_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Lease what is due, oldest first: pending, unparked, past any backoff, unleased.

        ``thread_id`` narrows it to one conversation, as the store does.
        """
        now = self._store.clock
        due = [
            row
            for row in self.rows
            if row["state"] == "pending"
            and (thread_id is None or row["thread_id"] == thread_id)
            and row["waiting_job_id"] is None
            and row["not_before"] <= now
            and (row["lease_expires_at"] is None or row["lease_expires_at"] < now)
        ][:limit]
        for row in due:
            row["attempts"] += 1
            row["lease_expires_at"] = now + 120.0
            row["lease_owner"] = uuid4().hex
        return [dict(row) for row in due]

    async def release_wakeup(
        self,
        *,
        wakeup_id: str,
        waiting_job_id: str | None = None,
        retry_in_seconds: float | None = None,
        detail: str | None = None,
        conn: Any | None = None,
        lease_owner: str | None = None,
        restore_attempt: bool = False,
    ) -> None:
        """Give the lease back, parking behind a turn or waiting out a backoff."""
        row = self._row(wakeup_id)
        if lease_owner is not None and not self._live_lease(row, lease_owner):
            return
        row["lease_expires_at"] = None
        row["lease_owner"] = None
        # Only a backoff keeps the attempt the claim spent, as in the store.
        if restore_attempt or retry_in_seconds is None:
            row["attempts"] = max(0, row["attempts"] - 1)
        if retry_in_seconds is not None:
            # A backoff defers ``not_before``, never the lease: "do not touch
            # this yet" and "a worker may have died" are different facts.
            row["not_before"] = self._store.clock + retry_in_seconds
        if waiting_job_id is not None:
            row["waiting_job_id"] = waiting_job_id
        if detail is not None:
            row["detail"] = detail

    async def finish_wakeup(
        self,
        *,
        wakeup_id: str,
        processed: bool = True,
        detail: str | None = None,
        conn: Any | None = None,
        lease_owner: str | None = None,
    ) -> None:
        """Mark the obligation settled, with what was decided about it."""
        row = self._row(wakeup_id)
        if lease_owner is not None and not self._live_lease(row, lease_owner):
            return
        row["state"] = "processed" if processed else "pending"
        row["lease_expires_at"] = None
        row["lease_owner"] = None
        if detail is not None:
            row["detail"] = detail

    async def lock_wakeup(self, *, wakeup_id: str, lease_owner: str, conn: Any) -> bool:
        """Verify the snapshot still owns the pending wakeup before changing its goal."""
        assert conn is not None
        return self._live_lease(self._row(wakeup_id), lease_owner)

    def _live_lease(self, row: dict[str, Any], lease_owner: str) -> bool:
        return (
            row["state"] == "pending"
            and row["lease_owner"] == lease_owner
            and row["lease_expires_at"] is not None
            and row["lease_expires_at"] > self._store.clock
        )

    def transaction(self) -> _Transaction:
        """One transaction over the state a whole decision reads and writes."""
        return _Transaction()

    def _row(self, wakeup_id: str) -> dict[str, Any]:
        return next(row for row in self.rows if row["id"] == wakeup_id)


class FakeGoals:
    """Goal state, fenced by the revision the decision was made against."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    def _goal(self, thread_id: str) -> dict[str, Any] | None:
        return self._store.goal

    async def get_goal(self, *, thread_id: str, conn: Any | None = None) -> dict[str, Any] | None:
        return None if self._store.goal is None else dict(self._store.goal)

    async def lock_goal_state(self, *, thread_id: str, conn: Any) -> dict[str, Any] | None:
        return None if self._store.goal is None else dict(self._store.goal)

    async def has_pending_turn(self, *, thread_id: str, conn: Any | None = None) -> bool:
        return self._store.live_turn() is not None

    async def live_turn_id(self, *, thread_id: str, conn: Any | None = None) -> str | None:
        live = self._store.live_turn()
        if live is not None:
            # The read succeeded; whether the turn is still live for the *next*
            # write is the race this models.
            self._store.settle_live_turn()
            return str(live["id"])
        self._store.settle_live_turn()
        return None

    async def apply_action(
        self,
        *,
        thread_id: str,
        revision: int,
        action: Any,
        pause_reason: str | None = None,
        pause_detail: str | None = None,
        completed_by_job_id: str | None = None,
        conn: Any | None = None,
    ) -> dict[str, Any] | None:
        goal = self._store.goal
        if goal is None or goal["revision"] != revision:
            return None
        # The real column mapping, so a terminal action that cannot name its
        # reason or its completing turn fails here the way Postgres refuses it.
        state, reason, detail, basis, completed_by = _action_columns(
            action, pause_reason, pause_detail, completed_by_job_id
        )
        if state is None:
            return dict(goal)
        if state == "complete" and not completed_by:
            raise ValueError("a completed goal has to name the turn that earned it")
        goal.update(
            state=state,
            pause_reason=reason,
            pause_detail=detail,
            completion_basis=basis,
            completed_by_job_id=completed_by,
            revision=revision + 1,
        )
        self._store.cancel_queued_rounds()
        return dict(goal)


class FakeConversationCommands:
    """Appending turns, including the automatic rounds a decision creates."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def create_goal_round(
        self,
        *,
        conn: Any,
        thread_id: str,
        goal_id: str,
        revision: int,
        kind: str,
        round_number: int,
        prompt: str,
        client_request_id: str,
        client_request_fingerprint: str,
        wakeup_id: str | None = None,
    ) -> dict[str, Any] | None:
        if self._store.fail_rounds > 0:
            self._store.fail_rounds -= 1
            raise RuntimeError("the queue is unavailable")
        if any(
            existing["thread_id"] == thread_id
            and existing.get("client_request_id") == client_request_id
            for existing in self._store.turns
        ):
            # This event already produced a round; a replayed decision finds it
            # rather than starting a second turn.
            if wakeup_id is not None:
                await self._store.goal_wakeups.finish_wakeup(wakeup_id=wakeup_id, detail="replayed")
            return None
        created = turn(
            id=f"round_{round_number}",
            state="queued",
            turn_source="goal",
            goal_id=goal_id,
            round_kind=kind,
            input_seq_at_create=self._store.input_seq,
            turn_no=len(self._store.turns) + 1,
        )
        created.update(
            {
                "parent_job_id": self._store.latest_turn_id(),
                "goal_round": round_number,
                "task_prompt": prompt,
                "client_request_id": client_request_id,
            }
        )
        self._store.turns.append(created)
        if wakeup_id is not None:
            await self._store.goal_wakeups.finish_wakeup(wakeup_id=wakeup_id)
        return created


class FakeConversationQueries:
    """Reading turns, with the thread's live input counter stamped on them."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def latest_turn(self, *, thread_id: str) -> dict[str, Any] | None:
        newest = self._store.latest_turn()
        return None if newest is None else self._stamped(newest)

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        for existing in self._store.turns:
            if existing["id"] == job_id:
                return self._stamped(existing)
        return None

    async def get_job_for_update(self, *, conn: Any, job_id: str) -> dict[str, Any] | None:
        """The same read, under the caller's transaction.

        A real one takes the turn's row lock; this fake is single-threaded, so
        obeying ``conn`` and answering the same row is the whole of it.
        """
        assert conn is not None
        return await self.get_job(job_id)

    def _stamped(self, row: dict[str, Any]) -> dict[str, Any]:
        # ``JOB_SELECT`` reads the thread's counter live, not the turn's copy.
        return {**row, "thread_user_input_seq": self._store.input_seq}


class FakeSourceControl:
    """The entitlement port ``_check_access`` may consult; never reached here."""

    async def list_repo_grants(self, *, user_id: str) -> list[dict[str, Any]]:
        return []


class FakeGateway:
    """The Gateway's Agent access decision for the conversation's owner.

    Allowed unless a case withdraws it. ``unavailable`` makes the lookup fail
    the way an unreachable Gateway does, which is not an answer at all.
    """

    def __init__(self) -> None:
        self.allowed = True
        self.unavailable = False
        self.asked: list[str] = []

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        from cloud_agent.gateway import GatewayError

        self.asked.append(user_id)
        if self.unavailable:
            raise GatewayError("gateway unreachable")
        return {
            "user_id": user_id,
            "allowed": self.allowed,
            "permissions": ["agent.use"] if self.allowed else [],
        }


class FakePendingMessages:
    """The owner's waiting messages: none, unless a case puts one in line.

    Settling sends the first one as a person's turn when nothing is running,
    which is all the consumer needs of the real settlement: whether it just
    started a turn the goal has to wait behind.
    """

    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.waiting: list[str] = []

    async def settle_queue(self, *, thread_id: str, prepared: Any = None) -> SettleOutcome:
        if not self.waiting or self._store.live_turn() is not None:
            return SettleOutcome()
        prompt = self.waiting.pop(0)
        self._store.input_seq += 1
        sent = turn(
            id=f"person_{len(self._store.turns) + 1}",
            state="queued",
            input_seq_at_create=self._store.input_seq,
            turn_no=len(self._store.turns) + 1,
        )
        sent["task_prompt"] = prompt
        self._store.turns.append(sent)
        return SettleOutcome(job=sent, message_id=f"apmsg_{sent['id']}")


class FakeStore:
    """The repositories the consumer reaches, over one conversation."""

    def __init__(
        self,
        *,
        goal: dict[str, Any] | None = None,
        turns: list[dict[str, Any]] | None = None,
        input_seq: int = 0,
    ) -> None:
        self.clock = 1_000.0
        self.goal = dict(goal) if goal is not None else None
        self.turns = [dict(row) for row in (turns or [])]
        self.input_seq = input_seq
        #: How many automatic-round inserts fail before they are allowed through.
        self.fail_rounds = 0
        #: The race that used to attach a wait to a job that had already ended:
        #: the first read that finds a turn live settles it, so the read the
        #: parking write makes finds nothing to wait for.
        self.settle_live_turn_after_reads = 0
        self.goal_wakeups = FakeWakeups(self)
        self.goals = FakeGoals(self)
        self.conversation_commands = FakeConversationCommands(self)
        self.conversation_queries = FakeConversationQueries(self)
        self.source_control = FakeSourceControl()
        self.pending_messages = FakePendingMessages(self)
        #: Not a repository: the consumer is handed the Gateway client beside
        #: the store, and the cases pass this one.
        self.gateway = FakeGateway()

    def wakeup(self, wakeup_id: str) -> dict[str, Any]:
        return self.goal_wakeups._row(wakeup_id)

    def latest_turn(self) -> dict[str, Any] | None:
        return max(self.turns, key=lambda row: row["turn_no"], default=None)

    def latest_turn_id(self) -> str | None:
        newest = self.latest_turn()
        return None if newest is None else str(newest["id"])

    def live_turn(self) -> dict[str, Any] | None:
        return next(
            (row for row in self.turns if row["state"] in ("queued", "running")),
            None,
        )

    def settle_live_turn(self) -> None:
        """Let the turn the last live read found settle, if a race was asked for."""
        if not self.settle_live_turn_after_reads:
            return
        self.settle_live_turn_after_reads -= 1
        if self.settle_live_turn_after_reads:
            return
        live = next((row for row in self.turns if row["state"] in ("queued", "running")), None)
        if live is not None:
            live["state"] = "succeeded"

    def rounds(self) -> list[dict[str, Any]]:
        return [row for row in self.turns if row["turn_source"] == "goal"]

    def queued_rounds(self) -> list[dict[str, Any]]:
        return [row for row in self.rounds() if row["state"] == "queued"]

    def cancel_queued_rounds(self) -> None:
        """The goal stopped; the automatic rounds it asked for stop with it."""
        for row in self.turns:
            if row["state"] == "queued" and row["turn_source"] == "goal":
                row["state"] = "cancelled"


async def _consume(store: FakeStore) -> str:
    """Claim the next due wakeup and judge the goal, as the consumer loop does."""
    claimed = await store.goal_wakeups.claim_wakeups(limit=1)
    assert claimed, "no wakeup was due"
    return await consume_wakeup(store=store, gateway=store.gateway, wakeup=claimed[0])


async def test_an_expired_exhausted_worker_cannot_cancel_a_recovered_round():
    """A later successful lease holder wins over the old holder's failure cleanup."""
    from cloud_agent.goals.controller import _retry_or_pause

    store = FakeStore(goal=goal_row(), turns=[turn(id="job-1")])
    row = store.goal_wakeups.arm(trigger="owner", trigger_ref="set:1")
    row["attempts"] = MAX_WAKEUP_ATTEMPTS - 1
    old = (await store.goal_wakeups.claim_wakeups(limit=1))[0]
    store.clock += 121
    current = (await store.goal_wakeups.claim_wakeups(limit=1))[0]
    assert old["lease_owner"] != current["lease_owner"]
    assert (
        await consume_wakeup(store=store, gateway=store.gateway, wakeup=current)
        == "queue_work_round"
    )

    await _retry_or_pause(store, old)

    assert store.goal["state"] == "active"
    assert len(store.queued_rounds()) == 1
    assert store.wakeup(row["id"])["state"] == "processed"


async def test_an_expired_worker_cannot_release_or_finish_the_current_wakeup():
    """Both failure backoff and simple completion respect the lease owner."""
    from cloud_agent.goals.controller import _retry_or_pause

    store = FakeStore(goal=goal_row(), turns=[turn(id="job-1")])
    row = store.goal_wakeups.arm(trigger="owner", trigger_ref="set:1")
    old = (await store.goal_wakeups.claim_wakeups(limit=1))[0]
    store.clock += 121
    current = (await store.goal_wakeups.claim_wakeups(limit=1))[0]
    before = dict(row)
    await _retry_or_pause(store, old)
    # The simple "no goal" path must not mark the new holder's wakeup processed.
    store.goal = None
    assert await consume_wakeup(store=store, gateway=store.gateway, wakeup=old) == "none"
    assert row == before
    assert row["lease_owner"] == current["lease_owner"]


async def test_repository_outages_preserve_goal_scheduling_retries(monkeypatch):
    """An unavailable permission lookup waits without spending the retry budget."""
    from unittest.mock import AsyncMock

    from cloud_agent.entitlement import RepoAccessUnavailable

    store = FakeStore(goal=goal_row(), turns=[turn(id="job-1")])
    row = store.goal_wakeups.arm(trigger="owner", trigger_ref="set:1")
    lookup = AsyncMock(side_effect=RepoAccessUnavailable("temporarily unavailable"))
    monkeypatch.setattr("cloud_agent.goals.controller.require_entitled_repo", lookup)
    for _ in range(MAX_WAKEUP_ATTEMPTS + 2):
        assert await run_goal_wakeups(store=store, gateway=store.gateway) == 1
        assert row["state"] == "pending" and row["attempts"] == 0
        assert store.goal["state"] == "active"
        assert store.queued_rounds() == []
        assert await run_goal_wakeups(store=store, gateway=store.gateway) == 0
        store.clock += 31
    lookup.side_effect = None
    assert await run_goal_wakeups(store=store, gateway=store.gateway) == 1
    assert len(store.queued_rounds()) == 1


@pytest.fixture(autouse=True)
def _entitled_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repository every case here runs against is one this deployment allows."""
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", REPO)


async def test_an_owner_wakeup_on_an_idle_conversation_queues_the_first_work_round():
    store = FakeStore(goal=goal_row(), turns=[turn(id="job-1", input_seq_at_create=1)], input_seq=1)
    wakeup = store.goal_wakeups.arm(trigger="owner", trigger_ref="set:1")

    assert await _consume(store) == "queue_work_round"

    rounds = store.rounds()
    assert len(rounds) == 1
    queued = rounds[0]
    assert (queued["turn_source"], queued["goal_round_kind"], queued["goal_round"]) == (
        "goal",
        "work",
        1,
    )
    assert (queued["goal_id"], queued["round_goal_revision"]) == (GOAL_ID, 1)
    # No `of=`: this goal has no bound, and the round says only which one it is.
    assert '<goal_round kind="work" round="1">' in queued["task_prompt"]
    assert queued["client_request_id"] == client_request_id_for(
        thread_id=THREAD_ID, goal_id=GOAL_ID, kind="work", trigger_ref="set:1"
    )
    assert store.wakeup(wakeup["id"])["state"] == "processed"


async def test_a_finish_wakeup_with_an_absent_report_queues_the_next_work_round():
    store = FakeStore(
        goal=goal_row(rounds_used=1),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(
                id="job-2",
                turn_source="goal",
                goal_id=GOAL_ID,
                round_kind="work",
                report_parse="absent",
                input_seq_at_create=1,
                turn_no=2,
            ),
        ],
        input_seq=1,
    )
    store.goal_wakeups.arm(trigger="finish", trigger_ref="job:job-2")

    assert await _consume(store) == "queue_work_round"

    assert len(store.queued_rounds()) == 1
    queued = store.queued_rounds()[0]
    assert queued["goal_round"] == 2
    assert queued["parent_job_id"] == "job-2"
    assert queued["client_request_id"] == client_request_id_for(
        thread_id=THREAD_ID, goal_id=GOAL_ID, kind="work", trigger_ref="job:job-2"
    )


def _finished_round_store() -> tuple[FakeStore, dict[str, Any]]:
    """An unbounded goal whose last round finished, and the wakeup asking for more."""
    store = FakeStore(
        goal=goal_row(rounds_used=1),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(
                id="job-2",
                turn_source="goal",
                goal_id=GOAL_ID,
                round_kind="work",
                report_parse="absent",
                input_seq_at_create=1,
                turn_no=2,
            ),
        ],
        input_seq=1,
    )
    return store, store.goal_wakeups.arm(trigger="finish", trigger_ref="job:job-2")


async def test_a_withdrawn_agent_access_pauses_the_goal_instead_of_queueing(monkeypatch):
    """An automatic round has no request of the owner's to be refused at.

    Without asking here, an unbounded goal would keep queueing rounds for an
    owner the Gateway no longer admits. The pause says why, and the repository
    is not even looked up for an owner who may not use Agent at all.
    """
    from unittest.mock import AsyncMock

    lookup = AsyncMock()
    monkeypatch.setattr("cloud_agent.goals.controller.require_entitled_repo", lookup)
    store, _ = _finished_round_store()
    store.gateway.allowed = False

    assert await _consume(store) == "pause"

    assert store.gateway.asked == ["user-1"]
    assert (store.goal["state"], store.goal["pause_reason"]) == ("paused", "access_denied")
    assert "Agent access has not been granted" in store.goal["pause_detail"]
    assert store.queued_rounds() == []
    lookup.assert_not_awaited()


async def test_an_unconfirmed_agent_access_retries_without_spending_the_goal():
    """No answer is not permission, and not a reason to end the owner's goal."""
    store, row = _finished_round_store()
    store.gateway.unavailable = True

    for _ in range(MAX_WAKEUP_ATTEMPTS + 2):
        assert await run_goal_wakeups(store=store, gateway=store.gateway) == 1
        assert row["state"] == "pending" and row["attempts"] == 0
        assert store.goal["state"] == "active"
        assert store.queued_rounds() == []
        store.clock += 31
    store.gateway.unavailable = False
    assert await run_goal_wakeups(store=store, gateway=store.gateway) == 1
    assert len(store.queued_rounds()) == 1


async def test_a_consumer_with_no_gateway_queues_nothing():
    """A missing client cannot answer, so it cannot authorize a round either."""
    store, _ = _finished_round_store()

    assert await run_goal_wakeups(store=store, gateway=None) == 1

    assert store.goal["state"] == "active"
    assert store.queued_rounds() == []


async def test_an_unbounded_goal_queues_round_fifty_one():
    """The ten- and fifty-round defaults are gone from the scheduling path too.

    A goal the owner never bounded has to keep producing rounds however many it
    has run. This is the same regression the decision test pins, asserted one
    layer up so that a reader that clamped or defaulted the column would fail
    here rather than only there.
    """
    store = FakeStore(
        goal=goal_row(rounds_used=50),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(
                id="job-50",
                turn_source="goal",
                goal_id=GOAL_ID,
                round_kind="work",
                report_parse="absent",
                input_seq_at_create=1,
                turn_no=50,
            ),
        ],
        input_seq=1,
    )
    store.goal_wakeups.arm(trigger="finish", trigger_ref="job:job-50")

    assert await _consume(store) == "queue_work_round"

    queued = store.queued_rounds()[0]
    assert queued["goal_round"] == 51
    assert '<goal_round kind="work" round="51">' in queued["task_prompt"]
    assert store.goal["state"] == "active"


async def test_a_bound_the_owner_stated_still_stops_the_rounds():
    """Removing the platform's default is not removing the owner's choice."""
    store = FakeStore(
        goal=goal_row(rounds_used=5, max_rounds=5),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(
                id="job-5",
                turn_source="goal",
                goal_id=GOAL_ID,
                round_kind="work",
                report_parse="absent",
                input_seq_at_create=1,
                turn_no=5,
            ),
        ],
        input_seq=1,
    )
    store.goal_wakeups.arm(trigger="finish", trigger_ref="job:job-5")

    assert await _consume(store) == "pause"

    assert store.queued_rounds() == []
    assert (store.goal["state"], store.goal["pause_reason"]) == ("paused", "round_limit")


async def test_a_complete_report_completes_the_goal_on_the_turn_that_earned_it():
    store = FakeStore(
        goal=goal_row(rounds_used=1),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(
                id="job-2",
                turn_source="goal",
                goal_id=GOAL_ID,
                round_kind="work",
                report_parse="valid",
                report_outcome="complete",
                report_reason="pytest: 42 passed",
                input_seq_at_create=1,
                turn_no=2,
            ),
        ],
        input_seq=1,
    )
    wakeup = store.goal_wakeups.arm(trigger="finish", trigger_ref="job:job-2")

    assert await _consume(store) == "complete"

    assert store.goal["state"] == "complete"
    assert store.goal["completion_basis"] == "model_reported"
    assert store.goal["completed_by_job_id"] == "job-2"
    # Nothing follows a completed goal, and the obligation says what happened.
    assert store.queued_rounds() == []
    assert store.wakeup(wakeup["id"])["detail"] == "complete"


async def test_a_needs_input_report_pauses_and_names_what_is_missing():
    store = FakeStore(
        goal=goal_row(rounds_used=1),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(
                id="job-2",
                turn_source="goal",
                goal_id=GOAL_ID,
                round_kind="work",
                report_parse="valid",
                report_outcome="needs_input",
                report_reason="which region should I deploy to?",
                input_seq_at_create=1,
                turn_no=2,
            ),
        ],
        input_seq=1,
    )
    store.goal_wakeups.arm(trigger="finish", trigger_ref="job:job-2")

    assert await _consume(store) == "pause"

    assert (store.goal["state"], store.goal["pause_reason"]) == ("paused", "needs_input")
    assert store.goal["pause_detail"] == "which region should I deploy to?"
    assert store.queued_rounds() == []


async def test_a_wakeup_for_a_replaced_goal_is_dropped_without_touching_it():
    store = FakeStore(
        goal=goal_row(goal_id="goal-2"),
        turns=[turn(id="job-1", input_seq_at_create=1)],
        input_seq=1,
    )
    wakeup = store.goal_wakeups.arm(trigger="finish", trigger_ref="job:job-1", goal_id=GOAL_ID)

    assert await _consume(store) == "none"

    assert (store.goal["goal_id"], store.goal["state"]) == ("goal-2", "active")
    assert store.rounds() == []
    assert store.wakeup(wakeup["id"])["detail"] == "superseded goal"


async def test_a_wakeup_for_a_goal_that_has_moved_on_is_dropped():
    store = FakeStore(
        goal=goal_row(revision=2),
        turns=[turn(id="job-1", input_seq_at_create=1)],
        input_seq=1,
    )
    wakeup = store.goal_wakeups.arm(trigger="owner", trigger_ref="set:1", revision=1)

    assert await _consume(store) == "none"

    assert store.goal["revision"] == 2
    assert store.rounds() == []
    assert store.wakeup(wakeup["id"])["detail"] == "superseded revision"


async def test_a_wakeup_for_a_goal_that_is_not_active_is_dropped():
    store = FakeStore(
        goal=goal_row(state="paused", pause_reason="user", pause_detail="paused by the user"),
        turns=[turn(id="job-1", input_seq_at_create=1)],
        input_seq=1,
    )
    wakeup = store.goal_wakeups.arm(trigger="owner", trigger_ref="resume:2")

    assert await _consume(store) == "none"

    assert (store.goal["state"], store.goal["pause_reason"]) == ("paused", "user")
    assert store.rounds() == []
    assert store.wakeup(wakeup["id"])["detail"] == "goal paused"


async def test_a_live_turn_parks_the_wakeup_instead_of_queueing_behind_it():
    store = FakeStore(
        goal=goal_row(),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(id="job-2", state="queued", input_seq_at_create=2, turn_no=2),
        ],
        input_seq=2,
    )
    wakeup = store.goal_wakeups.arm(trigger="owner", trigger_ref="set:1")

    assert await _consume(store) == "wait"

    parked = store.wakeup(wakeup["id"])
    assert (parked["state"], parked["waiting_job_id"]) == ("pending", "job-2")
    assert store.rounds() == []


async def test_a_waiting_message_goes_before_the_round_the_goal_would_add():
    """The owner's queued message is the next turn; the goal waits behind it.

    A round just finished and the goal would continue, but the owner wrote
    something while it ran. That message is sent first — the decision parks
    behind the turn it became — and the goal is judged again once it ends.
    """
    store = FakeStore(
        goal=goal_row(rounds_used=1),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(
                id="round-1",
                turn_source="goal",
                goal_id=GOAL_ID,
                round_kind="work",
                report_parse="absent",
                input_seq_at_create=1,
                turn_no=2,
            ),
        ],
        input_seq=1,
    )
    store.pending_messages.waiting.append("also update the changelog")
    wakeup = store.goal_wakeups.arm(trigger="finish", trigger_ref="job:round-1")

    assert await _consume(store) == "wait"

    sent = store.latest_turn()
    assert sent is not None
    assert (sent["turn_source"], sent["task_prompt"]) == ("human", "also update the changelog")
    parked = store.wakeup(wakeup["id"])
    assert (parked["state"], parked["waiting_job_id"]) == ("pending", sent["id"])
    assert [row["id"] for row in store.rounds()] == ["round-1"]

    # The owner's turn ends; the goal is judged again and continues after it.
    sent["state"] = "succeeded"
    parked["waiting_job_id"] = None
    assert await _consume(store) == "queue_work_round"
    assert store.latest_turn()["turn_source"] == "goal"


async def test_a_replayed_wakeup_creates_no_second_round():
    """The event identity is the key: the same decision finds the round it made."""
    store = FakeStore(goal=goal_row(), turns=[turn(id="job-1", input_seq_at_create=1)], input_seq=1)
    store.goal_wakeups.arm(trigger="owner", trigger_ref="set:1")
    assert await _consume(store) == "queue_work_round"
    assert len(store.rounds()) == 1

    # The round it created has since settled, and the same owner write is
    # replayed (a client retry, or a consumer that crashed after committing).
    store.rounds()[0].update(state="succeeded", report_parse="absent")
    store.goal["rounds_used"] = 1
    store.goal_wakeups.arm(trigger="owner", trigger_ref="set:1")

    assert await _consume(store) == "none"
    assert len(store.rounds()) == 1


async def test_a_failing_consumer_backs_off_and_then_pauses_with_scheduling_failed():
    store = FakeStore(goal=goal_row(), turns=[turn(id="job-1", input_seq_at_create=1)], input_seq=1)
    wakeup = store.goal_wakeups.arm(trigger="owner", trigger_ref="set:1")
    store.fail_rounds = MAX_WAKEUP_ATTEMPTS

    for attempt in range(1, MAX_WAKEUP_ATTEMPTS + 1):
        store.clock += 3_600.0
        assert await run_goal_wakeups(store=store, gateway=store.gateway) == 1
        row = store.wakeup(wakeup["id"])
        if attempt < MAX_WAKEUP_ATTEMPTS:
            assert row["state"] == "pending"
            assert row["attempts"] == attempt
            assert row["not_before"] == store.clock + wakeup_backoff(attempt)
            assert row["lease_expires_at"] is None
            assert store.goal["state"] == "active"
            # The backoff is real: an immediate second pass finds nothing due.
            assert await store.goal_wakeups.claim_wakeups(limit=1) == []
        else:
            assert row["state"] == "processed"

    assert (store.goal["state"], store.goal["pause_reason"]) == ("paused", "scheduling_failed")
    assert store.rounds() == []


async def _reaper_pass(store: FakeStore) -> int:
    """The reaper's consumer pass, over every conversation."""
    return await run_goal_wakeups(store=store, gateway=store.gateway)


async def _finish_drain(store: FakeStore) -> int:
    """A finished turn's drain, over its own conversation."""
    return await continue_after_finish(
        store=store, job_id="job-1", thread_id=THREAD_ID, gateway=store.gateway
    )


@pytest.mark.parametrize("drain", [_reaper_pass, _finish_drain], ids=["reaper", "finish"])
@pytest.mark.parametrize(
    ("current_goal", "trigger_ref", "detail"),
    [
        (
            goal_row(goal_id="goal-2", revision=2, objective="ship something else"),
            "set:2",
            "superseded goal",
        ),
        (goal_row(revision=3), "resume:3", "superseded revision"),
    ],
    ids=["objective-replaced", "paused-and-resumed"],
)
async def test_an_exhausted_scheduling_failure_preserves_a_new_goal_authorization(
    monkeypatch: pytest.MonkeyPatch,
    current_goal: dict[str, Any],
    trigger_ref: str,
    detail: str,
    drain: Any,
):
    """A failure belongs to the goal revision that was being scheduled.

    The owner's replacement or pause/resume can commit while a scheduling
    attempt fails. Its new round and wakeup must survive the old attempt's
    exhausted retry handler, whether the reaper's pass or the conversation's
    own finish drain was consuming it.
    """
    store = FakeStore(goal=goal_row(), turns=[turn(id="job-1", input_seq_at_create=1)], input_seq=1)
    failed_wakeup = store.goal_wakeups.arm(trigger="owner", trigger_ref="set:1")
    failed_wakeup["attempts"] = MAX_WAKEUP_ATTEMPTS - 1

    async def fail_after_owner_write(**kwargs: Any) -> None:
        assert (kwargs["goal_id"], kwargs["revision"]) == (GOAL_ID, 1)
        store.goal = dict(current_goal)
        current_round = turn(
            id="current-round",
            state="queued",
            turn_source="goal",
            goal_id=current_goal["goal_id"],
            round_kind="work",
            input_seq_at_create=1,
            turn_no=2,
        )
        current_round["round_goal_revision"] = current_goal["revision"]
        store.turns.append(current_round)
        store.goal_wakeups.arm(
            trigger="owner",
            trigger_ref=trigger_ref,
            goal_id=current_goal["goal_id"],
            revision=current_goal["revision"],
        )
        raise RuntimeError("the old scheduling attempt failed")

    monkeypatch.setattr(store.conversation_commands, "create_goal_round", fail_after_owner_write)

    assert await drain(store) == 1

    assert store.goal == current_goal
    assert [row["id"] for row in store.queued_rounds()] == ["current-round"]
    failed = store.wakeup(failed_wakeup["id"])
    assert (failed["state"], failed["detail"], failed["lease_expires_at"]) == (
        "processed",
        detail,
        None,
    )
    current_wakeup = store.goal_wakeups.rows[-1]
    assert (current_wakeup["state"], current_wakeup["attempts"], current_wakeup["detail"]) == (
        "pending",
        0,
        None,
    )


async def test_an_unloaded_github_app_leaves_the_wakeup_unclaimed_until_it_loads():
    """Issue #47: a deployment fault must not spend the goal's attempts.

    Claiming charges the wakeup an attempt, and without credentials the
    entitlement check sees only the allowlist and pauses the goal as
    ``access_denied``. So a GitHub App this process cannot load stops the pass
    before the claim — for more passes than a real failure is allowed — and the
    same wakeup is consumed once the App loads.
    """
    from cloud_agent.source_control_runtime import SourceControlUnavailable

    store = FakeStore(goal=goal_row(), turns=[turn(id="job-1", input_seq_at_create=1)], input_seq=1)
    wakeup = store.goal_wakeups.arm(trigger="owner", trigger_ref="set:1")

    class RecoveringApp:
        loaded = False

        def get(self) -> object:
            if not self.loaded:
                raise SourceControlUnavailable("github")
            return object()  # the repository is allowlisted; the minter is not asked

    app = RecoveringApp()
    for _ in range(MAX_WAKEUP_ATTEMPTS + 3):
        store.clock += 3_600.0
        assert await run_goal_wakeups(store=store, gateway=store.gateway, github_app=app) == 0

    row = store.wakeup(wakeup["id"])
    assert (row["state"], row["attempts"], row["lease_expires_at"]) == ("pending", 0, None)
    assert store.goal["state"] == "active"
    assert store.rounds() == []

    app.loaded = True
    assert await run_goal_wakeups(store=store, gateway=store.gateway, github_app=app) == 1
    assert store.wakeup(wakeup["id"])["state"] == "processed"
    assert len(store.rounds()) == 1


async def test_a_finish_drains_its_own_conversation_and_no_one_elses():
    """The finish request decides this conversation's goal and leaves the rest queued.

    The claim used to take the deployment's oldest due wakeups, so a finish
    decided up to eight other conversations' goals, each with its own Gateway
    and GitHub calls, inside its runner's request; with that many waiting,
    this turn's own wakeup was not even among them. Theirs are the reaper's.
    """
    store, own = _finished_round_store()
    others = [
        store.goal_wakeups.arm(
            trigger="owner",
            trigger_ref="set:1",
            goal_id="goal-other",
            thread_id=f"athr_other_{index}",
        )
        for index in range(8)
    ]
    # Every one of them has been due for longer than this turn's own.
    store.goal_wakeups.rows = [*others, own]

    assert (
        await continue_after_finish(
            store=store, job_id="job-2", thread_id=THREAD_ID, gateway=store.gateway
        )
        == 1
    )

    assert store.wakeup(own["id"])["state"] == "processed"
    assert len(store.queued_rounds()) == 1
    for other in others:
        assert (other["state"], other["attempts"], other["lease_owner"]) == ("pending", 0, None)
    # The reaper's pass names no conversation, and decides theirs.
    assert await run_goal_wakeups(store=store, gateway=store.gateway) == 8
    assert {(other["state"], other["detail"]) for other in others} == {
        ("processed", "superseded goal")
    }


async def test_a_finish_without_a_conversation_drains_nothing():
    """No conversation, no goal: nothing to claim, and no attempt spent."""
    store, row = _finished_round_store()

    assert (
        await continue_after_finish(store=store, job_id="job-2", thread_id=None, gateway=None) == 0
    )

    assert (row["state"], row["attempts"], row["lease_owner"]) == ("pending", 0, None)


async def test_an_owner_wakeup_does_not_re_judge_the_turn_that_paused_the_goal():
    """The Resume button has to start work, not read the round that stopped it.

    A pause cancels the queued automatic round, so the newest turn of a paused
    goal is a *cancelled* one. Judging that on resume paused the goal straight
    back, which made the only Resume control in the product a no-op.
    """
    store = FakeStore(
        goal=goal_row(),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(
                id="job-2",
                state="cancelled",
                turn_source="goal",
                goal_id=GOAL_ID,
                round_kind="work",
                turn_no=2,
            ),
        ],
        input_seq=1,
    )
    store.goal_wakeups.arm(trigger="owner", trigger_ref="resume:2")

    assert await _consume(store) == "queue_work_round"
    assert store.goal["state"] == "active"
    queued = store.queued_rounds()
    assert len(queued) == 1
    assert queued[0]["goal_round"] == 1


async def test_a_wakeup_is_not_parked_behind_a_turn_that_already_finished():
    """The registration can lose its race, and the loss must not be permanent.

    Parking is a second write: the consumer reads "still running", the turn
    finishes, its finish finds nothing parked to release, and the wait would be
    attached to a job that has already ended. The consumer re-reads before it
    registers the wait, so the wakeup is left due instead — and the sweep
    repairs the same shape if it ever gets through.
    """
    store = FakeStore(
        goal=goal_row(),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(id="job-2", state="running", input_seq_at_create=2, turn_no=2),
        ],
        input_seq=2,
    )
    wakeup = store.goal_wakeups.arm(trigger="owner", trigger_ref="resume:2")
    # The turn is live for the read that chooses it, and finished by the read
    # the parking write makes.
    store.settle_live_turn_after_reads = 1

    assert await _consume(store) in ("wait", "queue_work_round")

    held = store.wakeup(wakeup["id"])
    assert held["waiting_job_id"] is None, "a wakeup is never parked behind a finished turn"


async def test_a_completion_is_refused_when_a_person_spoke_while_it_was_decided():
    """A message does not move the goal's revision, so it is checked separately.

    The decision is made outside the transaction that applies it. An owner's
    message accepted in between leaves the goal exactly where it was — same
    identity, same revision, still active — and only the conversation's own
    input counter shows that the round being completed is no longer current.
    """
    store = FakeStore(
        goal=goal_row(rounds_used=1),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(
                id="job-2",
                turn_source="goal",
                goal_id=GOAL_ID,
                round_kind="work",
                report_parse="valid",
                report_outcome="complete",
                report_reason="done",
                input_seq_at_create=1,
                turn_no=2,
            ),
        ],
        input_seq=2,
    )
    store.goal_wakeups.arm(trigger="finish", trigger_ref="job:job-2")

    assert await _consume(store) == "none"
    assert store.goal["state"] == "active"
    assert store.goal["completion_basis"] is None


async def test_a_completion_overtaken_by_a_message_waits_behind_its_turn(monkeypatch):
    """The overtaken completion's wakeup is the goal's only obligation; it is kept.

    A message accepted while the decision runs arms no wakeup of its own,
    because this one is still pending. Marking this one processed left an
    active goal that nothing looked at again after the message's turn.
    """
    store = FakeStore(
        goal=goal_row(rounds_used=1),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(
                id="job-2",
                turn_source="goal",
                goal_id=GOAL_ID,
                round_kind="work",
                report_parse="valid",
                report_outcome="complete",
                report_reason="done",
                input_seq_at_create=1,
                turn_no=2,
            ),
        ],
        input_seq=1,
    )
    wakeup = store.goal_wakeups.arm(trigger="finish", trigger_ref="job:job-2")
    messages: list[dict[str, Any]] = []

    async def a_message_lands_once(*args: Any, **kwargs: Any) -> None:
        # Between the decision's read and the transaction that applies it.
        if not messages:
            store.input_seq += 1
            messages.append(turn(id="job-3", state="queued", input_seq_at_create=2, turn_no=3))
            store.turns.append(messages[0])

    monkeypatch.setattr("cloud_agent.goals.controller.require_entitled_repo", a_message_lands_once)

    assert await _consume(store) == "wait"

    assert (store.goal["state"], store.goal["completion_basis"]) == ("active", None)
    parked = store.wakeup(wakeup["id"])
    assert (parked["state"], parked["waiting_job_id"]) == ("pending", "job-3")
    assert parked["attempts"] == 0

    # The person's turn ends, its finish releases the wait, and the goal goes on.
    messages[0]["state"] = "succeeded"
    parked["waiting_job_id"] = None
    assert await _consume(store) == "queue_work_round"
    assert len(store.queued_rounds()) == 1


async def test_an_owner_wakeup_on_an_archived_conversation_pauses_and_says_so():
    """Resume is authorized by the owner, not by "the conversation is fine".

    Ignoring the last turn's *result* must not become ignoring the conversation:
    an archived task, a lost session and a repository the owner can no longer
    reach all stop a resume too, and leaving the goal silently active would show
    a green badge over work nothing will ever run.
    """
    store = FakeStore(
        goal=goal_row(state="paused", pause_reason="user", pause_detail="paused by the user"),
        turns=[turn(id="job-1", input_seq_at_create=1, archived_at="2026-09-20T00:00:00Z")],
        input_seq=1,
    )
    store.goal["state"] = "active"
    store.goal_wakeups.arm(trigger="owner", trigger_ref="resume:2")

    assert await _consume(store) == "pause"
    assert (store.goal["state"], store.goal["pause_reason"]) == ("paused", "archived")
    assert store.rounds() == []


async def test_an_owner_wakeup_ignores_the_result_but_keeps_the_runtime():
    """The turn's outcome is neutralised; the conversation's runtime is not.

    A goal round can only run where its report can be read, and that is a fact
    about the runtime rather than about how the last turn ended — so a resume on
    a conversation whose runtime cannot deliver a report still pauses instead of
    queueing a round nobody can hear from.
    """
    store = FakeStore(
        goal=goal_row(),
        turns=[
            turn(id="job-1", state="cancelled", input_seq_at_create=1, runtime="not-a-runtime"),
        ],
        input_seq=1,
    )
    store.goal_wakeups.arm(trigger="owner", trigger_ref="resume:2")

    assert await _consume(store) == "pause"
    assert store.goal["pause_reason"] == "runtime_unsupported"


async def test_a_truncated_final_message_is_unreadable_rather_than_absent():
    """A clipped message is not a message that chose to say nothing.

    The report block is at the *end* of a final message and the runtime's text
    bound is what removes it, so reading a clipped message as "absent" would
    keep a goal working forever on a turn that may have reported completion.
    The runtime records that it clipped, and the reader has to consume it.
    """
    store = FakeStore(
        goal=goal_row(rounds_used=1),
        turns=[
            turn(id="job-1", input_seq_at_create=1),
            turn(
                id="job-2",
                turn_source="goal",
                goal_id=GOAL_ID,
                round_kind="work",
                report_parse="unavailable",
                report_detail="the runtime truncated this turn's final message",
                input_seq_at_create=1,
                turn_no=2,
            ),
        ],
        input_seq=1,
    )
    store.goal_wakeups.arm(trigger="finish", trigger_ref="job:job-2")

    assert await _consume(store) == "pause"
    assert (store.goal["state"], store.goal["pause_reason"]) == ("paused", "report_unavailable")


async def test_an_owner_wakeup_after_a_failed_round_schedules_instead_of_pausing():
    """A failed round is a result, and a resume is not a verdict on it.

    The failed-turn rule turns a turn that did not finish saying what it meant
    into an unreadable report, so the platform stops rather than looping. That
    is right for a decision made *from* the turn, and wrong for an owner trigger,
    which is authorized by the owner instead — otherwise resuming a goal paused
    by a failure pauses it straight back, one failure mode over from the
    cancelled turn this rule was already excluded for.
    """
    failed = turn(
        id="failed-round",
        state="failed",
        turn_source="goal",
        goal_id=GOAL_ID,
        round_kind="work",
        report_parse="absent",
        input_seq_at_create=1,
    )
    failed["thread_sandbox"] = {
        "context_status": "new",
        "native_state": "new",
        "state": "starting",
    }
    store = FakeStore(goal=goal_row(), turns=[failed], input_seq=1)
    store.goal_wakeups.arm(trigger="owner", trigger_ref="resume:1")

    assert await _consume(store) == "queue_work_round"
    assert store.goal["state"] == "active"
    assert len(store.queued_rounds()) == 1
