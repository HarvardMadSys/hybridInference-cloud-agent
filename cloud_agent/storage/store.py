"""Persistence for cloud agent-sandbox jobs (issue #1041, P0).

A small, self-contained store over the same Postgres pool used by the rest of
the gateway — kept separate from the (cached, auth-hot) ``OperationalStore``
contract, following the ``ResponseStore`` precedent.

Design (adjudicated in issue #1041):

- **State triad separation** — the sandbox/machine can vanish at any time;
  jobs, attempts, and events live here independently.
- **An expired lease is dead immediately**, not once the reaper notices. Every
  fenced write requires ``lease_expires_at > NOW()`` as well as a matching
  generation, so the window between expiry and the reaper's next pass is not a
  window in which a stalled worker can still write, renew, or finish.
- **Attempts + fencing tokens** — each grant of ownership is a new
  ``agent_attempts`` row with a monotonically increasing ``lease_generation``.
  Every write from a worker (heartbeat, events, state transitions) is
  compare-and-swapped against ``(attempt_id, lease_generation,
  status='running')`` so a zombie worker whose lease was reaped can never
  write again — the split-brain scenario is structurally impossible.
- **Reaper closes, never resets** — an expired attempt is marked
  ``superseded`` (with an ``attempt_superseded`` control event) and the job is
  requeued as a *new* attempt; history is append-only.
- **Publish is a group-snapshot side effect** — Agent turns stay terminal while
  an immutable ``agent_sandbox_snapshots`` row is claimed with ``SKIP LOCKED``.
  The sandbox never uploads a per-turn patch and never writes publish state.
- **Events** carry a global ``BIGSERIAL`` id (the SSE ``Last-Event-ID``
  cursor) plus a per-attempt ``seq`` with ``UNIQUE(attempt_id, seq)``. UI
  rewind after a retry is an ``attempt_superseded`` control event plus the new
  attempt's events — old events are never deleted or rewritten.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.capacity_protocol import (
    CAPACITY_HISTORY_RETENTION_S,
    CapacityHistoryPointWrite,
)
from cloud_agent.domain.execution import (
    ATTEMPT_ABORTED,
    ATTEMPT_RUNNING,
    CANCELLED,
    EVENT_ATTEMPT_ABORTED,
    EVENT_ATTEMPT_SUPERSEDED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATES,
)
from cloud_agent.domain.requests import check_client_request
from cloud_agent.execution_profiles import (
    KATA_RUNTIME,
    ExecutionProfile,
    compatible_shared_sandbox_followup_attested,
    default_execution_profile,
    shared_sandbox_claim_attested,
)
from cloud_agent.host_pool import (
    CLAIMABLE_STATUSES,
    HOST_ONLINE,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.sandbox import NATIVE_FORK_CAPABILITY_JSON, THREAD_SANDBOX_SOURCE
from cloud_agent.storage.repositories.audit import AuditRepository
from cloud_agent.storage.repositories.credential_queries import CredentialQueries
from cloud_agent.storage.repositories.grants import GrantRepository
from cloud_agent.storage.repositories.host_capacity import HostCapacityRepository
from cloud_agent.storage.repositories.host_pool import HostPoolRepository
from cloud_agent.storage.repositories.ledger import LedgerRepository
from cloud_agent.storage.repositories.mcp import McpConnectionRepository
from cloud_agent.storage.repositories.source_control import SourceControlRepository
from cloud_agent.task_titles import fallback_title

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)


def _new_job_id() -> str:
    """Generate a new agent job id."""
    return f"ajob_{secrets.token_hex(8)}"


def _new_thread_id() -> str:
    """Generate a stable conversation id shared by a thread's runs."""
    return f"athr_{secrets.token_hex(8)}"


def _new_sandbox_id() -> str:
    """Generate the platform identity of one retained Docker sandbox."""
    return f"asbx_{secrets.token_hex(16)}"


def _new_session_fork_id() -> str:
    """Generate one shared native-session fork operation id."""
    return f"asfork_{secrets.token_hex(16)}"


def _sandbox_row_to_dict(row: Any) -> dict[str, Any]:
    """Decode the JSON members embedded in the shared sandbox read shape."""
    result = dict(row)
    result["group_members"] = load_json(result.get("group_members")) or []
    return result


def resolve_runtime_version(
    *,
    runtime: str,
    thread_version: str | None,
    selected: dict[str, Any],
    available: dict[str, Any],
    host_default: dict[str, Any],
) -> str | None:
    """Which CLI version this attempt runs, in priority order.

    1. **The version its thread already runs.** A thread is a native CLI
       session stored in its retained container. An admin changing the
       selection must not reach into conversations already in flight: it
       applies to new groups, and existing ones keep the version they started
       on so the same native session remains readable.
    2. **The deployment's selection**, but only if this host's image actually
       holds it. A selection the host cannot serve falls through rather than
       failing the claim — an image rebuilt with a shorter menu drops versions
       out from under a stored choice, and a queue that parks on that is a
       worse outcome than a job running the default.
    3. **The host's default**, which is what every claim did before selection
       existed.

    ``None`` means the host has never reported a version for this runtime, and
    is stamped as-is: "unknown" is a truthful record, and inventing one would
    make the attempt row lie about what ran.
    """
    if thread_version:
        return thread_version
    choice = selected.get(runtime)
    if isinstance(choice, str) and choice:
        offered = available.get(runtime)
        if isinstance(offered, list) and choice in offered:
            return choice
        logger.warning(
            "Runtime version selection not offered by the claiming host",
            extra={
                "event": "agent_runtime_version_unavailable",
                "runtime": runtime,
                "selected": choice,
            },
        )
    default = host_default.get(runtime)
    return default if isinstance(default, str) and default else None


def _job_row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an ``agent_jobs`` row to a plain dict."""
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "parent_job_id": row["parent_job_id"],
        "turn_no": row["turn_no"],
        "native_turn_no": row["native_turn_no"],
        "user_id": row["user_id"],
        "repo": row["repo"],
        "base_sha": row["base_sha"],
        "task_prompt": row["task_prompt"],
        "title": row.get("title") or row.get("session_title"),
        "title_source": row.get("title_source"),
        "setup_script": row["setup_script"],
        "runtime": row["runtime"],
        "model": row["model"],
        "execution_profile": row["execution_profile"],
        "reasoning_effort": row["reasoning_effort"],
        "state": row["state"],
        "conversation_state": row.get("conversation_state"),
        "cancel_requested": row["cancel_requested"],
        "terminal_resume_pending": row["terminal_resume_pending"],
        "current_attempt_id": row["current_attempt_id"],
        "published_pr_url": row["published_pr_url"],
        "published_commit_sha": row["published_commit_sha"],
        "detail": row["detail"],
        "metadata": load_json(row["metadata"]),
        "fork_source_job_id": row["fork_source_job_id"],
        # Resolved once, at creation, against the deployment registry — not
        # re-read per attempt. A job runs with the tool surface it was created
        # with, so removing a server from the registry stops new jobs from
        # asking for it without changing what a running one was promised.
        "mcp_servers": list(row["mcp_servers"] or []),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "pinned_at": row.get("pinned_at"),
        "archived_at": row.get("archived_at"),
        "deleted_at": row.get("deleted_at"),
        "thread_sandbox": load_json(row.get("thread_sandbox")),
    }


# A job's own columns. What a job shares with every other turn of its
# conversation — repo, setup script, execution profile, tool servers — lives on
# the thread, so every read of a job joins ``agent_threads t`` and reports the
# thread's values under the job's keys.
_JOB_OWN_COLUMNS = (
    "id, thread_id, parent_job_id, turn_no, native_turn_no, user_id, base_sha, task_prompt, "
    "runtime, model, reasoning_effort, state, "
    "cancel_requested, terminal_resume_pending, current_attempt_id, "
    "published_pr_url, published_commit_sha, detail, "
    "metadata, fork_source_job_id, "
    "created_at, updated_at"
)
_THREAD_CONFIG_COLUMNS = "t.repo, t.setup_script, t.execution_profile, t.mcp_servers"
# Probe the unfinished-turn index before the latest-turn index: a cancelled
# queued follow-up must not hide work that is still running earlier in the chat.
_CONVERSATION_STATE_SQL = """
COALESCE(
    (SELECT active.state FROM agent_jobs active
     WHERE active.thread_id = j.thread_id AND active.user_id = j.user_id
       AND active.state IN ('queued', 'running')
     ORDER BY CASE WHEN active.state = 'running' THEN 0 ELSE 1 END, active.turn_no DESC
     LIMIT 1),
    (SELECT CASE WHEN latest.state = 'failed' THEN 'failed' ELSE 'idle' END
     FROM agent_jobs latest
     WHERE latest.thread_id = j.thread_id AND latest.user_id = j.user_id
     ORDER BY latest.turn_no DESC LIMIT 1)
)"""
_QUALIFIED_JOB_COLUMNS = (
    ", ".join(f"j.{column.strip()}" for column in _JOB_OWN_COLUMNS.split(","))
    + ", "
    + _THREAD_CONFIG_COLUMNS
    + f", {_CONVERSATION_STATE_SQL} AS conversation_state"
)
_JOB_SELECT = (
    f"SELECT {_QUALIFIED_JOB_COLUMNS} FROM agent_jobs j JOIN agent_threads t ON t.id = j.thread_id"
)


def _session_query(*, filter_repo: bool = False) -> str:
    """Filter project rows before looking up each conversation's latest turn."""
    repo_filter = "AND ($5::text IS NULL OR t.repo = $5)" if filter_repo else ""
    return f"""
WITH latest AS (
    SELECT {_QUALIFIED_JOB_COLUMNS}, t.id AS session_id, t.title AS session_title, t.title_source,
           t.archived_at, t.pinned_at, COALESCE(t.archived_at, j.created_at) AS sort_at,
           CASE
               WHEN j.state = 'queued' THEN 'queued'
               WHEN j.state = 'running' THEN 'running'
               WHEN j.state = 'succeeded' AND NULLIF(j.published_pr_url, '') IS NOT NULL THEN 'done'
               WHEN j.state = 'succeeded' THEN 'needs_review'
               ELSE j.state
           END AS display_state
    FROM agent_threads t
    CROSS JOIN LATERAL (
        SELECT * FROM agent_jobs latest_job
        WHERE latest_job.thread_id = t.id AND latest_job.user_id = $1
        ORDER BY latest_job.turn_no DESC, latest_job.id DESC
        LIMIT 1
    ) j
    WHERE t.user_id = $1 AND t.deleted_at IS NULL
      AND (($2 AND t.archived_at IS NOT NULL) OR (NOT $2 AND t.archived_at IS NULL))
      AND ($3::text IS NULL OR STRPOS(LOWER(t.title), LOWER($3)) > 0)
      {repo_filter}
), matched AS (
    SELECT * FROM latest
    WHERE $4::text IS NULL
       OR CASE WHEN $4 IN ('done', 'needs_review', 'cancelled')
               THEN display_state ELSE conversation_state END = $4
)
"""


# An idle native process and its HOT credential lease outlive the completed
# turn. Removing a chat from owner history must not tear down that process or
# its shared workspace. Actual work and unfinished publication remain visible
# until they settle; the retained rows keep lineage and billing attributable.
_SESSION_DELETE_BLOCK_SQL = """
CASE
    WHEN t.archived_at IS NULL THEN 'not_archived'
    WHEN EXISTS (
        SELECT 1 FROM agent_jobs busy_job
        WHERE busy_job.thread_id = t.session_id
          AND busy_job.state NOT IN ('succeeded', 'failed', 'cancelled')
    ) OR EXISTS (
        SELECT 1 FROM agent_attempts a
        JOIN agent_jobs busy_job ON busy_job.id = a.job_id
        WHERE busy_job.thread_id = t.session_id
          AND a.status = 'running' AND a.lease_expires_at > NOW()
    ) OR EXISTS (
        SELECT 1 FROM agent_thread_sandbox_memberships m
        WHERE m.thread_id = t.session_id
          AND (m.active_attempt_id IS NOT NULL OR m.resident_state IN ('starting', 'restarting'))
    ) THEN 'session_active'
    WHEN EXISTS (
        SELECT 1 FROM agent_session_forks f
        WHERE (f.source_thread_id = t.session_id OR f.target_thread_id = t.session_id)
          AND f.state IN ('preparing', 'materialized')
    ) OR EXISTS (
        SELECT 1 FROM agent_thread_sandbox_memberships m
        WHERE m.thread_id = t.session_id AND m.fork_operation_id IS NOT NULL
    ) THEN 'fork_in_progress'
    WHEN EXISTS (
        SELECT 1 FROM agent_sandbox_snapshots snapshot
        JOIN agent_snapshot_jobs included ON included.snapshot_id = snapshot.id
        JOIN agent_jobs included_job ON included_job.id = included.job_id
        WHERE included_job.thread_id = t.session_id
          AND snapshot.state IN ('pending', 'capturing', 'captured', 'publishing')
    ) THEN 'publication_pending'
    ELSE NULL
END
"""


def _session_cursor_scope(
    user_id: str, archived: bool, search: str | None, repo: str | None, state: str | None
) -> str:
    """Bind pagination to the same owner's filter selection."""
    return hashlib.sha256(json.dumps([user_id, archived, search, repo, state]).encode()).hexdigest()


def _decode_session_cursor(cursor: str | None, scope: str) -> tuple[datetime | None, str | None]:
    """Validate a bounded keyset cursor before sending its values to SQL."""
    if cursor is None:
        return None, None
    try:
        if len(cursor) > 1024:
            raise ValueError
        payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or payload.get("scope") != scope:
            raise ValueError
        sort_at = datetime.fromisoformat(payload["at"])
        thread_id = payload["id"]
        if (
            sort_at.tzinfo is None
            or not isinstance(thread_id, str)
            or not thread_id
            or len(thread_id) > 128
            or "\x00" in thread_id
        ):
            raise ValueError
        return sort_at, thread_id
    except (ValueError, TypeError, KeyError, binascii.Error) as exc:
        raise ValueError("Invalid session cursor for these filters") from exc


def _encode_session_cursor(sort_at: datetime, thread_id: str, scope: str) -> str:
    """Encode the last conversation key, not an offset into a moving list."""
    return base64.urlsafe_b64encode(
        json.dumps({"at": sort_at.isoformat(), "id": thread_id, "scope": scope}).encode()
    ).decode()


def _decode_conversation_cursor(
    cursor: str, scope: str
) -> tuple[bool, datetime | None, datetime, str]:
    """Validate a pagination boundary, including its owner and search scope."""
    try:
        if not cursor or len(cursor) > 2048:
            raise ValueError
        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(data, dict)
            or type(data.get("v")) is not int
            or data["v"] != 1
            or data.get("scope") != scope
        ):
            raise ValueError
        pin = data["pin"]
        activity = datetime.fromisoformat(data["activity"])
        thread = data["thread"]
        if pin is not None:
            pin = datetime.fromisoformat(pin)
            if pin.tzinfo is None:
                raise ValueError
        if activity.tzinfo is None or not isinstance(thread, str) or not 1 <= len(thread) <= 128:
            raise ValueError
        if "\x00" in thread:
            raise ValueError
        return pin is not None, pin, activity, thread
    except (ValueError, TypeError, KeyError, binascii.Error, OverflowError, RecursionError) as exc:
        raise ValueError("Invalid conversation cursor for this search") from exc


