"""API-surface tests for the agent-sandbox job router.

Runs against a minimal in-memory stand-in for ``AgentJobStore`` so the default
(no-database) suite covers the HTTP contract: owner scoping, capability-token
authentication, the fenced-write → 409 mapping, and SSE framing. The store's
own concurrency semantics are pinned separately by the ``dbtest`` suite.

Ported from the gateway with assertions untouched. Two things the router no
longer holds are supplied by the test app instead of by an import:

- **The model catalog and the MCP registry** arrive over HTTP now, so the
  route registry the source stubbed is a fake ``GatewayClient`` here.
- **The auth dependencies** are real as of E7, so nothing stands in for them
  any more: ``get_current_user`` verifies this service's own session token and
  ``verify_admin_access`` compares ``ADMIN_TOKEN``. There is no API-key branch
  to stand in for since 2026-08-04 — it was deleted, along with the operational
  store it read; the session is the only owner credential here.
- **The sandbox's credential** is a gateway grant since E9, not something the
  claim mints, so the fake gateway client has a grant-minting sibling.

Workspace operations always route through the attempt's enrolled host. The
fake store returns one test host row and individual cases replace the client
factory with their in-memory broker; there is no global broker fallback.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent import broker_routing, mcp_catalogue
from cloud_agent.api import routes as agent_jobs_router
from cloud_agent.api.deps import get_agent_job_store
from cloud_agent.mcp_catalogue import McpProvider
from cloud_agent.mcp_registry import mcp_cipher
from cloud_agent.tokens import mint_worker_token

pytestmark = pytest.mark.asyncio

_OWNER = "user-owner"
_OTHER = "user-other"
_MCP_ENCRYPTION_KEY = "api-test-mcp-encryption-key"
_MCP_CIPHER = mcp_cipher(_MCP_ENCRYPTION_KEY)


class FakeAgentJobStore:
    """In-memory stand-in exposing the AgentJobStore surface the router uses."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.archived_threads: dict[str, datetime] = {}
        self.pinned_threads: dict[str, datetime] = {}
        self.turn_telemetry: dict[str, dict[str, Any]] = {}
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
        self.mcp_connections: dict[str, list[dict[str, Any]]] = {}
        self.session_forks: dict[str, dict[str, Any]] = {}
        self._next_event_id = 1
        self._next_job = 1

    # -- MCP connections --
    #
    # Keyed by user, because that is the only way the real store can be asked.

    async def list_mcp_connections(self, *, user_id: str) -> list[dict[str, Any]]:
        return sorted(self.mcp_connections.get(user_id, []), key=lambda row: row["server"])

    async def delete_mcp_connection(self, *, user_id: str, server: str) -> bool:
        rows = self.mcp_connections.get(user_id, [])
        remaining = [row for row in rows if row["server"] != server]
        self.mcp_connections[user_id] = remaining
        return len(remaining) != len(rows)

    def connect_mcp(
        self,
        *,
        user_id: str,
        server: str,
        token: str = "ghu_owner_secret",
        expires_at: datetime | None = None,
    ) -> None:
        """Seed a connection the way the OAuth flow will once it lands."""
        self.mcp_connections.setdefault(user_id, []).append(
            {
                "server": server,
                "account_label": "octocat",
                "access_token_ciphertext": _MCP_CIPHER.encrypt(token),
                "refresh_token_ciphertext": None,
                "expires_at": expires_at,
            }
        )

    # -- owner surface --
    async def create_job(self, **kwargs: Any) -> dict[str, Any]:
        job_id = f"ajob_{self._next_job:04d}"
        self._next_job += 1
        thread_id = f"athr_{job_id}"
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
            "created_at": None,
            "updated_at": None,
            **kwargs,
        }
        job.setdefault("base_sha", None)
        job.setdefault("title", job["task_prompt"].splitlines()[0][:160])
        job.setdefault("metadata", None)
        job.setdefault("execution_profile", "trusted-v1")
        job.setdefault("reasoning_effort", None)
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
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        self.last_follow_up_request = (client_request_id, client_request_fingerprint)
        requested = self.jobs.get(parent_job_id)
        if requested is None or requested["user_id"] != user_id:
            return None
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
            "created_at": None,
            "updated_at": None,
            "pinned_at": self.pinned_threads.get(parent["thread_id"]),
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
        job = next(job for job in self.jobs.values() if job["current_attempt_id"] == attempt_id)
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
    ) -> bool:
        job = self.jobs.get(job_id)
        if job is None or not self._fenced(attempt_id, lease_generation):
            return False
        if job["state"] not in from_states:
            return False
        job["state"] = to_state
        if to_state in {"succeeded", "failed", "cancelled"}:
            job["terminal_resume_pending"] = False
        job["detail"] = detail
        # Mirrors the store's COALESCE: a worker may fill in a base the job
        # lacked, never overwrite one the owner pinned.
        if base_sha and not job.get("base_sha"):
            job["base_sha"] = base_sha
        return True

    async def release_claim(
        self, *, job_id: str, attempt_id: int, lease_generation: int | None = None
    ) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.get("current_attempt_id") != attempt_id:
            return False
        self.released.append((job_id, attempt_id))
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
def store() -> FakeAgentJobStore:
    """Provide a fresh in-memory store."""
    return FakeAgentJobStore()


def _attach_shared_workspace(store: FakeAgentJobStore, job_id: str) -> str:
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
    app.dependency_overrides[get_agent_job_store] = lambda: store
    # The create endpoints fail fast on models the deployment cannot serve, so
    # the gateway the test answers with must contain what the tests submit.
    app.dependency_overrides[get_gateway_client] = _FakeGateway
    # The sandbox credential comes from the gateway (E9); without this the
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
    app.dependency_overrides[agent_jobs_router.authenticate_agent_owner] = lambda: identity
    if claim_auth:
        app.dependency_overrides[agent_jobs_router.verify_claiming_host] = lambda: {
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
async def client(store: FakeAgentJobStore):
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
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
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
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    monkeypatch.setenv("AGENT_DEFAULT_EXECUTION_PROFILE", "untrusted-kata-v1")
    source_id = await _create_job(client)
    store.jobs[source_id]["state"] = "succeeded"

    monkeypatch.setenv("AGENT_DEFAULT_EXECUTION_PROFILE", "trusted-v1")
    child = await client.post(f"/v1/agent/jobs/{source_id}/follow-ups", json={"prompt": "continue"})

    assert child.status_code == 201
    assert child.json()["execution_profile"] == "untrusted-kata-v1"


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
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
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
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    search = AsyncMock(side_effect=ValueError("Invalid conversation cursor for this search"))
    monkeypatch.setattr(store, "list_conversations", search, raising=False)
    response = await client.get("/v1/agent/conversations", params={"cursor": "invalid"})
    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid conversation cursor for this search"


async def test_conversation_search_requires_owner_and_accepts_browser_session(
    store: FakeAgentJobStore, monkeypatch
):
    """Anonymous searches are refused; signed-in searches use the JWT owner."""
    search = AsyncMock(return_value={"conversations": [], "next_cursor": None})
    monkeypatch.setattr(store, "list_conversations", search, raising=False)
    app = _build_app(store)
    app.dependency_overrides.pop(agent_jobs_router.authenticate_agent_owner)
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
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """False selects the ordinary list rather than omitting the filter."""
    search = AsyncMock(return_value={"conversations": [], "next_cursor": None})
    monkeypatch.setattr(store, "list_conversations", search, raising=False)
    response = await client.get("/v1/agent/conversations", params={"pinned": "false"})
    assert response.status_code == 200
    assert search.await_args.kwargs["pinned"] is False


def _seed_event(
    store: FakeAgentJobStore,
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
    client: AsyncClient, store: FakeAgentJobStore
):
    """The bar counts the whole conversation, not just the job asked about."""
    parent_id = await _create_job(client)
    follow_up = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "and the docs too"},
    )
    child_id = follow_up.json()["id"]
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
        assert body["thread_id"] == follow_up.json()["thread_id"]
        assert body["turns"] == 2
        assert body["tool_calls"] == 1
        assert body["failed_tool_calls"] == 1
        assert body["input_tokens"] == 140
        assert body["output_tokens"] == 15
        assert body["tokens_source"] == "runtime"
        assert body["timing_source"] == "message_events"


