"""API-surface tests for the agent-sandbox job router.

Runs against a minimal in-memory stand-in for the storage layer so the default
(no-database) suite covers the HTTP contract: owner scoping, capability-token
authentication, the fenced-write → 409 mapping, and SSE framing. The store's
own concurrency semantics are pinned separately by the ``dbtest`` suite.

Ported from the gateway with assertions untouched. Two things the router no
longer holds are supplied by the test app instead of by an import:

- **The model catalog and the MCP registry** arrive over HTTP now, so the
  route registry the source stubbed is a fake ``GatewayClient`` here.
- **The auth dependencies** are real now, so nothing stands in for them
  any more: ``get_current_user`` verifies this service's own session token and
  ``verify_admin_access`` compares ``ADMIN_TOKEN``. There is no API-key branch
  to stand in for since 2026-08-04 — it was deleted, along with the operational
  store it read; the session is the only owner credential here.
- **The sandbox's credential** is a gateway grant now, not something the
  claim mints, so the fake gateway client has a grant-minting sibling.

Workspace operations always route through the attempt's enrolled host. The
fake store returns one test host row and individual cases replace the client
factory with their in-memory sandbox manager; there is no global sandbox manager fallback.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.owners import (
    CONNECTION_ID,
    CONNECTION_MODELS,
    SessionAccounts,
    connection_claim,
    connection_row,
    on_connection,
    use_session_secret,
)
from tests.support.storage import AllCapabilities, EmptyAttachments, EmptySkillLibrary, NoAccounts

from cloud_agent import sandbox_manager_routing
from cloud_agent.api import routes as agent_jobs_router, worker, workspace as agent_workspace
from cloud_agent.api._owner_auth import authenticate_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.attachments.models import AttachmentError
from cloud_agent.domain.execution import JobStreamState
from cloud_agent.domain.pending_messages import SettleOutcome
from cloud_agent.goals.models import GoalConflict
from cloud_agent.mcp.registry import mcp_cipher
from cloud_agent.model_preference import ModelPreferenceView
from cloud_agent.storage.repositories.conversation_commands import _goal_round_metadata
from cloud_agent.storage.repositories.goals import _pause_detail
from cloud_agent.thread_stats import summarize_thread
from cloud_agent.tokens import mint_worker_token, parse_residency_mcp_token

pytestmark = pytest.mark.asyncio

_OWNER = "user-owner"
_OTHER = "user-other"
_MCP_ENCRYPTION_KEY = "api-test-mcp-encryption-key"
_MCP_CIPHER = mcp_cipher(_MCP_ENCRYPTION_KEY)
_UPSTREAM_URL = "https://mcp.example.com/mcp/readonly"
_UPSTREAM_TOKEN = "ghu_owner_secret"


def _goal_row(
    goal: Any,
    *,
    goal_id: str = "goal_1",
    state: str = "active",
    revision: int = 1,
    rounds_used: int = 0,
    pause_reason: str | None = None,
    pause_detail: str | None = None,
) -> dict[str, Any]:
    """The conversation row a goal write leaves, as ``agent_threads`` holds it."""
    return {
        "goal_id": goal_id,
        "objective": goal.objective,
        "state": state,
        "pause_reason": pause_reason,
        "pause_detail": pause_detail,
        "completion_basis": None,
        "completed_by_job_id": None,
        "revision": revision,
        "max_rounds": goal.max_rounds,
        "rounds_used": rounds_used,
        "created_at": None,
        "updated_at": None,
    }


class FakeGoalWakeups:
    """The durable scheduling queue, as the owner-facing writes touch it.

    Consumption has its own unit tests (``tests/unit/test_goal_controller.py``)
    with a store that models the lease and the decision; here the queue only has
    to record what an accepted write owes, so that a route test can assert the
    obligation was written in the same breath as the goal.
    """

    def __init__(self, store: FakeStorage) -> None:
        self._store = store
        self.rows: list[dict[str, Any]] = []

    async def arm(
        self,
        *,
        thread_id: str,
        goal_id: str | None,
        revision: int,
        trigger: str,
        trigger_ref: str,
        waiting_job_id: str | None = None,
        conn: Any | None = None,
    ) -> str | None:
        """Record one obligation, or return the pending one it duplicates."""
        for row in self.rows:
            if (
                row["state"] == "pending"
                and row["thread_id"] == thread_id
                and row["trigger"] == trigger
                and row["trigger_ref"] == trigger_ref
            ):
                return row["id"]
        row = {
            "id": f"wakeup_{len(self.rows) + 1}",
            "thread_id": thread_id,
            "goal_id": goal_id,
            "revision": revision,
            "trigger": trigger,
            "trigger_ref": trigger_ref,
            "state": "pending",
            "waiting_job_id": waiting_job_id,
            "attempts": 0,
            "detail": None,
        }
        self.rows.append(row)
        return row["id"]

    async def claim_wakeups(
        self, *, limit: int = 1, thread_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Nothing is consumed here; the decision's own tests own that path."""
        return []

    def invalidate(self, thread_id: str, detail: str) -> None:
        """Pause, clear and resume void every obligation waiting on that goal."""
        for row in self.rows:
            if row["thread_id"] == thread_id and row["state"] == "pending":
                row["state"] = "processed"
                row["detail"] = detail
                row["waiting_job_id"] = None


