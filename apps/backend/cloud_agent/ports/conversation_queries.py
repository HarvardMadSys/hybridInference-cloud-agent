"""Port: reading conversations, their jobs, and the project list.

The read half of the conversation surface. Routes and the worker both read
through it; nothing here writes.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol

from cloud_agent.domain.read_receipts import READ_RECEIPT_BATCH_SIZE
from cloud_agent.domain.thread_history import THREAD_PAGE_SIZE


class ConversationQueries(Protocol):
    """Port: reading conversations, their jobs, and the project list."""

    async def list_read_receipts(
        self, *, user_id: str, after: int | None = None, limit: int = READ_RECEIPT_BATCH_SIZE
    ) -> dict[str, Any]:
        """Page the owner's completion receipts, including archived but not deleted chats."""
        ...

    async def get_thread_for_job(
        self,
        *,
        job_id: str,
        user_id: str,
        cursor: str | None = None,
        after: str | None = None,
        limit: int = THREAD_PAGE_SIZE,
        anchor: bool = False,
    ) -> dict[str, Any] | None:
        """Return a bounded, prompt-first page; cursor reads older, after reads newer."""
        ...

    async def get_thread_message_chunk(
        self,
        *,
        job_id: str,
        user_id: str,
        message_id: int,
        offset: int,
        limit: int,
    ) -> dict[str, Any] | None:
        """Read one owned message's bounded Unicode-character slice."""
        ...

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Fetch one job by id."""
        ...

    async def owns_job(self, *, job_id: str, user_id: str) -> bool:
        """Whether the job is this owner's and its conversation is not deleted.

        The check a route makes with :meth:`get_job` when ownership is all it
        needs, without reading the job's summary or its sandbox.
        """
        ...

    async def owner_changes(
        self, *, user_id: str, job_ids: list[str], thread_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """The current values an owner's tabs show for these jobs and threads.

        ``jobs`` and ``threads``, each row with its ``revision``. Only rows the
        owner holds are returned; an id that is missing was removed or is not
        theirs, and the caller reports it as gone.
        """
        ...

    async def latest_turn(self, *, thread_id: str) -> dict[str, Any] | None:
        """The newest turn of this conversation, whatever state it is in.

        Deliberately not only finished turns: the decision has to be able to see
        a turn that is still running, because "a person's turn is in flight" is
        the reason to wait rather than the reason to schedule.
        """
        ...

    async def get_job_for_update(self, *, conn: Any, job_id: str) -> dict[str, Any] | None:
        """Read one turn inside the caller's transaction, taking its row lock.

        The decision re-reads the turn it is about to judge under the same
        transaction that writes the outcome. Between the read that built the
        context and this one the turn can have been reaped, retried or
        superseded, and a decision made about a turn that has since changed is a
        decision about nothing.
        """
        ...

    async def latest_assistant_message_state(self, *, job_id: str) -> dict[str, Any] | None:
        """The last assistant message, and whether the runtime clipped it.

        ``truncated`` is the difference between "the model said nothing more"
        and "the transport stopped carrying what it said". A report reader has
        to tell those apart: the report block is at the *end* of a message, so a
        clipped one is a message whose answer may have been removed, and reading
        it as silence is exactly the failure the completeness rule exists to
        prevent.

        Ordered by insert, not by time: every assistant message mirrors exactly
        one ``message`` event, and the insert order *is* the order the runtime
        emitted them. A turn that produced no assistant text reports ``None``
        rather than the previous turn's words, which is what makes "no report"
        distinguishable from "reported".
        """
        ...

    async def list_jobs(
        self,
        *,
        user_id: str,
        limit: int = 50,
        archived: bool = False,
        repo: str | None = None,
        temporary: bool = False,
    ) -> list[dict[str, Any]]:
        """List a user's jobs with pinned threads first, then newest first.

        ``repo`` narrows the page to one project, which is how the sidebar
        pages a single project past the global newest-first window.
        ``temporary`` narrows it to the conversations without a repository.
        """
        ...

    async def list_conversations(
        self,
        *,
        user_id: str,
        q: str = "",
        repo: str | None = None,
        include_archived: bool = False,
        pinned: bool | None = None,
        limit: int = 50,
        cursor: str | None = None,
        temporary: bool = False,
    ) -> dict[str, Any]:
        """Page complete owned conversations, searching their canonical title.

        ``temporary`` keeps only the conversations without a repository.

        Activity is the thread's own update or the most recently updated of
        its turns, whichever is later. Any turn counts, not only the newest:
        an earlier turn completes or is published after a later one was
        cancelled, and that completion is the activity. It is one probe of the
        ``(thread_id, updated_at)`` index per owned thread, never an aggregate
        over the whole history, and the newest turn is fetched in full only
        for the bounded result page. Messages and job transitions move a
        thread; lease heartbeats do not. The unique thread id breaks ties.
        A cursor is scoped to its owner and filters, and is a pagination hint,
        never authority to read a thread. Updates may move a thread ahead of a
        previously issued cursor; refreshing starts again from the latest page.
        """
        ...

    async def list_projects(self, *, user_id: str, archived: bool = False) -> list[dict[str, Any]]:
        """Summarize a user's repos, most recently active first.

        The sidebar groups tasks by project, so it needs every repo the user
        has ever run in — not just those represented in the newest-first job
        page, which would silently drop dormant projects from the tree.
        """
        ...

    async def list_sessions(
        self,
        *,
        user_id: str,
        archived: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        search: str | None = None,
        repo: str | None = None,
        state: str | None = None,
        include_summary: bool = True,
        temporary: bool = False,
    ) -> dict[str, Any]:
        """Page complete conversations independently of their number of turns.

        ``temporary`` selects the conversations without a repository, the way
        ``repo`` selects one project; the facets always count every project.

        Project facets apply search/state/archive filters before the selected
        repo. The page and its counts share a database snapshot; the opaque
        keyset remains valid when earlier rows are unarchived or deleted.
        Row-only requests skip the full-history aggregate and return null counts.
        """
        ...

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        """Read a visible thread for the owner's MCP selection surface."""
        ...
