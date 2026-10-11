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
factory with their in-memory broker; there is no global broker fallback.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.storage import AllCapabilities, EmptyAttachments, EmptySkillLibrary

from cloud_agent import broker_routing
from cloud_agent.api import routes as agent_jobs_router, worker, workspace as agent_workspace
from cloud_agent.api._owner_auth import authenticate_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.application.follow_ups import PreparedFollowUp
from cloud_agent.domain.execution import JobStreamState
from cloud_agent.domain.pending_messages import SettleOutcome
from cloud_agent.goals.models import GoalConflict
from cloud_agent.mcp.registry import mcp_cipher
from cloud_agent.storage.repositories.goals import _pause_detail
from cloud_agent.tokens import mint_worker_token

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

    async def claim_wakeups(self, *, limit: int = 1) -> list[dict[str, Any]]:
        """Nothing is consumed here; the decision's own tests own that path."""
        return []

    async def pending_wakeup(self, *, thread_id: str) -> dict[str, Any] | None:
        for row in self.rows:
            if row["thread_id"] == thread_id and row["state"] == "pending":
                return dict(row)
        return None

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

    async def get_goal_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        job = self._store.jobs.get(job_id)
        return None if job is None else await self.get_goal(thread_id=str(job["thread_id"]))

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

    async def clear_goal(self, *, thread_id: str, user_id: str) -> bool:
        if not self._owned(thread_id, user_id) or self._goal(thread_id) is None:
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