class FakeGoals:
    """The conversation goal, as the owner routes reach for it.

    Stateful and rule-faithful on purpose: what these routes *are* is the rules
    about identity, revision and state that the Postgres repository applies, so
    a fake that returned a fixed row would make every case pass and prove
    nothing. The rules below are the repository's, restated for one process.
    """

    def __init__(self, store: FakeStorage) -> None:
        self._store = store
        self._next_goal = 1

    def _mint(self) -> str:
        goal_id = f"goal_{self._next_goal}"
        self._next_goal += 1
        return goal_id

    def _owned(self, thread_id: str, user_id: str) -> bool:
        return self._store.thread_owners.get(thread_id) == user_id

    def _goal(self, thread_id: str) -> dict[str, Any] | None:
        return self._store.thread_goals.get(thread_id)

    # -- reads --------------------------------------------------------------
    async def get_goal(self, *, thread_id: str) -> dict[str, Any] | None:
        goal = self._goal(thread_id)
        return None if goal is None else dict(goal)

    async def get_thread_goal_state(self, *, user_id: str, thread_id: str) -> dict[str, Any] | None:
        """The goal and its control version, or ``None`` for a foreign thread.

        The revision is answered even with no goal, because it is what the next
        write's ``expected_revision`` is compared against.
        """
        if not self._owned(thread_id, user_id):
            return None
        return {
            "goal": await self.get_goal(thread_id=thread_id),
            "revision": self._store.goal_revisions.get(thread_id, 0),
        }

    async def has_pending_turn(self, *, thread_id: str, conn: Any | None = None) -> bool:
        return any(
            job["thread_id"] == thread_id and job["state"] in {"queued", "running"}
            for job in self._store.jobs.values()
        )

    # -- owner writes -------------------------------------------------------
    def create_with_job(self, thread_id: str, goal: Any) -> None:
        """The goal a job-creating request writes, in the same transaction."""
        written = _goal_row(goal, goal_id=self._mint(), revision=1)
        self._store.thread_goals[thread_id] = written
        self._store.goal_revisions[thread_id] = written["revision"]

    async def set_goal(
        self,
        *,
        thread_id: str,
        user_id: str,
        goal: Any,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        if not self._owned(thread_id, user_id):
            return None
        current = self._goal(thread_id)
        revision = self._store.goal_revisions.get(thread_id, 0)
        if expected_revision is not None and expected_revision != revision:
            raise GoalConflict("This goal changed since you read it. Reload and try again.")
        objective_changed = current is None or current["objective"] != goal.objective
        # A goal that had finished is authorized again, even in identical words:
        # the last authorization was spent, so reusing its identity would let an
        # old report look current.
        reauthorized = current is not None and current["state"] == "complete"
        restart = objective_changed or current is None or current["state"] != "paused"
        minted = objective_changed or reauthorized
        written = {
            "goal_id": self._mint() if minted else current["goal_id"],
            "objective": goal.objective,
            "state": "active" if restart else "paused",
            "pause_reason": None if restart else (current["pause_reason"] or "user"),
            "pause_detail": (
                None if restart else (current["pause_detail"] or "the goal was edited while paused")
            ),
            "completion_basis": None,
            "completed_by_job_id": None,
            # Every accepted write moves the control version, even one that
            # only changed the cap.
            "revision": revision + 1,
            # The real store's tri-state: a request that says nothing about the
            # bound leaves the goal's own alone, and only an explicit ``null``
            # lifts it. Reading the field directly would clear it every time.
            "max_rounds": (
                goal.max_rounds
                if goal.states_max_rounds or current is None
                else current["max_rounds"]
            ),
            # Rounds belong to the identity, so a minted one starts from zero.
            "rounds_used": 0 if minted else current["rounds_used"],
            "created_at": (current or {}).get("created_at"),
            "updated_at": None,
        }
        self._store.thread_goals[thread_id] = written
        self._store.goal_revisions[thread_id] = written["revision"]
        self._cancel_queued_rounds(thread_id)
        if restart:
            await self._store.goal_wakeups.arm(
                thread_id=thread_id,
                goal_id=written["goal_id"],
                revision=written["revision"],
                trigger="owner",
                trigger_ref=f"set:{written['revision']}",
            )
        return dict(written)

    async def pause_goal(
        self,
        *,
        thread_id: str,
        revision: int,
        reason: str,
        detail: str | None = None,
    ) -> dict[str, Any] | None:
        current = self._goal(thread_id)
        if current is None or current["revision"] != revision or current["state"] != "active":
            return None
        written = {
            **current,
            "state": "paused",
            "pause_reason": reason,
            "pause_detail": detail if detail is not None else _pause_detail(reason),
            "completion_basis": None,
            "completed_by_job_id": None,
            "revision": revision + 1,
        }
        self._store.thread_goals[thread_id] = written
        self._store.goal_revisions[thread_id] = written["revision"]
        self._cancel_queued_rounds(thread_id)
        self._store.goal_wakeups.invalidate(thread_id, "goal paused")
        return dict(written)

    async def resume_goal(
        self, *, thread_id: str, user_id: str, revision: int
    ) -> dict[str, Any] | None:
        if not self._owned(thread_id, user_id):
            return None
        current = self._goal(thread_id)
        if current is None or current["revision"] != revision or current["state"] != "paused":
            return None
        # The real store's rule, modelled here because the API test has to be
        # able to observe it: a bound the goal has already reached is released
        # by the owner's own resume, and a bound it has not reached is kept.
        bound = current.get("max_rounds")
        spent = bound is not None and int(current.get("rounds_used") or 0) >= bound
        written = {
            **current,
            "max_rounds": None if spent else bound,
            "state": "active",
            "pause_reason": None,
            "pause_detail": None,
            "completion_basis": None,
            "completed_by_job_id": None,
            "revision": revision + 1,
        }
        self._store.thread_goals[thread_id] = written
        self._store.goal_revisions[thread_id] = written["revision"]
        self._store.goal_wakeups.invalidate(thread_id, "superseded by resume")
        await self._store.goal_wakeups.arm(
            thread_id=thread_id,
            goal_id=written["goal_id"],
            revision=written["revision"],
            trigger="owner",
            trigger_ref=f"resume:{written['revision']}",
        )
        return dict(written)

    async def clear_goal(
        self, *, thread_id: str, user_id: str, expected_revision: int | None = None
    ) -> bool:
        if not self._owned(thread_id, user_id):
            return False
        revision = self._store.goal_revisions.get(thread_id, 0)
        if expected_revision is not None and expected_revision != revision:
            raise GoalConflict("This goal changed since you read it. Reload and try again.")
        if self._goal(thread_id) is None:
            return False
        del self._store.thread_goals[thread_id]
        # The version keeps moving: the next write compares against the revision
        # clear left behind, not the one before it.
        self._store.goal_revisions[thread_id] = self._store.goal_revisions.get(thread_id, 0) + 1
        self._cancel_queued_rounds(thread_id)
        self._store.goal_wakeups.invalidate(thread_id, "goal cleared")
        return True

    def _cancel_queued_rounds(self, thread_id: str) -> None:
        """The rounds belong to the goal; when it stops, they stop with it."""
        for job in self._store.jobs.values():
            if (
                job["thread_id"] == thread_id
                and job["state"] == "queued"
                and job.get("turn_source") == "goal"
            ):
                job["state"] = "cancelled"
                job["cancel_requested"] = True


class FakeStorage(EmptySkillLibrary, EmptyAttachments, NoAccounts, AllCapabilities):
    """In-memory stand-in exposing the storage surface the router uses."""

    async def ledger_rows_for_job(self, *, job_id: str) -> list[dict[str, Any]]:
        """The stored snapshots for one job, empty before the first collection."""
        return [row for row in self.ledger_rows if row["job_id"] == job_id]

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.archived_threads: dict[str, datetime] = {}
        self.pinned_threads: dict[str, datetime] = {}
        #: The conversation-level goal, exactly as ``agent_threads`` holds it,
        #: and the control version that outlives a cleared one.
        self.thread_goals: dict[str, dict[str, Any]] = {}
        self.goal_revisions: dict[str, int] = {}
        #: Which owner each conversation belongs to, the way
        #: ``agent_threads.user_id`` scopes every goal read and write.
        self.thread_owners: dict[str, str] = {}
        #: How many messages a person has sent each conversation, as
        #: ``agent_threads.user_input_seq`` counts them. Every turn is stamped
        #: with it as it is written; only a person's turn moves it.
        self.thread_input_seqs: dict[str, int] = {}
        self.goal_wakeups = FakeGoalWakeups(self)
        self.goals = FakeGoals(self)
        self.turn_telemetry: dict[str, dict[str, Any]] = {}
        self.turn_summaries: dict[int, dict[str, Any]] = {}
        self.attempt_telemetry: dict[str, list[dict[str, Any]]] = {}
        self.attempt_jobs: dict[int, str] = {}
        self.live_fence: tuple[int, int] | None = None
        self.terminal_readiness: dict[int, bool] = {}
        self.thread_workspace_readiness: dict[str, bool] = {}
        self.workspace_start_fences: list[str] = []
        #: Grant-ledger rows a single-job read sums into the usage fields.
        #: Empty by default: no snapshot stored yet, so the fields stay None.
        self.ledger_rows: list[dict[str, Any]] = []
        self.sandbox_processes: list[dict[str, Any]] = []
        self.sandbox_admissions: list[dict[str, Any]] = []
        self.sandbox_settlements: list[dict[str, Any]] = []
        self.released: list[tuple[str, int]] = []
        self.runner_hosts: dict[str, dict[str, Any]] = {}
        self.active_host: str | None = None
        self.mcp_connections: dict[int, dict[str, Any]] = {}
        self.mcp_capabilities: dict[int, dict[str, Any]] = {}
        self.mcp_bindings: dict[str, list[dict[str, Any]]] = {}
        #: The owner's pinned default model, keyed by owner. There is no
        #: last-used half here: it is derived from ``self.jobs``, exactly as the
        #: repository derives it from the newest turn.
        self.session_forks: dict[str, dict[str, Any]] = {}
        #: The owner's waiting messages, and each thread's two queue facts, the
        #: way ``agent_pending_messages`` and ``agent_threads`` hold them.
        self.pending: dict[str, dict[str, Any]] = {}
        self.queue_paused: dict[str, str] = {}
        self.queue_settled: dict[str, str] = {}
        self._next_pending = 1
        #: The owner's account, approved: terminal fences read it.
        self.local_accounts = SessionAccounts()
        self.local_accounts.add(_OWNER)
        self._next_connection = 1
        self._next_event_id = 1
        self._next_job = 1

    async def get_model_preference(self, user_id: str) -> ModelPreferenceView:
        """The newest owned turn's model and connection, as the repository derives them."""
        jobs = [job for job in self.jobs.values() if job.get("user_id") == user_id]
        if not jobs:
            return ModelPreferenceView()
        newest = max(jobs, key=lambda job: job["id"])
        return ModelPreferenceView(
            last_model=newest["model"], last_model_connection_id=newest.get("model_connection_id")
        )

    # -- MCP connections --
    #
    # Keyed by immutable connection id, which is what a binding names. Labels
    # are display only: two connections to the same service are told apart by
    # id, and nothing here resolves by name.

    async def list_mcp_connections(
        self, *, user_id: str, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        rows = [row for row in self.mcp_connections.values() if row["user_id"] == user_id]
        if not include_deleted:
            rows = [row for row in rows if row["deleted_at"] is None]
        return sorted(rows, key=lambda row: (row["label"], row["id"]))

    async def get_mcp_connection(
        self, *, connection_id: int, user_id: str | None = None
    ) -> dict[str, Any] | None:
        row = self.mcp_connections.get(int(connection_id))
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return None
        return row

    async def get_mcp_capabilities(self, *, connection_id: int) -> dict[str, Any] | None:
        return self.mcp_capabilities.get(int(connection_id))

    async def list_mcp_bindings(self, *, thread_id: str) -> list[dict[str, Any]]:
        return list(self.mcp_bindings.get(thread_id, []))

    async def replace_mcp_bindings(self, *, thread_id: str, bindings: list[dict[str, Any]]) -> None:
        self.mcp_bindings[thread_id] = [
            self._thread_binding(thread_id, binding) for binding in bindings
        ]

    async def disconnect_mcp_connection(self, *, connection_id: int, user_id: str) -> bool:
        """Tombstone one connection and clear its secret, as the store does."""
        row = await self.get_mcp_connection(connection_id=connection_id, user_id=user_id)
        if row is None or row["deleted_at"] is not None:
            return False
        row.update(
            deleted_at=datetime.now(UTC),
            status="disabled",
            access_token_ciphertext=None,
            refresh_token_ciphertext=None,
            client_secret_ciphertext=None,
            expires_at=None,
            authorization_epoch=row["authorization_epoch"] + 1,
            credential_version=row["credential_version"] + 1,
            oauth_generation=row["oauth_generation"] + 1,
        )
        return True

    def connect_mcp(
        self,
        *,
        user_id: str,
        label: str = "GitHub",
        template: str | None = "github",
        url: str | None = _UPSTREAM_URL,
        auth_kind: str = "static",
        token: str | None = _UPSTREAM_TOKEN,
        expires_at: datetime | None = None,
        tools: tuple[str, ...] = ("get_issue",),
        status: str = "active",
        error_kind: str = "none",
        error_detail: str | None = None,
        deleted_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Seed one connection and its discovery result, as the flows will.

        A discovery result is seeded with the row because resolution reads the
        two together: a connection with no snapshot offers nothing, and a case
        about *which* tools a job may use would otherwise pass for the wrong
        reason.
        """
        connection_id = self._next_connection
        self._next_connection += 1
        row: dict[str, Any] = {
            "id": connection_id,
            "user_id": user_id,
            "label": label,
            "template": template,
            "transport": "streamable-http",
            "location": "remote",
            "url": url,
            "launch_spec": None,
            "auth_kind": auth_kind,
            "auth_config": {"header_name": "Authorization", "scheme": "Bearer"},
            "connector_id": None,
            "status": status,
            "error_kind": error_kind,
            "error_detail": error_detail,
            "access_token_ciphertext": _MCP_CIPHER.encrypt(token) if token else None,
            "refresh_token_ciphertext": None,
            "client_secret_ciphertext": None,
            "expires_at": expires_at,
            "scopes": [],
            "issuer": None,
            "resource": None,
            "refresh_rotates": False,
            "client_auth_method": None,
            "registration": None,
            "credential_version": 1,
            "config_revision": 1,
            "authorization_epoch": 1,
            "oauth_generation": 0,
            "capability_revision": 1,
            "account_label": "octocat",
            "last_login_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": deleted_at,
        }
        self.mcp_connections[connection_id] = row
        if tools:
            self.mcp_capabilities[connection_id] = {
                "connection_id": connection_id,
                "capability_revision": 1,
                "config_revision": 1,
                "protocol_version": "2026-07-28",
                "server_name": template or label,
                "server_version": "1.0",
                "tools": [
                    {
                        "name": name,
                        "description": name,
                        "input_schema": {"type": "object"},
                        "annotations": {},
                    }
                    for name in tools
                ],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
                "support": {},
                "discovered_at": None,
            }
        return row

    def _thread_binding(self, thread_id: str, binding: dict[str, Any]) -> dict[str, Any]:
        """One binding row as ``list_mcp_bindings`` returns it."""
        row = self.mcp_connections[int(binding["connection_id"])]
        return {
            "thread_id": thread_id,
            "connection_id": int(binding["connection_id"]),
            "alias": str(binding["alias"]),
            "enabled": bool(binding.get("enabled", True)),
            "selection_pending": bool(binding.get("selection_pending", False)),
            "config_revision": int(binding["config_revision"]),
            "authorization_epoch": int(binding["authorization_epoch"]),
            "capability_revision": int(binding["capability_revision"]),
            "policy_revision": int(binding.get("policy_revision", 1)),
            "rules": dict(binding.get("rules") or {}),
            "resource_rules": dict(binding.get("resource_rules") or {}),
            "prompt_rules": dict(binding.get("prompt_rules") or {}),
            "label": row["label"],
            "transport": row["transport"],
            "location": row["location"],
            "connection_status": row["status"],
            "error_kind": row["error_kind"],
            "error_detail": row["error_detail"],
            "deleted_at": row["deleted_at"],
        }

    def _binding_summary(self, binding: dict[str, Any]) -> dict[str, Any]:
        """One binding as a job response reports it: who, by alias and label."""
        row = self.mcp_connections[int(binding["connection_id"])]
        return {
            "connection_id": int(binding["connection_id"]),
            "alias": str(binding["alias"]),
            "label": row["label"],
        }

    async def list_repo_grants(self, *, user_id: str) -> list[dict[str, Any]]:
        """No GitHub App installations in these tests, which is the usual state.

        The aggregate integrations endpoint reads them, so the fake has to
        answer: the cases here are about MCP connections, and an absent method
        would fail them for a reason that has nothing to do with what they
        check.
        """
        del user_id
        return []

    # -- owner surface --
    async def create_job(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.pop("attachments", None)
        # Admission writes every turn with its connection; a fixture written
        # straight to the store is the same kind of turn.
        kwargs.setdefault("model_connection_id", CONNECTION_ID)
        job_id = f"ajob_{self._next_job:04d}"
        self._next_job += 1
        thread_id = f"athr_{job_id}"
        bindings = kwargs.pop("mcp_bindings", None) or []
        # ``goal`` is the conversation's, not the turn's: it is written below
        # and read back per request, exactly as ``JOB_SELECT`` reports it.
        goal = kwargs.pop("goal", None)
        self.thread_owners[thread_id] = str(kwargs["user_id"])
        job = {
            "id": job_id,
            "thread_id": thread_id,
            "parent_job_id": None,
            "turn_no": 1,
            "state": "queued",
            "cancel_requested": False,
            "terminal_resume_pending": False,
            "current_attempt_id": None,
            "published_pr_url": None,
            "published_commit_sha": None,
            "detail": None,
            "mcp_bindings": sorted(
                (self._binding_summary(binding) for binding in bindings),
                key=lambda summary: (summary["alias"], summary["connection_id"]),
            ),
            "created_at": None,
            "updated_at": None,
            **kwargs,
        }
        # The selection is written once, on the thread, and read per request --
        # the same shape ``agent_mcp_thread_bindings`` has, so a case about a
        # later turn exercises the same lookup the route uses.
        self.mcp_bindings[thread_id] = sorted(
            (self._thread_binding(thread_id, binding) for binding in bindings),
            key=lambda binding: (binding["alias"], binding["connection_id"]),
        )
        job.setdefault("base_sha", None)
        job.setdefault("title", job["task_prompt"].splitlines()[0][:160])
        job.setdefault("metadata", None)
        job.setdefault("execution_profile", "trusted-v1")
        job.setdefault("reasoning_effort", None)
        if goal is not None:
            self.goals.create_with_job(thread_id, goal)
        job["goal"] = self.thread_goals.get(thread_id)
        # As the store counts: the task of a conversation created with a goal is
        # its first message, and the first turn is stamped with the count.
        self.thread_input_seqs[thread_id] = 1 if goal is not None else 0
        job["input_seq_at_create"] = self.thread_input_seqs[thread_id]
        self.jobs[job_id] = job
        self.messages.append(
            {
                "id": len(self.messages) + 1,
                "thread_id": thread_id,
                "job_id": job_id,
                "role": "user",
                "content": job["task_prompt"],
                "created_at": None,
            }
        )
        return job

    async def replayed_submission(
        self, *, user_id: str, client_request_id: str, fingerprint: str | None
    ) -> dict[str, Any] | None:
        """The job this owner's key already made, a conflict, or ``None``.

        The routes ask this before their mutable checks, so a fake that did
        not answer it would send every resend down an ``AttributeError``
        instead of the replay the test is about.
        """
        for job in self.jobs.values():
            if job.get("user_id") != user_id:
                continue
            if job.get("client_request_id") != client_request_id:
                continue
            if job.get("client_request_fingerprint") != fingerprint:
                return {"error": "client_request_conflict"}
            return job
        return None

    async def insert_follow_up(
        self,
        *,
        parent_job_id: str,
        user_id: str,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        reset_reasoning_effort: bool = False,
        skills: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        yield_to_queued: bool = False,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
        model_connection_id: str | None = None,
    ) -> dict[str, Any] | None:
        """A person's next turn, as the store's ``insert_follow_up`` appends it.

        Not a storage method. The store calls that function inside the
        transaction that decided to append the turn, the queue's settlement or
        an idle-only message, and this fake's ``settle_queue`` and
        ``submit_message`` call this the same way. The turn is a person's and
        keeps the conversation's runtime; an automatic round is
        :meth:`create_goal_round`'s.
        """
        del attachments  # staged uploads have their own suite; this fake stages none
        requested = self.jobs.get(parent_job_id)
        if requested is None or requested["user_id"] != user_id:
            return None
        if requested["state"] == "cancelled":
            # Mirrors the store: a cancelled turn is not something a new turn
            # can continue from, and the route reports it as a gone session
            # rather than queueing work behind a turn that never happened.
            return {"error": "sandbox_context_lost"}
        if yield_to_queued and await self.goals.has_pending_turn(
            thread_id=str(requested["thread_id"])
        ):
            # Mirrors the store again, and it has to be asked here rather than
            # by the caller: the real one asks it under the thread lock, and a
            # fake that answered from a caller-side read would pass while the
            # window it exists to close stayed open.
            return {"error": "yielded"}
        parent = max(
            (job for job in self.jobs.values() if job["thread_id"] == requested["thread_id"]),
            key=lambda job: job["turn_no"],
        )
        job_id = f"ajob_{self._next_job:04d}"
        self._next_job += 1
        turn_no = (
            max(
                job["turn_no"]
                for job in self.jobs.values()
                if job["thread_id"] == parent["thread_id"]
            )
            + 1
        )
        thread_id = str(parent["thread_id"])
        self.thread_input_seqs[thread_id] = self.thread_input_seqs.get(thread_id, 0) + 1
        # Mirrors the store: a model is named with its source (null being the
        # Gateway), and an omitted model keeps the parent's model and source
        # together, so a follow-up never changes source without being told.
        source = model_connection_id if model else parent.get("model_connection_id")
        job = {
            **parent,
            "id": job_id,
            "parent_job_id": parent["id"],
            "turn_no": turn_no,
            "base_sha": parent.get("published_commit_sha") or parent.get("base_sha"),
            "task_prompt": prompt,
            "skills": skills if skills is not None else parent.get("skills"),
            "model": model or parent["model"],
            "model_connection_id": source,
            # Mirrors the store: inherit when omitted, and only the explicit
            # reset clears an effort the thread already carries.
            "reasoning_effort": (
                None
                if reset_reasoning_effort
                else (reasoning_effort or parent.get("reasoning_effort"))
            ),
            "state": "queued",
            "cancel_requested": False,
            "terminal_resume_pending": False,
            "current_attempt_id": None,
            "published_pr_url": None,
            "published_commit_sha": None,
            "detail": None,
            # A person's turn, whatever its parent was: it carries the
            # conversation's goal identity and no round of its own.
            "turn_source": "human",
            "goal_id": (self.thread_goals.get(thread_id) or {}).get("goal_id"),
            "goal_round": None,
            "goal_round_kind": None,
            "round_goal_revision": None,
            # A fresh turn has its own report columns, whatever its parent
            # ended on.
            "goal_report_parse": None,
            "goal_report_outcome": None,
            "goal_report_detail": None,
            # Stamped with the count this message moved the conversation to.
            "input_seq_at_create": self.thread_input_seqs[thread_id],
            "created_at": None,
            "updated_at": None,
            "pinned_at": self.pinned_threads.get(parent["thread_id"]),
            # The live goal, under the job's keys, the way every join read
            # reports it.
            "goal": self.thread_goals.get(str(parent["thread_id"])),
            # The child is its own submission, so it carries its own key
            # rather than inheriting the parent's.
            "client_request_id": client_request_id,
            "client_request_fingerprint": client_request_fingerprint,
        }
        self.jobs[job_id] = job
        self.messages.append(
            {
                "id": len(self.messages) + 1,
                "thread_id": job["thread_id"],
                "job_id": job_id,
                "role": "user",
                "content": prompt,
                "created_at": None,
            }
        )
        return job

    async def create_goal_round(
        self,
        *,
        thread_id: str,
        goal_id: str,
        revision: int,
        kind: str,
        round_number: int,
        prompt: str,
        client_request_id: str,
        client_request_fingerprint: str,
    ) -> dict[str, Any] | None:
        """One automatic round, as the store's ``create_goal_round`` appends it.

        The only write of a ``goal`` turn. Like the store's, it continues the
        newest turn with that turn's runtime, model, effort and skills, adds the
        goal report to what its metadata requires of a host, and is stamped with
        the conversation's input count without moving it. A key the
        conversation already used appends nothing, which is how a replayed
        decision finds the round it made. The controller's ``conn`` and
        ``wakeup_id`` are not modelled: nothing consumes this fake's wakeups.
        """
        turns = self._thread_turns(thread_id)
        if not turns:
            return None
        if any(job.get("client_request_id") == client_request_id for job in turns):
            return None
        parent = turns[-1]
        job_id = f"ajob_{self._next_job:04d}"
        self._next_job += 1
        job = {
            **parent,
            "id": job_id,
            "parent_job_id": parent["id"],
            "turn_no": parent["turn_no"] + 1,
            "base_sha": parent.get("published_commit_sha") or parent.get("base_sha"),
            "task_prompt": prompt,
            "metadata": _goal_round_metadata(parent.get("metadata")),
            "state": "queued",
            "cancel_requested": False,
            "terminal_resume_pending": False,
            "current_attempt_id": None,
            "published_pr_url": None,
            "published_commit_sha": None,
            "detail": None,
            "turn_source": "goal",
            "goal_id": goal_id,
            "goal_round": round_number,
            "goal_round_kind": kind,
            "round_goal_revision": revision,
            "goal_report_parse": None,
            "goal_report_outcome": None,
            "goal_report_detail": None,
            "input_seq_at_create": self.thread_input_seqs.get(thread_id, 0),
            "created_at": None,
            "updated_at": None,
            "pinned_at": self.pinned_threads.get(thread_id),
            "goal": self.thread_goals.get(thread_id),
            "client_request_id": client_request_id,
            "client_request_fingerprint": client_request_fingerprint,
        }
        self.jobs[job_id] = job
        self.messages.append(
            {
                "id": len(self.messages) + 1,
                "thread_id": thread_id,
                "job_id": job_id,
                "role": "user",
                "content": prompt,
                "created_at": None,
            }
        )
        return job

    # -- Pending messages --
    #
    # The queue the message route writes. A message that is sent becomes a turn
    # through ``insert_follow_up`` above, as the store's settlement sends one,
    # so the cases about a turn's fields keep asserting on the same fake.

    def _thread_turns(self, thread_id: str) -> list[dict[str, Any]]:
        return sorted(
            (job for job in self.jobs.values() if job["thread_id"] == thread_id),
            key=lambda job: job["turn_no"],
        )

    def _thread_live(self, thread_id: str) -> bool:
        return any(job["state"] in ("queued", "running") for job in self._thread_turns(thread_id))

    def _waiting(self, thread_id: str) -> list[dict[str, Any]]:
        return sorted(
            (row for row in self.pending.values() if row["thread_id"] == thread_id),
            key=lambda row: row["position"],
        )

    def _pause(self, thread_id: str, reason: str) -> None:
        """Mirrors ``PAUSE_QUEUE_SQL``: only a queue with something in it, first reason wins."""
        if self._waiting(thread_id):
            self.queue_paused.setdefault(thread_id, reason)

    def _continue_if_idle(self, thread_id: str) -> None:
        turns = self._thread_turns(thread_id)
        if turns and not self._thread_live(thread_id):
            self.queue_paused.pop(thread_id, None)
            self.queue_settled[thread_id] = turns[-1]["id"]

    async def latest_turn(self, *, thread_id: str) -> dict[str, Any] | None:
        turns = self._thread_turns(thread_id)
        return None if not turns else await self.get_job(turns[-1]["id"])

    async def replayed_message(
        self, *, user_id: str, client_request_id: str, fingerprint: str | None
    ) -> dict[str, Any] | None:
        for row in self.pending.values():
            if row["user_id"] == user_id and row["client_request_id"] == client_request_id:
                if row["client_request_fingerprint"] != fingerprint:
                    return {"error": "client_request_conflict"}
                return {"message": dict(row)}
        return None

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
        model_connection_id: str | None = None,
    ) -> dict[str, Any]:
        turns = self._thread_turns(thread_id)
        if not turns or turns[-1]["user_id"] != user_id:
            return {"error": "not_found"}
        if client_request_id is not None:
            made = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            if made is not None:
                return made if made.get("error") else {"job_id": made["id"]}
            waiting = await self.replayed_message(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            if waiting is not None:
                return waiting
        if thread_id in self.archived_threads:
            return {"error": "session_archived"}
        if require_idle:
            sandbox = turns[-1].get("thread_sandbox") or {}
            if (
                turns[-1]["id"] != expected_latest_job_id
                or any(turn["state"] not in ("succeeded", "failed", "cancelled") for turn in turns)
                or self._waiting(thread_id)
                or sandbox.get("workspace_publication_state") != "published"
            ):
                return {"error": "conversation_changed"}
            made = await self.insert_follow_up(
                parent_job_id=expected_latest_job_id,
                user_id=user_id,
                prompt=prompt,
                model=model,
                model_connection_id=model_connection_id,
                reasoning_effort=reasoning_effort,
                reset_reasoning_effort=reset_reasoning_effort,
                skills=skills,
                attachments=attachments,
                yield_to_queued=True,
                client_request_id=client_request_id,
                client_request_fingerprint=client_request_fingerprint,
            )
            if made is None:
                return {"error": "conversation_changed"}
            if made.get("error") is not None:
                return made
            self.queue_paused.pop(thread_id, None)
            self.queue_settled[thread_id] = expected_latest_job_id
            return {"job_id": made["id"]}
        self._continue_if_idle(thread_id)
        message_id = f"apmsg_{self._next_pending:04d}"
        self._next_pending += 1
        self.pending[message_id] = {
            "id": message_id,
            "thread_id": thread_id,
            "user_id": user_id,
            "position": self._next_pending,
            "state": "queued",
            "revision": 1,
            "prompt": prompt,
            "skill_ids": skill_ids,
            "attachment_ids": attachment_ids or None,
            "attachments": [],
            "model": model,
            "model_connection_id": model_connection_id if model else None,
            "reasoning_effort": reasoning_effort,
            "client_request_id": client_request_id,
            "client_request_fingerprint": client_request_fingerprint,
            "last_error": None,
            "created_at": None,
            "updated_at": None,
        }
        return {"message": dict(self.pending[message_id])}

    async def queue_state(self, *, thread_id: str, user_id: str) -> dict[str, Any] | None:
        turns = self._thread_turns(thread_id)
        if not turns or turns[-1]["user_id"] != user_id:
            return None
        return {
            "queue_paused_reason": self.queue_paused.get(thread_id),
            "messages": [dict(row) for row in self._waiting(thread_id)],
        }

    def _changeable(
        self, *, thread_id: str, user_id: str, message_id: str, revision: int
    ) -> dict[str, Any]:
        row = self.pending.get(message_id)
        if row is None or row["thread_id"] != thread_id or row["user_id"] != user_id:
            return {"error": "not_found"}
        if row["revision"] != revision:
            return {"error": "revision_conflict", "current": dict(row)}
        return row

    async def edit_message(
        self,
        *,
        thread_id: str,
        user_id: str,
        message_id: str,
        revision: int,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        row = self._changeable(
            thread_id=thread_id, user_id=user_id, message_id=message_id, revision=revision
        )
        if row.get("error"):
            return row
        row.update(changes)
        if row["state"] == "queued":
            row["last_error"] = None
        row["revision"] += 1
        return {"message": dict(row)}

    async def delete_message(
        self, *, thread_id: str, user_id: str, message_id: str, revision: int
    ) -> dict[str, Any]:
        row = self._changeable(
            thread_id=thread_id, user_id=user_id, message_id=message_id, revision=revision
        )
        if row.get("error"):
            return row
        del self.pending[message_id]
        return {"deleted": message_id}

    async def resume_queue(self, *, thread_id: str, user_id: str) -> dict[str, Any]:
        turns = self._thread_turns(thread_id)
        if not turns or turns[-1]["user_id"] != user_id:
            return {"error": "not_found"}
        if thread_id in self.archived_threads:
            return {"error": "session_archived"}
        self.queue_paused.pop(thread_id, None)
        self._continue_if_idle(thread_id)
        return {"queue_paused_reason": None}

    async def settle_queue(self, *, thread_id: str, prepared: Any = None) -> SettleOutcome:
        """Mirrors the store's settlement, sending through ``insert_follow_up``."""
        turns = self._thread_turns(thread_id)
        if not turns or self._thread_live(thread_id):
            return SettleOutcome()
        latest = turns[-1]
        if self.queue_settled.get(thread_id) != latest["id"]:
            self.queue_settled[thread_id] = latest["id"]
            reason = {"failed": "failed", "cancelled": "stopped"}.get(latest["state"])
            if reason is not None and self._waiting(thread_id):
                self._pause(thread_id, reason)
                return SettleOutcome()
        if thread_id in self.archived_threads or thread_id in self.queue_paused:
            return SettleOutcome()
        waiting = self._waiting(thread_id)
        if not waiting or waiting[0]["state"] != "queued":
            return SettleOutcome()
        head = waiting[0]
        if prepared is None or (
            prepared.message_id,
            prepared.revision,
            prepared.parent_job_id,
        ) != (head["id"], head["revision"], latest["id"]):
            return SettleOutcome(head=dict(head), parent_job_id=latest["id"])
        if prepared.error is not None:
            head.update(state="held", last_error=prepared.error, revision=head["revision"] + 1)
            return SettleOutcome()
        job = await self.insert_follow_up(
            parent_job_id=latest["id"],
            user_id=head["user_id"],
            prompt=head["prompt"],
            model=prepared.model,
            model_connection_id=prepared.model_connection_id,
            reasoning_effort=prepared.reasoning_effort,
            reset_reasoning_effort=prepared.reset_reasoning_effort,
            skills=prepared.skills,
            attachments=prepared.attachments,
            yield_to_queued=True,
            client_request_id=head["client_request_id"],
            client_request_fingerprint=head["client_request_fingerprint"],
        )
        if job is None or job.get("error"):
            if job is not None and job.get("error") == "sandbox_context_lost":
                head.update(
                    state="held",
                    last_error="This chat's retained sandbox has an indeterminate or lost "
                    "context and cannot safely accept another message.",
                    revision=head["revision"] + 1,
                )
            return SettleOutcome()
        del self.pending[head["id"]]
        return SettleOutcome(job=job, message_id=head["id"])

    async def threads_to_settle(self, *, limit: int = 50, after: str | None = None) -> list[str]:
        threads = sorted({row["thread_id"] for row in self.pending.values()})
        return [thread_id for thread_id in threads if after is None or thread_id > after][:limit]

    async def _copy_fork_history(
        self, *, source_job_id: str, user_id: str
    ) -> dict[str, Any] | None:
        source = self.jobs.get(source_job_id)
        if source is None or source["user_id"] != user_id:
            return None
        if source["state"] not in {"succeeded", "failed", "cancelled"}:
            return None
        new_thread_id = f"athr_fork_{self._next_job:04d}"
        turns = sorted(
            (
                job
                for job in self.jobs.values()
                if job["thread_id"] == source["thread_id"] and job["turn_no"] <= source["turn_no"]
            ),
            key=lambda job: job["turn_no"],
        )
        copied_id: dict[str, str] = {}
        previous: str | None = None
        for turn in turns:
            copy_id = f"ajob_{self._next_job:04d}"
            self._next_job += 1
            self.jobs[copy_id] = {
                **turn,
                "id": copy_id,
                "thread_id": new_thread_id,
                "parent_job_id": previous,
                "state": (
                    turn["state"]
                    if turn["state"] in {"succeeded", "failed", "cancelled"}
                    else "cancelled"
                ),
                "cancel_requested": False,
                "terminal_resume_pending": False,
                "current_attempt_id": None,
                "published_pr_url": None,
                "fork_source_job_id": turn["id"],
            }
            copied_id[turn["id"]] = copy_id
            previous = copy_id
        source_messages = sorted(
            (
                message
                for message in self.messages
                if message["thread_id"] == source["thread_id"]
                and self.jobs[message["job_id"]]["turn_no"] <= source["turn_no"]
            ),
            key=lambda message: (
                self.jobs[message["job_id"]]["turn_no"],
                0 if message["role"] == "user" else 1,
                message["id"],
            ),
        )
        for message in source_messages:
            self.messages.append(
                {
                    "id": len(self.messages) + 1,
                    "thread_id": new_thread_id,
                    "job_id": copied_id[message["job_id"]],
                    "role": message["role"],
                    "content": message["content"],
                    "created_at": message["created_at"],
                }
            )
        return self.jobs[copied_id[source["id"]]]

    async def prepare_session_fork(
        self,
        *,
        source_job_id: str,
        user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Model the new same-container native-fork saga for route tests."""
        source = self.jobs.get(source_job_id)
        if source is None or source["user_id"] != user_id:
            return None
        if source["state"] != "succeeded":
            return {"error": "anchor_not_settled"}
        sandbox = source.get("thread_sandbox")
        if not isinstance(sandbox, dict) or sandbox.get("shared_live") is not True:
            return {"error": "not_shared_live"}
        existing = next(
            (
                row
                for row in self.session_forks.values()
                if row["user_id"] == user_id and row["idempotency_key"] == idempotency_key
            ),
            None,
        )
        if existing is not None:
            return existing
        operation_id = f"asfork_{len(self.session_forks) + 1:04d}"
        operation = {
            "id": operation_id,
            "state": "preparing",
            "user_id": user_id,
            "idempotency_key": idempotency_key,
            "host": sandbox.get("host", "runner-a"),
            "sandbox_id": sandbox["sandbox_id"],
            "sandbox_generation": sandbox.get("generation", 1),
            "container_id": sandbox["container_id"],
            "runtime_id": sandbox.get("runtime_id", source["runtime"]),
            "runtime_version": sandbox.get("runtime_version"),
            "sandbox_image_digest": sandbox.get("sandbox_image_digest"),
            "resident_slot_capacity": sandbox.get("resident_slot_capacity", 8),
            "memory_limit": sandbox.get("memory_limit", "4g"),
            "cpu_limit": sandbox.get("cpu_limit", "2"),
            "disk_limit_mib": sandbox.get("disk_limit_mib", 0),
            "stop_timeout_seconds": sandbox.get("stop_timeout_seconds", 10),
            "source_thread_id": source["thread_id"],
            "origin_thread_id": sandbox.get("origin_thread_id", source["thread_id"]),
            "source_job_id": source_job_id,
            "source_native_session_id": sandbox["native_session_id"],
            "source_native_turn_no": sum(
                1
                for turn in self.jobs.values()
                if turn["thread_id"] == source["thread_id"]
                and turn["turn_no"] <= source["turn_no"]
                and turn["state"] == "succeeded"
            ),
            "source_activation_generation": sandbox.get("activation_generation", 1),
            "resident_slot_id": sandbox.get("resident_slot_id", 0),
            "target_thread_id": f"athr_fork_{self._next_job:04d}",
            "target_resident_slot_id": 1,
            "group_members": sandbox.get("group_members")
            or [
                {
                    "thread_id": source["thread_id"],
                    "native_session_id": sandbox["native_session_id"],
                    "context_status": "live",
                    "activation_generation": sandbox.get("activation_generation", 1),
                    "process_generation": 1,
                    "residency_generation": 1,
                    "resident_state": "live",
                    "resident_slot_id": sandbox.get("resident_slot_id", 0),
                    "last_settled_job_id": source_job_id,
                }
            ],
            "source_job": source,
        }
        self.session_forks[operation_id] = operation
        return operation

    async def claim_session_fork_materialization(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
        lease_seconds: float,
    ) -> bool:
        operation = self.session_forks.get(operation_id)
        if operation is None or operation["state"] != "preparing":
            return False
        if operation.get("coordinator_token") not in {None, coordinator_token}:
            return False
        operation["coordinator_token"] = coordinator_token
        operation["coordinator_lease_seconds"] = lease_seconds
        return True

    async def release_session_fork_materialization(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
    ) -> bool:
        operation = self.session_forks.get(operation_id)
        if operation is None or operation.get("coordinator_token") != coordinator_token:
            return False
        operation["coordinator_token"] = None
        return True

    async def establish_session_fork_residencies(
        self, *, operation_id: str, coordinator_token: str, ttl_seconds: float
    ) -> dict[str, Any] | None:
        operation = self.session_forks.get(operation_id)
        if operation is None or operation.get("coordinator_token") != coordinator_token:
            return None
        source = self.jobs[operation["source_job_id"]]
        return {
            "source": {
                "thread_id": operation["source_thread_id"],
                "generation": 11,
                "previous_grant_id": None,
                "establishing_attempt_id": f"fork-source:{operation_id}",
                "user_id": source["user_id"],
                "model": source["model"],
            },
            "target": {
                "thread_id": operation["target_thread_id"],
                "generation": 12,
                "previous_grant_id": None,
                "establishing_attempt_id": f"fork-target:{operation_id}",
                "user_id": source["user_id"],
                "model": source["model"],
            },
        }

    async def link_residency_grant(self, *, thread_id: str, generation: int, grant_id: str) -> bool:
        return True

    async def release_residency(self, *, thread_id: str, generation: int, host: str) -> str | None:
        return None

    async def record_session_fork_materialized(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any] | None:
        operation = self.session_forks.get(operation_id)
        if (
            operation is None
            or operation.get("coordinator_token") != coordinator_token
            or receipt.get("operation_id") != operation_id
        ):
            return None
        operation.update(
            state="materialized",
            coordinator_token=None,
            host_receipt=receipt,
            target_native_session_id=receipt["target_native_session_id"],
            source_process_generation=receipt["source_process_generation"],
            target_process_generation=receipt["target_process_generation"],
        )
        return operation

    async def finalize_session_fork(self, *, operation_id: str) -> dict[str, Any] | None:
        operation = self.session_forks.get(operation_id)
        if operation is None or operation["state"] not in {"materialized", "ready"}:
            return None
        if operation["state"] == "ready":
            return self.jobs.get(operation["target_anchor_job_id"])
        copied = await self._copy_fork_history(
            source_job_id=operation["source_job_id"],
            user_id=operation["user_id"],
        )
        if copied is None or copied["thread_id"] != operation["target_thread_id"]:
            return None
        source_sandbox = self.jobs[operation["source_job_id"]]["thread_sandbox"]
        target_member = {
            "thread_id": copied["thread_id"],
            "native_session_id": operation["target_native_session_id"],
            "context_status": "live",
            "activation_generation": 0,
            "process_generation": operation["target_process_generation"],
            "residency_generation": 12,
            "resident_state": "live",
            "resident_slot_id": operation["target_resident_slot_id"],
            "last_settled_job_id": copied["id"],
        }
        shared_members = [*source_sandbox.get("group_members", []), target_member]
        for child in self.jobs.values():
            if child["thread_id"] == copied["thread_id"]:
                child["thread_sandbox"] = {
                    **source_sandbox,
                    "thread_id": copied["thread_id"],
                    "native_session_id": operation["target_native_session_id"],
                    "resident_slot_id": operation["target_resident_slot_id"],
                    "group_members": shared_members,
                }
        operation.update(state="ready", target_anchor_job_id=copied["id"])
        return copied

    async def get_thread_for_job(
        self,
        *,
        job_id: str,
        user_id: str,
        cursor: str | None = None,
        after: str | None = None,
        limit: int = 50,
        anchor: bool = False,
    ) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        # The real read joins the thread and refuses a deleted conversation.
        if job is None or job["user_id"] != user_id or job.get("deleted_at") is not None:
            return None
        thread_id = job["thread_id"]
        jobs = sorted(
            (
                {**item, "pinned_at": self.pinned_threads.get(thread_id)}
                for item in self.jobs.values()
                if item["thread_id"] == thread_id
            ),
            key=lambda item: item["turn_no"],
        )
        return {
            "id": thread_id,
            "repo": job["repo"],
            "title": jobs[0].get("title") or jobs[0]["task_prompt"].splitlines()[0],
            "archived_at": self.archived_threads.get(thread_id),
            "created_at": None,
            "updated_at": None,
            "messages": sorted(
                (m for m in self.messages if m["thread_id"] == thread_id),
                key=lambda m: (
                    self.jobs[m["job_id"]]["turn_no"],
                    0 if m["role"] == "user" else 1,
                    m["id"],
                ),
            ),
            "jobs": jobs,
            "pending_messages": [dict(row) for row in self._waiting(thread_id)],
            "queue_paused_reason": self.queue_paused.get(thread_id),
        }

    async def thread_stats(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        # The real read joins the thread and refuses a deleted conversation.
        if job is None or job["user_id"] != user_id or job.get("deleted_at") is not None:
            return None
        thread_id = job["thread_id"]
        thread_jobs = sorted(
            (item for item in self.jobs.values() if item["thread_id"] == thread_id),
            key=lambda item: item["turn_no"],
        )
        job_ids = [item["id"] for item in thread_jobs]
        attempts = [{"job_id": item["id"], "started_at": None} for item in thread_jobs]
        events = [event for event in self.events if event["job_id"] in set(job_ids)]
        # This stand-in stamps no clocks (``append_event`` stores
        # ``created_at: None``), so timings are zero; the reducer's arithmetic
        # is pinned by ``tests/unit/test_thread_stats.py``.
        return {
            "thread_id": thread_id,
            "stats": summarize_thread(job_ids=job_ids, attempts=attempts, events=events),
        }

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        return {
            **job,
            "pinned_at": self.pinned_threads.get(job["thread_id"]),
            "archived_at": self.archived_threads.get(job["thread_id"]),
            # The live goal, under the job's keys — the shape ``JOB_SELECT``
            # gives every read, which is where the claim and the response
            # builder both look for it.
            "goal": self.thread_goals.get(str(job["thread_id"])),
        }

    async def job_stream_page(
        self, *, job_id: str, user_id: str, after_id: int, limit: int
    ) -> tuple[list[dict[str, Any]], JobStreamState] | None:
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        events = await self.list_events_after(job_id=job_id, after_id=after_id, limit=limit)
        return events, JobStreamState(job["state"], job.get("published_pr_url"))

    async def latest_assistant_message_state(self, *, job_id: str) -> dict[str, Any] | None:
        said = [
            message
            for message in self.messages
            if message["job_id"] == job_id and message["role"] == "assistant"
        ]
        if not said:
            return None
        newest = said[-1]
        return {
            "content": newest["content"],
            # A runtime that clipped its text says so on the event, and the
            # store carries it onto the row. The fake does the same.
            "truncated": newest.get("truncated") is True,
        }

    async def list_thread_events_after(
        self, *, job_id: str, user_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]] | None:
        """Every event of the containing thread, labelled by turn."""
        job = self.jobs.get(job_id)
        # The real read joins the thread and refuses a deleted conversation.
        if job is None or job.get("user_id") != user_id or job.get("deleted_at") is not None:
            return None
        thread_id = job.get("thread_id")
        turns = {
            other["id"]: other.get("turn_no") or 1
            for other in self.jobs.values()
            if other.get("thread_id") == thread_id
        }
        rows = [
            {**event, "turn_no": turns[event["job_id"]]}
            for event in self.events
            if event["job_id"] in turns and event["id"] > after_id
        ]
        return sorted(rows, key=lambda row: row["id"])[:limit]

    async def job_turn_telemetry(
        self, *, job_id: str, attempt_id: int | None = None
    ) -> dict[str, Any] | None:
        """Read only the selected job's requested attempt, or its newest one."""
        rows = await self.job_attempts_telemetry(job_id=job_id)
        if attempt_id is not None:
            return next((row for row in rows if row["attempt_id"] == attempt_id), None)
        return rows[-1] if rows else None

    async def job_attempts_telemetry(self, *, job_id: str) -> list[dict[str, Any]]:
        """All attempt windows, retaining their identity for the owner UI."""
        recorded = self.attempt_telemetry.get(job_id)
        if recorded is not None:
            return recorded
        newest = self.turn_telemetry.get(job_id)
        if newest is None:
            return []
        attempt_id = self.jobs[job_id].get("current_attempt_id") or 100
        return [{"attempt_id": attempt_id, "attempt_no": 1, **newest}]

    async def trajectory_timings(
        self, *, job_ids: list[str], user_id: str, max_attempts: int
    ) -> dict[str, Any]:
        """Return the compact, owner-fenced projection used by the timeline."""
        owned = [
            job_id
            for job_id in job_ids
            if (job := self.jobs.get(job_id)) is not None
            and job.get("user_id") == user_id
            and job.get("deleted_at") is None
        ]
        attempts = [
            {"job_id": job_id, **row}
            for job_id in owned
            for row in await self.job_attempts_telemetry(job_id=job_id)
        ]
        compact = []
        if len(attempts) <= max_attempts:
            for row in attempts:
                tool_calls = row.get("tool_calls") or []
                compact.append(
                    {
                        **row,
                        "tool_time_ms": row.get("tool_time_ms"),
                        "tool_calls_truncated": bool(row.get("tool_calls_truncated")),
                        "tool_timing_exact": bool(tool_calls)
                        and all(call.get("timing_source") == "exact" for call in tool_calls),
                    }
                )
        return {
            "owned_count": len(owned),
            "attempt_count": len(attempts),
            "attempts": compact,
        }

    async def record_attempt_resources(
        self, *, attempt_id: int, lease_generation: int, rollup: dict[str, Any]
    ) -> bool:
        """Store one window's rollup, fenced and idempotent like the real store."""
        if not self._fenced(attempt_id, lease_generation):
            return False
        job_id = self.attempt_jobs.get(attempt_id)
        if job_id is None:
            return False
        # Upsert, never append: a rollup describes a closed window, so a
        # retried post is the same window said twice.
        self.turn_telemetry[job_id] = {
            **self.turn_telemetry.get(job_id, {}),
            **rollup,
            "attempt_id": attempt_id,
        }
        return True

    async def record_attempt_turn_summary(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        measurement_id: str,
        report_seq: int,
        state: str,
        summary: dict[str, Any],
    ) -> str:
        """Apply the store's ordering rules to one cumulative snapshot.

        Deliberately a real reimplementation of the ordering rather than a
        dict assignment: the HTTP mapping for ``duplicate`` and ``conflict``
        is only meaningful if the fake can produce them the way the SQL does.
        The database clock, the row lock and the JSONB round trip are what the
        ``dbtest`` suite covers; this covers the contract above the store.
        """
        if not self._fenced(attempt_id, lease_generation):
            return "lease_lost"
        stored = self.turn_summaries.get(attempt_id)
        if stored is None:
            self.turn_summaries[attempt_id] = {
                "seq": report_seq,
                "summary": summary,
                "measurement_id": measurement_id,
                "state": state,
            }
            return "stored"
        if report_seq < stored["seq"]:
            return "conflict"
        if report_seq == stored["seq"]:
            return "duplicate" if stored["summary"] == summary else "conflict"
        if stored["state"] == "final" and state == "collecting":
            return "conflict"
        if stored["measurement_id"] != measurement_id:
            return "conflict"
        self.turn_summaries[attempt_id] = {
            "seq": report_seq,
            "summary": summary,
            "measurement_id": measurement_id,
            "state": state,
        }
        return "stored"

    async def record_thread_sandbox_process(self, **kwargs: Any) -> bool:
        """Record the process RPC under the worker and residency fences."""
        if not self._fenced(kwargs["attempt_id"], kwargs["lease_generation"]):
            return False
        self.sandbox_processes.append(dict(kwargs))
        return True

    async def admit_thread_sandbox_turn(self, **kwargs: Any) -> bool:
        """Record the at-most-once admission RPC under the worker fence."""
        if not self._fenced(kwargs["attempt_id"], kwargs["lease_generation"]):
            return False
        self.sandbox_admissions.append(dict(kwargs))
        return True

    async def settle_thread_sandbox(self, **kwargs: Any) -> dict[str, Any] | None:
        """Record settlement and return the minimal retained mapping response."""
        if not self._fenced(kwargs["attempt_id"], kwargs["lease_generation"]):
            return None
        self.sandbox_settlements.append(dict(kwargs))
        job = self.jobs[kwargs["job_id"]]
        return {
            "thread_id": kwargs["thread_id"],
            "sandbox_id": "asbx_test",
            "generation": kwargs["sandbox_generation"],
            "host": "runner-a",
            "container_name": "freeinference-test",
            "runtime_id": "claude-code",
            "execution_profile": job["execution_profile"],
            "state": "running",
            "context_status": kwargs["context_status"],
            "activation_generation": kwargs["activation_generation"],
            "process_generation": 4,
            "residency_generation": kwargs["residency_generation"],
            "admitted_job_id": kwargs["job_id"],
            "last_settled_job_id": kwargs["job_id"],
            "native_session_id": kwargs["native_session_id"],
        }

    async def list_jobs(
        self,
        *,
        user_id: str,
        limit: int = 50,
        archived: bool = False,
        repo: str | None = None,
        temporary: bool = False,
    ) -> list[dict[str, Any]]:
        jobs = [
            {**job, "pinned_at": self.pinned_threads.get(job["thread_id"])}
            for job in self.jobs.values()
            if job["user_id"] == user_id
            and (job["thread_id"] in self.archived_threads) is archived
            and (repo is None or job["repo"] == repo)
            and (not temporary or job["repo"] is None)
        ]
        minimum = datetime.min.replace(tzinfo=UTC)
        jobs.sort(
            key=lambda job: (
                job["pinned_at"] is not None,
                job["pinned_at"] or minimum,
                job.get("created_at") or minimum,
            ),
            reverse=True,
        )
        return jobs[:limit]

    async def list_projects(self, *, user_id: str, archived: bool = False) -> list[dict[str, Any]]:
        projects: dict[str, dict[str, Any]] = {}
        for job in self.jobs.values():
            if job["user_id"] != user_id:
                continue
            if (job["thread_id"] in self.archived_threads) is not archived:
                continue
            project = projects.setdefault(
                job["repo"],
                {
                    "repo": job["repo"],
                    "task_count": 0,
                    "active_count": 0,
                    "last_activity_at": None,
                    "pinned_count": 0,
                    "pinned_at": None,
                    "_threads": set(),
                    "_pinned_threads": set(),
                },
            )
            project["_threads"].add(job["thread_id"])
            project["task_count"] = len(project["_threads"])
            if job["state"] in {"queued", "running", "publishing"}:
                project["active_count"] += 1
            pinned_at = self.pinned_threads.get(job["thread_id"])
            if pinned_at is not None:
                project["_pinned_threads"].add(job["thread_id"])
                project["pinned_count"] = len(project["_pinned_threads"])
                if project["pinned_at"] is None or pinned_at > project["pinned_at"]:
                    project["pinned_at"] = pinned_at
            created = job.get("created_at")
            if created is not None and (
                project["last_activity_at"] is None or created > project["last_activity_at"]
            ):
                project["last_activity_at"] = created
        for project in projects.values():
            project.pop("_threads")
            project.pop("_pinned_threads")
        minimum = datetime.min.replace(tzinfo=UTC)
        return sorted(
            projects.values(),
            key=lambda project: (
                project["pinned_at"] is not None,
                project["pinned_at"] or minimum,
                project["last_activity_at"] or minimum,
            ),
            reverse=True,
        )

    async def set_thread_archived(
        self, *, job_id: str, user_id: str, archived: bool
    ) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        thread_id = job["thread_id"]
        if archived:
            archived_at = self.archived_threads.setdefault(thread_id, datetime.now(UTC))
            self._pause(thread_id, "archived")
        else:
            self.archived_threads.pop(thread_id, None)
            archived_at = None
        return {"thread_id": thread_id, "archived_at": archived_at}

    async def set_thread_title(
        self, *, job_id: str, user_id: str, title: str
    ) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id or job.get("deleted_at") is not None:
            return None
        for item in self.jobs.values():
            if item["thread_id"] == job["thread_id"]:
                item["title"] = title
        return {"thread_id": job["thread_id"], "title": title}

    async def set_thread_pinned(
        self, *, job_id: str, user_id: str, pinned: bool
    ) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        thread_id = job["thread_id"]
        if pinned:
            pinned_at = self.pinned_threads.setdefault(thread_id, datetime.now(UTC))
        else:
            self.pinned_threads.pop(thread_id, None)
            pinned_at = None
        return {"thread_id": thread_id, "pinned_at": pinned_at}

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        job = self.jobs.get(job_id)
        if job is None or (user_id is not None and job["user_id"] != user_id):
            return None
        if job["state"] in ("queued", "running"):
            self._pause(job["thread_id"], "stopped")
        job["cancel_requested"] = True
        if job["state"] == "queued":
            job["state"] = "cancelled"
            job["terminal_resume_pending"] = True
        return job["state"]

    async def list_events_after(
        self, *, job_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        return [
            event for event in self.events if event["job_id"] == job_id and event["id"] > after_id
        ][:limit]

    async def thread_workspace_ready(self, *, thread_id: str) -> bool:
        return self.thread_workspace_readiness.get(thread_id, True)

    async def fence_workspace_start(self, *, thread_id: str) -> tuple[str, int | None] | None:
        self.workspace_start_fences.append(thread_id)
        for job in self.jobs.values():
            sandbox = job.get("thread_sandbox")
            if job.get("thread_id") == thread_id and isinstance(sandbox, dict):
                state = sandbox.get("state")
                if state is None:
                    return None
                if state == "host_revoked":
                    return state, None
                return state, int(sandbox.get("start_seq") or 0) + 1
        return None

    async def fence_terminal_workspace(self, *, attempt_id: int, lease_generation: int) -> bool:
        if not self._fenced(attempt_id, lease_generation):
            return False
        self.terminal_readiness[attempt_id] = False
        return True

    async def list_terminal_resumes_pending(self, *, limit: int = 100) -> list[str]:
        return [
            job["id"]
            for job in self.jobs.values()
            if job["terminal_resume_pending"]
            and job["state"] in {"succeeded", "failed", "cancelled"}
        ][:limit]

    async def mark_terminal_resume_complete(self, *, job_id: str) -> bool:
        job = self.jobs[job_id]
        if not job["terminal_resume_pending"]:
            return False
        job["terminal_resume_pending"] = False
        return True

    # -- worker surface (fenced) --
    def _fenced(self, attempt_id: int, lease_generation: int) -> bool:
        return self.live_fence == (attempt_id, lease_generation)

    async def active_runner_host(self) -> str | None:
        return self.active_host

    async def runner_pool(self) -> tuple[list[dict[str, Any]], str | None]:
        rows = sorted(
            (
                {**entry, "is_active": entry["host"] == self.active_host}
                for entry in self.runner_hosts.values()
            ),
            key=lambda e: e["last_seen_at"],
            reverse=True,
        )
        return rows, self.active_host

    async def set_active_runner_host(self, *, host: str | None) -> bool:
        if host is not None and host not in self.runner_hosts:
            return False
        self.active_host = host
        return True

    async def forget_runner_host(self, *, host: str) -> str:
        if host == self.active_host:
            return "active"
        return "deleted" if self.runner_hosts.pop(host, None) is not None else "unknown"

    # Every executable job belongs to one enrolled host, including this
    # in-memory route fixture.
    async def host_for_attempt(self, *, attempt_id: int) -> dict[str, Any] | None:
        return {"host": "runner-test", "sandbox_manager_url": "http://sandbox_manager.test:8092"}

    async def host_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        return {"host": "runner-test", "sandbox_manager_url": "http://sandbox_manager.test:8092"}

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
        if self.active_host is not None and host != self.active_host:
            return None
        queued = [
            job
            for job in self.jobs.values()
            if job["state"] == "queued"
            and not any(
                earlier["thread_id"] == job["thread_id"]
                and earlier["turn_no"] < job["turn_no"]
                and earlier["state"] not in {"succeeded", "failed", "cancelled"}
                for earlier in self.jobs.values()
            )
        ]
        if not queued:
            return None
        job = queued[0]
        # Only a work round spends the budget, and only the first attempt at
        # one: the store charges the round when it is claimed, not when it is
        # queued, so a round that was cancelled never spends anything.
        goal = self.thread_goals.get(str(job["thread_id"]))
        if (
            goal is not None
            and job.get("turn_source") == "goal"
            and job.get("goal_round_kind") == "work"
            and job.get("current_attempt_id") is None
        ):
            goal["rounds_used"] = goal.get("rounds_used", 0) + 1
        job["state"] = "running"
        job["current_attempt_id"] = 100
        self.live_fence = (100, 1)
        self.terminal_readiness[100] = False
        self.attempt_jobs[100] = job["id"]
        return {**job, "attempt_id": 100, "attempt_no": 1, "lease_generation": 1}

    async def heartbeat(
        self, *, attempt_id: int, lease_generation: int, lease_ttl_seconds: float
    ) -> dict[str, Any]:
        if not self._fenced(attempt_id, lease_generation):
            return {"ok": False}
        job = next(job for job in self.jobs.values() if job["current_attempt_id"] == attempt_id)
        return {
            "ok": True,
            "job_id": job["id"],
            "state": job["state"],
            "cancel_requested": job["cancel_requested"],
        }

    async def append_event(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
        event_seq: int | None = None,
    ) -> int | None:
        if not self._fenced(attempt_id, lease_generation):
            return None
        if event_seq is not None:
            # Mirror the store: a replayed number answers with the stored row.
            for stored in self.events:
                if stored["attempt_id"] == attempt_id and stored.get("client_seq") == event_seq:
                    return stored["id"]
        # By the attempt the claim recorded where there is a record of it. A
        # test that arranges an attempt by hand has none, and falls back to the
        # job naming it — which is what the store's own foreign key means.
        recorded = self.attempt_jobs.get(attempt_id)
        job = (
            self.jobs[recorded]
            if recorded
            else next(job for job in self.jobs.values() if job["current_attempt_id"] == attempt_id)
        )
        event = {
            "id": self._next_event_id,
            "job_id": job["id"],
            "attempt_id": attempt_id,
            "seq": len(self.events) + 1,
            "client_seq": event_seq,
            "event_type": event_type,
            "payload": payload,
            "created_at": None,
        }
        self._next_event_id += 1
        self.events.append(event)
        phase = (payload or {}).get("phase") if event_type == "lifecycle" else None
        if phase == "workspace_ready":
            self.terminal_readiness[attempt_id] = True
        elif phase in {"workspace_preparing", "workspace_finalizing"}:
            self.terminal_readiness[attempt_id] = False
        if event_type == "message" and isinstance((payload or {}).get("text"), str):
            self.messages.append(
                {
                    "id": len(self.messages) + 1,
                    "thread_id": job["thread_id"],
                    "job_id": job["id"],
                    "role": "assistant",
                    "content": payload["text"],
                    "created_at": None,
                }
            )
        return event["id"]

    async def append_events(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        events: list[tuple[str, dict[str, Any] | None, int | None]],
    ) -> list[int] | None:
        # Mirror the store: one fence for the batch, each event as a single append stores it.
        if not self._fenced(attempt_id, lease_generation):
            return None
        stored = []
        for event_type, payload, event_seq in events:
            event_id = await self.append_event(
                attempt_id=attempt_id,
                lease_generation=lease_generation,
                event_type=event_type,
                payload=payload,
                event_seq=event_seq,
            )
            assert event_id is not None
            stored.append(event_id)
        return stored

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
        goal_envelope: Any | None = None,
    ) -> bool:
        job = self.jobs.get(job_id)
        if job is None or not self._fenced(attempt_id, lease_generation):
            return False
        if job["state"] not in from_states:
            return False
        job["state"] = to_state
        # The store writes this in the same transaction as the outcome, so a
        # terminal turn always carries the answer it ended on. There is no tool
        # half any more: the report is the turn's own final message.
        if goal_envelope is not None:
            job["goal_report_parse"] = goal_envelope.parse_result
            job["goal_report_outcome"] = goal_envelope.outcome
            # The model's sentence and the parser's refusal reason are two
            # columns, the way ``transition`` writes them.
            job["goal_report_reason"] = goal_envelope.reason
            job["goal_report_detail"] = goal_envelope.detail
        if to_state in {"succeeded", "failed", "cancelled"}:
            job["terminal_resume_pending"] = False
        job["detail"] = detail
        # Mirrors the store's COALESCE: a worker may fill in a base the job
        # lacked, never overwrite one the owner pinned.
        if base_sha and not job.get("base_sha"):
            job["base_sha"] = base_sha
        return True

    async def release_claim(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int | None = None,
        reason: str = "credential_unavailable",
        retry_after_seconds: float = 0,
    ) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.get("current_attempt_id") != attempt_id:
            return False
        # Mirrors the store's own guard: a turn whose prompt reached the CLI is
        # not releasable, because replaying it runs the instruction twice.
        if job.get("admitted"):
            return False
        self.released.append((job_id, attempt_id, reason))
        job["claim_retry_delay"] = retry_after_seconds
        job["state"] = "queued"
        job["current_attempt_id"] = None
        self.live_fence = None
        return True


# ``FakeOwnerAuthStore`` — the operational-store slice the ported API-key cases
# installed — was deleted with the branch it fed on 2026-08-04. Nothing in this
# service resolves a key to a user, so there is no store shape left to fake.


@pytest.fixture(autouse=True)
def _api_key_secret(monkeypatch):
    """Provide the signing secret for worker tokens and an entitled repo.

    The repo allowlist is deliberately empty by default (a deployment that has
    not been configured must refuse every job), so these tests declare the one
    repository they use.

    The secret is this service's own now (``AGENT_CONTROL_TOKEN_SECRET``); the
    gateway's ``API_KEY_SECRET`` never appears here. There is no settings cache
    to clear either — ``entitlement`` reads the allowlist straight from the
    environment.
    """
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "api-test-secret")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "api-test-sandbox-manager-secret")
    use_session_secret(monkeypatch)
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name,o/n,private/repo")
    # The key stored MCP credentials are wrapped under. Without it a deployment
    # can hold nobody's credential, and the composer legitimately offers
    # nothing — which would make every MCP case here pass for the wrong reason.
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _MCP_ENCRYPTION_KEY)
    yield


@pytest.fixture()
def store() -> FakeStorage:
    """Provide a fresh in-memory store."""
    return FakeStorage()


def _attach_shared_workspace(store: FakeStorage, job_id: str) -> str:
    """Give a route test the retained group a real runner would reserve."""
    job = store.jobs[job_id]
    thread_id = str(job["thread_id"])
    job["thread_sandbox"] = {
        "thread_id": thread_id,
        "origin_thread_id": thread_id,
        "shared_live": True,
    }
    return thread_id


# Every model id the tests submit must resolve, or create fails fast by design.
_TEST_MODELS = ("glm-5.1", "qwen-next", "m")
#: One model that takes an effort and one that does not, so the intersection
#: is exercised in both directions. No ``medium``, matching the real registry's
#: glm-5.3 entry: a fixture that offered the intuitive three would pass against
#: an implementation that assumed them.
_TEST_MODEL_EFFORTS = {"glm-5.1": ["low", "high", "max"]}


class _FakeOwners:
    """The account directory, answered in-process: every owner holds ``agent.use``."""

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        return {"user_id": user_id, "allowed": True, "permissions": ["agent.use"]}

    async def user_email(self, user_id: str) -> str:
        return f"{user_id}@example.org"


def _build_app(store, *, role: str = "admin", claim_auth: bool = True):
    """Build an app with the router mounted and auth stubbed to one identity.

    ``claim_auth`` controls whether an enrolled-host identity is supplied to
    ``/worker/claim``, so a test can assert that an ordinary caller is refused.
    """
    from cloud_agent.api.deps import (
        get_agent_app_credentials,
        get_owner_directory,
    )

    app = FastAPI()
    app.include_router(agent_jobs_router.router)
    app.dependency_overrides[get_storage] = lambda: store
    # Every owner request and claim is admitted on the account's permissions.
    app.dependency_overrides[get_owner_directory] = _FakeOwners
    # No GitHub App by default — the configuration most deployments start in,
    # and the one where a public repository still clones fine.
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    identity = {"user_id": _OWNER, "role": role, "authenticated": True}
    # Override identity only; every request still exercises account admission.
    app.dependency_overrides[authenticate_agent_owner] = lambda: identity
    if claim_auth:
        app.dependency_overrides[worker.verify_claiming_host] = lambda: {
            "host": "runner-test",
            "credential_hash": "test-credential-hash",
        }
    return app


def _browser_session(store: FakeStorage) -> str:
    """A live account session for the owner, as sign-in issues one."""
    if not isinstance(getattr(store, "local_accounts", None), SessionAccounts):
        store.local_accounts = SessionAccounts()
    return store.local_accounts.sign_in(_OWNER)


@pytest_asyncio.fixture()
async def client(store: FakeStorage):
    """Mount only the agent-jobs router with auth stubbed to a fixed owner."""
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def _create_job_raw(client: AsyncClient, *, model: str = "glm-5.1"):
    """Post a job and hand back the raw response, refusals included."""
    return await client.post(
        "/v1/agent/jobs",
        json=on_connection({"repo": "owner/name", "task_prompt": "fix it", "model": model}),
    )


async def _create_job(client: AsyncClient, *, model: str = "glm-5.1") -> str:
    """Create a job through the API and return its id."""
    response = await _create_job_raw(client, model=model)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _thread_of(store: FakeStorage, job_id: str) -> str:
    """The conversation a job belongs to; messages are addressed to it."""
    return str(store.jobs[job_id]["thread_id"])


async def _send(
    client: AsyncClient,
    store: FakeStorage,
    job_id: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    """Send a message to the conversation containing ``job_id``."""
    return await client.post(
        f"/v1/agent/threads/{_thread_of(store, job_id)}/messages", json=body, headers=headers
    )


async def _next_turn(
    client: AsyncClient,
    store: FakeStorage,
    after: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """End ``after``, then send a message, which is the next turn at once.

    A message sent while a turn is live waits in the queue rather than becoming
    a turn. Cases that need the second turn itself — to read its fields, pin it,
    archive it — end the turn in front first, which is what the owner's page
    waits for too.
    """
    store.jobs[after]["state"] = "succeeded"
    response = await _send(client, store, after, body, headers=headers)
    assert response.status_code == 201, response.text
    job = response.json()["job"]
    assert job is not None, response.text
    return job


class _WorkspaceContentsApp:
    """GitHub Contents stand-in that records the pinned reads the API makes."""

    def __init__(self, contents: dict[str, Any]) -> None:
        self.contents = contents
        self.calls: list[tuple[str, str, str]] = []

    async def repository_contents(self, repo: str, *, path: str, ref: str) -> Any:
        from cloud_agent.github_app import GitHubAppError

        self.calls.append((repo, path, ref))
        if path not in self.contents:
            raise GitHubAppError("not found", status=404)
        return self.contents[path]


async def test_create_get_list_round_trip(client: AsyncClient):
    """A created job is retrievable and listed for its owner."""
    job_id = await _create_job(client)

    got = await client.get(f"/v1/agent/jobs/{job_id}")
    assert got.status_code == 200
    assert got.json()["state"] == "queued"
    assert got.json()["runtime"] == "claude-code"

    listed = await client.get("/v1/agent/jobs")
    assert [job["id"] for job in listed.json()["jobs"]] == [job_id]


async def test_execution_profile_is_server_owned_and_reserved_metadata_is_dropped(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """The deployment chooses the boundary; a request cannot forge or lower it."""
    monkeypatch.setenv("AGENT_DEFAULT_EXECUTION_PROFILE", "untrusted-kata-v1")
    created = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {
                "repo": "owner/name",
                "task_prompt": "fix it",
                "model": "glm-5.1",
                "metadata": {
                    "origin": "api-test",
                    "execution_profile": "trusted-v1",
                    "sandbox_runtime": "runc",
                    "guest_kernel": "forged",
                },
            }
        ),
    )

    assert created.status_code == 201
    assert created.json()["execution_profile"] == "untrusted-kata-v1"
    assert created.json()["metadata"] == {"origin": "api-test"}
    assert store.jobs[created.json()["id"]]["execution_profile"] == "untrusted-kata-v1"

    rejected = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {
                "repo": "owner/name",
                "task_prompt": "try to downgrade",
                "model": "glm-5.1",
                "execution_profile": "trusted-v1",
            }
        ),
    )
    assert rejected.status_code == 422
    assert "server-owned" in rejected.text