async def test_thread_stats_report_absent_tokens_as_null(
    client: AsyncClient, store: FakeAgentJobStore
):
    """A runtime that reports no usage must not look like one that used none."""
    job_id = await _create_job(client)
    _seed_event(store, job_id=job_id, seq=1, event_type="raw", payload={"text": "tier-2 tail"})

    body = (await client.get(f"/v1/agent/jobs/{job_id}/thread/stats")).json()

    assert body["input_tokens"] is None
    assert body["output_tokens"] is None
    assert body["cache_hit_ratio"] is None
    assert body["output_tokens_per_second"] is None
    assert body["tokens_source"] == "unavailable"


async def test_thread_stats_are_owner_scoped(client: AsyncClient, store: FakeAgentJobStore):
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
    client: AsyncClient, store: FakeAgentJobStore
):
    """Archive hides every turn while leaving a live run untouched."""
    parent_id = await _create_job(client)
    follow_up = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "also update the docs"},
    )
    child_id = follow_up.json()["id"]
    thread_id = follow_up.json()["thread_id"]
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
    client: AsyncClient, store: FakeAgentJobStore
):
    """Any turn renames all owner-facing views while keeping execution inputs intact."""
    parent_id = await _create_job(client)
    follow_up = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups", json={"prompt": "also update the docs"}
    )
    child_id = follow_up.json()["id"]
    prompts = {job_id: store.jobs[job_id]["task_prompt"] for job_id in (parent_id, child_id)}
    response = await client.patch(
        f"/v1/agent/jobs/{child_id}/title", json={"title": "  Export CSV 导出  "}
    )
    assert response.status_code == 200
    assert response.json() == {
        "thread_id": follow_up.json()["thread_id"],
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
    next_turn = await client.post(
        f"/v1/agent/jobs/{child_id}/follow-ups", json={"prompt": "one more change"}
    )
    assert next_turn.json()["title"] == "Export CSV 导出"


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
    client: AsyncClient, store: FakeAgentJobStore
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
    client: AsyncClient, store: FakeAgentJobStore
):
    """Any turn pins the conversation and its project without touching run state."""
    parent_id = await _create_job(client)
    follow_up = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "also update the docs"},
    )
    child_id = follow_up.json()["id"]
    thread_id = follow_up.json()["thread_id"]
    newer = await store.create_job(
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


async def test_follow_up_response_inherits_the_thread_pin(
    client: AsyncClient,
):
    """A new turn reports the conversation state without waiting for a list refresh."""
    parent_id = await _create_job(client)
    pinned = await client.post(f"/v1/agent/jobs/{parent_id}/pin")

    follow_up = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "also update the docs"},
    )

    assert follow_up.status_code == 201
    assert follow_up.json()["pinned_at"] == pinned.json()["pinned_at"]


async def test_follow_up_queues_after_parent_and_uses_native_session_context(
    client: AsyncClient, store: FakeAgentJobStore
):
    """A user can send the next turn while the current sandbox is active."""
    parent_id = await _create_job(client)
    follow_up = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "now add a regression test", "model": "qwen-next"},
    )
    assert follow_up.status_code == 201
    child = follow_up.json()
    assert child["state"] == "queued"
    assert child["parent_job_id"] == parent_id
    assert child["turn_no"] == 2
    assert child["model"] == "qwen-next"

    thread = await client.get(f"/v1/agent/jobs/{child['id']}/thread")
    assert thread.status_code == 200
    assert [message["content"] for message in thread.json()["messages"]] == [
        "fix it",
        "now add a regression test",
    ]

    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    assert claim["job_id"] == parent_id
    blocked = await client.post("/v1/agent/worker/claim", json={"worker_id": "w2"})
    assert blocked.status_code == 200
    assert blocked.json() is None
    assert store.jobs[child["id"]]["state"] == "queued"
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
    assert store.jobs[child["id"]]["state"] == "queued"

    child_claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w2"})).json()
    assert child_claim["job_id"] == child["id"]
    assert "context_patch" not in child_claim
    assert "context_messages" not in child_claim
    assert "context_checkpoint" not in child_claim


async def test_cancelled_middle_turn_does_not_release_a_later_queued_turn(
    client: AsyncClient, store: FakeAgentJobStore
):
    """Every unfinished earlier turn blocks a claim, even across a cancellation."""
    parent_id = await _create_job(client)
    middle = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "skip this follow-up"},
    )
    assert middle.status_code == 201
    middle_id = middle.json()["id"]
    latest = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "run this after the first turn"},
    )
    assert latest.status_code == 201
    latest_id = latest.json()["id"]
    assert latest.json()["parent_job_id"] == middle_id
    assert latest.json()["state"] == "queued"

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


async def test_follow_up_model_switch_gets_the_same_fail_fast(client: AsyncClient):
    """Switching models on a follow-up is validated like a fresh job."""
    parent_id = await _create_job(client)
    response = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "try another model", "model": "gpt-nonexistent"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "model_not_available"


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


async def test_a_follow_up_cannot_switch_the_threads_harness(client: AsyncClient):
    """The harness is fixed per thread (design D2); its session is the memory.

    Echoing the thread's own runtime stays a 201 — existing callers do that —
    and a *different* registered runtime is refused. Native fork preserves the
    runtime too, so the response points at a genuinely new chat.
    """
    parent_id = await _create_job(client)

    switched = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "continue on another harness", "runtime": "codex"},
    )
    assert switched.status_code == 400
    assert "start a new chat" in switched.json()["detail"]["error"]["message"]

    echoed = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "continue here", "runtime": "claude-code"},
    )
    assert echoed.status_code == 201

    invented = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "continue on nothing", "runtime": "no-such-harness"},
    )
    assert invented.status_code == 400


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