class FakeStorage(EmptySkillLibrary, EmptyAttachments, AllCapabilities):
    """In-memory stand-in exposing the storage surface the router uses."""

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
        self.goal_wakeups = FakeGoalWakeups(self)
        self.goals = FakeGoals(self)
        self.turn_telemetry: dict[str, dict[str, Any]] = {}
        self.turn_summaries: dict[int, dict[str, Any]] = {}
        self.attempt_telemetry: dict[str, list[dict[str, Any]]] = {}
        self.attempt_jobs: dict[int, str] = {}
        self.live_fence: tuple[int, int] | None = None
        self.attempt_grants: dict[int, str] = {}
        self.terminal_readiness: dict[int, bool] = {}
        self.thread_workspace_readiness: dict[str, bool] = {}
        self.sandbox_admissions: list[dict[str, Any]] = []
        self.released: list[tuple[str, int]] = []
        self.runner_hosts: dict[str, dict[str, Any]] = {}
        self.active_host: str | None = None
        self.mcp_connections: dict[int, dict[str, Any]] = {}
        self.mcp_capabilities: dict[int, dict[str, Any]] = {}
        self.mcp_bindings: dict[str, list[dict[str, Any]]] = {}
        self.session_forks: dict[str, dict[str, Any]] = {}
        #: The owner's waiting messages, and each thread's two queue facts, the
        #: way ``agent_pending_messages`` and ``agent_threads`` hold them.
        self.pending: dict[str, dict[str, Any]] = {}
        self.queue_paused: dict[str, str] = {}
        self.queue_settled: dict[str, str] = {}
        self._next_pending = 1
        self._next_connection = 1
        self._next_event_id = 1
        self._next_job = 1

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
            deleted_at=datetime.now(timezone.utc),
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

    async def get_gitlab_connection(self, *, user_id: str) -> dict[str, Any] | None:
        """No GitLab account connected, for the same reason as the grants above."""
        del user_id
        return None

    async def list_repo_grants(self, *, user_id: str) -> list[dict[str, Any]]:
        """No GitHub App installations in these tests, which is the usual state.

        The aggregate integrations endpoint reads them, so the fake has to
        answer: the cases here are about MCP connections, and an absent method
        would fail them for a reason that has nothing to do with what they
        check.
        """
        del user_id
        return []

    async def get_github_account(self, *, user_id: str) -> dict[str, Any] | None:
        """No GitHub account connected either, for the same reason."""
        del user_id
        return None

    # -- owner surface --
    async def create_job(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.pop("attachments", None)
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
        goal: Any | None = None,
        attachments: list[dict[str, Any]] | None = None,
        turn_source: str = "human",
        goal_round: int | None = None,
        goal_round_kind: str | None = None,
        goal_revision: int | None = None,
        yield_to_queued: bool = False,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        del attachments  # staged uploads have their own suite; this fake stages none
        self.last_follow_up_request = (client_request_id, client_request_fingerprint)
        requested = self.jobs.get(parent_job_id)
        if goal is not None and requested is not None:
            await self.goals.set_goal(
                thread_id=str(requested["thread_id"]), user_id=user_id, goal=goal
            )
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
        job = {
            **parent,
            "id": job_id,
            "parent_job_id": parent["id"],
            "turn_no": turn_no,
            "base_sha": parent.get("published_commit_sha") or parent.get("base_sha"),
            "task_prompt": prompt,
            "skills": skills if skills is not None else parent.get("skills"),
            "runtime": runtime or parent["runtime"],
            "model": model or parent["model"],
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
            "turn_source": turn_source,
            "goal_round": goal_round,
            "goal_round_kind": goal_round_kind,
            "round_goal_revision": goal_revision,
            # A fresh turn has its own report columns, whatever its parent
            # ended on.
            "goal_report_parse": None,
            "goal_report_outcome": None,
            "goal_report_detail": None,
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

    # -- Pending messages --
    #
    # The queue the message route writes. A message that is sent becomes a turn
    # through ``create_follow_up`` above, so it becomes exactly the turn a
    # follow-up always made here, and the cases about a turn's fields keep
    # asserting on the same fake.

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
            made = await self.create_follow_up(
                parent_job_id=expected_latest_job_id,
                user_id=user_id,
                prompt=prompt,
                model=model,
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
        """Mirrors the store's settlement, sending through ``create_follow_up``."""
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
        job = await self.create_follow_up(
            parent_job_id=latest["id"],
            user_id=head["user_id"],
            prompt=head["prompt"],
            model=prepared.model,
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

    async def threads_to_settle(self, *, limit: int = 50) -> list[str]:
        return sorted({row["thread_id"] for row in self.pending.values()})[:limit]

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

    async def get_thread_for_job(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
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

    async def thread_stats_inputs(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        thread_id = job["thread_id"]
        thread_jobs = sorted(
            (item for item in self.jobs.values() if item["thread_id"] == thread_id),
            key=lambda item: item["turn_no"],
        )
        job_ids = [item["id"] for item in thread_jobs]
        return {
            "thread_id": thread_id,
            "job_ids": job_ids,
            # This stand-in stamps no clocks (``append_event`` stores
            # ``created_at: None``), so the timings come back zero here and
            # the arithmetic is pinned by ``tests/unit/test_thread_stats.py``
            # instead. What this fake is for is the HTTP contract: ownership,
            # shape, and the counts that need no clock.
            "attempts": [{"job_id": item["id"], "started_at": None} for item in thread_jobs],
            "events": [event for event in self.events if event["job_id"] in set(job_ids)],
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

    async def get_job_stream_state(self, *, job_id: str, user_id: str) -> JobStreamState | None:
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        return JobStreamState(job["state"], job.get("published_pr_url"))

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
        if job is None or job.get("user_id") != user_id:
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

    async def admit_thread_sandbox_turn(self, **kwargs: Any) -> bool:
        """Record the at-most-once admission RPC under the worker fence."""
        if not self._fenced(kwargs["attempt_id"], kwargs["lease_generation"]):
            return False
        self.sandbox_admissions.append(dict(kwargs))
        return True

    async def list_jobs(
        self,
        *,
        user_id: str,
        limit: int = 50,
        archived: bool = False,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        jobs = [
            {**job, "pinned_at": self.pinned_threads.get(job["thread_id"])}
            for job in self.jobs.values()
            if job["user_id"] == user_id
            and (job["thread_id"] in self.archived_threads) is archived
            and (repo is None or job["repo"] == repo)
        ]
        minimum = datetime.min.replace(tzinfo=timezone.utc)
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
        minimum = datetime.min.replace(tzinfo=timezone.utc)
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
            archived_at = self.archived_threads.setdefault(thread_id, datetime.now(timezone.utc))
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
            pinned_at = self.pinned_threads.setdefault(thread_id, datetime.now(timezone.utc))
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
        return {"host": "runner-test", "broker_url": "http://broker.test:8092"}

    async def host_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        return {"host": "runner-test", "broker_url": "http://broker.test:8092"}

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

    async def record_attempt_grant(
        self, *, attempt_id: int, lease_generation: int, grant_id: str
    ) -> bool:
        if self.live_fence != (attempt_id, lease_generation):
            return False
        self.attempt_grants[attempt_id] = grant_id
        return True

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
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "api-test-broker-secret")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "session-test-secret-32-bytes-minimum!")
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


class _FakeGateway:
    """The gateway's internal contracts, answered in-process.

    Replaces the source's ``_fake_route_exec``: the model catalog and the MCP
    registry are HTTP calls now, not registry objects the router holds.

    No MCP here: the registry moved to this service with its credentials and
    its relay, so a fake gateway that still offered one would suggest the
    gateway is still asked — which is the call this change removed.
    """

    async def model_catalog(self, user_id: str) -> list[str]:
        return list(_TEST_MODELS)

    #: Profile metadata remains separate from the explicit admission decision.
    status: ClassVar[dict[str, Any]] = {"exists": True, "active": True, "role": "admin"}

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        return {"user_id": user_id, "allowed": True, "permissions": ["agent.use"]}

    async def user_status(self, user_id: str) -> dict[str, Any]:
        return dict(self.status)

    aliases: ClassVar[dict[str, str]] = {}

    async def model_catalog_with_aliases(self, user_id: str) -> tuple[list[str], dict[str, str]]:
        """The catalog plus the gateway's alias translation table.

        No aliases here: these tests name canonical ids, and resolution has its
        own file. What matters is that the shape is the real one — a fake that
        answered only the older method sent every job-creation path down a
        `AttributeError` the moment the caller started asking for the table.
        """
        return list(_TEST_MODELS), dict(self.aliases)

    async def model_reasoning_efforts(self, user_id: str) -> dict[str, list[str]]:
        """Which models take a reasoning effort, and which values.

        Same reason the alias method exists here: the real client asks, so a
        fake that does not answer sends every composer and create path into an
        ``AttributeError`` rather than the behaviour under test.
        """
        return {model: list(values) for model, values in _TEST_MODEL_EFFORTS.items()}

    async def model_catalog_with_reasoning_efforts(
        self, user_id: str
    ) -> tuple[list[str], dict[str, list[str]]]:
        """The composer's model options, returned by one current catalog read."""
        return await self.model_catalog(user_id), await self.model_reasoning_efforts(user_id)


class _FakeGrants:
    """The gateway's grant contract, answered in-process.

    The claim asks for the sandbox's credential now instead of minting one, so
    an app with no grant client cannot claim at all — the same shape as the
    fake gateway above, and for the same reason.
    """

    def __init__(self) -> None:
        self.minted: list[dict[str, Any]] = []

    async def mint(self, **kwargs: Any) -> dict[str, Any]:
        self.minted.append(kwargs)
        return {
            "grant_id": f"agr_{len(self.minted)}",
            "token": f"agr.{kwargs['job_id']}.{kwargs['attempt_id']}",
            # The gateway returns the *effective* scope, and the claim refuses
            # a grant that does not cover the job's model. A fake omitting it
            # models a gateway that does not exist.
            "allowed_models": list(kwargs.get("models") or []),
        }

    async def renew(self, grant_id: str) -> dict[str, Any]:
        return {"grant_id": grant_id}

    async def revoke(self, grant_id: str) -> bool:
        return True


def _build_app(store, *, role: str = "admin", claim_auth: bool = True):
    """Build an app with the router mounted and auth stubbed to one identity.

    ``claim_auth`` controls whether an enrolled-host identity is supplied to
    ``/worker/claim``, so a test can assert that an ordinary caller is refused.
    """
    from cloud_agent.api.deps import (
        get_agent_app_credentials,
        get_gateway_client,
        get_grant_client,
        get_log_store,
    )

    app = FastAPI()
    app.include_router(agent_jobs_router.router)
    app.dependency_overrides[get_storage] = lambda: store
    # The create endpoints fail fast on models the deployment cannot serve, so
    # the gateway the test answers with must contain what the tests submit.
    app.dependency_overrides[get_gateway_client] = _FakeGateway
    # The sandbox credential comes from the gateway; without this the
    # claim would correctly refuse, having no configured grant client.
    app.dependency_overrides[get_grant_client] = _FakeGrants
    # No GitHub App by default — the configuration most deployments start in,
    # and the one where a public repository still clones fine.
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    # No log store: spend and usage are then absent from the response rather
    # than reported as a fabricated zero.
    app.dependency_overrides[get_log_store] = lambda: None
    identity = {"user_id": _OWNER, "role": role, "authenticated": True}
    # Override identity only; every request still exercises Gateway admission.
    app.dependency_overrides[authenticate_agent_owner] = lambda: identity
    if claim_auth:
        app.dependency_overrides[worker.verify_claiming_host] = lambda: {
            "host": "runner-test",
            "credential_hash": "test-credential-hash",
        }
    return app


def _browser_session(*, expires_delta=None) -> str:
    """Mint the browser credential this service issues.

    The gateway's ``create_access_token`` is ``identity.mint_session`` here —
    the same compact three-segment JWT with ``sub``/``email``/``exp``, signed
    with this service's own secret instead of the gateway's. ``mint_session``
    has no expiry parameter, so an expired one is the same claims re-signed.
    """
    import jwt

    from cloud_agent.identity import mint_session

    token = mint_session(user_id=_OWNER)
    if expires_delta is None:
        return token
    import os

    claims = jwt.decode(token, options={"verify_signature": False})
    claims["exp"] = int((datetime.now(timezone.utc) + expires_delta).timestamp())
    return jwt.encode(claims, os.environ["AGENT_SESSION_SECRET"], algorithm="HS256")


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
        json={"repo": "owner/name", "task_prompt": "fix it", "model": model},
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
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "metadata": {
                "origin": "api-test",
                "execution_profile": "trusted-v1",
                "sandbox_runtime": "runc",
                "guest_kernel": "forged",
            },
        },
    )

    assert created.status_code == 201
    assert created.json()["execution_profile"] == "untrusted-kata-v1"
    assert created.json()["metadata"] == {"origin": "api-test"}
    assert store.jobs[created.json()["id"]]["execution_profile"] == "untrusted-kata-v1"

    rejected = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "try to downgrade",
            "model": "glm-5.1",
            "execution_profile": "trusted-v1",
        },
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
        json={"repo": "o/n", "task_prompt": "a second project", "model": "glm-5.1"},
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
    job["updated_at"] = datetime(2026, 9, 10, tzinfo=timezone.utc)
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
            "/v1/agent/conversations", headers={"Authorization": f"Bearer {_browser_session()}"}
        )
    assert response.status_code == 200
    assert response.json() == {"conversations": [], "next_cursor": None}
    search.assert_awaited_once_with(
        user_id=_OWNER, q="", repo=None, include_archived=False, pinned=None, limit=50, cursor=None
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


async def test_thread_stats_answer_a_thread_that_never_ran(client: AsyncClient):
    """A queued thread has no events yet and still renders a bar."""
    job_id = await _create_job(client)

    body = (await client.get(f"/v1/agent/jobs/{job_id}/thread/stats")).json()

    assert body["turns"] == 1
    assert body["tool_calls"] == 0
    assert body["wall_seconds"] == 0.0
    assert body["ttft_seconds"] is None


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
    store.jobs[deleted_id]["deleted_at"] = datetime.now(timezone.utc)
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
    store.jobs[parent_id]["created_at"] = datetime(2026, 7, 1, tzinfo=timezone.utc)
    store.jobs[child_id]["created_at"] = datetime(2026, 7, 2, tzinfo=timezone.utc)
    store.jobs[newer_id]["created_at"] = datetime(2026, 7, 3, tzinfo=timezone.utc)
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
        client, store, parent_id, {"prompt": "now add a regression test", "model": "qwen-next"}
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

    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    assert claim["job_id"] == parent_id
    blocked = await client.post("/v1/agent/worker/claim", json={"worker_id": "w2"})
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

    child_claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w2"})).json()
    assert child_claim["job_id"] == child["id"]
    assert "context_patch" not in child_claim
    assert "context_messages" not in child_claim
    assert "context_checkpoint" not in child_claim


async def test_cancelled_middle_turn_does_not_release_a_later_queued_turn(
    client: AsyncClient, store: FakeStorage
):
    """Every unfinished earlier turn blocks a claim, even across a cancellation.

    Two turns queued behind a live one cannot come from the message route any
    more — it holds later messages in the queue — so they are appended through
    the store, which automatic rounds still do.
    """
    parent_id = await _create_job(client)
    middle = await store.create_follow_up(
        parent_job_id=parent_id, user_id=_OWNER, prompt="skip this follow-up"
    )
    assert middle is not None
    middle_id = middle["id"]
    latest = await store.create_follow_up(
        parent_job_id=parent_id, user_id=_OWNER, prompt="run this after the first turn"
    )
    assert latest is not None
    latest_id = latest["id"]
    assert latest["parent_job_id"] == middle_id
    assert latest["state"] == "queued"

    cancelled = await client.post(f"/v1/agent/jobs/{middle_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    assert claim["job_id"] == parent_id
    blocked = await client.post("/v1/agent/worker/claim", json={"worker_id": "w2"})
    assert blocked.status_code == 200
    assert blocked.json() is None
    assert store.jobs[latest_id]["state"] == "queued"

    finished = await client.post(
        f"/v1/agent/worker/jobs/{parent_id}/finish",
        json={"state": "succeeded"},
        headers={"Authorization": f"Bearer {claim['worker_token']}"},
    )
    assert finished.status_code == 200
    next_claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w2"})).json()
    assert next_claim["job_id"] == latest_id


async def test_create_fails_fast_on_a_model_this_deployment_cannot_serve(client: AsyncClient):
    """An unknown model is a 400 at create, not a dead job after claim+clone."""
    response = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "fix it", "model": "gpt-nonexistent"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]["error"]
    assert detail["type"] == "model_not_available"
    # The rejection names what would work, so the caller can fix the request.
    assert "glm-5.1" in detail["message"]


async def test_follow_up_model_switch_gets_the_same_fail_fast(
    client: AsyncClient, store: FakeStorage
):
    """Switching models in a message is validated like a fresh job, before it waits."""
    parent_id = await _create_job(client)
    response = await _send(
        client, store, parent_id, {"prompt": "try another model", "model": "gpt-nonexistent"}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "model_not_available"
    assert store.pending == {}


async def test_an_unregistered_runtime_is_refused_at_create(client: AsyncClient):
    """An arbitrary runtime string dies here, not as a claimed-then-dead job."""
    response = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "runtime": "no-such-harness",
        },
    )
    assert response.status_code == 400
    assert "no-such-harness" in response.json()["detail"]["error"]["message"]


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


async def test_config_offers_the_models_the_create_endpoint_accepts(client: AsyncClient):
    """The picker and the validator must be the same list, or one is lying."""
    config = await client.get("/v1/agent/config")
    assert config.status_code == 200
    assert config.json()["models"] == list(_TEST_MODELS)
    # This fixture has no GitHub App, but its operator allowlists public repos.
    # The API accepts those jobs and anonymous clone works, so the composer
    # must not disable itself behind a narrower historical field name.
    assert "owner/name" in config.json()["repos"]
    assert config.json()["github_connected"] is True


async def test_config_reads_one_current_catalog_and_rechecks_admission(store: FakeStorage):
    """One catalog response supplies both fields, for each admitted owner read."""
    from httpx import MockTransport, Response

    from cloud_agent.api.deps import get_gateway_client
    from cloud_agent.gateway import GatewayClient

    catalog_owners: list[str] = []
    admitted_owners: list[str] = []

    def handler(request):
        assert request.headers["Authorization"] == "Bearer config-dispatch-token"
        if request.url.path.endswith("/agent-access"):
            owner = request.url.path.split("/")[-2]
            admitted_owners.append(owner)
            return Response(
                200, json={"user_id": owner, "allowed": True, "permissions": ["agent.use"]}
            )
        assert request.url.path == "/internal/model-catalog"
        owner = request.url.params["user_id"]
        catalog_owners.append(owner)
        model = f"{owner}-model-{len(catalog_owners)}"
        return Response(200, json={"models": [model], "reasoning_efforts": {model: ["high"]}})

    app = _build_app(store)
    identity = {"user_id": _OWNER}
    app.dependency_overrides[authenticate_agent_owner] = lambda: identity
    async with AsyncClient(transport=MockTransport(handler)) as upstream:
        gateway = GatewayClient("https://gateway.example", "config-dispatch-token", client=upstream)
        app.dependency_overrides[get_gateway_client] = lambda: gateway
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for index, owner in enumerate((_OWNER, _OWNER, _OTHER), start=1):
                identity["user_id"] = owner
                response = await client.get("/v1/agent/config")
                assert response.status_code == 200, response.text
                model = f"{owner}-model-{index}"
                assert response.json()["models"] == [model]
                assert response.json()["model_reasoning_efforts"] == {model: ["high"]}
                assert len(catalog_owners) == index
                assert len(admitted_owners) == index
    assert catalog_owners == admitted_owners == [_OWNER, _OWNER, _OTHER]


@pytest.mark.parametrize(
    "payload,status",
    [({}, 200), ({"models": "all"}, 200), ({"models": ["unavailable"]}, 503)],
)
async def test_config_catalog_failure_offers_no_models_or_efforts(
    store: FakeStorage, payload, status
):
    """Catalog failure cannot widen options or discard the remaining config."""
    from httpx import MockTransport, Response

    from cloud_agent.api.deps import get_gateway_client
    from cloud_agent.gateway import GatewayClient

    def handler(request):
        if request.url.path.endswith("/agent-access"):
            return Response(
                200, json={"user_id": _OWNER, "allowed": True, "permissions": ["agent.use"]}
            )
        return Response(status, json=payload)

    app = _build_app(store)
    async with AsyncClient(transport=MockTransport(handler)) as upstream:
        gateway = GatewayClient("https://gateway.example", "config-dispatch-token", client=upstream)
        app.dependency_overrides[get_gateway_client] = lambda: gateway
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/agent/config")
    assert response.status_code == 200, response.text
    assert response.json()["models"] == []
    assert response.json()["model_reasoning_efforts"] == {}
    assert response.json()["repos"]
    assert response.json()["runtimes"]


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
        if len(started) == 3:
            all_started.set()
        await release.wait()

    async def repositories(user_id, **kwargs):
        nonlocal repositories_read
        assert user_id == _OWNER
        await wait_for_sources("repositories")
        repositories_read = True
        return ["owner/name"]

    async def model_options(self, user_id):
        assert user_id == _OWNER
        await wait_for_sources("models")
        return ["glm-5.1"], {"glm-5.1": ["high"]}

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

    monkeypatch.setattr(config_api, "repos_for_user", repositories)
    monkeypatch.setattr(_FakeGateway, "model_catalog_with_reasoning_efforts", model_options)
    monkeypatch.setattr(store, "list_mcp_connections", connections)
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
    assert response.json()["models"] == ["glm-5.1"]
    assert response.json()["model_reasoning_efforts"] == {"glm-5.1": ["high"]}
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
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
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

    async def _lost_during_create(**_kwargs):
        return {"error": "sandbox_context_lost"}

    monkeypatch.setattr(store, "create_follow_up", _lost_during_create)
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

    class Broker:
        async def __getattr__(self, _name: str):
            raise AssertionError("legacy fork must be rejected before a host mutation")

    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: Broker())
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

    class Broker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def fork_shared_session(self, source_thread_id: str, **kwargs: Any):
            self.calls.append((source_thread_id, kwargs))
            assert kwargs["expected_container_id"] == "container-shared"
            assert kwargs["disk_limit_mib"] == 16384
            assert kwargs["origin_thread_id"] == source_thread_id
            assert kwargs["source_native_session_id"] == "native-session-source"
            assert kwargs["source_native_turn_no"] == 1
            assert kwargs["target_thread_id"] != source_thread_id
            assert kwargs["source_launch"]["plugins"] == plugins
            assert kwargs["target_launch"]["plugins"] == plugins
            return {
                "operation_id": kwargs["operation_id"],
                "sandbox_id": kwargs["sandbox_id"],
                "sandbox_generation": kwargs["generation"],
                "container_id": kwargs["expected_container_id"],
                "runtime_id": kwargs["runtime_id"],
                "runtime_version": kwargs["runtime_version"],
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

    broker = Broker()
    plugins = (
        [{"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}] if with_plugins else []
    )
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
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
    assert len(broker.calls) == 1


async def test_shared_chat_can_fork_from_a_specific_historical_turn(
    store: FakeStorage, monkeypatch
):
    """The host receives the selected prefix, not an implicit latest fork."""

    class Broker:
        def __init__(self) -> None:
            self.native_turns: list[int] = []

        async def fork_shared_session(self, source_thread_id: str, **kwargs: Any):
            self.native_turns.append(kwargs["source_native_turn_no"])
            return {
                "operation_id": kwargs["operation_id"],
                "sandbox_id": kwargs["sandbox_id"],
                "sandbox_generation": kwargs["generation"],
                "container_id": kwargs["expected_container_id"],
                "runtime_id": kwargs["runtime_id"],
                "runtime_version": kwargs["runtime_version"],
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

    broker = Broker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
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
    assert broker.native_turns == [1]
    assert second["thread_id"] == first["thread_id"]


async def test_same_native_fork_request_has_one_external_coordinator(
    store: FakeStorage, monkeypatch
):
    """Two API workers must not rotate residencies or fork the CLI twice."""

    started = asyncio.Event()
    finish = asyncio.Event()

    class Broker:
        calls = 0

        async def fork_shared_session(self, source_thread_id: str, **kwargs: Any):
            self.calls += 1
            started.set()
            await finish.wait()
            return {
                "operation_id": kwargs["operation_id"],
                "sandbox_id": kwargs["sandbox_id"],
                "sandbox_generation": kwargs["generation"],
                "container_id": kwargs["expected_container_id"],
                "runtime_id": kwargs["runtime_id"],
                "runtime_version": kwargs["runtime_version"],
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

    broker = Broker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
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
    assert broker.calls == 1


async def test_ready_native_fork_retry_does_not_need_the_host(store: FakeStorage, monkeypatch):
    """A lost success response remains idempotent after the host disappears."""

    class Broker:
        calls = 0

        async def fork_shared_session(self, source_thread_id: str, **kwargs: Any):
            self.calls += 1
            return {
                "operation_id": kwargs["operation_id"],
                "sandbox_id": kwargs["sandbox_id"],
                "sandbox_generation": kwargs["generation"],
                "container_id": kwargs["expected_container_id"],
                "runtime_id": kwargs["runtime_id"],
                "runtime_version": kwargs["runtime_version"],
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

    broker = Broker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
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
        monkeypatch.setattr(broker_routing, "_client_for", lambda _host: None)
        retried = await client.post(path, headers=headers)

    assert first.status_code == 201
    assert retried.status_code == 201
    assert retried.json()["id"] == first.json()["id"]
    assert broker.calls == 1


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
    token = _browser_session()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as jwt_client:
        response = await jwt_client.get(
            "/v1/agent/jobs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"jobs": []}


async def test_expired_browser_jwt_is_not_reinterpreted_as_api_key(store: FakeStorage):
    """Credential dispatch must preserve a JWT's own expiry failure."""
    from datetime import timedelta

    app = _build_app(store)
    app.dependency_overrides.pop(authenticate_agent_owner)
    token = _browser_session(expires_delta=timedelta(seconds=-1))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as jwt_client:
        response = await jwt_client.get(
            "/v1/agent/jobs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json()["detail"].startswith("Token has expired")


# ``test_normal_api_key_still_authenticates_agent_owner`` stood here. It minted
# an ``hyi-*`` key against a faked operational store and asserted 200 plus
# ``update_key_last_used``. The API-key branch was deleted on 2026-08-04 — it
# had no producer here and never will — so the case has no subject left and
# passed only because it injected the store production never has. Recorded in
# MIGRATION.md's "Discarded tests" register, which also says where the property
# lives now: the gateway, which still authenticates API keys for its own
# surfaces.


async def test_sandbox_token_stays_out_of_owner_routes(store: FakeStorage):
    """A structured Agent token must not be mistaken for a browser JWT."""
    from cloud_agent.tokens import SCOPE_MODEL

    app = _build_app(store)
    app.dependency_overrides.pop(authenticate_agent_owner)
    token = mint_worker_token(
        job_id="ajob_0001",
        attempt_id=1,
        lease_generation=1,
        scope=SCOPE_MODEL,
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
    ):
        response = await client.get(path)
        assert response.status_code == 404, path

    write = await client.put(
        f"/v1/agent/jobs/{foreign['id']}/files",
        params={"path": "README.md"},
        json={"content": "forbidden"},
    )
    terminal = await client.post(
        f"/v1/agent/jobs/{foreign['id']}/terminal",
        json={"command": "pwd", "cwd": "/workspace"},
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
    assert terminal.status_code == 404
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


async def test_live_workspace_routes_use_the_job_broker(store: FakeStorage, monkeypatch):
    """Files, terminal and Git address the same retained chat workspace."""

    class Broker:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

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

        async def terminal(self, workspace_id: str, **kwargs):
            self.calls.append(("terminal", workspace_id, kwargs["command"], kwargs["cwd"]))
            return {"output": "ok\n", "stderr": "", "exit_code": 0, "cwd": "/workspace"}

        async def git(self, workspace_id: str, base_sha: str | None = None):
            self.calls.append(("git", workspace_id, base_sha))
            return {
                "available": True,
                "branch": "agent/thread",
                "changes": [{"code": " M", "path": "README.md"}],
                "patch": "diff --git a/README.md b/README.md\n",
                "commits": [],
            }

    broker = Broker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
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
        terminal = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminal",
            json={"command": "pwd", "cwd": "/workspace"},
        )
        git = await local_client.get(f"/v1/agent/jobs/{job_id}/git")

    assert listing.status_code == 200
    assert listing.json()["writable"] is True
    assert listing.json()["source"] == "workspace"
    assert saved.status_code == 200
    assert terminal.json()["output"] == "ok\n"
    assert git.json()["branch"] == "agent/thread"
    assert broker.calls == [
        ("files", workspace_id, ""),
        ("write", workspace_id, "README.md", "updated"),
        ("terminal", workspace_id, "pwd", "/workspace"),
        ("git", workspace_id, store.jobs[job_id].get("base_sha")),
    ]


async def test_binary_workspace_payload_preserves_owner_and_path_checks(
    store: FakeStorage, monkeypatch
):
    """Binary bytes use the same owned, normalized route as text previews."""

    class Broker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

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

    broker = Broker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
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
    assert broker.calls == [(workspace_id, "artifact.png")]


async def test_missing_shared_workspace_is_a_conflict_not_a_missing_file(
    store: FakeStorage, monkeypatch
):
    """The broker's shared-only 404 describes the workspace, not a file path."""
    from cloud_agent.workspace_broker_client import WorkspaceBrokerError

    class Broker:
        async def register_shared_sandbox(self, _sandbox: dict[str, Any]):
            return None

        async def files(self, _workspace_id: str, _path: str, *, profiling=None):
            raise WorkspaceBrokerError(404, "shared chat workspace is not materialized")

    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: Broker())
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
    """Broker restart cannot bypass a protected latest-turn workspace phase."""

    class Broker:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        async def register_shared_sandbox(self, _sandbox: dict[str, Any]):
            return None

        async def write_file(self, workspace_id: str, path: str, content: str):
            self.calls.append(("write", workspace_id, path, content))
            raise AssertionError("durably fenced write reached the broker")

        async def terminal(self, workspace_id: str, **kwargs):
            self.calls.append(("terminal", workspace_id, kwargs["command"]))
            raise AssertionError("durably fenced command reached the broker")

        async def terminal_input(self, workspace_id: str, terminal_id: str, *, data: str):
            self.calls.append(("input", workspace_id, terminal_id, data))
            raise AssertionError("durably fenced input reached the broker")

        async def resize_terminal(
            self, workspace_id: str, terminal_id: str, *, rows: int, cols: int
        ):
            self.calls.append(("resize", workspace_id, terminal_id, rows, cols))
            raise AssertionError("durably fenced resize reached the broker")

        async def delete_terminal(self, workspace_id: str, terminal_id: str):
            self.calls.append(("delete", workspace_id, terminal_id))
            return _terminal_session_descriptor(state="closed")

    broker = Broker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
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
        command = await client.post(
            f"/v1/agent/jobs/{job_id}/terminal",
            json={"command": "touch blocked", "cwd": "/workspace"},
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

    assert {saved.status_code, command.status_code, wrote.status_code, resized.status_code} == {409}
    for response in (saved, command, wrote, resized):
        assert response.json()["detail"]["error"]["type"] == "workspace_not_ready"
    assert killed.status_code == 200
    assert broker.calls == [("delete", job["thread_id"], "term_1")]


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


class _TerminalSessionBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.suspended: set[str] = set()
        self.stream = _FiniteTerminalStream(
            b'event: output\ndata: {"seq":1,"data":"aGk="}\n\n',
            b'event: exit\ndata: {"seq":2,"exit_code":0}\n\n',
        )

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
            from cloud_agent.workspace_broker_client import WorkspaceBrokerError

            raise WorkspaceBrokerError(409, "workspace terminals are suspended")
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
    """The gateway keeps broker credentials private while proxying PTY controls."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
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
    assert broker.calls == [
        ("create", workspace_id, 24, 80),
        ("list", workspace_id),
        ("input", workspace_id, "term_1", "bHMK"),
        ("resize", workspace_id, "term_1", 40, 120),
        ("delete", workspace_id, "term_1"),
    ]


async def test_terminal_list_passes_the_brokers_advertised_limits_through(
    store: FakeStorage, monkeypatch
):
    """The browser checks these limits before asking, so it has to receive them."""
    broker = _TerminalSessionBroker()
    advertised = {
        "terminals": [_terminal_session_descriptor()],
        "max_sessions": 2,
        "max_input_bytes": 1024,
    }

    async def list_terminals(workspace_id: str, *, profiling=None):
        broker.calls.append(("list", workspace_id))
        return advertised

    monkeypatch.setattr(broker, "list_terminals", list_terminals)
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["state"] = "succeeded"
        listed = await client.get(f"/v1/agent/jobs/{job_id}/terminals")

    assert listed.status_code == 200
    assert listed.json() == advertised


async def test_terminal_controls_use_the_attached_session_capability(
    store: FakeStorage, monkeypatch
):
    """Attached PTY controls skip entitlement and never replay the event log."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)

    async def unexpected_entitlement_recheck(**_kwargs):
        raise AssertionError("attached terminal controls must not call external entitlement")

    monkeypatch.setattr(
        agent_workspace,
        "_require_workspace_entitlement",
        unexpected_entitlement_recheck,
    )

    async def unexpected_event_query(**_kwargs):
        raise AssertionError("terminal input must use the attempt readiness field")

    monkeypatch.setattr(store, "list_events_after", unexpected_event_query)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        workspace_id = _attach_shared_workspace(store, job_id)
        claimed = await store.attempt_commands.claim_job(
            worker_id="worker-1",
            lease_ttl_seconds=60,
            host="runner-test",
            host_credential_hash="test-credential-hash",
        )
        assert claimed is not None
        await store.attempt_commands.append_event(
            attempt_id=claimed["attempt_id"],
            lease_generation=claimed["lease_generation"],
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
        wrote = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "eA=="},
        )
        resized = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/resize",
            json={"rows": 30, "cols": 100},
        )
        killed = await local_client.delete(f"/v1/agent/jobs/{job_id}/terminals/term_1")

    assert wrote.status_code == 200
    assert resized.status_code == 200
    assert killed.status_code == 200
    assert broker.calls == [
        ("input", workspace_id, "term_1", "eA=="),
        ("resize", workspace_id, "term_1", 30, 100),
        ("delete", workspace_id, "term_1"),
    ]


async def test_interactive_terminal_stream_is_relayed_byte_for_byte(
    store: FakeStorage, monkeypatch
):
    """Output and exit SSE frames cross the gateway without re-encoding."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)

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
    assert broker.calls == [("stream", workspace_id, "term_1", 7)]
    assert broker.stream.closed is True


async def test_full_terminal_lifecycle_is_available_while_agent_runs(
    store: FakeStorage, monkeypatch
):
    """PTY creation waits for checkout, then stays available during the run."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
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
    assert broker.calls == [
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
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        claim = await local_client.post(
            "/v1/agent/worker/claim",
            json={"worker_id": "worker-1"},
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
    assert broker.calls == [
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
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        workspace_id = _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["state"] = "cancelled"
        store.jobs[job_id]["terminal_resume_pending"] = True
        response = await local_client.get(f"/v1/agent/jobs/{job_id}/terminals")

    assert response.status_code == 200
    assert broker.calls == [
        ("resume_settled", workspace_id, job_id),
        ("list", workspace_id),
    ]
    assert store.jobs[job_id]["terminal_resume_pending"] is False


async def test_worker_re_suspends_terminals_if_ready_event_loses_lease(
    store: FakeStorage,
    monkeypatch,
):
    """A lease expiring during broker resume cannot expose background processes."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        claim = await local_client.post(
            "/v1/agent/worker/claim",
            json={"worker_id": "worker-1"},
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
    assert broker.calls == [
        ("suspend_all", workspace_id, 1, job_id),
        ("resume_all", workspace_id, 1, job_id),
        ("suspend_all", workspace_id, 1, job_id),
    ]


async def test_worker_re_suspends_terminals_if_finish_loses_lease(
    store: FakeStorage,
    monkeypatch,
):
    """A failed terminal transition compensates after broker resume."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        claim = await local_client.post(
            "/v1/agent/worker/claim",
            json={"worker_id": "worker-1"},
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
    assert broker.calls == [
        ("suspend_all", workspace_id, 1, job_id),
        ("resume_all", workspace_id, 1, job_id),
        ("suspend_all", workspace_id, 1, job_id),
    ]


async def test_terminal_readiness_uses_the_shared_group_barrier(store: FakeStorage, monkeypatch):
    """Owner PTYs open only after the durable shared workspace barrier clears."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
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
    assert broker.calls == [("create", workspace_id, 24, 80)]


async def test_terminal_routes_recheck_entitlement_before_contacting_broker(
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
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        response = await local_client.post(
            f"/v1/agent/jobs/{job['id']}/terminals",
            json={"rows": 24, "cols": 80},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "repo_not_allowed"
    assert broker.calls == []


async def test_terminal_routes_report_missing_broker_and_workspace_as_conflicts(
    store: FakeStorage, monkeypatch
):
    """A retained chat reports missing broker/workspace as actionable conflicts."""
    from cloud_agent.workspace_broker_client import WorkspaceBrokerError

    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["state"] = "succeeded"
        monkeypatch.setattr(broker_routing, "_client_for", lambda _host: None)
        missing_broker = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

        class MissingWorkspaceBroker(_TerminalSessionBroker):
            async def create_terminal(
                self, workspace_id: str, *, rows: int, cols: int, profiling=None
            ):
                raise WorkspaceBrokerError(404, "shared chat workspace is not materialized")

        monkeypatch.setattr(
            broker_routing,
            "_client_for",
            lambda _host: MissingWorkspaceBroker(),
        )
        missing_workspace = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

    assert missing_broker.status_code == 409
    assert missing_broker.json()["detail"]["error"]["type"] == "workspace_unavailable"
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
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        thread_id = _attach_shared_workspace(store, job_id)
        store.jobs[job_id]["current_attempt_id"] = 100
        response = await local_client.post(f"/v1/agent/jobs/{job_id}/cancel")

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert broker.calls == [("resume_settled", thread_id, job_id)]
    assert store.jobs[job_id]["terminal_resume_pending"] is False


async def test_worker_flow_claim_event_finish(client: AsyncClient, store: FakeStorage):
    """The worker path is claim → token → event → fenced finish."""
    job_id = await _create_job(client)

    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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


async def test_retained_prompt_admission_is_worker_and_generation_fenced(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    admitted = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/sandbox/admit",
        json={"sandbox_generation": 3, "activation_generation": 7},
        headers=auth,
    )

    assert admitted.status_code == 200, admitted.text
    assert store.sandbox_admissions == [
        {
            "thread_id": store.jobs[job_id]["thread_id"],
            "job_id": job_id,
            "attempt_id": 100,
            "lease_generation": 1,
            "sandbox_generation": 3,
            "activation_generation": 7,
        }
    ]


async def test_shared_abandon_releases_the_exact_residency_and_grant(
    store: FakeStorage, monkeypatch
):
    """A pre-admission process failure must not leave a renewable grant live."""

    class RecordingGrants(_FakeGrants):
        def __init__(self) -> None:
            super().__init__()
            self.revoked: list[str] = []

        async def revoke(self, grant_id: str) -> bool:
            self.revoked.append(grant_id)
            return True

    grants = RecordingGrants()
    app = _build_app(store)
    from cloud_agent.api.deps import get_grant_client

    app.dependency_overrides[get_grant_client] = lambda: grants
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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
    assert grants.revoked == ["agr_resident_shared"]


async def test_worker_api_bounds_oversized_events_without_failing_or_duplicating_the_turn(
    client: AsyncClient, store: FakeStorage
):
    """Older runners can upload large events; the API stores a labelled preview."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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
    response = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert response.status_code == 200
    assert response.json() is None


async def test_lost_lease_maps_to_409_on_every_write_path(client: AsyncClient, store: FakeStorage):
    """Once the fence moves on, every remaining worker write returns 409."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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
        denied = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert denied.status_code in (401, 403)
    assert store.jobs[next(iter(store.jobs))]["state"] == "queued"

    app = _build_app(store, claim_auth=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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
            json={"worker_id": "w1"},
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
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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
        json={"worker_id": "w1", "lease_ttl_seconds": 99_999_999},
    )
    assert response.status_code == 422


async def test_terminal_stream_drains_beyond_one_page(client: AsyncClient, store: FakeStorage):
    """A finished job with multiple pages of backlog delivers every event.

    Regression: the terminal path drained exactly one extra page before
    emitting job_finished, so a client attaching to a finished job with a
    large backlog silently lost everything past the second page.
    """
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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
# MIGRATION.md § Discarded tests → "`test_agent_jobs_api.py` — two cases E9
# replaced in place (E7/E8/E9)", case 1. Ratified 2026-08-04.


async def test_the_claim_hands_over_a_gateway_grant_not_a_minted_token(
    client: AsyncClient, store: FakeStorage
):
    """The sandbox credential is distinct from the runner's — and is not ours.

    Two separate claims, and the second is the point: this service can
    mint the runner's control token because it verifies it, and it must *not*
    be able to mint the sandbox's, because the thing that credential buys is
    inference the gateway meters. So the sandbox token deliberately does not
    parse in this service's own namespace at all.
    """
    await _create_job(client)
    body = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    assert body["sandbox_token"]
    assert body["sandbox_token"] != body["worker_token"]

    from cloud_agent.tokens import SCOPE_FULL, InvalidAgentToken, parse_worker_token

    assert parse_worker_token(body["worker_token"])["scope"] == SCOPE_FULL
    with pytest.raises(InvalidAgentToken):
        parse_worker_token(body["sandbox_token"])

    # And the grant is linked to the attempt under its own fence, because
    # renewal and revocation are both lookups through that link.
    assert store.attempt_grants == {100: "agr_1"}


# ``test_sandbox_token_cannot_write_job_state`` was here, asserting 403
# ``insufficient_scope`` on every worker endpoint. The refusal is now a 401:
# the sandbox holds a gateway grant, which is not a credential in this
# service's namespace at all, so it does not reach the scope check — it fails
# to parse. Same guarantee, one step earlier and one status code over.
#
# The case below is its in-place replacement: same four endpoints, same
# positive control, line-for-line identical otherwise. The full record — and
# the note that ``_worker_claims``' 403 scope branch lost its only test with
# the original — is MIGRATION.md § Discarded tests → "`test_agent_jobs_api.py`
# — two cases E9 replaced in place (E7/E8/E9)", case 2. Ratified 2026-08-04.


async def test_the_sandbox_grant_cannot_write_job_state(client: AsyncClient):
    """A credential leaked from inside the sandbox cannot poison the job.

    This is the payoff of running the runner outside the sandbox: the only
    credential the agent can reach buys model calls, not event-log writes,
    terminal transitions, or lease renewal.
    """
    job_id = await _create_job(client)
    body = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    sandbox_auth = {"Authorization": f"Bearer {body['sandbox_token']}"}

    for path, payload in (
        (f"/v1/agent/worker/jobs/{job_id}/events", {"event_type": "message"}),
        (f"/v1/agent/worker/jobs/{job_id}/finish", {"state": "succeeded"}),
        (f"/v1/agent/worker/jobs/{job_id}/heartbeat", {}),
    ):
        response = await client.post(path, json=payload, headers=sandbox_auth)
        assert response.status_code == 401, path
        assert response.json()["detail"]["error"]["type"] == "unauthorized"

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
# grant the sandbox now carries). Recorded in MIGRATION.md's discarded-test
# register.


async def test_event_type_guard_rejects_a_trailing_newline(client: AsyncClient):
    """`match()` with `$` accepted "message\\n"; the guard must use fullmatch."""
    from cloud_agent.api._job_views import _SAFE_EVENT_TYPE

    assert _SAFE_EVENT_TYPE.fullmatch("message") is not None
    assert _SAFE_EVENT_TYPE.fullmatch("message\n") is None


async def test_the_claim_carries_a_read_only_clone_credential(store: FakeStorage):
    """The runner needs to check the repository out; it must not get push rights.

    An installation token inherits *every* permission the App holds unless it
    asks for less — and this App holds `contents: write` so the publisher can
    push. Handing that to the runner would put a write credential on the host
    that executes untrusted repository code, which is exactly the thing the
    patch-out design exists to avoid.
    """
    from cloud_agent.api.deps import get_agent_app_credentials

    asked: dict[str, Any] = {}

    class FakeApp:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            asked.update({"repo": repo, **kwargs})
            return "ghs_clone_only"

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: FakeApp()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _create_job(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert claim.json()["clone_token"] == "ghs_clone_only"
    assert asked["permissions"] == {"contents": "read"}
    assert asked["repository_scoped"] is True


async def test_a_deployment_without_a_github_app_still_claims_jobs(client: AsyncClient):
    """No App configured is a working setup: a public repository needs no token."""
    await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert claim.status_code == 200
    assert claim.json()["clone_token"] is None


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
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert claim.status_code == 200
    assert claim.json()["job_id"] == job_id
    assert claim.json()["clone_token"] is None


async def test_a_transient_credential_error_is_retryable_not_terminal(
    store: FakeStorage,
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
            raise TimeoutError("api.github.com timed out")

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: FlakyApp()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _create_job(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

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

    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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
        json={
            "repo": "o/n",
            "task_prompt": "do the thing",
            "model": "m",
            "base_sha": pinned,
        },
    )
    job_id = created.json()["id"]

    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
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
        json={"repo": "someone-else/private", "task_prompt": "exfiltrate", "model": "m"},
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
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

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
        refused = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
        accepted = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert refused.status_code == 403
    assert store.jobs[denied["id"]]["state"] == "failed"
    assert accepted.status_code == 200
    assert accepted.json()["job_id"] == ready["id"]


async def test_spend_and_usage_come_from_the_ledger_not_the_agent(store: FakeStorage):
    """A job's numbers must not depend on what the agent says about itself.

    Models do misreport their own usage — this session watched one claim it had
    edited a file it never touched — so the owner-facing totals read the same
    billing ledger the budget check already trusts.
    """
    from cloud_agent.api.deps import get_log_store

    class Ledger:
        async def get_agent_job_cost(self, job_id: str) -> float:
            return 0.0075

        async def get_agent_job_usage(self, job_id: str) -> dict[str, float]:
            return {"tokens_in": 74000, "tokens_out": 523, "calls": 3}

    app = _build_app(store)
    app.dependency_overrides[get_log_store] = lambda: Ledger()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        got = await client.get(f"/v1/agent/jobs/{job_id}")

    body = got.json()
    assert body["spent_usd"] == 0.0075
    assert body["tokens_in"] == 74000
    assert body["model_calls"] == 3


async def test_a_deployment_without_a_ledger_reports_absent_not_zero(client: AsyncClient):
    """A missing source must not look like a job that spent nothing."""
    job_id = await _create_job(client)

    body = (await client.get(f"/v1/agent/jobs/{job_id}")).json()

    assert body["spent_usd"] is None
    assert body["tokens_in"] is None


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
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    for path, body in (
        (f"/v1/agent/worker/jobs/{job_id}/publish/begin", None),
        (f"/v1/agent/worker/jobs/{job_id}/publish/complete", {"pr_url": "https://evil/pr/1"}),
    ):
        response = await client.post(path, json=body, headers=auth)
        assert response.status_code == 404, f"{path} must not exist"


class _FakeGitHubApp:
    """Stands in for the App: a user token maps to installations, which cover repos.

    ``repos_by_installation`` is what each installation covers; ``pushable``,
    where given, is the part of it this user may push to — what GitHub answers
    when asked as the user. Absent, the user may push to all of it.
    """

    def __init__(
        self,
        installations,
        repos_by_installation,
        *,
        exchange_fails=False,
        login="octocat",
        pushable=None,
    ):
        from cloud_agent.source_control import SourceControlCipher

        self.installations = installations
        self.repos = repos_by_installation
        self.pushable = pushable
        self.exchange_fails = exchange_fails
        self.login = login
        self.exchanged: list[str] = []
        self.listed_with: list[str] = []
        self.revoked: list[str] = []
        self.refused_tokens: set[str] = set()
        self.forgotten: list[str] = []
        self.cipher = SourceControlCipher("test-source-control-key")

    async def exchange_user_code(self, code: str) -> Any:
        from cloud_agent.github_app import UserTokens

        if self.exchange_fails:
            raise RuntimeError("GitHub refused the code exchange")
        self.exchanged.append(code)
        return UserTokens(
            access_token=f"user-token-for-{code}",
            access_expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
            refresh_token=f"refresh-for-{code}",
            refresh_expires_at=datetime.now(timezone.utc) + timedelta(days=180),
        )

    async def user_profile(self, user_token: str) -> dict[str, Any]:
        assert user_token.startswith("user-token-for-"), "the profile is the token's own"
        return {"id": 583231, "login": self.login}

    async def writable_repositories_for_user(self, user_token: str, installation_id: int):
        from cloud_agent.github_app import UserAuthorizationRevoked

        if user_token in self.refused_tokens:
            raise UserAuthorizationRevoked("GitHub refused the user's token")
        self.listed_with.append(user_token)
        if self.pushable is not None:
            return list(self.pushable.get(installation_id, []))
        return list(self.repos.get(installation_id, []))

    # The fake answers fresh every time; the minute-long memory is the real
    # class's, and tested there.
    def cached_user_repositories(self, user_id, installation_ids):
        return None

    def remember_user_repositories(self, user_id, installation_ids, repositories):
        pass

    def forget_user(self, user_id):
        self.forgotten.append(user_id)

    async def revoke_user_authorization(self, access_token: str) -> None:
        self.revoked.append(access_token)

    user_authorization_configured = True

    def user_authorization_url(self, state: str) -> str:
        return f"https://github.test/login/oauth/authorize?state={state}"

    async def installations_for_user(self, user_token: str) -> list[dict[str, Any]]:
        return self.installations

    async def repositories_for_installation(self, installation_id: int) -> list[str]:
        return self.repos.get(installation_id, [])

    async def token_for(self, repo: str, **kwargs: Any) -> str:
        return "ghs_x"


def _store_with_grants(store: FakeStorage) -> FakeStorage:
    """Give the fake store the grant surface the router uses."""
    store.grants = {}
    store.consumed_oauth_states = set()

    async def record_repo_grant(*, user_id, installation_id, account_login=None):
        store.grants.setdefault(user_id, {})[installation_id] = account_login

    async def list_repo_grants(*, user_id):
        return [
            {"installation_id": iid, "account_login": login}
            for iid, login in store.grants.get(user_id, {}).items()
        ]

    async def revoke_repo_grant(*, user_id, installation_id):
        return store.grants.get(user_id, {}).pop(installation_id, "missing") != "missing"

    async def consume_oauth_state(*, state_hash, user_id, provider):
        assert user_id == "user-owner"
        assert provider == "github"
        if state_hash in store.consumed_oauth_states:
            return None
        store.consumed_oauth_states.add(state_hash)
        return {"code_verifier_ciphertext": None}

    store.issued_oauth_states = []

    async def create_oauth_state(**kwargs):
        store.issued_oauth_states.append(kwargs)

    store.github_accounts = {}

    async def connect_github_account(
        *,
        user_id,
        github_user_id,
        login,
        installations,
        access_token_ciphertext=None,
        access_expires_at=None,
        refresh_token_ciphertext=None,
        refresh_expires_at=None,
    ):
        store.github_accounts[user_id] = {
            "github_user_id": github_user_id,
            "login": login,
            "access_token_ciphertext": access_token_ciphertext,
            "access_expires_at": access_expires_at,
            "refresh_token_ciphertext": refresh_token_ciphertext,
            "refresh_expires_at": refresh_expires_at,
        }
        store.grants[user_id] = {
            item["installation_id"]: item.get("account_login") for item in installations
        }

    async def get_github_account(*, user_id):
        account = store.github_accounts.get(user_id)
        return dict(account) if account else None

    async def claim_github_token_refresh(*, user_id, lease_seconds, margin_seconds):
        return None

    async def store_github_tokens(**kwargs):
        return False

    async def clear_github_tokens(*, user_id, access_token_ciphertext):
        account = store.github_accounts.get(user_id)
        if not account or account.get("access_token_ciphertext") != access_token_ciphertext:
            return False
        account.update(access_token_ciphertext=None, refresh_token_ciphertext=None)
        return True

    async def disconnect_github_account(*, user_id):
        store.github_accounts.pop(user_id, None)
        store.grants.pop(user_id, None)

    store.record_repo_grant = record_repo_grant
    store.list_repo_grants = list_repo_grants
    store.revoke_repo_grant = revoke_repo_grant
    store.consume_oauth_state = consume_oauth_state
    store.create_oauth_state = create_oauth_state
    store.connect_github_account = connect_github_account
    store.get_github_account = get_github_account
    store.claim_github_token_refresh = claim_github_token_refresh
    store.store_github_tokens = store_github_tokens
    store.clear_github_tokens = clear_github_tokens
    store.disconnect_github_account = disconnect_github_account
    return store


async def test_connecting_records_only_what_github_attests(store: FakeStorage, monkeypatch):
    """The entitlement must come from GitHub's answer, not the browser's claim.

    The browser sends a code; the platform exchanges it for a token that speaks
    as that user and asks GitHub which installations they can reach. An
    installation id posted directly would be exactly the confused deputy this
    path exists to prevent.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from cloud_agent.api.deps import get_agent_app_credentials, get_github_app_state
    from cloud_agent.source_control_runtime import GitHubAppState

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service", "acme/web"]},
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    # The config and disconnect routes answer while the App is unavailable,
    # so they read it through the lenient dependency.
    app.dependency_overrides[get_github_app_state] = lambda: GitHubAppState(
        credentials=app_creds, load_status="ready"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        connected = await client.post(
            "/v1/agent/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        config = await client.get("/v1/agent/config")

    assert connected.status_code == 200
    assert app_creds.exchanged == ["abc123"], "the code must be exchanged, not trusted"
    assert connected.json()["connections"][0]["installation_id"] == 77
    # The picker now offers what this user actually connected.
    assert config.json()["repos"] == ["acme/service", "acme/web"]
    assert config.json()["github_connected"] is True


async def test_a_connected_user_may_run_only_their_own_repos(store: FakeStorage, monkeypatch):
    """Connecting one owner must not entitle someone else's repository."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from cloud_agent.api.deps import get_agent_app_credentials

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service"]},
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        mine = await client.post(
            "/v1/agent/jobs",
            json={"repo": "acme/service", "task_prompt": "fix it", "model": "m"},
        )
        theirs = await client.post(
            "/v1/agent/jobs",
            json={"repo": "someone-else/private", "task_prompt": "exfiltrate", "model": "m"},
        )

    assert mine.status_code == 201
    assert theirs.status_code == 403
    assert "connected" in theirs.text


async def test_a_failed_exchange_grants_nothing(store: FakeStorage, monkeypatch):
    """A code that does not verify must leave the user with no entitlement."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from cloud_agent.api.deps import get_agent_app_credentials, get_github_app_state
    from cloud_agent.source_control_runtime import GitHubAppState

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service"]},
        exchange_fails=True,
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    # The config and disconnect routes answer while the App is unavailable,
    # so they read it through the lenient dependency.
    app.dependency_overrides[get_github_app_state] = lambda: GitHubAppState(
        credentials=app_creds, load_status="ready"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        connected = await client.post(
            "/v1/agent/github/connect",
            json={"code": "stolen", "state": "test-state-value-123456"},
        )
        config = await client.get("/v1/agent/config")

    assert connected.status_code == 400
    assert config.json()["repos"] == []


async def test_disconnecting_removes_the_entitlement(store: FakeStorage, monkeypatch):
    """Revoking here is half of it; uninstalling on GitHub is the other half."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from cloud_agent.api.deps import get_agent_app_credentials, get_github_app_state
    from cloud_agent.source_control_runtime import GitHubAppState

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service"]},
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    # The config and disconnect routes answer while the App is unavailable,
    # so they read it through the lenient dependency.
    app.dependency_overrides[get_github_app_state] = lambda: GitHubAppState(
        credentials=app_creds, load_status="ready"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        await client.delete("/v1/agent/github/connect/77")
        after = await client.post(
            "/v1/agent/jobs",
            json={"repo": "acme/service", "task_prompt": "fix it", "model": "m"},
        )

    assert after.status_code == 403


_INSTALL_URL = "https://github.com/apps/agent-test/installations/new"


def _connectable_app(store: FakeStorage, app_creds: _FakeGitHubApp) -> FastAPI:
    """An app where users can connect their own GitHub: App, OAuth and install URL."""
    from cloud_agent.api.deps import get_agent_app_credentials, get_github_app_state
    from cloud_agent.source_control_runtime import GitHubAppState

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    app.dependency_overrides[get_github_app_state] = lambda: GitHubAppState(
        credentials=app_creds, load_status="ready"
    )
    return app


async def test_connecting_records_the_account_even_before_any_installation(
    store: FakeStorage, monkeypatch
):
    """Having connected and having repository access are two different states.

    A user with no installation yet used to leave no row at all, and the
    composer — which read "connected" as "has a repository" — sent them back to
    a Connect button they had just pressed.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(installations=[], repos_by_installation={})
    grants_store = _store_with_grants(store)
    app = _connectable_app(grants_store, app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        connected = await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        config = await client.get("/v1/agent/config")
        integrations = await client.get("/v1/agent/integrations")

    assert connected.status_code == 200
    assert connected.json()["connections"] == []
    assert connected.json()["install_url"].startswith(_INSTALL_URL)
    # Who connected is GitHub's answer for the exchanged token, and the token
    # itself is kept only encrypted.
    account = grants_store.github_accounts[_OWNER]
    assert (account["github_user_id"], account["login"]) == (583231, "octocat")
    assert "user-token-for-abc123" not in str(account)
    assert app_creds.cipher.decrypt(account["access_token_ciphertext"]) == "user-token-for-abc123"
    assert app_creds.cipher.decrypt(account["refresh_token_ciphertext"]) == "refresh-for-abc123"

    assert config.json()["github_connected"] is True
    assert config.json()["repos"] == []

    github_row = integrations.json()["providers"][0]
    assert github_row["connected"] is True
    assert github_row["accounts"] == []
    # Where repository access is added — including a personal account the App
    # is not installed on yet — is offered without an installation to manage.
    assert github_row["manage_url"].startswith(_INSTALL_URL)


async def test_connecting_offers_the_install_page_until_the_app_is_on_the_users_own_account(
    store: FakeStorage, monkeypatch
):
    """An organization's installation is not the user's own.

    The install page used to be offered only when connecting found no
    installation at all. A member of an organization that already had the App
    found that one, skipped the page, and could not tell that their own
    repositories needed an installation of their own.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme", "account_id": 1001}],
        repos_by_installation={77: ["acme/service"], 88: ["octocat/dotfiles"]},
    )
    app = _connectable_app(_store_with_grants(store), app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        org_only = await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "first", "state": "test-state-value-first-1"},
        )
        # The user installs the App on their own account and comes back through
        # the same callback.
        app_creds.installations = [
            {"installation_id": 77, "account_login": "acme", "account_id": 1001},
            {"installation_id": 88, "account_login": "octocat", "account_id": 583231},
        ]
        with_own = await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "second", "state": "test-state-value-second-2"},
        )

    assert [row["installation_id"] for row in org_only.json()["connections"]] == [77]
    assert org_only.json()["repos"] == ["acme/service"]
    assert org_only.json()["install_url"].startswith(_INSTALL_URL)
    assert with_own.json()["install_url"] is None
    assert with_own.json()["repos"] == ["acme/service", "octocat/dotfiles"]


async def test_a_user_is_offered_only_the_repositories_they_can_push_to(
    store: FakeStorage, monkeypatch
):
    """The installation covers more than this user was given; only theirs are offered.

    Asked with the installation's token, GitHub lists every repository the
    installation covers — for an organization, repositories an outside
    collaborator on one of them was never given, which the platform would then
    clone into their sandbox. Asked as the user, it lists theirs, with their
    permission on each: the agent pushes a branch, so push is the bar.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme", "account_id": 1001}],
        repos_by_installation={77: ["acme/secret", "acme/service", "acme/readonly"]},
        pushable={77: ["acme/service"]},
    )
    app = _connectable_app(_store_with_grants(store), app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        config = await client.get("/v1/agent/config")
        mine = await client.post(
            "/v1/agent/jobs",
            json={"repo": "acme/service", "task_prompt": "fix it", "model": "m"},
        )
        secret = await client.post(
            "/v1/agent/jobs",
            json={"repo": "acme/secret", "task_prompt": "read it", "model": "m"},
        )
        branches = await client.get("/v1/agent/branches", params={"repo": "acme/secret"})

    assert config.json()["repos"] == ["acme/service"]
    assert mine.status_code == 201
    assert secret.status_code == 403
    assert branches.status_code == 403
    # Asked as the user, with the token the connection exchanged.
    assert set(app_creds.listed_with) == {"user-token-for-abc123"}


async def test_a_token_github_refuses_is_forgotten_and_the_user_connects_again(
    store: FakeStorage, monkeypatch
):
    """A revoked authorization closes everything, and the composer asks to reconnect."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme", "account_id": 1001}],
        repos_by_installation={77: ["acme/service"]},
    )
    grants_store = _store_with_grants(store)
    app = _connectable_app(grants_store, app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        app_creds.refused_tokens.add("user-token-for-abc123")
        refused = await client.post(
            "/v1/agent/jobs",
            json={"repo": "acme/service", "task_prompt": "fix it", "model": "m"},
        )
        config = await client.get("/v1/agent/config")

    assert refused.status_code == 403
    assert "Reconnect GitHub" in refused.text
    assert grants_store.github_accounts[_OWNER]["access_token_ciphertext"] is None
    assert config.json()["github_connected"] is False
    assert config.json()["repos"] == []


async def test_disconnecting_github_forgets_the_token_and_withdraws_the_authorization(
    store: FakeStorage, monkeypatch
):
    """The stored token is why this exists: removing installations one by one left it."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme", "account_id": 1001}],
        repos_by_installation={77: ["acme/service"]},
    )
    grants_store = _store_with_grants(store)
    app = _connectable_app(grants_store, app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        removed = await client.delete("/v1/agent/integrations/github")
        config = await client.get("/v1/agent/config")
        after = await client.post(
            "/v1/agent/jobs",
            json={"repo": "acme/service", "task_prompt": "fix it", "model": "m"},
        )

    assert removed.status_code == 200
    github_row = removed.json()["providers"][0]
    assert (github_row["connected"], github_row["accounts"]) == (False, [])
    assert removed.json()["providers"][1]["provider"] == "gitlab", "the page's whole state"
    assert app_creds.revoked == ["user-token-for-abc123"]
    assert grants_store.github_accounts == {}
    assert config.json()["github_connected"] is False
    assert after.status_code == 403


async def test_refresh_asks_github_again_rather_than_reusing_the_last_answer(
    store: FakeStorage, monkeypatch
):
    """The composer's Refresh is for a repository just given to the App on GitHub."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    app_creds = _FakeGitHubApp(installations=[], repos_by_installation={})
    app = _connectable_app(_store_with_grants(store), app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/v1/agent/config")
        assert app_creds.forgotten == []
        fresh = await client.get("/v1/agent/config", params={"fresh": "true"})

    assert fresh.status_code == 200
    assert app_creds.forgotten == [_OWNER]


async def test_reconnecting_keeps_only_the_installations_github_still_reports(
    store: FakeStorage, monkeypatch
):
    """An installation the user lost stays granted only until they reconnect.

    Grants were only ever added, so leaving an organization kept its
    repositories until someone removed the connection by hand.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[
            {"installation_id": 77, "account_login": "acme"},
            {"installation_id": 88, "account_login": "octocat"},
        ],
        repos_by_installation={77: ["acme/service"], 88: ["octocat/dotfiles"]},
    )
    grants_store = _store_with_grants(store)
    app = _connectable_app(grants_store, app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "first", "state": "test-state-value-first-1"},
        )
        before = await client.get("/v1/agent/config")
        app_creds.installations = [{"installation_id": 88, "account_login": "octocat"}]
        await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "second", "state": "test-state-value-second-2"},
        )
        after = await client.get("/v1/agent/config")
        lost = await client.post(
            "/v1/agent/jobs",
            json={"repo": "acme/service", "task_prompt": "fix it", "model": "m"},
        )

    assert before.json()["repos"] == ["acme/service", "octocat/dotfiles"]
    assert after.json()["repos"] == ["octocat/dotfiles"]
    assert grants_store.grants[_OWNER] == {88: "octocat"}
    assert lost.status_code == 403


async def test_an_allowlisted_repository_does_not_stand_in_for_connecting(
    store: FakeStorage, monkeypatch
):
    """Where users can connect GitHub, they connect before choosing anything.

    The allowlist is still an entitlement — the create endpoint honours it — but
    it no longer makes the composer skip the connection: that is how a user ran
    their first task without knowing it would change a GitHub repository.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "acme/public")
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(installations=[], repos_by_installation={})
    app = _connectable_app(_store_with_grants(store), app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get("/v1/agent/config")

    assert config.json()["repos"] == ["acme/public"]
    assert config.json()["github_connected"] is False


async def test_with_nothing_to_connect_to_the_allowlist_is_the_whole_entitlement(
    store: FakeStorage, monkeypatch
):
    """The dogfood shape: no connectable App, so an allowlisted repository runs."""
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "acme/public")
    monkeypatch.delenv("AGENT_GITHUB_APP_INSTALL_URL", raising=False)
    app = _build_app(_store_with_grants(store))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get("/v1/agent/config")

    assert config.json()["repos"] == ["acme/public"]
    assert config.json()["github_connected"] is True


async def test_a_connection_with_no_token_is_made_again_before_anything_runs(
    store: FakeStorage, monkeypatch
):
    """Grants from before users' tokens were kept: GitHub cannot be asked as them.

    Such a connection used to be the whole entitlement, read with the
    installation's token — every repository the installation covered. It now
    grants nothing until the user connects again, and says so.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    grants_store = _store_with_grants(store)
    await grants_store.record_repo_grant(user_id=_OWNER, installation_id=77, account_login="acme")
    app_creds = _FakeGitHubApp(installations=[], repos_by_installation={77: ["acme/service"]})
    app = _connectable_app(grants_store, app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get("/v1/agent/config")
        integrations = await client.get("/v1/agent/integrations")
        refused = await client.post(
            "/v1/agent/jobs",
            json={"repo": "acme/service", "task_prompt": "fix it", "model": "m"},
        )

    assert config.json()["github_connected"] is False
    assert config.json()["repos"] == []
    assert integrations.json()["providers"][0]["connected"] is False
    assert refused.status_code == 403
    assert "Reconnect GitHub" in refused.text


# ── Issue #47: a GitHub App this process could not load ────────────────────


@pytest.fixture(scope="module")
def app_private_pem() -> str:
    """A real key: recovery signs with it, as the running service would."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class _StepClock:
    """The runtime's monotonic clock, moved by the test instead of waited for."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _app_with_unloaded_github(
    store: FakeStorage, key_path: Any, clock: _StepClock, minter: Any
) -> FastAPI:
    """An app whose GitHub App is configured but whose key is not there yet.

    The real dependencies over a real runtime. ``_build_app`` answers ``None``
    for the credentials, which is exactly the misreading issue #47 was about,
    so that override is removed rather than kept beside the runtime. Once the
    key can be read the runtime signs with it for real and then hands back
    ``minter``, so recovery needs no network.
    """
    from cloud_agent.api.deps import get_agent_app_credentials
    from cloud_agent.source_control_runtime import (
        GitHubAppProvider,
        GitLabOAuthLoad,
        SourceControlRuntime,
    )

    app = _build_app(store)
    del app.dependency_overrides[get_agent_app_credentials]
    provider = GitHubAppProvider(
        {"AGENT_GITHUB_APP_ID": "12345", "AGENT_GITHUB_APP_PRIVATE_KEY_PATH": str(key_path)},
        clock=clock,
        credentials_factory=lambda config: minter,
    )
    app.state.source_control = SourceControlRuntime(github=provider, gitlab=GitLabOAuthLoad({}))
    return app


async def test_an_unloaded_github_app_is_temporarily_unavailable_until_its_key_loads(
    store: FakeStorage, tmp_path, app_private_pem: str
):
    """What a user meets during the outage, and after it, in one process.

    Not "admin setup required" and not "no access": the App is configured and
    this process has not read its key yet. The pages that describe state keep
    answering, with the user's connection intact; the ones that act on GitHub
    answer 503 with a retry hint. Once the key can be read, the same process
    serves all of it again — no restart.
    """
    grants_store = _store_with_grants(store)
    key = tmp_path / "github-app.pem"
    clock = _StepClock()
    minter = _FakeGitHubApp(installations=[], repos_by_installation={77: ["acme/service"]})
    await grants_store.connect_github_account(
        user_id=_OWNER,
        github_user_id=583231,
        login="octocat",
        installations=[{"installation_id": 77, "account_login": "acme"}],
        access_token_ciphertext=minter.cipher.encrypt("user-token-for-earlier"),
    )
    app = _app_with_unloaded_github(grants_store, key, clock, minter)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_job_raw(client)
        branches = await client.get("/v1/agent/branches", params={"repo": "owner/name"})
        integrations = await client.get("/v1/agent/integrations")
        config = await client.get("/v1/agent/config")

        key.write_text(app_private_pem)
        clock.now += 30
        recreated = await _create_job_raw(client)
        integrations_after = await client.get("/v1/agent/integrations")
        config_after = await client.get("/v1/agent/config")

    for refused in (created, branches):
        assert refused.status_code == 503
        assert refused.headers["retry-after"] == "30"
        assert refused.json()["detail"]["error"]["type"] == "source_control_unavailable"
        assert refused.json()["detail"]["error"]["provider"] == "github"

    assert integrations.status_code == 200
    github_row, gitlab_row = integrations.json()["providers"]
    assert github_row["load_status"] == "unavailable"
    assert (github_row["configured"], github_row["connect_url"]) == (False, None)
    assert github_row["connected"] is True, "an outage is not a lost connection"
    assert [account["label"] for account in github_row["accounts"]] == ["acme"]
    assert github_row["repositories"] == []
    assert gitlab_row["load_status"] == "unconfigured"

    assert config.status_code == 200
    assert config.json()["github_load_status"] == "unavailable"
    assert config.json()["github_connected"] is False
    assert config.json()["runtimes"], "the rest of the composer's answers still hold"

    assert recreated.status_code == 201
    github_after = integrations_after.json()["providers"][0]
    assert github_after["load_status"] == "ready"
    assert [repo["name"] for repo in github_after["repositories"]] == ["acme/service"]
    assert config_after.json()["github_load_status"] == "ready"
    assert config_after.json()["github_connected"] is True


async def test_a_connection_can_be_removed_while_the_app_is_unloaded(store: FakeStorage, tmp_path):
    """The grant is this service's own row; removing it never needed GitHub."""
    grants_store = _store_with_grants(store)
    await grants_store.record_repo_grant(user_id=_OWNER, installation_id=77, account_login="acme")
    minter = _FakeGitHubApp(installations=[], repos_by_installation={})
    app = _app_with_unloaded_github(grants_store, tmp_path / "github-app.pem", _StepClock(), minter)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        removed = await client.delete("/v1/agent/integrations/github/connections/77")

    assert removed.status_code == 200
    assert removed.json()["connections"] == []
    assert grants_store.grants[_OWNER] == {}


async def test_a_claim_while_the_app_is_unloaded_leaves_the_job_queued(
    store: FakeStorage, tmp_path, app_private_pem: str
):
    """The runner is told to come back: no attempt spent, nothing minted.

    With ``None`` for credentials the claim checked only the allowlist and
    handed the job over without a clone token, so a private repository failed
    for good at checkout. The refusal comes before the claim itself.
    """
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://test"
    ) as client:
        job_id = await _create_job(client)
    key = tmp_path / "github-app.pem"
    clock = _StepClock()
    minter = _FakeGitHubApp(installations=[], repos_by_installation={})
    app = _app_with_unloaded_github(store, key, clock, minter)
    grants = _FakeGrants()
    from cloud_agent.api.deps import get_grant_client

    app.dependency_overrides[get_grant_client] = lambda: grants
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        refused = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
        while_down = dict(store.jobs[job_id])
        minted_while_down = list(grants.minted)

        key.write_text(app_private_pem)
        clock.now += 30
        claimed = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert refused.status_code == 503
    assert refused.json()["detail"]["error"]["type"] == "source_control_unavailable"
    assert (while_down["state"], while_down["current_attempt_id"]) == ("queued", None)
    assert minted_while_down == []
    assert claimed.status_code == 200
    assert claimed.json()["job_id"] == job_id
    assert claimed.json()["clone_token"] == "ghs_x"


async def test_a_turn_finishes_while_the_app_is_unloaded_and_its_wakeup_waits(
    store: FakeStorage, tmp_path
):
    """The finish is recorded and acknowledged; only the next goal round waits.

    A finish the runner could not get acknowledged would be retried and then
    lost with the lease, so it must not depend on the App. The goal consumer it
    drains stops before claiming anything, which is what leaves a wakeup's
    attempts unspent (see ``test_goal_controller``).
    """
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://test"
    ) as client:
        job_id = await _create_job(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    claimed_wakeups: list[int] = []
    original_claim = store.goal_wakeups.claim_wakeups

    async def counting_claim(*, limit: int = 1) -> list[dict[str, Any]]:
        claimed_wakeups.append(limit)
        return await original_claim(limit=limit)

    store.goal_wakeups.claim_wakeups = counting_claim
    minter = _FakeGitHubApp(installations=[], repos_by_installation={})
    app = _app_with_unloaded_github(store, tmp_path / "github-app.pem", _StepClock(), minter)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        finish = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/finish",
            json={"state": "succeeded"},
            headers=auth,
        )

    assert finish.status_code == 200
    assert store.jobs[job_id]["state"] == "succeeded"
    assert claimed_wakeups == [], "no wakeup may be claimed while the App is unloaded"


async def test_a_branch_is_pinned_to_a_commit_at_creation(store: FakeStorage, monkeypatch):
    """A job records the sha, never the branch name.

    A branch moves. A job that stored "dev" would silently mean a different
    tree by the time it ran, and the publisher applies its patch onto a pinned
    commit — so resolving late would mean generating a diff against one tree
    and pushing it onto another.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    from cloud_agent.api.deps import get_agent_app_credentials

    class BranchApp(_FakeGitHubApp):
        async def resolve_ref(self, repo: str, ref: str) -> str:
            assert ref == "dev"
            return "f" * 40

    app_creds = BranchApp(installations=[], repos_by_installation={})
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/agent/jobs",
            json={
                "repo": "owner/name",
                "task_prompt": "fix it",
                "model": "m",
                "base_ref": "dev",
                "metadata": {"_agent_base_ref": "spoofed", "ticket": "ABC-1"},
            },
        )

    assert created.status_code == 201
    assert created.json()["base_sha"] == "f" * 40
    assert created.json()["base_ref"] == "dev"
    assert created.json()["output_branch"].startswith("agent/")
    assert created.json()["metadata"] == {"ticket": "ABC-1"}


async def test_reserved_base_ref_metadata_cannot_invent_a_branch(client: AsyncClient):
    """Without base_ref, caller metadata must not forge the Git panel's base branch."""
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "m",
            "metadata": {"_agent_base_ref": "forged"},
        },
    )

    assert created.status_code == 201
    assert created.json()["base_ref"] is None
    assert created.json()["metadata"] is None


async def test_an_unresolvable_branch_is_refused_at_creation(store: FakeStorage, monkeypatch):
    """Better a clear 400 than a job queued against a ref that does not exist."""
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    from cloud_agent.api.deps import get_agent_app_credentials

    class BrokenBranchApp(_FakeGitHubApp):
        async def resolve_ref(self, repo: str, ref: str) -> str:
            raise RuntimeError("no such branch")

    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: BrokenBranchApp(
        installations=[], repos_by_installation={}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/agent/jobs",
            json={
                "repo": "owner/name",
                "task_prompt": "fix it",
                "model": "m",
                "base_ref": "nope",
            },
        )

    assert created.status_code == 400
    assert "nope" in created.text


async def test_branches_are_only_listed_for_an_entitled_repo(store: FakeStorage, monkeypatch):
    """This reads through the platform's installation, so it needs the same gate.

    Without it, the endpoint enumerates branches of any repository the App
    happens to cover — which is the confused deputy again, one level down.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    from cloud_agent.api.deps import get_agent_app_credentials

    class BranchApp(_FakeGitHubApp):
        async def branches_for_repo(self, repo: str) -> dict[str, Any]:
            return {"default": "main", "branches": ["main", "dev"]}

    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: BranchApp(
        installations=[], repos_by_installation={}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mine = await client.get("/v1/agent/branches", params={"repo": "owner/name"})
        theirs = await client.get("/v1/agent/branches", params={"repo": "someone/private"})

    assert mine.status_code == 200
    assert mine.json()["default"] == "main"
    assert theirs.status_code == 403


# ── MCP connections ───────────────────────────────────────────────────


def _binding(
    connection_id: int,
    *,
    alias: str = "github",
    tools: dict[str, str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """One ``McpBindingInput`` body, as the composer sends it.

    ``tools`` is the selection itself, name to deny/confirm/allow. A name that
    is absent is denied, which is why every case here names the tools it means.
    """
    binding: dict[str, Any] = {
        "connection_id": connection_id,
        "alias": alias,
        "tools": {"get_issue": "allow"} if tools is None else tools,
        "resources": {},
        "prompts": {},
        "enabled": True,
    }
    binding.update(overrides)
    return binding


def _job_body(**overrides: Any) -> dict[str, Any]:
    """A valid submission, with the fields under test overridable."""
    body: dict[str, Any] = {"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1"}
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("runtime", "plugins"),
    [
        ("claude-code", [{"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}]),
        ("codex", [{"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}]),
        ("pi", [{"plugin": "@owner/agent-extension@1.2.3"}]),
        ("opencode", [{"plugin": "@owner/agent-extension@1.2.3"}]),
        ("kilo", [{"plugin": "@owner/agent-extension@1.2.3"}]),
    ],
)
async def test_plugins_are_retained_and_visible_on_job_reads(
    client: AsyncClient, store: FakeStorage, runtime: str, plugins: list[dict[str, str]]
):
    created = await client.post("/v1/agent/jobs", json=_job_body(runtime=runtime, plugins=plugins))

    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    assert store.jobs[job_id]["plugins"] == plugins
    assert [
        {key: value for key, value in entry.items() if value is not None}
        for entry in created.json()["plugins"]
    ] == plugins
    detail = await client.get(f"/v1/agent/jobs/{job_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["plugins"] == created.json()["plugins"]


async def test_plugins_cannot_be_changed_in_a_follow_up(client: AsyncClient, store: FakeStorage):
    parent_id = await _create_job(client)
    response = await _send(client, store, parent_id, {"prompt": "continue", "plugins": []})

    assert response.status_code == 422, response.text
    assert store.pending == {}


async def test_duplicate_plugins_are_refused_before_model_lookup(
    client: AsyncClient, store: FakeStorage
):
    plugin = {"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}
    response = await client.post(
        "/v1/agent/jobs", json=_job_body(plugins=[plugin, plugin], model="no-such-model")
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["type"] == "invalid_plugin_selection"
    assert store.jobs == {}


async def test_plugin_shape_is_validated_for_the_requested_runtime(
    client: AsyncClient, store: FakeStorage
):
    response = await client.post(
        "/v1/agent/jobs",
        json=_job_body(plugins=[{"plugin": "reviewer", "marketplace": "owner/plugins"}]),
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["type"] == "invalid_plugin_selection"
    assert store.jobs == {}


async def test_plugin_changes_cannot_reuse_a_submission_key(
    client: AsyncClient, store: FakeStorage
):
    first_plugins = [{"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}]
    headers = {"Idempotency-Key": "plugin-submit"}
    first = await client.post(
        "/v1/agent/jobs", json=_job_body(plugins=first_plugins), headers=headers
    )
    assert first.status_code == 201, first.text
    repeated = await client.post(
        "/v1/agent/jobs", json=_job_body(plugins=first_plugins), headers=headers
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["id"] == first.json()["id"]
    changed = await client.post(
        "/v1/agent/jobs",
        json=_job_body(plugins=[{**first_plugins[0], "marketplace": "owner/another-marketplace"}]),
        headers=headers,
    )
    assert changed.status_code == 409, changed.text
    assert len(store.jobs) == 1


async def test_the_composer_is_told_which_connections_and_which_runtimes(
    client: AsyncClient, store: FakeStorage
):
    """A picker built from anything but the create endpoint's rules is decoration.

    The composer learns which connections exist, what they offer and whether
    they currently work. It never learns the address or the credential:
    ``McpConnectionSummary`` has no field either could land in.
    """
    store.connect_mcp(
        user_id=_OWNER,
        label="DeepWiki",
        template="deepwiki",
        url="https://wiki.example.com/mcp",
        tools=("read_wiki",),
    )
    store.connect_mcp(user_id=_OWNER, label="GitHub", tools=("get_issue",))
    config = (await client.get("/v1/agent/config")).json()

    assert [entry["label"] for entry in config["mcp_connections"]] == ["DeepWiki", "GitHub"]
    # The composer is told the same list the create path enforces, so a picker
    # cannot offer a runtime that would then be refused.
    assert config["mcp_runtimes"] == ["claude-code", "codex", "kilo", "opencode", "pi"]
    assert sorted(config["plugin_runtimes"]) == ["claude-code", "codex", "kilo", "opencode", "pi"]
    assert [entry["tool_count"] for entry in config["mcp_connections"]] == [1, 1]
    assert all(entry["available"] for entry in config["mcp_connections"])
    assert _UPSTREAM_TOKEN not in json.dumps(config)
    assert "mcp.example.com" not in json.dumps(config)


async def test_the_composer_offers_only_what_this_user_connected(
    client: AsyncClient, store: FakeStorage
):
    """The property the deployment catalogue could not have.

    Both users exist and both could connect the same service; only the
    connection differs, and it is the only thing that may decide what the owner
    sees.
    """
    store.connect_mcp(user_id=_OTHER, token="ghu_someone_elses_secret")

    config = (await client.get("/v1/agent/config")).json()
    assert config["mcp_connections"] == []
    assert "ghu_someone_elses_secret" not in json.dumps(config)


async def test_omitting_bindings_means_no_connections(client: AsyncClient, store: FakeStorage):
    """Connecting is availability, not consent.

    A task that says nothing about tools gets none: a job that silently received
    every connection its owner has would be spending credentials the prompt was
    never written around.
    """
    store.connect_mcp(user_id=_OWNER)
    created = await client.post("/v1/agent/jobs", json=_job_body())

    assert created.status_code == 201, created.text
    assert created.json()["mcp_bindings"] == []


async def test_an_explicit_empty_list_means_no_connections(client: AsyncClient, store: FakeStorage):
    """Choosing none is a real choice and must not be read as "unset"."""
    store.connect_mcp(user_id=_OWNER)
    created = await client.post("/v1/agent/jobs", json=_job_body(mcp_bindings=[]))

    assert created.status_code == 201, created.text
    assert created.json()["mcp_bindings"] == []


async def test_a_job_records_the_bindings_it_was_created_with(
    client: AsyncClient, store: FakeStorage
):
    """The response names the connection by alias and label, never by address."""
    row = store.connect_mcp(user_id=_OWNER, label="GitHub", tools=("get_issue",))
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )

    assert created.status_code == 201, created.text
    assert created.json()["mcp_bindings"] == [
        {"connection_id": row["id"], "alias": "github", "label": "GitHub"}
    ]
    assert "mcp.example.com" not in created.text


async def test_a_selection_is_stamped_against_the_snapshot_it_was_chosen_from(
    client: AsyncClient, store: FakeStorage
):
    """An approval is an approval *of something*.

    The fingerprint records the tool's description and schema at the moment the
    owner allowed it. Without it, a server that later redefines the tool would
    inherit the old approval for a different thing wearing the same name.
    """
    row = store.connect_mcp(user_id=_OWNER, tools=("get_issue",))
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )
    assert created.status_code == 201, created.text

    (binding,) = store.mcp_bindings[created.json()["thread_id"]]
    rule = binding["rules"]["get_issue"]
    assert rule["policy"] == "allow"
    assert len(rule["fingerprint"]) == 64


@pytest.mark.parametrize("explicit", [False, True])
async def test_new_jobs_cannot_store_ambiguous_mcp_aliases(client, store, explicit):
    first = store.connect_mcp(user_id=_OWNER, tools=("get_issue",))
    second = store.connect_mcp(user_id=_OWNER, tools=("get_issue",))
    selections = [_binding(first["id"]), _binding(second["id"])]
    for selection in selections:
        selection["alias"] = "github" if explicit else None
    created = await client.post("/v1/agent/jobs", json=_job_body(mcp_bindings=selections))
    if explicit:
        assert created.status_code == 400
        assert "more than one connection" in created.text
    else:
        assert created.status_code == 201, created.text
        aliases = [binding["alias"] for binding in created.json()["mcp_bindings"]]
        assert len(aliases) == len(set(aliases)) == 2


@pytest.mark.parametrize("surface", ["resources", "prompts"])
@pytest.mark.parametrize("policy", ["allow", "confirm", "deny", "invalid"])
async def test_new_jobs_honor_mcp_resource_and_prompt_only_selections(
    client, store, surface, policy
):
    connection = store.connect_mcp(user_id=_OWNER)
    store.mcp_capabilities[connection["id"]][surface] = [{"name": "docs"}]
    created = await client.post(
        "/v1/agent/jobs",
        json=_job_body(
            mcp_bindings=[
                {
                    "connection_id": connection["id"],
                    "tools": {},
                    surface: {"docs": policy},
                }
            ]
        ),
    )
    if policy == "invalid":
        assert created.status_code == 400
        assert store.mcp_bindings == {}
    else:
        assert created.status_code == 201, created.text
        binding = store.mcp_bindings[created.json()["thread_id"]][0]
        assert binding["enabled"] is (policy != "deny")
        assert binding["resource_rules" if surface == "resources" else "prompt_rules"] == {
            "docs": policy
        }


async def test_naming_a_connection_the_caller_does_not_own_is_refused_at_creation(
    client: AsyncClient, store: FakeStorage
):
    """Refused in the same words as one that does not exist.

    A caller must not be able to tell "nobody has this" from "somebody else has
    this" -- the difference is another user's account state, and the reply to a
    request that may not have it is the same either way.
    """
    theirs = store.connect_mcp(user_id=_OTHER, token="ghu_someone_elses_secret")

    owned = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(theirs["id"])])
    )
    missing = await client.post("/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(9999)]))

    assert owned.status_code == 400
    assert (
        owned.json()["detail"]["error"]["message"].replace(str(theirs["id"]), "9999")
        == missing.json()["detail"]["error"]["message"]
    )
    assert "ghu_someone_elses_secret" not in owned.text


@pytest.mark.parametrize("runtime", ["codex", "kilo", "claude-code", "opencode", "pi"])
async def test_every_supported_runtime_accepts_mcp(
    client: AsyncClient, store: FakeStorage, runtime
):
    """The composer and submission API promise the same MCP experience."""
    row = store.connect_mcp(user_id=_OWNER)
    created = await client.post(
        "/v1/agent/jobs",
        json=_job_body(runtime=runtime, mcp_bindings=[_binding(row["id"])]),
    )
    assert created.status_code == 201
    assert created.json()["mcp_bindings"][0]["connection_id"] == row["id"]


async def test_an_unknown_tool_selection_is_refused_at_creation(
    client: AsyncClient, store: FakeStorage
):
    """A selection naming a tool the server does not offer is a refusal.

    Accepting it would create a job around a tool the agent turns out not to
    have, which does not fail -- it quietly produces the wrong work.
    """
    row = store.connect_mcp(user_id=_OWNER, tools=("get_issue",))
    created = await client.post(
        "/v1/agent/jobs",
        json=_job_body(mcp_bindings=[_binding(row["id"], tools={"delete_repo": "allow"})]),
    )

    assert created.status_code == 400
    assert "delete_repo" in created.text
    assert "does not offer" in created.text


async def test_a_connection_that_cannot_be_used_says_why_instead_of_vanishing(
    client: AsyncClient, store: FakeStorage
):
    """Connected and unusable is a real state, and both surfaces must name it.

    Omitting it from the composer leaves its owner with a picker that silently
    drops the server they are looking for; letting a job name it puts a dead
    credential behind tools the prompt was written around.
    """
    row = store.connect_mcp(
        user_id=_OWNER,
        label="GitHub",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    config = (await client.get("/v1/agent/config")).json()
    (entry,) = config["mcp_connections"]
    assert entry["connection_id"] == row["id"]
    assert entry["available"] is False
    assert "expired" in entry["error"]

    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )
    assert created.status_code == 400
    assert "expired" in created.text


async def test_disconnecting_clears_the_secret_and_bumps_the_epoch(
    client: AsyncClient, store: FakeStorage
):
    """A tombstone that still holds a token is not a disconnect.

    The row is kept so a binding can say *this was disconnected* rather than
    *this never existed*, and its versions move so any approval made against the
    old credential stops resolving instead of quietly continuing.
    """
    row = store.connect_mcp(user_id=_OWNER)
    other = store.connect_mcp(user_id=_OTHER, token="ghu_someone_elses_secret")
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )
    assert created.status_code == 201, created.text

    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    assert row["access_token_ciphertext"] is None
    assert row["refresh_token_ciphertext"] is None
    assert row["deleted_at"] is not None
    assert row["status"] == "disabled"
    assert row["authorization_epoch"] == 2
    # Only the caller's row: a disconnect without its owner scope would take
    # everyone else's connection with it.
    assert other["access_token_ciphertext"] is not None
    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=other["id"], user_id=_OWNER)
        is False
    )
    assert await store.mcp.list_mcp_connections(user_id=_OWNER) == []
    assert len(await store.mcp.list_mcp_connections(user_id=_OWNER, include_deleted=True)) == 1


async def test_a_follow_up_runs_when_an_account_connection_was_disconnected(
    client: AsyncClient, store: FakeStorage
):
    """One disconnected integration must not prevent unrelated conversation work."""
    row = store.connect_mcp(user_id=_OWNER, label="GitHub")
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )
    job_id = created.json()["id"]
    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    follow_up = await _next_turn(client, store, job_id, {"prompt": "again"})

    assert follow_up["state"] == "queued"
    from cloud_agent.mcp.registry import servers_for_thread

    assert await servers_for_thread(store.mcp, thread_id=created.json()["thread_id"]) == {}


async def test_a_follow_up_still_runs_while_its_thread_s_connection_is_connected(
    client: AsyncClient, store: FakeStorage
):
    """The check must not refuse the ordinary case it runs on."""
    row = store.connect_mcp(user_id=_OWNER)
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )

    follow_up = await _next_turn(client, store, created.json()["id"], {"prompt": "again"})

    assert follow_up["state"] == "queued"


async def test_disconnecting_leaves_a_running_jobs_binding_list_alone(
    client: AsyncClient, store: FakeStorage
):
    """A job keeps the connection it was created with and loses the credential.

    Nothing hunts down live jobs on disconnect: bindings are read per call, so
    the job's own list stays as it was and each call fails the usability check
    -- which is what the follow-up case above pins at the HTTP surface.
    """
    row = store.connect_mcp(user_id=_OWNER)
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )
    job_id = created.json()["id"]
    assert created.json()["mcp_bindings"][0]["connection_id"] == row["id"]

    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    assert job["mcp_bindings"] == [
        {"connection_id": row["id"], "alias": "github", "label": "GitHub"}
    ]
    assert row["access_token_ciphertext"] is None


async def test_the_integrations_page_sees_every_connection_and_its_reason(
    client: AsyncClient, store: FakeStorage
):
    """One request for one page, and an unusable connection is reported not hidden.

    The aggregate endpoint is where the settings screen learns both halves at
    once. A connection that has stopped working must appear with its reason:
    a page that silently omits it leaves its owner looking for a server that is
    right there, and the separate per-connection API is where they act on it.
    """
    working = store.connect_mcp(user_id=_OWNER, label="GitHub")
    store.connect_mcp(user_id=_OTHER, label="Someone else's", template="sentry")
    broken = store.connect_mcp(user_id=_OWNER, label="Broken", template="wiki")

    response = await client.get("/v1/agent/integrations")

    assert response.status_code == 200
    rows = {entry["name"]: entry for entry in response.json()["mcp"]}
    assert set(rows) == {"GitHub", "Broken"}
    assert rows["GitHub"]["connected"] is True
    assert rows["GitHub"]["configured"] is True
    assert rows["GitHub"]["error"] is None
    assert rows["Broken"]["connected"] is True
    del working, broken


async def test_the_claim_hands_the_runner_the_job_s_bindings(
    client: AsyncClient, store: FakeStorage
):
    """The runner passes aliases to the adapter; it never learns the addresses."""
    github = store.connect_mcp(user_id=_OWNER, label="GitHub", tools=("get_issue",))
    wiki = store.connect_mcp(
        user_id=_OWNER,
        label="DeepWiki",
        template="deepwiki",
        url="https://wiki.example.com/mcp",
        tools=("read_wiki",),
    )
    created = await client.post(
        "/v1/agent/jobs",
        json=_job_body(
            mcp_bindings=[
                _binding(github["id"]),
                _binding(wiki["id"], alias="deepwiki", tools={"read_wiki": "allow"}),
            ]
        ),
    )
    assert created.status_code == 201, created.text

    app = _build_app(store, claim_auth=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as runner:
        claimed = await runner.post(
            "/v1/agent/worker/claim", json={"worker_id": "w-1", "lease_ttl_seconds": 60}
        )
    assert claimed.status_code == 200
    body = claimed.json()
    # Aliases, not summaries: the runtime builds the relay URL's path segment
    # from each name, so a dict here would either fail the schema or address a
    # server that does not exist.
    assert body["mcp_bindings"] == ["deepwiki", "github"]
    assert "mcp.example.com" not in json.dumps(body)
    assert "wiki.example.com" not in json.dumps(body)


async def test_the_claim_hands_over_the_mcp_credential_too(client: AsyncClient):
    """**The whole MCP chain, measured across the claim boundary.**

    This is where it broke and nothing noticed. `worker_claim` minted an
    `mcp`-scoped token and computed the relay address, and
    `WorkerClaimResponse` declared neither — so pydantic dropped both on the
    way out and every sandbox got an empty MCP credential.

    The relay's own tests all passed, because they construct a token and call
    the endpoint directly. They never cross this boundary, which is exactly
    where the two halves stopped agreeing: a field with a writer, a reader, and
    a schema in between that knew about neither.
    """
    await _create_job(client)

    body = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()

    assert body["mcp_token"], "the sandbox was handed no MCP credential"

    from cloud_agent.tokens import SCOPE_MCP, parse_worker_token

    # Scoped, and not the same credential as either of the other two.
    assert parse_worker_token(body["mcp_token"])["scope"] == SCOPE_MCP
    assert body["mcp_token"] != body["worker_token"]
    assert body["mcp_token"] != body["sandbox_token"]


async def test_the_claim_names_no_mcp_relay_address(client: AsyncClient, monkeypatch):
    """The relay is the claiming host's own, so the control plane names none.

    It used to hand one over from `AGENT_SANDBOX_MCP_URL`, a copy of every
    host's tunnel alias that nothing kept in step with the hosts. Left unset,
    every turn whose owner had connected a tool failed at its resident launch.
    A control plane whose environment still carries the variable must not
    bring the copy back.
    """
    monkeypatch.setenv("AGENT_SANDBOX_MCP_URL", "http://agent-mcp:8000")
    await _create_job(client)

    body = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()

    assert "mcp_base_url" not in body
    assert "agent-mcp" not in json.dumps(body)


async def test_a_job_created_with_an_alias_stores_the_canonical_id(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """**Validated as an alias, stored as an alias — the shape of the bug.**

    The gateway mints a grant against canonical ids. A job that recorded
    `legacy-name` produced a grant whose `allowed_models` matched nothing, so
    the job was created successfully and died at its first inference call with
    a 404 for a model that plainly exists.

    Resolution translates once; everything after it works in canonical ids.
    """
    monkeypatch.setitem(_FakeGateway.aliases, "legacy-name", _TEST_MODELS[0])

    await _create_job(client, model="legacy-name")

    (job,) = list(store.jobs.values())
    assert job["model"] == _TEST_MODELS[0], "the alias was stored instead of the canonical id"


async def test_the_owner_sees_what_their_turn_consumed(client: AsyncClient, store: FakeStorage):
    """The footer's numbers, end to end: runner posts, owner reads."""
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}

    ack = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/resources",
        json={
            "cpu_usec": 18_200_000,
            "mem_peak_bytes": 1_288_490_188,
            "net_rx_bytes": 35_651_584,
            "net_tx_bytes": 2_097_152,
            "sample_count": 42,
            "coverage": 1.0,
            "sampler_version": "1",
        },
        headers=auth,
    )
    assert ack.status_code == 200

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    assert job["resources"]["cpu_usec"] == 18_200_000
    assert job["resources"]["mem_peak_bytes"] == 1_288_490_188
    assert job["resources"]["coverage"] == 1.0


async def test_shared_member_accounting_refuses_container_attribution(
    client: AsyncClient,
):
    """The semantic version is a server-enforced promise, not runner trust.

    Fork siblings can overlap in one container.  Accepting either an aggregate
    delta or a periodic tool sample under ``shared-member-v1`` would silently
    charge one chat for the other's work.
    """
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    endpoint = f"/v1/agent/worker/jobs/{job_id}/resources"
    base = {
        "sample_count": 0,
        "coverage": 0.0,
        "sampler_version": "shared-member-v1",
    }

    invalid = [
        {**base, "cpu_usec": 1},
        {**base, "sample_count": 1},
        {
            **base,
            "tool_calls": [
                {
                    "call_seq": 1,
                    "tool_use_id": "toolu_1",
                    "tool_name": "Bash",
                    "sample_count": 2,
                    "measured_span_ms": 1,
                    "cpu_usec_exclusive": 1,
                }
            ],
        },
    ]
    for payload in invalid:
        refused = await client.post(endpoint, json=payload, headers=auth)
        assert refused.status_code == 422, refused.text

    accepted = await client.post(
        endpoint,
        json={
            **base,
            "tool_time_ms": 25,
            "tool_calls": [
                {
                    "call_seq": 1,
                    "tool_use_id": "toolu_1",
                    "tool_name": "Bash",
                    "duration_ms": 25,
                    "sample_count": 0,
                    "overlap_n": 1,
                }
            ],
        },
        headers=auth,
    )
    assert accepted.status_code == 200, accepted.text


async def test_resource_sequences_and_process_references_are_consistent(
    client: AsyncClient,
):
    """Malformed worker evidence is a 422, never a database primary-key 500."""
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    endpoint = f"/v1/agent/worker/jobs/{job_id}/resources"
    call = {
        "call_seq": 1,
        "tool_use_id": "toolu_1",
        "tool_name": "Bash",
    }
    process = {
        "process_seq": 1,
        "uid": 11003,
        "pid": 42,
        "tgid": 42,
        "comm": "tiny",
        "started_offset_ms": 1,
        "tool_call_seqs": [1],
    }
    base = {
        "sampler_version": "shared-member-v1",
        "process_observer": "kata-guest-ebpf-v1",
    }
    invalid = [
        {**base, "tool_calls": [call, {**call, "tool_use_id": "toolu_2"}]},
        {
            **base,
            "tool_calls": [{**call, "process_count": 2}],
            "processes": [process, {**process, "pid": 43, "tgid": 43}],
        },
        {
            **base,
            "tool_calls": [{**call, "process_count": 1}],
            "processes": [{**process, "tool_call_seqs": [1, 1]}],
        },
        {**base, "tool_calls": [call], "processes": [{**process, "tool_call_seqs": [2]}]},
        {**base, "tool_calls": [{**call, "process_count": 1}]},
    ]

    for payload in invalid:
        refused = await client.post(endpoint, json=payload, headers=auth)
        assert refused.status_code == 422, refused.text


async def test_per_call_rows_survive_the_wire_in_both_directions(
    client: AsyncClient, store: FakeStorage
):
    """Posted by the runner, read by the owner, over the real request models.

    Written against the wire rather than the store because that is where this
    kind of field is lost: a model missing the attribute accepts the post,
    answers 200, and drops it silently — which no test that hands a dict to
    the store can see.
    """
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}

    ack = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/resources",
        json={
            "cpu_usec": 5_000_000,
            "sample_count": 12,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_time_ms": 9_543,
            "tool_calls": [
                {
                    "call_seq": 1,
                    "tool_use_id": "toolu_long",
                    "tool_name": "Bash",
                    "timing_source": "exact",
                    "duration_ms": 9_523,
                    "sample_count": 9,
                    "overlap_n": 1,
                    "measured_span_ms": 8_000,
                    "cpu_usec_exclusive": 4_400_000,
                    "mem_peak_exclusive_bytes": 270_000_000,
                    "process_count": 1,
                    "process_cpu_usec": 15_000,
                    "process_mem_peak_bytes": 2_228_224,
                    "process_io_read_bytes": 0,
                    "process_io_write_bytes": 8_388_608,
                    "process_resources_partial": True,
                },
                {
                    "call_seq": 2,
                    "tool_use_id": "toolu_short",
                    "tool_name": "Read",
                    "duration_ms": 20,
                    "sample_count": 0,
                    "overlap_n": 1,
                },
            ],
            "tool_calls_truncated": False,
            "process_observer": "kata-guest-ebpf-v1",
            "process_observation_complete": True,
            "process_events_lost": 0,
            "processes": [
                {
                    "process_seq": 1,
                    "uid": 11003,
                    "pid": 42,
                    "tgid": 42,
                    "ppid": 7,
                    "comm": "tiny",
                    "executable": "/bin/tiny",
                    "started_offset_ms": 12,
                    "duration_ms": 1,
                    "exit_code": 0,
                    "observed_fork": True,
                    "observed_exec": True,
                    "observed_exit": True,
                    "tool_call_seqs": [1],
                    "cpu_usec": 15_000,
                    "mem_peak_bytes": 2_228_224,
                    "io_read_bytes": 0,
                    "io_write_bytes": 8_388_608,
                    "io_rchar_bytes": 8_390_683,
                    "io_wchar_bytes": 8_388_701,
                }
            ],
        },
        headers=auth,
    )
    assert ack.status_code == 200, ack.text

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    assert job["telemetry_attempt_id"] == claim["attempt_id"]
    resources = job["resources"]
    assert resources["tool_calls_truncated"] is False
    assert resources["tool_time_ms"] == 9_543
    long_call, short_call = resources["tool_calls"]
    assert long_call["tool_name"] == "Bash"
    assert long_call["timing_source"] == "exact"
    assert short_call["timing_source"] == "message", "an unmarked call claimed precision"
    assert long_call["cpu_usec_exclusive"] == 4_400_000
    assert long_call["measured_span_ms"] == 8_000
    assert long_call["process_count"] == 1
    assert long_call["process_cpu_usec"] == 15_000
    assert long_call["process_mem_peak_bytes"] == 2_228_224
    assert long_call["process_io_write_bytes"] == 8_388_608
    assert long_call["process_resources_partial"] is True
    assert short_call["process_cpu_usec"] is None
    assert short_call["process_resources_partial"] is False
    assert resources["process_observer"] == "kata-guest-ebpf-v1"
    assert resources["process_observation_complete"] is True
    assert resources["process_events_lost"] == 0
    (process,) = resources["processes"]
    assert process["executable"] == "/bin/tiny"
    assert process["duration_ms"] == 1
    assert process["tool_call_seqs"] == [1]
    assert process["cpu_usec"] == 15_000
    assert process["mem_peak_bytes"] == 2_228_224
    assert process["io_write_bytes"] == 8_388_608
    assert process["io_rchar_bytes"] == 8_390_683
    # The 97 % case, all the way to the reader: a row, a duration, and no
    # counters at all. A zero here would be a number nobody measured.
    assert short_call["sample_count"] == 0
    assert short_call["cpu_usec_exclusive"] is None
    assert short_call["cpu_usec_shared"] is None
    assert short_call["mem_peak_exclusive_bytes"] is None