async def test_follow_up_inherits_profile_instead_of_rereading_policy(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    monkeypatch.setenv("AGENT_DEFAULT_EXECUTION_PROFILE", "untrusted-kata-v1")
    source_id = await _create_job(client)
    store.jobs[source_id]["state"] = "succeeded"

    monkeypatch.setenv("AGENT_DEFAULT_EXECUTION_PROFILE", "trusted-v1")
    child = await _send(client, store, source_id, {"prompt": "continue"})

    assert child.status_code == 201
    assert child.json()["job"]["execution_profile"] == "untrusted-kata-v1"


async def test_jobs_page_by_repo_and_projects_summarize_the_task_tree(client: AsyncClient):
    """The sidebar groups by project, so it pages one repo and lists them all."""
    first = await _create_job(client)
    second = await client.post(
        "/v1/agent/jobs",
        json=on_connection({"repo": "o/n", "task_prompt": "a second project", "model": "glm-5.1"}),
    )
    assert second.status_code == 201

    one_repo = await client.get("/v1/agent/jobs?repo=owner/name")
    assert [job["id"] for job in one_repo.json()["jobs"]] == [first]

    projects = (await client.get("/v1/agent/projects")).json()["projects"]
    by_repo = {project["repo"]: project for project in projects}
    assert set(by_repo) == {"owner/name", "o/n"}
    assert by_repo["o/n"]["task_count"] == 1
    assert by_repo["o/n"]["active_count"] == 1


async def test_repo_filter_rejects_a_value_git_would_read_as_an_option(client: AsyncClient):
    """The filter is shape-checked like the create field it mirrors."""
    rejected = await client.get("/v1/agent/jobs?repo=--upload-pack=sh")
    assert rejected.status_code == 422


async def test_conversation_search_forwards_owner_filters_and_shapes_latest_job(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """Search returns the canonical title separately from the latest prompt."""
    job_id = await _create_job(client)
    job = store.jobs[job_id]
    job["updated_at"] = datetime(2026, 9, 10, tzinfo=UTC)
    search = AsyncMock(
        return_value={
            "conversations": [
                {
                    "job": job,
                    "title": "Original conversation title",
                    "archived": True,
                    "last_activity_at": job["updated_at"],
                }
            ],
            "next_cursor": "opaque-page-boundary",
        }
    )
    monkeypatch.setattr(store, "list_conversations", search, raising=False)
    response = await client.get(
        "/v1/agent/conversations",
        params={
            "q": "  Original  ",
            "repo": "owner/name",
            "include_archived": "true",
            "pinned": "true",
            "limit": 17,
            "cursor": "previous-page",
        },
    )
    assert response.status_code == 200
    search.assert_awaited_once_with(
        user_id=_OWNER,
        q="Original",
        repo="owner/name",
        include_archived=True,
        pinned=True,
        limit=17,
        cursor="previous-page",
        temporary=False,
    )
    item = response.json()["conversations"][0]
    assert item["job"]["id"] == job_id
    assert "user_id" not in item["job"]
    assert item["title"] == "Original conversation title"
    assert item["archived"] is True
    assert item["last_activity_at"] == job["updated_at"].isoformat()
    assert response.json()["next_cursor"] == "opaque-page-boundary"


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"repo": "--upload-pack=sh"},
        {"q": "x" * 201},
        {"include_archived": "invalid"},
        {"pinned": "invalid"},
        {"cursor": ""},
        {"cursor": "x" * 2049},
    ],
)
async def test_conversation_search_rejects_invalid_parameters(client: AsyncClient, params):
    """Malformed searches never reach persistence."""
    response = await client.get("/v1/agent/conversations", params=params)
    assert response.status_code == 422


