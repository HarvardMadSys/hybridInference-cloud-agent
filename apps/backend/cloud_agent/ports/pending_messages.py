"""Port: the messages an owner has written but the platform has not sent yet.

A thread's queue of pending messages, and the one transaction that turns the
first of them into the next turn. Every write is scoped to the owner, and every
change to a message is conditional on the revision the caller read, so two tabs
produce one winner and a loser that can see what won.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from cloud_agent.domain.pending_messages import PreparedHead, SettleOutcome


class PendingMessageRepository(Protocol):
    """Port: a thread's pending messages, and settling its queue."""

    async def submit_message(
        self,
        *,
        thread_id: str,
        user_id: str,
        prompt: str,
        skill_ids: list[int] | None,
        attachment_ids: list[int] | None,
        model: str | None,
        reasoning_effort: str | None,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
        require_idle: bool = False,
        expected_latest_job_id: str | None = None,
        reset_reasoning_effort: bool = False,
        skills: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Put one message at the end of the owner's thread's queue.

        Answers ``{"message": row}``; ``{"job_id": id}`` for an idle-only turn
        or when this key already made a turn; or ``{"error": ...}`` —
        ``not_found``, ``session_archived``,
        ``client_request_conflict``, ``attachments_unavailable`` or
        ``conversation_changed``.

        An idle-only action locks its membership, then its sandbox group after
        the thread lock. It requires the expected newest turn, no live turn,
        waiting message, active member or Fork in the shared group, and its
        latest snapshot to be published. Using the resolved skills, attachments
        and effort choices, it creates the turn in this same transaction, never
        a pending message. It replays its original receipt before these checks,
        but cannot newly queue behind or resume waiting work.

        When nothing in the thread is running this is also the owner deciding
        to continue: a pause is cleared and the newest turn's ending counts as
        seen, so older waiting messages go first and this one follows them.
        Normal submissions also lock their membership then group before writing
        the queue, if present, so they cannot race an idle-only group's check.
        """
        ...

    async def replayed_message(
        self, *, user_id: str, client_request_id: str, fingerprint: str | None
    ) -> dict[str, Any] | None:
        """The pending message an owner's key already made, a conflict, or ``None``."""
        ...

    async def queue_state(self, *, thread_id: str, user_id: str) -> dict[str, Any] | None:
        """The owner's thread's pause reason and messages, or ``None`` if not theirs."""
        ...

    async def edit_message(
        self,
        *,
        thread_id: str,
        user_id: str,
        message_id: str,
        revision: int,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        """Change what a message says or whether it is held, at the revision read.

        ``changes`` holds only what the caller set: ``prompt``, ``skill_ids``,
        ``attachment_ids``, ``model``, ``reasoning_effort`` and ``state``
        (``held`` or ``queued``). Answers ``{"message": row}`` or ``{"error":
        ...}`` — ``not_found``, ``revision_conflict`` or ``not_editable`` with
        the ``current`` row, or ``attachments_unavailable``.
        """
        ...

    async def delete_message(
        self, *, thread_id: str, user_id: str, message_id: str, revision: int
    ) -> dict[str, Any]:
        """Remove a message at the revision read; its uploads return to staged.

        The uploads are released by the foreign key, in this statement: they go
        back to being ordinary staged uploads, as if never attached.
        """
        ...

    async def resume_queue(self, *, thread_id: str, user_id: str) -> dict[str, Any]:
        """Clear the owner's pause so the queue may move again.

        Answers ``{"queue_paused_reason": None}``, or ``{"error": "not_found"}``
        / ``{"error": "session_archived"}``.
        """
        ...

    async def begin_send_now(
        self, *, thread_id: str, user_id: str, message_id: str, revision: int
    ) -> dict[str, Any]:
        """Choose one waiting message to go into the live turn.

        Under the thread's lock the message must be ``queued`` at the revision
        the owner read and carry no uploads — a turn's uploads arrive with its
        prompt, not in the middle. Then, by what the conversation is doing:

        - nothing is live: this is an ordinary send, so a paused queue
          continues and ``{"idle": True}`` asks the caller to settle it;
        - its newest turn is being stopped: ``{"error": "not_running"}``;
        - a turn is live, running or still waiting for a host: the message
          becomes ``sending``, tied to that turn, and ``{"message", "job_id",
          "attempt_id"}`` names what to hand it to. ``attempt_id`` is ``None``
          until a host has the turn.

        Other refusals read like an edit's — ``not_found``, and
        ``revision_conflict`` or ``not_editable`` with ``current`` — plus
        ``has_attachments`` and ``session_archived``.
        """
        ...

    async def return_to_queue(self, *, message_id: str, revision: int) -> dict[str, Any] | None:
        """Put a message its turn will not take back in line, as it was.

        ``revision`` is the one it took when it was chosen to send now; a
        message the queue has moved on from since — taken by the turn, or
        already back in line — is left alone. Returns the message as it now
        is, or ``None`` when it is gone.
        """
        ...

    async def settle_queue(
        self, *, thread_id: str, prepared: PreparedHead | None = None
    ) -> SettleOutcome:
        """Act on the newest turn's ending, and send the first message if it may go.

        One transaction under the thread's lock. A turn that is still running
        means nothing to do. A newest turn that ended ``failed`` or
        ``cancelled`` and has not been acted on pauses a non-empty queue. A
        paused, archived, empty or held-at-the-front queue sends nothing.
        Otherwise the first message becomes the next turn — but only with a
        ``prepared`` check made for exactly that message and that turn; without
        one the outcome names the message and the turn to check.
        """
        ...

    async def threads_to_settle(self, *, limit: int = 50) -> list[str]:
        """Threads whose queue a settlement could move or pause right now.

        For the periodic sweep: a turn that ended without the finish route —
        the reaper's verdict, a control plane restarted mid-request — still
        gets its queue acted on.
        """
        ...