async def test_a_turn_with_no_per_call_rows_reads_as_an_empty_list(
    client: AsyncClient, store: FakeStorage
):
    """Every turn recorded before this shipped, and every unmeasured one."""
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/resources",
        json={"sample_count": 0, "coverage": 0.0, "sampler_version": "2"},
        headers=auth,
    )

    resources = (await client.get(f"/v1/agent/jobs/{job_id}")).json()["resources"]
    assert resources["tool_calls"] == []
    assert resources["tool_calls_truncated"] is False
    # Null, not zero: a turn nobody measured did not spend no time in tools.
    assert resources["tool_time_ms"] is None


async def test_more_calls_than_the_cap_are_refused_rather_than_truncated_server_side(
    client: AsyncClient, store: FakeStorage
):
    """The runner caps and flags; the server must not quietly cap a second time."""
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}

    response = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/resources",
        json={
            "sample_count": 1,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [
                {"call_seq": i + 1, "tool_use_id": f"t{i}", "tool_name": "Read"} for i in range(501)
            ],
        },
        headers=auth,
    )
    assert response.status_code == 422


async def test_a_rollup_is_the_same_window_said_twice_not_two_windows(
    client: AsyncClient, store: FakeStorage
):
    """Retrying the post must not double a turn's consumption."""
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    body = {"cpu_usec": 1_000, "sample_count": 5, "coverage": 1.0, "sampler_version": "1"}

    await client.post(f"/v1/agent/worker/jobs/{job_id}/resources", json=body, headers=auth)
    await client.post(f"/v1/agent/worker/jobs/{job_id}/resources", json=body, headers=auth)

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    assert job["resources"]["cpu_usec"] == 1_000, "a retried post doubled the turn's CPU"