async def test_conversation_search_rejects_invalid_cursor(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    search = AsyncMock(side_effect=ValueError("Invalid conversation cursor for this search"))
    monkeypatch.setattr(store, "list_conversations", search, raising=False)
    response = await client.get("/v1/agent/conversations", params={"cursor": "invalid"})
    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid conversation cursor for this search"


async def test_conversation_search_requires_owner_and_accepts_browser_session(
    store: FakeStorage, monkeypatch
):
    """Anonymous searches are refused; signed-in searches use the JWT owner."""
    search = AsyncMock(return_value={"conversations": [], "next_cursor": None})
    monkeypatch.setattr(store, "list_conversations", search, raising=False)
    app = _build_app(store)
    app.dependency_overrides.pop(authenticate_agent_owner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.get("/v1/agent/conversations")
        assert anonymous.status_code == 401
        search.assert_not_awaited()
        response = await client.get(
            "/v1/agent/conversations",
            headers={"Authorization": f"Bearer {_browser_session(store)}"},
        )
    assert response.status_code == 200
    assert response.json() == {"conversations": [], "next_cursor": None}
    search.assert_awaited_once_with(
        user_id=_OWNER,
        q="",
        repo=None,
        include_archived=False,
        pinned=None,
        limit=50,
        cursor=None,
        temporary=False,
    )


async def test_conversation_search_forwards_unpinned_filter(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """False selects the ordinary list rather than omitting the filter."""
    search = AsyncMock(return_value={"conversations": [], "next_cursor": None})
    monkeypatch.setattr(store, "list_conversations", search, raising=False)
    response = await client.get("/v1/agent/conversations", params={"pinned": "false"})
    assert response.status_code == 200
    assert search.await_args.kwargs["pinned"] is False


def _seed_event(
    store: FakeStorage,
    *,
    job_id: str,
    seq: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Put one event straight into the stand-in, bypassing the fenced write."""
    store.events.append(
        {
            "id": len(store.events) + 1,
            "job_id": job_id,
            "attempt_id": 1,
            "seq": seq,
            "event_type": event_type,
            "payload": payload or {},
            "created_at": None,
        }
    )


async def test_thread_stats_summarize_every_turn_in_the_thread(
    client: AsyncClient, store: FakeStorage
):
    """The bar counts the whole conversation, not just the job asked about."""
    parent_id = await _create_job(client)
    follow_up = await _next_turn(client, store, parent_id, {"prompt": "and the docs too"})
    child_id = follow_up["id"]
    _seed_event(
        store,
        job_id=parent_id,
        seq=1,
        event_type="tool_use",
        payload={"id": "a", "name": "bash", "usage": {"input_tokens": 100, "output_tokens": 10}},
    )
    _seed_event(
        store,
        job_id=parent_id,
        seq=2,
        event_type="tool_result",
        payload={"tool_use_id": "a", "is_error": True},
    )
    _seed_event(
        store,
        job_id=child_id,
        seq=3,
        event_type="message",
        payload={"text": "done", "usage": {"input_tokens": 40, "output_tokens": 5}},
    )

    # Either end of the thread answers with the same thread-wide summary.
    for job_id in (parent_id, child_id):
        response = await client.get(f"/v1/agent/jobs/{job_id}/thread/stats")

        assert response.status_code == 200
        body = response.json()
        assert body["thread_id"] == follow_up["thread_id"]
        assert body["turns"] == 2
        assert body["tool_calls"] == 1
        assert body["failed_tool_calls"] == 1
        assert body["input_tokens"] == 140
        assert body["output_tokens"] == 15
        assert body["tokens_source"] == "runtime"
        assert body["timing_source"] == "message_events"


async def test_thread_stats_report_absent_tokens_as_null(client: AsyncClient, store: FakeStorage):
    """A runtime that reports no usage must not look like one that used none."""
    job_id = await _create_job(client)
    _seed_event(store, job_id=job_id, seq=1, event_type="raw", payload={"text": "tier-2 tail"})

    body = (await client.get(f"/v1/agent/jobs/{job_id}/thread/stats")).json()

    assert body["input_tokens"] is None
    assert body["output_tokens"] is None
    assert body["cache_hit_ratio"] is None
    assert body["output_tokens_per_second"] is None
    assert body["tokens_source"] == "unavailable"


async def test_thread_stats_are_owner_scoped(client: AsyncClient, store: FakeStorage):
    """Someone else's thread is not summarizable, same as it is not readable."""
    job_id = await _create_job(client)
    store.jobs[job_id]["user_id"] = _OTHER

    response = await client.get(f"/v1/agent/jobs/{job_id}/thread/stats")

    assert response.status_code == 404


async def test_thread_reads_answer_from_the_owner_scoped_query_alone(
    client: AsyncClient, store: FakeStorage
):
    """Thread, stats, and event pages fence and serve in one owner-scoped read.

    The query that serves each read is already scoped to the caller's own,
    undeleted job, so a whole-job fetch before it — sandbox subqueries,
    skill summaries, conversation state — buys the handler nothing: it
    discards the row. Someone else's job and a missing one must stay one
    indistinguishable 404.
    """
    job_id = await _create_job(client)
    foreign_id = await _create_job(client)
    store.jobs[foreign_id]["user_id"] = _OTHER
    whole_job = store.conversation_queries.get_job
    calls = {"whole_job": 0}

    async def counting_get_job(job_id: str):
        calls["whole_job"] += 1
        return await whole_job(job_id)

    store.conversation_queries.get_job = counting_get_job

    for suffix in ("/thread", "/thread/stats", "/thread/events"):
        owned = await client.get(f"/v1/agent/jobs/{job_id}{suffix}")
        assert owned.status_code == 200, (suffix, owned.text)
        foreign = await client.get(f"/v1/agent/jobs/{foreign_id}{suffix}")
        assert foreign.status_code == 404, suffix
        missing = await client.get(f"/v1/agent/jobs/ajob_missing{suffix}")
        assert missing.status_code == 404, suffix
    assert calls["whole_job"] == 0


async def test_thread_stats_answer_a_thread_that_never_ran(client: AsyncClient):
    """A queued thread has no events yet and still renders a bar."""
    job_id = await _create_job(client)

    body = (await client.get(f"/v1/agent/jobs/{job_id}/thread/stats")).json()

    assert body["turns"] == 1
    assert body["tool_calls"] == 0
    assert body["wall_seconds"] == 0.0
    assert body["first_output_seconds"] is None


async def test_archived_threads_leave_the_active_project_tree(client: AsyncClient):
    """A project with nothing but archived work is not an active folder."""
    job_id = await _create_job(client)
    assert (await client.post(f"/v1/agent/jobs/{job_id}/archive")).status_code == 200

    assert (await client.get("/v1/agent/projects")).json() == {"projects": []}
    archived = (await client.get("/v1/agent/projects?archived=true")).json()["projects"]
    assert [project["repo"] for project in archived] == ["owner/name"]


async def test_archive_and_restore_filter_the_entire_thread_without_cancelling(
    client: AsyncClient, store: FakeStorage
):
    """Archive hides every turn while leaving a live run untouched."""
    parent_id = await _create_job(client)
    follow_up = await _next_turn(client, store, parent_id, {"prompt": "also update the docs"})
    child_id = follow_up["id"]
    thread_id = follow_up["thread_id"]
    store.jobs[parent_id]["state"] = "running"

    archived = await client.post(f"/v1/agent/jobs/{child_id}/archive")

    assert archived.status_code == 200
    assert archived.json()["thread_id"] == thread_id
    assert archived.json()["archived"] is True
    assert archived.json()["archived_at"] is not None
    thread = (await client.get(f"/v1/agent/jobs/{child_id}/thread")).json()
    assert thread["archived_at"] == archived.json()["archived_at"]
    assert store.jobs[parent_id]["state"] == "running"
    assert store.jobs[parent_id]["cancel_requested"] is False
    assert (await client.get("/v1/agent/jobs")).json() == {"jobs": []}
    archived_jobs = (await client.get("/v1/agent/jobs?archived=true")).json()["jobs"]
    assert {job["id"] for job in archived_jobs} == {parent_id, child_id}

    restored = await client.delete(f"/v1/agent/jobs/{parent_id}/archive")

    assert restored.status_code == 200
    assert restored.json() == {
        "thread_id": thread_id,
        "archived": False,
        "archived_at": None,
    }
    assert (await client.get("/v1/agent/jobs?archived=true")).json() == {"jobs": []}
    thread = (await client.get(f"/v1/agent/jobs/{parent_id}/thread")).json()
    assert thread["archived_at"] is None
    active_jobs = (await client.get("/v1/agent/jobs")).json()["jobs"]
    assert {job["id"] for job in active_jobs} == {parent_id, child_id}


async def test_rename_from_a_follow_up_updates_the_conversation_and_preserves_prompts(
    client: AsyncClient, store: FakeStorage
):
    """Any turn renames all owner-facing views while keeping execution inputs intact."""
    parent_id = await _create_job(client)
    follow_up = await _next_turn(client, store, parent_id, {"prompt": "also update the docs"})
    child_id = follow_up["id"]
    prompts = {job_id: store.jobs[job_id]["task_prompt"] for job_id in (parent_id, child_id)}
    response = await client.patch(
        f"/v1/agent/jobs/{child_id}/title", json={"title": "  Export CSV 导出  "}
    )
    assert response.status_code == 200
    assert response.json() == {
        "thread_id": follow_up["thread_id"],
        "title": "Export CSV 导出",
    }
    for job_id in (parent_id, child_id):
        thread = (await client.get(f"/v1/agent/jobs/{job_id}/thread")).json()
        assert thread["title"] == "Export CSV 导出"
        assert all(job["title"] == "Export CSV 导出" for job in thread["jobs"])
        assert (await client.get(f"/v1/agent/jobs/{job_id}")).json()["title"] == "Export CSV 导出"
        assert store.jobs[job_id]["task_prompt"] == prompts[job_id]
    assert all(
        job["title"] == "Export CSV 导出"
        for job in (await client.get("/v1/agent/jobs")).json()["jobs"]
    )
    next_turn = await _next_turn(client, store, child_id, {"prompt": "one more change"})
    assert next_turn["title"] == "Export CSV 导出"


@pytest.mark.parametrize("title", ["", " \n\t ", "x" * 161, None, 123])
async def test_rename_rejects_invalid_titles(client: AsyncClient, title):
    """Blank and oversized titles cannot overwrite a conversation's name."""
    job_id = await _create_job(client)
    response = await client.patch(f"/v1/agent/jobs/{job_id}/title", json={"title": title})
    assert response.status_code == 422


async def test_rename_accepts_trimmed_title_limit(client: AsyncClient):
    """Surrounding whitespace does not consume the canonical title limit."""
    job_id = await _create_job(client)
    response = await client.patch(
        f"/v1/agent/jobs/{job_id}/title", json={"title": " " + "名" * 160 + " "}
    )
    assert response.status_code == 200
    assert response.json()["title"] == "名" * 160


async def test_rename_hides_foreign_missing_and_deleted_jobs(
    client: AsyncClient, store: FakeStorage
):
    """Renaming exposes no other owner's jobs and cannot restore deleted history."""
    foreign_id = await _create_job(client)
    store.jobs[foreign_id]["user_id"] = _OTHER
    deleted_id = await _create_job(client)
    store.jobs[deleted_id]["deleted_at"] = datetime.now(UTC)
    for job_id in (foreign_id, deleted_id, "ajob_missing"):
        response = await client.patch(f"/v1/agent/jobs/{job_id}/title", json={"title": "New title"})
        assert response.status_code == 404
        assert response.json()["detail"]["error"]["type"] == "not_found"


async def test_pin_and_unpin_are_thread_scoped_idempotent_and_reorder_lists(
    client: AsyncClient, store: FakeStorage
):
    """Any turn pins the conversation and its project without touching run state."""
    parent_id = await _create_job(client)
    follow_up = await _next_turn(client, store, parent_id, {"prompt": "also update the docs"})
    child_id = follow_up["id"]
    thread_id = follow_up["thread_id"]
    newer = await store.conversation_commands.create_job(
        user_id=_OWNER,
        repo="owner/newer",
        task_prompt="newer task",
        runtime="claude-code",
        model="glm-5.1",
    )
    newer_id = newer["id"]
    store.jobs[parent_id]["created_at"] = datetime(2026, 7, 1, tzinfo=UTC)
    store.jobs[child_id]["created_at"] = datetime(2026, 7, 2, tzinfo=UTC)
    store.jobs[newer_id]["created_at"] = datetime(2026, 7, 3, tzinfo=UTC)
    store.jobs[parent_id]["state"] = "running"

    pinned = await client.post(f"/v1/agent/jobs/{child_id}/pin")

    assert pinned.status_code == 200
    assert pinned.json()["thread_id"] == thread_id
    assert pinned.json()["pinned"] is True
    assert pinned.json()["pinned_at"] is not None
    pinned_again = await client.post(f"/v1/agent/jobs/{parent_id}/pin")
    assert pinned_again.json() == pinned.json()
    assert store.jobs[parent_id]["state"] == "running"
    assert store.jobs[parent_id]["cancel_requested"] is False

    jobs = (await client.get("/v1/agent/jobs")).json()["jobs"]
    assert [job["id"] for job in jobs[:2]] == [child_id, parent_id]
    assert jobs[0]["pinned_at"] == jobs[1]["pinned_at"] == pinned.json()["pinned_at"]
    assert jobs[2]["id"] == newer_id
    projects = (await client.get("/v1/agent/projects")).json()["projects"]
    assert [project["repo"] for project in projects] == ["owner/name", "owner/newer"]
    assert projects[0]["pinned_count"] == 1
    assert projects[0]["pinned_at"] == pinned.json()["pinned_at"]

    unpinned = await client.delete(f"/v1/agent/jobs/{parent_id}/pin")

    assert unpinned.json() == {
        "thread_id": thread_id,
        "pinned": False,
        "pinned_at": None,
    }
    jobs = (await client.get("/v1/agent/jobs")).json()["jobs"]
    assert jobs[0]["id"] == newer_id
    assert all(job["pinned_at"] is None for job in jobs)
    projects = (await client.get("/v1/agent/projects")).json()["projects"]
    assert [project["repo"] for project in projects] == ["owner/newer", "owner/name"]


async def test_follow_up_response_inherits_the_thread_pin(client: AsyncClient, store: FakeStorage):
    """A new turn reports the conversation state without waiting for a list refresh."""
    parent_id = await _create_job(client)
    pinned = await client.post(f"/v1/agent/jobs/{parent_id}/pin")

    follow_up = await _next_turn(client, store, parent_id, {"prompt": "also update the docs"})

    assert follow_up["pinned_at"] == pinned.json()["pinned_at"]


async def test_a_message_sent_during_a_turn_waits_then_follows_it_in_native_context(
    client: AsyncClient, store: FakeStorage
):
    """A user can send the next message while the current turn is active.

    It waits in the queue — editable, not yet in the transcript — and becomes
    the next turn when the current one finishes, continuing the same native
    session rather than a replayed one.
    """
    parent_id = await _create_job(client)
    sent = await _send(
        client,
        store,
        parent_id,
        {
            "prompt": "now add a regression test",
            "model": "qwen-next",
            "model_connection_id": CONNECTION_ID,
        },
    )
    assert sent.status_code == 201
    assert sent.json()["job"] is None
    waiting = sent.json()["pending"]
    assert waiting["state"] == "queued"
    assert waiting["prompt"] == "now add a regression test"
    assert waiting["model"] == "qwen-next"

    thread = await client.get(f"/v1/agent/jobs/{parent_id}/thread")
    assert thread.status_code == 200
    assert [message["content"] for message in thread.json()["messages"]] == ["fix it"]
    assert [row["id"] for row in thread.json()["pending_messages"]] == [waiting["id"]]

    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    assert claim["job_id"] == parent_id
    blocked = await client.post(
        "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w2"})
    )
    assert blocked.status_code == 200
    assert blocked.json() is None
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{parent_id}/events",
        json={"event_type": "message", "payload": {"text": "implemented the fix"}},
        headers=auth,
    )
    finished = await client.post(
        f"/v1/agent/worker/jobs/{parent_id}/finish",
        json={"state": "succeeded"},
        headers=auth,
    )
    assert finished.status_code == 200

    thread = (await client.get(f"/v1/agent/jobs/{parent_id}/thread")).json()
    assert thread["pending_messages"] == []
    child = next(job for job in thread["jobs"] if job["id"] != parent_id)
    assert child["state"] == "queued"
    assert child["parent_job_id"] == parent_id
    assert child["turn_no"] == 2
    assert child["model"] == "qwen-next"
    # The message is in the transcript now, after the turn it waited for.
    assert [message["content"] for message in thread["messages"]] == [
        "fix it",
        "implemented the fix",
        "now add a regression test",
    ]

    child_claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w2"}))
    ).json()
    assert child_claim["job_id"] == child["id"]
    assert "context_patch" not in child_claim
    assert "context_messages" not in child_claim
    assert "context_checkpoint" not in child_claim