async def test_follow_ups_append_to_latest_turn_and_reject_blank_prompts(
    client: AsyncClient,
):
    """Old links still append linearly, and whitespace is not a durable turn."""
    parent_id = await _create_job(client)
    first = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "first follow-up"},
    )
    assert first.status_code == 201

    # Submit from the old parent URL again. It must attach to the current tip,
    # not create a sibling that could run against the same branch concurrently.
    second = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "second follow-up"},
    )
    assert second.status_code == 201
    assert second.json()["parent_job_id"] == first.json()["id"]
    assert second.json()["turn_no"] == 3
    assert second.json()["state"] == "queued"

    blank = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "   \n  "},
    )
    assert blank.status_code == 422


async def test_follow_up_is_refused_when_retained_context_is_lost(
    client: AsyncClient, store: FakeAgentJobStore
):
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "failed"
    store.jobs[job_id]["thread_sandbox"] = {
        "sandbox_id": "asbx_1",
        "state": "stopped_dirty",
        "context_status": "lost",
    }

    response = await client.post(
        f"/v1/agent/jobs/{job_id}/follow-ups",
        json={"prompt": "try the next message"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "sandbox_context_lost"
    assert len(store.jobs) == 1


async def test_follow_up_handles_context_loss_during_admission_checks(
    client: AsyncClient,
    store: FakeAgentJobStore,
    monkeypatch: pytest.MonkeyPatch,
):
    """The locked store check closes the gap after the advisory API read."""
    job_id = await _create_job(client)

    async def _lost_during_create(**_kwargs):
        return {"error": "sandbox_context_lost"}

    monkeypatch.setattr(store, "create_follow_up", _lost_during_create)
    response = await client.post(
        f"/v1/agent/jobs/{job_id}/follow-ups",
        json={"prompt": "race with a lost activation"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "sandbox_context_lost"
    assert len(store.jobs) == 1


async def test_chat_without_shared_native_state_cannot_be_forked(
    client: AsyncClient, store: FakeAgentJobStore
):
    """Fork never falls back to copying transcript rows or workspace data."""
    source_id = await _create_job(client)
    store.jobs[source_id]["state"] = "succeeded"

    response = await client.post(f"/v1/agent/jobs/{source_id}/fork")

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "native_context_unavailable"
    assert len(store.jobs) == 1


async def test_fork_of_an_active_turn_is_refused(client: AsyncClient, store: FakeAgentJobStore):
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
    store: FakeAgentJobStore,
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
    store: FakeAgentJobStore,
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
    store: FakeAgentJobStore, monkeypatch
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


async def test_shared_chat_fork_creates_two_native_sessions_in_one_container(
    store: FakeAgentJobStore, monkeypatch
):
    """Clicking Fork immediately materializes a sibling process, not a clone."""

    class Broker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def fork_shared_session(self, source_thread_id: str, **kwargs: Any):
            self.calls.append((source_thread_id, kwargs))
            assert kwargs["expected_container_id"] == "container-shared"
            assert kwargs["origin_thread_id"] == source_thread_id
            assert kwargs["source_native_session_id"] == "native-session-source"
            assert kwargs["source_native_turn_no"] == 1
            assert kwargs["target_thread_id"] != source_thread_id
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
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_id = await _create_job(client)
        source = store.jobs[job_id]
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
    assert len(broker.calls) == 1


async def test_shared_chat_can_fork_from_a_specific_historical_turn(
    store: FakeAgentJobStore, monkeypatch
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
        second_response = await client.post(
            f"/v1/agent/jobs/{first_id}/follow-ups",
            json={"prompt": "a second completed turn"},
        )
        assert second_response.status_code == 201
        second = store.jobs[second_response.json()["id"]]
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
    store: FakeAgentJobStore, monkeypatch
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


async def test_ready_native_fork_retry_does_not_need_the_host(
    store: FakeAgentJobStore, monkeypatch
):
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


async def test_fork_is_owner_scoped(client: AsyncClient, store: FakeAgentJobStore):
    """Someone else's job id forks as 404, indistinguishable from absent."""
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"
    store.jobs[job_id]["user_id"] = _OTHER

    response = await client.post(f"/v1/agent/jobs/{job_id}/fork")

    assert response.status_code == 404


async def test_browser_jwt_can_list_agent_jobs(store: FakeAgentJobStore):
    """Regression: the web login JWT was interpreted as an invalid API key."""
    app = _build_app(store)
    app.dependency_overrides.pop(agent_jobs_router.authenticate_agent_owner)
    token = _browser_session()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as jwt_client:
        response = await jwt_client.get(
            "/v1/agent/jobs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"jobs": []}


async def test_expired_browser_jwt_is_not_reinterpreted_as_api_key(store: FakeAgentJobStore):
    """Credential dispatch must preserve a JWT's own expiry failure."""
    from datetime import timedelta

    app = _build_app(store)
    app.dependency_overrides.pop(agent_jobs_router.authenticate_agent_owner)
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


async def test_sandbox_token_stays_out_of_owner_routes(store: FakeAgentJobStore):
    """A structured Agent token must not be mistaken for a browser JWT."""
    from cloud_agent.tokens import SCOPE_MODEL

    app = _build_app(store)
    app.dependency_overrides.pop(agent_jobs_router.authenticate_agent_owner)
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
async def test_a_gateway_allowed_non_admin_owner_can_read(store: FakeAgentJobStore, role: str):
    """The explicit policy, rather than a profile role, admits the owner."""
    app = _build_app(store, role=role)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs")
    assert response.status_code == 200


async def test_other_users_jobs_are_404_not_403(client: AsyncClient, store: FakeAgentJobStore):
    """Someone else's job is indistinguishable from a missing one."""
    foreign = await store.create_job(
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


async def test_live_workspace_routes_use_the_job_broker(store: FakeAgentJobStore, monkeypatch):
    """Files, terminal and Git address the same retained chat workspace."""

    class Broker:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def register_shared_sandbox(self, _sandbox: dict[str, Any]):
            return None

        async def files(self, workspace_id: str, path: str):
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


async def test_missing_shared_workspace_is_a_conflict_not_a_missing_file(
    store: FakeAgentJobStore, monkeypatch
):
    """The broker's shared-only 404 describes the workspace, not a file path."""
    from cloud_agent_host.workspace_broker_client import WorkspaceBrokerError

    class Broker:
        async def register_shared_sandbox(self, _sandbox: dict[str, Any]):
            return None

        async def files(self, _workspace_id: str, _path: str):
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
    store: FakeAgentJobStore, monkeypatch
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

    async def create_terminal(self, workspace_id: str, *, rows: int, cols: int):
        self.calls.append(("create", workspace_id, rows, cols))
        return _terminal_session_descriptor()

    async def list_terminals(self, workspace_id: str):
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

    async def stream_terminal(self, workspace_id: str, terminal_id: str, *, after: int):
        self.calls.append(("stream", workspace_id, terminal_id, after))
        return self.stream

    async def terminal_input(self, workspace_id: str, terminal_id: str, *, data: str):
        self.calls.append(("input", workspace_id, terminal_id, data))
        if workspace_id in self.suspended:
            from cloud_agent_host.workspace_broker_client import WorkspaceBrokerError

            raise WorkspaceBrokerError(409, "workspace terminals are suspended")
        return _terminal_session_descriptor()

    async def resize_terminal(self, workspace_id: str, terminal_id: str, *, rows: int, cols: int):
        self.calls.append(("resize", workspace_id, terminal_id, rows, cols))
        return {**_terminal_session_descriptor(), "rows": rows, "cols": cols}

    async def delete_terminal(self, workspace_id: str, terminal_id: str):
        self.calls.append(("delete", workspace_id, terminal_id))
        return _terminal_session_descriptor(state="closed")


async def test_interactive_terminal_routes_proxy_owner_session_lifecycle(
    store: FakeAgentJobStore, monkeypatch
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


async def test_terminal_controls_use_the_attached_session_capability(
    store: FakeAgentJobStore, monkeypatch
):
    """Attached PTY controls skip entitlement and never replay the event log."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)

    async def unexpected_entitlement_recheck(**_kwargs):
        raise AssertionError("attached terminal controls must not call external entitlement")

    monkeypatch.setattr(
        agent_jobs_router,
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
        claimed = await store.claim_job(
            worker_id="worker-1",
            lease_ttl_seconds=60,
            host="runner-test",
            host_credential_hash="test-credential-hash",
        )
        assert claimed is not None
        await store.append_event(
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
    store: FakeAgentJobStore, monkeypatch
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
    store: FakeAgentJobStore, monkeypatch
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
        claimed = await store.claim_job(
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

        event_id = await store.append_event(
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
        retry_event_id = await store.append_event(
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
    store: FakeAgentJobStore, monkeypatch
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
    store: FakeAgentJobStore,
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
    store: FakeAgentJobStore,
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
    store: FakeAgentJobStore,
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


async def test_terminal_readiness_uses_the_shared_group_barrier(
    store: FakeAgentJobStore, monkeypatch
):
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
    store: FakeAgentJobStore, monkeypatch
):
    """Owning a chat does not bypass current repository entitlement."""
    job = await store.create_job(
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
    store: FakeAgentJobStore, monkeypatch
):
    """A retained chat reports missing broker/workspace as actionable conflicts."""
    from cloud_agent_host.workspace_broker_client import WorkspaceBrokerError

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
            async def create_terminal(self, workspace_id: str, *, rows: int, cols: int):
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
async def test_workspace_files_reject_path_traversal(store: FakeAgentJobStore, unsafe_path: str):
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


async def test_workspace_files_recheck_repository_entitlement(store: FakeAgentJobStore):
    """Owning a legacy row does not entitle its repository after access is revoked."""
    from cloud_agent.api.deps import get_agent_app_credentials

    foreign_repo_job = await store.create_job(
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
    store: FakeAgentJobStore,
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


async def test_worker_flow_claim_event_finish(client: AsyncClient, store: FakeAgentJobStore):
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
    client: AsyncClient, store: FakeAgentJobStore
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
    store: FakeAgentJobStore, monkeypatch
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


async def test_a_replayed_event_seq_is_idempotent(client: AsyncClient, store: FakeAgentJobStore):
    """A retry of a dropped response gets the stored id, not a duplicate row."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    body = {"event_type": "message", "payload": {"text": "hello"}, "event_seq": 1}

    first = await client.post(f"/v1/agent/worker/jobs/{job_id}/events", json=body, headers=auth)
    replay = await client.post(f"/v1/agent/worker/jobs/{job_id}/events", json=body, headers=auth)

    assert first.json()["event_id"] == replay.json()["event_id"]
    assert len(store.events) == 1


async def test_worker_api_bounds_oversized_events_without_failing_or_duplicating_the_turn(
    client: AsyncClient, store: FakeAgentJobStore
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


async def test_lost_lease_maps_to_409_on_every_write_path(
    client: AsyncClient, store: FakeAgentJobStore
):
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
    client: AsyncClient, store: FakeAgentJobStore
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


async def test_sse_resumes_from_last_event_id(client: AsyncClient, store: FakeAgentJobStore):
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


async def test_missing_store_returns_503(store: FakeAgentJobStore):
    """Without a database the agent API reports 503 rather than crashing."""
    app = _build_app(None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs")
    assert response.status_code == 503


async def test_claim_requires_an_enrolled_host_identity(store: FakeAgentJobStore):
    """An ordinary customer cannot dequeue and read another tenant's job.

    Regression: claim_job takes the oldest queued job across all tenants and
    the response carries that job's repo, prompt, metadata, and a working
    capability token — so plain API-key auth here was a cross-tenant leak.
    """
    await store.create_job(
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


async def test_pool_wide_bearer_token_does_not_open_claim(store: FakeAgentJobStore):
    """Only an enrolled, individually revocable host may dequeue work."""
    await store.create_job(
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
    client: AsyncClient, store: FakeAgentJobStore
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


async def test_terminal_stream_drains_beyond_one_page(
    client: AsyncClient, store: FakeAgentJobStore
):
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
        await store.append_event(
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
# is precisely the credential E9 removed, because it was signed with a root the
# gateway does not know and therefore bought nothing. The property it protected
# (the sandbox gets a *different*, weaker credential than the runner) is pinned
# below against the credential that replaced it.
#
# The case below is its in-place replacement. The full record — the original's
# four assertions, which three of them the replacement keeps verbatim, and why
# "does not parse at all" is stronger than "parses with a narrow scope" — is
# MIGRATION.md § Discarded tests → "`test_agent_jobs_api.py` — two cases E9
# replaced in place (E7/E8/E9)", case 1. Ratified 2026-08-04.


async def test_the_claim_hands_over_a_gateway_grant_not_a_minted_token(
    client: AsyncClient, store: FakeAgentJobStore
):
    """The sandbox credential is distinct from the runner's — and is not ours.

    Two separate claims, and the second is the point of E9: this service can
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
# ``test_the_sandbox_grant_cannot_write_job_state`` (E9's in-place replacement
# for ``test_sandbox_token_cannot_write_job_state``; 401 against the gateway
# grant the sandbox now carries). Recorded in MIGRATION.md's discarded-test
# register.


async def test_event_type_guard_rejects_a_trailing_newline(client: AsyncClient):
    """`match()` with `$` accepted "message\\n"; the guard must use fullmatch."""
    from cloud_agent.api.routes import _SAFE_EVENT_TYPE

    assert _SAFE_EVENT_TYPE.fullmatch("message") is not None
    assert _SAFE_EVENT_TYPE.fullmatch("message\n") is None


async def test_the_claim_carries_a_read_only_clone_credential(store: FakeAgentJobStore):
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
    store: FakeAgentJobStore,
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
    store: FakeAgentJobStore,
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
    client: AsyncClient, store: FakeAgentJobStore
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
    client: AsyncClient, store: FakeAgentJobStore
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


async def test_a_legacy_row_gets_no_credential_at_claim(store: FakeAgentJobStore):
    """A row written before the check existed must not yield a token now."""
    from cloud_agent.api.deps import get_agent_app_credentials

    minted: list[str] = []

    class FakeApp:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            minted.append(repo)
            return "ghs_should_not_happen"

    # Straight into the store, bypassing the API — the shape a pre-existing row has.
    await store.create_job(
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
    # And the job went back to the queue rather than burning an attempt.
    assert store.released


async def test_spend_and_usage_come_from_the_ledger_not_the_agent(store: FakeAgentJobStore):
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


async def test_the_owner_can_see_what_the_sandbox_could_reach(client: AsyncClient, monkeypatch):
    """The egress posture is reported, not left to trust."""
    monkeypatch.setenv("AGENT_SANDBOX_NETWORK", "agent-egress")
    monkeypatch.setenv("AGENT_EGRESS_AGENT_TIER", "platform_only")
    job_id = await _create_job(client)

    body = (await client.get(f"/v1/agent/jobs/{job_id}")).json()

    assert body["agent_egress_tier"] == "platform_only"


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
    """Stands in for the App: a user token maps to installations, which cover repos."""

    def __init__(self, installations, repos_by_installation, *, exchange_fails=False):
        self.installations = installations
        self.repos = repos_by_installation
        self.exchange_fails = exchange_fails
        self.exchanged: list[str] = []

    async def exchange_user_code(self, code: str) -> str:
        if self.exchange_fails:
            raise RuntimeError("GitHub refused the code exchange")
        self.exchanged.append(code)
        return f"user-token-for-{code}"

    async def installations_for_user(self, user_token: str) -> list[dict[str, Any]]:
        return self.installations

    async def repositories_for_installation(self, installation_id: int) -> list[str]:
        return self.repos.get(installation_id, [])

    async def token_for(self, repo: str, **kwargs: Any) -> str:
        return "ghs_x"


def _store_with_grants(store: FakeAgentJobStore) -> FakeAgentJobStore:
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

    store.record_repo_grant = record_repo_grant
    store.list_repo_grants = list_repo_grants
    store.revoke_repo_grant = revoke_repo_grant
    store.consume_oauth_state = consume_oauth_state
    return store


async def test_connecting_records_only_what_github_attests(store: FakeAgentJobStore, monkeypatch):
    """The entitlement must come from GitHub's answer, not the browser's claim.

    The browser sends a code; the platform exchanges it for a token that speaks
    as that user and asks GitHub which installations they can reach. An
    installation id posted directly would be exactly the confused deputy this
    path exists to prevent.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from cloud_agent.api.deps import get_agent_app_credentials

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service", "acme/web"]},
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
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


async def test_a_connected_user_may_run_only_their_own_repos(store: FakeAgentJobStore, monkeypatch):
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


async def test_a_failed_exchange_grants_nothing(store: FakeAgentJobStore, monkeypatch):
    """A code that does not verify must leave the user with no entitlement."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from cloud_agent.api.deps import get_agent_app_credentials

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service"]},
        exchange_fails=True,
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        connected = await client.post(
            "/v1/agent/github/connect",
            json={"code": "stolen", "state": "test-state-value-123456"},
        )
        config = await client.get("/v1/agent/config")

    assert connected.status_code == 400
    assert config.json()["repos"] == []


async def test_disconnecting_removes_the_entitlement(store: FakeAgentJobStore, monkeypatch):
    """Revoking here is half of it; uninstalling on GitHub is the other half."""
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
        await client.delete("/v1/agent/github/connect/77")
        after = await client.post(
            "/v1/agent/jobs",
            json={"repo": "acme/service", "task_prompt": "fix it", "model": "m"},
        )

    assert after.status_code == 403


async def test_a_branch_is_pinned_to_a_commit_at_creation(store: FakeAgentJobStore, monkeypatch):
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


async def test_an_unresolvable_branch_is_refused_at_creation(store: FakeAgentJobStore, monkeypatch):
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


async def test_branches_are_only_listed_for_an_entitled_repo(store: FakeAgentJobStore, monkeypatch):
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


# ── MCP server selection ──────────────────────────────────────────────


_UPSTREAM_URL = "https://mcp.example.com/mcp/readonly"
_UPSTREAM_TOKEN = "ghu_owner_secret"

#: Two providers for the mechanism's sake, not the real catalogue's. Pinning
#: these cases to the shipped entries would make them fail whenever a provider
#: is added or withdrawn — a fact about somebody else's service, tested where it
#: belongs in ``test_mcp_catalogue.py``.
_TEST_PROVIDERS = (
    McpProvider(
        name="deepwiki",
        label="DeepWiki",
        description="",
        url="https://wiki.example.com/mcp",
        read_scope="allowlist",
        scope_note="test double",
        tools=frozenset({"read_wiki"}),
    ),
    McpProvider(
        name="github",
        label="GitHub",
        description="",
        url=_UPSTREAM_URL,
        read_scope="endpoint",
        scope_note="test double",
    ),
)


def _catalogue(monkeypatch, *names: str) -> None:
    """Install a catalogue for one test.

    Patched on the module rather than at each import site because both
    ``catalogue()`` and ``get_provider()`` read these globals when called, so
    every consumer sees the same list — which is the point: a provider that is
    on offer to the composer must be the one job creation resolves against.
    """
    providers = tuple(entry for entry in _TEST_PROVIDERS if not names or entry.name in names)
    monkeypatch.setattr(mcp_catalogue, "CATALOGUE", providers)
    monkeypatch.setattr(mcp_catalogue, "_BY_NAME", {entry.name: entry for entry in providers})


async def test_the_composer_is_told_which_servers_and_which_runtimes(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """A picker built from anything but the create endpoint's rules is decoration."""
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github", token=_UPSTREAM_TOKEN)
    store.connect_mcp(user_id=_OWNER, server="deepwiki", token=_UPSTREAM_TOKEN)
    config = (await client.get("/v1/agent/config")).json()

    assert [entry["name"] for entry in config["mcp_servers"]] == ["deepwiki", "github"]
    assert config["mcp_runtimes"] == ["claude-code"]
    # The composer learns what a server exposes, never how to reach it or what
    # to present when it gets there.
    assert _UPSTREAM_TOKEN not in json.dumps(config)
    assert "mcp.example.com" not in json.dumps(config)


async def test_the_composer_offers_only_what_this_user_connected(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """The whole point of #49: another person's connection is not on offer.

    Both users exist and both servers are in the catalogue; only the connection
    differs, and it is the only thing that may decide what the owner sees.
    """
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OTHER, server="github", token="ghu_someone_elses_secret")

    config = (await client.get("/v1/agent/config")).json()
    assert config["mcp_servers"] == []
    assert "ghu_someone_elses_secret" not in json.dumps(config)


async def test_omitting_servers_takes_everything_the_owner_connected(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """Connecting is the opt-in, so a job that chose nothing still gets them."""
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github")
    created = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1"},
    )
    assert created.status_code == 201
    assert created.json()["mcp_servers"] == ["github"]


async def test_a_user_who_connected_nothing_gets_a_job_that_runs_without_mcp(
    client: AsyncClient, monkeypatch
):
    """No MCP is a supported shape, not an error."""
    _catalogue(monkeypatch)
    created = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1"},
    )
    assert created.status_code == 201
    assert created.json()["mcp_servers"] == []


async def test_an_explicit_empty_list_means_no_servers(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """Choosing none is a real choice and must not be read as "unset"."""
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github")
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "mcp_servers": [],
        },
    )
    assert created.status_code == 201
    assert created.json()["mcp_servers"] == []


async def test_an_unknown_server_is_refused_at_creation(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """Better than discovering mid-run that the task's one tool is missing."""
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github")
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "mcp_servers": ["sentry"],
        },
    )
    assert created.status_code == 400
    assert "sentry" in created.text


async def test_naming_a_server_someone_else_connected_is_refused_at_creation(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """Refused in the same words as one that does not exist.

    A caller must not be able to tell "nobody has this" from "somebody else
    has this" — the difference is another user's account state, and the reply
    to a request that may not have it is the same either way.
    """
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OTHER, server="github", token="ghu_someone_elses_secret")
    body = {
        "repo": "owner/name",
        "task_prompt": "fix it",
        "model": "glm-5.1",
    }

    theirs = await client.post("/v1/agent/jobs", json={**body, "mcp_servers": ["github"]})
    nobodys = await client.post("/v1/agent/jobs", json={**body, "mcp_servers": ["sentry"]})

    assert theirs.status_code == 400
    assert (
        theirs.json()["detail"]["error"]["message"].replace("github", "sentry")
        == nobodys.json()["detail"]["error"]["message"]
    )
    assert "ghu_someone_elses_secret" not in theirs.text


async def test_a_runtime_that_cannot_load_mcp_refuses_the_job(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """Silently dropping the tools would produce confidently wrong work."""
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github")
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "runtime": "codex",
            "mcp_servers": ["github"],
        },
    )
    assert created.status_code == 400
    assert "codex" in created.text


@contextlib.asynccontextmanager
async def _integrations_client(store: FakeAgentJobStore):
    """A client for the Integrations page, which reads more of the store.

    The page answers for source control as well as MCP, so the fake needs the
    grant surface and a GitLab lookup before it can be asked at all — neither
    of which any assertion here is about.
    """
    equipped = _store_with_grants(store)

    async def get_gitlab_connection(*, user_id: str):
        return None

    equipped.get_gitlab_connection = get_gitlab_connection
    app = _build_app(equipped)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        yield api


async def test_the_integrations_page_lists_the_catalogue_with_connection_state(
    store: FakeAgentJobStore, monkeypatch
):
    """The whole catalogue, marked — not just the part already connected.

    Someone with no connections is asking "what can I connect?", and a list of
    their connections is empty exactly when that question matters most.
    """
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github", token=_UPSTREAM_TOKEN)

    async with _integrations_client(store) as api:
        body = (await api.get("/v1/agent/integrations")).json()
    rows = {entry["name"]: entry for entry in body["mcp"]}

    assert set(rows) == {"github", "deepwiki"}
    assert rows["github"]["connected"] is True
    assert rows["github"]["account_label"] == "octocat"
    assert rows["deepwiki"]["connected"] is False
    # Connecting is not possible until the OAuth flow lands, and the row says
    # so rather than offering a button that cannot work.
    assert rows["github"]["configured"] is False
    assert rows["github"]["connect_url"] is None
    assert _UPSTREAM_TOKEN not in json.dumps(body)


async def test_the_integrations_page_never_shows_another_users_connection(
    store: FakeAgentJobStore, monkeypatch
):
    """Connected is a fact about the caller, and only the caller."""
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OTHER, server="github", token="ghu_someone_elses_secret")

    async with _integrations_client(store) as api:
        body = (await api.get("/v1/agent/integrations")).json()
    rows = {entry["name"]: entry for entry in body["mcp"]}

    assert rows["github"]["connected"] is False
    assert rows["github"]["account_label"] is None
    assert "ghu_someone_elses_secret" not in json.dumps(body)


async def test_disconnecting_removes_only_the_callers_credential(
    store: FakeAgentJobStore, monkeypatch
):
    """Two users connected the same server; one disconnects."""
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github")
    store.connect_mcp(user_id=_OTHER, server="github", token="ghu_someone_elses_secret")

    async with _integrations_client(store) as api:
        response = await api.delete("/v1/agent/integrations/mcp/github")

    assert response.status_code == 200
    rows = {entry["name"]: entry for entry in response.json()["mcp"]}
    assert rows["github"]["connected"] is False
    assert [row["server"] for row in store.mcp_connections[_OTHER]] == ["github"]


async def test_disconnecting_a_server_that_was_never_connected_is_a_404(
    store: FakeAgentJobStore, monkeypatch
):
    """Not an error worth hiding, and not a silent success either."""
    _catalogue(monkeypatch)
    async with _integrations_client(store) as api:
        response = await api.delete("/v1/agent/integrations/mcp/github")
    assert response.status_code == 404


async def test_a_follow_up_is_refused_when_its_thread_s_server_was_disconnected(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """Fail fast at creation, on **every** turn — not only a runtime switch.

    A thread's servers are inherited by each turn, and whether they are still
    connected changes on its own, from the Integrations page, between turns.
    Validating only when the caller also switched runtime left the ordinary
    follow-up — same harness, next prompt — returning 201 and discovering at
    its first tool call that the tools the prompt was written around are gone.
    """
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github")
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "mcp_servers": ["github"],
        },
    )
    job_id = created.json()["id"]

    async with _integrations_client(store) as api:
        assert (await api.delete("/v1/agent/integrations/mcp/github")).status_code == 200

    # No runtime in the body: the plain "keep going" follow-up.
    follow_up = await client.post(f"/v1/agent/jobs/{job_id}/follow-ups", json={"prompt": "again"})

    assert follow_up.status_code == 400
    assert "github" in follow_up.text


async def test_a_follow_up_still_runs_while_its_thread_s_server_is_connected(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """The check must not refuse the ordinary case it now runs on."""
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github")
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "mcp_servers": ["github"],
        },
    )

    follow_up = await client.post(
        f"/v1/agent/jobs/{created.json()['id']}/follow-ups", json={"prompt": "again"}
    )

    assert follow_up.status_code == 201


async def test_an_expired_connection_is_neither_offered_nor_usable(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """A stored expiry in the past is a token we already know will be refused.

    Offering it would put the server in the composer, let a job be created
    around it, and send a dead bearer token upstream — with the answer sitting
    in the row all three read.
    """
    _catalogue(monkeypatch)
    store.connect_mcp(
        user_id=_OWNER,
        server="github",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    config = (await client.get("/v1/agent/config")).json()
    assert config["mcp_servers"] == []

    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "mcp_servers": ["github"],
        },
    )
    assert created.status_code == 400

    # And a job that named nothing does not silently pick it up either.
    default = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1"},
    )
    assert default.json()["mcp_servers"] == []


async def test_the_integrations_page_says_why_a_connection_buys_nothing(
    store: FakeAgentJobStore, monkeypatch
):
    """Connected and unusable is a real state, and the page must name it.

    Reporting it as simply connected puts *Connected (octocat)* beside a
    composer offering nothing, with no way for the owner to tell which of the
    two is wrong or what to do about it.
    """
    _catalogue(monkeypatch)
    store.connect_mcp(
        user_id=_OWNER,
        server="github",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    async with _integrations_client(store) as api:
        body = (await api.get("/v1/agent/integrations")).json()
    row = {entry["name"]: entry for entry in body["mcp"]}["github"]

    assert row["connected"] is True
    assert "expired" in row["error"]


async def test_disconnecting_leaves_a_running_jobs_server_list_alone(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """A job keeps the name it was created with and loses the credential.

    Nothing hunts down live jobs on disconnect: the relay resolves the owner's
    connections per call, so the job's own ``mcp_servers`` list stays as it was
    and the server simply stops resolving — which is what
    ``test_a_server_the_owner_no_longer_has_reports_it_as_unavailable`` pins at
    the relay.
    """
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github")
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "mcp_servers": ["github"],
        },
    )
    assert created.json()["mcp_servers"] == ["github"]

    async with _integrations_client(store) as api:
        assert (await api.delete("/v1/agent/integrations/mcp/github")).status_code == 200

    job = (await client.get(f"/v1/agent/jobs/{created.json()['id']}")).json()
    assert job["mcp_servers"] == ["github"]
    assert await store.list_mcp_connections(user_id=_OWNER) == []


async def test_the_claim_hands_the_runner_the_job_s_servers(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """The runner passes names to the adapter; it never learns the addresses."""
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github", token=_UPSTREAM_TOKEN)
    store.connect_mcp(user_id=_OWNER, server="deepwiki", token=_UPSTREAM_TOKEN)
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "glm-5.1",
            "mcp_servers": ["github", "deepwiki"],
        },
    )
    assert created.status_code == 201

    app = _build_app(store, claim_auth=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as runner:
        claimed = await runner.post(
            "/v1/agent/worker/claim", json={"worker_id": "w-1", "lease_ttl_seconds": 60}
        )
    assert claimed.status_code == 200
    body = claimed.json()
    assert body["mcp_servers"] == ["deepwiki", "github"]
    assert "mcp.example.com" not in json.dumps(body)


async def test_the_claim_hands_over_the_mcp_credential_too(client: AsyncClient, monkeypatch):
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
    monkeypatch.setenv("AGENT_SANDBOX_MCP_URL", "http://agent-mcp:8000")
    await _create_job(client)

    body = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()

    assert body["mcp_base_url"] == "http://agent-mcp:8000"
    assert body["mcp_token"], "the sandbox was handed no MCP credential"

    from cloud_agent.tokens import SCOPE_MCP, parse_worker_token

    # Scoped, and not the same credential as either of the other two.
    assert parse_worker_token(body["mcp_token"])["scope"] == SCOPE_MCP
    assert body["mcp_token"] != body["worker_token"]
    assert body["mcp_token"] != body["sandbox_token"]


async def test_a_deployment_with_no_relay_hands_over_no_mcp_credential(
    client: AsyncClient, monkeypatch
):
    """Empty address, and the runtime refuses rather than guessing.

    Handing over a token with nowhere to spend it would point the sandbox at
    whatever it fell back to — which is the inference gateway, a service that
    does not answer for MCP.
    """
    monkeypatch.delenv("AGENT_SANDBOX_MCP_URL", raising=False)
    await _create_job(client)

    body = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()

    assert body["mcp_base_url"] == ""


async def test_a_job_created_with_an_alias_stores_the_canonical_id(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
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


async def test_an_unknown_model_is_still_refused_at_creation(client: AsyncClient):
    """The other half: resolution must not have turned refusal into a pass."""
    response = await _create_job_raw(client, model="no-such-model")

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "model_not_available"


async def test_the_owner_sees_what_their_turn_consumed(
    client: AsyncClient, store: FakeAgentJobStore
):
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
    client: AsyncClient, store: FakeAgentJobStore
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
    client: AsyncClient, store: FakeAgentJobStore
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
    client: AsyncClient, store: FakeAgentJobStore
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
    client: AsyncClient, store: FakeAgentJobStore
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
    client: AsyncClient, store: FakeAgentJobStore
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


async def test_an_unmeasured_turn_reports_nothing_rather_than_zero(
    client: AsyncClient, store: FakeAgentJobStore
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
    client: AsyncClient, store: FakeAgentJobStore, state: str
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
    client: AsyncClient, store: FakeAgentJobStore
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
    client: AsyncClient, store: FakeAgentJobStore
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
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
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
    client: AsyncClient, store: FakeAgentJobStore
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


async def test_a_follow_up_inherits_the_efforts_of_the_turn_before_it(client: AsyncClient):
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

    inherited = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups", json={"prompt": "keep going"}
    )
    assert inherited.status_code == 201
    assert inherited.json()["reasoning_effort"] == "high"

    switched = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "now the easy bit", "reasoning_effort": "low"},
    )
    assert switched.status_code == 201
    assert switched.json()["reasoning_effort"] == "low"


async def test_a_follow_up_can_reset_an_inherited_effort_to_auto(client: AsyncClient):
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

    reset = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "back to normal", "reasoning_effort": "auto"},
    )

    assert reset.status_code == 201
    assert reset.json()["reasoning_effort"] is None


async def test_a_follow_up_effort_is_checked_against_the_model_it_switches_to(
    client: AsyncClient,
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

    response = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "switch", "model": "qwen-next", "reasoning_effort": "high"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "reasoning_effort_not_available"


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
    client: AsyncClient, store: FakeAgentJobStore
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
    await client.post(f"/v1/agent/worker/jobs/{first_id}/finish", json={"state": "succeeded"})
    second = await client.post(
        f"/v1/agent/jobs/{first_id}/follow-ups", json={"prompt": "and now the docs"}
    )
    second_id = second.json()["id"]

    per_job = (await client.get(f"/v1/agent/jobs/{second_id}/events")).json()
    assert per_job["events"] == []

    thread = (await client.get(f"/v1/agent/jobs/{second_id}/thread/events")).json()
    assert [event["event_type"] for event in thread["events"]] == ["tool_use", "message"]
    assert {event["turn_no"] for event in thread["events"]} == {1}
    assert thread["events"][0]["payload"]["name"] == "Bash"


async def test_a_thread_event_read_refuses_a_foreign_job(
    client: AsyncClient, store: FakeAgentJobStore
):
    job_id = await _create_job(client)
    store.jobs[job_id]["user_id"] = "someone-else"

    response = await client.get(f"/v1/agent/jobs/{job_id}/thread/events")

    assert response.status_code == 404


@pytest.mark.parametrize("session_state", ["queued", "running", "idle", "failed"])
async def test_session_listing_exposes_stable_titles_facets_and_archive_state(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch, session_state: str
):
    job_id = await _create_job(client)
    await store.set_thread_archived(job_id=job_id, user_id=_OWNER, archived=True)
    job = await store.get_job(job_id)
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
    client: AsyncClient, store: FakeAgentJobStore
):
    job_id = await _create_job(client)
    store.jobs[job_id].update(state="cancelled", conversation_state="running")
    response = await client.get(f"/v1/agent/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert response.json()["conversation_state"] == "running"


async def test_session_list_can_omit_summary(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
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
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    listing = AsyncMock(side_effect=ValueError("Invalid session cursor for these filters"))
    monkeypatch.setattr(store, "list_sessions", listing, raising=False)
    assert (await client.get("/v1/agent/sessions?cursor=bad")).status_code == 400
    for params in ({"state": "succeeded"}, {"limit": 201}, {"search": "bad\x00search"}):
        assert (await client.get("/v1/agent/sessions", params=params)).status_code == 422
    assert listing.await_count == 1


async def test_session_mutations_use_explicit_owner_snapshot(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
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
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch, error: str, status: int
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
    client: AsyncClient, store: FakeAgentJobStore
):
    job_id = await _create_job(client)
    assert (await client.post(f"/v1/agent/jobs/{job_id}/archive")).status_code == 200
    detail = await client.get(f"/v1/agent/jobs/{job_id}")
    assert detail.status_code == 200 and detail.json()["archived_at"] is not None
    assert (await client.get(f"/v1/agent/jobs/{job_id}/thread")).status_code == 200
    blocked = await client.post(f"/v1/agent/jobs/{job_id}/follow-ups", json={"prompt": "continue"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"]["type"] == "session_archived"
    assert len(store.jobs) == 1
    assert (await client.delete(f"/v1/agent/jobs/{job_id}/archive")).status_code == 200
    assert (
        await client.post(f"/v1/agent/jobs/{job_id}/follow-ups", json={"prompt": "continue"})
    ).status_code == 201


async def test_follow_up_rechecks_archive_at_the_atomic_store_boundary(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    job_id = await _create_job(client)
    monkeypatch.setattr(
        store, "create_follow_up", AsyncMock(return_value={"error": "session_archived"})
    )
    response = await client.post(f"/v1/agent/jobs/{job_id}/follow-ups", json={"prompt": "continue"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "session_archived"


async def test_deleted_history_cannot_be_read_resumed_forked_or_used_as_workspace(
    client: AsyncClient, store: FakeAgentJobStore
):
    job_id = await _create_job(client)
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
    for suffix, body in (("/follow-ups", {"prompt": "continue"}), ("/fork", {}), ("/cancel", {})):
        response = await client.post(f"/v1/agent/jobs/{job_id}{suffix}", json=body)
        assert response.status_code == 404, (suffix, response.text)


@pytest.fixture(autouse=True)
def _configured_identity_issuer(monkeypatch):
    monkeypatch.setenv("GATEWAY_ISSUER", "https://gateway.example")


async def test_a_submission_key_reaches_the_store_with_a_fingerprint(
    client: AsyncClient, store: FakeAgentJobStore
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
    follow = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "and then"},
        headers={"Idempotency-Key": "turn-2"},
    )
    assert follow.status_code == 201, follow.text
    key, fingerprint = store.last_follow_up_request
    assert key == "turn-2" and isinstance(fingerprint, str) and len(fingerprint) == 64


async def test_a_submission_without_a_key_carries_none(
    client: AsyncClient, store: FakeAgentJobStore
):
    job_id = await _create_job(client)
    assert store.jobs[job_id]["client_request_id"] is None
    assert store.jobs[job_id]["client_request_fingerprint"] is None


async def test_a_reused_key_under_different_content_is_a_409(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
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
    client: AsyncClient, store: FakeAgentJobStore
):
    """The replay wins over a check that changed after the first send.

    The first send succeeded and its response was lost; the session was then
    archived. A retry under the same key and content is that same submission,
    so it must return the turn the first send made rather than the 409 the
    archive would give a genuinely new turn.
    """
    parent_id = await _create_job(client)
    body = {"prompt": "and then"}
    headers = {"Idempotency-Key": "turn-2"}

    first = await client.post(f"/v1/agent/jobs/{parent_id}/follow-ups", json=body, headers=headers)
    assert first.status_code == 201, first.text

    await store.set_thread_archived(job_id=parent_id, user_id=_OWNER, archived=True)

    again = await client.post(f"/v1/agent/jobs/{parent_id}/follow-ups", json=body, headers=headers)
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert len(store.jobs) == 2


async def test_a_follow_up_resend_after_the_sandbox_is_lost_returns_the_first_turn(
    client: AsyncClient, store: FakeAgentJobStore
):
    """Same rule for the other advisory check the handler runs before the store."""
    parent_id = await _create_job(client)
    body = {"prompt": "and then"}
    headers = {"Idempotency-Key": "turn-2"}

    first = await client.post(f"/v1/agent/jobs/{parent_id}/follow-ups", json=body, headers=headers)
    assert first.status_code == 201, first.text

    store.jobs[parent_id]["thread_sandbox"] = {"shared_live": True, "context_status": "lost"}

    again = await client.post(f"/v1/agent/jobs/{parent_id}/follow-ups", json=body, headers=headers)
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]


async def test_a_create_resend_after_a_tool_server_disconnects_returns_the_first_job(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """A tool the first send was allowed to use is not re-authorised on a resend."""
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github")
    body = {
        "repo": "owner/name",
        "task_prompt": "fix it",
        "model": "glm-5.1",
        "mcp_servers": ["github"],
    }
    headers = {"Idempotency-Key": "submit-1"}

    first = await client.post("/v1/agent/jobs", json=body, headers=headers)
    assert first.status_code == 201, first.text

    await store.delete_mcp_connection(user_id=_OWNER, server="github")

    again = await client.post("/v1/agent/jobs", json=body, headers=headers)
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert len(store.jobs) == 1


async def test_a_new_key_still_runs_the_checks_that_can_refuse_it(
    client: AsyncClient, store: FakeAgentJobStore, monkeypatch
):
    """Answering the resend first must not turn the mutable checks off.

    A different key is a different submission: the disconnected tool server
    that a replay may ignore still refuses a job that has not been created.
    """
    _catalogue(monkeypatch)
    store.connect_mcp(user_id=_OWNER, server="github")
    body = {
        "repo": "owner/name",
        "task_prompt": "fix it",
        "model": "glm-5.1",
        "mcp_servers": ["github"],
    }

    first = await client.post("/v1/agent/jobs", json=body, headers={"Idempotency-Key": "submit-1"})
    assert first.status_code == 201, first.text

    await store.delete_mcp_connection(user_id=_OWNER, server="github")

    fresh = await client.post("/v1/agent/jobs", json=body, headers={"Idempotency-Key": "submit-2"})
    assert fresh.status_code == 400, fresh.text
    assert len(store.jobs) == 1
