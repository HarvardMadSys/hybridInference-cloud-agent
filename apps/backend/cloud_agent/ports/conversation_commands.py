"""Port: creating and mutating conversations and their jobs.

The write half of the conversation surface: submission, follow-ups,
archiving, titles, and the replay check that makes a resend safe.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from cloud_agent.goals.models import GoalInput


class ConversationCommands(Protocol):
    """Port: creating and mutating conversations and their jobs."""

    async def create_job(
        self,
        *,
        user_id: str,
        repo: str | None,
        task_prompt: str,
        runtime: str,
        model: str,
        execution_profile: str | None = None,
        reasoning_effort: str | None = None,
        base_sha: str | None = None,
        setup_script: str | None = None,
        environment_selection: dict[str, Any] | None = None,
        plugins: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        mcp_bindings: list[dict[str, Any]] | None = None,
        skills: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        goal: GoalInput | None = None,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Create a queued job and return it.

        ``goal`` sets the conversation's completion condition in the same
        transaction that creates it, so the first turn is already working
        toward it rather than learning about it one turn later.

        Callers that do not name a profile still go through the deployment's
        server-owned default.  This matters for internal producers that call
        the store directly: bypassing the HTTP route must not silently turn a
        secure deployment's jobs into ``trusted-v1`` work.

        ``client_request_id`` is the caller's key for this one submission,
        unique per owner, with ``client_request_fingerprint`` describing what
        was submitted under it. A resend with the same pair returns the job
        the first send created; the same key with a different fingerprint
        returns ``{"error": "client_request_conflict"}`` instead of someone
        else's task. Both or neither.
        """
        ...

    async def create_follow_up(
        self,
        *,
        parent_job_id: str,
        user_id: str,
        prompt: str,
        runtime: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        reset_reasoning_effort: bool = False,
        skills: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        goal: GoalInput | None = None,
        turn_source: str = "human",
        goal_round: int | None = None,
        goal_round_kind: str | None = None,
        goal_revision: int | None = None,
        yield_to_queued: bool = False,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Append a user turn and create the run that will answer it.

        ``yield_to_queued`` refuses to append while another turn of this
        conversation is already queued or running, answering
        ``{"error": "yielded"}``. It is asked inside this transaction's thread
        lock, which is the only place the answer cannot go stale.

        ``goal`` sets or replaces the conversation's completion condition in the
        same transaction that appends this turn.

        ``turn_source``, ``goal_round``, ``goal_round_kind`` and
        ``goal_revision`` describe an automatic round the platform is running
        toward the conversation's goal. They are written by the continuation
        path only; a person's message never sets them, and there is deliberately
        no parameter here that could change the objective — an automatic round
        cannot edit the goal it is running for.

        ``reasoning_effort`` overrides the inherited depth; omitting it keeps
        the parent's, as an omitted model does. ``reset_reasoning_effort``
        clears it back to Auto — the one thing a null override cannot express,
        since null is what "inherit" already sends.

        Every follow-up is ``queued`` until all earlier turns have finished.
        The thread row is locked before selecting the latest turn, so
        concurrent submissions form one linear chain instead of siblings that
        could later mutate the same branch at once. A request made from an old
        turn also appends to the current tip, matching chat semantics.

        ``client_request_id`` and ``client_request_fingerprint`` work as for
        :meth:`create_job`: a resend returns the turn the first send made,
        the same key under different content returns
        ``{"error": "client_request_conflict"}``.
        """
        ...

    async def create_goal_round(
        self,
        *,
        conn: Any | None = None,
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
        """Append one automatic round inside the caller's transaction.

        The scheduling obligation and the turn it produced commit together —
        that pair is the whole reason this is not a second call to
        :meth:`create_follow_up`. ``conn`` is already holding the conversation's
        lock, taken by the controller before it read the state this decision was
        made from, so nothing can be appended between the read and this write.

        Returns ``None`` when this event already produced a round. That is not
        an error: a replayed wakeup is supposed to find the work it already did
        rather than starting a second turn, and the identity that makes it find
        it is the turn that triggered the decision. It also returns ``None``
        when the conversation is gone.

        ``conn`` is the caller's transaction, and the controller always passes
        one: this insert commits with the wakeup's completion. The fallback is
        for a caller that wants a round on its own, and in that case this method
        owns the transaction.
        """
        ...

    async def set_sessions_archived(
        self, *, thread_ids: list[str], user_id: str, archived: bool
    ) -> dict[str, Any]:
        """Set every owned selected thread's archive state, or change none."""
        ...

    async def delete_sessions(self, *, thread_ids: list[str], user_id: str) -> dict[str, Any]:
        """Irreversibly hide chats from their owner while retaining audit rows.

        A chat need not be archived or idle: the same commit cancels its
        queued turns, asks a running turn to stop, pauses its goal and
        releases its residency. ``released_residencies`` lists each released
        generation with its host and grant id for the caller to revoke and to
        close on the owning host. The job, residency and sandbox ledgers are
        kept; uploaded files and unsent messages are deleted outright.
        """
        ...

    async def set_thread_archived(
        self, *, job_id: str, user_id: str, archived: bool
    ) -> dict[str, Any] | None:
        """Archive or restore the owned thread containing ``job_id``."""
        ...

    async def set_thread_title(
        self, *, job_id: str, user_id: str, title: str
    ) -> dict[str, Any] | None:
        """Rename the owned conversation without changing any turn's prompt."""
        ...

    async def get_thread_title_prompt(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> str | None:
        """Return the first prompt if this fenced attempt may still name its thread."""
        ...

    async def set_generated_thread_title(
        self, *, job_id: str, attempt_id: int, lease_generation: int, title: str
    ) -> bool:
        """Save a missing/recovered title while its attempt remains current."""
        ...

    async def set_thread_pinned(
        self, *, job_id: str, user_id: str, pinned: bool
    ) -> dict[str, Any] | None:
        """Pin or unpin the owned thread containing ``job_id``."""
        ...

    async def replayed_submission(
        self, *, user_id: str, client_request_id: str, fingerprint: str | None
    ) -> dict[str, Any] | None:
        """The job an owner's key already made, a conflict, or ``None`` for a new key.

        Public because the routes ask it *before* their checks that can change
        between two sends of one submission — model resolution, repo
        entitlement, tool-server connections, whether the session is still
        open. A resend must reach the job the first send made whatever has
        happened since; only a key that has never been used runs those checks.
        :meth:`create_job` and :meth:`create_follow_up` still ask it
        themselves, so a race that slips past the route is still resolved by
        the unique index rather than by a second task.
        """
        ...