async def test_a_waiting_message_is_held_when_its_owners_agent_access_was_withdrawn(
    client: AsyncClient, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
):
    """The turn in front ending is not the owner asking again.

    A waiting message is sent by the settlement, with no request of its
    owner's behind it, so only the settlement can notice that the Gateway no
    longer admits them. It holds the message with the reason instead of
    turning it into a turn that would run anyway.
    """
    parent_id = await _create_job(client)
    sent = await _send(client, store, parent_id, {"prompt": "now add a regression test"})
    assert sent.status_code == 201
    waiting = sent.json()["pending"]
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    assert claim["job_id"] == parent_id

    async def withdrawn(self: Any, user_id: str) -> dict[str, Any]:
        return {"user_id": user_id, "allowed": False, "permissions": []}

    monkeypatch.setattr(_FakeOwners, "agent_access", withdrawn)
    finished = await client.post(
        f"/v1/agent/worker/jobs/{parent_id}/finish",
        json={"state": "succeeded"},
        headers={"Authorization": f"Bearer {claim['worker_token']}"},
    )
    assert finished.status_code == 200

    held = store.pending[waiting["id"]]
    assert held["state"] == "held"
    assert held["last_error"] == "Agent access has not been granted for this account."
    assert [job["id"] for job in store.jobs.values()] == [parent_id]


async def test_cancelled_middle_turn_does_not_release_a_later_queued_turn(
    client: AsyncClient, store: FakeStorage
):
    """Every unfinished earlier turn blocks a claim, even across a cancellation.

    Two turns queued behind a live one cannot come from the message route any
    more — it holds later messages in the queue — so they are appended directly,
    the way ``insert_follow_up`` appends when it is not asked to yield.
    """
    parent_id = await _create_job(client)
    middle = await store.insert_follow_up(
        parent_job_id=parent_id, user_id=_OWNER, prompt="skip this follow-up"
    )
    assert middle is not None
    middle_id = middle["id"]
    latest = await store.insert_follow_up(
        parent_job_id=parent_id, user_id=_OWNER, prompt="run this after the first turn"
    )
    assert latest is not None
    latest_id = latest["id"]
    assert latest["parent_job_id"] == middle_id
    assert latest["state"] == "queued"

    cancelled = await client.post(f"/v1/agent/jobs/{middle_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    assert claim["job_id"] == parent_id
    blocked = await client.post(
        "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w2"})
    )
    assert blocked.status_code == 200
    assert blocked.json() is None
    assert store.jobs[latest_id]["state"] == "queued"

    finished = await client.post(
        f"/v1/agent/worker/jobs/{parent_id}/finish",
        json={"state": "succeeded"},
        headers={"Authorization": f"Bearer {claim['worker_token']}"},
    )
    assert finished.status_code == 200
    next_claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w2"}))
    ).json()
    assert next_claim["job_id"] == latest_id


async def test_create_fails_fast_on_a_model_the_connection_does_not_list(client: AsyncClient):
    """An unlisted model is a 400 at create, not a dead job after claim+clone."""
    response = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "gpt-nonexistent",
            "model_connection_id": CONNECTION_ID,
        },
    )
    assert response.status_code == 400
    detail = response.json()["detail"]["error"]
    assert (detail["type"], detail["reason"]) == ("model_connection_unusable", "model_not_listed")


async def test_create_without_a_connection_is_a_verdict_not_an_outage(client: AsyncClient):
    """There is no other source: the owner is asked to choose a connection's model."""
    response = await client.post(
        "/v1/agent/jobs", json={"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["type"] == "model_connection_required"


async def test_create_refuses_an_effort_on_a_connection(client: AsyncClient):
    response = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {"repo": "owner/name", "task_prompt": "fix it", "reasoning_effort": "high"}
        ),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["reason"] == "effort_unsupported"


async def test_follow_up_model_switch_gets_the_same_fail_fast(
    client: AsyncClient, store: FakeStorage
):
    """Switching models in a message is validated like a fresh job, before it waits."""
    parent_id = await _create_job(client)
    response = await _send(
        client,
        store,
        parent_id,
        {
            "prompt": "try another model",
            "model": "gpt-nonexistent",
            "model_connection_id": CONNECTION_ID,
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["reason"] == "model_not_listed"
    assert store.pending == {}


async def test_follow_up_model_switch_without_its_connection_is_refused(
    client: AsyncClient, store: FakeStorage
):
    parent_id = await _create_job(client)
    response = await _send(client, store, parent_id, {"prompt": "again", "model": "glm-5.1"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "model_connection_required"
    assert store.pending == {}


async def test_an_unregistered_runtime_is_refused_at_create(client: AsyncClient):
    """An arbitrary runtime string dies here, not as a claimed-then-dead job."""
    response = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {
                "repo": "owner/name",
                "task_prompt": "fix it",
                "model": "glm-5.1",
                "runtime": "no-such-harness",
            }
        ),
    )
    assert response.status_code == 400
    assert "no-such-harness" in response.json()["detail"]["error"]["message"]


@pytest.mark.parametrize(
    "change",
    [
        {"task_prompt": "   \n  "},
        {"task_prompt": "x" * 100_001},
        {"model": "m" * 256},
        {"runtime": "r" * 65},
        {"metadata": {f"key-{index}": index for index in range(65)}},
        {"metadata": {"blob": "x" * (16 * 1024)}},
    ],
)
async def test_a_first_turn_is_bounded_like_every_later_message(
    client: AsyncClient, store: FakeStorage, change: dict[str, Any]
):
    """Whitespace is not a task, and nothing a turn stores is unbounded.

    The first turn's prompt is stored twice and its metadata is copied onto
    every later turn and returned with every job read, so neither may be
    larger than a message to the same conversation could be.
    """
    response = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1", **change}
        ),
    )
    assert response.status_code == 422, response.text
    assert store.jobs == {}


async def test_an_upload_lost_between_resolving_and_claiming_is_a_400(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """The claim inside the create's transaction is the boundary, and says why.

    Another submission, the stale-upload sweep or a waiting message can take a
    file after the selection resolved. The store then refuses the claim and
    writes nothing; the owner gets the same refusal a vanished upload always
    gets, not a bare ``500 Internal Server Error``.
    """
    upload = {"id": 7, "path": "notes.md", "size_bytes": 5, "content_type": "text/plain"}
    monkeypatch.setattr(
        store, "select_attachments", AsyncMock(return_value=[upload]), raising=False
    )
    lost = AttachmentError("One or more uploads are no longer available. Attach them again.")
    monkeypatch.setattr(store, "create_job", AsyncMock(side_effect=lost))

    response = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {
                "repo": "owner/name",
                "task_prompt": "read the notes",
                "model": "glm-5.1",
                "attachment_ids": [7],
            }
        ),
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == {
        "type": "invalid_attachment_selection",
        "message": "One or more uploads are no longer available. Attach them again.",
    }


async def test_a_first_turn_keeps_the_trimmed_task(client: AsyncClient, store: FakeStorage):
    """The same trimming a message gets, so a first turn reads like the rest."""
    response = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {"repo": "owner/name", "task_prompt": "  fix it \n", "model": "glm-5.1"}
        ),
    )
    assert response.status_code == 201, response.text
    assert store.jobs[response.json()["id"]]["task_prompt"] == "fix it"


async def test_a_message_cannot_name_a_harness(client: AsyncClient, store: FakeStorage):
    """The harness is fixed per thread; its session is the memory.

    A message has no ``runtime`` field at all — not even one echoing the
    thread's own, which the removed follow-up route tolerated. A request that
    names one is refused outright rather than having the field dropped.
    """
    parent_id = await _create_job(client)
    for runtime in ("codex", "claude-code", "no-such-harness"):
        response = await _send(client, store, parent_id, {"prompt": "continue", "runtime": runtime})
        assert response.status_code == 422, (runtime, response.text)
    assert store.pending == {}
    assert len(store.jobs) == 1


async def test_config_offers_the_connections_the_create_endpoint_accepts(client: AsyncClient):
    """The picker and the validator must be the same list, or one is lying."""
    config = await client.get("/v1/agent/config")
    assert config.status_code == 200
    body = config.json()
    assert "models" not in body
    assert "gateway_inference" not in body
    (option,) = body["model_connections"]
    assert option["id"] == CONNECTION_ID
    assert set(option["models"]) == set(CONNECTION_MODELS)
    assert body["model_connection_required"] is False
    # This fixture has no GitHub App, but its operator allowlists public repos.
    # The API accepts those jobs and anonymous clone works, so the composer
    # must not disable itself behind a narrower historical field name.
    assert "owner/name" in body["repos"]
    assert body["github_connected"] is True