async def test_a_superseded_attempt_cannot_relabel_what_the_live_one_used(
    client: AsyncClient, store: FakeStorage
):
    """Fenced like every other worker write."""
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    # A takeover moves the fence to a new attempt generation.
    store.live_fence = (101, 2)

    refused = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/resources",
        json={"cpu_usec": 999, "sample_count": 1, "coverage": 1.0, "sampler_version": "1"},
        headers=auth,
    )
    assert refused.status_code == 409

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    assert job["resources"] is None


def _turn_resources_body(
    *,
    report_seq: int = 1,
    state: str = "final",
    measurement_id: str = "00000000-0000-4000-8000-000000000001",
    cpu_usec: int = 80_000_000,
    observed_through_ms: int = 130_000,
) -> dict[str, Any]:
    """One canonical ``member_execution_window`` body, as the guest posts it."""
    return {
        "schema_version": 1,
        "measurement_id": measurement_id,
        "report_seq": report_seq,
        "state": state,
        "scope": "member_execution_window",
        "source": "kata-guest-member-window-v1",
        "window": {
            "duration_ms": 130_000,
            "observed_through_ms": observed_through_ms,
            "open_scan_ms": 12,
            "close_scan_ms": 15,
        },
        "cpu": {
            "status": "available",
            "method": "live_and_exit_task_counters",
            "usec": cpu_usec,
            "reason_codes": [],
        },
        "memory": {
            "status": "available",
            "method": "sampled_member_pss",
            "peak_bytes": 1_288_490_189,
            "sample_interval_ms": 1000,
            "valid_samples": 131,
            "expected_samples": 131,
            "max_gap_ms": 1000,
            "reason_codes": [],
        },
        "storage_io": {
            "status": "unavailable",
            "method": "task_io_accounting",
            "read_bytes": None,
            "write_bytes": None,
            "reason_codes": ["filesystem_attribution_unverified"],
        },
    }


