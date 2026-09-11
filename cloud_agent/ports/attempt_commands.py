"""Port: the attempt lifecycle -- claim, heartbeat, transition, retire.

Where the lease and its fencing generation live. Every write here is fenced
or idempotent, because a superseded attempt must not publish anything.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol


class AttemptCommands(Protocol):
    """Port: the attempt lifecycle -- claim, heartbeat, transition, retire."""

    async def claim_job(
        self,
        *,
        worker_id: str,
        lease_ttl_seconds: float,
        host: str,
        host_credential_hash: str,
        claim_attestation: dict[str, Any] | None = None,
        expected_deployment_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Claim the oldest queued job, creating a new fenced attempt.

        Returns the job dict extended with ``attempt_id``, ``attempt_no`` and
        ``lease_generation``, or ``None`` when the queue is empty. Concurrent
        workers are safe via ``FOR UPDATE SKIP LOCKED``.

        A queued follow-up becomes eligible only after every earlier turn in
        its thread is terminal. Checking all unfinished earlier turns also
        prevents a cancelled intermediate turn from letting its descendants
        overtake an active ancestor. The indexed eligibility check precedes
        the limit, so a blocked thread cannot hide runnable work behind it.

        The caller is always an enrolled host whose exact credential hash is
        re-checked while its capacity slot is taken. An operator pin chooses
        the host for chats that do not have a sandbox yet. A retained chat is
        instead hard-pinned to the host that owns its Docker writable layer;
        without portable storage, sending that follow-up to the newly selected
        host would be data loss disguised as failover.

        The policy read is the first statement of the *claiming* transaction and
        takes the row shared, so it cannot be overtaken by a switch committing
        between the check and the dequeue. The same query uses that decision to
        exclude new chats from the old host while still admitting only retained
        groups whose immutable ``host`` is that old host.

        **The pool gate (G4) sits after the pin and answers the same way.**
        A reported host must be ``online`` and have a free slot, and the job it
        takes must require nothing the host did not declare. All three refusals
        return ``None`` — the same "nothing for you" a drained or unpinned
        runner already gets — because a runner has no useful different
        behaviour for "the queue is empty" and "the queue has work you cannot
        run". Capacity and capability are operator-visible on the admin
        surface, which is where the question is answerable.

        Every profile requires a shared-live capability attestation from the
        exact claiming replica. New groups require the current protocol; the
        explicitly compatible v1 protocol may claim only an existing group on
        its owning host. ``untrusted-kata-v1`` additionally requires the Kata
        runtime plus unequal host/guest kernels. The mutable heartbeat row is
        not evidence for a particular replica during a rolling deploy.
        """
        ...

    async def release_claim(
        self, *, job_id: str, attempt_id: int, lease_generation: int | None = None
    ) -> bool:
        """Hand a just-claimed job back to the queue without spending a retry.

        For the case where the *platform* could not go through with a claim it
        already made — it could not mint the repository credential, say. The
        job never got its turn, so making it pay for the attempt is wrong twice
        over: it waits out a lease it will never use, and after
        ``max_attempts`` such failures the reaper fails it outright. A brief
        GitHub outage would terminally fail every queued private-repo job
        without an agent ever starting.

        The attempt row stays, marked ``aborted``, with a control event saying
        why: history is append-only, and "the platform dropped this one" is
        exactly the kind of thing an operator later needs to see. It simply
        does not count — see the retry budget in :meth:`reap_expired`.
        """
        ...

    async def heartbeat(
        self, *, attempt_id: int, lease_generation: int, lease_ttl_seconds: float
    ) -> dict[str, Any]:
        """Extend a live lease and report cancellation, fenced by generation.

        Returns ``{"ok": False}`` when the attempt is no longer the owner
        (superseded, finished, or generation mismatch) — the worker must stop
        immediately. On success also returns ``cancel_requested`` so workers
        learn about cancellation without extra polling.
        """
        ...

    async def transition(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        from_states: tuple[str, ...],
        to_state: str,
        detail: str | None = None,
        base_sha: str | None = None,
    ) -> bool:
        """Move a job between states, fenced by the attempt's live lease.

        Terminal transitions also close the attempt (``finished``). Returns
        False when the caller lost the lease or the job is not in
        ``from_states`` — the caller must stop.

        ``base_sha`` fills in the commit the worker resolved, and only when the
        job does not already have one: an owner who pinned a commit must get a
        patch against *that* commit, so a worker may report the base it used
        but never overwrite the base it was given.

        Raises ``ValueError`` for a ``succeeded`` transition on a turn that
        never settled its native context: the schema refuses the row, and the
        caller — not this method — is what has to change.
        """
        ...

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        """Request cancellation; returns the job's resulting state.

        Queued jobs cancel immediately. Running jobs get the flag
        set — the worker observes it on its next heartbeat and performs the
        fenced transition. Terminal jobs are a no-op. ``user_id`` scopes the
        cancel to the owner. Returns ``None`` for an unknown job.
        """
        ...

    async def reap_expired(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Close expired attempts and requeue (or fail) their jobs.

        For each expired ``running`` attempt: mark it ``superseded``, append an
        ``attempt_superseded`` control event, then decide the job's fate —
        requeue while attempts remain, ``cancelled`` if cancellation was
        pending, ``failed`` once ``max_attempts`` is exhausted. Publication is
        a snapshot's lifecycle, not the job's, so no job is ever caught
        mid-publish here. Returns a summary per reaped attempt.
        """
        ...

    async def append_event(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
        event_seq: int | None = None,
    ) -> int | None:
        """Append one event, fenced by the live lease.

        Returns the global event id (the SSE ``Last-Event-ID`` cursor), or
        ``None`` when the caller lost the lease. The row lock on the attempt
        serializes ``seq`` assignment for the single legitimate writer.
        """
        ...

    async def record_continuity_mode(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        mode: str | None,
        context_status: str | None = None,
    ) -> bool:
        """Stamp the diagnostic rung and optional product context semantic."""
        ...

    async def record_attempt_resources(
        self, *, attempt_id: int, lease_generation: int, rollup: dict[str, Any]
    ) -> bool:
        """Store one attempt's resource rollup, lease-fenced and idempotent.

        Fenced like every other worker write: a superseded attempt must not be
        able to overwrite what the live one consumed. Idempotent because the
        runner may retry the post — a rollup describes a closed window, so a
        repeat is the same window said twice, never a second window to add.

        Absent counters stay NULL. Current shared-member writers intentionally
        cannot assign whole-container totals to one member; historical sampler
        failures likewise must not be reported as a machine that used nothing.

        The per-call rows ride the same fence and the same transaction: they
        describe spans inside this window, so a caller that could not write
        the window must not be able to write its parts. They are replaced
        rather than merged, because the list is one statement about the whole
        attempt — a retry reporting fewer calls than a previous post means
        fewer calls, not a set to union with.
        """
        ...

    async def sweep_queued_follow_ups(self) -> None:
        """Fail queued follow-ups whose earlier native context is unavailable.

        Claiming already excludes these rows. This periodic safety net gives
        legacy pending turns a terminal answer using the partial queued
        indexes, without a cleanup scan on every claim poll.
        """
        ...