async def test_config_without_a_usable_connection_asks_for_one(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """An owner whose connections serve no Agent is told to set one up."""

    async def none(*, user_id):
        return []

    monkeypatch.setattr(store, "list_model_connections", none)
    body = (await client.get("/v1/agent/config")).json()
    assert body["model_connections"] == []
    assert body["model_connection_required"] is True
    assert body["default_model"] is None


async def test_config_reports_where_a_new_task_should_start(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """A new task starts where the newest one ran, while that connection still offers it."""
    first = (await client.get("/v1/agent/config")).json()
    assert (first["default_model"], first["default_model_connection_id"]) == (None, None)

    await _create_job(client, model="qwen-next")
    remembered = (await client.get("/v1/agent/config")).json()
    assert (remembered["default_model"], remembered["default_model_connection_id"]) == (
        "qwen-next",
        CONNECTION_ID,
    )

    # The connection no longer lists the model: nothing is pre-selected.
    async def narrowed(*, user_id):
        return [connection_row(user_id, models=("glm-5.1",))]

    monkeypatch.setattr(store, "list_model_connections", narrowed)
    gone = (await client.get("/v1/agent/config")).json()
    assert (gone["default_model"], gone["default_model_connection_id"]) == (None, None)


async def test_config_reads_independent_sources_concurrently(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """Each source starts before the slow repository read releases its result."""
    from cloud_agent.api import config as config_api

    started: set[str] = set()
    all_started = asyncio.Event()
    release = asyncio.Event()
    repositories_read = False
    account_reads = 0

    async def wait_for_sources(name):
        started.add(name)
        # Repository entitlement, model connections, MCP connections, and the
        # owner's model preference: every independent source this read fans out.
        if len(started) == 4:
            all_started.set()
        await release.wait()

    async def repositories(user_id, **kwargs):
        nonlocal repositories_read
        assert user_id == _OWNER
        await wait_for_sources("repositories")
        repositories_read = True
        return ["owner/name"]

    async def model_connections(*, user_id):
        assert user_id == _OWNER
        await wait_for_sources("models")
        return [connection_row(user_id)]

    async def connections(*, user_id):
        assert user_id == _OWNER
        await wait_for_sources("mcp")
        return []

    async def account(*, user_id):
        nonlocal account_reads
        assert user_id == _OWNER
        assert repositories_read, "entitlement must finish before its account is read"
        account_reads += 1
        return None

    async def preference(user_id):
        assert user_id == _OWNER
        await wait_for_sources("preference")
        return ModelPreferenceView(last_model="glm-5.1", last_model_connection_id=CONNECTION_ID)

    monkeypatch.setattr(config_api, "repos_for_user", repositories)
    monkeypatch.setattr(store, "list_model_connections", model_connections)
    monkeypatch.setattr(store, "list_mcp_connections", connections)
    monkeypatch.setattr(store, "get_model_preference", preference)
    monkeypatch.setattr(store, "get_github_account", account)
    request = asyncio.create_task(client.get("/v1/agent/config"))
    try:
        # This is a deadlock limit, not a speed assertion: each source waits
        # for release, so a serial implementation cannot reach all_started.
        await asyncio.wait_for(all_started.wait(), timeout=2)
        assert not request.done()
        assert account_reads == 0
    finally:
        release.set()
        response = await request
    assert response.status_code == 200, response.text
    assert account_reads == 1
    assert response.json()["repos"] == ["owner/name"]
    assert [option["id"] for option in response.json()["model_connections"]] == [CONNECTION_ID]
    assert response.json()["mcp_connections"] == []


async def test_messages_append_to_latest_turn_and_reject_blank_prompts(
    client: AsyncClient, store: FakeStorage
):
    """Messages continue the conversation linearly, and whitespace is not a turn."""
    parent_id = await _create_job(client)
    first = await _next_turn(client, store, parent_id, {"prompt": "first follow-up"})
    assert first["parent_job_id"] == parent_id

    # Sent while that turn is live: it waits, and becomes the turn after it —
    # never a sibling that could run against the same branch concurrently.
    second = await _send(client, store, parent_id, {"prompt": "second follow-up"})
    assert second.status_code == 201
    assert second.json()["job"] is None
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    assert claim["job_id"] == first["id"]
    finished = await client.post(
        f"/v1/agent/worker/jobs/{first['id']}/finish",
        json={"state": "succeeded"},
        headers={"Authorization": f"Bearer {claim['worker_token']}"},
    )
    assert finished.status_code == 200
    third = max(
        (job for job in store.jobs.values() if job["thread_id"] == first["thread_id"]),
        key=lambda job: job["turn_no"],
    )
    assert third["task_prompt"] == "second follow-up"
    assert third["parent_job_id"] == first["id"]
    assert third["turn_no"] == 3
    assert third["state"] == "queued"

    blank = await _send(client, store, parent_id, {"prompt": "   \n  "})
    assert blank.status_code == 422


async def test_follow_up_is_refused_when_retained_context_is_lost(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "failed"
    store.jobs[job_id]["thread_sandbox"] = {
        "sandbox_id": "asbx_1",
        "state": "stopped_dirty",
        "context_status": "lost",
    }

    response = await _send(client, store, job_id, {"prompt": "try the next message"})

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "sandbox_context_lost"
    assert len(store.jobs) == 1
    assert store.pending == {}


async def test_follow_up_handles_context_loss_during_admission_checks(
    client: AsyncClient,
    store: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
):
    """The locked store check closes the gap after the advisory API read.

    The context is lost between the checks the route made and the turn being
    written. The message is not lost with it: it is held, with the reason on
    it, and no turn is created.
    """
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"

    async def _lost_during_insert(**_kwargs):
        return {"error": "sandbox_context_lost"}

    monkeypatch.setattr(store, "insert_follow_up", _lost_during_insert)
    response = await _send(client, store, job_id, {"prompt": "race with a lost activation"})

    assert response.status_code == 201
    assert response.json()["job"] is None
    held = response.json()["pending"]
    assert held["state"] == "held"
    assert "lost context" in held["last_error"]
    assert len(store.jobs) == 1


async def test_chat_without_shared_native_state_cannot_be_forked(
    client: AsyncClient, store: FakeStorage
):
    """Fork never falls back to copying transcript rows or workspace data."""
    source_id = await _create_job(client)
    store.jobs[source_id]["state"] = "succeeded"

    response = await client.post(f"/v1/agent/jobs/{source_id}/fork")

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "native_context_unavailable"
    assert len(store.jobs) == 1


async def test_a_fork_from_a_removed_hosts_sandbox_says_its_server_is_gone(
    client: AsyncClient, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
):
    source_id = await _create_job(client)
    store.jobs[source_id].update(
        state="succeeded",
        thread_sandbox={"sandbox_id": "asbx_shared", "shared_live": True},
    )

    async def _on_a_removed_host(**_kwargs):
        return {"error": "host_removed"}

    monkeypatch.setattr(store, "prepare_session_fork", _on_a_removed_host)
    response = await client.post(f"/v1/agent/jobs/{source_id}/fork")

    assert response.status_code == 409
    error = response.json()["detail"]["error"]
    assert error["type"] == "native_context_unavailable"
    assert "server that has been removed" in error["message"]


async def test_fork_of_an_active_turn_is_refused(client: AsyncClient, store: FakeStorage):
    """A running turn's output is not history yet, so it cannot anchor a fork."""
    job_id = await _create_job(client)

    queued = await client.post(f"/v1/agent/jobs/{job_id}/fork")
    assert queued.status_code == 409
    assert queued.json()["detail"]["error"]["type"] == "not_settled"
    assert queued.json()["detail"]["error"]["message"] == (
        "This turn is still active; wait for it to settle before forking it."
    )

    store.jobs[job_id]["state"] = "running"
    running = await client.post(f"/v1/agent/jobs/{job_id}/fork")
    assert running.status_code == 409


async def test_failed_turn_is_neither_advertised_nor_accepted_as_a_fork_anchor(
    client: AsyncClient,
    store: FakeStorage,
):
    job_id = await _create_job(client)
    job = store.jobs[job_id]
    job.update(
        state="failed",
        native_turn_no=1,
        thread_sandbox={
            "sandbox_id": "asbx_shared",
            "shared_live": True,
            "native_fork_capable": True,
            "native_state": "ready",
            "fork_operation_id": None,
            "active_attempt_id": None,
            "last_settled_job_id": job_id,
        },
    )

    detail = await client.get(f"/v1/agent/jobs/{job_id}")
    fork = await client.post(f"/v1/agent/jobs/{job_id}/fork")

    assert detail.status_code == 200
    assert detail.json()["native_fork_available"] is False
    assert fork.status_code == 409
    assert fork.json()["detail"]["error"]["type"] == "anchor_not_settled"


async def test_successful_turn_needs_a_persisted_native_ordinal_before_advertising_fork(
    client: AsyncClient,
    store: FakeStorage,
):
    job_id = await _create_job(client)
    job = store.jobs[job_id]
    job.update(
        state="succeeded",
        native_turn_no=None,
        thread_sandbox={
            "sandbox_id": "asbx_shared",
            "shared_live": True,
            "native_fork_capable": True,
            "native_state": "ready",
            "fork_operation_id": None,
            "active_attempt_id": None,
            "last_settled_job_id": job_id,
        },
    )

    without_ordinal = await client.get(f"/v1/agent/jobs/{job_id}")
    job["native_turn_no"] = 1
    with_ordinal = await client.get(f"/v1/agent/jobs/{job_id}")

    assert without_ordinal.json()["native_fork_available"] is False
    assert with_ordinal.json()["native_fork_available"] is True


async def test_legacy_retained_chat_cannot_fall_back_to_a_docker_clone(
    store: FakeStorage, monkeypatch
):
    """Historical one-chat containers stay readable but are never cloned."""

    class SandboxManager:
        async def __getattr__(self, _name: str):
            raise AssertionError("legacy fork must be rejected before a host mutation")

    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: SandboxManager())
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        store.jobs[job_id].update(
            state="succeeded",
            thread_sandbox={
                "sandbox_id": "asbx_legacy",
                "container_id": "container-legacy",
                "native_session_id": "native-session-legacy",
                "shared_live": False,
            },
        )

        response = await client.post(f"/v1/agent/jobs/{job_id}/fork")

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "native_fork_unavailable"
    assert store.session_forks == {}


@pytest.mark.parametrize("with_plugins, lease_seconds", [(False, 210), (True, 990)])
async def test_shared_chat_fork_creates_two_native_sessions_in_one_container(
    store: FakeStorage, monkeypatch, with_plugins: bool, lease_seconds: int
):
    """Clicking Fork immediately materializes a sibling process, not a clone."""

    class SandboxManager:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def fork_shared_session(self, source_thread_id: str, **kwargs: Any):
            self.calls.append((source_thread_id, kwargs))
            assert kwargs["identity"]["expected_container_id"] == "container-shared"
            assert kwargs["identity"]["disk_limit_mib"] == 16384
            assert kwargs["identity"]["origin_thread_id"] == source_thread_id
            assert kwargs["source_native_session_id"] == "native-session-source"
            assert kwargs["source_native_turn_no"] == 1
            assert kwargs["target_thread_id"] != source_thread_id
            assert kwargs["source_launch"]["plugins"] == plugins
            assert kwargs["target_launch"]["plugins"] == plugins
            return {
                "operation_id": kwargs["operation_id"],
                "sandbox_id": kwargs["identity"]["sandbox_id"],
                "sandbox_generation": kwargs["identity"]["generation"],
                "container_id": kwargs["identity"]["expected_container_id"],
                "runtime_id": kwargs["identity"]["runtime_id"],
                "runtime_version": kwargs["identity"]["runtime_version"],
                "source_thread_id": source_thread_id,
                "source_native_session_id": kwargs["source_native_session_id"],
                "target_thread_id": kwargs["target_thread_id"],
                "target_native_session_id": "native-session-child",
                "target_resident_slot_id": kwargs["target_slot_id"],
                "source_residency_generation": kwargs["source_launch"]["residency_generation"],
                "target_residency_generation": kwargs["target_launch"]["residency_generation"],
                "source_process_generation": 7,
                "target_process_generation": 1,
            }

    sandbox_manager = SandboxManager()
    plugins = (
        [{"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}] if with_plugins else []
    )
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        source = store.jobs[job_id]
        source["plugins"] = plugins
        source["state"] = "succeeded"
        source["thread_sandbox"] = {
            "thread_id": source["thread_id"],
            "sandbox_id": "asbx_shared",
            "generation": 3,
            "activation_generation": 4,
            "container_id": "container-shared",
            "container_name": "freeinference-chat-shared",
            "runtime_id": "claude-code",
            "runtime_version": "2.1.220",
            "sandbox_image_digest": "sha256:shared-image",
            "native_session_id": "native-session-source",
            "state": "running",
            "context_status": "live",
            "shared_live": True,
            "resident_slot_id": 0,
            "resident_slot_capacity": 8,
            "disk_limit_mib": 16384,
            "group_members": [],
        }

        response = await client.post(
            f"/v1/agent/jobs/{job_id}/fork",
            headers={"Idempotency-Key": "fork-on-click"},
        )

    assert response.status_code == 201
    forked = store.jobs[response.json()["id"]]
    assert forked["thread_id"] != source["thread_id"]
    assert forked["thread_sandbox"]["sandbox_id"] == source["thread_sandbox"]["sandbox_id"]
    assert forked["thread_sandbox"]["container_id"] == source["thread_sandbox"]["container_id"]
    assert forked["thread_sandbox"]["native_session_id"] == "native-session-child"
    assert response.json()["plugins"] == plugins
    assert next(iter(store.session_forks.values()))["coordinator_lease_seconds"] == lease_seconds
    assert len(sandbox_manager.calls) == 1


async def test_shared_chat_can_fork_from_a_specific_historical_turn(
    store: FakeStorage, monkeypatch
):
    """The host receives the selected prefix, not an implicit latest fork."""

    class SandboxManager:
        def __init__(self) -> None:
            self.native_turns: list[int] = []

        async def fork_shared_session(self, source_thread_id: str, **kwargs: Any):
            self.native_turns.append(kwargs["source_native_turn_no"])
            return {
                "operation_id": kwargs["operation_id"],
                "sandbox_id": kwargs["identity"]["sandbox_id"],
                "sandbox_generation": kwargs["identity"]["generation"],
                "container_id": kwargs["identity"]["expected_container_id"],
                "runtime_id": kwargs["identity"]["runtime_id"],
                "runtime_version": kwargs["identity"]["runtime_version"],
                "source_thread_id": source_thread_id,
                "source_native_session_id": kwargs["source_native_session_id"],
                "target_thread_id": kwargs["target_thread_id"],
                "target_native_session_id": "native-session-at-turn-one",
                "target_resident_slot_id": kwargs["target_slot_id"],
                "source_residency_generation": kwargs["source_launch"]["residency_generation"],
                "target_residency_generation": kwargs["target_launch"]["residency_generation"],
                "source_process_generation": 3,
                "target_process_generation": 1,
            }

    sandbox_manager = SandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first_id = await _create_job(client)
        first = store.jobs[first_id]
        first["state"] = "succeeded"
        first["thread_sandbox"] = {
            "thread_id": first["thread_id"],
            "sandbox_id": "asbx_shared_historical",
            "generation": 2,
            "activation_generation": 2,
            "container_id": "container-shared-historical",
            "container_name": "freeinference-chat-shared-historical",
            "runtime_id": "claude-code",
            "runtime_version": "2.1.220",
            "sandbox_image_digest": "sha256:shared-image",
            "native_session_id": "native-session-source",
            "state": "running",
            "context_status": "live",
            "shared_live": True,
            "resident_slot_id": 0,
            "resident_slot_capacity": 8,
            "group_members": [],
        }
        second_response = await _send(
            client, store, first_id, {"prompt": "a second completed turn"}
        )
        assert second_response.status_code == 201
        second = store.jobs[second_response.json()["job"]["id"]]
        second["state"] = "succeeded"

        response = await client.post(
            f"/v1/agent/jobs/{first_id}/fork",
            headers={"Idempotency-Key": "fork-historical-turn-one"},
        )

    assert response.status_code == 201
    forked = store.jobs[response.json()["id"]]
    assert forked["turn_no"] == 1
    assert forked["fork_source_job_id"] == first_id
    assert sandbox_manager.native_turns == [1]
    assert second["thread_id"] == first["thread_id"]


async def test_same_native_fork_request_has_one_external_coordinator(
    store: FakeStorage, monkeypatch
):
    """Two API workers must not rotate residencies or fork the CLI twice."""

    started = asyncio.Event()
    finish = asyncio.Event()

    class SandboxManager:
        calls = 0

        async def fork_shared_session(self, source_thread_id: str, **kwargs: Any):
            self.calls += 1
            started.set()
            await finish.wait()
            return {
                "operation_id": kwargs["operation_id"],
                "sandbox_id": kwargs["identity"]["sandbox_id"],
                "sandbox_generation": kwargs["identity"]["generation"],
                "container_id": kwargs["identity"]["expected_container_id"],
                "runtime_id": kwargs["identity"]["runtime_id"],
                "runtime_version": kwargs["identity"]["runtime_version"],
                "source_thread_id": source_thread_id,
                "source_native_session_id": kwargs["source_native_session_id"],
                "target_thread_id": kwargs["target_thread_id"],
                "target_native_session_id": "native-session-child",
                "target_resident_slot_id": kwargs["target_slot_id"],
                "source_residency_generation": kwargs["source_launch"]["residency_generation"],
                "target_residency_generation": kwargs["target_launch"]["residency_generation"],
                "source_process_generation": 2,
                "target_process_generation": 1,
            }

    sandbox_manager = SandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        source = store.jobs[job_id]
        source["state"] = "succeeded"
        source["thread_sandbox"] = {
            "thread_id": source["thread_id"],
            "sandbox_id": "asbx_shared",
            "generation": 1,
            "activation_generation": 1,
            "container_id": "container-shared",
            "runtime_id": "claude-code",
            "runtime_version": "2.1.220",
            "native_session_id": "native-session-source",
            "shared_live": True,
            "resident_slot_id": 0,
            "resident_slot_capacity": 8,
        }
        path = f"/v1/agent/jobs/{job_id}/fork"
        headers = {"Idempotency-Key": "same-click"}
        first = asyncio.create_task(client.post(path, headers=headers))
        await asyncio.wait_for(started.wait(), timeout=1)
        competing = await client.post(path, headers=headers)
        finish.set()
        completed = await first

    assert competing.status_code == 409
    assert competing.json()["detail"]["error"]["type"] == "fork_in_progress"
    assert competing.headers["retry-after"] == "2"
    assert completed.status_code == 201
    assert sandbox_manager.calls == 1


async def test_ready_native_fork_retry_does_not_need_the_host(store: FakeStorage, monkeypatch):
    """A lost success response remains idempotent after the host disappears."""

    class SandboxManager:
        calls = 0

        async def fork_shared_session(self, source_thread_id: str, **kwargs: Any):
            self.calls += 1
            return {
                "operation_id": kwargs["operation_id"],
                "sandbox_id": kwargs["identity"]["sandbox_id"],
                "sandbox_generation": kwargs["identity"]["generation"],
                "container_id": kwargs["identity"]["expected_container_id"],
                "runtime_id": kwargs["identity"]["runtime_id"],
                "runtime_version": kwargs["identity"]["runtime_version"],
                "source_thread_id": source_thread_id,
                "source_native_session_id": kwargs["source_native_session_id"],
                "target_thread_id": kwargs["target_thread_id"],
                "target_native_session_id": "native-session-child",
                "target_resident_slot_id": kwargs["target_slot_id"],
                "source_residency_generation": kwargs["source_launch"]["residency_generation"],
                "target_residency_generation": kwargs["target_launch"]["residency_generation"],
                "source_process_generation": 2,
                "target_process_generation": 1,
            }

    sandbox_manager = SandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        source = store.jobs[job_id]
        source["state"] = "succeeded"
        source["thread_sandbox"] = {
            "thread_id": source["thread_id"],
            "sandbox_id": "asbx_shared",
            "generation": 1,
            "activation_generation": 1,
            "container_id": "container-shared",
            "runtime_id": "claude-code",
            "runtime_version": "2.1.220",
            "native_session_id": "native-session-source",
            "shared_live": True,
            "resident_slot_id": 0,
            "resident_slot_capacity": 8,
        }
        path = f"/v1/agent/jobs/{job_id}/fork"
        headers = {"Idempotency-Key": "lost-success-response"}
        first = await client.post(path, headers=headers)
        monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: None)
        retried = await client.post(path, headers=headers)

    assert first.status_code == 201
    assert retried.status_code == 201
    assert retried.json()["id"] == first.json()["id"]
    assert sandbox_manager.calls == 1


async def test_fork_is_owner_scoped(client: AsyncClient, store: FakeStorage):
    """Someone else's job id forks as 404, indistinguishable from absent."""
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"
    store.jobs[job_id]["user_id"] = _OTHER

    response = await client.post(f"/v1/agent/jobs/{job_id}/fork")

    assert response.status_code == 404


async def test_browser_jwt_can_list_agent_jobs(store: FakeStorage):
    """Regression: the web login JWT was interpreted as an invalid API key."""
    app = _build_app(store)
    app.dependency_overrides.pop(authenticate_agent_owner)
    token = _browser_session(store)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as jwt_client:
        response = await jwt_client.get(
            "/v1/agent/jobs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"jobs": []}


async def test_an_ended_session_is_refused(store: FakeStorage):
    """A session ended server-side (sign-out, password change) authenticates nothing."""

    app = _build_app(store)
    app.dependency_overrides.pop(authenticate_agent_owner)
    token = _browser_session(store)
    store.local_accounts.revoke_all()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as session_client:
        response = await session_client.get(
            "/v1/agent/jobs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid authentication token."


# ``test_normal_api_key_still_authenticates_agent_owner`` stood here. It minted
# an ``hyi-*`` key against a faked operational store and asserted 200 plus
# ``update_key_last_used``. The API-key branch was deleted on 2026-08-04 — it
# had no producer here and never will — so the case has no subject left and
# passed only because it injected the store production never has. Recorded in
# the extraction record's "Discarded tests" register, which also says where the property
# lives now: the gateway, which still authenticates API keys for its own
# surfaces.


async def test_sandbox_token_stays_out_of_owner_routes(store: FakeStorage):
    """A structured Agent token must not be mistaken for a browser JWT."""
    from cloud_agent.tokens import SCOPE_MCP

    app = _build_app(store)
    app.dependency_overrides.pop(authenticate_agent_owner)
    token = mint_worker_token(
        job_id="ajob_0001",
        attempt_id=1,
        lease_generation=1,
        scope=SCOPE_MCP,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as sandbox_client:
        response = await sandbox_client.get(
            "/v1/agent/jobs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "insufficient_scope"


@pytest.mark.parametrize("role", ["free", "pro", "internal"])
async def test_a_gateway_allowed_non_admin_owner_can_read(store: FakeStorage, role: str):
    """The explicit policy, rather than a profile role, admits the owner."""
    app = _build_app(store, role=role)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs")
    assert response.status_code == 200


async def test_other_users_jobs_are_404_not_403(client: AsyncClient, store: FakeStorage):
    """Someone else's job is indistinguishable from a missing one."""
    foreign = await store.conversation_commands.create_job(
        user_id=_OTHER,
        repo="owner/other",
        task_prompt="not yours",
        runtime="claude-code",
        model="glm-5.1",
    )
    for path in (
        f"/v1/agent/jobs/{foreign['id']}",
        f"/v1/agent/jobs/{foreign['id']}/events",
        f"/v1/agent/jobs/{foreign['id']}/files",
        f"/v1/agent/jobs/{foreign['id']}/git",
        f"/v1/agent/jobs/{foreign['id']}/git/metadata",
    ):
        response = await client.get(path)
        assert response.status_code == 404, path

    write = await client.put(
        f"/v1/agent/jobs/{foreign['id']}/files",
        params={"path": "README.md"},
        json={"content": "forbidden"},
    )
    terminal_session_requests = [
        await client.post(
            f"/v1/agent/jobs/{foreign['id']}/terminals",
            json={"rows": 24, "cols": 80},
        ),
        await client.get(f"/v1/agent/jobs/{foreign['id']}/terminals"),
        await client.get(f"/v1/agent/jobs/{foreign['id']}/terminals/term_1/stream"),
        await client.post(
            f"/v1/agent/jobs/{foreign['id']}/terminals/term_1/input",
            json={"data": "bHMK"},
        ),
        await client.post(
            f"/v1/agent/jobs/{foreign['id']}/terminals/term_1/resize",
            json={"rows": 30, "cols": 100},
        ),
        await client.delete(f"/v1/agent/jobs/{foreign['id']}/terminals/term_1"),
    ]
    assert write.status_code == 404
    assert all(response.status_code == 404 for response in terminal_session_requests)
    cancel = await client.post(f"/v1/agent/jobs/{foreign['id']}/cancel")
    assert cancel.status_code == 404
    for method in (client.post, client.delete):
        archive = await method(f"/v1/agent/jobs/{foreign['id']}/archive")
        assert archive.status_code == 404
        missing = await method("/v1/agent/jobs/ajob_missing/archive")
        assert missing.status_code == 404
        pin = await method(f"/v1/agent/jobs/{foreign['id']}/pin")
        assert pin.status_code == 404
        missing_pin = await method("/v1/agent/jobs/ajob_missing/pin")
        assert missing_pin.status_code == 404


async def test_live_workspace_routes_use_the_job_sandbox_manager(store: FakeStorage, monkeypatch):
    """Files and Git address the same retained chat workspace."""

    class SandboxManager:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def with_start_seq(self, _start_seq: int | None):
            return self

        async def register_shared_sandbox(self, _sandbox: dict[str, Any]):
            return None

        async def files(self, workspace_id: str, path: str, *, profiling=None):
            self.calls.append(("files", workspace_id, path))
            return {
                "path": path,
                "kind": "directory",
                "entries": [],
                "writable": True,
                "source": "workspace",
            }

        async def write_file(self, workspace_id: str, path: str, content: str):
            self.calls.append(("write", workspace_id, path, content))
            return {
                "path": path,
                "kind": "file",
                "content": content,
                "size": len(content),
                "writable": True,
                "source": "workspace",
            }

        async def git_metadata(self, workspace_id: str, base_sha: str | None = None):
            self.calls.append(("git_metadata", workspace_id, base_sha))
            return {
                "available": True,
                "revision": "a" * 64,
                "branch": "agent/thread",
                "changes": [{"code": " M", "path": "README.md"}],
                "change_count": 1,
            }

        async def git(self, workspace_id: str, base_sha: str | None = None):
            self.calls.append(("git", workspace_id, base_sha))
            return {
                "available": True,
                "revision": "a" * 64,
                "branch": "agent/thread",
                "changes": [{"code": " M", "path": "README.md"}],
                "patch": "diff --git a/README.md b/README.md\n",
                "commits": [],
            }

    sandbox_manager = SandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        workspace_id = _attach_shared_workspace(store, job_id)
        listing = await local_client.get(f"/v1/agent/jobs/{job_id}/files")
        saved = await local_client.put(
            f"/v1/agent/jobs/{job_id}/files",
            params={"path": "README.md"},
            json={"content": "updated"},
        )
        metadata = await local_client.get(f"/v1/agent/jobs/{job_id}/git/metadata")
        git = await local_client.get(f"/v1/agent/jobs/{job_id}/git")

    assert listing.status_code == 200
    assert listing.json()["writable"] is True
    assert listing.json()["source"] == "workspace"
    assert saved.status_code == 200
    assert metadata.json() == {
        "available": True,
        "revision": "a" * 64,
        "branch": "agent/thread",
        "changes": [{"code": " M", "path": "README.md"}],
        "change_count": 1,
    }
    assert "patch" not in metadata.json()
    assert git.json()["branch"] == "agent/thread"
    assert sandbox_manager.calls == [
        ("files", workspace_id, ""),
        ("write", workspace_id, "README.md", "updated"),
        ("git_metadata", workspace_id, store.jobs[job_id].get("base_sha")),
        ("git", workspace_id, store.jobs[job_id].get("base_sha")),
    ]
    # Each request may start the stopped container, so each fences it first.
    assert store.workspace_start_fences == [workspace_id] * 4


async def test_binary_workspace_payload_preserves_owner_and_path_checks(
    store: FakeStorage, monkeypatch
):
    """Binary bytes use the same owned, normalized route as text previews."""

    class SandboxManager:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def with_start_seq(self, _start_seq: int | None):
            return self

        async def register_shared_sandbox(self, _sandbox: dict[str, Any]):
            return None

        async def files(self, workspace_id: str, path: str, *, profiling=None):
            self.calls.append((workspace_id, path))
            return {
                "path": path,
                "kind": "file",
                "content": None,
                "content_base64": "iVBORw0KGgoAaW1hZ2U=",
                "media_type": "image/png",
                "size": 14,
                "binary": True,
                "truncated": False,
                "source": "workspace",
            }

    sandbox_manager = SandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        workspace_id = _attach_shared_workspace(store, job_id)
        foreign = await store.conversation_commands.create_job(
            user_id=_OTHER,
            repo="owner/other",
            task_prompt="not yours",
            runtime="claude-code",
            model="glm-5.1",
        )
        _attach_shared_workspace(store, foreign["id"])
        fetched = await client.get(
            f"/v1/agent/jobs/{job_id}/files", params={"path": "artifact.png"}
        )
        refused = await client.get(
            f"/v1/agent/jobs/{foreign['id']}/files", params={"path": "artifact.png"}
        )
        traversal = await client.get(
            f"/v1/agent/jobs/{job_id}/files", params={"path": "../artifact.png"}
        )

    assert fetched.status_code == 200
    assert fetched.json()["content_base64"] == "iVBORw0KGgoAaW1hZ2U="
    assert fetched.json()["media_type"] == "image/png"
    assert fetched.json()["binary"] is True
    assert refused.status_code == 404
    assert traversal.status_code == 400
    assert sandbox_manager.calls == [(workspace_id, "artifact.png")]


async def test_missing_shared_workspace_is_a_conflict_not_a_missing_file(
    store: FakeStorage, monkeypatch
):
    """The sandbox manager's shared-only 404 describes the workspace, not a file path."""
    from cloud_agent.sandbox_manager_client import SandboxManagerError

    class SandboxManager:
        def with_start_seq(self, _start_seq: int | None):
            return self

        async def register_shared_sandbox(self, _sandbox: dict[str, Any]):
            return None

        async def files(self, _workspace_id: str, _path: str, *, profiling=None):
            raise SandboxManagerError(404, "shared chat workspace is not materialized")

    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: SandboxManager())
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        _attach_shared_workspace(store, job_id)
        response = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == {
        "type": "workspace_unavailable",
        "message": "This chat has no retained workspace.",
    }


async def test_retained_mutations_honor_thread_fence_even_through_a_settled_turn_url(
    store: FakeStorage, monkeypatch
):
    """Sandbox Manager restart cannot bypass a protected latest-turn workspace phase."""

    class SandboxManager:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def with_start_seq(self, _start_seq: int | None):
            return self

        async def register_shared_sandbox(self, _sandbox: dict[str, Any]):
            return None

        async def write_file(self, workspace_id: str, path: str, content: str):
            self.calls.append(("write", workspace_id, path, content))
            raise AssertionError("durably fenced write reached the sandbox manager")

        async def terminal_input(self, workspace_id: str, terminal_id: str, *, data: str):
            self.calls.append(("input", workspace_id, terminal_id, data))
            raise AssertionError("durably fenced input reached the sandbox manager")

        async def resize_terminal(
            self, workspace_id: str, terminal_id: str, *, rows: int, cols: int
        ):
            self.calls.append(("resize", workspace_id, terminal_id, rows, cols))
            raise AssertionError("durably fenced resize reached the sandbox manager")

        async def delete_terminal(self, workspace_id: str, terminal_id: str):
            self.calls.append(("delete", workspace_id, terminal_id))
            return _terminal_session_descriptor(state="closed")

    sandbox_manager = SandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        job = store.jobs[job_id]
        job["state"] = "succeeded"
        _attach_shared_workspace(store, job_id)
        store.thread_workspace_readiness[job["thread_id"]] = False

        saved = await client.put(
            f"/v1/agent/jobs/{job_id}/files?path=README.md",
            json={"content": "blocked"},
        )
        wrote = await client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "eA=="},
        )
        resized = await client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/resize",
            json={"rows": 30, "cols": 100},
        )
        killed = await client.delete(f"/v1/agent/jobs/{job_id}/terminals/term_1")

    assert {saved.status_code, wrote.status_code, resized.status_code} == {409}
    for response in (saved, wrote, resized):
        assert response.json()["detail"]["error"]["type"] == "workspace_not_ready"
    assert killed.status_code == 200
    assert sandbox_manager.calls == [("delete", job["thread_id"], "term_1")]


def _terminal_session_descriptor(*, state: str = "running") -> dict[str, Any]:
    return {
        "id": "term_1",
        "shell": "/bin/bash",
        "state": state,
        "cwd": "/workspace",
        "rows": 24,
        "cols": 80,
        "last_seq": 2,
    }


class _FiniteTerminalStream:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _TerminalSessionSandboxManager:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.suspended: set[str] = set()
        self.stream = _FiniteTerminalStream(
            b'event: output\ndata: {"seq":1,"data":"aGk="}\n\n',
            b'event: exit\ndata: {"seq":2,"exit_code":0}\n\n',
        )

    def with_start_seq(self, _start_seq: int | None):
        return self

    async def register_shared_sandbox(self, _sandbox: dict[str, Any]):
        return None

    async def create_terminal(self, workspace_id: str, *, rows: int, cols: int, profiling=None):
        self.calls.append(("create", workspace_id, rows, cols))
        return _terminal_session_descriptor()

    async def list_terminals(self, workspace_id: str, *, profiling=None):
        self.calls.append(("list", workspace_id))
        return {"terminals": [_terminal_session_descriptor()]}

    async def suspend_terminals(
        self,
        workspace_id: str,
        *,
        lease_generation: int,
        job_id: str | None = None,
    ):
        self.calls.append(("suspend_all", workspace_id, lease_generation, job_id))
        self.suspended.add(workspace_id)
        return {"ok": True}

    async def resume_terminals(
        self,
        workspace_id: str,
        *,
        lease_generation: int,
        job_id: str | None = None,
    ):
        self.calls.append(("resume_all", workspace_id, lease_generation, job_id))
        self.suspended.discard(workspace_id)
        return {"ok": True}

    async def resume_settled_terminals(
        self,
        workspace_id: str,
        *,
        job_id: str | None = None,
    ):
        self.calls.append(("resume_settled", workspace_id, job_id))
        self.suspended.discard(workspace_id)
        return {"ok": True}

    async def stream_terminal(
        self, workspace_id: str, terminal_id: str, *, after: int, profiling=None
    ):
        self.calls.append(("stream", workspace_id, terminal_id, after))
        return self.stream

    async def terminal_input(self, workspace_id: str, terminal_id: str, *, data: str):
        self.calls.append(("input", workspace_id, terminal_id, data))
        if workspace_id in self.suspended:
            from cloud_agent.sandbox_manager_client import SandboxManagerError

            raise SandboxManagerError(409, "workspace terminals are suspended")
        return _terminal_session_descriptor()

    async def resize_terminal(self, workspace_id: str, terminal_id: str, *, rows: int, cols: int):
        self.calls.append(("resize", workspace_id, terminal_id, rows, cols))
        return {**_terminal_session_descriptor(), "rows": rows, "cols": cols}

    async def delete_terminal(self, workspace_id: str, terminal_id: str):
        self.calls.append(("delete", workspace_id, terminal_id))
        return _terminal_session_descriptor(state="closed")


async def test_interactive_terminal_routes_proxy_owner_session_lifecycle(
    store: FakeStorage, monkeypatch
):
    """The gateway keeps sandbox manager credentials private while proxying PTY controls."""
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        workspace_id = _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["state"] = "succeeded"

        created = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )
        listed = await local_client.get(f"/v1/agent/jobs/{job_id}/terminals")
        wrote = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": base64.b64encode(b"ls\n").decode()},
        )
        resized = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/resize",
            json={"rows": 40, "cols": 120},
        )
        killed = await local_client.delete(f"/v1/agent/jobs/{job_id}/terminals/term_1")

    assert created.status_code == 200
    assert created.json()["id"] == "term_1"
    assert listed.json() == {"terminals": [_terminal_session_descriptor()]}
    assert wrote.status_code == 200
    assert resized.json()["rows"] == 40
    assert resized.json()["cols"] == 120
    assert killed.status_code == 200
    assert killed.json()["state"] == "closed"
    assert sandbox_manager.calls == [
        ("create", workspace_id, 24, 80),
        ("list", workspace_id),
        ("input", workspace_id, "term_1", "bHMK"),
        ("resize", workspace_id, "term_1", 40, 120),
        ("delete", workspace_id, "term_1"),
    ]


async def test_terminal_list_passes_the_sandbox_managers_advertised_limits_through(
    store: FakeStorage, monkeypatch
):
    """The browser checks these limits before asking, so it has to receive them."""
    sandbox_manager = _TerminalSessionSandboxManager()
    advertised = {
        "terminals": [_terminal_session_descriptor()],
        "max_sessions": 2,
        "max_input_bytes": 1024,
    }

    async def list_terminals(workspace_id: str, *, profiling=None):
        sandbox_manager.calls.append(("list", workspace_id))
        return advertised

    monkeypatch.setattr(sandbox_manager, "list_terminals", list_terminals)
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["state"] = "succeeded"
        listed = await client.get(f"/v1/agent/jobs/{job_id}/terminals")

    assert listed.status_code == 200
    assert listed.json() == advertised


async def test_interactive_terminal_stream_is_relayed_byte_for_byte(
    store: FakeStorage, monkeypatch
):
    """Output and exit SSE frames cross the gateway without re-encoding."""
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)

    async def unexpected_event_query(**_kwargs):
        raise AssertionError("attached terminal streams must not query lifecycle events")

    monkeypatch.setattr(store, "list_events_after", unexpected_event_query)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        workspace_id = _attach_shared_workspace(store, job_id)
        response = await local_client.get(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/stream",
            params={"after": 7},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == (
        b'event: output\ndata: {"seq":1,"data":"aGk="}\n\n'
        b'event: exit\ndata: {"seq":2,"exit_code":0}\n\n'
    )
    assert sandbox_manager.calls == [("stream", workspace_id, "term_1", 7)]
    assert sandbox_manager.stream.closed is True


class _EndlessTerminalStream:
    """A shell that never exits, its frames cut where a network might cut them."""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        seq = 0
        while True:
            seq += 1
            frame = f'event: output\ndata: {{"seq":{seq},"data":"aGk="}}\n\n'.encode()
            for cut in (frame[:9], frame[9:-1], frame[-1:]):
                await asyncio.sleep(0.005)
                yield cut

    async def aclose(self) -> None:
        self.closed = True


async def test_a_live_terminal_stream_ends_at_the_owner_stream_cap_between_frames(
    store: FakeStorage, monkeypatch
):
    """Admission is checked when a stream opens, so a live shell's is not open forever.

    The relay stops after the cap, and only where a frame ends: the browser
    resumes after the last sequence it applied, and that request is admitted
    again. The sandbox manager's stream is released.
    """
    sandbox_manager = _TerminalSessionSandboxManager()
    sandbox_manager.stream = _EndlessTerminalStream()  # type: ignore[assignment]
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    monkeypatch.setattr(agent_workspace, "OWNER_STREAM_MAX_S", 0.1)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        _attach_shared_workspace(store, job_id)
        response = await asyncio.wait_for(
            client.get(f"/v1/agent/jobs/{job_id}/terminals/term_1/stream"), 5
        )

    assert response.status_code == 200
    *frames, rest = response.content.split(b"\n\n")
    assert rest == b"", "the relay stopped where a frame ends"
    sequences = [json.loads(frame.split(b"data: ", 1)[1])["seq"] for frame in frames]
    assert sequences == list(range(1, len(frames) + 1))
    assert sequences, "frames before the cap are relayed"
    assert sandbox_manager.stream.closed is True


async def test_full_terminal_lifecycle_is_available_while_agent_runs(
    store: FakeStorage, monkeypatch
):
    """PTY creation waits for checkout, then stays available during the run."""
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        queued_create = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )
        queued_list = await local_client.get(f"/v1/agent/jobs/{job_id}/terminals")

        workspace_id = _attach_shared_workspace(store, job_id)
        store.thread_workspace_readiness[workspace_id] = False
        claimed = await store.attempt_commands.claim_job(
            worker_id="worker-1",
            lease_ttl_seconds=60,
            host="runner-test",
            host_credential_hash="test-credential-hash",
        )
        assert claimed is not None
        early_create = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )
        early_list = await local_client.get(f"/v1/agent/jobs/{job_id}/terminals")

        event_id = await store.attempt_commands.append_event(
            attempt_id=claimed["attempt_id"],
            lease_generation=claimed["lease_generation"],
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
        assert event_id is not None
        store.thread_workspace_readiness[workspace_id] = True
        create = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )
        write_before_retry = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "bHMK"},
        )
        store.jobs[job_id]["state"] = "queued"
        store.thread_workspace_readiness[workspace_id] = False
        write_while_requeued = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "cHdkCg=="},
        )
        store.jobs[job_id]["state"] = "running"
        store.jobs[job_id]["current_attempt_id"] = 101
        store.live_fence = (101, 2)
        write_while_preparing = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "cHdkCg=="},
        )
        retry_event_id = await store.attempt_commands.append_event(
            attempt_id=101,
            lease_generation=2,
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
        assert retry_event_id is not None
        store.thread_workspace_readiness[workspace_id] = True
        write_after_retry = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "cHdkCg=="},
        )
        resize = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/resize",
            json={"rows": 30, "cols": 100},
        )
        listed = await local_client.get(f"/v1/agent/jobs/{job_id}/terminals")
        killed_once = await local_client.delete(f"/v1/agent/jobs/{job_id}/terminals/term_1")
        killed_twice = await local_client.delete(f"/v1/agent/jobs/{job_id}/terminals/term_1")

    for response in (queued_create, queued_list, early_create, early_list):
        assert response.status_code == 409
        assert response.json()["detail"]["error"]["type"] == "workspace_not_ready"
    for response in (write_while_requeued, write_while_preparing):
        assert response.status_code == 409
        assert response.json()["detail"]["error"]["type"] == "workspace_not_ready"
    for response in (
        create,
        write_before_retry,
        write_after_retry,
        resize,
        listed,
        killed_once,
        killed_twice,
    ):
        assert response.status_code == 200
    assert create.json()["state"] == "running"
    assert write_before_retry.json()["state"] == "running"
    assert write_after_retry.json()["state"] == "running"
    assert resize.json()["rows"] == 30
    assert resize.json()["cols"] == 100
    assert listed.status_code == 200
    assert listed.json() == {"terminals": [_terminal_session_descriptor()]}
    assert killed_once.json()["state"] == "closed"
    assert killed_twice.json()["state"] == "closed"
    assert sandbox_manager.calls == [
        ("create", workspace_id, 24, 80),
        ("input", workspace_id, "term_1", "bHMK"),
        ("input", workspace_id, "term_1", "cHdkCg=="),
        ("resize", workspace_id, "term_1", 30, 100),
        ("list", workspace_id),
        ("delete", workspace_id, "term_1"),
        ("delete", workspace_id, "term_1"),
    ]