async def test_a_turn_resource_snapshot_is_acknowledged_and_its_retry_too(
    client: AsyncClient, store: FakeStorage
):
    """An acknowledged checkpoint whose response was lost must be safe to resend."""
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    endpoint = f"/v1/agent/worker/jobs/{job_id}/turn-resources"
    body = _turn_resources_body(report_seq=2)

    first = await client.post(endpoint, json=body, headers=auth)
    retry = await client.post(endpoint, json=body, headers=auth)

    assert first.status_code == 200
    assert first.json()["ok"] is True
    assert retry.status_code == 200
    assert retry.json()["ok"] is True
    assert store.turn_summaries[claim["attempt_id"]]["seq"] == 2


async def test_a_stale_or_disagreeing_snapshot_is_refused_with_409(
    client: AsyncClient, store: FakeStorage
):
    """``conflict`` is not ``lease_lost``: the worker keeps going, the post does not."""
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    endpoint = f"/v1/agent/worker/jobs/{job_id}/turn-resources"
    assert (
        await client.post(endpoint, json=_turn_resources_body(report_seq=5), headers=auth)
    ).status_code == 200

    stale = await client.post(endpoint, json=_turn_resources_body(report_seq=4), headers=auth)
    disagreeing = await client.post(
        endpoint, json=_turn_resources_body(report_seq=5, cpu_usec=1), headers=auth
    )

    assert stale.status_code == 409
    assert stale.json()["detail"]["error"]["type"] == "turn_summary_conflict"
    assert disagreeing.status_code == 409
    assert disagreeing.json()["detail"]["error"]["type"] == "turn_summary_conflict"
    assert store.turn_summaries[claim["attempt_id"]]["seq"] == 5