class AgentJobStore:
    """Postgres-backed store for jobs, attempts, events, and retained groups."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap a shared asyncpg pool.

        The repositories below are handed the same pool, so a migrated
        capability is a second view of one pool rather than a second
        connection.
        """
        self._pool = pool
        self._mcp = McpConnectionRepository(pool)
        self._credential_queries = CredentialQueries(pool)
        self._audit = AuditRepository(pool)
        self._host_pool = HostPoolRepository(pool)
        self._source_control = SourceControlRepository(pool)
        self._ledger = LedgerRepository(pool)
        self._host_capacity = HostCapacityRepository(pool)
        self._grants = GrantRepository(pool)

    @staticmethod
    async def _fail_unrunnable_follow_ups(
        conn: Any,
        *,
        thread_id: str,
        detail: str = "not run because an earlier turn lost the retained sandbox context",
    ) -> None:
        """Give queued descendants a terminal answer when continuity is LOST.

        A retained chat cannot be claimed again after its context becomes
        indeterminate. Leaving already-submitted turns in ``queued`` would
        therefore create a permanent queue entry rather than
        a recoverable retry. Every LOST transition calls this helper in the
        same transaction that fences the sandbox row.
        """
        await conn.execute(
            """
            UPDATE agent_jobs
            SET state = 'failed', detail = $2, updated_at = NOW()
            WHERE thread_id = $1 AND state = 'queued'
            """,
            thread_id,
            detail[:2000],
        )

    @staticmethod
    async def _fail_discontinuous_follow_ups(
        conn: Any,
        *,
        parent_job_id: str | None = None,
    ) -> None:
        """Fail queued descendants that cannot preserve native continuity.

        A successful parent requires a usable shared membership. The whole
        pending tail fails if that membership is missing or lost. Terminal
        writers scope this cleanup to the parent they just changed; the
        periodic sweep handles legacy rows. Scheduling itself only checks
        eligibility and never needs to promote a queued turn.
        """
        await conn.execute(
            """
            WITH invalid_threads AS (
                SELECT DISTINCT child.thread_id
                FROM agent_jobs child
                JOIN agent_jobs parent ON parent.id = child.parent_job_id
                WHERE child.state = 'queued'
                  AND parent.state = 'succeeded'
                  AND ($1::text IS NULL OR parent.id = $1)
                  AND NOT EXISTS (
                        SELECT 1
                        FROM agent_thread_sandbox_memberships m
                        JOIN agent_sandboxes g ON g.id = m.sandbox_id
                        WHERE m.thread_id = child.thread_id
                          AND m.context_status <> 'lost'
                          AND m.native_state <> 'lost'
                          AND g.state NOT IN ('stopped_dirty', 'lost')
                  )
            )
            UPDATE agent_jobs pending
            SET state = 'failed',
                detail = 'not run because its parent predates retained-session continuity',
                updated_at = NOW()
            WHERE pending.thread_id IN (SELECT thread_id FROM invalid_threads)
              AND pending.state = 'queued'
            """,
            parent_job_id,
        )

    async def sweep_queued_follow_ups(self) -> None:
        """Fail queued follow-ups whose earlier native context is unavailable.

        Claiming already excludes these rows. This periodic safety net gives
        legacy pending turns a terminal answer using the partial queued
        indexes, without a cleanup scan on every claim poll.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await self._fail_discontinuous_follow_ups(conn)

    async def initialize(self) -> None:
        """No-op: schema and backfills are Alembic's now, not startup's.

        In the source repository this method ran on every boot and did two
        jobs. It created the tables and indexes — which a migration tool
        exists to replace, since creating a table "if not exists" silently
        does nothing when the table exists with the *wrong* columns, hence the
        trail of ADD COLUMN statements after it. And it ran five **data**
        migrations: backfilling threads and thread messages for jobs that
        predate them, seeding the singleton runner-policy row, and setting
        thread_id/turn_no on legacy jobs.

        Both moved to ``migrations/versions/0001_baseline.py``, verbatim. The
        second half is easy to overlook precisely because it hid inside a
        method named for the first.

        Kept as a no-op rather than deleted so E6 can move the bootstrap call
        site unchanged.
        """
        return None

    async def record_repo_grant(
        self, *, user_id: str, installation_id: int, account_login: str | None = None
    ) -> None:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.record_repo_grant(
            user_id=user_id, installation_id=installation_id, account_login=account_login
        )

    async def list_repo_grants(self, *, user_id: str) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.list_repo_grants(user_id=user_id)

    async def revoke_repo_grant(self, *, user_id: str, installation_id: int) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.revoke_repo_grant(
            user_id=user_id, installation_id=installation_id
        )

    # OAuth state is stored hashed and consumed atomically. A callback for a
    # different user/provider, an expired state, and a replay all return no
    # row and therefore cannot be distinguished or used to claim authority.

    async def create_oauth_state(
        self,
        *,
        state_hash: str,
        user_id: str,
        provider: str,
        code_verifier_ciphertext: str | None,
        expires_at: Any,
    ) -> None:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.create_oauth_state(
            state_hash=state_hash,
            user_id=user_id,
            provider=provider,
            code_verifier_ciphertext=code_verifier_ciphertext,
            expires_at=expires_at,
        )

    async def consume_oauth_state(
        self, *, state_hash: str, user_id: str, provider: str
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.consume_oauth_state(
            state_hash=state_hash, user_id=user_id, provider=provider
        )

    async def upsert_gitlab_connection(
        self,
        *,
        user_id: str,
        external_user_id: int,
        username: str,
        display_name: str | None,
        web_url: str | None,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> int:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.upsert_gitlab_connection(
            user_id=user_id,
            external_user_id=external_user_id,
            username=username,
            display_name=display_name,
            web_url=web_url,
            access_token_ciphertext=access_token_ciphertext,
            refresh_token_ciphertext=refresh_token_ciphertext,
            expires_at=expires_at,
        )

    async def get_gitlab_connection(self, *, user_id: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.get_gitlab_connection(user_id=user_id)

    async def update_gitlab_tokens(
        self,
        *,
        connection_id: int,
        user_id: str,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.update_gitlab_tokens(
            connection_id=connection_id,
            user_id=user_id,
            access_token_ciphertext=access_token_ciphertext,
            refresh_token_ciphertext=refresh_token_ciphertext,
            expires_at=expires_at,
        )

    async def delete_gitlab_connection(self, *, user_id: str, connection_id: int) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._source_control.delete_gitlab_connection(
            user_id=user_id, connection_id=connection_id
        )

    # ── MCP connections ────────────────────────────────────────────────
    #
    # One row per user per server: what *this person* connected, holding their
    # credential. Every query here is scoped by ``user_id`` and none takes a
    # connection id alone, so there is no shape in which one user's row can be
    # read or removed through another's request — the property the deployment
    # registry could not have, because it had no user to scope by.

    async def list_mcp_connections(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return this user's MCP connections. Implemented by the repository."""
        return await self._mcp.list_mcp_connections(user_id=user_id)

    async def upsert_mcp_connection(
        self,
        *,
        user_id: str,
        server: str,
        account_label: str | None,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str | None,
        expires_at: Any,
    ) -> int:
        """Store one credential per server. Implemented by the repository."""
        return await self._mcp.upsert_mcp_connection(
            user_id=user_id,
            server=server,
            account_label=account_label,
            access_token_ciphertext=access_token_ciphertext,
            refresh_token_ciphertext=refresh_token_ciphertext,
            expires_at=expires_at,
        )

    async def delete_mcp_connection(self, *, user_id: str, server: str) -> bool:
        """Disconnect one server. Implemented by the repository."""
        return await self._mcp.delete_mcp_connection(user_id=user_id, server=server)

    # ── Jobs ───────────────────────────────────────────────────────────

    async def create_job(
        self,
        *,
        user_id: str,
        repo: str,
        task_prompt: str,
        runtime: str,
        model: str,
        execution_profile: str | None = None,
        reasoning_effort: str | None = None,
        base_sha: str | None = None,
        setup_script: str | None = None,
        metadata: dict[str, Any] | None = None,
        mcp_servers: list[str] | None = None,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Create a queued job and return it.

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
        check_client_request(client_request_id, client_request_fingerprint)
        if execution_profile is None:
            execution_profile = default_execution_profile().value
        if client_request_id is not None:
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            if replayed is not None:
                return replayed
        job_id = _new_job_id()
        thread_id = _new_thread_id()
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                # What every turn of this conversation shares is written once,
                # here, on the thread; the turns below carry only their own.
                await conn.execute(
                    """
                    INSERT INTO agent_threads
                        (id, user_id, repo, title, title_source,
                         setup_script, execution_profile, mcp_servers)
                    VALUES ($1, $2, $3, $4, 'fallback', $5, $6, $7)
                    """,
                    thread_id,
                    user_id,
                    repo,
                    fallback_title(task_prompt),
                    setup_script,
                    execution_profile,
                    list(mcp_servers or []),
                )
                await conn.execute(
                    """
                    INSERT INTO agent_jobs
                        (id, thread_id, turn_no, user_id, base_sha, task_prompt,
                         runtime, model, reasoning_effort, metadata,
                         client_request_id, client_request_fingerprint)
                    VALUES ($1, $2, 1, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
                    """,
                    job_id,
                    thread_id,
                    user_id,
                    base_sha,
                    task_prompt,
                    runtime,
                    model,
                    reasoning_effort,
                    json.dumps(metadata) if metadata is not None else None,
                    client_request_id,
                    client_request_fingerprint,
                )
                row = await conn.fetchrow(f"{_JOB_SELECT} WHERE j.id = $1", job_id)
                await conn.execute(
                    """
                    INSERT INTO agent_thread_messages (thread_id, job_id, role, content)
                    VALUES ($1, $2, 'user', $3)
                    """,
                    thread_id,
                    job_id,
                    task_prompt,
                )
        except asyncpg.UniqueViolationError as exc:
            # Two sends of one submission racing: the loser reads the winner.
            if exc.constraint_name != "uq_agent_jobs_client_request":
                raise
            # The violated constraint is on client_request_id, so the winner has one.
            assert client_request_id is not None
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            assert replayed is not None
            return replayed
        result = _job_row_to_dict(row)
        result["title"] = fallback_title(task_prompt)
        result["title_source"] = "fallback"
        return result

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
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Append a user turn and create the run that will answer it.

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
        check_client_request(client_request_id, client_request_fingerprint)
        if client_request_id is not None:
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            if replayed is not None:
                return replayed
        try:
            return await self._create_follow_up(
                parent_job_id=parent_job_id,
                user_id=user_id,
                prompt=prompt,
                runtime=runtime,
                model=model,
                reasoning_effort=reasoning_effort,
                reset_reasoning_effort=reset_reasoning_effort,
                client_request_id=client_request_id,
                client_request_fingerprint=client_request_fingerprint,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name != "uq_agent_jobs_client_request":
                raise
            # The violated constraint is on client_request_id, so the winner has one.
            assert client_request_id is not None
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            assert replayed is not None
            return replayed

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
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, client_request_fingerprint FROM agent_jobs "
                "WHERE user_id = $1 AND client_request_id = $2",
                user_id,
                client_request_id,
            )
        if row is None:
            return None
        if row["client_request_fingerprint"] != fingerprint:
            return {"error": "client_request_conflict"}
        return await self.get_job(str(row["id"]))

    async def _create_follow_up(
        self,
        *,
        parent_job_id: str,
        user_id: str,
        prompt: str,
        runtime: str | None,
        model: str | None,
        reasoning_effort: str | None,
        reset_reasoning_effort: bool,
        client_request_id: str | None,
        client_request_fingerprint: str | None,
    ) -> dict[str, Any] | None:
        """The one-transaction body of :meth:`create_follow_up`."""
        job_id = _new_job_id()
        async with self._pool.acquire() as conn, conn.transaction():
            requested = await conn.fetchrow(
                "SELECT thread_id FROM agent_jobs WHERE id = $1 AND user_id = $2",
                parent_job_id,
                user_id,
            )
            if requested is None:
                return None
            thread = await conn.fetchrow(
                "SELECT id, title, title_source, pinned_at, archived_at FROM agent_threads "
                "WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE",
                requested["thread_id"],
                user_id,
            )
            if thread is None:
                return None
            if thread["archived_at"] is not None:
                return {"error": "session_archived"}
            thread_id = thread["id"]
            parent = await conn.fetchrow(
                f"{_JOB_SELECT} WHERE j.thread_id = $1 "
                "ORDER BY j.turn_no DESC LIMIT 1 FOR UPDATE OF j",
                thread_id,
            )
            if parent is None:  # Defensive: every thread is created with turn one.
                return None
            # The owner-facing route checks this before doing entitlement and
            # MCP work, but that check is necessarily advisory: an activation
            # can become indeterminate while those awaits run. Locking the
            # durable sandbox here closes that TOCTOU. If this transaction
            # wins the row first, the concurrent LOST writer subsequently
            # fails the inserted turn in its own transaction; if LOST wins,
            # no turn is inserted at all.
            # Every lifecycle writer takes membership before sandbox. Locking
            # the two joined rows in planner order would make that order
            # implicit and could invert against host reconciliation.
            await conn.fetchval(
                """
                SELECT thread_id
                FROM agent_thread_sandbox_memberships
                WHERE thread_id = $1
                FOR UPDATE
                """,
                thread_id,
            )
            sandbox = await conn.fetchrow(
                """
                SELECT g.state, m.context_status, TRUE AS shared_live
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1
                FOR UPDATE OF g
                """,
                thread_id,
            )
            if sandbox is not None and (
                sandbox["context_status"] == "lost" or sandbox["state"] in {"stopped_dirty", "lost"}
            ):
                return {"error": "sandbox_context_lost"}
            if sandbox is None and parent["state"] == SUCCEEDED:
                # A successful native turn must have settled inside its group.
                # Rows that predate that invariant are audit history, not a
                # source from which the new runtime can fabricate continuity.
                return {"error": "sandbox_context_lost"}
            turn_no = parent["turn_no"] + 1
            # Repo, setup script, execution profile and tool servers are the
            # thread's, not copied per turn: the next turn of a conversation
            # runs with the surface the thread has been running with, and a
            # follow-up cannot quietly gain a server the owner never chose.
            await conn.execute(
                """
                INSERT INTO agent_jobs
                    (id, thread_id, parent_job_id, turn_no, user_id, base_sha,
                     task_prompt, runtime, model, reasoning_effort, state, metadata,
                     client_request_id, client_request_fingerprint)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14)
                """,
                job_id,
                thread_id,
                parent["id"],
                turn_no,
                user_id,
                parent["published_commit_sha"] or parent["base_sha"],
                prompt,
                runtime or parent["runtime"],
                model or parent["model"],
                None
                if reset_reasoning_effort
                else (reasoning_effort or parent["reasoning_effort"]),
                QUEUED,
                json.dumps(load_json(parent["metadata"]))
                if parent["metadata"] is not None
                else None,
                client_request_id,
                client_request_fingerprint,
            )
            row = await conn.fetchrow(f"{_JOB_SELECT} WHERE j.id = $1", job_id)
            await conn.execute(
                """
                INSERT INTO agent_thread_messages (thread_id, job_id, role, content)
                VALUES ($1, $2, 'user', $3)
                """,
                thread_id,
                job_id,
                prompt,
            )
            await conn.execute(
                "UPDATE agent_threads SET updated_at = NOW() WHERE id = $1", thread_id
            )
        result = _job_row_to_dict(row)
        result["title"] = thread["title"]
        result["title_source"] = thread["title_source"]
        result["pinned_at"] = thread["pinned_at"]
        return result

    async def _session_fork_details(self, conn: Any, operation_id: str) -> dict[str, Any] | None:
        """Return one native-fork saga with its immutable host launch inputs."""
        operation = await conn.fetchrow(
            "SELECT * FROM agent_session_forks WHERE id = $1",
            operation_id,
        )
        if operation is None:
            return None
        sandbox = await conn.fetchrow(
            f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
            operation["source_thread_id"],
        )
        source = await conn.fetchrow(
            f"{_JOB_SELECT} WHERE j.id = $1",
            operation["source_job_id"],
        )
        result = dict(operation)
        result["host_receipt"] = load_json(result.get("host_receipt"))
        if sandbox is not None:
            sandbox_dict = _sandbox_row_to_dict(sandbox)
            result["thread_sandbox"] = sandbox_dict
            for key, value in sandbox_dict.items():
                result.setdefault(key, value)
        if source is not None:
            result["source_job"] = _job_row_to_dict(source)
            # Assigned at the native settlement boundary, not inferred from
            # the final job state. A lease may expire after context settlement
            # but before job finalization; subsequent successful turns must
            # still resolve to the provider's correct ordinal.
            result["source_native_turn_no"] = int(source["native_turn_no"] or 0)
        return result

    async def prepare_session_fork(
        self,
        *,
        source_job_id: str,
        user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Fence a settled native boundary and reserve an invisible child."""
        if not idempotency_key or len(idempotency_key) > 256 or "\x00" in idempotency_key:
            raise ValueError("session fork idempotency key is invalid")
        operation_id = _new_session_fork_id()
        target_thread_id = _new_thread_id()
        async with self._pool.acquire() as conn, conn.transaction():
            # A request may race itself across two HTTP workers. Serialize the
            # unique-key lookup before either can observe a missing row.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                json.dumps([user_id, idempotency_key], separators=(",", ":")),
            )
            # Finalization holds the operation before the source thread. An
            # idempotent retry must use that same order before checking the
            # owner-history tombstone, or it could deadlock with finalization.
            existing = await conn.fetchrow(
                """
                SELECT * FROM agent_session_forks
                WHERE user_id = $1 AND idempotency_key = $2
                FOR UPDATE
                """,
                user_id,
                idempotency_key,
            )
            thread_id = await conn.fetchval(
                "SELECT thread_id FROM agent_jobs WHERE id = $1 AND user_id = $2",
                source_job_id,
                user_id,
            )
            owned_thread = await conn.fetchval(
                "SELECT id FROM agent_threads "
                "WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE",
                thread_id,
                user_id,
            )
            if owned_thread is None:
                return None
            if existing is not None:
                result = await self._session_fork_details(conn, str(existing["id"]))
                if result is not None and existing["source_job_id"] != source_job_id:
                    result["error"] = "idempotency_conflict"
                return result

            source = await conn.fetchrow(
                f"{_JOB_SELECT} WHERE j.id = $1 AND j.user_id = $2 FOR UPDATE OF j",
                source_job_id,
                user_id,
            )
            if source is None:
                return None
            # A failed/cancelled platform job is not proof of a completed native
            # exchange. Historical rewind is anchored only on successful turns;
            # their ordinal is resolved against the provider transcript later.
            if source["state"] != SUCCEEDED:
                return {"error": "anchor_not_settled"}
            if not source["native_turn_no"]:
                return {"error": "native_context_unavailable"}
            await conn.fetchval(
                """
                SELECT thread_id
                FROM agent_thread_sandbox_memberships
                WHERE thread_id = $1
                FOR UPDATE
                """,
                source["thread_id"],
            )
            row = await conn.fetchrow(
                f"""
                SELECT m.*, g.generation AS sandbox_generation, g.host,
                       g.container_id, g.container_name, g.runtime_id,
                       g.runtime_version, g.sandbox_image_digest,
                       g.execution_profile, g.state AS sandbox_state,
                       g.resident_slot_capacity, g.memory_limit, g.cpu_limit,
                       g.stop_timeout_seconds,
                       EXISTS (
                           SELECT 1 FROM agent_runner_hosts h
                           WHERE h.host = g.host
                             AND h.status = 'online'
                             AND h.capabilities @>
                                 '{NATIVE_FORK_CAPABILITY_JSON}'::jsonb
                             AND h.sandbox_image_digest = g.sandbox_image_digest
                       ) AS native_fork_capable
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1
                FOR UPDATE OF g
                """,
                source["thread_id"],
            )
            if row is None:
                return {"error": "not_shared_live"}
            if not row["native_fork_capable"]:
                # Existing retained v1 containers remain valid for follow-up
                # turns.  Their bundled helpers do not understand historical
                # anchors, however, so never degrade an at-anchor fork into a
                # latest-session fork during a rolling upgrade.
                return {"error": "native_fork_unavailable"}
            if row["active_attempt_id"] is not None:
                return {"error": "source_busy"}
            if row["native_state"] != "ready" or not row["native_session_id"]:
                return {"error": "native_context_unavailable"}
            latest_settled = await conn.fetchrow(
                """
                SELECT turn_no, native_turn_no FROM agent_jobs
                WHERE id = $1 AND thread_id = $2
                """,
                row["last_settled_job_id"],
                source["thread_id"],
            )
            if (
                latest_settled is None
                or latest_settled["native_turn_no"] is None
                or int(latest_settled["turn_no"]) < int(source["turn_no"])
                or int(latest_settled["native_turn_no"]) < int(source["native_turn_no"])
            ):
                return {"error": "native_context_unavailable"}
            if row["resident_slot_id"] is None:
                return {"error": "sandbox_not_ready"}
            if row["fork_operation_id"] is not None:
                return {"error": "fork_in_progress"}
            if row["sandbox_state"] in {
                "lost",
                "stopped_dirty",
                "provisioning",
                "stopping",
            }:
                return {"error": "sandbox_not_ready"}
            if not row["container_id"]:
                return {"error": "sandbox_not_ready"}
            if row["runtime_id"] != source["runtime"]:
                return {"error": "runtime_mismatch"}

            occupied_rows = await conn.fetch(
                """
                SELECT resident_slot_id AS slot
                FROM agent_thread_sandbox_memberships
                WHERE sandbox_id = $1 AND resident_slot_id IS NOT NULL
                UNION ALL
                SELECT target_resident_slot_id AS slot
                FROM agent_session_forks
                WHERE sandbox_id = $1 AND state IN ('preparing', 'materialized')
                  AND target_resident_slot_id IS NOT NULL
                """,
                row["sandbox_id"],
            )
            occupied = {int(item["slot"]) for item in occupied_rows}
            target_slot = next(
                (
                    slot
                    for slot in range(int(row["resident_slot_capacity"]))
                    if slot not in occupied
                ),
                None,
            )
            if target_slot is None:
                return {"error": "resident_capacity_exhausted"}

            # The child exists from here on: a thread with no turns, invisible
            # to every owner-facing list until finalization copies the
            # history in, and a membership that already owns its resident
            # slot and can carry the residency the host is about to open.
            source_thread = await conn.fetchrow(
                "SELECT repo, title, setup_script, execution_profile, mcp_servers "
                "FROM agent_threads WHERE id = $1",
                source["thread_id"],
            )
            await conn.execute(
                """
                INSERT INTO agent_threads
                    (id, user_id, repo, title, title_source,
                     setup_script, execution_profile, mcp_servers)
                VALUES ($1, $2, $3, $4, 'inherited', $5, $6, $7)
                """,
                target_thread_id,
                user_id,
                source_thread["repo"],
                f"{source_thread['title']} (fork)"[:160],
                source_thread["setup_script"],
                source_thread["execution_profile"],
                list(source_thread["mcp_servers"] or []),
            )
            await conn.execute(
                """
                INSERT INTO agent_session_forks (
                    id, user_id, idempotency_key, sandbox_id,
                    sandbox_generation, container_id, runtime_id,
                    runtime_version, source_thread_id, source_job_id,
                    source_native_session_id, source_activation_generation,
                    target_thread_id, target_resident_slot_id
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8,
                    $9, $10, $11, $12, $13, $14
                )
                """,
                operation_id,
                user_id,
                idempotency_key,
                row["sandbox_id"],
                row["sandbox_generation"],
                row["container_id"],
                row["runtime_id"],
                row["runtime_version"],
                source["thread_id"],
                source_job_id,
                row["native_session_id"],
                row["activation_generation"],
                target_thread_id,
                target_slot,
            )
            await conn.execute(
                """
                INSERT INTO agent_thread_sandbox_memberships (
                    thread_id, user_id, sandbox_id, resident_slot_id,
                    forked_from_thread_id, fork_anchor_job_id, fork_operation_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                target_thread_id,
                user_id,
                row["sandbox_id"],
                target_slot,
                source["thread_id"],
                source_job_id,
                operation_id,
            )
            await conn.execute(
                """
                UPDATE agent_thread_sandbox_memberships
                SET native_state = 'fork_preparing', fork_operation_id = $2,
                    updated_at = NOW()
                WHERE thread_id = $1
                """,
                source["thread_id"],
                operation_id,
            )
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET active_operation_count = active_operation_count + 1,
                    idle_deadline = NULL, last_activity_at = NOW(), updated_at = NOW()
                WHERE id = $1
                """,
                row["sandbox_id"],
            )
            return await self._session_fork_details(conn, operation_id)

    async def establish_session_fork_residencies(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
        ttl_seconds: float,
    ) -> dict[str, Any] | None:
        """Create/recover the two independent process credentials for a fork."""
        if not coordinator_token or len(coordinator_token) > 256 or "\x00" in coordinator_token:
            raise ValueError("session fork coordinator token is invalid")
        async with self._pool.acquire() as conn, conn.transaction():
            operation = await conn.fetchrow(
                "SELECT * FROM agent_session_forks WHERE id = $1 FOR UPDATE",
                operation_id,
            )
            if (
                operation is None
                or operation["state"] != "preparing"
                or operation["coordinator_token"] != coordinator_token
            ):
                return None
            source_job = await conn.fetchrow(
                """
                SELECT user_id, model FROM agent_jobs
                WHERE id = $1 AND user_id = $2
                """,
                operation["source_job_id"],
                operation["user_id"],
            )
            if source_job is None:
                return None
            generations: dict[str, int] = {}
            previous: dict[str, str | None] = {}
            thread_ids = {
                "source": str(operation["source_thread_id"]),
                "target": str(operation["target_thread_id"]),
            }
            sandbox_host = await conn.fetchval(
                "SELECT host FROM agent_sandboxes WHERE id = $1 AND generation = $2",
                operation["sandbox_id"],
                operation["sandbox_generation"],
            )
            if sandbox_host is None:
                return None
            for role, thread_id in thread_ids.items():
                current = await conn.fetchrow(
                    """
                    SELECT residency_grant_id FROM agent_thread_sandbox_memberships
                    WHERE thread_id = $1 AND sandbox_id = $2
                    FOR UPDATE
                    """,
                    thread_id,
                    operation["sandbox_id"],
                )
                if current is None:
                    return None
                # The grant token itself is deliberately never stored.  A
                # retried HTTP fork therefore cannot safely reuse a residency
                # whose token may already be inside an unacknowledged host
                # process: minting over the same generation would relabel that
                # old-token process as current.  Rotate the generation on every
                # PREPARING attempt, revoke the prior linked grant, and make the
                # supervisor restart only these two member processes.
                previous[role] = (
                    current["residency_grant_id"] or operation[f"previous_{role}_grant_id"]
                )
                created = await conn.fetchrow(
                    """
                    UPDATE agent_thread_sandbox_memberships m
                    SET residency_generation = m.residency_generation + 1,
                        residency_status = 'hot',
                        residency_lease_expires_at = NOW() + make_interval(secs => $3),
                        residency_grant_id = NULL,
                        residency_binding_id = NULL,
                        residency_established_at = NOW(),
                        residency_last_active_at = NOW(),
                        updated_at = NOW()
                    FROM agent_sandboxes g
                    WHERE m.thread_id = $1 AND m.sandbox_id = $2
                      AND g.id = m.sandbox_id AND g.generation = $4
                    RETURNING m.residency_generation AS generation
                    """,
                    thread_id,
                    operation["sandbox_id"],
                    ttl_seconds,
                    operation["sandbox_generation"],
                )
                if created is None:
                    return None
                generations[role] = int(created["generation"])

            await conn.execute(
                """
                UPDATE agent_session_forks
                SET source_residency_generation = $2,
                    target_residency_generation = $3,
                    previous_source_grant_id = $4,
                    previous_target_grant_id = $5,
                    updated_at = NOW()
                WHERE id = $1
                """,
                operation_id,
                generations["source"],
                generations["target"],
                previous["source"],
                previous["target"],
            )
            await conn.execute(
                """
                UPDATE agent_thread_sandbox_memberships
                SET resident_state = 'restarting', updated_at = NOW()
                WHERE thread_id = $1 AND residency_generation = $2
                  AND fork_operation_id = $3
                """,
                operation["source_thread_id"],
                generations["source"],
                operation_id,
            )
            common = {
                "user_id": source_job["user_id"],
                "model": source_job["model"],
            }
            return {
                "source": {
                    **common,
                    "thread_id": thread_ids["source"],
                    "generation": generations["source"],
                    "previous_grant_id": previous["source"],
                    "establishing_attempt_id": f"fork-source:{operation_id}",
                },
                "target": {
                    **common,
                    "thread_id": thread_ids["target"],
                    "generation": generations["target"],
                    "previous_grant_id": previous["target"],
                    "establishing_attempt_id": f"fork-target:{operation_id}",
                },
            }

    async def claim_session_fork_materialization(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
        lease_seconds: float,
    ) -> bool:
        """Lease the one external native mutation for a PREPARING saga.

        Idempotency-key insertion is serialized in :meth:`prepare_session_fork`,
        but its HTTP handler necessarily leaves that transaction before minting
        credentials and calling the host. This second durable fence prevents
        two workers handling the same request from rotating both member
        residencies and invoking the native fork concurrently. An abandoned
        coordinator can be taken over after the bounded lease.
        """
        if not coordinator_token or len(coordinator_token) > 256 or "\x00" in coordinator_token:
            raise ValueError("session fork coordinator token is invalid")
        if lease_seconds <= 0:
            raise ValueError("session fork coordinator lease must be positive")
        async with self._pool.acquire() as conn:
            claimed = await conn.fetchval(
                """
                UPDATE agent_session_forks
                SET coordinator_token = $2,
                    coordinator_lease_expires_at = NOW() + make_interval(secs => $3),
                    updated_at = NOW()
                WHERE id = $1 AND state = 'preparing'
                  AND (
                    coordinator_token IS NULL
                    OR coordinator_lease_expires_at IS NULL
                    OR coordinator_lease_expires_at <= NOW()
                    OR coordinator_token = $2
                  )
                RETURNING 1
                """,
                operation_id,
                coordinator_token,
                lease_seconds,
            )
        return claimed is not None

    async def release_session_fork_materialization(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
    ) -> bool:
        """Release a coordinator that failed before any host mutation began."""
        async with self._pool.acquire() as conn:
            released = await conn.fetchval(
                """
                UPDATE agent_session_forks
                SET coordinator_token = NULL, coordinator_lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE id = $1 AND state = 'preparing' AND coordinator_token = $2
                RETURNING 1
                """,
                operation_id,
                coordinator_token,
            )
        return released is not None

    async def record_session_fork_materialized(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Persist a validated host receipt before exposing the child chat."""
        async with self._pool.acquire() as conn, conn.transaction():
            operation = await conn.fetchrow(
                "SELECT * FROM agent_session_forks WHERE id = $1 FOR UPDATE",
                operation_id,
            )
            if operation is None or operation["state"] not in {"preparing", "materialized"}:
                return None
            if (
                operation["state"] == "preparing"
                and operation["coordinator_token"] != coordinator_token
            ):
                return None
            expected = {
                "operation_id": operation_id,
                "sandbox_id": operation["sandbox_id"],
                "sandbox_generation": int(operation["sandbox_generation"]),
                "container_id": operation["container_id"],
                "runtime_id": operation["runtime_id"],
                "runtime_version": operation["runtime_version"],
                "source_thread_id": operation["source_thread_id"],
                "source_native_session_id": operation["source_native_session_id"],
                "target_thread_id": operation["target_thread_id"],
                "target_resident_slot_id": int(operation["target_resident_slot_id"]),
                "source_residency_generation": int(operation["source_residency_generation"] or 0),
                "target_residency_generation": int(operation["target_residency_generation"] or 0),
            }
            if any(receipt.get(key) != value for key, value in expected.items()):
                return None
            target_native_id = str(receipt.get("target_native_session_id") or "")
            source_process = int(receipt.get("source_process_generation") or 0)
            target_process = int(receipt.get("target_process_generation") or 0)
            if (
                not target_native_id
                or target_native_id == operation["source_native_session_id"]
                or source_process <= 0
                or target_process <= 0
            ):
                return None
            if operation["state"] == "materialized":
                prior = load_json(operation["host_receipt"])
                return (
                    await self._session_fork_details(conn, operation_id)
                    if prior == receipt
                    else None
                )
            collision = await conn.fetchval(
                """
                SELECT 1 FROM agent_thread_sandbox_memberships
                WHERE sandbox_id = $1 AND native_session_id = $2
                """,
                operation["sandbox_id"],
                target_native_id,
            )
            if collision is not None:
                return None
            await conn.execute(
                """
                UPDATE agent_session_forks
                SET target_native_session_id = $2,
                    source_process_generation = $3,
                    target_process_generation = $4,
                    host_receipt = $5::jsonb, state = 'materialized',
                    coordinator_token = NULL, coordinator_lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE id = $1
                """,
                operation_id,
                target_native_id,
                source_process,
                target_process,
                json.dumps(receipt),
            )
            await conn.execute(
                """
                UPDATE agent_thread_sandbox_memberships
                SET process_generation = $2, residency_generation = $3,
                    resident_state = 'live', updated_at = NOW()
                WHERE thread_id = $1 AND fork_operation_id = $4
                """,
                operation["source_thread_id"],
                source_process,
                operation["source_residency_generation"],
                operation_id,
            )
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET state = 'running', last_activity_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND generation = $2 AND container_id = $3
                """,
                operation["sandbox_id"],
                operation["sandbox_generation"],
                operation["container_id"],
            )
            return await self._session_fork_details(conn, operation_id)

    async def finalize_session_fork(self, *, operation_id: str) -> dict[str, Any] | None:
        """Atomically create the visible child history, membership and native edge."""
        anchor_id: str | None = None
        async with self._pool.acquire() as conn, conn.transaction():
            operation = await conn.fetchrow(
                "SELECT * FROM agent_session_forks WHERE id = $1 FOR UPDATE",
                operation_id,
            )
            if operation is None:
                return None
            if operation["state"] == "ready":
                anchor_id = str(operation["target_anchor_job_id"] or "") or None
            elif operation["state"] == "materialized":
                # Match create_follow_up's thread -> job -> membership lock
                # order. Locking the source job first here while a concurrent
                # follow-up held the thread row produced a real PostgreSQL
                # deadlock cycle (each transaction then waited for the other).
                thread = await conn.fetchrow(
                    "SELECT repo, title, setup_script, execution_profile, mcp_servers "
                    "FROM agent_threads WHERE id = $1 FOR UPDATE",
                    operation["source_thread_id"],
                )
                if thread is None:
                    return None
                source = await conn.fetchrow(
                    f"{_JOB_SELECT} WHERE j.id = $1 FOR UPDATE OF j",
                    operation["source_job_id"],
                )
                membership = await conn.fetchrow(
                    """
                    SELECT * FROM agent_thread_sandbox_memberships
                    WHERE thread_id = $1 FOR UPDATE
                    """,
                    operation["source_thread_id"],
                )
                if (
                    source is None
                    or membership is None
                    or membership["fork_operation_id"] != operation_id
                    or membership["native_session_id"] != operation["source_native_session_id"]
                    or not operation["target_native_session_id"]
                ):
                    return None
                turns = await conn.fetch(
                    f"{_JOB_SELECT} WHERE j.thread_id = $1 AND j.turn_no <= $2 ORDER BY j.turn_no",
                    operation["source_thread_id"],
                    source["turn_no"],
                )
                messages = await conn.fetch(
                    """
                    SELECT m.job_id, m.role, m.content, m.created_at
                    FROM agent_thread_messages m
                    JOIN agent_jobs j ON j.id = m.job_id
                    WHERE m.thread_id = $1 AND j.turn_no <= $2
                    ORDER BY j.turn_no,
                             CASE WHEN m.role = 'user' THEN 0 ELSE 1 END, m.id
                    """,
                    operation["source_thread_id"],
                    source["turn_no"],
                )
                copied: dict[str, str] = {}
                previous_copy: str | None = None
                for turn in turns:
                    copy_id = _new_job_id()
                    copied[str(turn["id"])] = copy_id
                    await conn.execute(
                        """
                        INSERT INTO agent_jobs (
                            id, thread_id, parent_job_id, turn_no, native_turn_no, user_id,
                            base_sha, task_prompt, runtime, model, reasoning_effort, state,
                            published_pr_url, published_commit_sha, detail, metadata,
                            fork_source_job_id, created_at, updated_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                            $13, $14, $15, $16::jsonb, $17,
                            clock_timestamp(), clock_timestamp()
                        )
                        """,
                        copy_id,
                        operation["target_thread_id"],
                        previous_copy,
                        turn["turn_no"],
                        turn["native_turn_no"],
                        operation["user_id"],
                        turn["base_sha"],
                        turn["task_prompt"],
                        turn["runtime"],
                        turn["model"],
                        turn["reasoning_effort"],
                        turn["state"] if turn["state"] in TERMINAL_STATES else CANCELLED,
                        turn["published_pr_url"],
                        turn["published_commit_sha"],
                        turn["detail"],
                        json.dumps(load_json(turn["metadata"]))
                        if turn["metadata"] is not None
                        else None,
                        turn["id"],
                    )
                    previous_copy = copy_id
                await conn.executemany(
                    """
                    INSERT INTO agent_thread_messages
                        (thread_id, job_id, role, content, created_at)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    [
                        (
                            operation["target_thread_id"],
                            copied[str(message["job_id"])],
                            message["role"],
                            message["content"],
                            message["created_at"],
                        )
                        for message in messages
                    ],
                )
                anchor_id = copied[str(source["id"])]
                # Preparation made the member; finalization makes it live.
                await conn.execute(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET native_session_id = $3, native_state = 'ready',
                        context_status = 'live', process_generation = $4,
                        residency_generation = $5, resident_state = 'live',
                        resident_slot_id = $6, last_settled_job_id = $7,
                        fork_operation_id = NULL, last_error = NULL,
                        updated_at = NOW()
                    WHERE thread_id = $1 AND sandbox_id = $2 AND fork_operation_id = $8
                    """,
                    operation["target_thread_id"],
                    operation["sandbox_id"],
                    operation["target_native_session_id"],
                    operation["target_process_generation"],
                    operation["target_residency_generation"],
                    operation["target_resident_slot_id"],
                    anchor_id,
                    operation_id,
                )
                await conn.execute(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET native_state = 'ready', fork_operation_id = NULL,
                        process_generation = $2, residency_generation = $3,
                        resident_state = 'live', updated_at = NOW()
                    WHERE thread_id = $1 AND fork_operation_id = $4
                    """,
                    operation["source_thread_id"],
                    operation["source_process_generation"],
                    operation["source_residency_generation"],
                    operation_id,
                )
                await conn.execute(
                    """
                    INSERT INTO agent_native_session_edges (
                        operation_id, sandbox_id, source_thread_id,
                        target_thread_id, source_native_session_id,
                        target_native_session_id
                    ) VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    operation_id,
                    operation["sandbox_id"],
                    operation["source_thread_id"],
                    operation["target_thread_id"],
                    operation["source_native_session_id"],
                    operation["target_native_session_id"],
                )
                await conn.execute(
                    """
                    UPDATE agent_session_forks
                    SET target_anchor_job_id = $2, state = 'ready',
                        completed_at = NOW(), updated_at = NOW()
                    WHERE id = $1
                    """,
                    operation_id,
                    anchor_id,
                )
                await conn.execute(
                    """
                    UPDATE agent_sandboxes g
                    SET active_operation_count = GREATEST(active_operation_count - 1, 0),
                        last_activity_at = NOW(),
                        idle_deadline = CASE
                            WHEN active_operation_count = 1
                             AND NOT EXISTS (
                                SELECT 1 FROM agent_thread_sandbox_memberships m
                                WHERE m.sandbox_id = g.id
                                  AND m.active_attempt_id IS NOT NULL
                             )
                            THEN NOW() + make_interval(secs => idle_timeout_seconds)
                            ELSE NULL
                        END,
                        updated_at = NOW()
                    WHERE g.id = $1
                    """,
                    operation["sandbox_id"],
                )
            else:
                return None
        return None if anchor_id is None else await self.get_job(anchor_id)

    async def reap_stalled_session_forks(
        self, *, stall_seconds: float = 900.0
    ) -> list[dict[str, Any]]:
        """Recover durable native-fork receipts or fail an ambiguous attempt.

        ``materialized`` has a validated host receipt and is safe to finalize.
        A stale ``preparing`` operation has no receipt the control plane can
        trust, so it is failed without exposing its hidden target. Its exact
        source/target residency generations are lapsed for member-scoped host
        cleanup and grant revocation; siblings are never touched.
        """
        actions: list[dict[str, Any]] = []
        async with self._pool.acquire() as conn, conn.transaction():
            candidates = await conn.fetch(
                """
                SELECT id FROM agent_session_forks
                WHERE state IN ('preparing', 'materialized')
                  AND updated_at < NOW() - make_interval(secs => $1)
                ORDER BY id
                """,
                stall_seconds,
            )
            for candidate in candidates:
                operation = await conn.fetchrow(
                    "SELECT * FROM agent_session_forks WHERE id = $1 FOR UPDATE SKIP LOCKED",
                    candidate["id"],
                )
                if operation is None:
                    continue
                if operation["state"] == "materialized":
                    item = dict(operation)
                    item["recovery_action"] = "finalize"
                    actions.append(item)
                    continue
                if operation["state"] != "preparing":
                    continue
                changed = await conn.fetchrow(
                    """
                    UPDATE agent_session_forks
                    SET state = 'failed',
                        last_error = 'native fork coordinator stopped before a receipt was recorded',
                        coordinator_token = NULL,
                        coordinator_lease_expires_at = NULL,
                        updated_at = NOW(), completed_at = NOW()
                    WHERE id = $1 AND state = 'preparing'
                      AND updated_at < NOW() - make_interval(secs => $2)
                    RETURNING *
                    """,
                    operation["id"],
                    stall_seconds,
                )
                if changed is None:
                    continue
                await conn.execute(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET native_state = CASE
                            WHEN context_status = 'lost' THEN 'lost' ELSE 'ready' END,
                        fork_operation_id = NULL,
                        resident_state = CASE
                            WHEN context_status = 'lost' THEN 'lost' ELSE 'restarting' END,
                        last_error = $3, updated_at = NOW()
                    WHERE thread_id = $1 AND fork_operation_id = $2
                    """,
                    operation["source_thread_id"],
                    operation["id"],
                    changed["last_error"],
                )
                await conn.execute(
                    """
                    UPDATE agent_sandboxes g
                    SET active_operation_count = GREATEST(active_operation_count - 1, 0),
                        idle_deadline = CASE
                            WHEN active_operation_count = 1
                             AND NOT EXISTS (
                                SELECT 1 FROM agent_thread_sandbox_memberships m
                                WHERE m.sandbox_id = g.id
                                  AND m.active_attempt_id IS NOT NULL
                             )
                            THEN NOW() + make_interval(secs => idle_timeout_seconds)
                            ELSE NULL
                        END,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    operation["sandbox_id"],
                )
                cleanup: list[dict[str, Any]] = []
                sandbox_host = await conn.fetchval(
                    "SELECT host FROM agent_sandboxes WHERE id = $1",
                    operation["sandbox_id"],
                )
                for role, thread_column, generation_column in (
                    (
                        "source",
                        "source_thread_id",
                        "source_residency_generation",
                    ),
                    (
                        "target",
                        "target_thread_id",
                        "target_residency_generation",
                    ),
                ):
                    generation = operation[generation_column]
                    if generation is None:
                        continue
                    residency = await conn.fetchrow(
                        """
                        UPDATE agent_thread_sandbox_memberships m
                        SET residency_status = 'none', residency_last_active_at = NOW()
                        FROM agent_sandboxes g
                        WHERE m.thread_id = $1 AND m.residency_generation = $2
                          AND g.id = m.sandbox_id AND g.host = $3
                          AND m.residency_status <> 'none'
                        RETURNING m.thread_id, m.residency_generation AS generation,
                                  m.residency_grant_id AS grant_id
                        """,
                        operation[thread_column],
                        generation,
                        sandbox_host,
                    )
                    if residency is not None:
                        member = dict(residency)
                        member["role"] = role
                        cleanup.append(member)
                # The target existed only as the placeholder preparation made.
                # No turn ever landed in it, so it is tombstoned the way an
                # owner's delete tombstones a chat, and its resident slot is
                # given back to the group.
                await conn.execute(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET resident_slot_id = NULL, native_state = 'lost',
                        context_status = 'lost', resident_state = 'lost',
                        fork_operation_id = NULL, last_error = $3, updated_at = NOW()
                    WHERE thread_id = $1 AND fork_operation_id = $2
                    """,
                    operation["target_thread_id"],
                    operation["id"],
                    changed["last_error"],
                )
                await conn.execute(
                    """
                    UPDATE agent_threads
                    SET deleted_at = NOW(), updated_at = NOW()
                    WHERE id = $1 AND deleted_at IS NULL
                      AND NOT EXISTS (SELECT 1 FROM agent_jobs WHERE thread_id = $1)
                    """,
                    operation["target_thread_id"],
                )
                item = dict(changed)
                item["recovery_action"] = "cleanup"
                item["cleanup_residencies"] = cleanup
                actions.append(item)
        return actions

    async def get_thread_for_job(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Return the conversation containing an owned job."""
        async with self._pool.acquire() as conn:
            thread = await conn.fetchrow(
                """
                SELECT t.id, t.repo, t.title, t.created_at, t.updated_at,
                       t.pinned_at, t.archived_at
                FROM agent_threads t
                JOIN agent_jobs j ON j.thread_id = t.id
                WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL
                """,
                job_id,
                user_id,
            )
            if thread is None:
                return None
            messages = await conn.fetch(
                """
                SELECT m.id, m.role, m.content, m.job_id, m.created_at
                FROM agent_thread_messages m
                JOIN agent_jobs j ON j.id = m.job_id
                WHERE m.thread_id = $1
                ORDER BY j.turn_no, CASE WHEN m.role = 'user' THEN 0 ELSE 1 END, m.id
                """,
                thread["id"],
            )
            jobs = await conn.fetch(
                f"{_JOB_SELECT} WHERE j.thread_id = $1 ORDER BY j.turn_no",
                thread["id"],
            )
            sandbox = await conn.fetchrow(
                f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                thread["id"],
            )
            job_rows = [_job_row_to_dict(job) for job in jobs]
            for job in job_rows:
                job["title"] = thread["title"]
                job["pinned_at"] = thread["pinned_at"]
                job["archived_at"] = thread["archived_at"]
                if sandbox is not None:
                    job["thread_sandbox"] = _sandbox_row_to_dict(sandbox)
        return {
            "id": thread["id"],
            "repo": thread["repo"],
            "title": thread["title"],
            "created_at": thread["created_at"],
            "updated_at": thread["updated_at"],
            "archived_at": thread["archived_at"],
            "messages": [dict(message) for message in messages],
            "jobs": job_rows,
        }

    async def list_thread_events_after(
        self,
        *,
        job_id: str,
        user_id: str,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]] | None:
        """Page a whole thread's events, payloads intact.

        The per-job read beside this one is scoped to the turn being watched.
        Without this thread read, history keeps only durable messages, so
        everything an earlier turn *did* becomes invisible even though every
        row remains stored.

        Payloads are intentionally preserved: the caller needs tool inputs and
        outputs to reconstruct the historical activity. The result is paged so
        a long thread is not pulled into memory in one response.

        :returns: ``None`` when the job does not exist or is not the caller's,
            matching :meth:`get_thread_for_job`.
        """
        async with self._pool.acquire() as conn:
            thread_id = await conn.fetchval(
                "SELECT j.thread_id FROM agent_jobs j "
                "JOIN agent_threads t ON t.id = j.thread_id "
                "WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL",
                job_id,
                user_id,
            )
            if thread_id is None:
                return None
            rows = await conn.fetch(
                """
                SELECT e.id,
                       e.job_id,
                       j.turn_no,
                       e.attempt_id,
                       e.seq,
                       e.event_type,
                       e.payload,
                       e.created_at
                FROM agent_job_events e
                JOIN agent_jobs j ON j.id = e.job_id
                WHERE j.thread_id = $1 AND e.id > $2
                ORDER BY e.id
                LIMIT $3
                """,
                thread_id,
                after_id,
                limit,
            )
        return [{**dict(row), "payload": load_json(row["payload"])} for row in rows]

    async def thread_stats_inputs(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Return everything needed to summarize the thread containing a job.

        The event rows come back with their bulky text fields stripped in
        SQL: the summary reads identity, pairing and usage keys only, and a
        long thread's transcript is megabytes we would otherwise pull across
        the wire to throw away. Nothing is filtered by event type — wall
        time is measured from a turn's first to last event, so a Tier-2
        thread whose only events are ``raw`` still gets its duration.

        :returns: ``None`` when the job does not exist or is not the
            caller's, matching :meth:`get_thread_for_job`.
        """
        async with self._pool.acquire() as conn:
            thread_id = await conn.fetchval(
                "SELECT j.thread_id FROM agent_jobs j "
                "JOIN agent_threads t ON t.id = j.thread_id "
                "WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL",
                job_id,
                user_id,
            )
            if thread_id is None:
                return None
            jobs = await conn.fetch(
                "SELECT id FROM agent_jobs WHERE thread_id = $1 ORDER BY turn_no",
                thread_id,
            )
            attempts = await conn.fetch(
                """
                SELECT a.job_id, a.started_at
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE j.thread_id = $1
                """,
                thread_id,
            )
            events = await conn.fetch(
                """
                SELECT e.job_id,
                       e.attempt_id,
                       e.seq,
                       e.event_type,
                       e.created_at,
                       (e.payload - ARRAY['text', 'input', 'content'])
                       || jsonb_build_object(
                           'has_text', COALESCE(e.payload->>'text', '') ~ '[^[:space:]]'
                       ) AS payload
                FROM agent_job_events e
                JOIN agent_jobs j ON j.id = e.job_id
                WHERE j.thread_id = $1
                ORDER BY e.attempt_id, e.seq
                """,
                thread_id,
            )
        return {
            "thread_id": thread_id,
            "job_ids": [row["id"] for row in jobs],
            "attempts": [dict(row) for row in attempts],
            "events": [
                {
                    "job_id": row["job_id"],
                    "attempt_id": row["attempt_id"],
                    "seq": row["seq"],
                    "event_type": row["event_type"],
                    "created_at": row["created_at"],
                    "payload": load_json(row["payload"]),
                }
                for row in events
            ],
        }

    async def job_usage_inputs(self, *, job_id: str) -> dict[str, Any]:
        """Read runtime usage for this job's attempts, excluding sibling turns.

        Keep the same compact payload projection as thread_stats_inputs so
        output-presence detection and retry deduplication have identical
        semantics when compared with this job's Gateway snapshots.
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            attempts = await conn.fetch(
                "SELECT job_id, started_at FROM agent_attempts WHERE job_id = $1",
                job_id,
            )
            events = await conn.fetch(
                """
                SELECT job_id, attempt_id, seq, event_type, created_at,
                       (payload - ARRAY['text', 'input', 'content'])
                       || jsonb_build_object(
                           'has_text', COALESCE(payload->>'text', '') ~ '[^[:space:]]'
                       ) AS payload
                FROM agent_job_events
                WHERE job_id = $1
                ORDER BY attempt_id, seq
                """,
                job_id,
            )
        return {
            "job_ids": [job_id],
            "attempts": [dict(row) for row in attempts],
            "events": [{**dict(row), "payload": load_json(row["payload"])} for row in events],
        }

    async def ledger_rows_for_job(self, *, job_id: str) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._ledger.ledger_rows_for_job(job_id=job_id)

    async def ledger_rows_for_conversation(
        self, *, job_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._ledger.ledger_rows_for_conversation(job_id=job_id, user_id=user_id)

    async def queue_job_ledger_refresh(self, *, job_id: str) -> int:
        """Delegates to the repository that implements this capability."""
        return await self._ledger.queue_job_ledger_refresh(job_id=job_id)

    async def claim_due_ledger_refresh(self, *, lease_seconds: float = 60) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._ledger.claim_due_ledger_refresh(lease_seconds=lease_seconds)

    async def complete_ledger_refresh(
        self,
        *,
        ledger_id: int,
        refresh_generation: int,
        terminal: bool,
        report: dict[str, Any] | None,
        error_code: str | None,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._ledger.complete_ledger_refresh(
            ledger_id=ledger_id,
            refresh_generation=refresh_generation,
            terminal=terminal,
            report=report,
            error_code=error_code,
        )

    async def resolve_model_credential(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._credential_queries.resolve_model_credential(
            job_id=job_id, attempt_id=attempt_id, lease_generation=lease_generation
        )

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Fetch one job by id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_QUALIFIED_JOB_COLUMNS}, t.title, t.title_source, t.pinned_at, t.archived_at, t.deleted_at,
                       to_jsonb(s) AS thread_sandbox
                FROM agent_jobs j
                LEFT JOIN agent_threads t ON t.id = j.thread_id
                LEFT JOIN {THREAD_SANDBOX_SOURCE} ON s.thread_id = j.thread_id
                WHERE j.id = $1
                """,
                job_id,
            )
        return _job_row_to_dict(row) if row else None

    async def list_jobs(
        self,
        *,
        user_id: str,
        limit: int = 50,
        archived: bool = False,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """List a user's jobs with pinned threads first, then newest first.

        ``repo`` narrows the page to one project, which is how the sidebar
        pages a single project past the global newest-first window.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_QUALIFIED_JOB_COLUMNS}, t.title, t.title_source, t.pinned_at, t.archived_at
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = $1
                WHERE j.user_id = $1
                  AND t.deleted_at IS NULL
                  AND ($4::text IS NULL OR t.repo = $4)
                  AND (($3 AND t.archived_at IS NOT NULL)
                       OR (NOT $3 AND t.archived_at IS NULL))
                ORDER BY t.pinned_at DESC NULLS LAST, j.created_at DESC
                LIMIT $2
                """,
                user_id,
                limit,
                archived,
                repo,
            )
        return [_job_row_to_dict(row) for row in rows]

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
    ) -> dict[str, Any]:
        """Page complete owned conversations, searching their canonical title.

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
        q = q.strip()
        if not 1 <= limit <= 200 or len(q) > 200 or "\x00" in q:
            raise ValueError("Invalid conversation search parameters")
        scope_filters = [user_id, q, repo, include_archived]
        # Preserve issued cursors for searches that do not filter pin state.
        if pinned is not None:
            scope_filters.append(pinned)
        scope = hashlib.sha256(json.dumps(scope_filters).encode()).hexdigest()
        boundary = (
            _decode_conversation_cursor(cursor, scope)
            if cursor is not None
            else (None, None, None, None)
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                WITH owned AS (
                    SELECT t.id AS thread_id, t.title, t.pinned_at,
                           t.archived_at IS NOT NULL AS archived,
                           GREATEST(t.updated_at, freshest.updated_at) AS last_activity_at
                    FROM agent_threads t
                    CROSS JOIN LATERAL (
                        SELECT j.updated_at
                        FROM agent_jobs j
                        WHERE j.thread_id = t.id AND j.user_id = $1
                        ORDER BY j.updated_at DESC
                        LIMIT 1
                    ) freshest
                    WHERE t.user_id = $1 AND t.deleted_at IS NULL
                      AND ($2::text IS NULL OR t.repo = $2)
                      AND ($3::text = '' OR POSITION(LOWER($3) IN LOWER(t.title)) > 0
                           OR POSITION(LOWER($3) IN LOWER(t.repo)) > 0)
                      AND ($4::boolean OR t.archived_at IS NULL)
                      AND ($10::boolean IS NULL OR (t.pinned_at IS NOT NULL) = $10)
                ), page AS (
                    SELECT * FROM owned
                    WHERE ($6::boolean IS NULL OR (
                          pinned_at IS NOT NULL,
                          COALESCE(pinned_at, '-infinity'::timestamptz),
                          last_activity_at, thread_id
                      ) < (
                          $6, COALESCE($7::timestamptz, '-infinity'::timestamptz),
                          $8::timestamptz, $9::text
                      ))
                    ORDER BY pinned_at DESC NULLS LAST, last_activity_at DESC, thread_id DESC
                    LIMIT $5
                )
                SELECT {_QUALIFIED_JOB_COLUMNS}, page.title, page.pinned_at,
                       page.archived, page.last_activity_at
                FROM page
                JOIN agent_threads t ON t.id = page.thread_id
                JOIN LATERAL (
                    SELECT * FROM agent_jobs latest
                    WHERE latest.thread_id = page.thread_id AND latest.user_id = $1
                    ORDER BY latest.turn_no DESC
                    LIMIT 1
                ) j ON TRUE
                ORDER BY page.pinned_at DESC NULLS LAST,
                         page.last_activity_at DESC, page.thread_id DESC
                """,
                user_id,
                repo,
                q,
                include_archived,
                limit + 1,
                *boundary,
                pinned,
            )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "v": 1,
                        "scope": scope,
                        "pin": last["pinned_at"].isoformat() if last["pinned_at"] else None,
                        "activity": last["last_activity_at"].isoformat(),
                        "thread": last["thread_id"],
                    },
                    separators=(",", ":"),
                ).encode()
            ).decode()
        return {
            "conversations": [
                {
                    "job": _job_row_to_dict(row),
                    "title": row["title"],
                    "archived": row["archived"],
                    "last_activity_at": row["last_activity_at"],
                }
                for row in rows[:limit]
            ],
            "next_cursor": next_cursor,
        }

    async def list_projects(self, *, user_id: str, archived: bool = False) -> list[dict[str, Any]]:
        """Summarize a user's repos, most recently active first.

        The sidebar groups tasks by project, so it needs every repo the user
        has ever run in — not just those represented in the newest-first job
        page, which would silently drop dormant projects from the tree.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT t.repo,
                       COUNT(DISTINCT j.thread_id) AS task_count,
                       COUNT(*) FILTER (
                           WHERE j.state IN ('queued', 'running')
                       ) AS active_count,
                       MAX(GREATEST(t.updated_at, j.updated_at)) AS last_activity_at,
                       COUNT(DISTINCT j.thread_id) FILTER (
                           WHERE t.pinned_at IS NOT NULL
                       ) AS pinned_count,
                       MAX(t.pinned_at) AS pinned_at
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = $1
                WHERE j.user_id = $1
                  AND t.deleted_at IS NULL
                  AND (($2 AND t.archived_at IS NOT NULL)
                       OR (NOT $2 AND t.archived_at IS NULL))
                GROUP BY t.repo
                ORDER BY pinned_at DESC NULLS LAST, last_activity_at DESC
                """,
                user_id,
                archived,
            )
        return [
            {
                "repo": row["repo"],
                "task_count": int(row["task_count"]),
                "active_count": int(row["active_count"]),
                "last_activity_at": row["last_activity_at"],
                "pinned_count": int(row["pinned_count"]),
                "pinned_at": row["pinned_at"],
            }
            for row in rows
        ]

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
    ) -> dict[str, Any]:
        """Page complete conversations independently of their number of turns.

        Project facets apply search/state/archive filters before the selected
        repo. The page and its counts share a database snapshot; the opaque
        keyset remains valid when earlier rows are unarchived or deleted.
        Row-only requests skip the full-history aggregate and return null counts.
        """
        scope = _session_cursor_scope(user_id, archived, search, repo, state)
        after_at, after_id = _decode_session_cursor(cursor, scope)
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            projects = (
                await conn.fetch(
                    _session_query()
                    + "SELECT repo, COUNT(*) AS count FROM matched GROUP BY repo ORDER BY repo",
                    user_id,
                    archived,
                    search,
                    state,
                )
                if include_summary
                else None
            )
            rows = await conn.fetch(
                _session_query(filter_repo=True)
                + f"""
                SELECT t.*, {_SESSION_DELETE_BLOCK_SQL} AS delete_block_reason
                FROM matched t
                WHERE ($6::timestamptz IS NULL OR (t.sort_at, t.session_id) < ($6, $7::text))
                ORDER BY t.sort_at DESC, t.session_id DESC
                LIMIT $8
                """,
                user_id,
                archived,
                search,
                state,
                repo,
                after_at,
                after_id,
                limit + 1,
            )
        page = rows[:limit]
        return {
            "sessions": [
                {
                    "thread_id": row["session_id"],
                    "title": row["session_title"],
                    "archived_at": row["archived_at"],
                    "job": _job_row_to_dict(row),
                    "can_delete": row["delete_block_reason"] is None,
                    "delete_block_reason": row["delete_block_reason"],
                }
                for row in page
            ],
            "total": (
                sum(int(item["count"]) for item in projects if repo is None or item["repo"] == repo)
                if projects is not None
                else None
            ),
            "projects": (
                [{"repo": item["repo"], "count": int(item["count"])} for item in projects]
                if projects is not None
                else None
            ),
            "next_cursor": (
                _encode_session_cursor(page[-1]["sort_at"], page[-1]["session_id"], scope)
                if len(rows) > limit
                else None
            ),
        }

    async def set_sessions_archived(
        self, *, thread_ids: list[str], user_id: str, archived: bool
    ) -> dict[str, Any]:
        """Set every owned selected thread's archive state, or change none."""
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT id FROM agent_threads "
                "WHERE id = ANY($1::text[]) AND user_id = $2 AND deleted_at IS NULL "
                "ORDER BY id FOR UPDATE",
                thread_ids,
                user_id,
            )
            if len(rows) != len(thread_ids):
                return {"error": "not_found"}
            updated = await conn.fetch(
                """
                UPDATE agent_threads
                SET archived_at = CASE WHEN $3 THEN COALESCE(archived_at, NOW()) ELSE NULL END
                WHERE id = ANY($1::text[]) AND user_id = $2
                RETURNING id AS thread_id, archived_at
                """,
                thread_ids,
                user_id,
                archived,
            )
        return {"sessions": [dict(row) for row in updated]}

    async def delete_sessions(self, *, thread_ids: list[str], user_id: str) -> dict[str, Any]:
        """Irreversibly hide settled archived history while retaining audit rows.

        The thread lock serializes owner deletion against follow-up insertion,
        native-fork preparation and archive changes. Worker lifecycle access
        deliberately keeps the immutable job, residency and sandbox ledgers.
        A fork's shared workspace is never removed by a chat-history action.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT id FROM agent_threads "
                "WHERE id = ANY($1::text[]) AND user_id = $2 AND deleted_at IS NULL "
                "ORDER BY id FOR UPDATE",
                thread_ids,
                user_id,
            )
            if len(rows) != len(thread_ids):
                return {"error": "not_found"}
            blocked = await conn.fetchrow(
                f"""
                WITH selected AS (
                    SELECT id AS session_id, archived_at FROM agent_threads
                    WHERE id = ANY($1::text[]) AND user_id = $2
                ), checked AS (
                    SELECT t.session_id, {_SESSION_DELETE_BLOCK_SQL} AS reason
                    FROM selected t
                )
                SELECT reason FROM checked WHERE reason IS NOT NULL ORDER BY session_id LIMIT 1
                """,
                thread_ids,
                user_id,
            )
            if blocked is not None:
                return {"error": blocked["reason"]}
            await conn.execute(
                "UPDATE agent_threads SET deleted_at = NOW() "
                "WHERE id = ANY($1::text[]) AND user_id = $2",
                thread_ids,
                user_id,
            )
        return {"thread_ids": thread_ids}

    async def set_thread_archived(
        self, *, job_id: str, user_id: str, archived: bool
    ) -> dict[str, Any] | None:
        """Archive or restore the owned thread containing ``job_id``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_threads t
                SET archived_at = CASE
                    WHEN $3 THEN COALESCE(t.archived_at, NOW())
                    ELSE NULL
                END
                WHERE t.user_id = $2
                  AND t.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM agent_jobs j
                      WHERE j.id = $1
                        AND j.thread_id = t.id
                        AND j.user_id = $2
                  )
                RETURNING t.id AS thread_id, t.archived_at
                """,
                job_id,
                user_id,
                archived,
            )
        if row is None:
            return None
        return {"thread_id": row["thread_id"], "archived_at": row["archived_at"]}

    async def set_thread_title(
        self, *, job_id: str, user_id: str, title: str
    ) -> dict[str, Any] | None:
        """Rename the owned conversation without changing any turn's prompt."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_threads t
                SET title = $3, title_source = 'manual'
                WHERE t.user_id = $2
                  AND t.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM agent_jobs j
                      WHERE j.id = $1 AND j.thread_id = t.id AND j.user_id = $2
                  )
                RETURNING t.id AS thread_id, t.title
                """,
                job_id,
                user_id,
                title,
            )
        return dict(row) if row is not None else None

    async def set_generated_thread_title(
        self, *, job_id: str, attempt_id: int, lease_generation: int, title: str
    ) -> bool:
        """Save a first-turn title while its running/successful attempt remains current."""
        async with self._pool.acquire() as conn, conn.transaction():
            # Match follow-up/fork lock ordering: thread before job. Locking the
            # source also makes a simultaneous manual rename win in either order.
            thread_id = await conn.fetchval(
                """
                SELECT t.id FROM agent_threads t
                JOIN agent_jobs j ON j.thread_id = t.id
                WHERE j.id = $1 AND j.turn_no = 1
                  AND t.title_source = 'fallback' AND t.deleted_at IS NULL
                FOR UPDATE OF t
                """,
                job_id,
            )
            if thread_id is None:
                return False
            live = await conn.fetchval(
                """
                SELECT a.id FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE j.id = $1 AND j.current_attempt_id = a.id
                  AND a.id = $2 AND a.lease_generation = $3 AND a.attempt_no = 1
                  AND (
                      (j.state = 'running' AND a.status = 'running'
                       AND a.lease_expires_at > clock_timestamp())
                      OR (j.state = 'succeeded' AND a.status = 'finished')
                  )
                FOR SHARE OF a, j
                """,
                job_id,
                attempt_id,
                lease_generation,
            )
            if live is None:
                return False
            updated = await conn.fetchval(
                """
                UPDATE agent_threads SET title = $2, title_source = 'generated'
                WHERE id = $1 AND title_source = 'fallback' AND deleted_at IS NULL
                RETURNING id
                """,
                thread_id,
                title,
            )
            return updated is not None

    async def set_thread_pinned(
        self, *, job_id: str, user_id: str, pinned: bool
    ) -> dict[str, Any] | None:
        """Pin or unpin the owned thread containing ``job_id``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_threads t
                SET pinned_at = CASE
                    WHEN $3 THEN COALESCE(t.pinned_at, NOW())
                    ELSE NULL
                END
                WHERE t.user_id = $2
                  AND t.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM agent_jobs j
                      WHERE j.id = $1
                        AND j.thread_id = t.id
                        AND j.user_id = $2
                  )
                RETURNING t.id AS thread_id, t.pinned_at
                """,
                job_id,
                user_id,
                pinned,
            )
        if row is None:
            return None
        return {"thread_id": row["thread_id"], "pinned_at": row["pinned_at"]}

    # ── Runner hosts ───────────────────────────────────────────────────
    #
    # Hosts enter the pool only through one-time enrollment and authenticated
    # heartbeats. Claims pull work, but never create or rename host rows.

    async def active_runner_host(self) -> str | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.active_runner_host()

    async def runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], datetime]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.runner_pool()

    async def deployment_runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], set[str]]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.deployment_runner_pool()

    async def list_runner_hosts(self) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.list_runner_hosts()

    async def set_runtime_version(self, *, runtime: str, version: str | None) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.set_runtime_version(runtime=runtime, version=version)

    async def set_active_runner_host(self, *, host: str | None) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.set_active_runner_host(host=host)

    async def forget_runner_host(self, *, host: str) -> str:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.forget_runner_host(host=host)

    # ── Host pool: enrollment, heartbeat, capacity ─────────────────────
    #
    # The methods above are #1158's: a host appears by polling, and an operator
    # picks one. These add the half that lets a pool be *managed* — a machine
    # is enrolled with a credential, promises to heartbeat, declares how much
    # it can take and what it can run, and is drained or revoked by name.
    #
    # The two halves share one table and compose rather than compete: the pin
    # decides which host may materialize a new chat, retained groups select
    # their immutable owner, and the pool decides whether either host is in a
    # claimable state.
    #
    # Lock order everywhere below: policy row, then job rows, then host rows —
    # the order #1167 established, extended by one table. Nothing here takes a
    # host row and then reaches back for a job or the policy.

    async def issue_enroll_token(
        self, *, token_hash: str, host: str, ttl_seconds: float, issued_by: str | None = None
    ) -> dict[str, Any]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.issue_enroll_token(
            token_hash=token_hash, host=host, ttl_seconds=ttl_seconds, issued_by=issued_by
        )

    async def consume_enroll_token(self, *, token_hash: str, host: str) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.consume_enroll_token(token_hash=token_hash, host=host)

    async def get_host(self, *, host: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.get_host(host=host)

    async def register_host(
        self,
        *,
        host: str,
        credential_hash: str,
        capabilities: list[str] | None = None,
        slots_total: int | None = None,
        broker_url: str | None = None,
        runner_version: str | None = None,
        labels: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.register_host(
            host=host,
            credential_hash=credential_hash,
            capabilities=capabilities,
            slots_total=slots_total,
            broker_url=broker_url,
            runner_version=runner_version,
            labels=labels,
        )

    async def authenticate_host(self, *, host: str, credential_hash: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.authenticate_host(host=host, credential_hash=credential_hash)

    async def record_host_heartbeat(
        self,
        *,
        host: str,
        credential_hash: str | None = None,
        capacity: Mapping[str, Any] | None = None,
        capacity_sample_age_ms: int | None = None,
        physical_host: str | None = None,
        slots_total: int | None = None,
        runner_version: str | None = None,
        broker_url: str | None = None,
        capabilities: list[str] | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: dict[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.record_host_heartbeat(
            host=host,
            credential_hash=credential_hash,
            capacity=capacity,
            capacity_sample_age_ms=capacity_sample_age_ms,
            physical_host=physical_host,
            slots_total=slots_total,
            runner_version=runner_version,
            broker_url=broker_url,
            capabilities=capabilities,
            sandbox_runtime=sandbox_runtime,
            sandbox_image_digest=sandbox_image_digest,
            runtime_versions=runtime_versions,
            runtime_versions_available=runtime_versions_available,
        )

    async def list_capacity_history_candidates(
        self, *, after_host: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._host_capacity.list_capacity_history_candidates(
            after_host=after_host, limit=limit
        )

    async def record_capacity_history_point(
        self,
        *,
        host: str,
        capacity_epoch: Any,
        point: CapacityHistoryPointWrite,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._host_capacity.record_capacity_history_point(
            host=host, capacity_epoch=capacity_epoch, point=point
        )

    async def prune_capacity_history(
        self, *, older_than_seconds: float = CAPACITY_HISTORY_RETENTION_S, limit: int = 2000
    ) -> int:
        """Delegates to the repository that implements this capability."""
        return await self._host_capacity.prune_capacity_history(
            older_than_seconds=older_than_seconds, limit=limit
        )

    async def host_capacity_history(
        self, *, host: str, range_seconds: int
    ) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_capacity.host_capacity_history(
            host=host, range_seconds=range_seconds
        )

    async def set_host_status(self, *, host: str, status: str) -> str:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.set_host_status(host=host, status=status)

    async def remove_host(self, *, host: str, force: bool = False) -> str:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.remove_host(host=host, force=force)

    async def reap_offline_hosts(
        self,
        *,
        interval_seconds: float,
        missed_intervals: int,
    ) -> list[str]:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.reap_offline_hosts(
            interval_seconds=interval_seconds, missed_intervals=missed_intervals
        )

    async def host_for_attempt(self, *, attempt_id: int) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.host_for_attempt(attempt_id=attempt_id)

    async def host_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        """Delegates to the repository that implements this capability."""
        return await self._host_pool.host_for_job(job_id=job_id)

    @staticmethod
    async def _release_host_slot(conn: Any, host: str | None) -> None:
        """Give one slot back, and never take the count below zero.

        Called from inside the transaction that just moved an attempt out of
        ``running``, and only when that move actually changed a row — so it
        runs exactly once per attempt. The ``slots_used > 0`` predicate is the
        second line of defence and the schema CHECK is the third: a counter
        that drifts negative would make a host look permanently over-committed
        and quietly stop it claiming, with nothing in the logs.
        """
        if host is None:
            return
        await conn.execute(
            "UPDATE agent_runner_hosts SET slots_used = slots_used - 1 "
            "WHERE host = $1 AND slots_used > 0",
            host,
        )

    # ── Claim / lease ──────────────────────────────────────────────────

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
        attestation = claim_attestation or {}
        sandbox_runtime = attestation.get("sandbox_runtime")
        sandbox_image_digest = attestation.get("sandbox_image_digest")
        runtime_versions = attestation.get("runtime_versions")
        if not isinstance(runtime_versions, dict):
            runtime_versions = {}
        # The menu, from the same place and for the same reason as the default
        # beside it: the attestation is *this replica's* image, and the
        # heartbeat row is not evidence about it during a rolling deployment.
        # A selection is only honoured when the machine about to run it says it
        # can.
        runtime_versions_available = attestation.get("runtime_versions_available")
        if not isinstance(runtime_versions_available, dict):
            runtime_versions_available = {}
        host_kernel = attestation.get("host_kernel")
        guest_kernel = attestation.get("guest_kernel")
        shared_attestation = shared_sandbox_claim_attested(
            attestation,
            expected_deployment_sha=expected_deployment_sha,
        )
        followup_attestation = compatible_shared_sandbox_followup_attested(attestation)
        if not host or not host_credential_hash or not followup_attestation:
            return None
        async with self._pool.acquire() as conn, conn.transaction():
            policy = await conn.fetchrow(
                "SELECT active_host, runtime_versions FROM agent_runner_policy WHERE id FOR SHARE"
            )
            active_host = None if policy is None else policy["active_host"]
            selected_versions = (
                {} if policy is None else load_json(policy["runtime_versions"]) or {}
            )
            pin_allows_new_sandbox = active_host is None or host == active_host
            host_row = await conn.fetchrow(
                "SELECT status, capabilities, slots_total, slots_used, "
                "(credential_hash IS NOT NULL AND credential_hash = $2) "
                "AS credential_matches "
                "FROM agent_runner_hosts WHERE host = $1",
                host,
                host_credential_hash,
            )
            # This read only chooses which profile may be considered. The slot
            # UPDATE below is the decision point and closes concurrent drain,
            # revocation, and re-enrollment races.
            if host_row is None or host_row["status"] not in CLAIMABLE_STATUSES:
                return None
            if (
                host_row["slots_total"] is not None
                and host_row["slots_used"] >= host_row["slots_total"]
            ):
                return None
            capabilities = host_row["capabilities"]
            credential_matches = bool(host_row["credential_matches"])
            secure_attestation = bool(
                credential_matches
                and sandbox_runtime == KATA_RUNTIME
                and isinstance(host_kernel, str)
                and bool(host_kernel.strip())
                and isinstance(guest_kernel, str)
                and bool(guest_kernel.strip())
                and host_kernel.strip() != guest_kernel.strip()
            )
            job_row = await conn.fetchrow(
                f"""
                SELECT {_QUALIFIED_JOB_COLUMNS}, to_jsonb(s) AS thread_sandbox,
                       t.title_source
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id
                LEFT JOIN {THREAD_SANDBOX_SOURCE} ON s.thread_id = j.thread_id
                WHERE j.state = 'queued' AND j.cancel_requested = FALSE
                  AND NOT EXISTS (
                        SELECT 1 FROM agent_jobs earlier
                        WHERE earlier.thread_id = j.thread_id
                          AND earlier.turn_no < j.turn_no
                          AND earlier.state IN ('queued', 'running')
                  )
                  AND COALESCE(j.metadata->'requires', '[]'::jsonb) <@ $1::jsonb
                  AND (
                        t.execution_profile = '{ExecutionProfile.TRUSTED_V1.value}'
                        OR (
                            t.execution_profile = '{ExecutionProfile.UNTRUSTED_KATA_V1.value}'
                            AND $3::boolean
                        )
                  )
                  AND (
                        (
                            s.thread_id IS NULL AND $5::boolean AND $4::boolean
                            AND NOT EXISTS (
                                SELECT 1 FROM agent_jobs parent
                                WHERE parent.id = j.parent_job_id
                                  AND parent.state = 'succeeded'
                            )
                        )
                        OR (
                            s.host = $2
                            AND $6::boolean
                            AND s.fork_operation_id IS NULL
                            AND s.context_status <> 'lost'
                            AND s.native_state <> 'lost'
                            AND s.state IN (
                                'provisioning', 'starting', 'running', 'stopped'
                            )
                            AND s.shared_live
                        )
                  )
                ORDER BY j.created_at
                LIMIT 1
                FOR UPDATE OF j SKIP LOCKED
                """,
                capabilities,
                host,
                secure_attestation,
                shared_attestation,
                pin_allows_new_sandbox,
                followup_attestation,
            )
            if job_row is None:
                return None
            job_id = job_row["id"]
            # The atomic half of the slot claim. Under READ COMMITTED two
            # claims hitting the same host row serialize and the loser
            # re-evaluates capacity against the winner's committed value.
            took_slot = await conn.fetchval(
                f"""
                UPDATE agent_runner_hosts
                SET slots_used = slots_used + 1
                WHERE host = $1
                  AND status = '{HOST_ONLINE}'
                  AND (slots_total IS NULL OR slots_used < slots_total)
                  AND credential_hash = $2
                RETURNING slots_used
                """,
                host,
                host_credential_hash,
            )
            if took_slot is None:
                return None
            next_no = await conn.fetchval(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM agent_attempts WHERE job_id = $1",
                job_id,
            )
            thread_sandbox = load_json(job_row["thread_sandbox"])
            # A thread keeps the version it started on; see
            # ``resolve_runtime_version``. Read here, inside the claim, because
            # the answer must be the one the attempt is stamped with — the
            # TOCTOU that stamping exists to close.
            thread_version = None
            if job_row["thread_id"]:
                thread_version = await conn.fetchval(
                    """
                    SELECT a.runtime_version
                    FROM agent_attempts a
                    JOIN agent_jobs j ON j.id = a.job_id
                    WHERE j.thread_id = $1 AND a.runtime_version IS NOT NULL
                    ORDER BY a.started_at DESC
                    LIMIT 1
                    """,
                    job_row["thread_id"],
                )
            runtime_version = resolve_runtime_version(
                runtime=job_row["runtime"],
                thread_version=thread_version,
                selected=selected_versions,
                available=runtime_versions_available,
                host_default=runtime_versions,
            )
            attempt_image_digest = sandbox_image_digest
            if isinstance(thread_sandbox, dict):
                # A retained chat executes in the already-created container,
                # not in the runner replica's current image. A rolling image
                # upgrade must therefore keep stamping the immutable image and
                # CLI version that this exact writable layer actually uses.
                # This also preserves an explicit NULL version: it means the
                # old container's image default, not today's selected version.
                runtime_version = thread_sandbox.get("runtime_version")
                attempt_image_digest = thread_sandbox.get("sandbox_image_digest")
            attempt = await conn.fetchrow(
                """
                INSERT INTO agent_attempts
                    (job_id, thread_id, attempt_no, lease_owner, lease_generation,
                     lease_expires_at, base_sha, host_id,
                     sandbox_runtime, sandbox_image_digest, runtime_version,
                     host_kernel, guest_kernel)
                VALUES ($1, $13, $2, $3, $4, NOW() + make_interval(secs => $5), $6, $7,
                        $8, $9, $10, $11, $12)
                RETURNING id, attempt_no, lease_generation,
                          sandbox_runtime, sandbox_image_digest, runtime_version
                """,
                job_id,
                next_no,
                worker_id,
                next_no,
                lease_ttl_seconds,
                job_row["base_sha"],
                host,
                sandbox_runtime,
                attempt_image_digest,
                runtime_version,
                host_kernel,
                guest_kernel,
                job_row["thread_id"],
            )
            await conn.execute(
                "UPDATE agent_jobs SET state = 'running', current_attempt_id = $2, "
                "updated_at = NOW() WHERE id = $1",
                job_id,
                attempt["id"],
            )
        claim = _job_row_to_dict(job_row)
        claim["state"] = RUNNING
        claim["current_attempt_id"] = attempt["id"]
        claim["attempt_id"] = attempt["id"]
        claim["attempt_no"] = attempt["attempt_no"]
        claim["title_generation_pending"] = (
            job_row["title_source"] == "fallback"
            and job_row["turn_no"] == 1
            and attempt["attempt_no"] == 1
        )
        claim["lease_generation"] = attempt["lease_generation"]
        # The stamp as written, not as the host row reads later: a rolling
        # upgrade heartbeat can move that row after this attempt starts.
        claim["sandbox_runtime"] = attempt["sandbox_runtime"]
        claim["sandbox_image_digest"] = attempt["sandbox_image_digest"]
        claim["runtime_version"] = attempt["runtime_version"]
        # The runner receives the exact retained ownership row so it can ask
        # this host's supervisor to start the recorded container.
        claim["thread_sandbox"] = thread_sandbox
        return claim

    @staticmethod
    async def _attempts_spent(conn: Any, job_id: str) -> int:
        """Count attempts that actually got their turn.

        The retry budget used to be read off ``attempt_no``, which numbers
        every claim including ones the platform abandoned before the agent
        started. Counting instead means an aborted claim is free: numbers still
        advance (they are referenced by events, and gaps are informative), but
        only attempts that ran spend the budget.
        """
        return await conn.fetchval(
            "SELECT count(*) FROM agent_attempts WHERE job_id = $1 AND status <> $2",
            job_id,
            ATTEMPT_ABORTED,
        )

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
        async with self._pool.acquire() as conn, conn.transaction():
            released_row = await conn.fetchrow(
                """
                UPDATE agent_attempts
                SET status = $2, finished_at = NOW()
                WHERE id = $1 AND status = 'running'
                  AND ($3::bigint IS NULL OR lease_generation = $3)
                  AND lease_expires_at > NOW()
                RETURNING job_id, host_id
                """,
                attempt_id,
                ATTEMPT_ABORTED,
                lease_generation,
            )
            if released_row is None:
                return False
            released = released_row["job_id"]
            # The store's own answer, not the caller's. `agent_job_events.job_id`
            # has no foreign key, so a mismatched (job_id, attempt_id) pair would
            # write a control event into a different job's stream — potentially a
            # different tenant's.
            await self._insert_event(
                conn,
                job_id=released,
                attempt_id=attempt_id,
                event_type=EVENT_ATTEMPT_ABORTED,
                payload={"reason": "credential_unavailable"},
            )
            # Fenced on this attempt still being the current one, so a job that
            # has since moved on is never dragged back to `queued`.
            #
            # An owner who cancelled while we held the claim gets `cancelled`,
            # not `queued`. Requeueing them was a dead end: `claim_job` skips
            # queued rows with `cancel_requested`, and the reaper only reaches
            # jobs with a *running* attempt — which this no longer has — so the
            # job sat in `queued` that nothing could ever move again.
            released_state = await conn.fetchval(
                """
                UPDATE agent_jobs
                SET state = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'queued' END,
                    terminal_resume_pending = cancel_requested,
                    current_attempt_id = NULL,
                    updated_at = NOW()
                WHERE id = $1 AND current_attempt_id = $2 AND state = 'running'
                RETURNING state
                """,
                released,
                attempt_id,
            )
            # All close-attempt paths take locks in attempt -> job -> host
            # order. Previously transition took job -> attempt while this path
            # took attempt -> host -> job, so concurrent finishes and aborts
            # could form a cycle across two jobs. Hung off the guarded attempt
            # UPDATE above, this still runs once and cannot double-count even
            # when the job UPDATE changed no row.
            await self._release_host_slot(conn, released_row["host_id"])
            if released_state == CANCELLED:
                # A cancelled parent is a turn boundary like any other
                # terminal outcome: fail any child that cannot preserve
                # continuity in this transaction.
                await self._fail_discontinuous_follow_ups(conn, parent_job_id=released)
        return True

    async def heartbeat(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        lease_ttl_seconds: float,
    ) -> dict[str, Any]:
        """Extend a live lease and report cancellation, fenced by generation.

        Returns ``{"ok": False}`` when the attempt is no longer the owner
        (superseded, finished, or generation mismatch) — the worker must stop
        immediately. On success also returns ``cancel_requested`` so workers
        learn about cancellation without extra polling.
        """
        async with self._pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                UPDATE agent_attempts
                SET lease_expires_at = NOW() + make_interval(secs => $3)
                WHERE id = $1 AND lease_generation = $2 AND status = 'running'
                  AND lease_expires_at > NOW()
                RETURNING job_id
                """,
                attempt_id,
                lease_generation,
                lease_ttl_seconds,
            )
            if job_id is None:
                return {"ok": False}
            row = await conn.fetchrow(
                "SELECT state, cancel_requested FROM agent_jobs WHERE id = $1", job_id
            )
        return {
            "ok": True,
            "job_id": job_id,
            "state": row["state"],
            "cancel_requested": row["cancel_requested"],
        }

    # ── Fenced state transitions ───────────────────────────────────────

    @staticmethod
    async def _transition_job(
        conn: Any,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        from_states: tuple[str, ...],
        to_state: str,
        detail: str | None,
        base_sha: str | None,
    ) -> Any:
        """The fenced job UPDATE behind :meth:`transition`; ``None`` when fenced out."""
        return await conn.fetchrow(
            """
            UPDATE agent_jobs j
            SET state = $4,
                detail = COALESCE($5, j.detail),
                base_sha = COALESCE(j.base_sha, $7),
                updated_at = NOW()
            WHERE j.id = $1
              AND j.state = ANY($3::text[])
              AND j.current_attempt_id = $2
              AND EXISTS (
                    SELECT 1 FROM agent_attempts a
                    WHERE a.id = $2 AND a.lease_generation = $6
                      AND a.status = 'running'
                      AND a.lease_expires_at > NOW()
              )
            RETURNING j.id, j.thread_id
            """,
            job_id,
            attempt_id,
            list(from_states),
            to_state,
            detail,
            lease_generation,
            base_sha,
        )

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
        async with self._pool.acquire() as conn, conn.transaction():
            # Lock the attempt before its job, matching release_claim and the
            # reaper. Without an explicit first lock, the job UPDATE below and
            # a concurrent abort can each hold the row the other needs.
            live_attempt = await conn.fetchval(
                """
                SELECT id
                FROM agent_attempts
                WHERE id = $1 AND lease_generation = $2
                  AND status = 'running' AND lease_expires_at > NOW()
                FOR UPDATE
                """,
                attempt_id,
                lease_generation,
            )
            if live_attempt is None:
                return False
            # A success carries its native turn ordinal. The runner settles the
            # shared workspace before it reports success and settlement assigns
            # the ordinal; the schema refuses a succeeded row without one, and
            # nothing here guesses it on the caller's behalf — a success that
            # skipped settlement is a protocol violation and fails loudly.
            try:
                updated = await self._transition_job(
                    conn,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    lease_generation=lease_generation,
                    from_states=from_states,
                    to_state=to_state,
                    detail=detail,
                    base_sha=base_sha,
                )
            except asyncpg.CheckViolationError as exc:
                if "native_turn" not in str(exc):
                    raise
                raise ValueError(
                    "a succeeded turn must settle its native context before it finishes"
                ) from exc
            if updated is None:
                return False
            if to_state in TERMINAL_STATES:
                sandbox_transition = None
                # Close any retained-sandbox turn boundary in the same
                # transaction as the job outcome.  A successful turn has
                # already persisted ``last_settled_job_id`` and only clears
                # its admission receipt.  Finishing before that boundary is
                # either an admitted/ambiguous turn (LOST) or a pre-admission
                # failure (STOPPING, safe to contain without replaying stdin).
                #
                # This is also the crash backstop for the runner's finally:
                # a terminal job must never leave ``active_attempt_id`` stuck
                # on an attempt the reaper will no longer visit.
                if updated["thread_id"] is not None:
                    sandbox_transition = await conn.fetchrow(
                        """
                        UPDATE agent_thread_sandbox_memberships m
                        SET context_status = CASE
                                WHEN m.last_settled_job_id IS DISTINCT FROM $1
                                     AND (
                                        m.admitted_job_id = $1
                                        OR g.container_id IS NULL
                                     ) THEN 'lost'
                                ELSE m.context_status
                            END,
                            native_state = CASE
                                WHEN m.last_settled_job_id IS DISTINCT FROM $1
                                     AND (
                                        m.admitted_job_id = $1
                                        OR g.container_id IS NULL
                                     ) THEN 'lost'
                                ELSE m.native_state
                            END,
                            resident_state = CASE
                                WHEN m.last_settled_job_id = $1 THEN m.resident_state
                                WHEN m.admitted_job_id = $1 OR g.container_id IS NULL
                                    THEN 'lost'
                                ELSE 'restarting'
                            END,
                            active_attempt_id = CASE
                                WHEN m.active_attempt_id = $2 THEN NULL
                                ELSE m.active_attempt_id
                            END,
                            admitted_job_id = CASE
                                WHEN m.admitted_job_id = $1 THEN NULL
                                ELSE m.admitted_job_id
                            END,
                            admitted_at = CASE
                                WHEN m.admitted_job_id = $1 THEN NULL
                                ELSE m.admitted_at
                            END,
                            last_error = CASE
                                WHEN m.last_settled_job_id = $1 THEN m.last_error
                                ELSE COALESCE($4, 'retained turn ended before settlement')
                            END,
                            updated_at = NOW()
                        FROM agent_sandboxes g
                        WHERE m.thread_id = $3 AND g.id = m.sandbox_id
                          AND (
                                m.active_attempt_id = $2
                                OR m.admitted_job_id = $1
                          )
                        RETURNING m.context_status, m.sandbox_id,
                                  m.last_settled_job_id = $1 AS settled
                        """,
                        job_id,
                        attempt_id,
                        updated["thread_id"],
                        detail,
                    )
                    if sandbox_transition is not None:
                        await conn.execute(
                            """
                            UPDATE agent_sandboxes g
                            SET state = CASE
                                    WHEN $3 AND g.container_id IS NULL THEN 'lost'
                                    WHEN $2 THEN 'running'
                                    WHEN g.active_operation_count > 0 OR EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.active_attempt_id IS NOT NULL
                                    ) THEN 'running'
                                    WHEN EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.thread_id <> $4
                                          AND peer.context_status <> 'lost'
                                          AND peer.native_state <> 'lost'
                                    ) THEN 'running'
                                    ELSE 'stopping'
                                END,
                                last_activity_at = NOW(),
                                idle_deadline = CASE
                                    WHEN (
                                        $2 OR EXISTS (
                                            SELECT 1
                                            FROM agent_thread_sandbox_memberships peer
                                            WHERE peer.sandbox_id = g.id
                                              AND peer.thread_id <> $4
                                              AND peer.context_status <> 'lost'
                                              AND peer.native_state <> 'lost'
                                        )
                                     )
                                     AND g.active_operation_count = 0
                                     AND NOT EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.active_attempt_id IS NOT NULL
                                     )
                                    THEN COALESCE(
                                        g.idle_deadline,
                                        NOW() + make_interval(secs => g.idle_timeout_seconds)
                                    )
                                    ELSE NULL
                                END,
                                updated_at = NOW()
                            WHERE g.id = $1
                            """,
                            sandbox_transition["sandbox_id"],
                            bool(sandbox_transition["settled"]),
                            sandbox_transition["context_status"] == "lost",
                            updated["thread_id"],
                        )
                    if (
                        sandbox_transition is not None
                        and sandbox_transition["context_status"] == "lost"
                    ):
                        await conn.execute(
                            "UPDATE agent_attempts SET context_status = 'lost' WHERE id = $1",
                            attempt_id,
                        )
                        await self._fail_unrunnable_follow_ups(
                            conn,
                            thread_id=updated["thread_id"],
                        )
                finished = await conn.fetchrow(
                    "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() "
                    "WHERE id = $1 AND status = 'running' RETURNING host_id",
                    attempt_id,
                )
                # Only when this call is the one that closed the attempt. The
                # job-level fence above already makes a second terminal
                # transition impossible, but the slot must not depend on that
                # being true two layers away.
                if finished is not None:
                    await self._release_host_slot(conn, finished["host_id"])
                await conn.execute(
                    "UPDATE agent_jobs SET terminal_resume_pending = FALSE WHERE id = $1",
                    job_id,
                )
                # Native-session continuity is ready at settlement. Group
                # snapshot publication is asynchronous and never gates the
                # next message in this shared workspace.
                if not (
                    sandbox_transition is not None
                    and sandbox_transition["context_status"] == "lost"
                ):
                    await self._fail_discontinuous_follow_ups(conn, parent_job_id=job_id)
        return True

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
        async with self._pool.acquire() as conn, conn.transaction():
            attempt = await conn.fetchrow(
                "SELECT job_id, status, lease_generation, "
                "lease_expires_at <= NOW() AS expired "
                "FROM agent_attempts WHERE id = $1 FOR UPDATE",
                attempt_id,
            )
            if (
                attempt is None
                or attempt["status"] != ATTEMPT_RUNNING
                or attempt["lease_generation"] != lease_generation
                or attempt["expired"]
            ):
                return None
            phase = (payload or {}).get("phase") if event_type == "lifecycle" else None
            if phase == "workspace_ready":
                await conn.execute(
                    "UPDATE agent_attempts SET terminal_ready = TRUE WHERE id = $1",
                    attempt_id,
                )
            elif phase in {"workspace_preparing", "workspace_finalizing"}:
                await conn.execute(
                    "UPDATE agent_attempts SET terminal_ready = FALSE WHERE id = $1",
                    attempt_id,
                )
            return await self._insert_event(
                conn,
                job_id=attempt["job_id"],
                attempt_id=attempt_id,
                event_type=event_type,
                payload=payload,
                client_seq=event_seq,
            )

    async def record_continuity_mode(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        mode: str | None,
        context_status: str | None = None,
    ) -> bool:
        """Stamp the diagnostic rung and optional product context semantic."""
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    UPDATE agent_attempts
                    SET continuity_mode = COALESCE($3, continuity_mode),
                        context_status = COALESCE($4, context_status)
                    WHERE id = $1 AND lease_generation = $2
                      AND status = 'running' AND lease_expires_at > NOW()
                    RETURNING id
                    """,
                    attempt_id,
                    lease_generation,
                    mode,
                    context_status,
                )
            )

    async def record_attempt_resources(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        rollup: dict[str, Any],
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
        tool_calls = rollup.get("tool_calls") or []
        processes = rollup.get("processes") or []
        async with self._pool.acquire() as conn, conn.transaction():
            recorded = bool(
                await conn.fetchval(
                    """
                    WITH live AS (
                        SELECT id, job_id
                        FROM agent_attempts
                        WHERE id = $1 AND lease_generation = $2
                          AND status = 'running' AND lease_expires_at > NOW()
                    )
                    INSERT INTO agent_attempt_resources (
                        attempt_id, job_id, cpu_usec, mem_peak_bytes, mem_avg_bytes,
                        io_read_bytes, io_write_bytes, net_rx_bytes, net_tx_bytes,
                        sample_count, coverage, sampler_version, tool_calls_truncated,
                        tool_time_ms, process_observer, process_observation_complete,
                        process_events_lost, processes_truncated
                    )
                    SELECT live.id, live.job_id, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                           $13, $14, $15, $16, $17, $18
                    FROM live
                    ON CONFLICT (attempt_id) DO UPDATE SET
                        cpu_usec = EXCLUDED.cpu_usec,
                        mem_peak_bytes = EXCLUDED.mem_peak_bytes,
                        mem_avg_bytes = EXCLUDED.mem_avg_bytes,
                        io_read_bytes = EXCLUDED.io_read_bytes,
                        io_write_bytes = EXCLUDED.io_write_bytes,
                        net_rx_bytes = EXCLUDED.net_rx_bytes,
                        net_tx_bytes = EXCLUDED.net_tx_bytes,
                        sample_count = EXCLUDED.sample_count,
                        coverage = EXCLUDED.coverage,
                        sampler_version = EXCLUDED.sampler_version,
                        tool_calls_truncated = EXCLUDED.tool_calls_truncated,
                        tool_time_ms = EXCLUDED.tool_time_ms,
                        process_observer = EXCLUDED.process_observer,
                        process_observation_complete = EXCLUDED.process_observation_complete,
                        process_events_lost = EXCLUDED.process_events_lost,
                        processes_truncated = EXCLUDED.processes_truncated
                    RETURNING attempt_id
                    """,
                    attempt_id,
                    lease_generation,
                    rollup.get("cpu_usec"),
                    rollup.get("mem_peak_bytes"),
                    rollup.get("mem_avg_bytes"),
                    rollup.get("io_read_bytes"),
                    rollup.get("io_write_bytes"),
                    rollup.get("net_rx_bytes"),
                    rollup.get("net_tx_bytes"),
                    int(rollup.get("sample_count") or 0),
                    float(rollup.get("coverage") or 0.0),
                    str(rollup.get("sampler_version") or "unknown"),
                    bool(rollup.get("tool_calls_truncated")),
                    rollup.get("tool_time_ms"),
                    rollup.get("process_observer"),
                    bool(rollup.get("process_observation_complete")),
                    int(rollup.get("process_events_lost") or 0),
                    bool(rollup.get("processes_truncated")),
                )
            )
            if not recorded:
                return False
            await conn.execute(
                "DELETE FROM agent_tool_call_resources WHERE attempt_id = $1",
                attempt_id,
            )
            if tool_calls:
                await conn.executemany(
                    """
                    INSERT INTO agent_tool_call_resources (
                        attempt_id, call_seq, tool_use_id, tool_name, timing_source,
                        duration_ms, sample_count, overlap_n, measured_span_ms,
                        cpu_usec_exclusive, cpu_usec_shared,
                        mem_peak_exclusive_bytes, mem_peak_shared_bytes, process_count,
                        process_cpu_usec, process_mem_peak_bytes,
                        process_io_read_bytes, process_io_write_bytes,
                        process_resources_partial
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                            $15, $16, $17, $18, $19)
                    """,
                    [
                        (
                            attempt_id,
                            int(call["call_seq"]),
                            str(call["tool_use_id"]),
                            str(call["tool_name"]),
                            str(call.get("timing_source") or "message"),
                            call.get("duration_ms"),
                            int(call.get("sample_count") or 0),
                            int(call.get("overlap_n") or 1),
                            call.get("measured_span_ms"),
                            call.get("cpu_usec_exclusive"),
                            call.get("cpu_usec_shared"),
                            call.get("mem_peak_exclusive_bytes"),
                            call.get("mem_peak_shared_bytes"),
                            int(call.get("process_count") or 0),
                            call.get("process_cpu_usec"),
                            call.get("process_mem_peak_bytes"),
                            call.get("process_io_read_bytes"),
                            call.get("process_io_write_bytes"),
                            bool(call.get("process_resources_partial")),
                        )
                        for call in tool_calls
                    ],
                )
            await conn.execute(
                "DELETE FROM agent_process_observations WHERE attempt_id = $1",
                attempt_id,
            )
            if processes:
                await conn.executemany(
                    """
                    INSERT INTO agent_process_observations (
                        attempt_id, process_seq, uid, pid, tgid, ppid, comm, executable,
                        started_offset_ms, duration_ms, exit_code,
                        observed_fork, observed_exec, observed_exit, tool_call_seqs,
                        cpu_usec, mem_peak_bytes, io_read_bytes, io_write_bytes,
                        io_rchar_bytes, io_wchar_bytes
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                            $14, $15, $16, $17, $18, $19, $20, $21)
                    """,
                    [
                        (
                            attempt_id,
                            int(process["process_seq"]),
                            int(process["uid"]),
                            int(process["pid"]),
                            int(process["tgid"]),
                            process.get("ppid"),
                            str(process["comm"]),
                            process.get("executable"),
                            int(process.get("started_offset_ms") or 0),
                            process.get("duration_ms"),
                            process.get("exit_code"),
                            bool(process.get("observed_fork")),
                            bool(process.get("observed_exec")),
                            bool(process.get("observed_exit")),
                            [int(seq) for seq in process.get("tool_call_seqs") or []],
                            process.get("cpu_usec"),
                            process.get("mem_peak_bytes"),
                            process.get("io_read_bytes"),
                            process.get("io_write_bytes"),
                            process.get("io_rchar_bytes"),
                            process.get("io_wchar_bytes"),
                        )
                        for process in processes
                    ],
                )
            return True

    async def job_attempts_telemetry(self, *, job_id: str) -> list[dict[str, Any]]:
        """Read lightweight attempt summaries without tool or process rows."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id AS attempt_id, attempt_no, continuity_mode, context_status,
                       started_at, finished_at
                FROM agent_attempts
                WHERE job_id = $1
                ORDER BY attempt_no
                """,
                job_id,
            )
            return [dict(row) for row in rows]

    async def job_turn_telemetry(
        self, *, job_id: str, attempt_id: int | None = None
    ) -> dict[str, Any] | None:
        """Read one job-scoped attempt, defaulting to the newest attempt.

        Newest attempt rather than the current one: a retried turn's earlier
        attempts each have their own window, and the one a reader wants is the
        one that produced the result in front of them. One repeatable-read
        snapshot keeps the aggregate, per-call rows, and process rows from
        different rollup retries out of the same response. The child rows stay
        separate statements so counters arrive as numbers rather than as JSON
        this pool would hand back as text.
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(
                isolation="repeatable_read",
                readonly=True,
            ),
        ):
            row = await conn.fetchrow(
                """
                SELECT a.id AS attempt_id, a.attempt_no,
                       a.continuity_mode, a.context_status,
                       a.started_at, a.finished_at,
                       r.cpu_usec, r.mem_peak_bytes, r.mem_avg_bytes,
                       r.io_read_bytes, r.io_write_bytes,
                       r.net_rx_bytes, r.net_tx_bytes,
                       r.sample_count, r.coverage, r.sampler_version, r.tool_time_ms,
                       r.process_observer,
                       COALESCE(r.process_observation_complete, FALSE)
                           AS process_observation_complete,
                       COALESCE(r.process_events_lost, 0) AS process_events_lost,
                       COALESCE(r.processes_truncated, FALSE) AS processes_truncated,
                       COALESCE(r.tool_calls_truncated, FALSE) AS tool_calls_truncated
                FROM agent_attempts a
                LEFT JOIN agent_attempt_resources r ON r.attempt_id = a.id
                WHERE a.job_id = $1
                  AND ($2::bigint IS NULL OR a.id = $2)
                ORDER BY a.attempt_no DESC
                LIMIT 1
                """,
                job_id,
                attempt_id,
            )
            if row is None:
                return None
            telemetry = dict(row)
            calls = await conn.fetch(
                """
                SELECT call_seq, tool_use_id, tool_name,
                       timing_source, duration_ms,
                       sample_count, overlap_n, measured_span_ms,
                       cpu_usec_exclusive, cpu_usec_shared,
                       mem_peak_exclusive_bytes, mem_peak_shared_bytes, process_count,
                       process_cpu_usec, process_mem_peak_bytes,
                       process_io_read_bytes, process_io_write_bytes,
                       process_resources_partial
                FROM agent_tool_call_resources
                WHERE attempt_id = $1
                ORDER BY call_seq
                """,
                telemetry["attempt_id"],
            )
            telemetry["tool_calls"] = [dict(call) for call in calls]
            processes = await conn.fetch(
                """
                SELECT process_seq, uid, pid, tgid, ppid, comm, executable,
                       started_offset_ms, duration_ms, exit_code,
                       observed_fork, observed_exec, observed_exit, tool_call_seqs,
                       cpu_usec, mem_peak_bytes, io_read_bytes, io_write_bytes,
                       io_rchar_bytes, io_wchar_bytes
                FROM agent_process_observations
                WHERE attempt_id = $1
                ORDER BY process_seq
                """,
                telemetry["attempt_id"],
            )
            telemetry["processes"] = [dict(process) for process in processes]
            return telemetry

    async def thread_workspace_ready(self, *, thread_id: str) -> bool:
        """Return whether a retained thread permits owner workspace mutation.

        The broker's in-memory suspension is the immediate barrier. This
        durable copy closes broker-restart and old-turn-URL gaps: an active
        attempt must have published ``workspace_ready``.
        """
        async with self._pool.acquire() as conn:
            ready = await conn.fetchval(
                """
                SELECT
                    m.context_status <> 'lost'
                    AND m.native_state <> 'lost'
                    AND g.state NOT IN (
                        'provisioning', 'stopping', 'stopped_dirty', 'lost'
                    )
                    AND m.fork_operation_id IS NULL
                    AND (
                        m.active_attempt_id IS NULL
                        OR EXISTS (
                            SELECT 1 FROM agent_attempts a
                            WHERE a.id = m.active_attempt_id
                              AND a.status = 'running'
                              AND a.lease_expires_at > NOW()
                              AND a.terminal_ready = TRUE
                        )
                    )
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1
                """,
                thread_id,
            )
        return bool(ready)

    async def fence_terminal_workspace(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
    ) -> bool:
        """Revoke readiness only for the still-live fenced attempt."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agent_attempts
                SET terminal_ready = FALSE
                WHERE id = $1
                  AND lease_generation = $2
                  AND status = 'running'
                  AND lease_expires_at > NOW()
                """,
                attempt_id,
                lease_generation,
            )
        return result == "UPDATE 1"

    async def list_terminal_resumes_pending(self, *, limit: int = 100) -> list[str]:
        """List settled workspaces whose broker resume still needs confirmation."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id
                FROM agent_jobs
                WHERE terminal_resume_pending = TRUE
                  AND state = ANY($1::text[])
                ORDER BY updated_at
                LIMIT $2
                """,
                list(TERMINAL_STATES),
                limit,
            )
        return [row["id"] for row in rows]

    async def mark_terminal_resume_complete(self, *, job_id: str) -> bool:
        """Clear durable recovery state after the broker confirms a resume."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agent_jobs
                SET terminal_resume_pending = FALSE
                WHERE id = $1
                  AND terminal_resume_pending = TRUE
                  AND state = ANY($2::text[])
                """,
                job_id,
                list(TERMINAL_STATES),
            )
        return result == "UPDATE 1"

    async def list_events_after(
        self,
        *,
        job_id: str,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return events for a job with a global id strictly greater than the cursor."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, job_id, attempt_id, seq, event_type, payload, created_at
                FROM agent_job_events
                WHERE job_id = $1 AND id > $2
                ORDER BY id
                LIMIT $3
                """,
                job_id,
                after_id,
                limit,
            )
        return [
            {
                "id": row["id"],
                "job_id": row["job_id"],
                "attempt_id": row["attempt_id"],
                "seq": row["seq"],
                "event_type": row["event_type"],
                "payload": load_json(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def _insert_event(
        self,
        conn: Any,
        *,
        job_id: str,
        attempt_id: int,
        event_type: str,
        payload: dict[str, Any] | None,
        client_seq: int | None = None,
    ) -> int:
        """Insert one event row inside the caller's transaction.

        A ``client_seq`` the attempt has already stored is a retry of a
        response that never arrived: answer with the stored row's id and do
        not run the thread-message mirror again — it ran when the row landed.
        """
        if client_seq is not None:
            existing = await conn.fetchval(
                "SELECT id FROM agent_job_events WHERE attempt_id = $1 AND client_seq = $2",
                attempt_id,
                client_seq,
            )
            if existing is not None:
                return int(existing)
        event_id = await conn.fetchval(
            """
            INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload, client_seq)
            VALUES (
                $1, $2,
                (SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_job_events WHERE attempt_id = $2),
                $3, $4::jsonb, $5
            )
            RETURNING id
            """,
            job_id,
            attempt_id,
            event_type,
            json.dumps(payload) if payload is not None else None,
            client_seq,
        )
        text = (payload or {}).get("text")
        if event_type == "message" and isinstance(text, str) and text.strip():
            await conn.execute(
                """
                INSERT INTO agent_thread_messages
                    (thread_id, job_id, role, content, source_event_id)
                SELECT thread_id, id, 'assistant', $2, $3
                FROM agent_jobs
                WHERE id = $1 AND thread_id IS NOT NULL
                ON CONFLICT (source_event_id) DO NOTHING
                """,
                job_id,
                text,
                event_id,
            )
            await conn.execute(
                """
                UPDATE agent_threads t
                SET updated_at = NOW()
                FROM agent_jobs j
                WHERE j.id = $1 AND t.id = j.thread_id
                """,
                job_id,
            )
        return event_id

    # ── Restartable thread sandbox ─────────────────────────────────────

    async def reserve_thread_sandbox(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        host: str,
        sandbox_id: str,
        container_name: str,
        runtime_id: str,
        runtime_version: str | None,
        sandbox_image_digest: str | None,
        execution_profile: str,
        resident_slot_capacity: int = 8,
        memory_limit: str = "4g",
        cpu_limit: str = "2",
        stop_timeout_seconds: int = 10,
    ) -> dict[str, Any] | None:
        """Reserve/start one thread's durable sandbox under its attempt fence.

        Unlike HOT residency this mapping is never taken over by another host:
        before persistent storage, the retained container's writable layer is
        the only copy. A different host must leave the turn queued rather than
        create an empty replacement and call it continuity.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            earns = await conn.fetchrow(
                """
                SELECT j.id, j.thread_id, j.user_id
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE a.id = $1 AND a.lease_generation = $2
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                  AND a.host_id = $3 AND j.thread_id = $4
                  AND j.current_attempt_id = a.id
                FOR SHARE OF a, j
                """,
                attempt_id,
                lease_generation,
                host,
                thread_id,
            )
            if earns is None:
                return None
            # Claiming is fenced by the worker's shared-sandbox-v1
            # attestation. Every reservation therefore creates or reuses a
            # shared group; there is no one-to-one fallback.
            return await self._reserve_shared_thread_sandbox(
                conn,
                thread_id=thread_id,
                attempt_id=attempt_id,
                host=host,
                sandbox_id=sandbox_id,
                container_name=container_name,
                runtime_id=runtime_id,
                runtime_version=runtime_version,
                sandbox_image_digest=sandbox_image_digest,
                execution_profile=execution_profile,
                resident_slot_capacity=resident_slot_capacity,
                memory_limit=memory_limit,
                cpu_limit=cpu_limit,
                stop_timeout_seconds=stop_timeout_seconds,
                user_id=str(earns["user_id"]),
                job_id=str(earns["id"]),
            )

    async def _reserve_shared_thread_sandbox(
        self,
        conn: Any,
        *,
        thread_id: str,
        attempt_id: int,
        host: str,
        sandbox_id: str,
        container_name: str,
        runtime_id: str,
        runtime_version: str | None,
        sandbox_image_digest: str | None,
        execution_profile: str,
        resident_slot_capacity: int,
        memory_limit: str,
        cpu_limit: str,
        stop_timeout_seconds: int,
        user_id: str,
        job_id: str,
    ) -> dict[str, Any] | None:
        """Reserve one independently fenced member in a shared-live group."""
        if not 2 <= resident_slot_capacity <= 128:
            raise ValueError("shared sandbox resident slot capacity must be between 2 and 128")
        # Lock membership first everywhere; Docker reconciliation uses the
        # same order before it fences the shared group row.
        await conn.fetchval(
            """
            SELECT thread_id
            FROM agent_thread_sandbox_memberships
            WHERE thread_id = $1
            FOR UPDATE
            """,
            thread_id,
        )
        membership = await conn.fetchrow(
            """
            SELECT m.*, g.origin_thread_id
            FROM agent_thread_sandbox_memberships m
            JOIN agent_sandboxes g ON g.id = m.sandbox_id
            WHERE m.thread_id = $1
            FOR UPDATE OF g
            """,
            thread_id,
        )
        if membership is None:
            capability = await conn.fetchval(
                """
                SELECT 1 FROM agent_runner_hosts
                WHERE host = $1 AND status = 'online'
                  AND capabilities @> '["shared-sandbox-v1"]'::jsonb
                """,
                host,
            )
            if capability is None:
                raise ValueError("host has not advertised shared-sandbox-v1")
            await conn.execute(
                """
                INSERT INTO agent_sandboxes (
                    id, origin_thread_id, user_id, host, container_name,
                    runtime_id, runtime_version, sandbox_image_digest,
                    execution_profile, resident_slot_capacity, memory_limit,
                    cpu_limit, stop_timeout_seconds
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                sandbox_id,
                thread_id,
                user_id,
                host,
                container_name,
                runtime_id,
                runtime_version,
                sandbox_image_digest,
                execution_profile,
                resident_slot_capacity,
                memory_limit,
                cpu_limit,
                stop_timeout_seconds,
            )
            await conn.execute(
                """
                INSERT INTO agent_thread_sandbox_memberships (
                    thread_id, user_id, sandbox_id, resident_slot_id
                ) VALUES ($1, $2, $3, 0)
                """,
                thread_id,
                user_id,
                sandbox_id,
            )
            membership = await conn.fetchrow(
                """
                SELECT m.*, g.origin_thread_id
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1
                FOR UPDATE OF g
                """,
                thread_id,
            )
        if membership is None:
            return None
        group = await conn.fetchrow(
            "SELECT * FROM agent_sandboxes WHERE id = $1 FOR UPDATE",
            membership["sandbox_id"],
        )
        if group is None:
            return None
        immutable = {
            "id": sandbox_id,
            "host": host,
            "container_name": container_name,
            "runtime_id": runtime_id,
            "runtime_version": runtime_version,
            "sandbox_image_digest": sandbox_image_digest,
            "execution_profile": execution_profile,
            "memory_limit": memory_limit,
            "cpu_limit": cpu_limit,
            "stop_timeout_seconds": stop_timeout_seconds,
        }
        if any(group[key] != value for key, value in immutable.items()):
            raise ValueError("shared sandbox immutable identity or runtime changed")
        if group["state"] in {"lost", "stopped_dirty"} or membership["native_state"] == "lost":
            return None
        if membership["fork_operation_id"] is not None:
            # The dequeue query carries the same fence, but admission is a
            # separate transaction and may race a Fork request that wins after
            # claim. Never append a turn to the source session while its
            # settled prefix is being materialized.
            return None
        active_attempt = membership["active_attempt_id"]
        if active_attempt is not None and active_attempt != attempt_id:
            return None
        admitted_job = membership["admitted_job_id"]
        if admitted_job is not None and admitted_job != job_id:
            return None
        already_admitted = active_attempt == attempt_id and group["state"] in {
            "starting",
            "running",
        }
        activation_generation = int(membership["activation_generation"])
        if not already_admitted:
            activation_generation += 1
        context_status = "new" if membership["last_settled_job_id"] is None else "restored"
        await conn.execute(
            """
            UPDATE agent_thread_sandbox_memberships
            SET active_attempt_id = $2, activation_generation = $3,
                context_status = $4, resident_state = CASE
                    WHEN resident_state = 'live' THEN resident_state ELSE 'starting' END,
                last_error = NULL, updated_at = NOW()
            WHERE thread_id = $1
            """,
            thread_id,
            attempt_id,
            activation_generation,
            context_status,
        )
        await conn.execute(
            """
            UPDATE agent_sandboxes
            SET state = 'starting', idle_deadline = NULL,
                last_activity_at = NOW(), updated_at = NOW()
            WHERE id = $1
            """,
            group["id"],
        )
        row = await conn.fetchrow(
            f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
            thread_id,
        )
        return None if row is None else _sandbox_row_to_dict(row)

    async def mark_thread_sandbox_running(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        container_id: str,
        context_status: str,
    ) -> dict[str, Any] | None:
        """Confirm Docker start under both attempt and sandbox generations."""
        async with self._pool.acquire() as conn, conn.transaction():
            locked_membership = await conn.fetchval(
                """
                SELECT thread_id
                FROM agent_thread_sandbox_memberships
                WHERE thread_id = $1
                FOR UPDATE
                """,
                thread_id,
            )
            if locked_membership is None:
                return None
            shared = await conn.fetchrow(
                """
                WITH updated_group AS (
                    UPDATE agent_sandboxes g
                    SET state = 'running', container_id = $6,
                        last_activity_at = NOW(), updated_at = NOW()
                    FROM agent_thread_sandbox_memberships m
                    JOIN agent_attempts a ON a.id = m.active_attempt_id
                    JOIN agent_jobs j ON j.id = a.job_id
                    WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                      AND g.generation = $4
                      AND m.activation_generation = $5
                      AND m.active_attempt_id = $2
                      AND (g.container_id IS NULL OR g.container_id = $6)
                      AND a.id = $2 AND a.lease_generation = $3
                      AND a.status = 'running' AND a.lease_expires_at > NOW()
                      AND j.current_attempt_id = a.id AND j.thread_id = m.thread_id
                    RETURNING g.id
                )
                UPDATE agent_thread_sandbox_memberships m
                SET context_status = $7,
                    resident_state = CASE
                        WHEN m.resident_state = 'live' THEN 'live' ELSE 'starting' END,
                    updated_at = NOW()
                FROM updated_group u
                WHERE m.thread_id = $1 AND m.sandbox_id = u.id
                RETURNING m.thread_id
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                container_id,
                context_status,
            )
            if shared is None:
                return None
            row = await conn.fetchrow(
                f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                thread_id,
            )
        return None if row is None else _sandbox_row_to_dict(row)

    async def record_thread_sandbox_process(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        process_generation: int,
    ) -> bool:
        """Record the supervisor's diagnostic CLI process generation."""
        async with self._pool.acquire() as conn:
            shared = await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET process_generation = $6, resident_state = 'live', updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND g.generation = $4
                  AND m.activation_generation = $5
                  AND m.active_attempt_id = $2
                  AND a.id = $2 AND a.lease_generation = $3
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                RETURNING m.thread_id
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                process_generation,
            )
            return shared is not None

    @staticmethod
    async def _capture_attempt_ledger(
        conn: asyncpg.Connection, *, thread_id: str, attempt_id: int
    ) -> None:
        """Copy the exact residency grant inside a fenced admission transaction.

        The per-attempt grant is a separate, disposable credential. A NULL
        residency binding means an older writer never recorded its Gateway,
        so it deliberately leaves this attempt untracked. Re-admission keeps
        the first reference; rotation preserves an additional distinct grant.
        """
        await conn.execute(
            """
            INSERT INTO agent_attempt_ledger (
                attempt_id, grant_id, residency_generation, binding_id, window_since
            )
            SELECT a.id, m.residency_grant_id, m.residency_generation,
                   m.residency_binding_id, a.started_at
            FROM agent_thread_sandbox_memberships m
            JOIN agent_attempts a ON a.id = m.active_attempt_id
            JOIN agent_jobs j ON j.id = a.job_id AND j.thread_id = m.thread_id
            WHERE m.thread_id = $1 AND a.id = $2
              AND m.residency_generation > 0
              AND m.residency_grant_id IS NOT NULL AND m.residency_grant_id <> ''
              AND m.residency_binding_id IS NOT NULL AND m.residency_binding_id <> ''
            ON CONFLICT (attempt_id, grant_id) DO NOTHING
            """,
            thread_id,
            attempt_id,
        )

    async def record_shared_thread_residency(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_id: str,
        residency_generation: int,
    ) -> bool:
        """Bind a residency and its immutable attribution before CLI startup."""
        async with self._pool.acquire() as conn, conn.transaction():
            # The new ledger FK locks its attempt too. Acquire that lock
            # before membership, matching terminal transition/reaper order,
            # and keep the live-attempt fence stable through the capture.
            if (
                await conn.fetchval(
                    "SELECT id FROM agent_attempts WHERE id = $1 AND lease_generation = $2 "
                    "AND status = 'running' AND lease_expires_at > NOW() FOR SHARE",
                    attempt_id,
                    lease_generation,
                )
                is None
            ):
                return False
            shared = await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET resident_state = CASE
                        WHEN resident_state = 'live' THEN 'live' ELSE 'starting' END,
                    updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND m.sandbox_id = $4
                  AND g.id = m.sandbox_id
                  AND m.residency_generation = $5 AND m.residency_status = 'hot'
                  AND m.active_attempt_id = $2
                  AND a.id = $2 AND a.lease_generation = $3
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                RETURNING m.thread_id
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_id,
                residency_generation,
            )
            if shared is None:
                return False
            await self._capture_attempt_ledger(conn, thread_id=thread_id, attempt_id=attempt_id)
            return True

    async def admit_thread_sandbox_turn(
        self,
        *,
        thread_id: str,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
    ) -> bool:
        """Reserve one prompt delivery durably before Agent CLI stdin.

        The update is idempotent for the same job/attempt, but a different job
        cannot replace an unresolved admission.  A control-plane reservation
        followed by a runner crash is intentionally conservative: the reaper
        records LOST instead of sending the same user message a second time.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Keep the same attempt -> membership order as startup capture
            # and finishing an attempt. The FK insert must not invert it.
            if (
                await conn.fetchval(
                    "SELECT id FROM agent_attempts WHERE id = $1 AND lease_generation = $2 "
                    "AND status = 'running' AND lease_expires_at > NOW() FOR SHARE",
                    attempt_id,
                    lease_generation,
                )
                is None
            ):
                return False
            shared = await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET admitted_job_id = $6,
                    admitted_at = COALESCE(m.admitted_at, NOW()),
                    updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a, agent_jobs j
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND g.generation = $4
                  AND m.activation_generation = $5
                  AND m.active_attempt_id = $2
                  AND a.id = $2 AND a.lease_generation = $3
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                  AND j.id = $6 AND j.current_attempt_id = a.id
                  AND j.thread_id = m.thread_id AND j.state = 'running'
                  AND (m.admitted_job_id IS NULL OR m.admitted_job_id = $6)
                RETURNING m.thread_id
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                job_id,
            )
            if shared is None:
                return False
            await self._capture_attempt_ledger(conn, thread_id=thread_id, attempt_id=attempt_id)
            await conn.execute(
                """
                UPDATE agent_sandboxes g
                SET idle_deadline = NULL, last_activity_at = NOW(), updated_at = NOW()
                FROM agent_thread_sandbox_memberships m
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                """,
                thread_id,
            )
            return True

    async def settle_thread_sandbox(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        job_id: str,
        context_status: str,
        native_session_id: str | None,
        idle_timeout_seconds: float,
        base_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Close one turn boundary and arm deactivation without deleting state."""
        async with self._pool.acquire() as conn, conn.transaction():
            # Match create_follow_up and fork finalization: thread -> job ->
            # membership. Taking the membership first while a concurrent send
            # held the job produced a real PostgreSQL deadlock cycle.
            locked_thread = await conn.fetchval(
                "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE",
                thread_id,
            )
            if locked_thread is None:
                return None
            locked_job = await conn.fetchval(
                "SELECT id FROM agent_jobs WHERE id = $1 AND thread_id = $2 FOR UPDATE",
                job_id,
                thread_id,
            )
            if locked_job is None:
                return None
            # A committed settlement can lose only its HTTP acknowledgement.
            # Return that same boundary without incrementing workspace_epoch or
            # creating another snapshot. The original attempt must still own
            # a live fence; a retry cannot resurrect a superseded activation.
            # No sandbox-mode predicate: the rewritten schema has no ``mode``
            # column, and the retained shared-session topology is unconditional,
            # so every sandbox row is a shared-live one.
            replay = await conn.fetchval(
                """
                SELECT m.thread_id
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                JOIN agent_attempts a ON a.id = $2
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE m.thread_id = $1
                  AND g.generation = $4 AND m.activation_generation = $5
                  AND m.active_attempt_id IS NULL
                  AND m.admitted_job_id = $6 AND m.last_settled_job_id = $6
                  AND m.native_state = 'ready' AND m.context_status = $7
                  AND ($8::text IS NULL OR m.native_session_id = $8)
                  AND a.lease_generation = $3 AND a.job_id = $6
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                  AND j.thread_id = m.thread_id AND j.current_attempt_id = a.id
                  AND j.state = 'running'
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                job_id,
                context_status,
                native_session_id,
            )
            if replay is not None:
                row = await conn.fetchrow(
                    f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                    thread_id,
                )
                return _sandbox_row_to_dict(row)
            shared = await conn.fetchrow(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET active_attempt_id = NULL, last_settled_job_id = $6,
                    context_status = $7,
                    native_session_id = COALESCE($8, m.native_session_id),
                    native_state = 'ready', resident_state = 'live',
                    last_error = NULL, updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND g.generation = $4
                  AND m.activation_generation = $5
                  AND m.active_attempt_id = $2
                  AND m.admitted_job_id = $6
                  AND a.id = $2 AND a.lease_generation = $3
                  AND a.job_id = $6 AND a.status = 'running'
                  AND a.lease_expires_at > NOW()
                RETURNING m.sandbox_id
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                job_id,
                context_status,
                native_session_id,
            )
            if shared is None:
                return None
            native_turn_no = await conn.fetchval(
                """
                UPDATE agent_jobs current_job
                SET native_turn_no = COALESCE(
                    current_job.native_turn_no,
                    (
                        SELECT COALESCE(MAX(previous.native_turn_no), 0) + 1
                        FROM agent_jobs previous
                        WHERE previous.thread_id = $1
                          AND previous.turn_no < current_job.turn_no
                    )
                )
                WHERE current_job.id = $2 AND current_job.thread_id = $1
                RETURNING native_turn_no
                """,
                thread_id,
                job_id,
            )
            if native_turn_no is None:
                raise ValueError("shared workspace settlement has no native turn ordinal")
            if base_sha is None:
                base_sha = await conn.fetchval(
                    "SELECT base_sha FROM agent_jobs WHERE id = $1",
                    job_id,
                )
            group = await conn.fetchrow(
                """
                    UPDATE agent_sandboxes g
                    SET state = 'running', last_activity_at = NOW(),
                        idle_timeout_seconds = $2,
                        idle_deadline = CASE
                            WHEN g.active_operation_count = 0
                             AND NOT EXISTS (
                                SELECT 1
                                FROM agent_thread_sandbox_memberships peer
                                WHERE peer.sandbox_id = g.id
                                  AND peer.active_attempt_id IS NOT NULL
                             )
                            THEN NOW() + make_interval(secs => $2)
                            ELSE NULL
                        END,
                        workspace_epoch = workspace_epoch + 1,
                        published_base_sha = COALESCE(g.published_base_sha, $3),
                        snapshot_state = CASE
                            WHEN g.snapshot_state IN ('snapshotting', 'importing', 'dirty')
                                THEN g.snapshot_state
                            ELSE 'pending'
                        END,
                        updated_at = NOW()
                    WHERE g.id = $1
                    RETURNING g.workspace_epoch, g.published_base_sha
                    """,
                shared["sandbox_id"],
                idle_timeout_seconds,
                base_sha,
            )
            if group is None or not group["published_base_sha"]:
                raise ValueError("shared workspace settlement has no resolved Git base")
            pending = await conn.fetchrow(
                """
                    SELECT id FROM agent_sandbox_snapshots
                    WHERE sandbox_id = $1 AND state = 'pending'
                    FOR UPDATE
                    """,
                shared["sandbox_id"],
            )
            # The anchor must be one of the included jobs — a deferred foreign
            # key checks that at commit, so the snapshot row and its
            # membership row land in this same transaction, in either order.
            if pending is None:
                snapshot_id = await conn.fetchval(
                    """
                    INSERT INTO agent_sandbox_snapshots (
                        sandbox_id, workspace_epoch, anchor_job_id, base_sha
                    )
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    shared["sandbox_id"],
                    group["workspace_epoch"],
                    job_id,
                    group["published_base_sha"],
                )
            else:
                snapshot_id = pending["id"]
                await conn.execute(
                    """
                    UPDATE agent_sandbox_snapshots
                    SET workspace_epoch = $2, anchor_job_id = $3, updated_at = NOW()
                    WHERE id = $1 AND state = 'pending'
                    """,
                    snapshot_id,
                    group["workspace_epoch"],
                    job_id,
                )
            await conn.execute(
                """
                INSERT INTO agent_snapshot_jobs (snapshot_id, job_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                snapshot_id,
                job_id,
            )
            row = await conn.fetchrow(
                f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                thread_id,
            )
        return None if row is None else _sandbox_row_to_dict(row)

    async def reconcile_thread_sandbox_observation(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
        running: bool,
        activity_active: bool | None = None,
        idle_remaining_seconds: float | None = None,
        members: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Reconcile one authenticated shared-group Docker observation."""
        return await self._reconcile_shared_sandbox_observation(
            thread_id=thread_id,
            host=host,
            sandbox_id=sandbox_id,
            container_id=container_id,
            sandbox_generation=sandbox_generation,
            running=running,
            activity_active=activity_active,
            idle_remaining_seconds=idle_remaining_seconds,
            members=members or [],
        )

    async def _reconcile_shared_sandbox_observation(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
        running: bool,
        activity_active: bool | None,
        idle_remaining_seconds: float | None,
        members: list[dict[str, Any]],
    ) -> str | None:
        """Join one Docker group observation with independently fenced members."""
        async with self._pool.acquire() as conn, conn.transaction():
            # The stopped path updates every durable member, including one a
            # partial host census omitted. Lock the complete bounded group in
            # deterministic order before its sandbox row. Settlement,
            # admission, fork, and terminal writers use the same membership
            # -> sandbox order; taking only reported members would recreate
            # the inverse order when the bulk stopped-state update runs.
            await conn.fetch(
                """
                SELECT thread_id
                FROM agent_thread_sandbox_memberships
                WHERE sandbox_id = $1
                ORDER BY thread_id
                FOR UPDATE
                """,
                sandbox_id,
            )
            group = await conn.fetchrow(
                """
                SELECT * FROM agent_sandboxes
                WHERE id = $1 AND origin_thread_id = $2 AND host = $3
                  AND generation = $4
                FOR UPDATE
                """,
                sandbox_id,
                thread_id,
                host,
                sandbox_generation,
            )
            if group is None:
                return None
            if group["container_id"] not in {None, container_id}:
                return None
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET container_id = COALESCE(container_id, $2), updated_at = NOW()
                WHERE id = $1
                """,
                sandbox_id,
                container_id,
            )
            for member in members[:128]:
                member_thread = str(member.get("thread_id") or "")
                if not member_thread:
                    continue
                observed_activation = int(member.get("activation_generation") or 0)
                observed_process = int(member.get("process_generation") or 0)
                observed_residency = int(member.get("residency_generation") or 0)
                resident_state = str(member.get("resident_state") or "stopped")
                if resident_state not in {"stopped", "starting", "live", "restarting", "lost"}:
                    continue
                # A heartbeat may confirm an equal/newer local process state,
                # but cannot move a durable activation fence backwards or
                # overwrite a process belonging to a newer residency.
                await conn.execute(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET process_generation = CASE
                            WHEN native_state = 'lost' OR context_status = 'lost'
                                THEN process_generation
                            ELSE GREATEST(process_generation, $3)
                        END,
                        resident_state = CASE
                            WHEN native_state = 'lost' OR context_status = 'lost'
                                THEN 'lost'
                            WHEN residency_generation <= $4
                             AND process_generation <= $3
                             AND activation_generation <= $2 THEN $5
                            ELSE resident_state
                        END,
                        updated_at = NOW()
                    WHERE thread_id = $1 AND sandbox_id = $6
                    """,
                    member_thread,
                    observed_activation,
                    observed_process,
                    observed_residency,
                    resident_state,
                    sandbox_id,
                )

            active_member = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM agent_thread_sandbox_memberships
                    WHERE sandbox_id = $1 AND active_attempt_id IS NOT NULL
                )
                """,
                sandbox_id,
            )
            # A host heartbeat is an observation sampled before this database
            # transaction.  It may say "locally idle" just before a native
            # fork/snapshot coordinator increments its durable operation
            # fence.  Never let that stale sample re-arm an idle deadline in
            # the middle of the externally visible mutation.
            durable_activity = (
                bool(active_member)
                or int(group["active_operation_count"]) > 0
                or (group["snapshot_state"] == "snapshotting")
            )
            # Docker creation can outlive the runner that requested it. An
            # explicit pre-admission abandon leaves the group PROVISIONING
            # with no active member; if Docker reports that exact labelled
            # object afterwards, it is an orphan to stop, not a fresh active
            # group to adopt. This recovery fence prevents a broker-local
            # lease from keeping an unowned container resident forever.
            if group["state"] == "provisioning" and not durable_activity:
                recovered = await conn.fetchval(
                    """
                    UPDATE agent_sandboxes
                    SET state = CASE WHEN $2 THEN 'stopping' ELSE 'stopped' END,
                        idle_deadline = NULL, updated_at = NOW()
                    WHERE id = $1
                    RETURNING state
                    """,
                    sandbox_id,
                    running,
                )
                return None if recovered is None else str(recovered)
            if running:
                deadline_sql = "idle_deadline"
                deadline_arg: float | None = None
                if activity_active is True or durable_activity:
                    deadline_sql = "NULL"
                elif activity_active is False and idle_remaining_seconds is not None:
                    deadline_sql = "NOW() + make_interval(secs => $2)"
                    deadline_arg = idle_remaining_seconds
                elif activity_active is None:
                    # A broker reconstructed this running container without
                    # its old monotonic clock. Give a group whose durable
                    # deadline was cleared one bounded recovery window, but do
                    # not refresh that window on every host heartbeat. Once a
                    # real terminal/operation closes, the broker reports its
                    # concrete decreasing remainder through the branch above.
                    deadline_sql = (
                        "COALESCE(idle_deadline, "
                        "NOW() + make_interval(secs => idle_timeout_seconds))"
                    )
                row = await conn.fetchrow(
                    f"""
                    UPDATE agent_sandboxes
                    SET state = 'running', idle_deadline = {deadline_sql},
                        updated_at = NOW()
                    WHERE id = $1
                    RETURNING state
                    """,
                    *((sandbox_id, deadline_arg) if deadline_arg is not None else (sandbox_id,)),
                )
                return None if row is None else str(row["state"])

            if active_member:
                return None
            row = await conn.fetchrow(
                """
                UPDATE agent_sandboxes
                SET state = 'stopped', idle_deadline = NULL, updated_at = NOW()
                WHERE id = $1 AND (
                    state IN ('stopping', 'stopped_dirty', 'lost')
                    OR (state = 'running' AND idle_deadline IS NOT NULL
                        AND idle_deadline <= NOW())
                )
                RETURNING state
                """,
                sandbox_id,
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE agent_thread_sandbox_memberships
                SET resident_state = CASE
                        WHEN native_state = 'lost' THEN 'lost' ELSE 'stopped' END,
                    updated_at = NOW()
                WHERE sandbox_id = $1
                """,
                sandbox_id,
            )
            return None if row is None else str(row["state"])

    async def due_thread_sandbox_stop(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
    ) -> dict[str, Any] | None:
        """Return the exact shared-group fence for a due retained stop."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT origin_thread_id AS thread_id, generation,
                       0::bigint AS activation_generation,
                       CASE
                           WHEN state IN ('stopped_dirty', 'lost') THEN 'lost'
                           WHEN state = 'stopping' THEN 'orphaned_attempt'
                           ELSE 'idle'
                       END AS reason,
                       TRUE AS shared_live
                FROM agent_sandboxes g
                WHERE g.id = $1 AND g.origin_thread_id = $2 AND g.host = $3
                  AND g.container_id = $4 AND g.generation = $5
                  AND g.active_operation_count = 0
                  AND g.snapshot_state <> 'snapshotting'
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_thread_sandbox_memberships m
                      WHERE m.sandbox_id = g.id AND m.active_attempt_id IS NOT NULL
                  )
                  AND (
                        (state = 'running' AND idle_deadline IS NOT NULL
                            AND idle_deadline <= NOW())
                        OR state IN ('stopping', 'stopped_dirty', 'lost')
                  )
                """,
                sandbox_id,
                thread_id,
                host,
                container_id,
                sandbox_generation,
            )
        return None if row is None else dict(row)

    async def abandon_thread_sandbox(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        lost: bool,
        detail: str | None,
        idle_timeout_seconds: float,
    ) -> dict[str, Any] | None:
        """Release a failed shared-member activation without replaying it."""
        async with self._pool.acquire() as conn, conn.transaction():
            shared = await conn.fetchrow(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET active_attempt_id = NULL,
                    context_status = CASE WHEN $6 THEN 'lost' ELSE m.context_status END,
                    native_state = CASE WHEN $6 THEN 'lost' ELSE m.native_state END,
                    resident_state = CASE WHEN $6 THEN 'lost' ELSE 'restarting' END,
                    last_error = $7, updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND g.generation = $4
                  AND m.activation_generation = $5
                  AND m.active_attempt_id = $2
                  AND (
                        ($6 AND (m.admitted_job_id IS NULL OR m.admitted_job_id = a.job_id))
                        OR (NOT $6 AND m.admitted_job_id IS NULL)
                  )
                  AND a.id = $2 AND a.lease_generation = $3
                RETURNING m.sandbox_id
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                lost,
                detail,
            )
            if shared is None:
                return None
            await conn.execute(
                """
                UPDATE agent_sandboxes g
                SET state = CASE
                        WHEN $3 AND g.container_id IS NULL THEN 'lost'
                        WHEN g.container_id IS NULL THEN 'provisioning'
                        WHEN g.active_operation_count > 0 OR EXISTS (
                            SELECT 1
                            FROM agent_thread_sandbox_memberships peer
                            WHERE peer.sandbox_id = g.id
                              AND peer.active_attempt_id IS NOT NULL
                        ) THEN 'running'
                        WHEN EXISTS (
                            SELECT 1
                            FROM agent_thread_sandbox_memberships peer
                            WHERE peer.sandbox_id = g.id
                              AND peer.thread_id <> $5
                              AND peer.context_status <> 'lost'
                              AND peer.native_state <> 'lost'
                        ) THEN 'running'
                        ELSE 'stopping'
                    END,
                    last_activity_at = NOW(),
                    idle_timeout_seconds = $2,
                    idle_deadline = CASE
                        WHEN g.container_id IS NOT NULL
                         AND g.active_operation_count = 0
                         AND NOT EXISTS (
                            SELECT 1
                            FROM agent_thread_sandbox_memberships peer
                            WHERE peer.sandbox_id = g.id
                              AND peer.active_attempt_id IS NOT NULL
                         )
                         AND EXISTS (
                            SELECT 1
                            FROM agent_thread_sandbox_memberships peer
                            WHERE peer.sandbox_id = g.id
                              AND peer.thread_id <> $5
                              AND peer.context_status <> 'lost'
                              AND peer.native_state <> 'lost'
                         )
                        THEN NOW() + make_interval(secs => $2)
                        ELSE NULL
                    END,
                    last_error = CASE WHEN $3 THEN $4 ELSE g.last_error END,
                    updated_at = NOW()
                WHERE g.id = $1
                """,
                shared["sandbox_id"],
                idle_timeout_seconds,
                lost,
                detail,
                thread_id,
            )
            if lost:
                await conn.execute(
                    "UPDATE agent_attempts SET context_status = 'lost' "
                    "WHERE id = $1 AND lease_generation = $2",
                    attempt_id,
                    lease_generation,
                )
                await self._fail_unrunnable_follow_ups(conn, thread_id=thread_id)
            current = await conn.fetchrow(
                f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                thread_id,
            )
        return None if current is None else _sandbox_row_to_dict(current)

    async def thread_sandbox(self, *, thread_id: str) -> dict[str, Any] | None:
        """Return the durable local sandbox mapping, including STOPPED/LOST."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                thread_id,
            )
        if row is None:
            return None
        result = dict(row)
        result["group_members"] = load_json(result.get("group_members")) or []
        return result

    # ── Thread residency (design §7, issue #57) ────────────────────────
    #
    # One row per thread, never deleted, generation strictly increasing —
    # see migration 0009 for why deletion is forbidden. Every mutation is
    # generation-scoped so a late writer from a previous residency changes
    # nothing: heartbeats cannot re-promote, releases cannot end a successor.

    async def establish_residency(
        self,
        *,
        thread_id: str,
        host: str,
        runtime_id: str,
        runtime_version: str | None,
        ttl_seconds: float,
        sandbox_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Take (or rotate) a chat's residency; ``None`` = not earned.

        Residency is something a host earns by *currently* running the
        thread's turn — attempt on this host, ``running``, lease unexpired —
        and the check shares a transaction and a row lock with the write, so
        a finish or a supersede cannot slip between them, and an expired
        attempt the reaper has not reached yet earns nothing. The lease lives
        on the chat's membership row: its generation only ever increments,
        and every rotation clears the grant so the caller revokes the
        predecessor's (``previous_grant_id``) and mints anew.

        ``runtime_id`` and ``runtime_version`` are accepted for the worker
        contract and not stored: the sandbox the membership belongs to is the
        authority on what runs in it.
        """
        del runtime_id, runtime_version
        async with self._pool.acquire() as conn, conn.transaction():
            earns = await conn.fetchrow(
                """
                SELECT j.id AS job_id, j.user_id, j.model, a.id AS attempt_id
                FROM agent_jobs j
                JOIN agent_attempts a ON a.id = j.current_attempt_id
                WHERE j.thread_id = $1 AND a.host_id = $2
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                LIMIT 1
                FOR SHARE OF a
                """,
                thread_id,
                host,
            )
            if earns is None:
                return None
            # Attempt, then membership: the order every admission writer keeps.
            member = await conn.fetchrow(
                """
                SELECT m.residency_grant_id, g.host, g.runtime_id, g.runtime_version
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1 AND g.host = $2
                  AND ($3::text IS NULL OR m.sandbox_id = $3)
                FOR UPDATE OF m
                """,
                thread_id,
                host,
                sandbox_id,
            )
            if member is None:
                return None
            row = await conn.fetchrow(
                """
                UPDATE agent_thread_sandbox_memberships
                SET residency_generation = residency_generation + 1,
                    residency_status = 'hot',
                    residency_lease_expires_at = NOW() + make_interval(secs => $2),
                    residency_grant_id = NULL,
                    residency_binding_id = NULL,
                    residency_established_at = NOW(),
                    residency_last_active_at = NOW(),
                    updated_at = NOW()
                WHERE thread_id = $1
                RETURNING thread_id, sandbox_id,
                          residency_generation AS generation,
                          residency_status AS status,
                          residency_lease_expires_at AS lease_expires_at,
                          residency_grant_id AS grant_id,
                          residency_binding_id AS ledger_binding_id,
                          residency_established_at AS established_at,
                          residency_last_active_at AS last_active_at
                """,
                thread_id,
                ttl_seconds,
            )
        result = dict(row)
        result["host"] = member["host"]
        result["runtime_id"] = member["runtime_id"]
        result["runtime_version"] = member["runtime_version"]
        result["previous_grant_id"] = member["residency_grant_id"]
        result["establishing_job_id"] = earns["job_id"]
        result["establishing_attempt_id"] = earns["attempt_id"]
        result["user_id"] = earns["user_id"]
        result["model"] = earns["model"]
        return result

    async def link_residency_grant(
        self,
        *,
        thread_id: str,
        generation: int,
        grant_id: str,
        binding_id: str | None = None,
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._grants.link_residency_grant(
            thread_id=thread_id, generation=generation, grant_id=grant_id, binding_id=binding_id
        )

    async def heartbeat_residency(
        self, *, thread_id: str, generation: int, ttl_seconds: float, host: str | None = None
    ) -> str | None:
        """Extend a residency's lease — only the generation that holds it.

        Returns ``None`` when nothing was extended (lost/taken over), else the
        residency's grant id — ``""`` when it runs without one — so the caller
        can renew the credential in the same pass. ``host`` narrows the write
        to the machine the row names: a beat is a holder's statement, and a
        guessed (thread, generation) from anywhere else must extend nothing.
        """
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_lease_expires_at = NOW() + make_interval(secs => $3),
                    residency_last_active_at = NOW()
                FROM agent_sandboxes g
                WHERE m.thread_id = $1 AND m.residency_generation = $2
                  AND m.residency_status = 'hot'
                  AND g.id = m.sandbox_id AND ($4::text IS NULL OR g.host = $4)
                RETURNING COALESCE(m.residency_grant_id, '')
                """,
                thread_id,
                generation,
                ttl_seconds,
                host,
            )

    async def release_residency(
        self, *, thread_id: str, generation: int, host: str | None = None
    ) -> str | None:
        """End a residency (teardown/eviction) — only its own generation.

        Returns ``None`` when nothing was released, else the grant id to
        revoke (``""`` when the residency ran without one).
        """
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none', residency_last_active_at = NOW()
                FROM agent_sandboxes g
                WHERE m.thread_id = $1 AND m.residency_generation = $2
                  AND m.residency_status <> 'none'
                  AND g.id = m.sandbox_id AND ($3::text IS NULL OR g.host = $3)
                RETURNING COALESCE(m.residency_grant_id, '')
                """,
                thread_id,
                generation,
                host,
            )

    async def release_shared_residency_if_quiescent(
        self,
        *,
        thread_id: str,
        generation: int,
        process_generation: int,
        host: str,
        sandbox_id: str,
    ) -> str | None:
        """Release a stopped shared member unless a durable transition owns it.

        A host observation is sampled before its heartbeat transaction. Native
        Fork can rotate and mint a new residency after that sample but before
        the heartbeat reaches this method. The group's durable operation fence,
        member attempt, and observed process generation therefore participate
        in the same UPDATE that releases the grant. The membership must also
        have accepted the stopped/lost observation; checking any of these in
        Python would recreate the revoke-after-mint race this method exists to
        prevent.
        """
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none', residency_last_active_at = NOW()
                FROM agent_sandboxes g
                WHERE m.thread_id = $1 AND m.residency_generation = $2
                  AND m.sandbox_id = $4 AND g.id = m.sandbox_id AND g.host = $3
                  AND m.residency_status <> 'none'
                  AND m.process_generation <= $5
                  AND m.resident_state IN ('stopped', 'lost')
                  AND m.active_attempt_id IS NULL
                  AND g.active_operation_count = 0
                  AND g.snapshot_state <> 'snapshotting'
                RETURNING COALESCE(m.residency_grant_id, '')
                """,
                thread_id,
                generation,
                host,
                sandbox_id,
                process_generation,
            )

    async def resident_host(self, *, thread_id: str) -> dict[str, Any] | None:
        """The live hot residency for a thread, or ``None``.

        Live means unexpired: the reaper marks expiry on its own schedule,
        and scheduling decisions between passes must not trust a lease the
        clock has already taken away.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT m.thread_id, m.sandbox_id, g.host, g.runtime_id, g.runtime_version,
                       m.residency_generation AS generation,
                       m.residency_status AS status,
                       m.residency_lease_expires_at AS lease_expires_at,
                       m.residency_grant_id AS grant_id,
                       m.residency_binding_id AS ledger_binding_id
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1 AND m.residency_status = 'hot'
                  AND m.residency_lease_expires_at > NOW()
                """,
                thread_id,
            )
        return None if row is None else dict(row)

    async def reap_expired_residencies(self) -> list[dict[str, Any]]:
        """Mark every hot residency whose lease lapsed as ``none``.

        The control plane's unilateral half of the fence: the host may still
        be running the sandbox, but its generation no longer opens anything —
        not heartbeats, not credentials — and the next turn goes COLD.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none'
                FROM agent_sandboxes g
                WHERE m.residency_status = 'hot' AND m.residency_lease_expires_at <= NOW()
                  AND g.id = m.sandbox_id
                RETURNING m.thread_id, m.residency_generation AS generation, g.host,
                          m.residency_grant_id AS grant_id
                """
            )
        return [dict(row) for row in rows]

    # ── Cancellation ───────────────────────────────────────────────────

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        """Request cancellation; returns the job's resulting state.

        Queued jobs cancel immediately. Running jobs get the flag
        set — the worker observes it on its next heartbeat and performs the
        fenced transition. Terminal jobs are a no-op. ``user_id`` scopes the
        cancel to the owner. Returns ``None`` for an unknown job.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT state FROM agent_jobs WHERE id = $1 "
                "AND ($2::text IS NULL OR user_id = $2) FOR UPDATE",
                job_id,
                user_id,
            )
            if row is None:
                return None
            state = row["state"]
            if state == QUEUED:
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'cancelled', cancel_requested = TRUE, "
                    "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                    job_id,
                )
                await self._fail_discontinuous_follow_ups(conn, parent_job_id=job_id)
                return CANCELLED
            if state == RUNNING:
                await conn.execute(
                    "UPDATE agent_jobs SET cancel_requested = TRUE, updated_at = NOW() "
                    "WHERE id = $1",
                    job_id,
                )
            return state

    # ── Shared-workspace snapshot coordination ─────────────────────────

    async def claim_due_sandbox_snapshot(self) -> dict[str, Any] | None:
        """Fence one quiescent shared workspace for bounded patch capture.

        This claim never waits for publication and never changes a job state.
        The host revalidates quiescence against its live operation ledger; a
        turn that entered between this transaction and the broker call simply
        makes the capture retry later.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT s.id AS snapshot_id, s.sandbox_id, s.workspace_epoch,
                       s.anchor_job_id, s.base_sha, g.generation,
                       g.origin_thread_id, g.container_id, g.container_name,
                       g.runtime_id, g.runtime_version, g.sandbox_image_digest,
                       g.memory_limit, g.cpu_limit, g.stop_timeout_seconds,
                       g.resident_slot_capacity,
                       COALESCE((
                           SELECT jsonb_agg(
                               jsonb_build_object(
                                   'thread_id', m.thread_id,
                                   'native_session_id', m.native_session_id,
                                   'context_status', m.context_status,
                                   'activation_generation', m.activation_generation,
                                   'process_generation', m.process_generation,
                                   'residency_generation', m.residency_generation,
                                   'resident_state', m.resident_state,
                                   'resident_slot_id', m.resident_slot_id,
                                   'last_settled_job_id', m.last_settled_job_id
                               ) ORDER BY m.created_at, m.thread_id
                           )
                           FROM agent_thread_sandbox_memberships m
                           WHERE m.sandbox_id = g.id
                             AND m.resident_slot_id IS NOT NULL
                       ), '[]'::jsonb) AS members
                FROM agent_sandbox_snapshots s
                JOIN agent_sandboxes g ON g.id = s.sandbox_id
                WHERE s.state = 'pending'
                  AND g.state IN ('running', 'stopped')
                  AND g.snapshot_state = 'pending'
                  AND g.active_operation_count = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_thread_sandbox_memberships active
                      WHERE active.sandbox_id = g.id
                        AND active.active_attempt_id IS NOT NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_snapshot_jobs included
                      JOIN agent_jobs included_job ON included_job.id = included.job_id
                      WHERE included.snapshot_id = s.id
                        AND included_job.state NOT IN ('succeeded', 'failed', 'cancelled')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_sandbox_snapshots prior
                      WHERE prior.sandbox_id = s.sandbox_id
                        AND prior.workspace_epoch < s.workspace_epoch
                        AND prior.state <> 'published'
                  )
                ORDER BY s.created_at, s.id
                LIMIT 1
                FOR UPDATE OF s, g SKIP LOCKED
                """
            )
            if row is None:
                return None
            captured = await conn.fetchval(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'capturing', last_error = NULL, updated_at = NOW()
                WHERE id = $1 AND state = 'pending'
                RETURNING id
                """,
                row["snapshot_id"],
            )
            if captured is None:
                return None
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET snapshot_state = 'snapshotting', idle_deadline = NULL,
                    updated_at = NOW()
                WHERE id = $1 AND snapshot_state = 'pending'
                """,
                row["sandbox_id"],
            )
        result = dict(row)
        result["members"] = load_json(result.get("members")) or []
        return result

    async def record_sandbox_snapshot_capture(
        self,
        *,
        snapshot_id: int,
        sandbox_id: str,
        sandbox_generation: int,
        container_id: str,
        workspace_epoch: int,
        base_sha: str,
        patch: str,
        content_sha256: str,
    ) -> str | None:
        """Store one immutable broker receipt and release the capture barrier.

        Returns ``captured`` or ``published`` (for an empty no-op snapshot),
        and ``None`` for a stale/mismatched receipt.
        """
        digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(digest, content_sha256):
            raise ValueError("sandbox snapshot content hash does not match its patch")
        async with self._pool.acquire() as conn, conn.transaction():
            capture = await conn.fetchval(
                """
                SELECT sandbox_id FROM agent_sandbox_snapshots
                WHERE id = $1 AND sandbox_id = $2 AND workspace_epoch = $3
                  AND base_sha = $4 AND state = 'capturing'
                FOR UPDATE
                """,
                snapshot_id,
                sandbox_id,
                workspace_epoch,
                base_sha,
            )
            if capture is None:
                return None
            # Lock and revalidate the exact Docker lifecycle identity before
            # accepting an asynchronous host receipt. A stale capture from a
            # superseded generation must not mark its replacement RUNNING or
            # become that replacement's publication base.
            group = await conn.fetchval(
                """
                SELECT id FROM agent_sandboxes
                WHERE id = $1 AND generation = $2 AND container_id = $3
                  AND snapshot_state = 'snapshotting'
                FOR UPDATE
                """,
                sandbox_id,
                sandbox_generation,
                container_id,
            )
            if group is None:
                return None
            state = "captured" if patch.strip() else "published"
            row = await conn.fetchrow(
                """
                UPDATE agent_sandbox_snapshots
                SET patch_content = $5, content_sha256 = $6, state = $7,
                    published_commit_sha = CASE WHEN $7 = 'published' THEN base_sha END,
                    captured_at = NOW(),
                    published_at = CASE WHEN $7 = 'published' THEN NOW() END,
                    last_error = NULL, updated_at = NOW()
                WHERE id = $1 AND sandbox_id = $2 AND workspace_epoch = $3
                  AND base_sha = $4 AND state = 'capturing'
                RETURNING sandbox_id
                """,
                snapshot_id,
                sandbox_id,
                workspace_epoch,
                base_sha,
                patch,
                content_sha256,
                state,
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE agent_sandboxes g
                SET state = 'running',
                    snapshot_state = CASE
                        WHEN EXISTS (
                            SELECT 1 FROM agent_sandbox_snapshots pending
                            WHERE pending.sandbox_id = g.id AND pending.state = 'pending'
                        ) THEN 'pending' ELSE 'quiescent' END,
                    idle_deadline = CASE
                        WHEN g.active_operation_count = 0
                         AND NOT EXISTS (
                            SELECT 1 FROM agent_thread_sandbox_memberships active
                            WHERE active.sandbox_id = g.id
                              AND active.active_attempt_id IS NOT NULL
                         )
                        THEN NOW() + make_interval(secs => g.idle_timeout_seconds)
                        ELSE NULL END,
                    updated_at = NOW()
                WHERE g.id = $1 AND g.generation = $2 AND g.container_id = $3
                  AND g.snapshot_state = 'snapshotting'
                """,
                sandbox_id,
                sandbox_generation,
                container_id,
            )
        return state

    async def defer_sandbox_snapshot_capture(
        self,
        *,
        snapshot_id: int,
        sandbox_id: str,
        sandbox_generation: int,
        container_id: str,
        detail: str,
        dirty: bool = False,
    ) -> bool:
        """Release a failed host capture; busy retries, unsafe census dirties."""
        next_state = "failed" if dirty else "pending"
        group_state = "dirty" if dirty else "pending"
        async with self._pool.acquire() as conn, conn.transaction():
            claimed_sandbox_id = await conn.fetchval(
                """
                SELECT sandbox_id FROM agent_sandbox_snapshots
                WHERE id = $1 AND state = 'capturing'
                FOR UPDATE
                """,
                snapshot_id,
            )
            if claimed_sandbox_id is None:
                return False
            if str(claimed_sandbox_id) != sandbox_id:
                raise ValueError("snapshot claim belongs to a different sandbox")
            group = await conn.fetchval(
                """
                SELECT id FROM agent_sandboxes
                WHERE id = $1 AND generation = $2 AND container_id = $3
                  AND snapshot_state = 'snapshotting'
                FOR UPDATE
                """,
                sandbox_id,
                sandbox_generation,
                container_id,
            )
            if group is None:
                # The async result belongs to an obsolete Docker identity.
                # Make that immutable snapshot visibly failed, but leave the
                # successor group's lifecycle fields untouched.
                await conn.execute(
                    """
                    UPDATE agent_sandbox_snapshots
                    SET state = 'failed',
                        last_error = 'snapshot capture lost its sandbox lifecycle fence',
                        updated_at = NOW()
                    WHERE id = $1 AND state = 'capturing'
                    """,
                    snapshot_id,
                )
                return False
            await conn.execute(
                """
                UPDATE agent_sandbox_snapshots
                SET state = $2, last_error = $3, updated_at = NOW()
                WHERE id = $1 AND state = 'capturing'
                """,
                snapshot_id,
                next_state,
                detail[:2000],
            )
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET snapshot_state = $2,
                    idle_deadline = CASE WHEN active_operation_count = 0
                         AND NOT EXISTS (
                            SELECT 1 FROM agent_thread_sandbox_memberships active
                            WHERE active.sandbox_id = agent_sandboxes.id
                              AND active.active_attempt_id IS NOT NULL
                         )
                        THEN NOW() + make_interval(secs => idle_timeout_seconds)
                        ELSE NULL END,
                    last_error = CASE WHEN $2 = 'dirty' THEN $3 ELSE last_error END,
                    updated_at = NOW()
                WHERE id = $1 AND generation = $4 AND container_id = $5
                  AND snapshot_state = 'snapshotting'
                """,
                sandbox_id,
                group_state,
                detail[:2000],
                sandbox_generation,
                container_id,
            )
        return True

    async def reap_stalled_sandbox_snapshot_captures(
        self, *, stall_seconds: float = 300.0
    ) -> list[int]:
        """Return abandoned CAPTURING claims to the retryable pending queue."""
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'pending',
                    last_error = 'snapshot coordinator stopped during capture',
                    updated_at = NOW()
                WHERE state = 'capturing'
                  AND updated_at < NOW() - make_interval(secs => $1)
                RETURNING id, sandbox_id
                """,
                stall_seconds,
            )
            if rows:
                await conn.execute(
                    """
                    UPDATE agent_sandboxes
                    SET snapshot_state = 'pending',
                        idle_deadline = CASE
                            WHEN active_operation_count = 0
                             AND NOT EXISTS (
                                SELECT 1 FROM agent_thread_sandbox_memberships active
                                WHERE active.sandbox_id = agent_sandboxes.id
                                  AND active.active_attempt_id IS NOT NULL
                             )
                            THEN NOW() + make_interval(secs => idle_timeout_seconds)
                            ELSE NULL END,
                        updated_at = NOW()
                    WHERE id = ANY($1::text[]) AND snapshot_state = 'snapshotting'
                    """,
                    [row["sandbox_id"] for row in rows],
                )
        return [int(row["id"]) for row in rows]

    # ── Platform-side publishing ───────────────────────────────────────
    #
    # Publishing is driven by the trusted control plane, never by the Agent
    # worker. The host captures a fenced, immutable group snapshot; no sandbox
    # credential can upload or replace it. Snapshot state plus ``SKIP LOCKED``
    # provides exactly-once claiming across control-plane replicas.

    async def claim_for_publish(self) -> dict[str, Any] | None:
        """Take one immutable shared-workspace snapshot.

        Marks it ``publishing`` in the same transaction, so a second publisher
        cannot pick it up. The thread lock is shared with retained-fork
        preparation: a group snapshot may capture a succeeded boundary or a
        publisher may start mutating its lineage, but those operations cannot
        overlap. Returns the snapshot and its publication metadata, or ``None``.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            snapshot = await conn.fetchrow(
                """
                SELECT s.id AS snapshot_id, s.sandbox_id, s.workspace_epoch,
                       s.anchor_job_id AS id, s.base_sha, s.patch_content AS patch,
                       (
                           SELECT COALESCE(
                               jsonb_agg(included.job_id ORDER BY included.created_at, included.job_id),
                               '[]'::jsonb
                           )
                           FROM agent_snapshot_jobs included
                           WHERE included.snapshot_id = s.id
                       ) AS included_job_ids,
                       g.origin_thread_id,
                       g.generation, g.container_id, g.runtime_id,
                       g.runtime_version, g.sandbox_image_digest,
                       g.memory_limit, g.cpu_limit, g.stop_timeout_seconds,
                       g.resident_slot_capacity,
                       COALESCE((
                           SELECT jsonb_agg(
                               jsonb_build_object(
                                   'thread_id', member.thread_id,
                                   'native_session_id', member.native_session_id,
                                   'context_status', member.context_status,
                                   'activation_generation', member.activation_generation,
                                   'process_generation', member.process_generation,
                                   'residency_generation', member.residency_generation,
                                   'resident_state', member.resident_state,
                                   'resident_slot_id', member.resident_slot_id,
                                   'last_settled_job_id', member.last_settled_job_id
                               ) ORDER BY member.created_at, member.thread_id
                           )
                           FROM agent_thread_sandbox_memberships member
                           WHERE member.sandbox_id = g.id
                             AND member.resident_slot_id IS NOT NULL
                       ), '[]'::jsonb) AS members,
                       j.thread_id, j.parent_job_id, j.user_id, t.repo,
                       j.task_prompt, j.metadata,
                       prior.published_pr_url AS parent_pr_url
                FROM agent_sandbox_snapshots s
                JOIN agent_sandboxes g ON g.id = s.sandbox_id
                JOIN agent_jobs j ON j.id = s.anchor_job_id
                JOIN agent_threads t ON t.id = j.thread_id
                LEFT JOIN LATERAL (
                    SELECT previous.published_pr_url
                    FROM agent_sandbox_snapshots previous
                    WHERE previous.sandbox_id = s.sandbox_id
                      AND previous.published_pr_url IS NOT NULL
                    ORDER BY previous.workspace_epoch DESC
                    LIMIT 1
                ) prior ON TRUE
                WHERE s.state = 'captured'
                  AND s.patch_content IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_sandbox_snapshots earlier
                      WHERE earlier.sandbox_id = s.sandbox_id
                        AND earlier.workspace_epoch < s.workspace_epoch
                        AND earlier.state <> 'published'
                  )
                ORDER BY s.created_at, s.id
                LIMIT 1
                FOR UPDATE OF s SKIP LOCKED
                """
            )
            if snapshot is not None:
                claimed = await conn.fetchval(
                    """
                    UPDATE agent_sandbox_snapshots
                    SET state = 'publishing', last_error = NULL, updated_at = NOW()
                    WHERE id = $1 AND state = 'captured'
                    RETURNING id
                    """,
                    snapshot["snapshot_id"],
                )
                if claimed is None:
                    return None
                attempt_id = await conn.fetchval(
                    "SELECT current_attempt_id FROM agent_jobs WHERE id = $1",
                    snapshot["id"],
                )
                if attempt_id is not None:
                    await self._insert_event(
                        conn,
                        job_id=snapshot["id"],
                        attempt_id=attempt_id,
                        event_type="lifecycle",
                        payload={
                            "phase": "shared_snapshot_publishing",
                            "snapshot_id": snapshot["snapshot_id"],
                            "workspace_epoch": snapshot["workspace_epoch"],
                        },
                    )
                return {
                    "publication_kind": "shared_snapshot",
                    "snapshot_id": int(snapshot["snapshot_id"]),
                    "sandbox_id": snapshot["sandbox_id"],
                    "workspace_epoch": int(snapshot["workspace_epoch"]),
                    "included_job_ids": load_json(snapshot["included_job_ids"]) or [],
                    "job_id": snapshot["id"],
                    "anchor_job_id": snapshot["id"],
                    "thread_id": snapshot["thread_id"],
                    "branch_id": snapshot["origin_thread_id"],
                    "origin_thread_id": snapshot["origin_thread_id"],
                    "generation": int(snapshot["generation"]),
                    "container_id": snapshot["container_id"],
                    "runtime_id": snapshot["runtime_id"],
                    "runtime_version": snapshot["runtime_version"],
                    "sandbox_image_digest": snapshot["sandbox_image_digest"],
                    "memory_limit": snapshot["memory_limit"],
                    "cpu_limit": snapshot["cpu_limit"],
                    "stop_timeout_seconds": int(snapshot["stop_timeout_seconds"]),
                    "resident_slot_capacity": int(snapshot["resident_slot_capacity"]),
                    "members": load_json(snapshot["members"]) or [],
                    "parent_job_id": snapshot["parent_job_id"],
                    "parent_pr_url": snapshot["parent_pr_url"],
                    "user_id": snapshot["user_id"],
                    "repo": snapshot["repo"],
                    "base_sha": snapshot["base_sha"],
                    "task_prompt": snapshot["task_prompt"],
                    "metadata": load_json(snapshot["metadata"]),
                    "patch": snapshot["patch"],
                }
            return None

    async def record_snapshot_publish(
        self,
        *,
        snapshot_id: int,
        pr_url: str,
        commit_sha: str,
    ) -> bool:
        """Advance one group lineage and wait for object-only base import.

        Included Agent turns stay succeeded throughout.  Publication is a
        group artifact and must never rewrite a successful model turn as a
        failure merely because GitHub or the broker was unavailable later.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'published', published_pr_url = $2,
                    published_commit_sha = $3, published_at = NOW(),
                    last_error = NULL, updated_at = NOW()
                WHERE id = $1 AND state = 'publishing'
                RETURNING sandbox_id, anchor_job_id
                """,
                snapshot_id,
                pr_url,
                commit_sha,
            )
            if row is None:
                return False
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET published_base_sha = $2, snapshot_state = 'importing',
                    idle_deadline = NULL, updated_at = NOW()
                WHERE id = $1
                """,
                row["sandbox_id"],
                commit_sha,
            )
            await conn.execute(
                """
                UPDATE agent_sandbox_snapshots
                SET base_sha = $2, updated_at = NOW()
                WHERE sandbox_id = $1 AND state = 'pending'
                """,
                row["sandbox_id"],
                commit_sha,
            )
            await conn.execute(
                """
                UPDATE agent_jobs
                SET published_pr_url = COALESCE(published_pr_url, $2),
                    published_commit_sha = COALESCE(published_commit_sha, $3),
                    updated_at = NOW()
                WHERE id IN (
                    SELECT job_id FROM agent_snapshot_jobs WHERE snapshot_id = $1
                ) AND state = 'succeeded'
                """,
                snapshot_id,
                pr_url,
                commit_sha,
            )
            attempt_id = await conn.fetchval(
                "SELECT current_attempt_id FROM agent_jobs WHERE id = $1",
                row["anchor_job_id"],
            )
            if attempt_id is not None:
                await self._insert_event(
                    conn,
                    job_id=row["anchor_job_id"],
                    attempt_id=attempt_id,
                    event_type="lifecycle",
                    payload={
                        "phase": "shared_snapshot_published",
                        "snapshot_id": snapshot_id,
                        "pr_url": pr_url,
                    },
                )
        return True

    async def fail_snapshot_publish(self, *, snapshot_id: int, detail: str) -> None:
        """Retain a rejected group artifact without failing included turns."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'failed', last_error = $2, updated_at = NOW()
                WHERE id = $1 AND state = 'publishing'
                RETURNING sandbox_id, anchor_job_id
                """,
                snapshot_id,
                detail[:2000],
            )
            if row is None:
                return
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET snapshot_state = 'dirty', last_error = $2,
                    idle_deadline = CASE
                        WHEN active_operation_count = 0
                         AND NOT EXISTS (
                            SELECT 1 FROM agent_thread_sandbox_memberships active
                            WHERE active.sandbox_id = agent_sandboxes.id
                              AND active.active_attempt_id IS NOT NULL
                         )
                        THEN NOW() + make_interval(secs => idle_timeout_seconds)
                        ELSE NULL END,
                    updated_at = NOW()
                WHERE id = $1
                """,
                row["sandbox_id"],
                detail[:2000],
            )
            attempt_id = await conn.fetchval(
                "SELECT current_attempt_id FROM agent_jobs WHERE id = $1",
                row["anchor_job_id"],
            )
            if attempt_id is not None:
                await self._insert_event(
                    conn,
                    job_id=row["anchor_job_id"],
                    attempt_id=attempt_id,
                    event_type="error",
                    payload={
                        "phase": "shared_snapshot_publish_rejected",
                        "snapshot_id": snapshot_id,
                        "detail": detail[:2000],
                    },
                )

    async def pending_snapshot_base_import(self) -> dict[str, Any] | None:
        """Return one published group commit whose object import is pending."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT latest.id AS snapshot_id, latest.sandbox_id,
                       latest.anchor_job_id, latest.published_commit_sha,
                       g.origin_thread_id, g.generation, g.container_id,
                       g.runtime_id, g.runtime_version, g.sandbox_image_digest,
                       g.memory_limit, g.cpu_limit, g.stop_timeout_seconds,
                       g.resident_slot_capacity, t.repo, j.user_id,
                       COALESCE((
                           SELECT jsonb_agg(
                               jsonb_build_object(
                                   'thread_id', member.thread_id,
                                   'native_session_id', member.native_session_id,
                                   'context_status', member.context_status,
                                   'activation_generation', member.activation_generation,
                                   'process_generation', member.process_generation,
                                   'residency_generation', member.residency_generation,
                                   'resident_state', member.resident_state,
                                   'resident_slot_id', member.resident_slot_id,
                                   'last_settled_job_id', member.last_settled_job_id
                               ) ORDER BY member.created_at, member.thread_id
                           )
                           FROM agent_thread_sandbox_memberships member
                           WHERE member.sandbox_id = g.id
                             AND member.resident_slot_id IS NOT NULL
                       ), '[]'::jsonb) AS members
                FROM agent_sandboxes g
                JOIN LATERAL (
                    SELECT s.id, s.sandbox_id, s.anchor_job_id,
                           s.published_commit_sha
                    FROM agent_sandbox_snapshots s
                    WHERE s.sandbox_id = g.id AND s.state = 'published'
                      AND s.published_commit_sha = g.published_base_sha
                    ORDER BY s.workspace_epoch DESC
                    LIMIT 1
                ) latest ON TRUE
                JOIN agent_jobs j ON j.id = latest.anchor_job_id
                JOIN agent_threads t ON t.id = j.thread_id
                WHERE g.snapshot_state = 'importing'
                ORDER BY g.updated_at
                LIMIT 1
                """
            )
        if row is None:
            return None
        result = dict(row)
        result["members"] = load_json(result.get("members")) or []
        return result

    async def complete_snapshot_base_import(self, *, snapshot_id: int, commit_sha: str) -> bool:
        """Acknowledge that the exact published commit resolves in Docker."""
        async with self._pool.acquire() as conn, conn.transaction():
            sandbox_id = await conn.fetchval(
                """
                SELECT sandbox_id FROM agent_sandbox_snapshots
                WHERE id = $1 AND state = 'published'
                  AND published_commit_sha = $2
                FOR UPDATE
                """,
                snapshot_id,
                commit_sha,
            )
            if sandbox_id is None:
                return False
            updated = await conn.fetchval(
                """
                UPDATE agent_sandboxes g
                SET state = 'running',
                    snapshot_state = CASE
                        WHEN EXISTS (
                            SELECT 1 FROM agent_sandbox_snapshots pending
                            WHERE pending.sandbox_id = g.id AND pending.state = 'pending'
                        ) THEN 'pending' ELSE 'quiescent' END,
                    last_error = NULL,
                    idle_deadline = CASE
                        WHEN g.active_operation_count = 0
                         AND NOT EXISTS (
                            SELECT 1 FROM agent_thread_sandbox_memberships active
                            WHERE active.sandbox_id = g.id
                              AND active.active_attempt_id IS NOT NULL
                         )
                        THEN NOW() + make_interval(secs => g.idle_timeout_seconds)
                        ELSE NULL END,
                    updated_at = NOW()
                WHERE g.id = $1 AND g.snapshot_state = 'importing'
                  AND g.published_base_sha = $2
                RETURNING g.id
                """,
                sandbox_id,
                commit_sha,
            )
        return updated is not None

    async def record_snapshot_base_import_failure(self, *, snapshot_id: int, detail: str) -> None:
        """Keep an idempotent import retry visible without losing the commit."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_sandbox_snapshots
                SET last_error = $2, updated_at = NOW()
                WHERE id = $1 AND state = 'published'
                """,
                snapshot_id,
                detail[:2000],
            )

    # ── Reaper ─────────────────────────────────────────────────────────

    async def reap_stalled_snapshot_publishes(self, *, stall_seconds: float = 900.0) -> list[int]:
        """Fail ambiguous shared publishes without changing Agent turn state."""
        detail = "the publisher stopped before finishing; manual review required"
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'failed', last_error = $2, updated_at = NOW()
                WHERE state = 'publishing'
                  AND published_pr_url IS NULL
                  AND updated_at < NOW() - make_interval(secs => $1)
                RETURNING id, sandbox_id
                """,
                stall_seconds,
                detail,
            )
            if rows:
                await conn.execute(
                    """
                    UPDATE agent_sandboxes
                    SET snapshot_state = 'dirty', last_error = $2,
                        idle_deadline = CASE
                            WHEN active_operation_count = 0
                             AND NOT EXISTS (
                                SELECT 1 FROM agent_thread_sandbox_memberships active
                                WHERE active.sandbox_id = agent_sandboxes.id
                                  AND active.active_attempt_id IS NOT NULL
                             )
                            THEN NOW() + make_interval(secs => idle_timeout_seconds)
                            ELSE NULL END,
                        updated_at = NOW()
                    WHERE id = ANY($1::text[])
                    """,
                    [row["sandbox_id"] for row in rows],
                    detail,
                )
        return [int(row["id"]) for row in rows]

    async def reap_expired(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Close expired attempts and requeue (or fail) their jobs.

        For each expired ``running`` attempt: mark it ``superseded``, append an
        ``attempt_superseded`` control event, then decide the job's fate —
        requeue while attempts remain, ``cancelled`` if cancellation was
        pending, ``failed`` once ``max_attempts`` is exhausted. Publication is
        a snapshot's lifecycle, not the job's, so no job is ever caught
        mid-publish here. Returns a summary per reaped attempt.
        """
        actions: list[dict[str, Any]] = []
        async with self._pool.acquire() as conn, conn.transaction():
            expired = await conn.fetch(
                """
                SELECT a.id AS attempt_id, a.job_id, a.attempt_no, a.host_id,
                       j.state, j.cancel_requested, j.current_attempt_id,
                       member.thread_id AS sandbox_thread_id,
                       member.sandbox_id,
                       member.active_attempt_id AS sandbox_active_attempt_id,
                       member.admitted_job_id AS sandbox_admitted_job_id,
                       member.last_settled_job_id AS sandbox_last_settled_job_id,
                       member.residency_generation
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                LEFT JOIN (
                    agent_thread_sandbox_memberships member
                    JOIN agent_sandboxes sandbox
                      ON sandbox.id = member.sandbox_id
                ) ON member.thread_id = j.thread_id
                WHERE a.status = 'running' AND a.lease_expires_at < NOW()
                ORDER BY a.id
                FOR UPDATE OF a, j SKIP LOCKED
                """
            )
            for row in expired:
                attempt_id = row["attempt_id"]
                job_id = row["job_id"]
                await conn.execute(
                    "UPDATE agent_attempts SET status = 'superseded', finished_at = NOW() "
                    "WHERE id = $1",
                    attempt_id,
                )
                # The SELECT took this row ``FOR UPDATE`` while it was still
                # ``running``, so this loop body runs once per attempt however
                # many reaper passes overlap — which is what makes the slot
                # release safe here without a second predicate.
                await self._release_host_slot(conn, row["host_id"])
                await self._insert_event(
                    conn,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    event_type=EVENT_ATTEMPT_SUPERSEDED,
                    payload={"attempt_no": row["attempt_no"], "reason": "lease_expired"},
                )
                sandbox_thread_id = row["sandbox_thread_id"]
                sandbox_active_attempt_id = row["sandbox_active_attempt_id"]
                sandbox_admitted_job_id = row["sandbox_admitted_job_id"]
                sandbox_last_settled_job_id = row["sandbox_last_settled_job_id"]
                settled_retained_turn = sandbox_last_settled_job_id == job_id
                admitted_ambiguously = (
                    sandbox_admitted_job_id == job_id and not settled_retained_turn
                )
                owns_retained_activation = sandbox_active_attempt_id == attempt_id
                retained_activation_expired = (
                    admitted_ambiguously or owns_retained_activation
                ) and not settled_retained_turn
                settled_finalization_expired = (
                    settled_retained_turn and sandbox_admitted_job_id == job_id
                )
                cleanup_residency: dict[str, Any] | None = None
                if retained_activation_expired:
                    # The admission receipt identifies a definitely ambiguous
                    # prompt. Even before admission, an expired runner may
                    # have created a container without returning its identity
                    # to the control plane. The preview therefore fails all
                    # expired retained activations closed instead of guessing
                    # that a retry is safe; ordinary CLI/process replacement
                    # at a clean turn boundary remains supported.
                    lost_member = await conn.fetchrow(
                        """
                        UPDATE agent_thread_sandbox_memberships
                        SET native_state = 'lost', context_status = 'lost',
                            resident_state = 'lost',
                            active_attempt_id = CASE
                                WHEN active_attempt_id = $2 THEN NULL
                                ELSE active_attempt_id
                            END,
                            admitted_job_id = CASE
                                WHEN admitted_job_id = $1 THEN NULL
                                ELSE admitted_job_id
                            END,
                            admitted_at = CASE
                                WHEN admitted_job_id = $1 THEN NULL
                                ELSE admitted_at
                            END,
                            last_error = CASE
                                WHEN admitted_job_id = $1
                                    THEN 'attempt lease expired after prompt admission'
                                ELSE 'retained activation lease expired'
                            END,
                            updated_at = NOW()
                        WHERE thread_id = $3 AND sandbox_id = $4
                          AND last_settled_job_id IS DISTINCT FROM $1
                          AND (active_attempt_id = $2 OR admitted_job_id = $1)
                        RETURNING residency_generation
                        """,
                        job_id,
                        attempt_id,
                        sandbox_thread_id,
                        row["sandbox_id"],
                    )
                    if lost_member is not None:
                        await conn.execute(
                            """
                            UPDATE agent_sandboxes g
                            SET state = CASE
                                    WHEN g.container_id IS NULL THEN 'lost'
                                    WHEN g.active_operation_count > 0 OR EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.active_attempt_id IS NOT NULL
                                    ) THEN 'running'
                                    WHEN EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.context_status <> 'lost'
                                          AND peer.native_state <> 'lost'
                                    ) THEN 'running'
                                    ELSE 'stopping'
                                END,
                                last_activity_at = NOW(),
                                idle_deadline = CASE
                                    WHEN g.container_id IS NOT NULL
                                     AND g.active_operation_count = 0
                                     AND NOT EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.active_attempt_id IS NOT NULL
                                     )
                                     AND EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.context_status <> 'lost'
                                          AND peer.native_state <> 'lost'
                                     )
                                    THEN NOW() + make_interval(
                                        secs => g.idle_timeout_seconds
                                    )
                                    ELSE NULL
                                END,
                                updated_at = NOW()
                            WHERE g.id = $1
                            """,
                            row["sandbox_id"],
                        )
                        residency_generation = int(lost_member["residency_generation"] or 0)
                        if residency_generation > 0:
                            released = await conn.fetchrow(
                                """
                                UPDATE agent_thread_sandbox_memberships m
                                SET residency_status = 'none',
                                    residency_last_active_at = NOW()
                                FROM agent_sandboxes g
                                WHERE m.thread_id = $1 AND m.residency_generation = $2
                                  AND m.residency_status <> 'none'
                                  AND g.id = m.sandbox_id
                                RETURNING m.thread_id,
                                          m.residency_generation AS generation,
                                          g.host, m.residency_grant_id AS grant_id
                                """,
                                sandbox_thread_id,
                                residency_generation,
                            )
                            if released is not None:
                                cleanup_residency = dict(released)
                    await conn.execute(
                        "UPDATE agent_attempts SET context_status = 'lost' WHERE id = $1",
                        attempt_id,
                    )
                    await self._fail_unrunnable_follow_ups(
                        conn,
                        thread_id=sandbox_thread_id,
                    )
                elif settled_finalization_expired:
                    # The native context boundary is durable; only the final
                    # job transition was lost. Never replay this prompt and
                    # never relabel its usable context LOST. Close admission,
                    # preserve the settled idle deadline, and fail the job for
                    # explicit operator/user review.
                    await conn.execute(
                        """
                        UPDATE agent_thread_sandbox_memberships
                        SET active_attempt_id = CASE
                                WHEN active_attempt_id = $2 THEN NULL
                                ELSE active_attempt_id
                            END,
                            admitted_job_id = CASE
                                WHEN admitted_job_id = $1 THEN NULL
                                ELSE admitted_job_id
                            END,
                            admitted_at = CASE
                                WHEN admitted_job_id = $1 THEN NULL
                                ELSE admitted_at
                            END,
                            last_error = 'job finalization expired after context settlement',
                            updated_at = NOW()
                        WHERE thread_id = $3
                          AND last_settled_job_id = $1
                        """,
                        job_id,
                        attempt_id,
                        sandbox_thread_id,
                    )
                if row["current_attempt_id"] != attempt_id or row["state"] in TERMINAL_STATES:
                    # Stale attempt of an already-moved-on job: closing it is enough.
                    action = "closed_stale"
                elif retained_activation_expired:
                    action = CANCELLED if row["cancel_requested"] else FAILED
                    await conn.execute(
                        """
                        UPDATE agent_jobs
                        SET state = $2,
                            detail = $3,
                            terminal_resume_pending = TRUE,
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        job_id,
                        action,
                        (
                            "cancelled after an indeterminate retained Agent CLI turn"
                            if row["cancel_requested"]
                            else (
                                "retained Agent CLI turn became indeterminate after prompt admission"
                                if admitted_ambiguously
                                else "retained sandbox activation lease expired; context marked lost"
                            )
                        ),
                    )
                elif settled_finalization_expired:
                    action = CANCELLED if row["cancel_requested"] else FAILED
                    await conn.execute(
                        """
                        UPDATE agent_jobs
                        SET state = $2,
                            detail = $3,
                            terminal_resume_pending = TRUE,
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        job_id,
                        action,
                        (
                            "cancelled after the Agent CLI turn settled"
                            if row["cancel_requested"]
                            else (
                                "Agent CLI context settled, but job finalization expired; "
                                "the prompt was not replayed"
                            )
                        ),
                    )
                elif row["cancel_requested"]:
                    action = CANCELLED
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'cancelled', "
                        "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                        job_id,
                    )
                elif await self._attempts_spent(conn, job_id) >= max_attempts:
                    action = FAILED
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'failed', detail = $2, "
                        "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                        job_id,
                        f"exhausted {max_attempts} attempts (lease expired)",
                    )
                else:
                    action = QUEUED
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() WHERE id = $1",
                        job_id,
                    )
                if action in TERMINAL_STATES and not retained_activation_expired:
                    # A terminal reaper outcome is semantically the same turn
                    # boundary as ``transition``. Keep the linear chat moving
                    # when the context is usable; the LOST branch above has
                    # already failed every unrunnable descendant instead.
                    await self._fail_discontinuous_follow_ups(conn, parent_job_id=job_id)
                action_result: dict[str, Any] = {
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "action": action,
                }
                if cleanup_residency is not None:
                    action_result["cleanup_residency"] = cleanup_residency
                actions.append(action_result)
        return actions

    # ── Inference grants (E9) ──────────────────────────────────────────
    #
    # The grant itself lives at the gateway; what lives here is the *link*
    # between an attempt and the capability minted for it, and that link is the
    # fence. After the split the gateway holds no attempt or lease state at
    # all, so nothing there can notice that an attempt was superseded — this
    # store is the only place that knows, which is why renewal reads from here
    # rather than from a timer.

    async def record_attempt_grant(
        self, *, attempt_id: int, lease_generation: int, grant_id: str
    ) -> bool:
        """Delegates to the repository that implements this capability."""
        return await self._grants.record_attempt_grant(
            attempt_id=attempt_id, lease_generation=lease_generation, grant_id=grant_id
        )

    async def list_renewable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._grants.list_renewable_grants(limit=limit)

    async def take_revocable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Delegates to the repository that implements this capability."""
        return await self._grants.take_revocable_grants(limit=limit)

    # ── Admin audit trail (E7) ─────────────────────────────────────────

    async def record_admin_action(
        self,
        *,
        action: str,
        actor_id: str,
        admin_ip: str,
        target_user_id: str | None = None,
        details: dict[str, Any] | None = None,
        success: bool = True,
    ) -> int:
        """Delegates to the repository that implements this capability."""
        return await self._audit.record_admin_action(
            action=action,
            actor_id=actor_id,
            admin_ip=admin_ip,
            target_user_id=target_user_id,
            details=details,
            success=success,
        )