async def test_worker_suspends_terminals_around_protected_workspace_phases(
    store: FakeStorage, monkeypatch
):
    """Preparation and capture pause PTYs; execution and settled jobs resume them."""
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        claim = await local_client.post(
            "/v1/agent/worker/claim",
            json=connection_claim({"worker_id": "worker-1"}),
        )
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
        workspace_id = _attach_shared_workspace(store, job_id)

        preparing = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/suspend",
            json={"phase": "workspace_preparing"},
            headers=auth,
        )
        resumed = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/resume",
            headers=auth,
        )
        input_during_run = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "eA=="},
        )
        finalizing = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/suspend",
            json={"phase": "workspace_finalizing"},
            headers=auth,
        )
        input_during_capture = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "eA=="},
        )
        finished = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/finish",
            json={"state": "succeeded"},
            headers=auth,
        )
        input_after_finish = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "eA=="},
        )

    assert preparing.status_code == 200
    assert resumed.status_code == 200
    assert input_during_run.status_code == 200
    assert finalizing.status_code == 200
    assert input_during_capture.status_code == 409
    assert finished.status_code == 200
    assert input_after_finish.status_code == 200
    assert sandbox_manager.calls == [
        ("suspend_all", workspace_id, 1, job_id),
        ("resume_all", workspace_id, 1, job_id),
        ("input", workspace_id, "term_1", "eA=="),
        ("suspend_all", workspace_id, 1, job_id),
        ("input", workspace_id, "term_1", "eA=="),
        ("resume_all", workspace_id, 1, job_id),
        ("input", workspace_id, "term_1", "eA=="),
    ]


async def test_settled_terminal_access_replays_durable_resume_after_restart(
    store: FakeStorage,
    monkeypatch,
):
    """The first owner access repairs a persisted resume missed before restart."""
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        workspace_id = _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["state"] = "cancelled"
        store.jobs[job_id]["terminal_resume_pending"] = True
        response = await local_client.get(f"/v1/agent/jobs/{job_id}/terminals")

    assert response.status_code == 200
    assert sandbox_manager.calls == [
        ("resume_settled", workspace_id, job_id),
        ("list", workspace_id),
    ]
    assert store.jobs[job_id]["terminal_resume_pending"] is False


async def test_worker_re_suspends_terminals_if_ready_event_loses_lease(
    store: FakeStorage,
    monkeypatch,
):
    """A lease expiring during sandbox manager resume cannot expose background processes."""
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        claim = await local_client.post(
            "/v1/agent/worker/claim",
            json=connection_claim({"worker_id": "worker-1"}),
        )
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
        workspace_id = _attach_shared_workspace(store, job_id)
        await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/suspend",
            json={"phase": "workspace_preparing"},
            headers=auth,
        )

        async def lose_lease_before_ready(**_kwargs):
            store.live_fence = None
            return None

        monkeypatch.setattr(store, "append_event", lose_lease_before_ready)
        response = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/resume",
            headers=auth,
        )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "lease_lost"
    assert sandbox_manager.calls == [
        ("suspend_all", workspace_id, 1, job_id),
        ("resume_all", workspace_id, 1, job_id),
        ("suspend_all", workspace_id, 1, job_id),
    ]


async def test_worker_re_suspends_terminals_if_finish_loses_lease(
    store: FakeStorage,
    monkeypatch,
):
    """A failed terminal transition compensates after sandbox manager resume."""
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        claim = await local_client.post(
            "/v1/agent/worker/claim",
            json=connection_claim({"worker_id": "worker-1"}),
        )
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
        workspace_id = _attach_shared_workspace(store, job_id)
        await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/suspend",
            json={"phase": "workspace_finalizing"},
            headers=auth,
        )

        async def lose_lease_before_finish(**_kwargs):
            store.live_fence = None
            return False

        monkeypatch.setattr(store, "transition", lose_lease_before_finish)
        response = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/finish",
            json={"state": "succeeded"},
            headers=auth,
        )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "lease_lost"
    assert sandbox_manager.calls == [
        ("suspend_all", workspace_id, 1, job_id),
        ("resume_all", workspace_id, 1, job_id),
        ("suspend_all", workspace_id, 1, job_id),
    ]


async def test_terminal_readiness_uses_the_shared_group_barrier(store: FakeStorage, monkeypatch):
    """Owner PTYs open only after the durable shared workspace barrier clears."""
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        workspace_id = _attach_shared_workspace(store, job_id)
        store.thread_workspace_readiness[workspace_id] = False
        blocked = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

        store.thread_workspace_readiness[workspace_id] = True
        ready = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"]["type"] == "workspace_not_ready"
    assert ready.status_code == 200
    assert sandbox_manager.calls == [("create", workspace_id, 24, 80)]


async def test_terminal_routes_recheck_entitlement_before_contacting_sandbox_manager(
    store: FakeStorage, monkeypatch
):
    """Owning a chat does not bypass current repository entitlement."""
    job = await store.conversation_commands.create_job(
        user_id=_OWNER,
        repo="someone/private",
        task_prompt="old task",
        runtime="claude-code",
        model="glm-5.1",
    )
    _attach_shared_workspace(store, job["id"])
    job["state"] = "succeeded"
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        response = await local_client.post(
            f"/v1/agent/jobs/{job['id']}/terminals",
            json={"rows": 24, "cols": 80},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "repo_not_allowed"
    assert sandbox_manager.calls == []


async def test_terminal_routes_report_missing_sandbox_manager_and_workspace_as_conflicts(
    store: FakeStorage, monkeypatch
):
    """A retained chat reports missing sandbox manager/workspace as actionable conflicts."""
    from cloud_agent.sandbox_manager_client import SandboxManagerError

    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["state"] = "succeeded"
        monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: None)
        missing_sandbox_manager = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

        class MissingSandboxManager(_TerminalSessionSandboxManager):
            async def create_terminal(
                self, workspace_id: str, *, rows: int, cols: int, profiling=None
            ):
                raise SandboxManagerError(404, "shared chat workspace is not materialized")

        monkeypatch.setattr(
            sandbox_manager_routing,
            "_client_for",
            lambda _host: MissingSandboxManager(),
        )
        missing_workspace = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

    assert missing_sandbox_manager.status_code == 409
    assert missing_sandbox_manager.json()["detail"]["error"]["type"] == "workspace_unavailable"
    assert missing_workspace.status_code == 409
    assert missing_workspace.json()["detail"]["error"]["type"] == "workspace_unavailable"


@pytest.mark.parametrize(
    ("path", "method", "body"),
    [
        ("terminals", "post", {"rows": 1, "cols": 80}),
        ("terminals", "post", {"rows": 24, "cols": 501}),
        ("terminals/term_1/input", "post", {"data": "not base64"}),
        (
            "terminals/term_1/input",
            "post",
            {"data": base64.b64encode(b"x" * (64 * 1024 + 1)).decode()},
        ),
        ("terminals/term_1/resize", "post", {"rows": 201, "cols": 80}),
        ("terminals/term_1/resize", "post", {"rows": 24, "cols": 19}),
        ("terminals/term_1/stream?after=-1", "get", None),
        (f"terminals/term_1/stream?after={2**63}", "get", None),
    ],
)
async def test_interactive_terminal_request_bounds(
    client: AsyncClient, path: str, method: str, body: dict[str, Any] | None
):
    """PTY dimensions, input bytes, and replay cursors are bounded at the gateway."""
    job_id = await _create_job(client)
    url = f"/v1/agent/jobs/{job_id}/{path}"
    if body is None:
        response = await getattr(client, method)(url)
    else:
        response = await getattr(client, method)(url, json=body)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../secret",
        "/etc/passwd",
        "C:/Windows",
        r"dir\file",
        ".GIT/config",
        "a//b",
        "nul\x00path",
    ],
)
async def test_workspace_files_reject_path_traversal(store: FakeStorage, unsafe_path: str):
    """Decoded traversal and platform-specific aliases fail before GitHub is called."""
    from cloud_agent.api.deps import get_agent_app_credentials

    app_creds = _WorkspaceContentsApp({"": []})
    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        store.jobs[job_id]["base_sha"] = "b" * 40
        response = await local_client.get(
            f"/v1/agent/jobs/{job_id}/files", params={"path": unsafe_path}
        )

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "invalid_path"
    assert app_creds.calls == []


async def test_workspace_files_recheck_repository_entitlement(store: FakeStorage):
    """Owning a legacy row does not entitle its repository after access is revoked."""
    from cloud_agent.api.deps import get_agent_app_credentials

    foreign_repo_job = await store.conversation_commands.create_job(
        user_id=_OWNER,
        repo="someone/private",
        task_prompt="old task",
        runtime="claude-code",
        model="glm-5.1",
        base_sha="c" * 40,
    )
    app_creds = _WorkspaceContentsApp({"": []})
    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        response = await local_client.get(f"/v1/agent/jobs/{foreign_repo_job['id']}/files")

    assert response.status_code == 403
    assert app_creds.calls == []


async def test_cancel_queued_job(client: AsyncClient):
    """Cancelling a queued job reports the terminal state immediately."""
    job_id = await _create_job(client)
    response = await client.post(f"/v1/agent/jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert response.json()["cancel_requested"] is True


async def test_cancel_requeued_job_authoritatively_resumes_terminals(
    store: FakeStorage,
    monkeypatch,
):
    """Cancellation must not strand sessions paused by an expired attempt."""
    sandbox_manager = _TerminalSessionSandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        thread_id = _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["current_attempt_id"] = 100
        response = await local_client.post(f"/v1/agent/jobs/{job_id}/cancel")

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert sandbox_manager.calls == [("resume_settled", thread_id, job_id)]
    assert store.jobs[job_id]["terminal_resume_pending"] is False


async def test_cancel_reports_its_commit_when_the_workspace_host_cannot_be_reached(
    store: FakeStorage,
    monkeypatch,
):
    """Stop answers with the cancel it committed, not with the host it could not reach.

    The retained group's sandbox manager cannot be hydrated, so the terminals are not
    resumed now; the durable marker stays set for the reaper's next pass.
    """
    from cloud_agent.sandbox_manager_client import SandboxManagerError

    sandbox_manager = _TerminalSessionSandboxManager()

    async def unreachable(_sandbox: dict[str, Any]) -> None:
        raise SandboxManagerError(503, "The shared chat supervisor is temporarily unavailable.")

    monkeypatch.setattr(sandbox_manager, "register_shared_sandbox", unreachable)
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["current_attempt_id"] = 100
        response = await client.post(f"/v1/agent/jobs/{job_id}/cancel")

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "cancelled"
    assert response.json()["cancel_requested"] is True
    assert sandbox_manager.calls == []
    assert store.jobs[job_id]["terminal_resume_pending"] is True, "left for the reaper"


async def test_worker_flow_claim_event_finish(client: AsyncClient, store: FakeStorage):
    """The worker path is claim → token → event → fenced finish."""
    job_id = await _create_job(client)

    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    assert claim.status_code == 200
    body = claim.json()
    assert body["job_id"] == job_id
    token = body["worker_token"]
    auth = {"Authorization": f"Bearer {token}"}

    beat = await client.post(f"/v1/agent/worker/jobs/{job_id}/heartbeat", json={}, headers=auth)
    assert beat.status_code == 200
    assert beat.json()["cancel_requested"] is False

    event = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json={"event_type": "message", "payload": {"text": "hello"}},
        headers=auth,
    )
    assert event.status_code == 201
    assert event.json()["event_id"] == 1

    # The owner can read the event back. Git changes remain in the retained
    # workspace and are never uploaded as a per-turn worker artifact.
    events = await client.get(f"/v1/agent/jobs/{job_id}/events")
    assert [event["event_type"] for event in events.json()["events"]] == ["message"]
    assert events.json()["next_cursor"] == 1

    finish = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish",
        json={"state": "succeeded"},
        headers=auth,
    )
    assert finish.status_code == 200
    assert store.jobs[job_id]["state"] == "succeeded"


async def test_worker_residency_establish_uses_the_token_attempt_fence(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """The API must not substitute a same-host current attempt for its caller."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    calls: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    async def establish_residency(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "thread_id": store.jobs[job_id]["thread_id"],
            "sandbox_id": "asbx_test",
            "generation": 7,
            "previous_grant_id": None,
            "establishing_attempt_id": kwargs["attempt_id"],
            "user_id": _OWNER,
            "model": store.jobs[job_id]["model"],
            "runtime": store.jobs[job_id]["runtime"],
            "model_connection_id": CONNECTION_ID,
        }

    async def record_shared_thread_residency(**kwargs: Any) -> bool:
        records.append(dict(kwargs))
        return True

    monkeypatch.setattr(store, "establish_residency", establish_residency, raising=False)
    monkeypatch.setattr(
        store, "record_shared_thread_residency", record_shared_thread_residency, raising=False
    )
    response = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/sandbox/residency",
        json={
            "sandbox_id": "asbx_test",
            "runtime_id": "claude-code",
            "runtime_version": "2.1.220",
        },
        headers=auth,
    )

    assert response.status_code == 200, response.text
    assert response.json()["generation"] == 7
    # The process's MCP credential names this residency, not the attempt.
    assert parse_residency_mcp_token(response.json()["mcp_credential"]) == {
        "thread_id": store.jobs[job_id]["thread_id"],
        "residency_generation": 7,
    }
    assert calls[0]["attempt_id"] == 100
    assert calls[0]["lease_generation"] == 1
    assert calls[0]["thread_id"] == store.jobs[job_id]["thread_id"]
    assert calls[0]["host"] == "runner-test"
    assert records == [
        {
            "thread_id": store.jobs[job_id]["thread_id"],
            "attempt_id": 100,
            "lease_generation": 1,
            "sandbox_id": "asbx_test",
            "residency_generation": 7,
        }
    ]


async def test_retained_lifecycle_writes_echo_every_generation_fence(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    process = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/sandbox/process",
        json={
            "sandbox_generation": 3,
            "activation_generation": 7,
            "residency_generation": 11,
            "process_generation": 4,
        },
        headers=auth,
    )
    admitted = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/sandbox/admit",
        json={
            "sandbox_generation": 3,
            "activation_generation": 7,
            "residency_generation": 11,
        },
        headers=auth,
    )
    settled = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/sandbox/settle",
        json={
            "sandbox_generation": 3,
            "activation_generation": 7,
            "residency_generation": 11,
            "context_status": "live",
            "native_session_id": "native-one",
        },
        headers=auth,
    )

    assert process.status_code == 200, process.text
    assert admitted.status_code == 200, admitted.text
    assert settled.status_code == 200, settled.text
    assert store.sandbox_processes == [
        {
            "thread_id": store.jobs[job_id]["thread_id"],
            "attempt_id": 100,
            "lease_generation": 1,
            "sandbox_generation": 3,
            "activation_generation": 7,
            "residency_generation": 11,
            "process_generation": 4,
        }
    ]
    assert store.sandbox_admissions == [
        {
            "thread_id": store.jobs[job_id]["thread_id"],
            "job_id": job_id,
            "attempt_id": 100,
            "lease_generation": 1,
            "sandbox_generation": 3,
            "activation_generation": 7,
            "residency_generation": 11,
        }
    ]
    assert store.sandbox_settlements == [
        {
            "thread_id": store.jobs[job_id]["thread_id"],
            "attempt_id": 100,
            "lease_generation": 1,
            "sandbox_generation": 3,
            "activation_generation": 7,
            "residency_generation": 11,
            "job_id": job_id,
            "context_status": "live",
            "native_session_id": "native-one",
            "base_sha": None,
            "idle_timeout_seconds": 1800.0,
        }
    ]


async def test_marking_a_group_running_pushes_its_ssh_state_as_it_is_now(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """A change made while the group started reached no push; this one reaches it.

    The claim's SSH state was read before the group was reserved, and an
    owner's change only looks for running groups. Once the group is running,
    every later change finds it, and this push covers the ones before.
    """
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    thread_id = _thread_of(store, job_id)
    pushes: list[tuple[Any, dict[str, Any]]] = []

    async def push(storage: Any, **kwargs: Any) -> None:
        pushes.append((storage, kwargs))

    async def mark_thread_sandbox_running(**kwargs: Any) -> dict[str, Any] | None:
        if not store._fenced(kwargs["attempt_id"], kwargs["lease_generation"]):
            return None
        return {
            "thread_id": kwargs["thread_id"],
            "sandbox_id": "asbx_test",
            "generation": kwargs["sandbox_generation"],
            "host": "runner-test",
            "container_id": kwargs["container_id"],
            "container_name": "freeinference-test",
            "runtime_id": "claude-code",
            "execution_profile": "trusted-v1",
            "state": "running",
            "context_status": kwargs["context_status"],
            "activation_generation": kwargs["activation_generation"],
            "process_generation": 0,
        }

    monkeypatch.setattr(worker, "push_ssh_access", push, raising=False)
    monkeypatch.setattr(
        store, "mark_thread_sandbox_running", mark_thread_sandbox_running, raising=False
    )
    body = {
        "sandbox_generation": 3,
        "activation_generation": 7,
        "container_id": "container-test",
        "context_status": "new",
    }

    running = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/sandbox/running", json=body, headers=auth
    )

    assert running.status_code == 200, running.text
    assert pushes == [(store, {"thread_ids": [thread_id]})]

    # A superseded attempt confirms nothing, and so pushes nothing.
    store.live_fence = (101, 2)
    lost = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/sandbox/running", json=body, headers=auth
    )

    assert lost.status_code == 409, lost.text
    assert len(pushes) == 1


async def test_shared_abandon_releases_the_exact_residency(store: FakeStorage, monkeypatch):
    """A pre-admission process failure must not leave its residency open."""

    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        claim = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

        job = store.jobs[job_id]
        sandbox = {
            "thread_id": job["thread_id"],
            "sandbox_id": "asbx_shared",
            "generation": 3,
            "host": "runner-a",
            "container_id": "container-shared",
            "container_name": "freeinference-shared",
            "runtime_id": "claude-code",
            "runtime_version": "2.1.220",
            "sandbox_image_digest": "sha256:shared",
            "execution_profile": "trusted-v1",
            "state": "stopping",
            "context_status": "new",
            "activation_generation": 7,
            "process_generation": 4,
            "shared_live": True,
            "origin_thread_id": job["thread_id"],
            "resident_state": "stopped",
            "residency_generation": 11,
            "resident_slot_id": 0,
            "resident_slot_capacity": 8,
        }
        releases: list[tuple[str, int, str]] = []

        async def abandon_thread_sandbox(**kwargs: Any) -> dict[str, Any]:
            assert kwargs["attempt_id"] == 100
            assert kwargs["lease_generation"] == 1
            return sandbox

        async def release_residency(*, thread_id: str, generation: int, host: str) -> str:
            releases.append((thread_id, generation, host))
            return "agr_resident_shared"

        monkeypatch.setattr(
            store,
            "abandon_thread_sandbox",
            abandon_thread_sandbox,
            raising=False,
        )
        monkeypatch.setattr(store, "release_residency", release_residency)

        response = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/sandbox/abandon",
            json={
                "sandbox_generation": 3,
                "activation_generation": 7,
                "lost": False,
                "detail": "process failed before admission",
            },
            headers=auth,
        )

    assert response.status_code == 200, response.text
    assert releases == [(job["thread_id"], 11, "runner-a")]


async def test_worker_api_bounds_oversized_events_without_failing_or_duplicating_the_turn(
    client: AsyncClient, store: FakeStorage
):
    """Older runners can upload large events; the API stores a labelled preview."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    body = {
        "event_type": "tool_result",
        "payload": {"tool_use_id": "tool-1", "content": "image data " * 100_000},
        "event_seq": 1,
    }
    first = await client.post(f"/v1/agent/worker/jobs/{job_id}/events", json=body, headers=auth)
    replay = await client.post(f"/v1/agent/worker/jobs/{job_id}/events", json=body, headers=auth)
    assert first.status_code == replay.status_code == 201
    assert first.json()["event_id"] == replay.json()["event_id"]
    assert len(store.events) == 1
    payload = store.events[0]["payload"]
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 256 * 1024
    assert payload["truncated"] is True
    assert payload["tool_use_id"] == "tool-1"
    assert "truncated" in payload["content"]