async def test_a_superseded_attempt_cannot_file_a_turn_resource_snapshot(
    client: AsyncClient, store: FakeStorage
):
    """The fence is the same one every other worker write uses."""
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    store.live_fence = (101, 2)

    refused = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/turn-resources",
        json=_turn_resources_body(),
        headers=auth,
    )

    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "lease_lost"
    assert store.turn_summaries == {}


async def test_an_oversized_turn_resource_snapshot_is_refused_before_parsing(
    client: AsyncClient, store: FakeStorage
):
    """The 16 KiB cap is on the bytes that crossed the wire, not on the model.

    The body carries a valid snapshot plus one long unknown field, so a server
    that validated first and bounded second would answer 422 or 200 here. The
    only correct answer is 413: the contract bounds the payload itself.
    """
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    body = _turn_resources_body()
    body["padding"] = "x" * (16 * 1024)

    refused = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/turn-resources", json=body, headers=auth
    )

    assert refused.status_code == 413
    assert refused.json()["detail"]["error"]["type"] == "payload_too_large"
    assert store.turn_summaries == {}


async def test_a_turn_resource_response_never_echoes_body_identity(
    client: AsyncClient, store: FakeStorage
):
    """Identity comes from the signed claims, so the body cannot name anything.

    A body that tries to select another attempt must be stored against the
    caller's own fenced attempt and answered with no identity at all.
    """
    job_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    body = _turn_resources_body()
    body.update({"attempt_id": 999, "lease_generation": 7, "job_id": "ajob_someone_else"})

    response = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/turn-resources", json=body, headers=auth
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert set(store.turn_summaries) == {claim["attempt_id"]}
    assert "job_id" not in response.text
    assert "attempt_id" not in response.text


async def test_an_unmeasured_turn_reports_nothing_rather_than_zero(
    client: AsyncClient, store: FakeStorage
):
    """The distinction the whole feature rests on.

    A turn nobody measured has no resources object at all. Reading zeros here
    would be indistinguishable from a turn that genuinely consumed nothing —
    and the reader would have no way to tell which they were looking at.
    """
    job_id = await _create_job(client)

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()

    assert job["telemetry_attempt_id"] is None
    assert job["resources"] is None
    assert job["continuity_mode"] is None
    assert job["started_at"] is None
    assert job["finished_at"] is None
    assert job["duration_ms"] is None


@pytest.mark.parametrize("state", ["running", "failed"])
async def test_job_timing_does_not_invent_a_missing_attempt_finish(
    client: AsyncClient, store: FakeStorage, state: str
):
    """A job update or terminal state cannot supply an unrecorded end time."""
    job_id = await _create_job(client)
    started = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    store.jobs[job_id].update(
        state=state,
        created_at=started - timedelta(minutes=1),
        updated_at=started + timedelta(minutes=5),
    )
    store.attempt_telemetry[job_id] = [
        {
            "attempt_id": 100,
            "attempt_no": 1,
            "started_at": started,
            "finished_at": None,
        }
    ]

    for query in ("", "?include_resources=false"):
        response = await client.get(f"/v1/agent/jobs/{job_id}{query}")
        assert response.status_code == 200
        job = response.json()
        assert job["started_at"] == started.isoformat()
        assert job["finished_at"] is None
        assert job["duration_ms"] is None


async def test_job_list_and_thread_do_not_substitute_job_dates_for_execution_timing(
    client: AsyncClient, store: FakeStorage
):
    """Paths without a telemetry read leave the execution window unknown."""
    job_id = await _create_job(client)
    started = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    store.jobs[job_id].update(
        state="succeeded",
        created_at=started - timedelta(minutes=1),
        updated_at=started + timedelta(minutes=5),
    )
    store.attempt_telemetry[job_id] = [
        {
            "attempt_id": 100,
            "attempt_no": 1,
            "started_at": started,
            "finished_at": started + timedelta(seconds=9),
        }
    ]

    for path in ("/v1/agent/jobs", f"/v1/agent/jobs/{job_id}/thread"):
        response = await client.get(path)
        assert response.status_code == 200
        job = response.json()["jobs"][0]
        assert job["started_at"] is None
        assert job["finished_at"] is None
        assert job["duration_ms"] is None


