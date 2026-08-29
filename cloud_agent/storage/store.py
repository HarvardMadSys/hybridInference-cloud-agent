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
  Every write from a worker (heartbeat, events, artifacts, state transitions,
  publish) is compare-and-swapped against ``(attempt_id, lease_generation,
  status='running')`` so a zombie worker whose lease was reaped can never
  write again — the split-brain scenario is structurally impossible.
- **Reaper closes, never resets** — an expired attempt is marked
  ``superseded`` (with an ``attempt_superseded`` control event) and the job is
  requeued as a *new* attempt; history is append-only.
- **Publish is the critical fencing point** — the only external side effect.
  ``running -> publishing`` is a fenced one-shot transition and
  ``complete_publish`` additionally requires ``published_pr_url IS NULL``, so
  at most one attempt can ever publish. A job whose lease expires while
  ``publishing`` is failed (not requeued): the publisher may already have
  created the branch/PR, and re-publishing automatically could duplicate it.
- **Events** carry a global ``BIGSERIAL`` id (the SSE ``Last-Event-ID``
  cursor) plus a per-attempt ``seq`` with ``UNIQUE(attempt_id, seq)``. UI
  rewind after a retry is an ``attempt_superseded`` control event plus the new
  attempt's events — old events are never deleted or rewritten.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import asyncpg

from cloud_agent.execution_profiles import (
    KATA_RUNTIME,
    ExecutionProfile,
    default_execution_profile,
)
from cloud_agent.host_pool import (
    CLAIMABLE_STATUSES,
    HOST_DISABLED,
    HOST_ENROLLING,
    HOST_OFFLINE,
    HOST_ONLINE,
    normalize_capabilities,
    transition_allowed,
)
from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)

# Job lifecycle. ``cancel_requested`` is a flag orthogonal to ``state``: it is
# delivered to the running worker via the heartbeat response, and the worker
# performs the fenced transition to ``cancelled``.
QUEUED = "queued"
WAITING = "waiting"
RUNNING = "running"
PUBLISHING = "publishing"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATES = (SUCCEEDED, FAILED, CANCELLED)

# Attempt lifecycle.
ATTEMPT_RUNNING = "running"
ATTEMPT_SUPERSEDED = "superseded"
ATTEMPT_FINISHED = "finished"

# Control event appended by the reaper when it supersedes an attempt.
EVENT_ATTEMPT_SUPERSEDED = "attempt_superseded"
EVENT_ATTEMPT_ABORTED = "attempt_aborted"

# An attempt the platform gave up on before the agent ever started — the job
# never got its turn, so it must not count toward the retry budget.
ATTEMPT_ABORTED = "aborted"


def _new_job_id() -> str:
    """Generate a new agent job id."""
    return f"ajob_{secrets.token_hex(8)}"


def _new_thread_id() -> str:
    """Generate a stable conversation id shared by a thread's runs."""
    return f"athr_{secrets.token_hex(8)}"


def _new_sandbox_id() -> str:
    """Generate the platform identity of one retained Docker sandbox."""
    return f"asbx_{secrets.token_hex(16)}"


def _new_sandbox_fork_id() -> str:
    """Generate the idempotency key for one database/Docker fork saga."""
    return f"afork_{secrets.token_hex(16)}"


def _load_json(value: Any) -> Any:
    """Decode a JSONB column that asyncpg may hand back as str or object."""
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to decode agent job JSON column", exc_info=True)
            return None
    return value