async def test_empty_queue_claim_returns_null(client: AsyncClient):
    """Claiming with nothing queued is a 200 with a null body, not an error."""
    response = await client.post(
        "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
    )
    assert response.status_code == 200
    assert response.json() is None


async def test_lost_lease_maps_to_409_on_every_write_path(client: AsyncClient, store: FakeStorage):
    """Once the fence moves on, every remaining worker write returns 409."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    # The reaper hands ownership to a new attempt/generation.
    store.live_fence = (101, 2)

    calls = [
        ("post", f"/v1/agent/worker/jobs/{job_id}/heartbeat", {}),
        ("post", f"/v1/agent/worker/jobs/{job_id}/events", {"event_type": "message"}),
        ("post", f"/v1/agent/worker/jobs/{job_id}/finish", {"state": "succeeded"}),
    ]
    for method, path, payload in calls:
        response = await getattr(client, method)(
            path, json=payload if payload is not None else {}, headers=auth
        )
        assert response.status_code == 409, path
        assert response.json()["detail"]["error"]["type"] == "lease_lost"


async def test_worker_token_is_required_and_scoped(client: AsyncClient):
    """Missing, malformed, and cross-job tokens are all rejected."""
    job_id = await _create_job(client)
    body = {"event_type": "message"}

    missing = await client.post(f"/v1/agent/worker/jobs/{job_id}/events", json=body)
    assert missing.status_code == 401

    bad = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json=body,
        headers={"Authorization": "Bearer not-a-token"},
    )
    assert bad.status_code == 401

    # A valid token minted for another job must not work here.
    foreign_token = mint_worker_token(job_id="ajob_other", attempt_id=1, lease_generation=1)
    wrong_job = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json=body,
        headers={"Authorization": f"Bearer {foreign_token}"},
    )
    assert wrong_job.status_code == 409


async def test_finish_rejects_non_terminal_state(client: AsyncClient):
    """A worker cannot 'finish' into a non-terminal state."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    response = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "running"}, headers=auth
    )
    assert response.status_code == 400


async def test_sse_stream_replays_and_closes_on_terminal_state(
    client: AsyncClient, store: FakeStorage
):
    """The SSE stream emits id-tagged frames and ends with job_finished."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json={"event_type": "message", "payload": {"text": "one"}},
        headers=auth,
    )
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "succeeded"}, headers=auth
    )

    async with client.stream("GET", f"/v1/agent/jobs/{job_id}/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert "id: 1" in body
    assert "event: message" in body
    assert "event: job_finished" in body
    payload = json.loads(body.split("event: job_finished\ndata: ")[1].split("\n")[0])
    assert payload["state"] == "succeeded"


async def test_sse_resumes_from_last_event_id(client: AsyncClient, store: FakeStorage):
    """Last-Event-ID skips already-delivered events."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    for text in ("one", "two"):
        await client.post(
            f"/v1/agent/worker/jobs/{job_id}/events",
            json={"event_type": "message", "payload": {"text": text}},
            headers=auth,
        )
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "succeeded"}, headers=auth
    )

    async with client.stream(
        "GET", f"/v1/agent/jobs/{job_id}/stream", headers={"Last-Event-ID": "1"}
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert '"text":"one"' not in body
    assert '"text":"two"' in body


async def test_missing_store_returns_503(store: FakeStorage):
    """Without a database the agent API reports 503 rather than crashing."""
    app = _build_app(None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs")
    assert response.status_code == 503


async def test_claim_requires_an_enrolled_host_identity(store: FakeStorage):
    """An ordinary customer cannot dequeue and read another tenant's job.

    Regression: claim_job takes the oldest queued job across all tenants and
    the response carries that job's repo, prompt, metadata, and a working
    capability token — so plain API-key auth here was a cross-tenant leak.
    """
    await store.conversation_commands.create_job(
        user_id="somebody-else",
        repo="private/repo",  # entitled below; the point here is the auth gate
        task_prompt="confidential task",
        runtime="claude-code",
        model="glm-5.1",
    )

    app = _build_app(store, role="pro", claim_auth=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )
    assert denied.status_code in (401, 403)
    assert store.jobs[next(iter(store.jobs))]["state"] == "queued"

    app = _build_app(store, claim_auth=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )
    assert allowed.status_code == 200
    assert allowed.json()["repo"] == "private/repo"


async def test_pool_wide_bearer_token_does_not_open_claim(store: FakeStorage):
    """Only an enrolled, individually revocable host may dequeue work."""
    await store.conversation_commands.create_job(
        user_id=_OWNER,
        repo="owner/name",
        task_prompt="fix it",
        runtime="claude-code",
        model="glm-5.1",
    )

    app = _build_app(store, claim_auth=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/agent/worker/claim",
            json=connection_claim({"worker_id": "w1"}),
            headers={"Authorization": "Bearer pool-wide-secret"},
        )
    assert response.status_code == 401
    assert next(iter(store.jobs.values()))["state"] == "queued"


async def test_worker_cannot_inject_sse_frames_via_event_type(
    client: AsyncClient, store: FakeStorage
):
    """A newline in event_type must not break out of the SSE event field.

    Regression: event_type was interpolated raw into "event: {type}", so a
    worker could append a crafted type and inject arbitrary frames — including
    a fake job_finished — into the owner's live stream.
    """
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    rejected = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json={"event_type": "message\nevent: job_finished\ndata: {}\n\nx", "payload": {}},
        headers=auth,
    )
    assert rejected.status_code == 422

    # Belt and braces: a row that somehow carries a bad type still renders safely.
    store.events.append(
        {
            "id": 999,
            "job_id": job_id,
            "attempt_id": 100,
            "seq": 1,
            "event_type": "evil\nevent: job_finished\ndata: {}\n",
            "payload": {},
            "created_at": None,
        }
    )
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "succeeded"}, headers=auth
    )
    async with client.stream("GET", f"/v1/agent/jobs/{job_id}/stream") as response:
        body = "".join([chunk async for chunk in response.aiter_text()])
    assert "event: evil" not in body
    assert "event: malformed" in body
    # The payload still *contains* the crafted text, but only JSON-escaped
    # inside a data field — it never starts a frame. Count frame boundaries,
    # not substrings: exactly one real job_finished frame was emitted.
    assert body.count("\n\nevent: job_finished") == 1
    assert "\nevent: job_finished" not in body.split("data: ", 1)[1].split("\n\n", 1)[0]


async def test_lease_ttl_is_capped_server_side(client: AsyncClient):
    """A worker cannot pick a lease long enough to outlive the reaper.

    Regression: lease_ttl_seconds had no upper bound, so a worker could claim
    with a decade-long lease. The reaper would never reclaim the job, making
    the attempt's capability token neither self-revoking nor cancellable.
    """
    await _create_job(client)
    response = await client.post(
        "/v1/agent/worker/claim",
        json=connection_claim({"worker_id": "w1", "lease_ttl_seconds": 99_999_999}),
    )
    assert response.status_code == 422


async def test_terminal_stream_drains_beyond_one_page(client: AsyncClient, store: FakeStorage):
    """A finished job with multiple pages of backlog delivers every event.

    Regression: the terminal path drained exactly one extra page before
    emitting job_finished, so a client attaching to a finished job with a
    large backlog silently lost everything past the second page.
    """
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    # More than two pages (page size is 500).
    total = 1100
    for index in range(total):
        await store.attempt_commands.append_event(
            attempt_id=100,
            lease_generation=1,
            event_type="message",
            payload={"index": index},
        )
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "succeeded"}, headers=auth
    )

    async with client.stream("GET", f"/v1/agent/jobs/{job_id}/stream") as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert body.count("event: message\n") == total
    assert f'"index":{total - 1}' in body
    assert "event: job_finished" in body
    tail = json.loads(body.split("event: job_finished\ndata: ")[1].split("\n")[0])
    assert tail["last_event_id"] == total


# ``test_claim_returns_a_separate_model_scoped_token`` was here. It asserted
# that the sandbox credential parsed as a ``SCOPE_MODEL`` worker token — which
# is precisely the credential the claim stopped minting, because it was signed
# with a root the gateway does not know and therefore bought nothing. The
# (the sandbox gets a *different*, weaker credential than the runner) is pinned
# below against the credential that replaced it.
#
# The case below is its in-place replacement. The full record — the original's
# four assertions, which three of them the replacement keeps verbatim, and why
# "does not parse at all" is stronger than "parses with a narrow scope" — is
# the extraction record § Discarded tests → "`test_agent_jobs_api.py` — two cases E9
# replaced in place (E7/E8/E9)", case 1. Ratified 2026-08-04.


async def test_the_claim_hands_over_the_owners_connection_and_no_sandbox_grant(
    client: AsyncClient, store: FakeStorage
):
    """The runner gets its control token and the turn's connection; there is no grant.

    The runner's control token parses in this service's own namespace; the
    sandbox's CLI runs on the owner's provider key from the connection.
    """
    await _create_job(client)
    body = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    assert "sandbox_token" not in body
    assert body["model_connection"]["connection_id"] == CONNECTION_ID

    from cloud_agent.tokens import SCOPE_FULL, parse_worker_token

    assert parse_worker_token(body["worker_token"])["scope"] == SCOPE_FULL


async def test_a_claim_from_a_host_that_cannot_run_connections_is_handed_back(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    refused = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert refused.status_code == 503
    assert store.jobs[job_id]["state"] == "queued"


# ``test_sandbox_token_cannot_write_job_state`` was here, asserting 403
# ``insufficient_scope`` on every worker endpoint. The refusal is now a 401:
# the sandbox holds a gateway grant, which is not a credential in this
# service's namespace at all, so it does not reach the scope check — it fails
# to parse. Same guarantee, one step earlier and one status code over.
#
# The case below is its in-place replacement: same four endpoints, same
# positive control, line-for-line identical otherwise. The full record — and
# the note that ``_worker_claims``' 403 scope branch lost its only test with
# the original — is the extraction record § Discarded tests → "`test_agent_jobs_api.py`
# — two cases E9 replaced in place (E7/E8/E9)", case 2. Ratified 2026-08-04.


async def test_the_sandbox_credential_cannot_write_job_state(client: AsyncClient):
    """A credential leaked from inside the sandbox cannot poison the job.

    This is the payoff of running the runner outside the sandbox: the only
    worker-namespace credential the agent can reach (``mcp`` scope) opens the
    MCP relay, not event-log writes, terminal transitions, or lease renewal.
    """
    job_id = await _create_job(client)
    body = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    sandbox_auth = {"Authorization": f"Bearer {body['mcp_token']}"}

    for path, payload in (
        (f"/v1/agent/worker/jobs/{job_id}/events", {"event_type": "message"}),
        (f"/v1/agent/worker/jobs/{job_id}/finish", {"state": "succeeded"}),
        (f"/v1/agent/worker/jobs/{job_id}/heartbeat", {}),
    ):
        response = await client.post(path, json=payload, headers=sandbox_auth)
        assert response.status_code == 403, path
        assert response.json()["detail"]["error"]["type"] == "insufficient_scope"

    # The runner's own token still works.
    runner_auth = {"Authorization": f"Bearer {body['worker_token']}"}
    ok = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json={"event_type": "message", "payload": {}},
        headers=runner_auth,
    )
    assert ok.status_code == 201


# ``test_sandbox_token_is_refused_on_owner_routes`` was here. It asserted only
# on ``serving.servers.auth._is_inference_path`` — a gateway function
# classifying gateway inference routes, with no call into the router at all.
# This service serves no inference route, so there is nothing to port it
# against and reproducing the predicate here would be inventing production
# code inside a migration. The wire-level half of the same guarantee is
# ``test_sandbox_token_stays_out_of_owner_routes`` (ported; 403
# ``insufficient_scope`` on ``/v1/agent/jobs``) and, for the worker surface,
# ``test_the_sandbox_grant_cannot_write_job_state`` (the in-place replacement
# for ``test_sandbox_token_cannot_write_job_state``; 401 against the gateway
# grant the sandbox now carries). Recorded in the extraction record's discarded-test
# register.


async def test_event_type_guard_rejects_a_trailing_newline(client: AsyncClient):
    """`match()` with `$` accepted "message\\n"; the guard must use fullmatch."""
    from cloud_agent.api._job_views import _SAFE_EVENT_TYPE

    assert _SAFE_EVENT_TYPE.fullmatch("message") is not None
    assert _SAFE_EVENT_TYPE.fullmatch("message\n") is None


async def test_the_claim_carries_a_read_only_clone_credential(store: FakeStorage):
    """Checkout stays read-only; sandbox publication uses a separate scoped token."""
    from cloud_agent.api.deps import get_agent_app_credentials

    asked: list[dict[str, Any]] = []

    class FakeApp:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            asked.append({"repo": repo, **kwargs})
            return (
                "ghs_clone_only"
                if kwargs["permissions"] == {"contents": "read"}
                else "sandbox-write-token"
            )

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: FakeApp()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        claim = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )

    assert claim.json()["clone_token"] == "ghs_clone_only"
    assert asked[0]["permissions"] == {"contents": "read"}
    assert asked[0]["repository_scoped"] is True
    assert claim.json()["github_token"] == "sandbox-write-token"
    assert asked[1] == {
        "repo": claim.json()["repo"],
        "permissions": {"contents": "write", "pull_requests": "write"},
        "repository_scoped": True,
        "holder": _thread_of(store, job_id),
    }
    assert len(asked) == 2


async def test_a_sandbox_never_holds_the_publisher_s_or_another_conversation_s_token(
    store: FakeStorage, monkeypatch
):
    """Whoever holds an installation token can revoke it, with no permission at all.

    The claim's write token came out of the publisher's own cache slot, so code
    in one sandbox could revoke the token publishing to its repository, and
    every other conversation's there, while the cache went on serving it.
    """
    import httpx

    from cloud_agent import github_app
    from cloud_agent.api.deps import get_agent_app_credentials
    from cloud_agent.publish_worker import _PUBLISH_SCOPE, _PUBLISH_TOKEN_MIN_TTL_S

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/owner/name/installation":
            return httpx.Response(200, json={"id": 1})
        if request.url.path == "/app/installations/1/access_tokens":
            token = f"ghs_{len(minted) + 1}"
            minted.append(token)
            return httpx.Response(201, json={"token": token, "expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(404, json={})

    minted: list[str] = []
    monkeypatch.setattr(github_app, "build_app_jwt", lambda config, **kwargs: "app-jwt")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as github:
        credentials = github_app.GitHubAppCredentials(
            github_app.AppConfig(app_id="1", private_key="unused", api_base="https://api.test"),
            client=github,
        )
        app = _build_app(store)
        app.dependency_overrides[get_agent_app_credentials] = lambda: credentials
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _create_job(client)
            await _create_job(client)
            first = (
                await client.post(
                    "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
                )
            ).json()
            second = (
                await client.post(
                    "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w2"})
                )
            ).json()
        publisher = await credentials.token_for(
            "owner/name",
            permissions=_PUBLISH_SCOPE,
            repository_scoped=True,
            min_ttl_s=_PUBLISH_TOKEN_MIN_TTL_S,
        )

    assert first["thread_id"] != second["thread_id"]
    assert len({first["github_token"], second["github_token"], publisher}) == 3
    # The read-only clone token is still shared: it never enters a sandbox.
    assert first["clone_token"] == second["clone_token"]


async def test_a_deployment_without_a_github_app_still_claims_jobs(client: AsyncClient):
    """No App configured is a working setup: a public repository needs no token."""
    await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))

    assert claim.status_code == 200
    assert claim.json()["clone_token"] is None
    assert claim.json()["github_token"] is None


async def test_an_uninstalled_app_hands_the_job_over_without_a_credential(
    store: FakeStorage,
):
    """A settled "not installed here" must not block the job.

    A public repository clones anonymously, and a private one fails with a
    message the owner can act on — which retrying would not improve.
    """
    from cloud_agent.api.deps import get_agent_app_credentials
    from cloud_agent.github_app import AppNotInstalled

    class UninstalledApp:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            raise AppNotInstalled(f"no App installation covers {repo}", status=404)

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: UninstalledApp()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        claim = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )

    assert claim.status_code == 200
    assert claim.json()["job_id"] == job_id
    assert claim.json()["clone_token"] is None
    assert claim.json()["github_token"] is None


@pytest.mark.parametrize(
    "failure_scope", [{"contents": "read"}, {"contents": "write", "pull_requests": "write"}]
)
async def test_a_transient_credential_error_is_retryable_not_terminal(
    store: FakeStorage,
    failure_scope: dict[str, str],
):
    """GitHub having a bad minute must not permanently fail someone's job.

    Handing the runner a null token here sends it off to clone anonymously,
    fail on a private repository, and mark the job *terminally* failed.
    Abandoning the attempt instead is the retryable path: the lease expires
    unheartbeated and the reaper requeues it.
    """
    from cloud_agent.api.deps import get_agent_app_credentials

    class FlakyApp:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            if kwargs["permissions"] == failure_scope:
                raise TimeoutError("api.github.com timed out")
            return "host-clone-token"

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: FlakyApp()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _create_job(client)
        claim = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )

    assert claim.status_code == 503, "a null token here would terminally fail the job"
    # And the claim is handed back, not merely left to lapse. A lapsed lease
    # still spends a retry, so an outage lasting three claim cycles would fail
    # the job outright — the outcome this path exists to avoid, only slower.
    assert store.released, "the attempt must be returned to the queue"
    assert store.jobs[store.released[0][0]]["state"] == "queued"


async def test_the_worker_reports_the_commit_it_actually_worked_from(
    client: AsyncClient, store: FakeStorage
):
    """A job submitted without a base_sha is unpublishable until one is recorded.

    The runner resolves the default branch when it checks the repository out,
    so it is the only component that knows — and the publisher refuses to apply
    a patch without a base.
    """
    job_id = await _create_job(client)
    assert store.jobs[job_id].get("base_sha") in (None, "")

    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    resolved = "a" * 40

    finish = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish",
        json={"state": "succeeded", "base_sha": resolved},
        headers=auth,
    )

    assert finish.status_code == 200
    assert store.jobs[job_id]["base_sha"] == resolved


async def test_a_worker_cannot_overwrite_a_base_the_owner_pinned(
    client: AsyncClient, store: FakeStorage
):
    """An owner who names a commit must get a patch against that commit."""
    pinned = "b" * 40
    created = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {
                "repo": "o/n",
                "task_prompt": "do the thing",
                "model": "m",
                "base_sha": pinned,
            }
        ),
    )
    job_id = created.json()["id"]

    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish",
        json={"state": "succeeded", "base_sha": "c" * 40},
        headers=auth,
    )

    assert store.jobs[job_id]["base_sha"] == pinned


async def test_an_unentitled_repo_is_refused_at_creation(client: AsyncClient):
    """The requester picks the repo; the platform supplies the GitHub authority.

    Without this check the two combine into a confused deputy: name any
    repository the App can reach, and read it back through your own job's
    events and retained workspace browser.
    """
    response = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {"repo": "someone-else/private", "task_prompt": "exfiltrate", "model": "m"}
        ),
    )

    assert response.status_code == 403
    assert "repo_not_allowed" in response.text


async def test_a_legacy_row_gets_no_credential_at_claim(store: FakeStorage):
    """A row written before the check existed must not yield a token now."""
    from cloud_agent.api.deps import get_agent_app_credentials

    minted: list[str] = []

    class FakeApp:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            minted.append(repo)
            return "ghs_should_not_happen"

    # Straight into the store, bypassing the API — the shape a pre-existing row has.
    await store.conversation_commands.create_job(
        user_id=_OWNER,
        repo="someone-else/private",
        task_prompt="legacy",
        runtime="claude-code",
        model="m",
    )

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: FakeApp()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        claim = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )

    assert claim.status_code == 403
    assert minted == [], "no credential may be minted for an unentitled repo"
    # A definitive refusal ends the oldest job so it cannot monopolize claims.
    assert store.released == []
    assert next(iter(store.jobs.values()))["state"] == "failed"


async def test_a_definitively_unentitled_oldest_job_does_not_block_the_next_job(store: FakeStorage):
    denied = await store.conversation_commands.create_job(
        user_id=_OWNER,
        repo="someone-else/private",
        task_prompt="oldest",
        runtime="claude-code",
        model="m",
    )
    ready = await store.conversation_commands.create_job(
        user_id=_OWNER,
        repo="o/n",
        task_prompt="ready",
        runtime="claude-code",
        model="m",
    )
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://test"
    ) as client:
        refused = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )
        accepted = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )
    assert refused.status_code == 403
    assert store.jobs[denied["id"]]["state"] == "failed"
    assert accepted.status_code == 200
    assert accepted.json()["job_id"] == ready["id"]


async def test_spend_and_usage_come_from_the_grant_ledger_not_the_agent(store: FakeStorage):
    """A job's numbers must not depend on what the agent says about itself.

    Models do misreport their own usage — this session watched one claim it had
    edited a file it never touched — so the owner-facing totals read the grant
    ledger, the same accounting the usage and admin surfaces use (#823).
    """

    from tests.unit.test_ledger_snapshots import END, ledger_row

    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        await client.post(f"/v1/agent/jobs/{job_id}/cancel")
        # One attempt's snapshot with known totals; a second, newer one for
        # the retry that superseded it — summed, not replaced.
        store.ledger_rows = [
            {**ledger_row(), "job_id": job_id},
            {
                **ledger_row(
                    id=2,
                    attempt_id=11,
                    attempt_no=2,
                    queried_at=END + timedelta(seconds=30),
                ),
                "job_id": job_id,
            },
        ]
        got = await client.get(f"/v1/agent/jobs/{job_id}")

    body = got.json()
    assert body["tokens_in"] == 240
    assert body["tokens_out"] == 40
    assert body["model_calls"] == 2
    assert body["spent_usd"] == float("0.00024690")


async def test_a_job_with_no_ledger_snapshot_reports_absent_not_zero(store: FakeStorage):
    """A snapshot that has not answered must not look like a job that spent nothing.

    The ledger rows exist only once the usage collector has stored a snapshot
    for the job's grants. Before that, ``summarize_job_ledger`` has no totals,
    and the response's fields stay ``None`` — the same absent-not-zero rule
    the old log-store path kept, now against the real source.
    """
    store.ledger_rows = []
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        got = await client.get(f"/v1/agent/jobs/{job_id}")

    body = got.json()
    assert body["spent_usd"] is None
    assert body["tokens_in"] is None
    assert body["tokens_out"] is None
    assert body["model_calls"] is None


async def test_a_job_reports_no_network_mode(client: AsyncClient):
    """Every task runs on the same sandbox network, so there is no mode to show.

    A per-job egress field would imply a choice that no longer exists; the
    response and the deployment config both leave it out.
    """
    job_id = await _create_job(client)

    body = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    config = (await client.get("/v1/agent/config")).json()

    for payload in (body, config):
        assert "setup_egress_tier" not in payload
        assert "agent_egress_tier" not in payload


async def test_a_worker_cannot_write_the_publish_record(client: AsyncClient):
    """Publishing is the platform's, not the sandbox runner's.

    These endpoints had no production caller — the runner leaves publishing to
    the platform and the publisher uses its own claim path — while letting a
    worker token write `published_pr_url` from an arbitrary string. That marked
    the job succeeded, showed the owner that URL, and made the real publisher
    skip the job forever (it only claims rows where the URL is null), so the
    patch was never gated or pushed.
    """
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    for path, body in (
        (f"/v1/agent/worker/jobs/{job_id}/publish/begin", None),
        (f"/v1/agent/worker/jobs/{job_id}/publish/complete", {"pr_url": "https://evil/pr/1"}),
    ):
        response = await client.post(path, json=body, headers=auth)
        assert response.status_code == 404, f"{path} must not exist"