async def test_job_detail_keeps_each_retry_s_telemetry_on_its_attempt(
    client: AsyncClient, store: FakeStorage
):
    """Selecting an old transcript must never show the retry's measurements."""
    job_id = await _create_job(client)
    started = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    newest_started = started + timedelta(seconds=5)
    store.attempt_telemetry = {
        job_id: [
            {
                "attempt_id": 100,
                "attempt_no": 1,
                "continuity_mode": "replay",
                "context_status": "lost",
                "started_at": started,
                "finished_at": started + timedelta(seconds=3),
                "sampler_version": "2",
                "cpu_usec": 1_000_000,
                "sample_count": 3,
                "coverage": 1.0,
            },
            {
                "attempt_id": 101,
                "attempt_no": 2,
                "continuity_mode": "warm_native",
                "context_status": "restored",
                "started_at": newest_started,
                "finished_at": newest_started + timedelta(seconds=9),
                "sampler_version": "2",
                "cpu_usec": 7_000_000,
                "sample_count": 9,
                "coverage": 1.0,
            },
        ]
    }

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()

    first, second = job["attempt_telemetry"]
    assert (first["attempt_id"], first["attempt_no"]) == (100, 1)
    assert first["duration_ms"] == 3_000
    assert second["duration_ms"] == 9_000
    assert "resources" not in first and "resources" not in second
    # Existing clients still receive the newest attempt at the top level.
    assert job["telemetry_attempt_id"] == 101
    assert job["started_at"] == newest_started.isoformat()
    assert job["finished_at"] == (newest_started + timedelta(seconds=9)).isoformat()
    assert job["duration_ms"] == 9_000
    assert job["resources"]["cpu_usec"] == 7_000_000

    lightweight = (await client.get(f"/v1/agent/jobs/{job_id}?include_resources=false")).json()
    assert lightweight["started_at"] == job["started_at"]
    assert lightweight["finished_at"] == job["finished_at"]
    assert lightweight["duration_ms"] == job["duration_ms"]

    old = (await client.get(f"/v1/agent/jobs/{job_id}/attempts/100/telemetry")).json()
    assert old["attempt_id"] == 100
    assert old["duration_ms"] == 3_000
    assert old["resources"]["cpu_usec"] == 1_000_000


async def test_retry_picker_polls_only_summaries_and_fetches_one_detail(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """Adding retries must not multiply the polled tool/process payload."""
    job_id = await _create_job(client)
    store.attempt_telemetry[job_id] = [
        {
            "attempt_id": attempt_id,
            "attempt_no": attempt_id,
            "sampler_version": "2",
            "sample_count": 1,
            "coverage": 1.0,
            "cpu_usec": attempt_id,
            "tool_calls": [
                {"call_seq": seq + 1, "tool_use_id": f"t{seq}", "tool_name": "Read"}
                for seq in range(500)
            ],
        }
        for attempt_id in range(1, 13)
    ]
    read_detail = AsyncMock(wraps=store.job_turn_telemetry)
    monkeypatch.setattr(store, "job_turn_telemetry", read_detail)
    # The real store already returns scalar summaries, independently of the
    # detail reader. Keep the fake faithful to that separate query.
    summaries = [
        {key: row[key] for key in ("attempt_id", "attempt_no")}
        for row in store.attempt_telemetry[job_id]
    ]
    monkeypatch.setattr(store, "job_attempts_telemetry", AsyncMock(return_value=summaries))
    for _ in range(2):
        response = await client.get(f"/v1/agent/jobs/{job_id}?include_resources=false")
        assert response.status_code == 200
        data = response.json()
        assert len(data["attempt_telemetry"]) == 12
        assert data["resources"] is None
        assert b'"tool_calls"' not in response.content
        assert b'"processes"' not in response.content
        assert len(response.content) < 5_000
    read_detail.assert_not_awaited()
    # Supply the full row only when the singular reader is called.
    read_detail.side_effect = lambda **kwargs: store.attempt_telemetry[job_id][
        kwargs["attempt_id"] - 1
    ]
    detail = await client.get(f"/v1/agent/jobs/{job_id}/attempts/3/telemetry")
    assert detail.status_code == 200
    assert len(detail.json()["resources"]["tool_calls"]) == 500
    read_detail.assert_awaited_once_with(job_id=job_id, attempt_id=3)


async def test_attempt_telemetry_is_fenced_to_the_owned_job(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    another = await _create_job(client)
    store.attempt_telemetry[another] = [{"attempt_id": 999, "attempt_no": 1}]
    assert (await client.get(f"/v1/agent/jobs/{job_id}/attempts/999/telemetry")).status_code == 404
    store.jobs[another]["user_id"] = "someone-else"
    assert (await client.get(f"/v1/agent/jobs/{another}/attempts/999/telemetry")).status_code == 404


# ── Reasoning effort ───────────────────────────────────────────────────


async def test_effort_is_offered_only_where_runtime_and_model_both_deliver(client: AsyncClient):
    """The composer is told both halves; it offers their intersection."""
    config = (await client.get("/v1/agent/config")).json()

    # claude-code can express effort; the Tier 2 runtimes cannot, and are
    # absent rather than listed with an empty vocabulary.
    # The raw capability, still the CLI's own vocabulary — the composer
    # narrows it to the platform list rather than the backend pre-chewing it.
    assert config["runtime_reasoning_efforts"]["claude-code"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert config["runtime_reasoning_efforts"]["codex"][:3] == ["none", "minimal", "low"]
    assert config["runtime_reasoning_efforts"]["pi"] == ["minimal", "low", "medium", "high"]
    assert config["model_reasoning_efforts"] == {"glm-5.1": ["low", "high", "max"]}


async def test_a_turn_records_the_effort_it_was_created_with(client: AsyncClient):
    response = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "reasoning_effort": "high",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["reasoning_effort"] == "high"


async def test_a_turn_without_an_effort_is_auto(client: AsyncClient):
    """Auto is null, and null is what every turn before this control reports."""
    job_id = await _create_job(client)

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()

    assert job["reasoning_effort"] is None


async def test_a_level_outside_the_platform_list_is_refused(client: AsyncClient):
    """``xhigh`` is a real claude-code level, and not one this platform offers.

    Refused rather than clamped to a neighbour: a turn served at a depth
    nobody chose, while reporting the one they did, is the same lie as a flag
    that resolves to nothing.
    """
    response = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "reasoning_effort": "xhigh",
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]["error"]
    assert detail["type"] == "reasoning_effort_not_available"
    # The refusal names what *is* available, so the caller can retry once.
    assert "low, medium, high, max" in detail["message"]


async def test_the_platform_list_is_offered_whatever_the_model_documents(
    client: AsyncClient,
):
    """The model entry is a yes/no, not a vocabulary.

    Per-model domains were withdrawn because the providers' docs disagreed
    with their own APIs, so a level the platform offers is accepted for any
    model that takes the parameter at all — `medium` here, which this
    fixture's model does not list.
    """
    response = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "reasoning_effort": "medium",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["reasoning_effort"] == "medium"


async def test_an_effort_on_a_model_that_takes_none_is_refused(client: AsyncClient):
    response = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "qwen-next",
            "reasoning_effort": "high",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["message"].endswith("Available: none.")


async def test_a_follow_up_inherits_the_efforts_of_the_turn_before_it(
    client: AsyncClient, store: FakeStorage
):
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "reasoning_effort": "high",
        },
    )
    parent_id = created.json()["id"]

    inherited = await _next_turn(client, store, parent_id, {"prompt": "keep going"})
    assert inherited["reasoning_effort"] == "high"

    switched = await _next_turn(
        client, store, inherited["id"], {"prompt": "now the easy bit", "reasoning_effort": "low"}
    )
    assert switched["reasoning_effort"] == "low"


async def test_a_follow_up_can_reset_an_inherited_effort_to_auto(
    client: AsyncClient, store: FakeStorage
):
    """Null already means "inherit", so clearing takes the explicit sentinel."""
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "reasoning_effort": "max",
        },
    )
    parent_id = created.json()["id"]

    reset = await _next_turn(
        client, store, parent_id, {"prompt": "back to normal", "reasoning_effort": "auto"}
    )

    assert reset["reasoning_effort"] is None