def resolve_runtime_version(
    *,
    runtime: str,
    thread_version: str | None,
    selected: dict[str, Any],
    available: dict[str, Any],
    host_default: dict[str, Any],
) -> str | None:
    """Which CLI version this attempt runs, in priority order.

    1. **The version its thread already runs.** A thread is a live harness
       session, and ``checkpoints.gate_for_claim`` refuses to restore one whose
       exported ``driver_version`` differs from the attempt's stamp — silently,
       into replay. So an admin changing the selection must not reach into
       conversations already in flight: it applies to new threads, and existing
       ones keep the version they started on.
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
        "user_id": row["user_id"],
        "repo": row["repo"],
        "base_sha": row["base_sha"],
        "task_prompt": row["task_prompt"],
        "setup_script": row["setup_script"],
        "runtime": row["runtime"],
        "model": row["model"],
        "execution_profile": row["execution_profile"],
        "reasoning_effort": row["reasoning_effort"],
        "state": row["state"],
        "cancel_requested": row["cancel_requested"],
        "terminal_resume_pending": row["terminal_resume_pending"],
        "current_attempt_id": row["current_attempt_id"],
        "published_pr_url": row["published_pr_url"],
        "published_commit_sha": row["published_commit_sha"],
        "detail": row["detail"],
        "metadata": _load_json(row["metadata"]),
        "fork_source_job_id": row["fork_source_job_id"],
        # Resolved once, at creation, against the deployment registry — not
        # re-read per attempt. A job runs with the tool surface it was created
        # with, so removing a server from the registry stops new jobs from
        # asking for it without changing what a running one was promised.
        "mcp_servers": list(row["mcp_servers"] or []),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "pinned_at": row.get("pinned_at"),
        "thread_sandbox": _load_json(row.get("thread_sandbox")),
    }


_JOB_COLUMNS = (
    "id, thread_id, parent_job_id, turn_no, user_id, repo, base_sha, task_prompt, "
    "setup_script, runtime, model, execution_profile, reasoning_effort, state, "
    "cancel_requested, terminal_resume_pending, current_attempt_id, "
    "published_pr_url, published_commit_sha, detail, "
    "metadata, fork_source_job_id, mcp_servers, "
    "created_at, updated_at"
)
_QUALIFIED_JOB_COLUMNS = ", ".join(f"j.{column.strip()}" for column in _JOB_COLUMNS.split(","))

#: How long a queued turn is reserved for its thread's resident host before
#: any host may claim it (design §7). Long enough to survive a busy runner's
#: poll cycle; short enough that a vanished host costs one COLD start, not a
#: stuck queue.
RESIDENCY_AFFINITY_HOLD_S = 30.0

# Everything a caller may see about a host. ``credential_hash`` is deliberately
# not in it — reading it back out would make the row as good as the credential
# — and is reported only as the boolean ``enrolled``.
_HOST_COLUMN_NAMES = (
    "host",
    "last_worker_id",
    "first_seen_at",
    "last_seen_at",
    "status",
    "labels",
    "capabilities",
    "slots_total",
    "slots_used",
    "runner_version",
    "broker_url",
    "last_heartbeat_at",
    "sandbox_runtime",
    "sandbox_image_digest",
    "runtime_versions",
    "runtime_versions_available",
)
_HOST_COLUMNS = ", ".join(_HOST_COLUMN_NAMES) + ", (credential_hash IS NOT NULL) AS enrolled"
# The same list qualified with ``h.``. Required wherever the query joins
# ``agent_attempts``, which has its own ``status`` and ``host_id``: unqualified,
# Postgres rejects the whole statement as ambiguous.
_QUALIFIED_HOST_COLUMNS = (
    ", ".join(f"h.{column}" for column in _HOST_COLUMN_NAMES)
    + ", (h.credential_hash IS NOT NULL) AS enrolled"
)


class AgentJobStore:
    """Postgres-backed store for agent jobs, attempts, events, and artifacts."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap a shared asyncpg pool."""
        self._pool = pool

    @staticmethod
    async def _fail_unrunnable_follow_ups(
        conn: Any,
        *,
        thread_id: str,
        detail: str = "not run because an earlier turn lost the retained sandbox context",
    ) -> None:
        """Give queued descendants a terminal answer when continuity is LOST.

        A retained chat cannot be claimed again after its context becomes
        indeterminate. Leaving already-submitted turns in ``waiting`` or
        ``queued`` would therefore create a permanent queue entry rather than
        a recoverable retry. Every LOST transition calls this helper in the
        same transaction that fences the sandbox row.
        """
        await conn.execute(
            """
            UPDATE agent_jobs
            SET state = 'failed', detail = $2, updated_at = NOW()
            WHERE thread_id = $1 AND state IN ('waiting', 'queued')
            """,
            thread_id,
            detail[:2000],
        )

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
        """Record that this user may use this installation."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_repo_grants (user_id, installation_id, account_login)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, installation_id)
                DO UPDATE SET account_login = EXCLUDED.account_login
                """,
                user_id,
                installation_id,
                account_login,
            )

    async def list_repo_grants(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return the installations this user has connected."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT installation_id, account_login FROM agent_repo_grants "
                "WHERE user_id = $1 ORDER BY id",
                user_id,
            )
        return [
            {"installation_id": row["installation_id"], "account_login": row["account_login"]}
            for row in rows
        ]

    async def revoke_repo_grant(self, *, user_id: str, installation_id: int) -> bool:
        """Drop one connection. Uninstalling on GitHub is the other half."""
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM agent_repo_grants WHERE user_id = $1 AND installation_id = $2 "
                "RETURNING id",
                user_id,
                installation_id,
            )
        return deleted is not None

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
        """Persist a short-lived state without retaining the bearer value."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM agent_oauth_states WHERE expires_at < NOW() - INTERVAL '1 day'"
            )
            await conn.execute(
                """
                INSERT INTO agent_oauth_states
                    (state_hash, user_id, provider, code_verifier_ciphertext, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                state_hash,
                user_id,
                provider,
                code_verifier_ciphertext,
                expires_at,
            )

    async def consume_oauth_state(
        self, *, state_hash: str, user_id: str, provider: str
    ) -> dict[str, Any] | None:
        """Consume one live state exactly once for its owning user/provider."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_oauth_states
                SET consumed_at = NOW()
                WHERE state_hash = $1 AND user_id = $2 AND provider = $3
                  AND consumed_at IS NULL AND expires_at > NOW()
                RETURNING code_verifier_ciphertext
                """,
                state_hash,
                user_id,
                provider,
            )
        return dict(row) if row else None

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
        """Store one GitLab.com identity per platform user."""
        async with self._pool.acquire() as conn:
            connection_id = await conn.fetchval(
                """
                INSERT INTO agent_gitlab_connections
                    (user_id, external_user_id, username, display_name, web_url,
                     access_token_ciphertext, refresh_token_ciphertext, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (user_id) DO UPDATE SET
                    external_user_id = EXCLUDED.external_user_id,
                    username = EXCLUDED.username,
                    display_name = EXCLUDED.display_name,
                    web_url = EXCLUDED.web_url,
                    access_token_ciphertext = EXCLUDED.access_token_ciphertext,
                    refresh_token_ciphertext = EXCLUDED.refresh_token_ciphertext,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                RETURNING id
                """,
                user_id,
                external_user_id,
                username,
                display_name,
                web_url,
                access_token_ciphertext,
                refresh_token_ciphertext,
                expires_at,
            )
        return int(connection_id)

    async def get_gitlab_connection(self, *, user_id: str) -> dict[str, Any] | None:
        """Return the owning user's connection, including encrypted tokens."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, external_user_id, username, display_name, web_url,
                       access_token_ciphertext, refresh_token_ciphertext, expires_at
                FROM agent_gitlab_connections WHERE user_id = $1
                """,
                user_id,
            )
        return dict(row) if row else None

    async def update_gitlab_tokens(
        self,
        *,
        connection_id: int,
        user_id: str,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> bool:
        """Rotate encrypted OAuth tokens without crossing user ownership."""
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE agent_gitlab_connections SET
                    access_token_ciphertext = $3,
                    refresh_token_ciphertext = $4,
                    expires_at = $5,
                    updated_at = NOW()
                WHERE id = $1 AND user_id = $2
                RETURNING id
                """,
                connection_id,
                user_id,
                access_token_ciphertext,
                refresh_token_ciphertext,
                expires_at,
            )
        return updated is not None

    async def delete_gitlab_connection(self, *, user_id: str, connection_id: int) -> bool:
        """Delete only a connection owned by the authenticated user."""
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM agent_gitlab_connections WHERE user_id = $1 AND id = $2 RETURNING id",
                user_id,
                connection_id,
            )
        return deleted is not None

    # ── MCP connections ────────────────────────────────────────────────
    #
    # One row per user per server: what *this person* connected, holding their
    # credential. Every query here is scoped by ``user_id`` and none takes a
    # connection id alone, so there is no shape in which one user's row can be
    # read or removed through another's request — the property the deployment
    # registry could not have, because it had no user to scope by.

    async def list_mcp_connections(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return this user's MCP connections, including encrypted tokens.

        Ordered by server name so the composer and the Integrations page agree
        on order without either sorting again.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_id, server, account_label, access_token_ciphertext,
                       refresh_token_ciphertext, expires_at, created_at, updated_at
                FROM agent_mcp_connections WHERE user_id = $1 ORDER BY server
                """,
                user_id,
            )
        return [dict(row) for row in rows]

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
        """Store one credential per user per server, replacing any earlier one.

        Reconnecting is an update rather than a second row: "connected" is a
        yes/no per person per server, and two live credentials for one pair
        would leave which one a job used down to row order.
        """
        async with self._pool.acquire() as conn:
            connection_id = await conn.fetchval(
                """
                INSERT INTO agent_mcp_connections
                    (user_id, server, account_label, access_token_ciphertext,
                     refresh_token_ciphertext, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id, server) DO UPDATE SET
                    account_label = EXCLUDED.account_label,
                    access_token_ciphertext = EXCLUDED.access_token_ciphertext,
                    refresh_token_ciphertext = EXCLUDED.refresh_token_ciphertext,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                RETURNING id
                """,
                user_id,
                server,
                account_label,
                access_token_ciphertext,
                refresh_token_ciphertext,
                expires_at,
            )
        return int(connection_id)

    async def delete_mcp_connection(self, *, user_id: str, server: str) -> bool:
        """Disconnect one server for this user; ``False`` if it was not connected."""
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM agent_mcp_connections WHERE user_id = $1 AND server = $2 RETURNING id",
                user_id,
                server,
            )
        return deleted is not None

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
    ) -> dict[str, Any]:
        """Create a queued job and return it.

        Callers that do not name a profile still go through the deployment's
        server-owned default.  This matters for internal producers that call
        the store directly: bypassing the HTTP route must not silently turn a
        secure deployment's jobs into ``trusted-v1`` work.
        """
        if execution_profile is None:
            execution_profile = default_execution_profile().value
        job_id = _new_job_id()
        thread_id = _new_thread_id()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO agent_threads (id, user_id, repo, title)
                VALUES ($1, $2, $3, $4)
                """,
                thread_id,
                user_id,
                repo,
                task_prompt.splitlines()[0][:160],
            )
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_jobs
                    (id, thread_id, turn_no, user_id, repo, base_sha, task_prompt,
                     setup_script, runtime, model, execution_profile, reasoning_effort,
                     metadata, mcp_servers)
                VALUES ($1, $2, 1, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12::jsonb, $13)
                RETURNING {_JOB_COLUMNS}
                """,
                job_id,
                thread_id,
                user_id,
                repo,
                base_sha,
                task_prompt,
                setup_script,
                runtime,
                model,
                execution_profile,
                reasoning_effort,
                json.dumps(metadata) if metadata is not None else None,
                list(mcp_servers or []),
            )
            await conn.execute(
                """
                INSERT INTO agent_thread_messages (thread_id, job_id, role, content)
                VALUES ($1, $2, 'user', $3)
                """,
                thread_id,
                job_id,
                task_prompt,
            )
        return _job_row_to_dict(row)

    async def resolve_legacy_published_commit(self, *, parent_job_id: str, commit_sha: str) -> bool:
        """Backfill an old published run's branch tip and release its child.

        Jobs published before conversation support have a PR URL and patch but
        no recorded commit. Their migrated thread id deliberately equals the
        old job id, preserving the existing ``agent/<job-id>`` branch. Once
        GitHub resolves that branch, the next turn can use it as its pinned
        base instead of re-applying the old patch and creating divergent git
        history.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_jobs
                SET published_commit_sha = $2, updated_at = NOW()
                WHERE id = $1 AND published_pr_url IS NOT NULL
                  AND published_commit_sha IS NULL
                RETURNING id
                """,
                parent_job_id,
                commit_sha,
            )
            if updated is None:
                return False
            await conn.execute(
                """
                UPDATE agent_jobs
                SET state = 'queued', base_sha = $2, updated_at = NOW()
                WHERE parent_job_id = $1 AND state = 'waiting'
                """,
                parent_job_id,
                commit_sha,
            )
        return True

    async def fail_waiting_follow_up(self, *, job_id: str, detail: str) -> bool:
        """Fail a follow-up that could not resolve its inherited git base."""
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchrow(
                """
                UPDATE agent_jobs
                SET state = 'failed', detail = $2, updated_at = NOW()
                WHERE id = $1 AND state = 'waiting'
                RETURNING thread_id, turn_no
                """,
                job_id,
                detail[:2000],
            )
            if updated is None:
                return False
            await conn.execute(
                """
                UPDATE agent_jobs
                SET state = 'failed', detail = $3, updated_at = NOW()
                WHERE thread_id = $1 AND turn_no > $2 AND state = 'waiting'
                """,
                updated["thread_id"],
                updated["turn_no"],
                "an earlier turn could not resume its draft PR branch",
            )
        return True

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
    ) -> dict[str, Any] | None:
        """Append a user turn and create the run that will answer it.

        ``reasoning_effort`` overrides the inherited depth; omitting it keeps
        the parent's, as an omitted model does. ``reset_reasoning_effort``
        clears it back to Auto — the one thing a null override cannot express,
        since null is what "inherit" already sends.

        A follow-up submitted while the latest turn is active is stored as
        ``waiting``. The thread row is locked before selecting that turn, so
        concurrent submissions form one linear chain instead of siblings that
        could later mutate the same branch at once. A request made from an old
        turn also appends to the current tip, matching chat semantics.
        """
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
                "SELECT id, pinned_at FROM agent_threads WHERE id = $1 AND user_id = $2 FOR UPDATE",
                requested["thread_id"],
                user_id,
            )
            if thread is None:
                return None
            thread_id = thread["id"]
            parent = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS}, "
                "EXISTS (SELECT 1 FROM agent_job_artifacts a "
                "        WHERE a.job_id = agent_jobs.id AND a.kind = 'patch') AS has_patch "
                "FROM agent_jobs WHERE thread_id = $1 "
                "ORDER BY turn_no DESC LIMIT 1 FOR UPDATE",
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
            sandbox = await conn.fetchrow(
                "SELECT state, context_status FROM agent_thread_sandboxes "
                "WHERE thread_id = $1 FOR UPDATE",
                thread_id,
            )
            if sandbox is not None and (
                sandbox["context_status"] == "lost" or sandbox["state"] in {"stopped_dirty", "lost"}
            ):
                return {"error": "sandbox_context_lost"}
            turn_no = parent["turn_no"] + 1
            parent_ready = parent["state"] in (FAILED, CANCELLED) or (
                parent["state"] == SUCCEEDED
                and (parent["published_commit_sha"] is not None or not parent["has_patch"])
            )
            state = QUEUED if parent_ready else WAITING
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_jobs
                    (id, thread_id, parent_job_id, turn_no, user_id, repo, base_sha,
                     task_prompt, setup_script, runtime, model, execution_profile,
                     reasoning_effort, state, metadata, mcp_servers)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                        $14, $15::jsonb, $16)
                RETURNING {_JOB_COLUMNS}
                """,
                job_id,
                thread_id,
                parent["id"],
                turn_no,
                user_id,
                parent["repo"],
                parent["published_commit_sha"] or parent["base_sha"],
                prompt,
                parent["setup_script"],
                runtime or parent["runtime"],
                model or parent["model"],
                parent["execution_profile"],
                None
                if reset_reasoning_effort
                else (reasoning_effort or parent["reasoning_effort"]),
                state,
                json.dumps(_load_json(parent["metadata"]))
                if parent["metadata"] is not None
                else None,
                # Inherited, not re-resolved: the next turn of a conversation
                # gets the tool surface the thread has been running with, so a
                # follow-up cannot quietly gain a server the owner never chose.
                list(parent["mcp_servers"] or []),
            )
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
        result["pinned_at"] = thread["pinned_at"]
        return result

    async def fork_thread(self, *, source_job_id: str, user_id: str) -> dict[str, Any] | None:
        """Duplicate a conversation up to (and including) one settled turn.

        Events are append-only and ``(thread_id, turn_no)`` is unique, so a
        thread cannot branch in place; a fork is a *new* thread whose turns
        are copies. The copies are display shells — terminal state, no
        attempts, no artifacts — which keeps them invisible to ``claim_job``
        (never queued), to the publisher (no patch artifact to claim), and to
        the reaper (no running attempt).

        Git lineage does carry over: each copy keeps ``base_sha`` and
        ``published_commit_sha``, and ``fork_source_job_id`` names the
        original turn so ``follow_up_context`` can fetch the source's
        still-unpublished patch for the fork's next run. The fork therefore
        continues from the same code state while publishing to its own
        ``agent/<thread-id>`` branch.

        Returns the copied anchor turn — the fork's navigation target — or
        ``None`` when the job is not the caller's or has not settled. An
        anchor mid-run is refused rather than partially copied: its patch
        does not exist yet, so history and workspace would disagree.
        """
        new_thread_id = _new_thread_id()
        async with self._pool.acquire() as conn, conn.transaction():
            source = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs WHERE id = $1 AND user_id = $2",
                source_job_id,
                user_id,
            )
            if source is None or source["state"] not in TERMINAL_STATES:
                return None
            thread = await conn.fetchrow(
                "SELECT repo, title FROM agent_threads WHERE id = $1 AND user_id = $2",
                source["thread_id"],
                user_id,
            )
            if thread is None:
                return None
            turns = await conn.fetch(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs "
                "WHERE thread_id = $1 AND turn_no <= $2 ORDER BY turn_no",
                source["thread_id"],
                source["turn_no"],
            )
            messages = await conn.fetch(
                """
                SELECT m.job_id, m.role, m.content, m.created_at
                FROM agent_thread_messages m
                JOIN agent_jobs j ON j.id = m.job_id
                WHERE m.thread_id = $1 AND j.turn_no <= $2
                ORDER BY j.turn_no, CASE WHEN m.role = 'user' THEN 0 ELSE 1 END, m.id
                """,
                source["thread_id"],
                source["turn_no"],
            )
            await conn.execute(
                "INSERT INTO agent_threads (id, user_id, repo, title) VALUES ($1, $2, $3, $4)",
                new_thread_id,
                user_id,
                thread["repo"],
                f"{thread['title']} (fork)"[:160],
            )
            copied_id: dict[str, str] = {}
            previous_copy: str | None = None
            for turn in turns:
                copy_id = _new_job_id()
                copied_id[turn["id"]] = copy_id
                await conn.execute(
                    """
                    INSERT INTO agent_jobs
                        (id, thread_id, parent_job_id, turn_no, user_id, repo, base_sha,
                         task_prompt, setup_script, runtime, model, execution_profile,
                         reasoning_effort, state, published_commit_sha, detail, metadata,
                         fork_source_job_id, mcp_servers, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                            $14, $15, $16, $17::jsonb, $18, $19,
                            clock_timestamp(), clock_timestamp())
                    """,
                    copy_id,
                    new_thread_id,
                    previous_copy,
                    turn["turn_no"],
                    user_id,
                    turn["repo"],
                    turn["base_sha"],
                    turn["task_prompt"],
                    turn["setup_script"],
                    turn["runtime"],
                    turn["model"],
                    turn["execution_profile"],
                    turn["reasoning_effort"],
                    # An unsettled earlier turn (a cancelled anchor can sit
                    # after a still-running parent) copies as cancelled: its
                    # messages are whatever had durably landed by now.
                    turn["state"] if turn["state"] in TERMINAL_STATES else CANCELLED,
                    turn["published_commit_sha"],
                    turn["detail"],
                    json.dumps(_load_json(turn["metadata"]))
                    if turn["metadata"] is not None
                    else None,
                    turn["id"],
                    # Carried with the turn, like its runtime and model. A fork
                    # that dropped these would give the copy a strictly smaller
                    # tool surface than the conversation it claims to continue,
                    # and the next turn would fail in a way that looks like the
                    # model got worse.
                    list(turn["mcp_servers"] or []),
                )
                previous_copy = copy_id
            # clock_timestamp(), not NOW(): NOW() is frozen for the whole
            # transaction, and list_jobs orders by created_at — copies must
            # both sort in turn order and surface as the newest conversation.
            await conn.executemany(
                """
                INSERT INTO agent_thread_messages (thread_id, job_id, role, content, created_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                [
                    (
                        new_thread_id,
                        copied_id[message["job_id"]],
                        message["role"],
                        message["content"],
                        message["created_at"],
                    )
                    for message in messages
                ],
            )
            anchor = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs WHERE id = $1",
                copied_id[source["id"]],
            )
        return _job_row_to_dict(anchor)

    async def prepare_retained_fork(
        self, *, source_job_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """Fence a latest-settled retained chat and persist its Docker saga.

        The transaction ends before the host is asked to commit an image.  A
        source-side ``fork_operation_id`` keeps a newly queued follow-up from
        being claimed in that gap, while a retry of the same in-flight fork
        receives the same target identities and can adopt an already-created
        clone instead of making another one.
        """
        operation_id = _new_sandbox_fork_id()
        target_thread_id = _new_thread_id()
        target_sandbox_id = _new_sandbox_id()
        async with self._pool.acquire() as conn, conn.transaction():
            requested = await conn.fetchrow(
                "SELECT thread_id FROM agent_jobs WHERE id = $1 AND user_id = $2",
                source_job_id,
                user_id,
            )
            if requested is None:
                return None
            thread = await conn.fetchrow(
                "SELECT id FROM agent_threads WHERE id = $1 AND user_id = $2 FOR UPDATE",
                requested["thread_id"],
                user_id,
            )
            if thread is None:
                return None
            # Publisher claims lock the same thread row before changing a
            # succeeded job to ``publishing``. Re-read the source only after
            # taking that lock, otherwise a pre-lock snapshot could let fork
            # preparation race past a publisher that just won ownership.
            source = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs WHERE id = $1 AND user_id = $2",
                source_job_id,
                user_id,
            )
            if source is None:
                return None
            if source["state"] not in TERMINAL_STATES:
                return {"error": "not_settled"}
            sandbox = await conn.fetchrow(
                "SELECT * FROM agent_thread_sandboxes WHERE thread_id = $1 FOR UPDATE",
                source["thread_id"],
            )
            if sandbox is None:
                return {"error": "not_retained"}

            existing_operation_id = sandbox["fork_operation_id"]
            if existing_operation_id is not None:
                existing = await conn.fetchrow(
                    "SELECT * FROM agent_sandbox_forks WHERE id = $1 FOR UPDATE",
                    existing_operation_id,
                )
                if (
                    existing is not None
                    and existing["state"] == "preparing"
                    and existing["source_job_id"] == source_job_id
                    and existing["user_id"] == user_id
                ):
                    return dict(existing)
                return {"error": "fork_in_progress"}

            if sandbox["last_settled_job_id"] != source_job_id:
                return {"error": "not_latest_settled"}
            if (
                sandbox["state"] not in {"running", "stopped"}
                or sandbox["context_status"] == "lost"
                or sandbox["container_id"] is None
                or sandbox["native_session_id"] is None
            ):
                return {"error": "sandbox_not_ready"}
            if sandbox["active_attempt_id"] is not None or sandbox["admitted_job_id"] is not None:
                return {"error": "sandbox_busy"}
            pending = await conn.fetchval(
                """
                SELECT 1 FROM agent_jobs
                WHERE thread_id = $1
                  AND state IN ('queued', 'waiting', 'running', 'publishing')
                LIMIT 1
                """,
                source["thread_id"],
            )
            if pending is not None:
                return {"error": "sandbox_busy"}

            operation = await conn.fetchrow(
                """
                INSERT INTO agent_sandbox_forks
                    (id, user_id, source_thread_id, source_job_id,
                     source_sandbox_id, source_sandbox_generation,
                     source_activation_generation, source_container_id,
                     source_native_session_id, target_thread_id, target_sandbox_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING *
                """,
                operation_id,
                user_id,
                source["thread_id"],
                source_job_id,
                sandbox["sandbox_id"],
                sandbox["generation"],
                sandbox["activation_generation"],
                sandbox["container_id"],
                sandbox["native_session_id"],
                target_thread_id,
                target_sandbox_id,
            )
            await conn.execute(
                "UPDATE agent_thread_sandboxes SET fork_operation_id = $2, "
                "updated_at = NOW() WHERE thread_id = $1",
                source["thread_id"],
                operation_id,
            )
        return dict(operation)

    async def finalize_retained_fork(
        self,
        *,
        operation_id: str,
        target_container_id: str,
        target_container_name: str,
        snapshot_image_digest: str,
        source_fingerprint: str | None,
        target_fingerprint: str | None,
    ) -> dict[str, Any] | None:
        """Atomically publish a broker-created sandbox clone and history copy."""
        async with self._pool.acquire() as conn, conn.transaction():
            seed = await conn.fetchrow(
                "SELECT * FROM agent_sandbox_forks WHERE id = $1",
                operation_id,
            )
            if seed is None:
                return None
            if seed["state"] == "ready":
                anchor = await conn.fetchrow(
                    f"""
                    SELECT {_QUALIFIED_JOB_COLUMNS}, t.pinned_at,
                           to_jsonb(s) AS thread_sandbox
                    FROM agent_jobs j
                    JOIN agent_threads t ON t.id = j.thread_id
                    LEFT JOIN agent_thread_sandboxes s ON s.thread_id = j.thread_id
                    WHERE j.id = $1
                    """,
                    seed["target_anchor_job_id"],
                )
                return None if anchor is None else _job_row_to_dict(anchor)
            if seed["state"] != "preparing":
                return None

            # All source-fork writers take locks in this order. Prepare retries
            # already hold thread -> sandbox before looking at the operation;
            # taking the operation first here creates the inverse edge and a
            # PostgreSQL deadlock during finalize/abort/reap races.
            thread = await conn.fetchrow(
                "SELECT repo, title FROM agent_threads WHERE id = $1 FOR UPDATE",
                seed["source_thread_id"],
            )
            sandbox = await conn.fetchrow(
                "SELECT * FROM agent_thread_sandboxes WHERE thread_id = $1 FOR UPDATE",
                seed["source_thread_id"],
            )
            operation = await conn.fetchrow(
                "SELECT * FROM agent_sandbox_forks WHERE id = $1 FOR UPDATE",
                operation_id,
            )
            if operation is None:
                return None
            if operation["state"] == "ready":
                anchor = await conn.fetchrow(
                    f"""
                    SELECT {_QUALIFIED_JOB_COLUMNS}, t.pinned_at,
                           to_jsonb(s) AS thread_sandbox
                    FROM agent_jobs j
                    JOIN agent_threads t ON t.id = j.thread_id
                    LEFT JOIN agent_thread_sandboxes s ON s.thread_id = j.thread_id
                    WHERE j.id = $1
                    """,
                    operation["target_anchor_job_id"],
                )
                return None if anchor is None else _job_row_to_dict(anchor)
            if operation["state"] != "preparing":
                return None
            source = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs WHERE id = $1",
                operation["source_job_id"],
            )
            if source is None or thread is None or sandbox is None:
                return None
            if (
                source["state"] not in TERMINAL_STATES
                or sandbox["fork_operation_id"] != operation_id
                or sandbox["sandbox_id"] != operation["source_sandbox_id"]
                or sandbox["generation"] != operation["source_sandbox_generation"]
                or sandbox["container_id"] != operation["source_container_id"]
                or sandbox["last_settled_job_id"] != operation["source_job_id"]
                or sandbox["active_attempt_id"] is not None
                or sandbox["admitted_job_id"] is not None
            ):
                return None

            turns = await conn.fetch(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs "
                "WHERE thread_id = $1 AND turn_no <= $2 ORDER BY turn_no",
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
                         CASE WHEN m.role = 'user' THEN 0 ELSE 1 END,
                         m.id
                """,
                operation["source_thread_id"],
                source["turn_no"],
            )
            await conn.execute(
                "INSERT INTO agent_threads (id, user_id, repo, title) VALUES ($1, $2, $3, $4)",
                operation["target_thread_id"],
                operation["user_id"],
                thread["repo"],
                f"{thread['title']} (fork)"[:160],
            )
            copied_id: dict[str, str] = {}
            previous_copy: str | None = None
            for turn in turns:
                copy_id = _new_job_id()
                copied_id[turn["id"]] = copy_id
                await conn.execute(
                    """
                    INSERT INTO agent_jobs
                        (id, thread_id, parent_job_id, turn_no, user_id, repo, base_sha,
                         task_prompt, setup_script, runtime, model, execution_profile,
                         reasoning_effort, state, published_commit_sha, detail, metadata,
                         fork_source_job_id, mcp_servers, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                            $14, $15, $16, $17::jsonb, $18, $19,
                            clock_timestamp(), clock_timestamp())
                    """,
                    copy_id,
                    operation["target_thread_id"],
                    previous_copy,
                    turn["turn_no"],
                    operation["user_id"],
                    turn["repo"],
                    turn["base_sha"],
                    turn["task_prompt"],
                    turn["setup_script"],
                    turn["runtime"],
                    turn["model"],
                    turn["execution_profile"],
                    turn["reasoning_effort"],
                    turn["state"] if turn["state"] in TERMINAL_STATES else CANCELLED,
                    turn["published_commit_sha"],
                    turn["detail"],
                    json.dumps(_load_json(turn["metadata"]))
                    if turn["metadata"] is not None
                    else None,
                    turn["id"],
                    list(turn["mcp_servers"] or []),
                )
                previous_copy = copy_id
            await conn.executemany(
                """
                INSERT INTO agent_thread_messages (thread_id, job_id, role, content, created_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                [
                    (
                        operation["target_thread_id"],
                        copied_id[message["job_id"]],
                        message["role"],
                        message["content"],
                        message["created_at"],
                    )
                    for message in messages
                ],
            )
            target_anchor_job_id = copied_id[operation["source_job_id"]]
            target_sandbox = await conn.fetchrow(
                """
                INSERT INTO agent_thread_sandboxes
                    (thread_id, sandbox_id, generation, host, container_id,
                     container_name, runtime_id, runtime_version,
                     sandbox_image_digest, execution_profile, state,
                     context_status, activation_generation, process_generation,
                     last_settled_job_id, native_session_id)
                VALUES ($1, $2, 1, $3, $4, $5, $6, $7, $8, $9, 'stopped',
                        'restored', 0, 0, $10, $11)
                RETURNING *
                """,
                operation["target_thread_id"],
                operation["target_sandbox_id"],
                sandbox["host"],
                target_container_id,
                target_container_name,
                sandbox["runtime_id"],
                sandbox["runtime_version"],
                snapshot_image_digest,
                sandbox["execution_profile"],
                target_anchor_job_id,
                operation["source_native_session_id"],
            )
            await conn.execute(
                """
                UPDATE agent_sandbox_forks
                SET state = 'ready', target_container_id = $2,
                    target_anchor_job_id = $3, snapshot_image_digest = $4,
                    source_fingerprint = $5, target_fingerprint = $6,
                    updated_at = NOW(), completed_at = NOW()
                WHERE id = $1
                """,
                operation_id,
                target_container_id,
                target_anchor_job_id,
                snapshot_image_digest,
                source_fingerprint,
                target_fingerprint,
            )
            await conn.execute(
                """
                UPDATE agent_thread_sandboxes
                SET fork_operation_id = NULL, updated_at = NOW()
                WHERE thread_id = $1 AND fork_operation_id = $2
                """,
                operation["source_thread_id"],
                operation_id,
            )
            anchor = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs WHERE id = $1",
                target_anchor_job_id,
            )
        result = _job_row_to_dict(anchor)
        result["thread_sandbox"] = dict(target_sandbox)
        return result

    async def abort_retained_fork(
        self,
        *,
        operation_id: str,
        detail: str,
        cleanup_pending: bool = False,
    ) -> dict[str, Any] | None:
        """Fail one incomplete fork and durably track any host cleanup still owed."""
        async with self._pool.acquire() as conn, conn.transaction():
            seed = await conn.fetchrow(
                "SELECT * FROM agent_sandbox_forks WHERE id = $1",
                operation_id,
            )
            if seed is None:
                return None
            if seed["state"] != "preparing":
                return dict(seed)
            # Match prepare/finalize's thread -> sandbox -> operation order.
            await conn.fetchval(
                "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE",
                seed["source_thread_id"],
            )
            await conn.fetchval(
                "SELECT thread_id FROM agent_thread_sandboxes WHERE thread_id = $1 FOR UPDATE",
                seed["source_thread_id"],
            )
            operation = await conn.fetchrow(
                "SELECT * FROM agent_sandbox_forks WHERE id = $1 FOR UPDATE",
                operation_id,
            )
            if operation is None:
                return None
            if operation["state"] == "preparing":
                await conn.execute(
                    """
                    UPDATE agent_sandbox_forks
                    SET state = 'failed', last_error = $2,
                        cleanup_pending = $3,
                        updated_at = NOW(), completed_at = NOW()
                    WHERE id = $1
                    """,
                    operation_id,
                    detail[:1000],
                    cleanup_pending,
                )
                await conn.execute(
                    """
                    UPDATE agent_thread_sandboxes
                    SET fork_operation_id = NULL, updated_at = NOW()
                    WHERE thread_id = $1 AND fork_operation_id = $2
                    """,
                    operation["source_thread_id"],
                    operation_id,
                )
            updated = await conn.fetchrow(
                "SELECT * FROM agent_sandbox_forks WHERE id = $1",
                operation_id,
            )
        return None if updated is None else dict(updated)

    async def complete_retained_fork_cleanup(self, *, operation_id: str) -> dict[str, Any] | None:
        """Acknowledge that the host removed this saga's incomplete resources."""
        async with self._pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE agent_sandbox_forks
                SET cleanup_pending = FALSE, updated_at = NOW()
                WHERE id = $1 AND state = 'failed' AND cleanup_pending = TRUE
                RETURNING *
                """,
                operation_id,
            )
        return None if updated is None else dict(updated)

    async def reap_stalled_sandbox_forks(
        self, *, stall_seconds: float = 900.0
    ) -> list[dict[str, Any]]:
        """Release crashed source claims and return every cleanup still owed."""
        async with self._pool.acquire() as conn, conn.transaction():
            candidates = await conn.fetch(
                """
                SELECT id, source_thread_id, state FROM agent_sandbox_forks
                WHERE (
                        state = 'preparing'
                        AND updated_at < NOW() - make_interval(secs => $1)
                      )
                   OR (state = 'failed' AND cleanup_pending = TRUE)
                ORDER BY source_thread_id, id
                """,
                stall_seconds,
            )
            result: list[dict[str, Any]] = []
            for candidate in candidates:
                if candidate["state"] == "preparing":
                    # Same source-writer lock order as prepare/finalize/abort.
                    # The operation lock uses SKIP LOCKED as rolling-upgrade
                    # protection against an older replica that took the old,
                    # inverse order before waiting on this thread row.
                    await conn.fetchval(
                        "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE",
                        candidate["source_thread_id"],
                    )
                    await conn.fetchval(
                        "SELECT thread_id FROM agent_thread_sandboxes "
                        "WHERE thread_id = $1 FOR UPDATE",
                        candidate["source_thread_id"],
                    )
                operation = await conn.fetchrow(
                    "SELECT * FROM agent_sandbox_forks WHERE id = $1 FOR UPDATE SKIP LOCKED",
                    candidate["id"],
                )
                if operation is None:
                    continue
                if operation["state"] == "preparing":
                    updated = await conn.fetchrow(
                        """
                        UPDATE agent_sandbox_forks
                        SET state = 'failed',
                            cleanup_pending = TRUE,
                            last_error = 'fork coordinator stopped before finalization',
                            updated_at = NOW(), completed_at = NOW()
                        WHERE id = $1 AND state = 'preparing'
                          AND updated_at < NOW() - make_interval(secs => $2)
                        RETURNING *
                        """,
                        operation["id"],
                        stall_seconds,
                    )
                    if updated is None:
                        continue
                    await conn.execute(
                        """
                        UPDATE agent_thread_sandboxes
                        SET fork_operation_id = NULL, updated_at = NOW()
                        WHERE thread_id = $1 AND fork_operation_id = $2
                        """,
                        operation["source_thread_id"],
                        operation["id"],
                    )
                    result.append(dict(updated))
                elif operation["state"] == "failed" and operation["cleanup_pending"]:
                    result.append(dict(operation))
        return result

    async def get_thread_for_job(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Return the conversation containing an owned job."""
        async with self._pool.acquire() as conn:
            thread = await conn.fetchrow(
                """
                SELECT t.id, t.repo, t.title, t.created_at, t.updated_at, t.pinned_at
                FROM agent_threads t
                JOIN agent_jobs j ON j.thread_id = t.id
                WHERE j.id = $1 AND j.user_id = $2
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
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs WHERE thread_id = $1 ORDER BY turn_no",
                thread["id"],
            )
            job_rows = [_job_row_to_dict(job) for job in jobs]
            for job in job_rows:
                job["pinned_at"] = thread["pinned_at"]
        return {
            "id": thread["id"],
            "repo": thread["repo"],
            "title": thread["title"],
            "created_at": thread["created_at"],
            "updated_at": thread["updated_at"],
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
                "SELECT thread_id FROM agent_jobs WHERE id = $1 AND user_id = $2",
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
        return [{**dict(row), "payload": _load_json(row["payload"])} for row in rows]

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
                "SELECT thread_id FROM agent_jobs WHERE id = $1 AND user_id = $2",
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
                       e.payload - ARRAY['text', 'input', 'content'] AS payload
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
                    "payload": _load_json(row["payload"]),
                }
                for row in events
            ],
        }

    async def follow_up_context(self, *, job_id: str) -> dict[str, Any]:
        """Return prior turns and the resumable parent patch for a claimed run."""
        async with self._pool.acquire() as conn:
            job = await conn.fetchrow(
                "SELECT thread_id, parent_job_id, turn_no FROM agent_jobs WHERE id = $1",
                job_id,
            )
            if job is None or job["parent_job_id"] is None:
                return {"messages": [], "patch": None}
            messages = await conn.fetch(
                """
                SELECT m.role, m.content
                FROM agent_thread_messages m
                JOIN agent_jobs j ON j.id = m.job_id
                WHERE m.thread_id = $1 AND j.turn_no < $2
                ORDER BY j.turn_no, CASE WHEN m.role = 'user' THEN 0 ELSE 1 END, m.id
                """,
                job["thread_id"],
                job["turn_no"],
            )
            patch = await conn.fetchval(
                """
                SELECT a.content
                FROM agent_job_artifacts a
                JOIN agent_jobs p ON p.id = a.job_id
                WHERE a.job_id = $1 AND a.kind = 'patch'
                  AND p.state IN ('succeeded', 'publishing', 'cancelled')
                  AND p.published_commit_sha IS NULL
                ORDER BY a.created_at DESC
                LIMIT 1
                """,
                job["parent_job_id"],
            )
            if patch is None:
                # A forked anchor is a copy with no artifacts of its own. Its
                # base_sha was frozen at fork time, so the *source* turn's
                # patch is still the uncommitted work this thread continues
                # from — even if the source thread published it later, that
                # commit landed on the source's branch, not in this fork's
                # base. The parent's own published_commit_sha stays the guard:
                # a fork taken after publish already carries the commit.
                patch = await conn.fetchval(
                    """
                    SELECT a.content
                    FROM agent_jobs parent
                    JOIN agent_jobs src ON src.id = parent.fork_source_job_id
                    JOIN agent_job_artifacts a ON a.job_id = src.id AND a.kind = 'patch'
                    WHERE parent.id = $1
                      AND parent.published_commit_sha IS NULL
                      AND src.state IN ('succeeded', 'publishing', 'cancelled')
                    ORDER BY a.created_at DESC
                    LIMIT 1
                    """,
                    job["parent_job_id"],
                )
        return {"messages": [dict(message) for message in messages], "patch": patch}

    async def resolve_model_credential(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
    ) -> dict[str, Any] | None:
        """Resolve a worker token into the identity its model calls run as.

        This is what lets the *same* capability token the sandbox already holds
        also authorize model traffic, so no second credential ever enters the
        sandbox. It returns ``None`` — meaning "reject" — unless the fence is
        still live, which makes revocation automatic: the moment the reaper
        supersedes the attempt or the job reaches a terminal state, the token
        stops buying inference. There is no separate key to remember to revoke.

        Returns ``{"user_id", "role", "job_id", "model"}``; the caller bills
        the job's owner. There is no per-task budget: a grant scopes what may
        be called, and the gateway's per-user daily quota governs how much.

        ``role`` is the owner's: the sandbox's model calls run with exactly the
        model access the owner has — they pay for them and could make them
        directly, so a narrower role here only produces 404s on models the
        composer legitimately offered. A suspended owner's jobs stop buying
        inference the moment the account does. The lookup tolerates a database
        without the ``users`` table (unit fixtures build only the agent
        schema); the caller treats a missing role as ``free``.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT j.user_id, j.id AS job_id, j.model, j.state,
                       j.mcp_servers
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE a.id = $1
                  AND a.lease_generation = $2
                  AND a.status = 'running'
                  AND a.lease_expires_at > NOW()
                  AND j.id = $3
                  AND j.current_attempt_id = a.id
                  AND j.state IN ('running', 'publishing')
                """,
                attempt_id,
                lease_generation,
                job_id,
            )
            if row is None:
                return None
            role: str | None = None
            try:
                owner = await conn.fetchrow(
                    "SELECT role, status FROM users WHERE id = $1", row["user_id"]
                )
            except asyncpg.UndefinedTableError:
                owner = None
            if owner is not None:
                if owner["status"] not in (None, "active"):
                    # The fence is live but the account behind it is not.
                    return None
                role = owner["role"]
        return {
            "user_id": row["user_id"],
            "role": role,
            "job_id": row["job_id"],
            # What this job may reach through the MCP proxy. Carried on the same
            # fenced lookup as the model credential so the two cannot disagree:
            # the instant the token stops buying inference it also stops
            # reaching tools.
            "mcp_servers": list(row["mcp_servers"] or []),
            "model": row["model"],
        }

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Fetch one job by id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_QUALIFIED_JOB_COLUMNS}, t.pinned_at,
                       to_jsonb(s) AS thread_sandbox
                FROM agent_jobs j
                LEFT JOIN agent_threads t ON t.id = j.thread_id
                LEFT JOIN agent_thread_sandboxes s ON s.thread_id = j.thread_id
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
                SELECT {_QUALIFIED_JOB_COLUMNS}, t.pinned_at
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = $1
                WHERE j.user_id = $1
                  AND ($4::text IS NULL OR j.repo = $4)
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

    async def list_projects(self, *, user_id: str, archived: bool = False) -> list[dict[str, Any]]:
        """Summarize a user's repos, most recently active first.

        The sidebar groups tasks by project, so it needs every repo the user
        has ever run in — not just those represented in the newest-first job
        page, which would silently drop dormant projects from the tree.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT j.repo,
                       COUNT(DISTINCT j.thread_id) AS task_count,
                       COUNT(*) FILTER (
                           WHERE j.state IN ('queued', 'waiting', 'running', 'publishing')
                       ) AS active_count,
                       MAX(j.created_at) AS last_activity_at,
                       COUNT(DISTINCT j.thread_id) FILTER (
                           WHERE t.pinned_at IS NOT NULL
                       ) AS pinned_count,
                       MAX(t.pinned_at) AS pinned_at
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = $1
                WHERE j.user_id = $1
                  AND (($2 AND t.archived_at IS NOT NULL)
                       OR (NOT $2 AND t.archived_at IS NULL))
                GROUP BY j.repo
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
    # The pool of machines that can run agent jobs, and which one currently
    # does. Runners pull work, so the gateway cannot push a job at a chosen
    # host; the only place "run agents over there now" can be decided is the
    # claim. A host therefore enters the pool by polling, not by being
    # registered — start a runner on a machine and it appears; stop it and the
    # row goes stale — and switching hosts is a gate on that same poll.
    #
    # Recording happens before the gate, deliberately: an operator has to be
    # able to see a host in order to switch to it, and a host that only became
    # visible once it was already active could never be picked in the first
    # place.

    async def touch_runner_host(self, *, host: str | None, worker_id: str) -> None:
        """Record a polling runner. ``None`` predates host reporting.

        Deliberately not part of the claim's transaction: being visible in the
        pool is not a decision, and holding a host row for the length of a
        claim would make every poll contend with every other.
        """
        if not host:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_runner_hosts (host, last_worker_id)
                VALUES ($1, $2)
                ON CONFLICT (host) DO UPDATE
                SET last_seen_at = NOW(), last_worker_id = EXCLUDED.last_worker_id
                """,
                host,
                worker_id,
            )

    async def active_runner_host(self) -> str | None:
        """The pinned host, or ``None`` when any runner may claim."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT active_host FROM agent_runner_policy WHERE id")

    async def runner_pool(self) -> tuple[list[dict[str, Any]], str | None, dict[str, str]]:
        """The pool, the pinned host and the version selection, read together.

        One transaction because all three are shown side by side: read
        separately, a switch landing between them renders a page whose list and
        whose header name different hosts, which reads as a bug in the thing
        the operator is using to fix a bug. The version selection joined that
        for the same reason — a page offering a menu that no longer contains
        the selection it displays is the same failure wearing a different hat.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            policy = await conn.fetchrow(
                "SELECT active_host, runtime_versions FROM agent_runner_policy WHERE id"
            )
            rows = await conn.fetch(
                f"""
                SELECT {_HOST_COLUMNS}
                FROM agent_runner_hosts
                ORDER BY last_seen_at DESC
                """
            )
        active = None if policy is None else policy["active_host"]
        selected = {} if policy is None else _load_json(policy["runtime_versions"]) or {}
        return (
            [{**self._host_row_to_dict(row), "is_active": row["host"] == active} for row in rows],
            active,
            selected,
        )

    async def list_runner_hosts(self) -> list[dict[str, Any]]:
        """Return every host seen polling, most recent poll first."""
        hosts, _, _ = await self.runner_pool()
        return hosts

    async def set_runtime_version(self, *, runtime: str, version: str | None) -> bool:
        """Choose the CLI version this deployment runs for one runtime.

        ``version=None`` clears the choice, which returns that runtime to
        whatever its claiming host reports as the image default.

        Returns ``False`` when no host in the pool offers ``version``. Storing a
        selection nothing can serve is not harmless: the claim would fall back
        silently, so the admin page would show a version that never runs — the
        pin's "switching to a typo" failure, one layer along.

        Same lock order as ``set_active_runner_host`` — policy row first, then
        the host rows it is checked against — so the two admin writes and the
        claim's shared read serialize with no cycle to deadlock on.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await conn.fetchval(
                "SELECT runtime_versions FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            if version is not None:
                offered = await conn.fetch(
                    "SELECT runtime_versions_available FROM agent_runner_hosts "
                    "WHERE runtime_versions_available IS NOT NULL FOR SHARE"
                )
                menus = (_load_json(row[0]) or {} for row in offered)
                if not any(version in (menu.get(runtime) or []) for menu in menus):
                    return False
            selection = dict(_load_json(current) or {})
            if version is None:
                selection.pop(runtime, None)
            else:
                selection[runtime] = version
            # Upsert for the same reason the host pin is one: against a database
            # whose singleton row is missing, a bare UPDATE touches nothing and
            # still reports success.
            await conn.execute(
                """
                INSERT INTO agent_runner_policy (id, runtime_versions) VALUES (TRUE, $1::jsonb)
                ON CONFLICT (id) DO UPDATE
                SET runtime_versions = EXCLUDED.runtime_versions, updated_at = NOW()
                """,
                json.dumps(selection),
            )
            return True

    async def set_active_runner_host(self, *, host: str | None) -> bool:
        """Pin agent jobs to one host, or unpin with ``None``.

        Returns ``False`` when ``host`` names a machine that has never polled —
        pinning to one would park the queue on a host that may not exist, which
        presents as "every job hangs" with nothing in the logs.

        Takes the policy row exclusively *first*, then the host row it is about
        to point at. Every writer here uses that order, and the claim takes the
        policy row shared, so the paths serialize with no cycle to deadlock on:
        a claim past its own read finishes before this commits, one that has
        not reached it waits and then sees this value, and a concurrent removal
        of the host being pinned cannot slip in between the check and the write.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT active_host FROM agent_runner_policy WHERE id FOR UPDATE")
            # Checked before anything is written — and locked, not merely read:
            # a bare existence check lets a concurrent removal commit before
            # this does, leaving the queue pinned to a host that is gone.
            if host is not None:
                known = await conn.fetchval(
                    "SELECT 1 FROM agent_runner_hosts WHERE host = $1 FOR SHARE", host
                )
                if not known:
                    return False
            # Upsert, not a bare UPDATE: against a database whose policy row is
            # missing, an UPDATE touches nothing and still reports success —
            # the switch would read as applied while the gate kept using the
            # old value.
            await conn.execute(
                """
                INSERT INTO agent_runner_policy (id, active_host) VALUES (TRUE, $1)
                ON CONFLICT (id) DO UPDATE
                SET active_host = EXCLUDED.active_host, updated_at = NOW()
                """,
                host,
            )
            return True

    async def forget_runner_host(self, *, host: str) -> str:
        """Drop a host from the pool. A runner still polling re-adds itself.

        Returns ``"deleted"``, ``"active"`` (refused) or ``"unknown"``.

        The active-host refusal lives here rather than in the caller because
        checking there is a read the switch can outrun: the endpoint sees the
        host idle, an admin pins it, and the delete then removes the row the
        gate is pointing at — silently unpinning the deployment and handing
        every machine the queue back. Same policy row, same order as the
        switch, so the two serialize.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            active = await conn.fetchval(
                "SELECT active_host FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            if active == host:
                return "active"
            deleted = await conn.fetchval(
                "DELETE FROM agent_runner_hosts WHERE host = $1 RETURNING host", host
            )
        return "deleted" if deleted is not None else "unknown"

    # ── Host pool: enrollment, heartbeat, capacity ─────────────────────
    #
    # The methods above are #1158's: a host appears by polling, and an operator
    # picks one. These add the half that lets a pool be *managed* — a machine
    # is enrolled with a credential, promises to heartbeat, declares how much
    # it can take and what it can run, and is drained or revoked by name.
    #
    # The two halves share one table and compose rather than compete: the pin
    # still decides *which* host may claim, and the pool decides *whether* that
    # host is in a state to. A deployment that never enrolls anything keeps
    # behaving exactly as it did, because a poll-registered row defaults to
    # ``online`` with no slot ceiling.
    #
    # Lock order everywhere below: policy row, then job rows, then host rows —
    # the order #1167 established, extended by one table. Nothing here takes a
    # host row and then reaches back for a job or the policy.

    @staticmethod
    def _host_row_to_dict(row: Any) -> dict[str, Any]:
        """Decode a host row's JSON columns; ``credential_hash`` never leaves."""
        host = dict(row)
        host["labels"] = _load_json(host.get("labels")) or {}
        host["capabilities"] = _load_json(host.get("capabilities")) or []
        # None (never reported) and {} (reported: no labels) stay distinct.
        host["runtime_versions"] = _load_json(host.get("runtime_versions"))
        host["runtime_versions_available"] = _load_json(host.get("runtime_versions_available"))
        return host

    async def issue_enroll_token(
        self, *, token_hash: str, host: str, ttl_seconds: float, issued_by: str | None = None
    ) -> dict[str, Any]:
        """Record a one-time enrollment token by hash and return its expiry.

        Only the hash is stored. An operator who loses the token issues another
        one; nobody — including anyone who can read this table — can recover
        the value that was handed out.

        ``host`` is the name this token may register, and it is stored in
        clear: it is not a secret, it is the authenticated decision the token
        carries. See :meth:`consume_enroll_token`.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_host_enroll_tokens (token_hash, host, issued_by, expires_at)
                VALUES ($1, $2, $3, NOW() + make_interval(secs => $4))
                RETURNING expires_at, created_at
                """,
                token_hash,
                host,
                issued_by,
                ttl_seconds,
            )
        return {"expires_at": row["expires_at"], "created_at": row["created_at"]}

    async def consume_enroll_token(self, *, token_hash: str, host: str) -> bool:
        """Spend an enrollment token, exactly once and only under its own name.

        Single-use is the UPDATE's own ``consumed_at IS NULL`` predicate rather
        than a read followed by a write: two machines booting from the same
        pasted token race here, and only one row update can win.

        **The name is part of the predicate, not a check after the fact.**
        ``register_host`` upserts, so registering under a live host's name
        revokes that host's credential and resets its row to ``enrolling`` —
        one typo during a setup takes a working machine out of the pool. The
        name therefore comes from the token an admin issued rather than from
        the machine presenting it. Because it is a predicate, a mismatch also
        does not *spend* the token: the typo is retried, not re-issued.

        A row whose ``host`` is NULL — issued before the binding existed —
        matches nothing, because ``NULL = $2`` is never true. That is the
        fail-closed direction: such a token was redeemable under any name.
        """
        async with self._pool.acquire() as conn:
            consumed = await conn.fetchval(
                """
                UPDATE agent_host_enroll_tokens
                SET consumed_at = NOW(), consumed_host = $2
                WHERE token_hash = $1 AND consumed_at IS NULL AND expires_at > NOW()
                  AND host = $2
                RETURNING token_hash
                """,
                token_hash,
                host,
            )
        return consumed is not None

    async def get_host(self, *, host: str) -> dict[str, Any] | None:
        """Return one host row, without its credential hash."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_HOST_COLUMNS} FROM agent_runner_hosts WHERE host = $1", host
            )
        return None if row is None else self._host_row_to_dict(row)

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
        """Enrol a machine, or re-enrol one that was rebuilt.

        Starts at ``enrolling``, not ``online``: registration proves somebody
        held a valid enrollment token, not that the machine is up. The first
        heartbeat is what says that, and it is one round trip away.

        Re-enrollment resets ``slots_used`` to the number of attempts actually
        running on this host rather than to zero. A machine that was rebuilt
        mid-job has a stale counter, and zero would over-subscribe it while the
        old attempts are still leased; counting is the only answer that is
        right in both the rebuilt and the merely-re-registered case.

        The upsert is what makes a rebuild possible, and it is also what makes
        a mistyped name destructive — it revokes whatever credential the name
        currently holds. It stays an upsert; what changed is who chooses the
        name. :meth:`consume_enroll_token` will only spend a token under the
        name an admin bound to it, so reaching this method under a live host's
        name is an operator's decision rather than a machine's.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_runner_hosts
                    (host, status, capabilities, slots_total, broker_url, runner_version,
                     labels, credential_hash)
                VALUES ($1, '{HOST_ENROLLING}', $2::jsonb, $3, $4, $5, $6::jsonb, $7)
                ON CONFLICT (host) DO UPDATE
                SET status = '{HOST_ENROLLING}',
                    capabilities = EXCLUDED.capabilities,
                    slots_total = EXCLUDED.slots_total,
                    broker_url = EXCLUDED.broker_url,
                    runner_version = EXCLUDED.runner_version,
                    labels = EXCLUDED.labels,
                    credential_hash = EXCLUDED.credential_hash,
                    last_heartbeat_at = NULL,
                    slots_used = (
                        SELECT count(*) FROM agent_attempts
                        WHERE host_id = EXCLUDED.host AND status = 'running'
                    )
                RETURNING {_HOST_COLUMNS}
                """,
                host,
                json.dumps(normalize_capabilities(capabilities)),
                slots_total,
                broker_url,
                runner_version,
                json.dumps(labels or {}),
                credential_hash,
            )
        return self._host_row_to_dict(row)

    async def authenticate_host(self, *, host: str, credential_hash: str) -> dict[str, Any] | None:
        """Return the host iff this credential is the one it currently holds.

        Fails closed on every other reading: an unknown host, a revoked
        credential (``credential_hash`` nulled), a superseded one, or a
        disabled row all answer ``None``. Compared in SQL so a revocation that
        commits mid-request is seen by the next call rather than by the next
        restart.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_HOST_COLUMNS} FROM agent_runner_hosts
                WHERE host = $1 AND credential_hash IS NOT NULL AND credential_hash = $2
                  AND status <> '{HOST_DISABLED}'
                """,
                host,
                credential_hash,
            )
        return None if row is None else self._host_row_to_dict(row)

    async def record_host_heartbeat(
        self,
        *,
        host: str,
        slots_total: int | None = None,
        runner_version: str | None = None,
        broker_url: str | None = None,
        capabilities: list[str] | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: dict[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        """Record a heartbeat, promoting the host to ``online`` when it may.

        ``enrolling → online`` and ``offline → online`` both happen here: a
        machine that comes back after a network partition needs no operator to
        put it back to work. ``draining`` and ``disabled`` deliberately do
        **not** — a heartbeat is the host saying "I am here", not "I should be
        taking work", and letting it re-promote itself would make drain a
        suggestion.

        Returns ``None`` for an unknown host. Every field is optional and only
        overwrites when supplied, so a heartbeat that carries nothing but
        liveness does not blank out what enrollment declared.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE agent_runner_hosts
                SET last_heartbeat_at = NOW(),
                    last_seen_at = NOW(),
                    status = CASE
                        WHEN status IN ('{HOST_ENROLLING}', '{HOST_OFFLINE}') THEN '{HOST_ONLINE}'
                        ELSE status
                    END,
                    slots_total = COALESCE($2, slots_total),
                    runner_version = COALESCE($3, runner_version),
                    broker_url = COALESCE($4, broker_url),
                    capabilities = COALESCE($5::jsonb, capabilities),
                    sandbox_runtime = COALESCE($6, sandbox_runtime),
                    sandbox_image_digest = COALESCE($7, sandbox_image_digest),
                    runtime_versions = COALESCE($8::jsonb, runtime_versions),
                    runtime_versions_available =
                        COALESCE($9::jsonb, runtime_versions_available)
                WHERE host = $1
                RETURNING {_HOST_COLUMNS}
                """,
                host,
                slots_total,
                runner_version,
                broker_url,
                None if capabilities is None else json.dumps(normalize_capabilities(capabilities)),
                # "" survives COALESCE deliberately: an empty runtime is the
                # host attesting shared-kernel runc, not declining to answer.
                sandbox_runtime,
                sandbox_image_digest,
                None if runtime_versions is None else json.dumps(runtime_versions),
                (
                    None
                    if runtime_versions_available is None
                    else json.dumps(runtime_versions_available)
                ),
            )
        return None if row is None else self._host_row_to_dict(row)

    async def set_host_status(self, *, host: str, status: str) -> str:
        """Move a host to ``status``, refusing transitions the table forbids.

        Returns ``"ok"``, ``"unknown"`` or ``"invalid"``. The table is consulted
        inside the transaction that writes, on a locked row: checking it in the
        caller means an admin draining a host and the reaper marking it offline
        can both read ``online`` and both write, and the loser's decision is the
        one that sticks.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await conn.fetchval(
                "SELECT status FROM agent_runner_hosts WHERE host = $1 FOR UPDATE", host
            )
            if current is None:
                return "unknown"
            if not transition_allowed(current, status):
                return "invalid"
            await conn.execute(
                "UPDATE agent_runner_hosts SET status = $2 WHERE host = $1", host, status
            )
        return "ok"

    async def remove_host(self, *, host: str, force: bool = False) -> str:
        """Revoke a host's credential and take it out of the pool.

        Returns ``"ok"``, ``"unknown"`` or ``"busy"``. Busy means the host is
        still holding slots: removing it then would leave running attempts
        whose machine has officially stopped existing, and nothing would notice
        until their leases lapsed. ``force`` accepts exactly that — the lease
        expiry *is* the recovery path. Legacy one-shot attempts retain their
        ordinary retry behavior. A retained activation cannot be replayed
        safely after its host disappears, so its expiry marks that chat LOST
        and fails already-waiting descendants instead.

        Disabling rather than deleting. The row is what
        ``agent_attempts.host_id`` points at, and what an operator reads when
        asking where a job ran; #1158's ``forget`` is still there for the
        machine that is genuinely gone. Removing or forgetting the host row is
        administrative bookkeeping; it is not by itself proof that a retained
        Docker writable layer was deleted.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, slots_used FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if row is None:
                return "unknown"
            if row["slots_used"] > 0 and not force:
                return "busy"
            await conn.execute(
                f"""
                UPDATE agent_runner_hosts
                SET status = '{HOST_DISABLED}', credential_hash = NULL
                WHERE host = $1
                """,
                host,
            )
        return "ok"

    async def reap_offline_hosts(
        self,
        *,
        interval_seconds: float,
        missed_intervals: int,
    ) -> list[str]:
        """Mark enrolled hosts that stopped heartbeating as ``offline``.

        Only *enrolled* hosts — ones holding a credential — are candidates.
        A host that joined by polling never promised to heartbeat, and marking
        it offline would stop the claims of a machine that is working
        perfectly, which is the whole pre-G deployment.

        ``COALESCE(last_heartbeat_at, first_seen_at)`` so a machine that
        registered and then never came up ages out of ``enrolling`` too,
        instead of sitting there looking like it is mid-setup forever.

        **``draining`` is not a candidate, and that is the point.** It was, and
        the result was that an operator's drain undid itself: this pass moved
        ``draining → offline``, and the next heartbeat moved ``offline →
        online``, because a machine returning from a partition is supposed to
        come back without an operator. A drained host that goes quiet and
        returns would silently rejoin the claim pool — quietly taking work the
        operator had just decided it must not take. Drain is an instruction,
        not an observation, so nothing here overwrites it; ``remove`` is what
        ends a drained host's life.

        Returns the hosts it changed.
        """
        deadline = interval_seconds * missed_intervals
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                UPDATE agent_runner_hosts
                SET status = '{HOST_OFFLINE}'
                WHERE credential_hash IS NOT NULL
                  AND status IN ('{HOST_ENROLLING}', '{HOST_ONLINE}')
                  AND COALESCE(last_heartbeat_at, first_seen_at)
                      < NOW() - make_interval(secs => $1)
                RETURNING host
                """,
                deadline,
            )
        return [row["host"] for row in rows]

    async def host_for_attempt(self, *, attempt_id: int) -> dict[str, Any] | None:
        """The host row an attempt ran on, or ``None`` when it had none.

        ``None`` covers both "this attempt predates host reporting" and "the
        host it ran on was forgotten" — the FK is ``ON DELETE SET NULL``, so
        both present as the same absence, and both mean the caller falls back
        to the single-host configuration.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_QUALIFIED_HOST_COLUMNS} FROM agent_runner_hosts h
                JOIN agent_attempts a ON a.host_id = h.host
                WHERE a.id = $1
                """,
                attempt_id,
            )
        return None if row is None else self._host_row_to_dict(row)

    async def host_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        """The job attempt's host, or its retained thread's durable host."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT COALESCE(a.host_id, s.host) AS owner_host,
                       {_QUALIFIED_HOST_COLUMNS}
                FROM agent_jobs j
                LEFT JOIN agent_attempts a ON a.id = j.current_attempt_id
                LEFT JOIN agent_thread_sandboxes s ON s.thread_id = j.thread_id
                LEFT JOIN agent_runner_hosts h
                  ON h.host = COALESCE(a.host_id, s.host)
                WHERE j.id = $1
                """,
                job_id,
            )
        if row is None or row["owner_host"] is None:
            return None
        host = self._host_row_to_dict(row)
        # A retained sandbox remains hard-pinned even if its host row was
        # administratively forgotten. Returning a placeholder (with no broker
        # URL) makes broker routing fail closed instead of falling back to a
        # different machine's single-host environment setting.
        host["host"] = row["owner_host"]
        return host

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
        host: str | None = None,
        host_credential_hash: str | None = None,
        claim_attestation: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Claim the oldest queued job, creating a new fenced attempt.

        Returns the job dict extended with ``attempt_id``, ``attempt_no`` and
        ``lease_generation``, or ``None`` when the queue is empty. Concurrent
        workers are safe via ``FOR UPDATE SKIP LOCKED``.

        When an operator has pinned a host, a runner reporting any other one —
        or none at all — claims nothing. Unpinned is the default and behaves as
        it always has: whoever polls first takes the job.

        The host check is the first statement of the *claiming* transaction and
        takes the policy row shared, so it cannot be overtaken by a switch
        committing between the check and the dequeue. Read on its own
        connection, as it was, that window was real: the old host reads "still
        me", the admin switches, and the machine the operator just moved away
        from takes one more job while the page says the switch succeeded.

        **The pool gate (G4) sits after the pin and answers the same way.**
        A reported host must be ``online`` and have a free slot, and the job it
        takes must require nothing the host did not declare. All three refusals
        return ``None`` — the same "nothing for you" a drained or unpinned
        runner already gets — because a runner has no useful different
        behaviour for "the queue is empty" and "the queue has work you cannot
        run". Capacity and capability are operator-visible on the admin
        surface, which is where the question is answerable.

        A runner reporting **no** host keeps the pre-pool path for
        ``trusted-v1`` exactly: no
        slot, no ``host_id``, and the env-configured broker. That is what E11's
        single-host setup is, and it must not need enrolling to keep working.
        Its capability set is empty, though, so a job that *declares*
        requirements will not go to it — the fail-closed reading, and a no-op
        today because nothing sets requirements yet.

        ``untrusted-kata-v1`` is a separate fail-closed gate. It requires the
        current per-host credential to still match while the host slot is
        taken, and a claim-time attestation from this exact replica naming the
        Kata runtime plus unequal host/guest kernels. The mutable heartbeat row
        is not evidence for a particular replica during a rolling deployment.
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
        await self.touch_runner_host(host=host, worker_id=worker_id)
        async with self._pool.acquire() as conn, conn.transaction():
            policy = await conn.fetchrow(
                "SELECT active_host, runtime_versions FROM agent_runner_policy WHERE id FOR SHARE"
            )
            active_host = None if policy is None else policy["active_host"]
            selected_versions = (
                {} if policy is None else _load_json(policy["runtime_versions"]) or {}
            )
            if active_host is not None and host != active_host:
                # Fail closed, including for a runner that reports no host:
                # leaving the machine an operator just switched away from able
                # to claim is the one outcome that makes the switch a lie.
                return None
            capabilities = "[]"
            credential_matches = False
            if host is not None:
                host_row = await conn.fetchrow(
                    "SELECT status, capabilities, slots_total, slots_used, "
                    "(credential_hash IS NOT NULL AND credential_hash = $2) "
                    "AS credential_matches "
                    "FROM agent_runner_hosts WHERE host = $1",
                    host,
                    host_credential_hash,
                )
                # This read only chooses which profile may be considered. The
                # slot UPDATE below is the decision point: it re-checks both
                # capacity and the exact credential hash, so a concurrent
                # drain, revocation or re-enrolment makes the claim roll back.
                # Avoiding an early host-row lock also preserves parallel
                # SKIP LOCKED dequeue across replicas on the same machine.
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
                host is not None
                and credential_matches
                and sandbox_runtime == KATA_RUNTIME
                and isinstance(host_kernel, str)
                and bool(host_kernel.strip())
                and isinstance(guest_kernel, str)
                and bool(guest_kernel.strip())
                and host_kernel.strip() != guest_kernel.strip()
            )
            # Follow-ups may be submitted while a run is active. Promote only
            # those whose direct parent has settled; a chain therefore
            # advances one turn at a time even with multiple waiting messages.
            await conn.execute(
                """
                UPDATE agent_jobs child
                SET state = 'queued', updated_at = NOW()
                FROM agent_jobs parent
                WHERE child.state = 'waiting'
                  AND child.parent_job_id = parent.id
                  AND parent.state = ANY($1::text[])
                  AND (
                        parent.state <> 'succeeded'
                        OR parent.published_commit_sha IS NOT NULL
                        OR NOT EXISTS (
                            SELECT 1 FROM agent_job_artifacts a
                            WHERE a.job_id = parent.id AND a.kind = 'patch'
                        )
                  )
                """,
                list(TERMINAL_STATES),
            )
            # Affinity (design §7): a job whose thread is hot on another host
            # is *held* for that host — its live process has the whole
            # conversation in memory, and any other machine would demote the
            # turn to COLD for no reason. The hold is a window, not a pin: if
            # the resident host does not collect within it (busy, gone,
            # draining), anyone may take the job and the ladder degrades one
            # rung, exactly as designed. ``updated_at`` is when the job last
            # became claimable, so waiting follow-ups time out from promotion,
            # not creation. FOR UPDATE OF j: the residency row is read, never
            # locked — claims must not serialize behind heartbeats.
            job_row = await conn.fetchrow(
                f"""
                SELECT {_QUALIFIED_JOB_COLUMNS}, to_jsonb(s) AS thread_sandbox
                FROM agent_jobs j
                LEFT JOIN agent_thread_residencies r
                  ON r.thread_id = j.thread_id
                 AND r.status = 'hot'
                 AND r.lease_expires_at > NOW()
                LEFT JOIN agent_thread_sandboxes s
                  ON s.thread_id = j.thread_id
                WHERE j.state = 'queued' AND j.cancel_requested = FALSE
                  AND COALESCE(j.metadata->'requires', '[]'::jsonb) <@ $1::jsonb
                  AND (
                        j.execution_profile = '{ExecutionProfile.TRUSTED_V1.value}'
                        OR (
                            j.execution_profile = '{ExecutionProfile.UNTRUSTED_KATA_V1.value}'
                            AND $3::boolean
                        )
                  )
                  AND (
                        r.thread_id IS NULL
                        OR r.host = $2
                        OR j.updated_at <= NOW()
                            - make_interval(secs => {RESIDENCY_AFFINITY_HOLD_S})
                  )
                  AND (
                        s.thread_id IS NULL
                        OR (
                            s.host = $2
                            AND s.fork_operation_id IS NULL
                            AND s.state IN (
                                'provisioning', 'starting', 'running', 'stopped'
                            )
                        )
                  )
                ORDER BY j.created_at
                LIMIT 1
                FOR UPDATE OF j SKIP LOCKED
                """,
                capabilities,
                host,
                secure_attestation,
            )
            if job_row is None:
                return None
            job_id = job_row["id"]
            if host is not None:
                # The atomic half of the slot claim. Under READ COMMITTED two
                # claims hitting the same host row serialize on it and the
                # loser re-evaluates ``slots_used < slots_total`` against the
                # winner's committed value, so a host with one free slot hands
                # out one — never two. ``status`` is re-checked here as well:
                # a drain that commits between the read above and this write
                # takes effect immediately rather than one job late.
                took_slot = await conn.fetchval(
                    f"""
                    UPDATE agent_runner_hosts
                    SET slots_used = slots_used + 1
                    WHERE host = $1
                      AND status = '{HOST_ONLINE}'
                      AND (slots_total IS NULL OR slots_used < slots_total)
                      AND ($3::text IS NULL OR credential_hash = $3)
                      AND (
                            $2 <> '{ExecutionProfile.UNTRUSTED_KATA_V1.value}'
                            OR $3::text IS NOT NULL
                      )
                    RETURNING slots_used
                    """,
                    host,
                    job_row["execution_profile"],
                    host_credential_hash,
                )
                if took_slot is None:
                    # Nothing has been written to the job yet, so returning
                    # here leaves it queued for whoever has room.
                    return None
            next_no = await conn.fetchval(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM agent_attempts WHERE job_id = $1",
                job_id,
            )
            thread_sandbox = _load_json(job_row["thread_sandbox"])
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
                    (job_id, attempt_no, lease_owner, lease_generation,
                     lease_expires_at, base_sha, host_id,
                     sandbox_runtime, sandbox_image_digest, runtime_version,
                     host_kernel, guest_kernel)
                VALUES ($1, $2, $3, $4, NOW() + make_interval(secs => $5), $6, $7,
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
        claim["lease_generation"] = attempt["lease_generation"]
        # The stamp as *written*, not as the host row reads later: the
        # checkpoint gate must compare against what this attempt runs, and a
        # rolling upgrade's heartbeat can move the host row between claim and
        # gate (review finding: the TOCTOU the stamping existed to kill).
        claim["sandbox_runtime"] = attempt["sandbox_runtime"]
        claim["sandbox_image_digest"] = attempt["sandbox_image_digest"]
        claim["runtime_version"] = attempt["runtime_version"]
        # A stopped retained sandbox is durable local state, not a short HOT
        # optimization. The claim query above pins it to this host without an
        # affinity timeout; the runner receives the exact row so it can ask
        # that host's supervisor to start the recorded container.
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
            # Hung off the guarded UPDATE above, not off entering this method:
            # the predicate is what makes an abort happen once, so the slot
            # release inherits that and cannot double-count.
            await self._release_host_slot(conn, released_row["host_id"])
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
            await conn.execute(
                """
                UPDATE agent_jobs
                SET state = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'queued' END,
                    terminal_resume_pending = cancel_requested,
                    current_attempt_id = NULL,
                    updated_at = NOW()
                WHERE id = $1 AND current_attempt_id = $2 AND state = 'running'
                """,
                released,
                attempt_id,
            )
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
        """
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchrow(
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
                        UPDATE agent_thread_sandboxes s
                        SET state = CASE
                                WHEN s.last_settled_job_id = $1 THEN s.state
                                WHEN s.admitted_job_id = $1 THEN 'stopped_dirty'
                                WHEN s.container_id IS NULL THEN 'stopped_dirty'
                                ELSE 'stopping'
                            END,
                            context_status = CASE
                                WHEN s.last_settled_job_id IS DISTINCT FROM $1
                                     AND (
                                        s.admitted_job_id = $1
                                        OR s.container_id IS NULL
                                     ) THEN 'lost'
                                ELSE s.context_status
                            END,
                            active_attempt_id = CASE
                                WHEN s.active_attempt_id = $2 THEN NULL
                                ELSE s.active_attempt_id
                            END,
                            admitted_job_id = CASE
                                WHEN s.admitted_job_id = $1 THEN NULL
                                ELSE s.admitted_job_id
                            END,
                            admitted_at = CASE
                                WHEN s.admitted_job_id = $1 THEN NULL
                                ELSE s.admitted_at
                            END,
                            idle_deadline = CASE
                                WHEN s.last_settled_job_id = $1 THEN s.idle_deadline
                                ELSE NULL
                            END,
                            last_error = CASE
                                WHEN s.last_settled_job_id = $1 THEN s.last_error
                                ELSE COALESCE($4, 'retained turn ended before settlement')
                            END,
                            updated_at = NOW()
                        WHERE s.thread_id = $3
                          AND (
                                s.active_attempt_id = $2
                                OR s.admitted_job_id = $1
                          )
                        RETURNING s.context_status
                        """,
                        job_id,
                        attempt_id,
                        updated["thread_id"],
                        detail,
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
                has_patch = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM agent_job_artifacts "
                    "WHERE job_id = $1 AND kind = 'patch')",
                    job_id,
                )
                # A successful patch still has to cross the trusted publish
                # boundary. The next turn waits for that commit so it can
                # fast-forward the same thread branch instead of opening one
                # PR per message. Failures/cancellations and no-op successes
                # have no publish step and may release their child now.
                if (to_state != SUCCEEDED or not has_patch) and not (
                    sandbox_transition is not None
                    and sandbox_transition["context_status"] == "lost"
                ):
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() "
                        "WHERE parent_job_id = $1 AND state = 'waiting'",
                        job_id,
                    )
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

        Absent counters stay NULL: a backend the sampler could not read is not
        a machine that used nothing, and ``coverage`` says which happened.
        """
        async with self._pool.acquire() as conn:
            return bool(
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
                        sample_count, coverage, sampler_version
                    )
                    SELECT live.id, live.job_id, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
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
                        sampler_version = EXCLUDED.sampler_version
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
                )
            )

    async def job_turn_telemetry(self, *, job_id: str) -> dict[str, Any] | None:
        """Read how a job's newest attempt ran: continuity, window, resources.

        Newest attempt rather than the current one: a retried turn's earlier
        attempts each have their own window, and the one a reader wants is the
        one that produced the result in front of them. One round trip, because
        this rides the job-detail read that the thread view already makes.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT a.continuity_mode, a.context_status,
                       a.started_at, a.finished_at,
                       r.cpu_usec, r.mem_peak_bytes, r.mem_avg_bytes,
                       r.io_read_bytes, r.io_write_bytes,
                       r.net_rx_bytes, r.net_tx_bytes,
                       r.sample_count, r.coverage, r.sampler_version
                FROM agent_attempts a
                LEFT JOIN agent_attempt_resources r ON r.attempt_id = a.id
                WHERE a.job_id = $1
                ORDER BY a.attempt_no DESC
                LIMIT 1
                """,
                job_id,
            )
            return dict(row) if row else None

    async def terminal_workspace_ready(self, *, attempt_id: int) -> bool:
        """Return whether the live attempt currently permits terminal writes.

        This is deliberately a primary-key lookup rather than an event-log
        replay: terminal input is latency-sensitive and may arrive every few
        milliseconds. Expired or superseded attempts are never considered
        ready, even before the reaper updates the owning job row.
        """
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    SELECT terminal_ready
                    FROM agent_attempts
                    WHERE id = $1
                      AND status = 'running'
                      AND lease_expires_at > NOW()
                    """,
                    attempt_id,
                )
            )

    async def thread_workspace_ready(self, *, thread_id: str) -> bool:
        """Return whether a retained thread permits owner workspace mutation.

        The broker's in-memory suspension is the immediate barrier. This
        durable copy closes broker-restart and old-turn-URL gaps: an active
        attempt must have published ``workspace_ready``, and a full-sandbox
        fork keeps the source immutable until its saga releases the fence.
        """
        async with self._pool.acquire() as conn:
            ready = await conn.fetchval(
                """
                SELECT
                    s.context_status <> 'lost'
                    AND s.state NOT IN ('provisioning', 'stopping', 'stopped_dirty', 'lost')
                    AND s.fork_operation_id IS NULL
                    AND (
                        (s.active_attempt_id IS NULL
                            AND s.state IN ('running', 'stopped'))
                        OR (
                            s.state = 'running'
                            AND EXISTS (
                                SELECT 1 FROM agent_attempts a
                                WHERE a.id = s.active_attempt_id
                                  AND a.status = 'running'
                                  AND a.lease_expires_at > NOW()
                                  AND a.terminal_ready = TRUE
                            )
                        )
                    )
                FROM agent_thread_sandboxes s
                WHERE s.thread_id = $1
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
                "payload": _load_json(row["payload"]),
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

    # ── Artifacts ──────────────────────────────────────────────────────

    async def save_artifact(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        kind: str,
        content: str,
    ) -> int | None:
        """Store (or replace) one artifact for an attempt, fenced by the lease."""
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
            return await conn.fetchval(
                """
                INSERT INTO agent_job_artifacts (job_id, attempt_id, kind, content)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (job_id, attempt_id, kind)
                DO UPDATE SET content = EXCLUDED.content, created_at = NOW()
                RETURNING id
                """,
                attempt["job_id"],
                attempt_id,
                kind,
                content,
            )

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
                SELECT j.id, j.thread_id
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
            row = await conn.fetchrow(
                "SELECT * FROM agent_thread_sandboxes WHERE thread_id = $1 FOR UPDATE",
                thread_id,
            )
            if row is None:
                row = await conn.fetchrow(
                    """
                    INSERT INTO agent_thread_sandboxes
                        (thread_id, sandbox_id, host, container_name,
                         runtime_id, runtime_version, sandbox_image_digest,
                         execution_profile)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING *
                    """,
                    thread_id,
                    sandbox_id,
                    host,
                    container_name,
                    runtime_id,
                    runtime_version,
                    sandbox_image_digest,
                    execution_profile,
                )
            immutable = {
                "sandbox_id": sandbox_id,
                "host": host,
                "container_name": container_name,
                "runtime_id": runtime_id,
                "runtime_version": runtime_version,
                "sandbox_image_digest": sandbox_image_digest,
                "execution_profile": execution_profile,
            }
            if any(row[key] != value for key, value in immutable.items()):
                raise ValueError("thread sandbox immutable identity or runtime changed")
            if row["state"] in {"lost", "stopped_dirty"}:
                return None
            active_attempt = row["active_attempt_id"]
            if active_attempt is not None and active_attempt != attempt_id:
                return None
            admitted_job = row["admitted_job_id"]
            if admitted_job is not None and admitted_job != earns["id"]:
                # A prior attempt may have delivered this thread's last prompt
                # and died before its outcome became durable.  A new prompt is
                # never admitted on top of that ambiguous boundary.
                return None

            previous_state = row["state"]
            already_admitted = active_attempt == attempt_id and previous_state in {
                "starting",
                "running",
            }
            activation_generation = int(row["activation_generation"])
            if not already_admitted:
                # Every distinct STARTING transition gets a new fence.  The
                # container may still be RUNNING between turns, but the prior
                # attempt and its credentials must never share an activation
                # generation with the next prompt admission.  This also makes
                # a just-stopped Docker container safe when its heartbeat has
                # not yet reconciled the durable row from RUNNING to STOPPED.
                activation_generation += 1
            context_status = "new" if row["last_settled_job_id"] is None else "restored"
            updated = await conn.fetchrow(
                """
                UPDATE agent_thread_sandboxes
                SET state = 'starting', active_attempt_id = $2,
                    activation_generation = $3, context_status = $4,
                    idle_deadline = NULL, last_error = NULL, updated_at = NOW()
                WHERE thread_id = $1
                RETURNING *
                """,
                thread_id,
                attempt_id,
                activation_generation,
                context_status,
            )
        return dict(updated)

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
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_thread_sandboxes s
                SET state = 'running', container_id = $6,
                    context_status = $7, updated_at = NOW()
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE s.thread_id = $1 AND s.generation = $4
                  AND s.activation_generation = $5
                  AND s.active_attempt_id = $2
                  AND (s.container_id IS NULL OR s.container_id = $6)
                  AND a.id = $2 AND a.lease_generation = $3
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                  AND j.current_attempt_id = a.id AND j.thread_id = s.thread_id
                RETURNING s.*
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                container_id,
                context_status,
            )
        return None if row is None else dict(row)

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
            return bool(
                await conn.fetchval(
                    """
                    UPDATE agent_thread_sandboxes s
                    SET process_generation = $6, updated_at = NOW()
                    FROM agent_attempts a
                    WHERE s.thread_id = $1 AND s.generation = $4
                      AND s.activation_generation = $5
                      AND s.active_attempt_id = $2
                      AND a.id = $2 AND a.lease_generation = $3
                      AND a.status = 'running' AND a.lease_expires_at > NOW()
                    RETURNING s.thread_id
                    """,
                    thread_id,
                    attempt_id,
                    lease_generation,
                    sandbox_generation,
                    activation_generation,
                    process_generation,
                )
            )

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
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    UPDATE agent_thread_sandboxes s
                    SET admitted_job_id = $6,
                        admitted_at = COALESCE(admitted_at, NOW()),
                        updated_at = NOW()
                    FROM agent_attempts a
                    JOIN agent_jobs j ON j.id = a.job_id
                    WHERE s.thread_id = $1 AND s.generation = $4
                      AND s.activation_generation = $5
                      AND s.active_attempt_id = $2
                      AND a.id = $2 AND a.lease_generation = $3
                      AND a.status = 'running' AND a.lease_expires_at > NOW()
                      AND j.id = $6 AND j.current_attempt_id = a.id
                      AND j.thread_id = s.thread_id AND j.state = 'running'
                      AND (s.admitted_job_id IS NULL OR s.admitted_job_id = $6)
                    RETURNING s.thread_id
                    """,
                    thread_id,
                    attempt_id,
                    lease_generation,
                    sandbox_generation,
                    activation_generation,
                    job_id,
                )
            )

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
    ) -> dict[str, Any] | None:
        """Close one turn boundary and arm deactivation without deleting state."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_thread_sandboxes s
                SET state = 'running', active_attempt_id = NULL,
                    last_settled_job_id = $6, context_status = $7,
                    native_session_id = COALESCE($8, native_session_id),
                    idle_deadline = NOW() + make_interval(secs => $9),
                    updated_at = NOW()
                FROM agent_attempts a
                WHERE s.thread_id = $1 AND s.generation = $4
                  AND s.activation_generation = $5
                  AND s.active_attempt_id = $2
                  AND s.admitted_job_id = $6
                  AND a.id = $2 AND a.lease_generation = $3
                  AND a.job_id = $6 AND a.status = 'running'
                  AND a.lease_expires_at > NOW()
                RETURNING s.*
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                job_id,
                context_status,
                native_session_id,
                idle_timeout_seconds,
            )
        return None if row is None else dict(row)

    async def mark_thread_sandbox_stopped(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_generation: int,
        activation_generation: int,
    ) -> bool:
        """Persist a supervisor-confirmed idle ``docker stop`` transition."""
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    UPDATE agent_thread_sandboxes
                    SET state = 'stopped', idle_deadline = NULL, updated_at = NOW()
                    WHERE thread_id = $1 AND host = $2 AND generation = $3
                      AND activation_generation = $4
                      AND active_attempt_id IS NULL AND state IN ('running', 'stopping')
                    RETURNING thread_id
                    """,
                    thread_id,
                    host,
                    sandbox_generation,
                    activation_generation,
                )
            )

    async def reconcile_thread_sandbox_observation(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
        activation_generation: int,
        running: bool,
        activity_active: bool | None = None,
        idle_remaining_seconds: float | None = None,
    ) -> str | None:
        """Reconcile a host-authenticated Docker observation into the ledger.

        Omission is never deletion. A STOPPED observation is accepted only
        after the control plane's own settled-idle deadline and while no
        attempt owns the sandbox, so an old heartbeat cannot stop a newer
        activation in the database. A RUNNING observation may promote a
        stopped row with the exact activation generation. Generation zero is
        also accepted while no attempt owns the row: that is the only identity
        a restarted broker can reconstruct from immutable Docker labels, and
        is needed when a terminal or file operation wakes the container
        outside a worker turn. Container, sandbox, and sandbox-generation
        identity still have to match exactly.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Docker creation can win just before the runner dies and posts
            # ``/running``. A host-authenticated observation carrying all
            # immutable labels may fill that one missing identifier so the
            # fenced LOST/STOPPING cleanup command can still target it.
            await conn.execute(
                """
                UPDATE agent_thread_sandboxes
                SET container_id = $4, updated_at = NOW()
                WHERE thread_id = $1 AND host = $2 AND sandbox_id = $3
                  AND generation = $5 AND container_id IS NULL
                """,
                thread_id,
                host,
                sandbox_id,
                container_id,
                sandbox_generation,
            )
            # A runner can fail after the supervisor created/started Docker
            # but before ``/running`` returned the container id. Its explicit
            # pre-admission abandon leaves the durable row reprovisionable
            # (PROVISIONING, no active attempt). If Docker then reports the
            # exact labelled object, it is no longer a blank provisioning
            # slot: a running object is an orphan that must be stopped, while
            # an already-stopped object is safe to reuse. In particular, an
            # orphaned in-memory supervisor lease must not turn heartbeat
            # activity into an immortal container.
            recovered_state = await conn.fetchval(
                """
                UPDATE agent_thread_sandboxes
                SET state = CASE WHEN $7 THEN 'stopping' ELSE 'stopped' END,
                    idle_deadline = NULL,
                    updated_at = NOW()
                WHERE thread_id = $1 AND host = $2 AND sandbox_id = $3
                  AND container_id = $4 AND generation = $5
                  AND state = 'provisioning'
                  AND active_attempt_id IS NULL AND admitted_job_id IS NULL
                  AND (
                    ($6 > 0 AND activation_generation = $6)
                    OR $6 = 0
                  )
                RETURNING state
                """,
                thread_id,
                host,
                sandbox_id,
                container_id,
                sandbox_generation,
                activation_generation,
                running,
            )
            if running and activity_active is not None:
                if activity_active:
                    await conn.execute(
                        """
                        UPDATE agent_thread_sandboxes
                        SET idle_deadline = NULL, updated_at = NOW()
                        WHERE thread_id = $1 AND host = $2 AND sandbox_id = $3
                          AND container_id = $4 AND generation = $5
                          AND state IN ('running', 'stopped')
                          AND (
                            ($6 > 0 AND activation_generation = $6)
                            OR ($6 = 0 AND active_attempt_id IS NULL)
                          )
                        """,
                        thread_id,
                        host,
                        sandbox_id,
                        container_id,
                        sandbox_generation,
                        activation_generation,
                    )
                elif idle_remaining_seconds is not None:
                    await conn.execute(
                        """
                        UPDATE agent_thread_sandboxes
                        SET idle_deadline = NOW() + make_interval(secs => $7),
                            updated_at = NOW()
                        WHERE thread_id = $1 AND host = $2 AND sandbox_id = $3
                          AND container_id = $4 AND generation = $5
                          AND state IN ('running', 'stopped')
                          AND active_attempt_id IS NULL
                          AND (
                            ($6 > 0 AND activation_generation = $6)
                            OR ($6 = 0 AND idle_deadline IS NULL)
                          )
                        """,
                        thread_id,
                        host,
                        sandbox_id,
                        container_id,
                        sandbox_generation,
                        activation_generation,
                        idle_remaining_seconds,
                    )
            state = await conn.fetchval(
                """
                UPDATE agent_thread_sandboxes
                SET state = CASE WHEN $7 THEN 'running' ELSE 'stopped' END,
                    idle_deadline = CASE WHEN $7 THEN idle_deadline ELSE NULL END,
                    updated_at = NOW()
                WHERE thread_id = $1 AND host = $2 AND sandbox_id = $3
                  AND container_id = $4 AND generation = $5
                  AND active_attempt_id IS NULL
                  AND (
                    ($7 AND state = 'stopped' AND (
                        ($6 > 0 AND activation_generation = $6)
                        OR $6 = 0
                    ))
                    OR
                    (NOT $7 AND ($6 = 0 OR activation_generation = $6)
                        AND (
                            (state = 'running' AND idle_deadline IS NOT NULL
                                AND idle_deadline <= NOW())
                            OR (state = 'stopping' AND active_attempt_id IS NULL)
                        ))
                  )
                RETURNING state
                """,
                thread_id,
                host,
                sandbox_id,
                container_id,
                sandbox_generation,
                activation_generation,
                running,
            )
        effective_state = state if state is not None else recovered_state
        return None if effective_state is None else str(effective_state)

    async def due_thread_sandbox_stop(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
    ) -> dict[str, Any] | None:
        """Return an exact fence for a due idle or failed-attempt stop."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT thread_id, generation, activation_generation,
                       CASE
                           WHEN state IN ('stopped_dirty', 'lost') THEN 'lost'
                           WHEN state = 'stopping' THEN 'orphaned_attempt'
                           ELSE 'idle'
                       END AS reason
                FROM agent_thread_sandboxes
                WHERE thread_id = $1 AND host = $2 AND sandbox_id = $3
                  AND container_id = $4 AND generation = $5
                  AND active_attempt_id IS NULL
                  AND (
                        (state = 'running' AND idle_deadline IS NOT NULL
                            AND idle_deadline <= NOW())
                        OR state IN ('stopping', 'stopped_dirty', 'lost')
                  )
                """,
                thread_id,
                host,
                sandbox_id,
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
        """Release a failed activation without claiming a settled boundary."""
        # Kept in the wire contract for rollout compatibility. Failed
        # activations stop immediately and therefore never arm settled-idle.
        _ = idle_timeout_seconds
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE agent_thread_sandboxes s
                SET state = CASE
                        WHEN $6 THEN 'stopped_dirty'
                        WHEN s.container_id IS NULL THEN 'provisioning'
                        ELSE 'stopping'
                    END,
                    context_status = CASE WHEN $6 THEN 'lost' ELSE s.context_status END,
                    active_attempt_id = NULL,
                    idle_deadline = NULL,
                    last_error = $7, updated_at = NOW()
                FROM agent_attempts a
                WHERE s.thread_id = $1 AND s.generation = $4
                  AND s.activation_generation = $5
                  AND s.active_attempt_id = $2
                  AND (
                        ($6 AND (
                            s.admitted_job_id IS NULL
                            OR s.admitted_job_id = a.job_id
                        ))
                        OR (NOT $6 AND s.admitted_job_id IS NULL)
                  )
                  AND a.id = $2 AND a.lease_generation = $3
                RETURNING s.*
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                lost,
                detail,
            )
            if row is not None and lost:
                await conn.execute(
                    "UPDATE agent_attempts SET context_status = 'lost' "
                    "WHERE id = $1 AND lease_generation = $2",
                    attempt_id,
                    lease_generation,
                )
                await self._fail_unrunnable_follow_ups(
                    conn,
                    thread_id=thread_id,
                )
        return None if row is None else dict(row)

    async def thread_sandbox(self, *, thread_id: str) -> dict[str, Any] | None:
        """Return the durable local sandbox mapping, including STOPPED/LOST."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_thread_sandboxes WHERE thread_id = $1",
                thread_id,
            )
        return None if row is None else dict(row)

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
        """Take (or take over) a thread's residency; ``None`` = not earned.

        Residency is something a host earns by *currently* running the
        thread's turn — attempt on this host, ``running``, lease unexpired —
        and the check shares a transaction and a row lock with the write, so
        a finish or a supersede cannot slip between them, and an expired
        attempt the reaper has not reached yet earns nothing (review
        finding 4). Atomic across racers: two hosts serialize on the row,
        each gets a distinct generation, and the larger one owns the thread.
        """
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
            # The predecessor's grant, captured under the same transaction: a
            # takeover is a credential rotation, and the old generation's
            # grant must be revocable by the caller that just superseded it.
            previous_grant_id = await conn.fetchval(
                "SELECT grant_id FROM agent_thread_residencies WHERE thread_id = $1 FOR UPDATE",
                thread_id,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO agent_thread_residencies
                    (thread_id, host, sandbox_id, runtime_id, runtime_version,
                     status, lease_expires_at)
                VALUES ($1, $2, $3, $4, $5, 'hot',
                        NOW() + make_interval(secs => $6))
                ON CONFLICT (thread_id) DO UPDATE SET
                    generation = agent_thread_residencies.generation + 1,
                    host = EXCLUDED.host,
                    sandbox_id = EXCLUDED.sandbox_id,
                    runtime_id = EXCLUDED.runtime_id,
                    runtime_version = EXCLUDED.runtime_version,
                    status = 'hot',
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    established_at = NOW(),
                    last_active_at = NOW()
                RETURNING *
                """,
                thread_id,
                host,
                sandbox_id,
                runtime_id,
                runtime_version,
                ttl_seconds,
            )
        result = dict(row)
        result["previous_grant_id"] = previous_grant_id
        result["establishing_job_id"] = earns["job_id"]
        result["establishing_attempt_id"] = earns["attempt_id"]
        result["user_id"] = earns["user_id"]
        result["model"] = earns["model"]
        return result

    async def link_residency_grant(self, *, thread_id: str, generation: int, grant_id: str) -> bool:
        """Record the grant a residency runs on — only its own generation.

        A link that matched nothing means the residency was taken over between
        mint and link; the caller revokes the orphan grant rather than handing
        out a credential nothing owns.
        """
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    UPDATE agent_thread_residencies
                    SET grant_id = $3
                    WHERE thread_id = $1 AND generation = $2 AND status = 'hot'
                    RETURNING thread_id
                    """,
                    thread_id,
                    generation,
                    grant_id,
                )
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
                UPDATE agent_thread_residencies
                SET lease_expires_at = NOW() + make_interval(secs => $3),
                    last_active_at = NOW()
                WHERE thread_id = $1 AND generation = $2 AND status = 'hot'
                  AND ($4::text IS NULL OR host = $4)
                RETURNING COALESCE(grant_id, '')
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
                UPDATE agent_thread_residencies
                SET status = 'none', last_active_at = NOW()
                WHERE thread_id = $1 AND generation = $2 AND status <> 'none'
                  AND ($3::text IS NULL OR host = $3)
                RETURNING COALESCE(grant_id, '')
                """,
                thread_id,
                generation,
                host,
            )

    async def record_residency_checkpoint(
        self, *, thread_id: str, generation: int, turn_id: str
    ) -> bool:
        """Note the newest turn whose checkpoint this residency exported."""
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    UPDATE agent_thread_residencies
                    SET last_checkpoint_turn_id = $3, last_active_at = NOW()
                    WHERE thread_id = $1 AND generation = $2
                    RETURNING thread_id
                    """,
                    thread_id,
                    generation,
                    turn_id,
                )
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
                SELECT * FROM agent_thread_residencies
                WHERE thread_id = $1 AND status = 'hot' AND lease_expires_at > NOW()
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
                UPDATE agent_thread_residencies
                SET status = 'none'
                WHERE status = 'hot' AND lease_expires_at <= NOW()
                RETURNING thread_id, generation, host, grant_id
                """
            )
        return [dict(row) for row in rows]

    async def prune_thread_checkpoints(self, *, keep_job_id: str) -> int:
        """Keep only ``keep_job_id``'s checkpoint within its thread.

        Latest-per-thread retention (design §6): the newest checkpoint is the
        only one the claim gate could ever hand out — an older one would fail
        ``through_turn_id`` — so keeping it would be pure storage cost. Rows
        are deleted, not archived; the transcript remains the durable record.
        """
        async with self._pool.acquire() as conn:
            deleted = await conn.fetch(
                """
                DELETE FROM agent_job_artifacts a
                USING agent_jobs sibling, agent_jobs kept
                WHERE a.kind = 'checkpoint'
                  AND kept.id = $1
                  AND sibling.id = a.job_id
                  AND sibling.thread_id = kept.thread_id
                  AND sibling.thread_id IS NOT NULL
                  AND a.job_id <> $1
                RETURNING a.id
                """,
                keep_job_id,
            )
        return len(deleted)

    async def get_artifact(self, *, job_id: str, kind: str) -> dict[str, Any] | None:
        """Fetch the most recent artifact of a kind for a job."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, job_id, attempt_id, kind, content, created_at
                FROM agent_job_artifacts
                WHERE job_id = $1 AND kind = $2
                ORDER BY attempt_id DESC
                LIMIT 1
                """,
                job_id,
                kind,
            )
        if row is None:
            return None
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            "kind": row["kind"],
            "content": row["content"],
            "created_at": row["created_at"],
        }

    # ── Cancellation ───────────────────────────────────────────────────

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        """Request cancellation; returns the job's resulting state.

        Queued jobs cancel immediately. Running/publishing jobs get the flag
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
            if state in (WAITING, QUEUED):
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'cancelled', cancel_requested = TRUE, "
                    "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                    job_id,
                )
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() "
                    "WHERE parent_job_id = $1 AND state = 'waiting'",
                    job_id,
                )
                return CANCELLED
            if state in (RUNNING, PUBLISHING):
                await conn.execute(
                    "UPDATE agent_jobs SET cancel_requested = TRUE, updated_at = NOW() "
                    "WHERE id = $1",
                    job_id,
                )
            return state

    # ── Platform-side publishing ───────────────────────────────────────
    #
    # Publishing is driven by the trusted server, not by the worker: the
    # sandbox holds no git credential, so it can only hand over a patch. These
    # three methods are therefore *not* lease-fenced — the fence exists to stop
    # zombie workers, and no worker is involved here. Exactly-once is enforced
    # instead by ``published_pr_url IS NULL`` plus ``SKIP LOCKED``, so two
    # gateway processes can run the publisher loop without double-publishing.

    async def claim_for_publish(self) -> dict[str, Any] | None:
        """Take one finished job that has a patch and no PR yet.

        Marks it ``publishing`` in the same transaction, so a second publisher
        cannot pick it up. The thread lock is shared with retained-fork
        preparation: a Docker snapshot may copy a succeeded boundary or a
        publisher may start mutating its lineage, but those operations cannot
        overlap. Returns the job plus its patch, or ``None``.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT j.id, j.thread_id, j.parent_job_id, j.user_id, j.repo, j.base_sha,
                       j.task_prompt, j.metadata, a.content AS patch,
                       thread_publish.published_pr_url AS parent_pr_url
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id
                JOIN agent_job_artifacts a
                  ON a.job_id = j.id AND a.kind = 'patch'
                LEFT JOIN LATERAL (
                    SELECT prior.published_pr_url
                    FROM agent_jobs prior
                    WHERE prior.thread_id = j.thread_id
                      AND prior.published_pr_url IS NOT NULL
                    ORDER BY prior.turn_no DESC
                    LIMIT 1
                ) thread_publish ON TRUE
                WHERE j.state = 'succeeded'
                  AND j.published_pr_url IS NULL
                  AND NOT EXISTS (
                        SELECT 1 FROM agent_thread_sandboxes s
                        WHERE s.thread_id = j.thread_id
                          AND s.fork_operation_id IS NOT NULL
                  )
                ORDER BY j.updated_at
                LIMIT 1
                FOR UPDATE OF t SKIP LOCKED
                """
            )
            if row is None:
                return None
            claimed = await conn.fetchval(
                "UPDATE agent_jobs SET state = 'publishing', updated_at = NOW() "
                "WHERE id = $1 AND state = 'succeeded' "
                "AND published_pr_url IS NULL RETURNING id",
                row["id"],
            )
            if claimed is None:
                return None
            job_id = row["id"]
            await self._insert_event(
                conn,
                job_id=job_id,
                attempt_id=await conn.fetchval(
                    "SELECT current_attempt_id FROM agent_jobs WHERE id = $1", job_id
                ),
                event_type="lifecycle",
                payload={"phase": "publishing"},
            )
        return {
            "job_id": row["id"],
            "thread_id": row["thread_id"],
            "parent_job_id": row["parent_job_id"],
            "parent_pr_url": row["parent_pr_url"],
            "user_id": row["user_id"],
            "repo": row["repo"],
            "base_sha": row["base_sha"],
            "task_prompt": row["task_prompt"],
            # Carries the branch the owner started from. The publisher targets
            # its PR at that branch, so without it every PR went to the
            # deployment default no matter what the job was pinned from.
            "metadata": _load_json(row["metadata"]),
            "patch": row["patch"],
        }

    async def record_publish(
        self, *, job_id: str, pr_url: str, commit_sha: str | None = None
    ) -> bool:
        """Record the published PR exactly once, returning the job to succeeded."""
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_jobs
                SET state = 'succeeded', published_pr_url = $2,
                    published_commit_sha = $3, updated_at = NOW()
                WHERE id = $1 AND published_pr_url IS NULL AND state = 'publishing'
                RETURNING id
                """,
                job_id,
                pr_url,
                commit_sha,
            )
            if updated is None:
                return False
            attempt_id = await conn.fetchval(
                "SELECT current_attempt_id FROM agent_jobs WHERE id = $1", job_id
            )
            if attempt_id is not None:
                await self._insert_event(
                    conn,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    event_type="lifecycle",
                    payload={"phase": "published", "pr_url": pr_url},
                )
            await conn.execute(
                """
                UPDATE agent_jobs
                SET state = 'queued', base_sha = COALESCE($2, base_sha), updated_at = NOW()
                WHERE parent_job_id = $1 AND state = 'waiting'
                """,
                job_id,
                commit_sha,
            )
        return True

    async def fail_publish(self, *, job_id: str, detail: str) -> None:
        """Mark a job whose patch could not be published.

        The job is failed rather than left ``publishing``: a rejected patch
        (a blocked ``.github/`` change, a leaked credential, a conflict) is a
        human-review situation, and silently retrying it would either spam the
        repository or hide the rejection.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            failed = await conn.fetchval(
                # `state = 'publishing'` as well as the null URL: without it a
                # late failure report could fail a job that had already moved
                # on — including one a later attempt published successfully.
                "UPDATE agent_jobs SET state = 'failed', detail = $2, updated_at = NOW() "
                "WHERE id = $1 AND published_pr_url IS NULL AND state = 'publishing' "
                "RETURNING id",
                job_id,
                detail[:2000],
            )
            if failed is None:
                return
            await conn.execute(
                "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() "
                "WHERE parent_job_id = $1 AND state = 'waiting'",
                job_id,
            )
            attempt_id = await conn.fetchval(
                "SELECT current_attempt_id FROM agent_jobs WHERE id = $1", job_id
            )
            if attempt_id is not None:
                await self._insert_event(
                    conn,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    event_type="error",
                    payload={"phase": "publish_rejected", "detail": detail[:2000]},
                )

    # ── Reaper ─────────────────────────────────────────────────────────

    async def reap_stalled_publishes(self, *, stall_seconds: float = 900.0) -> list[str]:
        """Fail jobs the publisher took and never finished.

        ``claim_for_publish`` moves a job to ``publishing`` in its own
        transaction; if the publisher then crashes, nothing else ever touches
        that row. ``reap_expired`` cannot help — it scans running *attempts*,
        and this job's attempt finished before publishing began — so the job
        sits in ``publishing`` forever, invisible to its owner and to the
        publish queue.

        Failed rather than retried, for the reason this module already gives
        for a lease that expires while publishing: the branch or PR may
        already exist, and a second automatic publish must never happen.
        Returns the job ids it failed.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE agent_jobs
                SET state = 'failed',
                    detail = $2,
                    updated_at = NOW()
                WHERE state = 'publishing'
                  AND published_pr_url IS NULL
                  AND updated_at < NOW() - make_interval(secs => $1)
                RETURNING id
                """,
                stall_seconds,
                "the publisher stopped before finishing; manual review required",
            )
            if rows:
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() "
                    "WHERE parent_job_id = ANY($1::text[]) AND state = 'waiting'",
                    [row["id"] for row in rows],
                )
        return [row["id"] for row in rows]

    async def reap_expired(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Close expired attempts and requeue (or fail) their jobs.

        For each expired ``running`` attempt: mark it ``superseded``, append an
        ``attempt_superseded`` control event, then decide the job's fate —
        requeue while attempts remain, ``cancelled`` if cancellation was
        pending, ``failed`` once ``max_attempts`` is exhausted. Jobs caught in
        ``publishing`` are failed rather than requeued: the publisher may have
        already created the branch/PR, and a second publish must never happen
        automatically. Returns a summary per reaped attempt.
        """
        actions: list[dict[str, Any]] = []
        async with self._pool.acquire() as conn, conn.transaction():
            expired = await conn.fetch(
                """
                SELECT a.id AS attempt_id, a.job_id, a.attempt_no, a.host_id,
                       j.state, j.cancel_requested, j.current_attempt_id,
                       s.thread_id AS sandbox_thread_id,
                       s.active_attempt_id AS sandbox_active_attempt_id,
                       s.admitted_job_id AS sandbox_admitted_job_id,
                       s.last_settled_job_id AS sandbox_last_settled_job_id
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                LEFT JOIN agent_thread_sandboxes s
                  ON s.thread_id = j.thread_id
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
                settled_retained_turn = row["sandbox_last_settled_job_id"] == job_id
                admitted_ambiguously = (
                    row["sandbox_admitted_job_id"] == job_id and not settled_retained_turn
                )
                owns_retained_activation = row["sandbox_active_attempt_id"] == attempt_id
                retained_activation_expired = (
                    admitted_ambiguously or owns_retained_activation
                ) and not settled_retained_turn
                settled_finalization_expired = (
                    settled_retained_turn and row["sandbox_admitted_job_id"] == job_id
                )
                if retained_activation_expired:
                    # The admission receipt identifies a definitely ambiguous
                    # prompt. Even before admission, an expired runner may
                    # have created a container without returning its identity
                    # to the control plane. The preview therefore fails all
                    # expired retained activations closed instead of guessing
                    # that a retry is safe; ordinary CLI/process replacement
                    # at a clean turn boundary remains supported.
                    await conn.execute(
                        """
                        UPDATE agent_thread_sandboxes
                        SET state = 'stopped_dirty',
                            context_status = 'lost',
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
                            idle_deadline = NULL,
                            last_error = CASE
                                WHEN admitted_job_id = $1
                                    THEN 'attempt lease expired after prompt admission'
                                ELSE 'retained activation lease expired'
                            END,
                            updated_at = NOW()
                        WHERE thread_id = $3
                          AND (active_attempt_id = $2 OR admitted_job_id = $1)
                        """,
                        job_id,
                        attempt_id,
                        row["sandbox_thread_id"],
                    )
                    await conn.execute(
                        "UPDATE agent_attempts SET context_status = 'lost' WHERE id = $1",
                        attempt_id,
                    )
                    await self._fail_unrunnable_follow_ups(
                        conn,
                        thread_id=row["sandbox_thread_id"],
                    )
                elif settled_finalization_expired:
                    # The native context boundary is durable; only the final
                    # job transition was lost. Never replay this prompt and
                    # never relabel its usable context LOST. Close admission,
                    # preserve the settled idle deadline, and fail the job for
                    # explicit operator/user review.
                    await conn.execute(
                        """
                        UPDATE agent_thread_sandboxes
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
                        row["sandbox_thread_id"],
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
                elif row["state"] == PUBLISHING:
                    action = FAILED
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'failed', detail = $2, "
                        "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                        job_id,
                        "publish attempt lease expired; manual review required",
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
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() "
                        "WHERE parent_job_id = $1 AND state = 'waiting'",
                        job_id,
                    )
                actions.append({"job_id": job_id, "attempt_id": attempt_id, "action": action})
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
        """Attach a minted grant to the attempt it was minted for.

        Fenced like every other worker write: an attempt that was superseded
        between the claim and this call does not get the grant recorded against
        it, so the renewal pass never picks it up and the capability dies at its
        own TTL.

        **The same three predicates as ``list_renewable_grants``, deliberately.**
        That query is the fence, and this write is what puts a row inside it —
        so a row this accepts and that rejects is a grant nothing will ever
        renew, bound to an attempt whose lease another runner may already have
        taken. The expiry check was missing here while being present there,
        which is exactly the pair that produces two live sandboxes for one job:
        a lease can lapse between the claim and this call without the
        generation changing, because nothing bumps a generation on expiry
        alone.

        Args:
            attempt_id: The attempt the grant belongs to.
            lease_generation: The fence the claim returned.
            grant_id: The gateway's identifier for the grant.

        Returns:
            Whether the row was still current enough to write.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agent_attempts SET grant_id = $3
                WHERE id = $1
                  AND lease_generation = $2
                  AND status = 'running'
                  AND lease_expires_at > NOW()
                """,
                attempt_id,
                lease_generation,
                grant_id,
            )
        return result.endswith(" 1")

    async def list_renewable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """List the grants belonging to attempts that are still current.

        **This is the fence.** A renewal loop that skipped this query and
        renewed whatever it minted would keep a superseded attempt's sandbox
        alive and spending, and the gateway could not catch it — it has no
        attempt state to check. Every predicate here is load-bearing:
        ``status = 'running'`` drops superseded and finished attempts, and
        ``lease_expires_at > NOW()`` drops the ones whose lease died before the
        reaper noticed.

        ``lease_generation`` rides along because the renewal pass may have to
        *end* one of these attempts — a grant the gateway keeps refusing — and
        every write that ends an attempt is fenced. Looking the generation up
        again at that point would read a row that may have moved on in between.

        Args:
            limit: Most rows to return in one pass.

        Returns:
            ``[{"attempt_id", "job_id", "grant_id", "lease_generation"}]`` for
            live attempts only.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id AS attempt_id, job_id, grant_id, lease_generation
                FROM agent_attempts
                WHERE grant_id IS NOT NULL
                  AND status = 'running'
                  AND lease_expires_at > NOW()
                -- **Most urgent first, not lowest id.** `ORDER BY id` returns
                -- the same first `limit` rows on every pass, so in a pool with
                -- more live attempts than that, everything above the cut is
                -- never renewed at all — its grants expire and its jobs die at
                -- a model call, while the loop reports itself healthy.
                --
                -- Ordering by the lease also rotates: a heartbeat pushes an
                -- attempt to the back, so the window moves. It is an
                -- approximation — the grant's own expiry lives at the gateway
                -- and this side cannot sort on it — which is why the caller
                -- logs a truncated pass rather than trusting the ordering to
                -- be sufficient on its own.
                ORDER BY lease_expires_at ASC, id ASC
                LIMIT $1
                """,
                limit,
            )
        return [
            {
                "attempt_id": row["attempt_id"],
                "job_id": row["job_id"],
                "grant_id": row["grant_id"],
                "lease_generation": row["lease_generation"],
            }
            for row in rows
        ]

    async def take_revocable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Claim the grants of attempts that stopped being current, and clear them.

        The complement of :meth:`list_renewable_grants`: a superseded attempt,
        a finished one, or one whose lease lapsed. Returning them and clearing
        the column in one statement is what stops two control-plane replicas —
        or two passes of the same loop — from both trying to revoke, and what
        stops a grant the gateway refuses to revoke from being retried forever.

        The clear is a data-modifying CTE rather than
        ``UPDATE ... RETURNING grant_id``, because ``RETURNING`` reports the
        **new** value — which is the ``NULL`` just written. That version passed
        every in-memory test and handed ``None`` to ``revoke`` against a real
        database; ``tests/integration/storage/test_agent_grant_store.py`` is
        what caught it.

        Clearing before the gateway has confirmed anything is deliberate.
        Revocation is an *acceleration* of the TTL, not the mechanism: the
        capability expires on its own, so a failed revoke costs lateness, and a
        row that kept being retried would cost a call every pass for as long as
        the deployment ran.

        Args:
            limit: Most rows to take in one pass.

        Returns:
            ``[{"attempt_id", "job_id", "grant_id"}]``, now detached from their
            attempts.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH dead AS (
                    SELECT id, job_id, grant_id
                    FROM agent_attempts
                    WHERE grant_id IS NOT NULL
                      AND (status <> 'running' OR lease_expires_at <= NOW())
                    ORDER BY id
                    LIMIT $1
                    FOR UPDATE SKIP LOCKED
                ), cleared AS (
                    UPDATE agent_attempts a SET grant_id = NULL
                    FROM dead WHERE a.id = dead.id
                    RETURNING a.id
                )
                SELECT id AS attempt_id, job_id, grant_id FROM dead
                """,
                limit,
            )
        return [
            {
                "attempt_id": row["attempt_id"],
                "job_id": row["job_id"],
                "grant_id": row["grant_id"],
            }
            for row in rows
        ]

    # ── Admin audit trail (E7) ─────────────────────────────────────────

    async def record_admin_action(
        self,
        *,
        action: str,
        admin_ip: str,
        target_user_id: str | None = None,
        details: dict[str, Any] | None = None,
        success: bool = True,
    ) -> int:
        """Append one row to this service's admin audit trail.

        The gateway's ``admin_audit_log`` stayed with the gateway. This one is
        ours, and it is append-only by convention rather than by constraint —
        there is no code here that updates or deletes a row.

        Args:
            action: Dotted action name, e.g. ``agent.runner_host.drain``.
            admin_ip: Best-effort caller address.
            target_user_id: The user acted upon, where there is one.
            details: Structured context. Never a credential.
            success: Whether the action succeeded.

        Returns:
            The new row's id.
        """
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO agent_admin_audit_log
                    (action, admin_ip, target_user_id, details, success)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                RETURNING id
                """,
                action,
                admin_ip,
                target_user_id,
                json.dumps(details) if details is not None else None,
                success,
            )