async def test_a_follow_up_effort_is_checked_against_the_model_it_switches_to(
    client: AsyncClient, store: FakeStorage
):
    """A pair offered last turn need not be offered this one."""
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "reasoning_effort": "high",
        },
    )
    parent_id = created.json()["id"]

    response = await _send(
        client,
        store,
        parent_id,
        {"prompt": "switch", "model": "qwen-next", "reasoning_effort": "high"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "reasoning_effort_not_available"
    assert store.pending == {}


async def test_a_level_the_runtime_cannot_express_is_refused_at_create(client: AsyncClient):
    """The other half of the intersection: the model takes it, the CLI cannot say it.

    pi reaches the wire with four of its six flag levels — `xhigh` arrives as
    `high` and `off` sends nothing — so offering `xhigh` would hand someone a
    depth this runtime cannot actually ask for. The refusal belongs here,
    before the job exists.
    """
    response = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "runtime": "pi",
            "model": "glm-5.1",
            "reasoning_effort": "xhigh",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "reasoning_effort_not_available"


async def test_every_runtime_still_takes_ordinary_turns(client: AsyncClient):
    """And nothing else about them changed: Auto is every turn they ever ran."""
    for runtime in ("pi", "opencode", "kilo", "codex"):
        response = await client.post(
            "/v1/agent/jobs",
            json={
                "repo": "owner/name",
                "task_prompt": "fix it",
                "runtime": runtime,
                "model": "glm-5.1",
            },
        )
        assert response.status_code == 201, f"{runtime}: {response.text}"
        assert response.json()["reasoning_effort"] is None


async def test_a_thread_read_returns_what_earlier_turns_did(
    client: AsyncClient, store: FakeStorage
):
    """A follow-up must not make the previous turn's tool activity unreachable."""
    first_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{first_id}/events",
        json={"event_type": "tool_use", "payload": {"id": "t1", "name": "Bash"}},
        headers=auth,
    )
    await client.post(
        f"/v1/agent/worker/jobs/{first_id}/events",
        json={"event_type": "message", "payload": {"text": "done"}},
        headers=auth,
    )
    # With the worker's credential: the first turn has to have finished for
    # the message below to become the next turn rather than wait for it.
    finished = await client.post(
        f"/v1/agent/worker/jobs/{first_id}/finish", json={"state": "succeeded"}, headers=auth
    )
    assert finished.status_code == 200
    second = await _send(client, store, first_id, {"prompt": "and now the docs"})
    second_id = second.json()["job"]["id"]

    per_job = (await client.get(f"/v1/agent/jobs/{second_id}/events")).json()
    assert per_job["events"] == []

    thread = (await client.get(f"/v1/agent/jobs/{second_id}/thread/events")).json()
    assert [event["event_type"] for event in thread["events"]] == ["tool_use", "message"]
    assert {event["turn_no"] for event in thread["events"]} == {1}
    assert thread["events"][0]["payload"]["name"] == "Bash"


async def test_a_thread_event_read_refuses_a_foreign_job(client: AsyncClient, store: FakeStorage):
    job_id = await _create_job(client)
    store.jobs[job_id]["user_id"] = "someone-else"

    response = await client.get(f"/v1/agent/jobs/{job_id}/thread/events")

    assert response.status_code == 404


@pytest.mark.parametrize("session_state", ["queued", "running", "idle", "failed"])
async def test_session_listing_exposes_stable_titles_facets_and_archive_state(
    client: AsyncClient, store: FakeStorage, monkeypatch, session_state: str
):
    job_id = await _create_job(client)
    await store.conversation_commands.set_thread_archived(
        job_id=job_id, user_id=_OWNER, archived=True
    )
    job = await store.conversation_queries.get_job(job_id)
    assert job is not None
    listing = AsyncMock(
        return_value={
            "sessions": [
                {
                    "thread_id": job["thread_id"],
                    "title": "Stable conversation title",
                    "archived_at": job["archived_at"],
                    "job": job,
                    "can_delete": False,
                    "delete_block_reason": "session_active",
                }
            ],
            "total": 51,
            "next_cursor": "opaque-next-page",
            "projects": [{"repo": "owner/name", "count": 51}],
        }
    )
    monkeypatch.setattr(store, "list_sessions", listing, raising=False)

    response = await client.get(
        "/v1/agent/sessions",
        params={
            "archived": "true",
            "search": "  Stable  ",
            "repo": "owner/name",
            "state": session_state,
            "limit": 20,
            "cursor": "previous-page",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sessions"][0]["title"] == "Stable conversation title"
    assert body["sessions"][0]["archived_at"] == body["sessions"][0]["job"]["archived_at"]
    assert body["sessions"][0]["delete_block_reason"] == "session_active"
    assert body["total"] == 51 and body["next_cursor"] == "opaque-next-page"
    assert body["projects"] == [{"repo": "owner/name", "count": 51}]
    listing.assert_awaited_once_with(
        user_id=_OWNER,
        archived=True,
        search="Stable",
        repo="owner/name",
        state=session_state,
        limit=20,
        cursor="previous-page",
        include_summary=True,
    )


async def test_job_response_preserves_turn_result_beside_conversation_activity(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    store.jobs[job_id].update(state="cancelled", conversation_state="running")
    response = await client.get(f"/v1/agent/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert response.json()["conversation_state"] == "running"


async def test_session_list_can_omit_summary(client: AsyncClient, store: FakeStorage, monkeypatch):
    listing = AsyncMock(
        return_value={"sessions": [], "total": None, "projects": None, "next_cursor": None}
    )
    monkeypatch.setattr(store, "list_sessions", listing, raising=False)
    response = await client.get("/v1/agent/sessions?include_summary=false&repo=owner/name")
    assert response.status_code == 200
    assert response.json() == {"sessions": [], "total": None, "projects": None, "next_cursor": None}
    assert listing.call_args.kwargs["include_summary"] is False
    assert listing.call_args.kwargs["repo"] == "owner/name"


async def test_session_list_rejects_bad_cursor_and_display_filters(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    listing = AsyncMock(side_effect=ValueError("Invalid session cursor for these filters"))
    monkeypatch.setattr(store, "list_sessions", listing, raising=False)
    assert (await client.get("/v1/agent/sessions?cursor=bad")).status_code == 400
    for params in ({"state": "succeeded"}, {"limit": 201}, {"search": "bad\x00search"}):
        assert (await client.get("/v1/agent/sessions", params=params)).status_code == 422
    assert listing.await_count == 1


async def test_session_mutations_use_explicit_owner_snapshot(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    thread_ids = ["athr_1", "athr_2"]
    archive = AsyncMock(
        return_value={
            "sessions": [{"thread_id": value, "archived_at": None} for value in thread_ids]
        }
    )
    delete = AsyncMock(return_value={"thread_ids": thread_ids})
    monkeypatch.setattr(store, "set_sessions_archived", archive, raising=False)
    monkeypatch.setattr(store, "delete_sessions", delete, raising=False)
    response = await client.post(
        "/v1/agent/sessions/archive", json={"thread_ids": thread_ids, "archived": False}
    )
    assert response.status_code == 200
    assert all(row["archived"] is False for row in response.json()["sessions"])
    archive.assert_awaited_once_with(thread_ids=thread_ids, user_id=_OWNER, archived=False)
    response = await client.post("/v1/agent/sessions/delete", json={"thread_ids": thread_ids})
    assert response.status_code == 200 and response.json() == {"thread_ids": thread_ids}
    delete.assert_awaited_once_with(thread_ids=thread_ids, user_id=_OWNER)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("not_found", 404),
        ("not_archived", 409),
        ("session_active", 409),
        ("fork_in_progress", 409),
        ("publication_pending", 409),
    ],
)
async def test_session_delete_conflicts_have_actionable_types(
    client: AsyncClient, store: FakeStorage, monkeypatch, error: str, status: int
):
    delete = AsyncMock(return_value={"error": error})
    monkeypatch.setattr(store, "delete_sessions", delete, raising=False)
    response = await client.post("/v1/agent/sessions/delete", json={"thread_ids": ["athr_1"]})
    assert response.status_code == status
    assert response.json()["detail"]["error"]["type"] == error


@pytest.mark.parametrize("thread_ids", [[], [""], ["a", "a"], ["bad\x00id"], ["x" * 129]])
async def test_session_mutations_refuse_ambiguous_selections(client: AsyncClient, thread_ids):
    for path, body in (
        ("archive", {"thread_ids": thread_ids, "archived": False}),
        ("delete", {"thread_ids": thread_ids}),
    ):
        assert (await client.post(f"/v1/agent/sessions/{path}", json=body)).status_code == 422
    too_many = {"thread_ids": [f"athr_{index}" for index in range(1001)]}
    assert (await client.post("/v1/agent/sessions/delete", json=too_many)).status_code == 422


async def test_archived_history_is_readable_but_follow_up_requires_unarchive(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    assert (await client.post(f"/v1/agent/jobs/{job_id}/archive")).status_code == 200
    detail = await client.get(f"/v1/agent/jobs/{job_id}")
    assert detail.status_code == 200 and detail.json()["archived_at"] is not None
    assert (await client.get(f"/v1/agent/jobs/{job_id}/thread")).status_code == 200
    blocked = await _send(client, store, job_id, {"prompt": "continue"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"]["type"] == "session_archived"
    assert len(store.jobs) == 1
    assert store.pending == {}
    assert (await client.delete(f"/v1/agent/jobs/{job_id}/archive")).status_code == 200
    assert (await _send(client, store, job_id, {"prompt": "continue"})).status_code == 201


async def test_follow_up_rechecks_archive_at_the_atomic_store_boundary(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """The store's locked check wins over the route's advisory read."""
    job_id = await _create_job(client)
    monkeypatch.setattr(
        store, "submit_message", AsyncMock(return_value={"error": "session_archived"})
    )
    response = await _send(client, store, job_id, {"prompt": "continue"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "session_archived"


async def test_an_idle_only_message_passes_its_guard_to_the_atomic_store(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """A stale action is refused before any queue settlement can send owner work."""
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"
    submitted = AsyncMock(return_value={"error": "conversation_changed"})
    settlement = AsyncMock()
    monkeypatch.setattr(store, "submit_message", submitted)
    monkeypatch.setattr(store, "settle_queue", settlement)

    response = await _send(
        client,
        store,
        job_id,
        {"prompt": "watch the PR", "require_idle": True, "expected_latest_job_id": job_id},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["type"] == "conversation_changed"
    submitted.assert_awaited_once()
    assert submitted.await_args.kwargs["require_idle"] is True
    assert submitted.await_args.kwargs["expected_latest_job_id"] == job_id
    settlement.assert_not_awaited()
    assert store.pending == {}
    assert len(store.jobs) == 1


@pytest.mark.parametrize(
    "guard",
    [
        {"require_idle": True},
        {"require_idle": True, "expected_latest_job_id": ""},
        {"expected_latest_job_id": "ajob_0001"},
        {"require_idle": "true", "expected_latest_job_id": "ajob_0001"},
    ],
)
async def test_an_idle_only_message_requires_a_complete_strict_guard(
    client: AsyncClient, store: FakeStorage, monkeypatch, guard: dict[str, Any]
):
    job_id = await _create_job(client)
    submitted = AsyncMock()
    monkeypatch.setattr(store, "submit_message", submitted)

    response = await _send(client, store, job_id, {"prompt": "watch the PR", **guard})

    assert response.status_code == 422, response.text
    submitted.assert_not_awaited()
    assert store.pending == {}
    assert len(store.jobs) == 1


async def test_an_idle_only_message_replays_after_its_turn_has_started(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"
    _attach_shared_workspace(store, job_id)
    store.jobs[job_id]["thread_sandbox"]["workspace_publication_state"] = "published"
    settlement = AsyncMock()
    monkeypatch.setattr(store, "settle_queue", settlement)
    body = {"prompt": "watch the PR", "require_idle": True, "expected_latest_job_id": job_id}
    headers = {"Idempotency-Key": "watch-pr-once"}

    first = await _send(client, store, job_id, body, headers=headers)
    assert first.status_code == 201, first.text
    watch_id = first.json()["job"]["id"]
    store.jobs[watch_id]["state"] = "running"
    again = await _send(client, store, job_id, body, headers=headers)

    assert again.status_code == 201, again.text
    assert again.json()["job"]["id"] == watch_id
    assert len(store.jobs) == 2
    assert store.pending == {}
    changed_guard = await _send(
        client,
        store,
        job_id,
        {**body, "expected_latest_job_id": watch_id},
        headers=headers,
    )
    assert changed_guard.status_code == 409, changed_guard.text
    assert changed_guard.json()["detail"]["error"]["type"] == "idempotency_conflict"
    assert len(store.jobs) == 2
    assert store.pending == {}
    settlement.assert_not_awaited()


async def test_an_idle_only_message_creates_its_turn_with_prepared_choices_without_settlement(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"
    store.jobs[job_id]["reasoning_effort"] = "high"
    thread_id = _attach_shared_workspace(store, job_id)
    store.jobs[job_id]["thread_sandbox"]["workspace_publication_state"] = "published"
    store.queue_paused[thread_id] = "failed"
    prepared = PreparedFollowUp(
        model="glm-5.1",
        reasoning_effort=None,
        reset_reasoning_effort=True,
        skills=[
            {
                "id": 5,
                "name": "Review",
                "description": "Review changes",
                "revision": 2,
                "instructions": "Inspect tests and edge cases.",
            }
        ],
        attachments=[
            {"id": 42, "path": "notes.txt", "content_type": "text/plain", "size_bytes": 11}
        ],
    )
    monkeypatch.setattr("cloud_agent.api.turns.prepare_follow_up", AsyncMock(return_value=prepared))
    created = AsyncMock(wraps=store.create_follow_up)
    settlement = AsyncMock()
    monkeypatch.setattr(store, "create_follow_up", created)
    monkeypatch.setattr(store, "settle_queue", settlement)

    response = await _send(
        client,
        store,
        job_id,
        {
            "prompt": "watch the PR",
            "require_idle": True,
            "expected_latest_job_id": job_id,
            "reasoning_effort": "auto",
            "skill_ids": [5],
            "attachment_ids": [42],
        },
    )

    assert response.status_code == 201, response.text
    watch = response.json()["job"]
    assert watch["parent_job_id"] == job_id
    assert watch["reasoning_effort"] is None
    assert store.jobs[watch["id"]]["skills"] == prepared.skills
    created.assert_awaited_once()
    assert created.await_args.kwargs["model"] == prepared.model
    assert created.await_args.kwargs["reasoning_effort"] is None
    assert created.await_args.kwargs["reset_reasoning_effort"] is True
    assert created.await_args.kwargs["skills"] == prepared.skills
    assert created.await_args.kwargs["attachments"] == prepared.attachments
    assert created.await_args.kwargs["yield_to_queued"] is True
    assert response.json()["pending"] is None
    assert store.pending == {}
    assert thread_id not in store.queue_paused
    assert store.queue_settled[thread_id] == job_id
    settlement.assert_not_awaited()


async def test_deleted_history_cannot_be_read_resumed_forked_or_used_as_workspace(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    thread_id = _thread_of(store, job_id)
    store.jobs[job_id]["deleted_at"] = datetime.now(timezone.utc)
    for suffix in (
        "",
        "/thread",
        "/thread/stats",
        "/thread/events",
        "/events",
        "/stream",
        "/files",
        "/git",
    ):
        response = await client.get(f"/v1/agent/jobs/{job_id}{suffix}")
        assert response.status_code == 404, (suffix, response.text)
    for suffix, body in (("/fork", {}), ("/cancel", {})):
        response = await client.post(f"/v1/agent/jobs/{job_id}{suffix}", json=body)
        assert response.status_code == 404, (suffix, response.text)
    # The conversation's own routes answer the same way: a message, the queue,
    # and continuing it.
    for method, path, body in (
        ("POST", f"/v1/agent/threads/{thread_id}/messages", {"prompt": "continue"}),
        ("GET", f"/v1/agent/threads/{thread_id}/messages", None),
        ("POST", f"/v1/agent/threads/{thread_id}/queue/resume", None),
    ):
        response = await client.request(method, path, json=body)
        assert response.status_code == 404, (path, response.text)
        assert response.json()["detail"]["error"]["type"] == "not_found"


@pytest.fixture(autouse=True)
def _configured_identity_issuer(monkeypatch):
    monkeypatch.setenv("GATEWAY_ISSUER", "https://gateway.example")


async def test_a_submission_key_reaches_the_store_with_a_fingerprint(
    client: AsyncClient, store: FakeStorage
):
    """The ``Idempotency-Key`` header becomes the job's submission key."""
    response = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1"},
        headers={"Idempotency-Key": "submit-1"},
    )
    assert response.status_code == 201, response.text
    created = store.jobs[response.json()["id"]]
    assert created["client_request_id"] == "submit-1"
    assert isinstance(created["client_request_fingerprint"], str)
    assert len(created["client_request_fingerprint"]) == 64

    parent_id = response.json()["id"]
    follow = await _send(
        client, store, parent_id, {"prompt": "and then"}, headers={"Idempotency-Key": "turn-2"}
    )
    assert follow.status_code == 201, follow.text
    waiting = store.pending[follow.json()["pending"]["id"]]
    key, fingerprint = waiting["client_request_id"], waiting["client_request_fingerprint"]
    assert key == "turn-2" and isinstance(fingerprint, str) and len(fingerprint) == 64


async def test_a_submission_without_a_key_carries_none(client: AsyncClient, store: FakeStorage):
    job_id = await _create_job(client)
    assert store.jobs[job_id]["client_request_id"] is None
    assert store.jobs[job_id]["client_request_fingerprint"] is None


async def test_idle_guards_preserve_ordinary_message_fingerprints_and_bind_the_newest_turn(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"
    submitted = AsyncMock(return_value={"error": "conversation_changed"})
    monkeypatch.setattr(store, "submit_message", submitted)
    bodies = [
        {"prompt": "watch the PR"},
        {"prompt": "watch the PR", "require_idle": False},
        {"prompt": "watch the PR", "require_idle": True, "expected_latest_job_id": job_id},
        {"prompt": "watch the PR", "require_idle": True, "expected_latest_job_id": "ajob_newer"},
    ]
    for index, body in enumerate(bodies):
        response = await _send(
            client, store, job_id, body, headers={"Idempotency-Key": f"fingerprint-{index}"}
        )
        assert response.status_code == 409, response.text
    fingerprints = [call.kwargs["client_request_fingerprint"] for call in submitted.await_args_list]
    legacy_payload = {
        "kind": "thread_message",
        "thread_id": _thread_of(store, job_id),
        "prompt": "watch the PR",
        "attachment_ids": None,
        "model": None,
        "reasoning_effort": None,
    }
    legacy_hash = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert fingerprints[0] == fingerprints[1] == legacy_hash
    assert fingerprints[2] != fingerprints[0]
    assert fingerprints[3] != fingerprints[2]
    assert store.pending == {}
    assert len(store.jobs) == 1


async def test_a_reused_key_under_different_content_is_a_409(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """The store's conflict answer is the API's ``idempotency_conflict``."""

    async def conflict(**_kwargs: Any) -> dict[str, Any]:
        return {"error": "client_request_conflict"}

    monkeypatch.setattr(store, "create_job", conflict)
    response = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1"},
        headers={"Idempotency-Key": "submit-1"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "idempotency_conflict"


async def test_a_follow_up_resend_after_archiving_returns_the_first_turn(
    client: AsyncClient, store: FakeStorage
):
    """The replay wins over a check that changed after the first send.

    The first send succeeded and its response was lost; the session was then
    archived. A retry under the same key and content is that same submission,
    so it must return the turn the first send made rather than the 409 the
    archive would give a genuinely new turn.
    """
    parent_id = await _create_job(client)
    store.jobs[parent_id]["state"] = "succeeded"
    body = {"prompt": "and then"}
    headers = {"Idempotency-Key": "turn-2"}

    first = await _send(client, store, parent_id, body, headers=headers)
    assert first.status_code == 201, first.text

    await store.conversation_commands.set_thread_archived(
        job_id=parent_id, user_id=_OWNER, archived=True
    )

    again = await _send(client, store, parent_id, body, headers=headers)
    assert again.status_code == 201, again.text
    assert again.json()["job"]["id"] == first.json()["job"]["id"]
    assert len(store.jobs) == 2


async def test_a_follow_up_resend_after_the_sandbox_is_lost_returns_the_first_turn(
    client: AsyncClient, store: FakeStorage
):
    """Same rule for the other advisory check the handler runs before the store."""
    parent_id = await _create_job(client)
    store.jobs[parent_id]["state"] = "succeeded"
    body = {"prompt": "and then"}
    headers = {"Idempotency-Key": "turn-2"}

    first = await _send(client, store, parent_id, body, headers=headers)
    assert first.status_code == 201, first.text

    store.jobs[parent_id]["thread_sandbox"] = {"shared_live": True, "context_status": "lost"}

    again = await _send(client, store, parent_id, body, headers=headers)
    assert again.status_code == 201, again.text
    assert again.json()["job"]["id"] == first.json()["job"]["id"]


async def test_a_create_resend_after_a_connection_disconnects_returns_the_first_job(
    client: AsyncClient, store: FakeStorage
):
    """A tool the first send was allowed to use is not re-authorised on a resend."""
    row = store.connect_mcp(user_id=_OWNER)
    body = _job_body(mcp_bindings=[_binding(row["id"])])
    headers = {"Idempotency-Key": "submit-1"}

    first = await client.post("/v1/agent/jobs", json=body, headers=headers)
    assert first.status_code == 201, first.text

    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    again = await client.post("/v1/agent/jobs", json=body, headers=headers)
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert len(store.jobs) == 1


async def test_a_new_key_still_runs_the_checks_that_can_refuse_it(
    client: AsyncClient, store: FakeStorage
):
    """Answering the resend first must not turn the mutable checks off.

    A different key is a different submission: the disconnected connection
    that a replay may ignore still refuses a job that has not been created.
    """
    row = store.connect_mcp(user_id=_OWNER)
    body = _job_body(mcp_bindings=[_binding(row["id"])])

    first = await client.post("/v1/agent/jobs", json=body, headers={"Idempotency-Key": "submit-1"})
    assert first.status_code == 201, first.text

    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    fresh = await client.post("/v1/agent/jobs", json=body, headers={"Idempotency-Key": "submit-2"})
    assert fresh.status_code == 400, fresh.text
    assert len(store.jobs) == 1


async def test_selected_skills_are_snapshotted_and_delivered_only_to_worker(
    client, store, monkeypatch
):
    rows = [
        {
            "id": 5,
            "name": "Review",
            "description": "Review changes",
            "revision": 1,
            "instructions": "Inspect tests and edge cases.",
            "default_enabled": True,
        }
    ]
    lookup = AsyncMock(return_value=rows)
    monkeypatch.setattr(store, "select_skills", lookup)
    created = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "model": "glm-5.1", "task_prompt": "fix it"},
        headers={"Idempotency-Key": "skill-submit"},
    )
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["skills"] == [
        {"id": 5, "name": "Review", "description": "Review changes", "revision": 1}
    ]
    assert job["task_prompt"] == "fix it"
    lookup.assert_awaited_once_with(user_id=_OWNER, skill_ids=None)
    rows[0]["instructions"] = "A later edit."
    rows[0]["revision"] = 2
    rows[0]["default_enabled"] = False
    # A resend must retain the first accepted content even if defaults changed.
    lookup.reset_mock()
    replay = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "model": "glm-5.1", "task_prompt": "fix it"},
        headers={"Idempotency-Key": "skill-submit"},
    )
    assert replay.json()["id"] == job["id"]
    lookup.assert_not_awaited()
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert claim.status_code == 200, claim.text
    assert "Inspect tests and edge cases." in claim.json()["task_prompt"]
    assert "A later edit." not in claim.json()["task_prompt"]
    assert claim.json()["task_prompt"].endswith("User task:\nfix it")
    assert store.messages[0]["content"] == "fix it"
    follow = await _next_turn(client, store, job["id"], {"prompt": "Continue"})
    assert follow["skills"][0]["revision"] == 1
    lookup.assert_not_awaited()
    updated = await _next_turn(client, store, follow["id"], {"prompt": "Update", "skill_ids": [5]})
    assert updated["skills"][0]["revision"] == 2
    cleared = await _next_turn(client, store, updated["id"], {"prompt": "Clear", "skill_ids": []})
    assert cleared["skills"] == []
    assert store.jobs[cleared["id"]]["skills"] == []


@pytest.mark.parametrize(
    "selection,status", [([999], 400), ([5, 5], 400), ([0], 422), ([True], 422), (["5"], 422)]
)
async def test_invalid_skill_selections_never_create_jobs(client, store, selection, status):
    result = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "model": "glm-5.1",
            "task_prompt": "fix it",
            "skill_ids": selection,
        },
    )
    assert result.status_code == status
    assert not store.jobs


# ── Returning a turn that never started ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_that_never_started_goes_back_to_the_queue(store: FakeStorage):
    """The host could not start the process; nobody should wait out its lease."""
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "worker-1"})
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

        released = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/release",
            json={"reason": "launch_failed"},
            headers=auth,
        )

    assert released.status_code == 200
    assert released.json() == {"ok": True, "state": "queued"}
    assert store.jobs[job_id]["state"] == "queued"
    assert store.jobs[job_id]["claim_retry_delay"] == 30
    # And the reason travels, because "this host cannot start turns" is what an
    # operator reads the trail for.
    assert store.released[-1][2] == "launch_failed"


@pytest.mark.asyncio
async def test_a_turn_that_started_cannot_be_returned(store: FakeStorage):
    """The refusal is the store's, not the caller's account of what happened.

    Replaying an admitted prompt executes a user's instruction a second time.
    A host that is wrong about what its child did must not be able to cause
    that, so the check lives where the evidence is.
    """
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "worker-1"})
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
        store.jobs[job_id]["admitted"] = True

        refused = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/release",
            json={"reason": "launch_failed"},
            headers=auth,
        )

    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "turn_not_releasable"
    assert store.jobs[job_id]["state"] == "running", "nothing was changed"


@pytest.mark.asyncio
async def test_returning_a_turn_needs_that_turn_own_worker_token(store: FakeStorage):
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        await client.post("/v1/agent/worker/claim", json={"worker_id": "worker-1"})

        unauthenticated = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/release", json={"reason": "launch_failed"}
        )

    assert unauthenticated.status_code == 401
    assert store.jobs[job_id]["state"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["", "Launch Failed", "launch failed", "x" * 65])
async def test_a_release_reason_outside_the_audit_vocabulary_is_refused(
    store: FakeStorage, reason: str
):
    """The reason lands in an append-only trail; it is a token, not free text."""
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "worker-1"})
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

        refused = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/release", json={"reason": reason}, headers=auth
        )

    assert refused.status_code == 422
    assert store.jobs[job_id]["state"] == "running"


async def test_follow_up_receipts_include_retained_membership_on_create_and_replay(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """Commands return bare SQL job rows; the HTTP receipt must use the owner read."""
    parent_id = await _create_job(client)
    store.jobs[parent_id]["state"] = "succeeded"
    sandbox = {
        "sandbox_id": "asbx_side_contract",
        "shared_live": True,
        "fork_anchor_job_id": "ajob_main_anchor",
        "native_state": "ready",
        "origin_thread_id": "athread_main",
    }
    store.jobs[parent_id]["thread_sandbox"] = sandbox
    create = store.create_follow_up
    replay = store.replayed_submission

    async def bare_create(**kwargs):
        job = await create(**kwargs)
        store.jobs[job["id"]]["thread_sandbox"] = sandbox
        return {**job, "thread_sandbox": None}

    async def bare_replay(**kwargs):
        job = await replay(**kwargs)
        return {**job, "thread_sandbox": None} if job else None

    monkeypatch.setattr(store, "create_follow_up", bare_create)
    monkeypatch.setattr(store, "replayed_submission", bare_replay)
    first = await _send(
        client, store, parent_id, {"prompt": "side reply"}, headers={"Idempotency-Key": "side-send"}
    )
    again = await _send(
        client, store, parent_id, {"prompt": "side reply"}, headers={"Idempotency-Key": "side-send"}
    )
    for response in (first, again):
        assert response.status_code == 201, response.text
        receipt = response.json()["job"]
        assert receipt["sandbox_id"] == sandbox["sandbox_id"]
        assert receipt["fork_anchor_job_id"] == sandbox["fork_anchor_job_id"]
        assert receipt["workspace_shared"] is True
    assert first.json()["job"]["id"] == again.json()["job"]["id"]
    assert len(store.jobs) == 2


@pytest.mark.parametrize("runtime", ["codex", "kilo", "claude-code", "opencode", "pi"])
async def test_claim_loads_current_default_servers_and_skips_unavailable_ones(
    client: AsyncClient, store: FakeStorage, runtime
):
    """Every CLI receives the live, usable set without a composer selection."""
    working = store.connect_mcp(user_id=_OWNER, label="Wiki")
    expired = store.connect_mcp(
        user_id=_OWNER,
        label="Expired",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    paused = store.connect_mcp(user_id=_OWNER, label="Paused")
    store.mcp_connections[paused["id"]]["enabled"] = False
    created = await client.post("/v1/agent/jobs", json=_job_body(runtime=runtime))
    assert created.status_code == 201
    thread = created.json()["thread_id"]
    # The real repository creates these defaults on list; storage tests exercise
    # that owner-scoped insert against Postgres. Keep the route fake explicit.
    store.mcp_bindings[thread] = [
        store._thread_binding(
            thread,
            {
                "connection_id": row["id"],
                "alias": f"mcp{row['id']}",
                "enabled": True,
                "selection_pending": True,
                "rules": {},
                "config_revision": row["config_revision"],
                "authorization_epoch": row["authorization_epoch"],
                "capability_revision": row["capability_revision"],
            },
        )
        for row in (working, expired, paused)
    ]
    app = _build_app(store, claim_auth=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as runner:
        claimed = await runner.post(
            "/v1/agent/worker/claim", json={"worker_id": "w-1", "lease_ttl_seconds": 60}
        )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["mcp_bindings"] == [f"mcp{working['id']}"]
    assert claimed.json()["mcp_token"]
