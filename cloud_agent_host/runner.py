"""The agent job runner (issue #1041, P0) — what actually executes a job.

Runs inside the sandbox (GitHub Actions for the P0 dogfood, a VM later) and is
the only component that ever touches agent output. Its shape follows from the
adjudicated security model rather than convenience:

- **One credential.** The dispatcher claims a job with its own credential and
  hands the runner a per-attempt capability token. That token is all the
  sandbox gets: it authorizes both event reporting *and* model calls, and it
  stops working the moment the job's fence moves. No GitHub token, no API key.
- **Patch-out, not push.** The runner never pushes. It emits a patch as an
  artifact; the trusted publisher outside the sandbox validates and pushes it.
  That is why the runner needs no git write credential at all.
- **Heartbeats are how cancellation arrives.** A background thread renews the
  lease and watches the reply for ``cancel_requested``; the agent is killed on
  the next beat. Losing the lease (409) is fatal by design — a runner whose
  attempt was superseded must stop rather than race its replacement.

Every step is written so that failing loudly beats continuing quietly: an
unparsable line becomes a raw event rather than disappearing, and a lost lease
ends the run instead of being retried.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import pathlib
import queue
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from cloud_agent_host.chat_sandbox import (
    CHAT_STATE_DIR,
    CHAT_WORKDIR,
    ChatSandboxManager,
    ChatSandboxSpec,
    RetainedChatBackend,
)
from cloud_agent_host.drivers import (
    LINE,
    DriverError,
    HarnessState,
    LaunchContext,
    TurnInput,
    driver_for,
)
from cloud_agent_host.enrollment import (
    HostHeartbeat,
    ensure_enrolled,
    establish_residency,
    release_residency,
)
from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_SOURCE,
    MAX_OBSERVED_PROCESSES,
    GuestObservation,
    add_process_counts,
    add_process_resources,
    attribute_processes,
)
from cloud_agent_host.logging_utils import configure_process_logging
from cloud_agent_host.patch_gate import branch_name_for, validate_patch
from cloud_agent_host.residency import ResidentEntry, ResidentPool
from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_host.resources import ResourceSampler, daemon_reachable
from cloud_agent_host.runtimes import (
    EMPTY_RUNTIME_MCP_CONFIG,
    AgentRuntime,
    NormalizedEvent,
    RuntimeMCPConfig,
    get_runtime,
    registered_runtimes,
)
from cloud_agent_host.sandbox import (
    KATA_RUNTIME,
    SANDBOX_PATH,
    ContainerBackend,
    SandboxBackend,
    SandboxError,
    SandboxProcess,
    SandboxSpec,
    build_backend_from_env,
    runtime_bin_dir,
)
from cloud_agent_host.session_supervisor import DEFAULT_CHAT_IDLE_TIMEOUT_S
from cloud_agent_host.session_supervisor_client import (
    SessionSupervisorClient,
    SessionSupervisorError,
)
from cloud_agent_host.setup import build_cache_from_env, run_setup
from cloud_agent_host.shared_protocol import (
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_PROTOCOL,
    SHARED_SANDBOX_RUNTIME_PINS,
)
from cloud_agent_host.source_archive import build_base_bundle, build_source_archive
from cloud_agent_host.tool_windows import (
    ToolMarkReader,
    ToolWindowTracker,
    attribute,
    tool_time_ms,
)
from cloud_agent_host.workspace_paths import purge_stale_workspaces, workspace_path
from cloud_agent_host.workspace_snapshot import (
    build_retained_workspace_snapshot,
    build_workspace_snapshot,
)

DEFAULT_LEASE_TTL_S = 120.0
HEARTBEAT_INTERVAL_S = 30.0
DEFAULT_AGENT_TIMEOUT_S = 3600.0
DEFAULT_WORKSPACE_TTL_S = 7 * 24 * 60 * 60
# How often the control checks run while the agent is silent.
_POLL_INTERVAL_S = 0.5
_TURN_INTERRUPT_DRAIN_S = 15.0
# Shared turns close their observer and then enforce a broker-side activation
# boundary whose reviewed filesystem normalization is allowed up to 120 s.
# Keep reading within the supervisor client's 180 s timeout so cancellation
# does not discard the already-closed guest telemetry merely because cleanup
# took longer than a local CLI needs to exit.
_SHARED_TURN_INTERRUPT_DRAIN_S = 180.0
_GIT_TIMEOUT_S = 300.0
# Bounds on the final in-sandbox `git diff`. Generous for a real patch, finite
# because the worktree's git configuration belongs to the agent by then.
_PATCH_TIMEOUT_S = 300.0
_PATCH_MAX_BYTES = 8 * 1024 * 1024

# Both are interpolated into a URL and handed to git, which reads a leading
# `-` as an option even where an operand belongs. Validate the shape here as
# well as at the API edge: this process holds the dispatcher credential.
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{7,64}")
_REPO_SLUG = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")

# Trusted-runner override for local development and self-hosted Git mirrors.
# The default remains GitHub. This is deliberately read only by the runner: a
# job request cannot choose where the platform sends a repository credential.
ENV_REPO_CLONE_BASE_URL = "AGENT_REPO_CLONE_BASE_URL"

# Hermetic git for anything the *runner* runs: no user config, no hooks, no
# credential helpers, no prompts.
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
    "GIT_LFS_SKIP_SMUDGE": "1",
}

# git refuses to touch a repository owned by another user ("dubious
# ownership"). The runner normally hands the worktree over with a chown so the
# ids match, but an unprivileged runner cannot — and there the agent's own
# `git status` and our patch build would both abort. Set for the sandbox side
# only; it is not config the runner itself ever runs under.
_SAFE_DIRECTORY_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": "*",
}


# A blocked egress attempt looks like a connection failure in the agent's own
# tool output, because that is where it surfaces: on a deny-all network the
# kernel drops the packet and the tool reports the failure. Recording these is
# what the design asks P0 to produce — the evidence for what an external
# default should allow. Deliberately narrow: a name-resolution or connection
# error carrying a host, not every failing command.
_EGRESS_FAILURE = re.compile(
    r"(?:"
    r"Could not resolve host|Name or service not known|Temporary failure in name resolution"
    r"|Connection refused|Network is unreachable|No route to host|Connection timed out"
    r"|getaddrinfo (?:failed|ENOTFOUND)|ENOTFOUND|EAI_AGAIN"
    r")",
    re.IGNORECASE,
)
# Hosts as they appear in those messages, or in any URL alongside them.
_HOST_IN_TEXT = re.compile(
    r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})|"
    r"(?:host|ENOTFOUND|resolve)[:\s]+'?([A-Za-z0-9.-]+\.[A-Za-z]{2,})'?",
    re.IGNORECASE,
)


def detect_blocked_egress(text: str) -> str | None:
    """Return the host an agent failed to reach, if this reads as a denial.

    Returns ``None`` for anything that is merely a failed command: a false
    "the sandbox tried to phone home" is worse than a missed one, because the
    whole point of the record is to be evidence.
    """
    if not text or not _EGRESS_FAILURE.search(text):
        return None
    match = _HOST_IN_TEXT.search(text)
    if match is None:
        return None
    host = match.group(1) or match.group(2)
    return host.strip("'\"").lower() or None


class LeaseLost(Exception):
    """Raised when this attempt no longer owns the job and must stop."""


class ClaimUnreachable(Exception):
    """The gateway could not be reached *to claim*, so no job was taken.

    Distinct from a transport failure later in the run: that one leaves an
    attempt running until its lease expires, and an operator needs to be sent
    to the job rather than told nothing was claimed.
    """


class WorktreeError(Exception):
    """Raised when the job's repository could not be materialized."""


@dataclass
class ClaimedJob:
    """What the dispatcher handed us."""

    job_id: str
    attempt_id: int
    attempt_no: int
    repo: str
    base_sha: str | None
    task_prompt: str
    runtime: str
    model: str
    worker_token: str
    # The inference credential that enters the sandbox: a gateway grant.
    sandbox_token: str
    execution_profile: str = "trusted-v1"
    # The MCP credential, and the relay it opens. A *second* pair, because
    # tools and inference are two services now: a leaked MCP token buys no
    # inference and a leaked grant buys no tools. Empty means this run has no
    # MCP, which the runtime refuses rather than resolving against the
    # inference address.
    mcp_token: str = ""
    mcp_base_url: str = ""
    # How hard the model should think on this turn, in the runtime's own
    # vocabulary. None is Auto: no flag, whatever the CLI and model default to.
    # Defaulted rather than required, because absent and Auto are the same
    # state — there is nothing a caller could pass to mean "unknown".
    reasoning_effort: str | None = None
    # Shell run before the agent, under the setup egress tier. Optional, so it
    # carries a default rather than forcing every caller to pass one.
    setup_script: str | None = None
    # Read-only, single-repo, short-lived: what the runner clones with. Stays
    # in the runner — it is never put in .git/config and never enters the
    # sandbox environment. ``None`` for a public repository.
    clone_token: str | None = None
    thread_id: str | None = None
    parent_job_id: str | None = None
    turn_no: int = 1
    # Prior platform messages are runtime-neutral. They let a follow-up keep
    # its conversational context even when the next turn switches harnesses.
    context_messages: list[dict[str, str]] = field(default_factory=list)
    # The prior successful run's edits, re-applied before the next sandbox is
    # started. Credentials still never cross the sandbox boundary.
    context_patch: str | None = None
    # The parent turn's ThreadCheckpoint envelope, already decrypted and gated
    # by the control plane. None means replay (design §9).
    context_checkpoint: dict[str, Any] | None = None
    # MCP servers this job was granted. Names only: the runner learns nothing
    # about where they actually are, because the sandbox reaches them through
    # the gateway's proxy rather than directly.
    mcp_servers: list[str] = field(default_factory=list)
    # The CLI version the control plane stamped on this attempt, resolved from
    # the thread's pin, the deployment's selection, or this host's default.
    # ``None`` from a control plane that predates selection — the image's
    # symlinked default then applies, which is what that control plane means.
    runtime_version: str | None = None
    # Durable one-chat/one-container mapping. Null is the legacy/new-thread
    # path; once present the scheduler pins this thread to its owning host.
    thread_sandbox: dict[str, Any] | None = None

    @classmethod
    def from_response(cls, body: dict[str, Any]) -> ClaimedJob:
        """Build from the claim endpoint's response."""
        return cls(
            job_id=body["job_id"],
            thread_id=body.get("thread_id"),
            parent_job_id=body.get("parent_job_id"),
            turn_no=int(body.get("turn_no") or 1),
            attempt_id=body["attempt_id"],
            attempt_no=body["attempt_no"],
            repo=body["repo"],
            base_sha=body.get("base_sha"),
            task_prompt=body["task_prompt"],
            setup_script=body.get("setup_script"),
            runtime=body["runtime"],
            model=body["model"],
            execution_profile=body.get("execution_profile") or "trusted-v1",
            # Absent from a control plane that predates the control, which is
            # the same thing as Auto — no separate "unknown" state to carry.
            reasoning_effort=body.get("reasoning_effort"),
            worker_token=body["worker_token"],
            # Older gateways return only worker_token; fall back so a
            # runner can still talk to one that predates scoped tokens.
            sandbox_token=body.get("sandbox_token") or body["worker_token"],
            mcp_token=body.get("mcp_token") or "",
            mcp_base_url=body.get("mcp_base_url") or "",
            clone_token=body.get("clone_token") or None,
            context_messages=list(body.get("context_messages") or []),
            context_patch=body.get("context_patch") or None,
            context_checkpoint=body.get("context_checkpoint") or None,
            mcp_servers=list(body.get("mcp_servers") or []),
            runtime_version=body.get("runtime_version") or None,
            thread_sandbox=(
                dict(body["thread_sandbox"])
                if isinstance(body.get("thread_sandbox"), dict)
                else None
            ),
        )


class ControlPlane:
    """Thin client for the worker endpoints, using the capability token."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {token}"})
        # This client serves exactly one attempt (one claim, one token), so a
        # plain counter is the whole numbering scheme. It makes each upload
        # idempotent server-side, which is what licenses the retry below.
        self._event_seq = 0

    def close(self) -> None:
        """Release the HTTP connection pool."""
        self._client.close()

    def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.post(f"{self._base}{path}", json=payload or {})
        if response.status_code == 409:
            raise LeaseLost(response.text[:200])
        response.raise_for_status()
        return response.json() if response.content else {}

    def append_event(self, event: NormalizedEvent) -> None:
        """Report one normalized event, retrying once now that replay is safe.

        The retry exists *because of* ``event_seq``: before it, a response
        lost on the wire left the runner unable to tell "never stored" from
        "stored, answer dropped", and re-sending risked a duplicate row in the
        owner's stream. With the number attached, the server answers a replay
        with the already-stored id, so one transport error no longer kills an
        otherwise healthy turn.
        """
        self._event_seq += 1
        body = {
            "event_type": event.event_type,
            "payload": event.payload,
            "event_seq": self._event_seq,
        }
        try:
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/events", body)
        except httpx.TransportError:
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/events", body)

    def heartbeat(self, ttl: float) -> dict[str, Any]:
        """Renew the lease and learn whether cancellation is pending."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/heartbeat", {"lease_ttl_seconds": ttl}
        )

    def save_artifact(self, kind: str, content: str) -> None:
        """Store an artifact produced by this attempt."""
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/artifacts",
            {"kind": kind, "content": content},
        )

    def report_continuity(self, mode: str, *, context_status: str | None = None) -> None:
        """Record this attempt's continuity rung. Best-effort observability."""
        try:
            body = {"mode": mode}
            if context_status is not None:
                body["context_status"] = context_status
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/continuity", body)
        except LeaseLost:
            raise
        except Exception:
            pass

    def report_context_status(self, context_status: str) -> None:
        """Record NEW/LIVE/RESTORED/LOST without inventing a continuity rung."""
        try:
            self._post(
                f"/v1/agent/worker/jobs/{self.job_id}/continuity",
                {"context_status": context_status},
            )
        except LeaseLost:
            raise
        except Exception:
            pass

    def report_resources(self, rollup: dict[str, Any]) -> None:
        """Report one closed window's resource rollup. Best-effort.

        Swallowed like every other observability write: a turn that ran must
        not fail because the platform could not record what it consumed.
        """
        try:
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/resources", rollup)
        except LeaseLost:
            raise
        except Exception:
            pass

    def reserve_chat_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Reserve immutable thread/container identity under this attempt fence."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/reserve",
            payload,
        )

    def mark_chat_sandbox_running(
        self,
        *,
        sandbox_generation: int,
        activation_generation: int,
        container_id: str,
        context_status: str,
    ) -> dict[str, Any]:
        """Record that Docker started the reserved activation."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/running",
            {
                "sandbox_generation": sandbox_generation,
                "activation_generation": activation_generation,
                "container_id": container_id,
                "context_status": context_status,
            },
        )

    def record_chat_process(
        self,
        *,
        sandbox_generation: int,
        activation_generation: int,
        process_generation: int,
    ) -> None:
        """Record a CLI process rotation inside the same chat container."""
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/process",
            {
                "sandbox_generation": sandbox_generation,
                "activation_generation": activation_generation,
                "process_generation": process_generation,
            },
        )

    def establish_shared_residency(
        self,
        *,
        sandbox_id: str,
        runtime_id: str,
        runtime_version: str | None,
    ) -> dict[str, Any]:
        """Obtain the member-scoped renewable credential owned by the broker."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/residency",
            {
                "sandbox_id": sandbox_id,
                "runtime_id": runtime_id,
                "runtime_version": runtime_version,
            },
        )

    def admit_chat_turn(
        self,
        *,
        sandbox_generation: int,
        activation_generation: int,
    ) -> None:
        """Reserve this job before its prompt may reach Agent CLI stdin."""
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/admit",
            {
                "sandbox_generation": sandbox_generation,
                "activation_generation": activation_generation,
            },
        )

    def settle_chat_sandbox(
        self,
        *,
        sandbox_generation: int,
        activation_generation: int,
        context_status: str,
        native_session_id: str | None,
        idle_timeout_seconds: float,
        base_sha: str | None = None,
    ) -> dict[str, Any]:
        """Persist a native context boundary before the host starts its idle timer."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/settle",
            {
                "sandbox_generation": sandbox_generation,
                "activation_generation": activation_generation,
                "context_status": context_status,
                "native_session_id": native_session_id,
                "base_sha": base_sha,
                "idle_timeout_seconds": idle_timeout_seconds,
            },
        )

    def abandon_chat_sandbox(
        self,
        *,
        sandbox_generation: int,
        activation_generation: int,
        lost: bool,
        detail: str | None,
        idle_timeout_seconds: float,
    ) -> dict[str, Any]:
        """Release a failed activation without claiming context continuity."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/abandon",
            {
                "sandbox_generation": sandbox_generation,
                "activation_generation": activation_generation,
                "lost": lost,
                "detail": detail,
                "idle_timeout_seconds": idle_timeout_seconds,
            },
        )

    def suspend_terminals(self, phase: str) -> None:
        """Freeze owner PTYs before workspace preparation or artifact capture."""
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/terminals/suspend",
            {"phase": phase},
        )

    def resume_terminals(self) -> None:
        """Resume retained PTYs and publish readiness for this attempt."""
        self._post(f"/v1/agent/worker/jobs/{self.job_id}/terminals/resume")

    def finish(self, state: str, detail: str | None = None, base_sha: str | None = None) -> None:
        """Drive the fenced terminal transition.

        ``base_sha`` reports the commit the agent actually worked from. A job
        may be submitted without one, and the publisher cannot apply a patch
        without knowing its base — so the runner, which resolves it when it
        checks the repository out, is the component that knows.
        """
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/finish",
            {"state": state, "detail": detail, "base_sha": base_sha},
        )

    job_id: str = ""


class Heartbeater:
    """Renews the lease in the background and surfaces cancellation."""

    def __init__(self, control: ControlPlane, *, ttl: float, interval: float) -> None:
        self._control = control
        self._ttl = ttl
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cancel_requested = False
        self.lease_lost = False

    def start(self) -> None:
        """Begin heartbeating on a daemon thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="agent-heartbeat")
        self._thread.start()

    def stop(self) -> None:
        """Stop heartbeating and join."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                reply = self._control.heartbeat(self._ttl)
            except LeaseLost:
                # Superseded or reaped: the replacement attempt owns the job now.
                self.lease_lost = True
                return
            except Exception:
                # A transient control-plane blip must not kill a healthy run;
                # if it persists, the lease expires and the reaper takes over.
                continue
            if reply.get("cancel_requested"):
                self.cancel_requested = True
                return


def claim(
    *,
    base_url: str,
    dispatcher_token: str,
    worker_id: str,
    lease_ttl: float,
    host: str | None = None,
    attestation: dict[str, Any] | None = None,
) -> ClaimedJob | None:
    """Claim the next queued job with the dispatcher credential.

    The dispatcher credential is used here and nowhere else — it does not
    travel into the agent's environment.

    ``host`` names the machine this runner sits on, which is how it joins the
    pool an operator picks from. Replicas on one machine share it; it is not
    ``worker_id``, which is deliberately unique per replica.
    """
    payload: dict[str, Any] = {"worker_id": worker_id, "lease_ttl_seconds": lease_ttl}
    if host:
        payload["host"] = host
    if attestation:
        payload["attestation"] = attestation
    try:
        # this legacy base_url still also feeds sandbox gateway traffic below.
        response = httpx.post(
            f"{base_url.rstrip('/')}/v1/agent/worker/claim",
            json=payload,
            headers={"Authorization": f"Bearer {dispatcher_token}"},
            timeout=30.0,
        )
    except httpx.TransportError as exc:
        # Tagged at the one place where "the gateway is unreachable" also means
        # "no job was claimed". The same error from a later call — an event, an
        # artifact, a terminal transition — happens with an attempt already
        # running, and reporting *that* as an unreached gateway would hide the
        # job an operator has to go look at.
        raise ClaimUnreachable(str(exc)) from exc
    response.raise_for_status()
    body = response.json()
    return ClaimedJob.from_response(body) if body else None


def _run_git(
    args: list[str],
    *,
    cwd: str | None = None,
    env_extra: dict[str, str] | None = None,
    input_text: str | None = None,
):
    """Run one git command hermetically and return the completed process."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **_GIT_ENV, **(env_extra or {})}
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        stdin=subprocess.DEVNULL if input_text is None else None,
        input=input_text,
        check=False,
    )


def apply_context_patch(workdir: str, patch: str) -> None:
    """Safely rehydrate a successful parent run into a fresh worktree."""
    gate = validate_patch(patch)
    if not gate.ok:
        raise WorktreeError(
            "refusing unsafe parent context patch: " + "; ".join(gate.violations[:3])
        )
    applied = _run_git(["apply", "--whitespace=nowarn"], cwd=workdir, input_text=patch)
    if applied.returncode != 0:
        raise WorktreeError(f"could not restore parent changes: {applied.stderr.strip()[:500]}")


def conversation_prompt(job: ClaimedJob, *, max_context_chars: int = 40_000) -> str:
    """Build a bounded, runtime-neutral prompt for a follow-up turn."""
    if not job.context_messages:
        return job.task_prompt
    rendered: list[str] = []
    remaining = max_context_chars
    # Prefer the newest context when a long-running thread exceeds the bound.
    for message in reversed(job.context_messages):
        role = str(message.get("role") or "user").upper()
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        chunk = f"{role}: {content}"
        if len(chunk) > remaining:
            chunk = chunk[-remaining:]
        rendered.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    rendered.reverse()
    history = "\n\n".join(rendered)
    return (
        "Continue an existing coding task in the current worktree. Previous successful "
        "edits have already been restored. Preserve them unless the new request asks otherwise.\n\n"
        f"Conversation so far:\n{history}\n\nNEW USER REQUEST:\n{job.task_prompt}"
    )


def _auth_env(clone_token: str | None) -> dict[str, str]:
    """Carry the clone credential in git's environment, never in argv.

    ``GIT_CONFIG_COUNT`` sets config for this invocation only. That matters
    twice over: the token never lands in ``.git/config`` (where the agent would
    read it out of the worktree we are about to mount), and it never appears in
    the process arguments (where any other user on the runner host could see it
    in ``ps`` for the duration of the fetch).
    """
    if not clone_token:
        return {}
    basic = base64.b64encode(f"x-access-token:{clone_token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def _assert_no_credential_on_disk(workdir: str, clone_token: str | None) -> None:
    """Fail rather than mount a worktree that carries the clone credential.

    The whole patch-out design rests on no repository credential reaching the
    sandbox, and this tree is about to be bind-mounted into it. Asserting the
    property beats assuming it: git writes several files during a fetch, and a
    future change to how we authenticate could quietly start recording the
    token in one of them.
    """
    if not clone_token:
        return
    git_dir = pathlib.Path(workdir) / ".git"
    for path in git_dir.rglob("*"):
        # Object and pack files hold repository content, not our configuration,
        # and are compressed; skipping them keeps this check bounded.
        if not path.is_file() or path.is_symlink() or "objects" in path.parts:
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if clone_token in text:
            raise WorktreeError(
                f"refusing to run: the clone credential was written to .git/{path.name}, "
                "which the sandbox would be able to read"
            )


def prepare_worktree(
    *,
    workdir: str,
    repo: str,
    base_sha: str | None,
    clone_token: str | None = None,
    remote_base: str = "https://github.com",
) -> str:
    """Materialize the job's repository in ``workdir`` and return its commit.

    Runs in the **trusted runner**, and deliberately before the agent starts:
    at this point the worktree is ours, not the agent's, so running git here is
    safe in a way that :func:`build_patch` afterwards is not.

    A shallow single-commit fetch, with no remote recorded. Not adding a remote
    is the point — it is what keeps the credential out of ``.git/config``, and
    the runner has no reason to offer the agent a push path it must not use.
    """
    if not _REPO_SLUG.fullmatch(repo or ""):
        raise WorktreeError(f"repo must be 'owner/name', got {repo!r}")
    if base_sha and not _COMMIT_SHA.fullmatch(base_sha):
        raise WorktreeError(f"base_sha must be a commit hash, got {base_sha!r}")

    url = f"{remote_base.rstrip('/')}/{repo}.git"
    auth = _auth_env(clone_token)

    init = _run_git(["init", "--quiet", workdir])
    if init.returncode != 0:
        raise WorktreeError(f"git init failed: {init.stderr.strip()[:200]}")

    # `--end-of-options` so a ref that survived the shape check above still
    # cannot be parsed as an option by a git version we did not anticipate.
    ref = base_sha or "HEAD"
    fetch = _run_git(
        ["fetch", "--quiet", "--depth", "1", url, "--end-of-options", ref],
        cwd=workdir,
        env_extra=auth,
    )
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout).strip().splitlines()
        raise WorktreeError(
            f"could not fetch {repo}@{ref}: {detail[-1] if detail else 'no output'}"
        )

    checkout = _run_git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=workdir)
    if checkout.returncode != 0:
        raise WorktreeError(f"could not check out {ref}: {checkout.stderr.strip()[:200]}")

    resolved = _run_git(["rev-parse", "HEAD"], cwd=workdir).stdout.strip()
    _assert_no_credential_on_disk(workdir, clone_token)
    return resolved or (base_sha or "")


def repo_clone_base_url(
    env: dict[str, str] | None = None, *, clone_token: str | None = None
) -> str:
    """Return the operator-selected repository origin, safely.

    A local Docker stack serves a tiny read-only ``git://`` repository so it
    can run without a GitHub account or a host bind mount. Production keeps
    using GitHub unless its trusted runner explicitly selects another origin.
    A repository credential is never sent over plaintext HTTP or the git
    protocol: those transports provide no confidentiality.
    """
    from urllib.parse import urlparse

    source = env if env is not None else os.environ
    configured = (source.get(ENV_REPO_CLONE_BASE_URL) or "").strip()
    base = configured or "https://github.com"
    parsed = urlparse(base)
    if parsed.scheme not in {"https", "http", "git"} or not parsed.hostname:
        raise WorktreeError(
            f"{ENV_REPO_CLONE_BASE_URL} must be an http(s) or git URL, got {base!r}"
        )
    if clone_token and parsed.scheme != "https":
        raise WorktreeError(
            f"refusing to send a repository credential over {parsed.scheme!r}; "
            f"{ENV_REPO_CLONE_BASE_URL} must use https when clone_token is present"
        )
    return base.rstrip("/")


def existing_checkout_sha(workdir: str, repo: str) -> str | None:
    """Return the checked-out commit if ``workdir`` already holds ``repo``.

    The GitHub Actions dogfood checks the repository out itself, so the runner
    must use that worktree rather than clone over it. Returns ``None`` when
    there is nothing usable here, and raises when a worktree exists but is a
    *different* repository — running a job against the wrong repo would produce
    a patch that looks plausible and applies to nothing.
    """
    if not (pathlib.Path(workdir) / ".git").exists():
        return None
    remote = _run_git(["config", "--get", "remote.origin.url"], cwd=workdir).stdout.strip()
    if remote:
        # Exact `owner/name`, not a suffix: `endswith` accepted a checkout of
        # `acme/foo` for a job targeting `me/foo`, and the agent would then run
        # against the wrong codebase and produce a patch that applies to
        # nothing.
        slug = remote.removesuffix(".git").rsplit(":", 1)[-1].strip("/")
        slug = "/".join(slug.split("/")[-2:])
        if slug.lower() != repo.lower():
            raise WorktreeError(
                f"the working tree holds {slug!r} but this job targets {repo!r}; "
                "refusing to run an agent against the wrong repository"
            )
    head = _run_git(["rev-parse", "HEAD"], cwd=workdir)
    return head.stdout.strip() if head.returncode == 0 else None


def checkout_output_branch(workdir: str, branch_id: str) -> str:
    """Put a fresh durable worktree on the publisher's local output branch."""
    branch = branch_name_for(branch_id)
    switched = _run_git(["checkout", "--quiet", "-B", branch, "HEAD"], cwd=workdir)
    if switched.returncode != 0:
        raise WorktreeError(
            f"could not create workspace branch {branch}: {switched.stderr.strip()[:200]}"
        )
    return branch


def align_existing_checkout(workdir: str, *, checked_out: str, base_sha: str | None) -> str:
    """Move a pre-populated worktree onto the commit the job pinned.

    The Actions runner checks out whatever ref triggered the workflow, which
    has nothing to do with the commit an owner pinned on their job. Running the
    agent on the wrong tree is not a visible failure: the store keeps the
    owner's pinned sha, and the publisher then applies a patch generated
    against one commit onto a different one. ``--3way`` makes that *usually*
    succeed, which is worse than failing — the PR looks plausible and encodes
    changes nobody wrote.

    Returns the commit actually in the worktree afterwards.
    """
    if not base_sha:
        return checked_out
    if not _COMMIT_SHA.fullmatch(base_sha):
        raise WorktreeError(f"base_sha must be a commit hash, got {base_sha!r}")

    # The owner may have pinned an abbreviated sha; compare resolved commits.
    resolved = _run_git(["rev-parse", "--verify", "--quiet", f"{base_sha}^{{commit}}"], cwd=workdir)
    target = resolved.stdout.strip()
    if resolved.returncode != 0 or not target:
        raise WorktreeError(
            f"the prepared working tree does not contain {base_sha}; refusing to run "
            "the agent against a different commit than the job pinned"
        )
    if target == checked_out:
        return checked_out

    switched = _run_git(["checkout", "--quiet", "--detach", target], cwd=workdir)
    if switched.returncode != 0:
        raise WorktreeError(
            f"could not check out the pinned commit {base_sha}: {switched.stderr.strip()[:200]}"
        )
    return target


def build_patch(
    workdir: str, backend: SandboxBackend | None = None, *, base_sha: str | None = None
) -> str:
    """Return the agent's work as a patch, or an empty string if nothing changed.

    Runs git **inside the sandbox**, never in the trusted runner. The agent
    owns this worktree, including its ``.git/config`` — and git reads that
    file. A trusted process running ``git diff`` here would execute whatever
    the agent put in ``diff.external`` (or a textconv filter, or
    ``core.fsmonitor``), with the runner's privileges: the dispatcher
    credential, and in the Compose deployment the Docker socket. Generating
    the patch in the same container the agent already ran in keeps that
    execution inside the boundary that was built for it.

    A disposable index records intent-to-add entries so new files appear in
    the diff without changing the owner's real staging state. Nothing is
    committed or pushed: the trusted publisher owns that side and re-validates
    the patch before it touches a repository.
    """
    if base_sha and not _COMMIT_SHA.fullmatch(base_sha):
        raise WorktreeError(f"base_sha must be a commit hash, got {base_sha!r}")
    target = base_sha or "HEAD"
    # Diff against the pinned base, not necessarily HEAD: the workspace
    # terminal legitimately lets its owner create local commits, and those
    # changes still belong in the platform-owned publishing patch.
    # Use a disposable index: inspecting/publishing a durable worktree must not
    # leave intent-to-add entries behind in the owner's real Git index.
    script = (
        'index_path="${TMPDIR:-/tmp}/hyi-patch-index.$$"; '
        'rm -f "$index_path"; trap \'rm -f "$index_path"\' EXIT; '
        f'GIT_INDEX_FILE="$index_path" git read-tree {target} && '
        'GIT_INDEX_FILE="$index_path" git add -A -N >/dev/null 2>&1 && '
        f'GIT_INDEX_FILE="$index_path" git diff --binary {target}'
    )
    if backend is None:
        # No sandbox available (unit tests, an operator running by hand). Run
        # with config sources neutralised — this is weaker than the sandbox,
        # because repo-local .git/config is still honoured by git, so it is
        # only appropriate where the worktree is already trusted.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        result = subprocess.run(
            ["/bin/sh", "-c", script],
            cwd=workdir,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise WorktreeError(
                f"could not build the workspace patch: {result.stderr.strip()[-500:]}"
            )
        return result.stdout

    process = backend.spawn(
        SandboxSpec(argv=["/bin/sh", "-c", script], workdir=workdir, env=_SAFE_DIRECTORY_ENV)
    )
    return _read_bounded(process, deadline_s=_PATCH_TIMEOUT_S, max_bytes=_PATCH_MAX_BYTES)


def _read_bounded(process: Any, *, deadline_s: float, max_bytes: int) -> str:
    """Drain a sandbox process's stdout under a deadline and a byte cap.

    This step still runs git against a worktree the agent owned for the whole
    run, including its ``.git/config``. A ``diff.external`` (or a textconv
    filter, or ``core.fsmonitor``) that never returns would otherwise block
    here forever — and because the heartbeat thread keeps renewing the lease
    until the enclosing ``finally``, the attempt never expires, the reaper
    never takes it, and a ``--loop`` runner claims nothing further. One hostile
    repository would take a runner out permanently.
    """
    chunks: list[str] = []
    total = 0
    lines: queue.Queue[str | None] = queue.Queue()

    def _pump() -> None:
        try:
            for line in process.lines():
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=_pump, daemon=True, name="agent-patch").start()
    deadline = time.monotonic() + deadline_s
    while True:
        if time.monotonic() > deadline:
            process.kill()
            raise WorktreeError(
                f"building the patch exceeded {deadline_s:.0f}s; the working tree's git "
                "configuration is agent-controlled and may be hostile"
            )
        try:
            line = lines.get(timeout=_POLL_INTERVAL_S)
        except queue.Empty:
            continue
        if line is None:
            break
        total += len(line.encode())
        if total > max_bytes:
            process.kill()
            raise WorktreeError(
                f"the patch exceeded {max_bytes} bytes while being read; refusing to "
                "buffer unbounded agent output"
            )
        chunks.append(line)

    exit_code = process.wait()
    if exit_code != 0:
        stderr = process.stderr_text().strip()
        raise WorktreeError(
            f"could not build the workspace patch (exit {exit_code}): {stderr[-500:]}"
        )
    return "".join(chunks)


def save_workspace_snapshot(
    control: ControlPlane,
    *,
    workdir: str,
    patch: str,
    retained_backend: RetainedChatBackend | None = None,
) -> bool:
    """Store the changed-file overlay without making it job-critical.

    The patch remains the authoritative publishing artifact. Files are a
    convenience view, so a local read error or an oversized artifact must not
    turn an otherwise completed agent run into a failed job. Lease loss is
    different: the attempt is fenced and must stop immediately.
    """
    try:
        content = (
            build_retained_workspace_snapshot(retained_backend, patch)
            if retained_backend is not None
            else build_workspace_snapshot(workdir, patch)
        )
        control.save_artifact("workspace_snapshot", content)
    except LeaseLost:
        raise
    except Exception as exc:
        print(f"workspace snapshot unavailable: {exc}", file=sys.stderr)
        return False
    return True


def _export_checkpoint(
    control: ControlPlane, *, session: Any, job: ClaimedJob, backend: Any
) -> None:
    """Package the turn's session into a ThreadCheckpoint and hand it up."""
    try:
        exported = session.export_state()
    except DriverError as exc:
        control.append_event(
            NormalizedEvent(
                "lifecycle", {"phase": "checkpoint_export_failed", "detail": str(exc)[:200]}
            )
        )
        return
    stamper = getattr(backend, "version_stamp", None)
    stamp: dict[str, Any] = stamper() if stamper is not None else {}
    envelope = {
        "harness_state": {
            "driver": session.runtime.name,
            "driver_version": (stamp.get("runtime_versions") or {}).get(session.runtime.name, ""),
            "state_format_version": 1,
            "runtime_image_digest": stamp.get("sandbox_image_digest", ""),
            "sha256": hashlib.sha256(exported.blob).hexdigest(),
            "size": len(exported.blob),
            "blob": base64.b64encode(exported.blob).decode("ascii"),
        },
        "consistency": {"through_turn_id": job.job_id, "attempt_id": job.attempt_id},
        "session_id": exported.session_id,
    }
    try:
        control.save_artifact("checkpoint", json.dumps(envelope))
    except LeaseLost:
        raise
    except Exception as exc:
        control.append_event(
            NormalizedEvent(
                "lifecycle", {"phase": "checkpoint_export_failed", "detail": str(exc)[:200]}
            )
        )
        return
    control.append_event(
        NormalizedEvent("lifecycle", {"phase": "checkpoint_exported", "size": len(exported.blob)})
    )


def _promote_to_resident(
    control: ControlPlane,
    *,
    runtime: AgentRuntime,
    job: ClaimedJob,
    backend: Any,
    workdir: str,
    state_dir: str,
    residents: ResidentPool | None,
    session_id: str | None = None,
) -> int | None:
    """Keep this thread's workspace resident; return the generation held.

    Nothing is spawned here: what stays is the worktree, the state mount with
    the session files the turn just wrote, and the CP-fenced residency row —
    plus, when the turn ran live, the process the caller attaches right after
    this with the generation returned here. A follow-up the process cannot
    serve restarts the CLI with ``--resume`` over the same tree.

    The return value is run_once's receipt: a residency this attempt
    established or refreshed is dropped unless the attempt confirms success,
    so a retry can never resume a session a failed attempt already advanced
    (review: the post-promotion lease-loss leak).
    """
    if residents is None or not job.thread_id:
        return None
    # A turn on a borrowed live session parses its events through *that*
    # session's runtime instance, so the caller passes the announced id in;
    # every restart-shaped path still reads this turn's own runtime.
    session_id = session_id or getattr(runtime, "session_id", None)
    if not session_id:
        return None
    held = residents.lookup(job.thread_id)
    if held is not None and held.grant_model is not None and held.grant_model != job.model:
        # A model switch re-keys the residency (D6 restart + design §14): the
        # grant is model-scoped, so refreshing in place would pin the thread
        # to a credential no further turn can use — warm forever. Dropping
        # first makes the offer below establish a successor generation, whose
        # mint is scoped to this turn's model.
        residents.drop(job.thread_id, release=True, generation=held.generation)
    stamper = getattr(backend, "version_stamp", None)
    stamp: dict[str, Any] = stamper() if stamper is not None else {}
    runtime_id = getattr(runtime, "name", "generic")
    generation = residents.offer(
        thread_id=job.thread_id,
        session_id=session_id,
        workdir=workdir,
        state_dir=state_dir,
        runtime_id=runtime_id,
        runtime_version=(stamp.get("runtime_versions") or {}).get(runtime_id),
        model=job.model,
    )
    control.append_event(
        NormalizedEvent(
            "lifecycle",
            {
                "phase": (
                    "residency_established" if generation is not None else "residency_declined"
                )
            },
        )
    )
    return generation


def _resident_base_ready(workdir: str, job: ClaimedJob, backend: SandboxBackend) -> bool:
    """Is the turn's pinned base resolvable in the resident tree?

    Probed **inside the sandbox**, exactly like ``build_patch`` and for the
    same reason: this tree — ``.git/config`` included — has belonged to the
    agent since adopt, and trusted-runner git against it would execute
    whatever the agent left in ``diff.external`` or ``core.fsmonitor`` with
    the runner's privileges. git's dubious-ownership refusal enforces the
    same boundary from the other side — the tree was chowned to the sandbox
    user at adopt, so runner-side git refuses it outright, which is how the
    staging acceptance caught the first version of this gate running in the
    wrong place (every resident turn silently demoted to cold).

    A base the tree cannot resolve ends the residency rather than fetching
    into it: the fetch would need the clone credential inside a filesystem
    the agent controlled, and the cold restart this degrades to clones
    runner-side, before the tree is handed over — where that credential
    already lives. A publish moving the base mid-thread is rare enough that
    one honest cold turn is the right price.
    """
    if not job.base_sha:
        return True
    if not _COMMIT_SHA.fullmatch(job.base_sha):
        return False
    process = backend.spawn(
        SandboxSpec(
            argv=["git", "cat-file", "-e", f"{job.base_sha}^{{commit}}"],
            workdir=workdir,
            env=dict(_SAFE_DIRECTORY_ENV),
        )
    )
    try:
        _read_bounded(process, deadline_s=_GIT_TIMEOUT_S, max_bytes=65536)
    except WorktreeError:
        return False
    return True


def state_from_checkpoint(envelope: dict[str, Any]) -> HarnessState | None:
    """Decode a claim's checkpoint envelope; ``None`` on any defect (replay)."""
    harness = envelope.get("harness_state") or {}
    session_id = str(envelope.get("session_id") or "")
    blob_b64 = harness.get("blob")
    if not session_id or not isinstance(blob_b64, str):
        return None
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except (ValueError, TypeError):
        return None
    if hashlib.sha256(blob).hexdigest() != harness.get("sha256"):
        return None
    return HarnessState(session_id=session_id, blob=blob)


def runtime_version_unavailable(job: ClaimedJob, backend: SandboxBackend) -> str | None:
    """Why this host cannot run the version the attempt is stamped with, if so.

    Normally unreachable: the control plane refuses a selection no host offers,
    and the claim falls back when the *claiming* host does not list it. What is
    left is drift — a host rebuilt with a shorter menu, claiming in the window
    before its heartbeat says so.

    Checked against the image's own labels rather than by probing inside the
    container. ``install-runtimes.sh`` installs and labels from the same
    argument and fails the build when a version does not install, so a version
    the labels offer is a version the image holds.
    """
    version = job.runtime_version
    if not version:
        # No selection reached this attempt — an older control plane, or a host
        # that never reported a version. The image default is exactly right.
        return None
    stamper = getattr(backend, "version_stamp", None)
    stamp: dict[str, Any] = stamper() if stamper is not None else {}
    available = (stamp.get("runtime_versions_available") or {}).get(job.runtime)
    if available is None or version in available:
        return None
    return (
        f"this attempt is stamped {job.runtime} {version}, which this host's sandbox "
        f"image does not contain (it offers: {', '.join(available) or 'nothing'}). "
        "Rebuild the image with that version in its *_EXTRA_VERSIONS, or clear the "
        "selection on the admin page."
    )


def _select_runtime_version(
    env: dict[str, str], *, job: ClaimedJob, backend: SandboxBackend
) -> None:
    """Point the sandbox's PATH at the CLI version this attempt is stamped with.

    A PATH prepend is the whole mechanism: every runtime resolves its binary by
    name — ``binary = "claude"``, and the pi/opencode/kilo wrappers end in
    ``os.execvp("pi", ...)`` — so neither the adapters nor the wrappers have to
    know a version was chosen.

    **Raises rather than falling back**, even though ``run_once`` has already
    reported the same condition as a failed attempt. With the versioned
    directory absent the binary name still resolves — to the image's symlinked
    default — and the job would run a different CLI than its attempt records.
    That row is what the heartbeat, the audit trail and the checkpoint gate all
    read as fact, and nothing downstream could notice it was wrong. A refused
    attempt is recoverable; a false record is not.
    """
    reason = runtime_version_unavailable(job, backend)
    if reason is not None:
        raise SandboxError(reason)
    if job.runtime_version:
        bin_dir = runtime_bin_dir(job.runtime, job.runtime_version)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', SANDBOX_PATH)}"


def run_agent(
    runtime: AgentRuntime,
    *,
    job: ClaimedJob,
    workdir: str,
    gateway_base_url: str,
    control: ControlPlane,
    heart: Heartbeater,
    timeout_s: float,
    backend: SandboxBackend,
    mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
    state: HarnessState | None = None,
    state_dir: str | None = None,
    resident: ResidentEntry | None = None,
    residents: ResidentPool | None = None,
    live_out: dict[str, Any] | None = None,
    context_status: str | None = None,
    strict_native_resume: bool = False,
    export_checkpoint: bool = True,
) -> tuple[int, str, list[str]]:
    """Run the agent, streaming its output back as normalized events.

    This frame exists to own the resource sampler. It is the only one every
    exit passes: the sampler starts deep inside the turn, at whichever spawn
    the turn actually uses, and an exception raised while preparing that turn
    used to leave its thread sampling for the life of the runner process.

    The rollup is reported whether or not anything was read. A window with
    coverage 0 is the row that says "this turn was not measured", and
    withholding it is what makes an unmeasured turn indistinguishable from
    one that consumed nothing — the single distinction this whole signal
    exists to preserve.
    """
    sampler = ResourceSampler(None)
    tool_windows = ToolWindowTracker()
    turn_started_mono = time.monotonic()
    observed_processes: list[SandboxProcess] = []
    observation_cursor = None
    observation_started = False
    live_observation_cursor = None
    live_observation_error: str | None = None
    live_observation_owner = (
        resident.live_session
        if resident is not None
        and resident.credential is not None
        and job.model == resident.grant_model
        and not mcp_config.server_ids
        and resident.live_session is not None
        and bool(getattr(resident.live_session, "process_alive", lambda: False)())
        else None
    )
    begin_observation = getattr(backend, "begin_guest_observation", None)

    def begin_turn_observation() -> None:
        """Fence after platform preparation and immediately before agent exec."""
        nonlocal observation_cursor, observation_started
        if observation_started or begin_observation is None:
            return
        observation_started = True
        try:
            observation_cursor = begin_observation()
        except Exception as exc:
            observation_cursor = None
            print(f"guest process observer unavailable: {exc}", file=sys.stderr)

    def begin_live_turn_observation(owner: Any) -> None:
        """Fence a surviving one-shot guest only when its prompt is attempted.

        The outer frame identifies the owner early so it can always close the
        cursor, but starting the cursor here avoids charging idle activity (or
        a previous turn's terminal observation) when the process dies before
        ``_run_agent`` reaches the HOT rung and falls back to a fresh process.
        """
        nonlocal live_observation_cursor, live_observation_error
        if (
            owner is not live_observation_owner
            or live_observation_cursor is not None
            or live_observation_error is not None
        ):
            return
        begin_live_observation = getattr(
            live_observation_owner,
            "begin_guest_observation",
            None,
        )
        if begin_live_observation is not None:
            try:
                live_observation_cursor = begin_live_observation()
            except Exception as exc:
                live_observation_error = str(exc)
                print(f"resident guest process observer unavailable: {exc}", file=sys.stderr)

    # Constructed before the turn so it remembers where the mark file already
    # ended: a resident session writes every turn's marks to the same file.
    kata_tool_marks = getattr(backend, "runtime", None) == KATA_RUNTIME
    mark_reader = (
        getattr(backend, "tool_mark_reader", None)
        if runtime.name == "claude-code" and not kata_tool_marks
        else None
    )
    try:
        tool_marks = (
            mark_reader()
            if mark_reader is not None
            else ToolMarkReader(
                state_dir if runtime.name == "claude-code" and not kata_tool_marks else None
            )
        )
    except Exception as exc:
        print(f"exact tool marks unavailable: {exc}", file=sys.stderr)
        tool_marks = ToolMarkReader(None)
    try:
        return _run_agent(
            runtime,
            job=job,
            workdir=workdir,
            gateway_base_url=gateway_base_url,
            control=control,
            heart=heart,
            timeout_s=timeout_s,
            backend=backend,
            mcp_config=mcp_config,
            state=state,
            state_dir=state_dir,
            resident=resident,
            residents=residents,
            live_out=live_out,
            context_status=context_status,
            strict_native_resume=strict_native_resume,
            export_checkpoint=export_checkpoint,
            sampler=sampler,
            tool_windows=tool_windows,
            observed_processes=observed_processes,
            begin_guest_observation=begin_turn_observation,
            begin_live_guest_observation=begin_live_turn_observation,
        )
    finally:
        with contextlib.suppress(Exception):
            rollup = sampler.stop()
            observations: list[GuestObservation] = []
            finish_observation = getattr(backend, "finish_guest_observation", None)
            if finish_observation is not None and observation_started:
                try:
                    observation = finish_observation(observation_cursor)
                except Exception as exc:
                    observation = GuestObservation.unavailable(str(exc))
                if observation is not None:
                    observations.append(observation)
            if live_observation_error is not None:
                observations.append(GuestObservation.unavailable(live_observation_error))
            elif live_observation_owner is not None and live_observation_cursor is not None:
                finish_live_observation = getattr(
                    live_observation_owner,
                    "finish_guest_observation",
                    None,
                )
                if finish_live_observation is not None:
                    try:
                        observation = finish_live_observation(live_observation_cursor)
                    except Exception as exc:
                        observation = GuestObservation.unavailable(str(exc))
                    if observation is not None:
                        observations.append(observation)
            for process in observed_processes:
                finish_process_observation = getattr(
                    process,
                    "finish_guest_observation",
                    None,
                )
                if finish_process_observation is None:
                    continue
                try:
                    observation = finish_process_observation()
                except Exception as exc:
                    observation = GuestObservation.unavailable(str(exc))
                if observation is not None:
                    observations.append(observation)
            # Kata marks come from the guest-root journal under the same UID
            # and loss fence as process events. Never mix in the agent-writable
            # legacy file there; it remains only the trusted-runc fallback.
            marks = list(tool_marks.read())
            for observation in observations:
                marks.extend(observation.tool_marks)
            tool_windows.apply_marks(marks)
            windows = tool_windows.windows()
            # After ``stop``, because it takes the window's closing reading:
            # attributing before it would hide whatever the last call did.
            tool_calls = attribute(
                sampler.samples(),
                windows,
                windows_complete=not tool_windows.truncated,
            )
            if observations:
                processes: list[dict[str, Any]] = []
                merged_processes_truncated = False
                process_counts = dict.fromkeys(range(1, len(windows) + 1), 0)
                for observation in observations:
                    rows, counts = attribute_processes(
                        observation,
                        windows,
                        origin_host_mono=turn_started_mono,
                    )
                    for row in rows:
                        if len(processes) >= MAX_OBSERVED_PROCESSES:
                            merged_processes_truncated = True
                            continue
                        row["process_seq"] = len(processes) + 1
                        processes.append(row)
                    for call_seq, count in counts.items():
                        process_counts[call_seq] += count
                sources = {
                    observation.source
                    for observation in observations
                    if observation.source is not None
                }
                process_observer = (
                    next(iter(sources))
                    if len(sources) == 1
                    else GUEST_OBSERVER_SOURCE
                    if kata_tool_marks
                    else None
                )
                observation_complete = (
                    not merged_processes_truncated
                    and len(sources) == 1
                    and all(observation.complete for observation in observations)
                )
                # A lossy census may have missed a process a call started, so
                # its exit-accounted numbers are floors, whatever survived.
                tool_calls = add_process_resources(
                    add_process_counts(tool_calls, process_counts),
                    processes,
                    observation_complete=observation_complete,
                )
                rollup.update(
                    {
                        "process_observer": process_observer,
                        "process_observation_complete": observation_complete,
                        "process_events_lost": sum(
                            observation.lost_events for observation in observations
                        ),
                        "processes_truncated": any(
                            observation.truncated for observation in observations
                        )
                        or merged_processes_truncated,
                        "processes": processes,
                    }
                )
            rollup["tool_calls"] = tool_calls
            rollup["tool_calls_truncated"] = tool_windows.truncated
            # The union, so the reader can see tool-bracketed versus non-tool
            # time. An incomplete call list cannot support that split.
            rollup["tool_time_ms"] = None if tool_windows.truncated else tool_time_ms(windows)
            control.report_resources(rollup)


def _run_agent(
    runtime: AgentRuntime,
    *,
    job: ClaimedJob,
    workdir: str,
    gateway_base_url: str,
    control: ControlPlane,
    heart: Heartbeater,
    timeout_s: float,
    backend: SandboxBackend,
    mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
    state: HarnessState | None = None,
    state_dir: str | None = None,
    resident: ResidentEntry | None = None,
    residents: ResidentPool | None = None,
    live_out: dict[str, Any] | None = None,
    context_status: str | None = None,
    strict_native_resume: bool = False,
    export_checkpoint: bool = True,
    sampler: ResourceSampler,
    tool_windows: ToolWindowTracker,
    observed_processes: list[SandboxProcess],
    begin_guest_observation: Any,
    begin_live_guest_observation: Any,
) -> tuple[int, str, list[str]]:
    """Run the agent, streaming its output back as normalized events.

    Returns ``(exit_code, tail, tool_errors)``. Raises :class:`LeaseLost` if this attempt
    stops owning the job mid-run.

    ``state`` is the parent turn's exported session (already decrypted and
    gated by the control plane): restore it and the turn runs with the
    agent's full memory on just the new prompt. Restore failing — or absent —
    falls back to the transcript replay this function always ran, inside the
    same attempt (design §9). ``state_dir`` is the host directory the sandbox
    persists session files into; without it nothing is exported or restored.

    ``live_out`` is how a surviving process leaves this function: a completed
    turn on a live session puts ``{"session", "session_id"}`` there, and the
    caller either attaches the session to the resident pool (which owns its
    teardown from then on) or closes it. A turn that ends any other way closes
    a live session it created itself; one borrowed from the pool is closed by
    the pool teardown its failure triggers.
    """

    def _report_continuity(mode: str) -> None:
        """Keep the pre-context-status control-client seam source-compatible."""
        if context_status is None:
            control.report_continuity(mode)
        else:
            control.report_continuity(mode, context_status=context_status)

    def _spawn_sampled(process: SandboxProcess) -> SandboxProcess:
        """Point the sampler at the sandbox this turn is about to use.

        Rebinding rather than binding once: every spawn is a *new* container,
        so a sampler still holding the previous one would keep reporting a
        container this turn does not own — and, on the hot fallback path, one
        that is already dead.

        Everything here is contained. This sits directly in the path that
        starts the agent, so a sampler that raised would not degrade a
        measurement — it would cost the user their turn.
        """
        try:
            # A VM-isolated runtime (kata) runs the workload inside a guest
            # kernel; the daemon's accounting for that container describes the
            # hypervisor, not the agent. Confident numbers about the wrong
            # thing are worse than none, so it is honestly not measured.
            sampler.rebind(
                None
                if getattr(backend, "is_vm_isolated", False)
                else getattr(process, "container", None)
            )
        except Exception as exc:
            print(f"resource sampler unavailable: {exc}", file=sys.stderr)
        observed_processes.append(process)
        return process

    def _spawn_turn(spec: SandboxSpec) -> SandboxProcess:
        """Fence after driver preparation, before the agent can execute."""
        begin_guest_observation()
        return _spawn_sampled(backend.spawn(spec))

    # Hermetic environment, and the *backend's* idea of it rather than the
    # runner's. For an isolated backend the runner's PATH and HOME name paths
    # inside the runner's own filesystem: forwarding HOME=/root into a
    # container that runs unprivileged makes every agent CLI fail on startup
    # trying to write its config. Inheriting the whole environment would be
    # worse still — it would hand the agent whatever the host is carrying.
    base_env = dict(backend.base_env())
    base_env.update(_SAFE_DIRECTORY_ENV)
    _select_runtime_version(base_env, job=job, backend=backend)

    driver = driver_for(runtime)

    def adopt_state() -> None:
        """Re-adopt only the runner-owned mount root, never a driver child."""
        assert state_dir is not None
        backend.adopt_workdir(state_dir)

    context = LaunchContext(
        workdir=workdir,
        gateway_base_url=gateway_base_url,
        # Only the model-scoped credential crosses into the sandbox. The
        # full capability token — which can write events, artifacts and
        # terminal states — stays out here with the runner. After E9,
        # grant scope and TTL bound the sandbox credential; spend remains
        # subject to the owning user's gateway quota. There is
        # deliberately no per-job monetary cap.
        credential=job.sandbox_token,
        # Runtime-specific permission flags must follow the actual
        # execution boundary. In particular, Codex cannot nest bubblewrap
        # inside the capability-dropped container backend.
        provides_isolation=backend.provides_isolation,
        # The platform places the process inside the sandbox (design D7);
        # the driver owns everything on the returned handle's transport.
        spawn=lambda argv, env: _spawn_turn(
            SandboxSpec(
                argv=argv,
                workdir=workdir,
                env=env,
                state_dir=state_dir,
                observe_processes=True,
            )
        ),
        base_env=base_env,
        # The runner is the only source of this value. Repository contents
        # never become runtime MCP configuration, and adapters without a
        # gateway-mediated path reject a non-empty set.
        mcp_config=mcp_config,
        state_dir=state_dir,
        install_tool_hooks=getattr(backend, "install_tool_hooks", None),
        adopt_state=adopt_state if state_dir is not None else None,
    )
    session = None
    turn = None
    turn_input: TurnInput | None = None
    #: A live session this turn spawned and still owns — closed on every exit
    #: except the completed hand-off to ``live_out``. One borrowed from the
    #: pool is the pool's to close, via the entry drop its failure triggers.
    fresh_live = None
    hot_ok = (
        resident is not None
        and resident.credential is not None
        and job.model == resident.grant_model
        and not mcp_config.server_ids
    )
    if hot_ok and resident is not None and resident.live_session is not None:
        # A hot turn spawns nothing, so the spawn seam above never fires. The
        # rung that reuses a process is exactly the rung whose consumption is
        # least obvious, so it is measured through the session it inherits.
        with contextlib.suppress(Exception):
            sampler.rebind(
                None
                if getattr(backend, "is_vm_isolated", False)
                else getattr(resident.live_session, "container", None)
            )
    stale_process = resident is not None and not hot_ok and resident.live_session is not None
    if stale_process and residents is not None and job.thread_id:
        # This turn will advance the thread past the idle process's memory —
        # another model (D6: restart), or an MCP turn whose relay token a
        # resident environment cannot carry yet. The process is stale the
        # moment the turn runs; the workspace stays resident.
        residents.detach_session(job.thread_id)
    if hot_ok:
        assert resident is not None
        # True HOT (design §7 + §14): the thread's own live process, running
        # on the residency's thread-attributed grant — minted at establish,
        # renewed by host beats, revoked at release/lapse/takeover. The
        # per-attempt token never enters this environment.
        live = resident.live_session
        was_alive = live is not None and bool(getattr(live, "process_alive", lambda: False)())
        if live is not None and not was_alive:
            # Died idle (or never spawned): not this turn's transport, but
            # the workspace is intact — respawn below, resume on disk.
            if residents is not None and job.thread_id:
                residents.detach_session(job.thread_id)
            live = None
        if live is None:
            launch_resident_live = getattr(driver, "launch_resident_live", None)
            if launch_resident_live is not None:
                hot_context = replace(
                    context,
                    credential=resident.credential,
                    # Sampled through the same seam as any other spawn: a
                    # resident process is still a container this turn runs
                    # work in, and it was the one rung nothing measured.
                    spawn_live=lambda argv, env: _spawn_turn(
                        SandboxSpec(
                            argv=argv,
                            workdir=workdir,
                            env=env,
                            state_dir=state_dir,
                            live_input=True,
                            observe_processes=True,
                        )
                    ),
                )
                try:
                    live = launch_resident_live(hot_context, session_id=resident.session_id)
                except DriverError:
                    live = None
                else:
                    fresh_live = live
        if live is not None:
            if state_dir is not None:
                backend.adopt_workdir(state_dir)
            try:
                begin_live_guest_observation(live)
                begin_guest_observation()
                turn = live.start_turn(
                    TurnInput(
                        prompt=job.task_prompt,
                        model=job.model,
                        reasoning_effort=job.reasoning_effort,
                    )
                )
            except DriverError as exc:
                # The process is gone, or refused the turn. Close what we
                # hold and run the ladder from warm in the same tree.
                if fresh_live is None and residents is not None and job.thread_id:
                    residents.detach_session(job.thread_id)
                else:
                    with contextlib.suppress(Exception):
                        live.close()
                fresh_live = None
                control.append_event(
                    NormalizedEvent(
                        "lifecycle", {"phase": "hot_fallback", "detail": str(exc)[:200]}
                    )
                )
            else:
                session = live
                turn_input = TurnInput(
                    prompt=job.task_prompt,
                    model=job.model,
                    reasoning_effort=job.reasoning_effort,
                )
                # ``hot`` only when the process predates the turn — a fresh
                # spawn is warm_native whatever transport it runs on, and a
                # label that overclaims makes the hit-rate lie.
                _report_continuity("hot" if was_alive else "warm_native")
    if session is None and resident is not None:
        # The restart rung over the resident workspace (design §7): a fresh
        # process resumed with ``--resume``, on this attempt's own token —
        # the path for threads whose residency has no grant, model switches,
        # and MCP turns.
        launch_resident = getattr(driver, "launch_resident", None)
        try:
            if launch_resident is None:
                raise DriverError(f"the {job.runtime!r} driver has no resident turns")
            session = launch_resident(context, session_id=resident.session_id)
        except DriverError as exc:
            if strict_native_resume:
                raise
            # Fall through to the ladder in the same tree.
            control.append_event(
                NormalizedEvent("lifecycle", {"phase": "warm_fallback", "detail": str(exc)[:200]})
            )
            session = None
        else:
            turn_input = TurnInput(
                prompt=job.task_prompt,
                model=job.model,
                reasoning_effort=job.reasoning_effort,
            )
            _report_continuity("warm_native")
    if session is None and state is not None:
        try:
            session = driver.launch(context, state)
        except DriverError as exc:
            # In-attempt fallback (design §9): the turn still runs, on replay,
            # and the degradation is a first-class row in the owner's stream.
            control.append_event(
                NormalizedEvent("lifecycle", {"phase": "cold_fallback", "detail": str(exc)[:200]})
            )
            session = None
        else:
            # The restored session *is* the memory: only the new prompt goes in.
            turn_input = TurnInput(
                prompt=job.task_prompt,
                model=job.model,
                reasoning_effort=job.reasoning_effort,
            )
            _report_continuity("cold_native")
    if session is None:
        session = driver.launch(context)
        turn_input = TurnInput(
            prompt=conversation_prompt(job),
            model=job.model,
            reasoning_effort=job.reasoning_effort,
        )
        if context_status == "new":
            # A first turn has no earlier context to replay or restore. Keep
            # the diagnostic rung empty instead of showing a false "replay"
            # warning in the existing UI.
            report_context = getattr(control, "report_context_status", None)
            if report_context is not None:
                report_context("new")
        else:
            _report_continuity("replay")
    if turn is None:
        assert turn_input is not None
        if state_dir is not None:
            # After restore, before spawn: whatever the driver just wrote (and
            # the empty dir on a fresh turn) must be writable by the sandbox
            # user, or the CLI cannot record the very session the next turn
            # would resume.
            backend.adopt_workdir(state_dir)
        turn = session.start_turn(turn_input)

    try:
        deadline = time.monotonic() + timeout_s
        tail: list[str] = []
        # Tool failures the agent may or may not acknowledge. A model that says "I
        # added the docstring" after its Edit was denied is a real behaviour we
        # observed, so the runner reports what the tools did rather than trusting
        # the agent's summary.
        tool_errors: list[str] = []
        # One row per host, not per retry: an agent that retries a blocked address
        # ten times tried to reach one place.
        denied_hosts: set[str] = set()
        forced_exit_code: int | None = None
        interrupt_deadline: float | None = None

        # Read stdout on a separate thread and poll for it here, so cancellation
        # and the deadline are honoured even when the agent goes quiet. Iterating
        # the stream directly meant a hung or long-thinking agent — which produces
        # no lines — was never checked, and ignored an owner's cancel until it
        # happened to print something.
        lines: queue.Queue[str | None] = queue.Queue()

        def _pump() -> None:
            try:
                for line in turn.raw_lines():
                    lines.put(line)
            finally:
                lines.put(None)

        pump = threading.Thread(target=_pump, daemon=True, name="agent-stdout")
        pump.start()

        while True:
            if heart.lease_lost:
                turn.cancel()
                if resident is not None and residents is not None and job.thread_id:
                    # The fence just moved: this workspace's residency must stop
                    # being reported the moment the turn aborts, not whenever the
                    # exception finishes unwinding (review finding 2). run_once's
                    # wrapper repeats this for every other exception; drop is
                    # idempotent and generation-scoped.
                    residents.drop(job.thread_id, release=True, generation=resident.generation)
                raise LeaseLost("lease lost while the agent was running")
            now = time.monotonic()
            if heart.cancel_requested and forced_exit_code is None:
                turn.cancel()
                control.append_event(NormalizedEvent("lifecycle", {"phase": "cancelled_by_owner"}))
                forced_exit_code = 130
                interrupt_deadline = now + _TURN_INTERRUPT_DRAIN_S
            if forced_exit_code is None and now > deadline:
                turn.cancel()
                control.append_event(
                    NormalizedEvent("error", {"text": f"agent exceeded {timeout_s:.0f}s"})
                )
                forced_exit_code = 124
                interrupt_deadline = now + _TURN_INTERRUPT_DRAIN_S
            if interrupt_deadline is not None and now > interrupt_deadline:
                return forced_exit_code or 1, "".join(tail), tool_errors

            try:
                line = lines.get(timeout=_POLL_INTERVAL_S)
            except queue.Empty:
                # No output yet; loop so the checks above keep running.
                continue
            if line is None:
                break

            tail.append(line)
            del tail[:-40]
            item = turn.event_for_line(line)
            # Stamped here rather than at the control plane, which is where
            # every duration the footer shows today is stamped: that clock
            # includes the runner's upload queue, this one does not.
            tool_windows.observe(item.event_type, item.payload, mono=time.monotonic())
            if item.event_type != LINE:
                if item.event_type == "tool_result" and (item.payload or {}).get("is_error"):
                    content = str((item.payload or {}).get("content", ""))
                    tool_errors.append(content[:200])
                    blocked = detect_blocked_egress(content)
                    if blocked and blocked not in denied_hosts:
                        denied_hosts.add(blocked)
                        # `host` is what makes this a first-class egress row in the
                        # owner's stream rather than one more failed command.
                        control.append_event(NormalizedEvent("error", {"host": blocked}))
                control.append_event(NormalizedEvent(item.event_type, item.payload))

        result = turn.wait()
        exit_code = forced_exit_code if forced_exit_code is not None else result.exit_code
        if live_out is not None and result.status == "completed" and forced_exit_code is None:
            # What the caller needs to keep the thread resident: the announced
            # session id (captured by the session's own runtime — for a borrowed
            # live session that is a previous turn's instance, not this turn's),
            # and the process itself when it survived its turn.
            live_out["session_id"] = getattr(session.runtime, "session_id", None)
            if bool(getattr(session, "process_alive", lambda: False)()):
                live_out["session"] = session
        if (
            result.status == "completed"
            and forced_exit_code is None
            and state_dir is not None
            and export_checkpoint
            and driver.capabilities().checkpoint_export
        ):
            # Every settled turn exports (design §6): this is COLD's substrate and
            # HOT's crash insurance. Failure is non-fatal — the user's work is the
            # patch, and losing a checkpoint costs one replay, not the turn — but
            # it is a first-class row, because a thread that quietly went
            # forgetful is the old bug wearing a new face.
            _export_checkpoint(control, session=session, job=job, backend=backend)
        stderr = turn.stderr_text()
        if stderr.strip():
            tail.append(stderr[-2000:])
        if tool_errors:
            # Surfaced as an event so the owner sees it in the stream, not only in
            # the final detail string.
            control.append_event(
                NormalizedEvent(
                    "error",
                    {"detail": f"{len(tool_errors)} tool call(s) failed", "first": tool_errors[0]},
                )
            )
        return exit_code, "".join(tail), tool_errors
    finally:
        # Every exit but the completed hand-off closes a live session this
        # turn spawned: a leaked resident process would keep running — and
        # keep holding its grant-scoped key — with no entry owning it.
        if fresh_live is not None and not (
            live_out is not None and live_out.get("session") is fresh_live
        ):
            with contextlib.suppress(Exception):
                fresh_live.close()


def _configured_restartable_runtimes() -> set[str]:
    """Return the explicit retained-runtime gate, defaulting to every adapter."""
    raw = os.environ.get("AGENT_RESTARTABLE_RUNTIMES")
    if not raw:
        return set(registered_runtimes())
    return {item.strip() for item in raw.split(",") if item.strip()}


def _configured_shared_live_runtimes() -> set[str]:
    """Return the exact runtimes explicitly admitted to the shared protocol."""
    raw = os.environ.get("AGENT_SHARED_LIVE_RUNTIMES")
    if not raw:
        return set(registered_runtimes())
    return {item.strip() for item in raw.split(",") if item.strip()}


def _shared_live_feature_enabled() -> bool:
    """Whether this host opted into creating new shared-live sandbox groups."""
    return os.environ.get("AGENT_SHARED_LIVE_SANDBOX", "") not in ("", "0")


def _shared_live_host_capabilities(
    backend: SandboxBackend,
    stamp: dict[str, Any],
) -> list[str]:
    """Advertise shared-v1 only when this exact host can honor its contract."""
    if not _shared_live_feature_enabled() or not isinstance(backend, ContainerBackend):
        return []
    try:
        slots = int(os.environ.get("AGENT_HOST_SLOTS") or 0)
    except ValueError:
        return []
    if slots < 2:
        return []
    try:
        resident_capacity = int(os.environ.get("AGENT_SHARED_RESIDENT_CAPACITY") or 8)
    except ValueError:
        return []
    if not 2 <= resident_capacity <= 128:
        return []
    if not (os.environ.get("AGENT_WORKSPACE_BROKER_URL") or "").strip():
        return []
    if not (os.environ.get("AGENT_WORKSPACE_BROKER_TOKEN") or "").strip():
        return []
    digest = stamp.get("sandbox_image_digest")
    if not isinstance(digest, str) or not digest.strip():
        return []
    if stamp.get("shared_sandbox_protocol") != SHARED_SANDBOX_PROTOCOL:
        return []
    versions = stamp.get("runtime_versions")
    if not isinstance(versions, dict) or any(
        versions.get(runtime_id) != version
        for runtime_id, version in SHARED_SANDBOX_RUNTIME_PINS.items()
    ):
        return []
    helpers = stamp.get("helper_versions")
    if not isinstance(helpers, dict) or any(
        helpers.get(helper_id) != version
        for helper_id, version in SHARED_SANDBOX_HELPER_PINS.items()
    ):
        return []
    for runtime_id in _configured_shared_live_runtimes():
        try:
            capabilities = driver_for(get_runtime(runtime_id)).capabilities()
        except KeyError:
            return []
        if (
            capabilities.resident_transport != "supervisor_live"
            or capabilities.fork != "latest_immediate"
            or not capabilities.concurrent_shared_workspace
        ):
            return []
    return ["shared-sandbox-v1"]


def _claim_attestation(backend: SandboxBackend) -> dict[str, Any]:
    """Build the exact-replica claim proof, including local rollout config."""
    stamper = getattr(backend, "version_stamp", None)
    stamp: dict[str, Any] = stamper() if stamper is not None else {}
    # ``version_stamp`` proves the immutable image. This separately proves the
    # polling process has the feature flag, broker, slots and driver surfaces
    # needed to use it; a sibling replica's host heartbeat cannot stand in for
    # those process-local facts during a rolling deployment.
    stamp["runner_capabilities"] = _shared_live_host_capabilities(backend, stamp)
    return stamp


def _retained_lifecycle_enabled() -> bool:
    """Whether either retained protocol needs host lifecycle reconciliation."""
    return _shared_live_feature_enabled() or os.environ.get(
        "AGENT_RESTARTABLE_SANDBOX", ""
    ) not in ("", "0")


def _shared_live_runtime_enabled(job: ClaimedJob) -> bool:
    """Route an existing shared member or a capability-gated new chat."""
    existing = job.thread_sandbox or {}
    if job.thread_sandbox is not None:
        # Durable ownership wins over a later rollout-flag change. Silently
        # switching either direction would create a second workspace.
        return existing.get("shared_live") is True
    return _shared_live_feature_enabled() and job.runtime in _configured_shared_live_runtimes()


def _restartable_mode_covers_every_runtime() -> bool:
    """Whether this runner has no legacy job path that needs a host bind."""
    if _shared_live_feature_enabled() and set(registered_runtimes()) <= (
        _configured_shared_live_runtimes()
    ):
        return True
    if os.environ.get("AGENT_RESTARTABLE_SANDBOX", "") in ("", "0"):
        return False
    return set(registered_runtimes()) <= _configured_restartable_runtimes()


def _restartable_runtime_enabled(job: ClaimedJob) -> bool:
    """Whether this turn belongs on the staged retained-container path."""
    if _shared_live_runtime_enabled(job):
        return True
    if job.thread_sandbox is not None:
        # Once a thread owns Docker-only local state, falling back to a fresh
        # legacy worktree would be silent context loss. The durable row wins
        # over a later flag change; rollback must drain/migrate explicitly.
        return True
    if os.environ.get("AGENT_RESTARTABLE_SANDBOX", "") in ("", "0"):
        return False
    return job.runtime in _configured_restartable_runtimes()


def _session_supervisor_from_env() -> SessionSupervisorClient:
    url = (os.environ.get("AGENT_WORKSPACE_BROKER_URL") or "").strip()
    token = (os.environ.get("AGENT_WORKSPACE_BROKER_TOKEN") or "").strip()
    if not url or not token:
        raise SandboxError(
            "restartable chat execution needs AGENT_WORKSPACE_BROKER_URL and "
            "AGENT_WORKSPACE_BROKER_TOKEN"
        )
    return SessionSupervisorClient(url, token, timeout=180)


def _retained_sandbox_heartbeat_rows() -> list[dict[str, Any]] | None:
    """Read Docker truth from the host singleton without breaking host beats.

    The host heartbeat must remain a liveness signal even while the workspace
    broker is restarting. Returning ``None`` means "not observed this beat";
    it must never be confused with an authoritative empty list or deletion.
    """
    client: SessionSupervisorClient | None = None
    try:
        client = _session_supervisor_from_env()
        rows = client.list()
        observations: list[dict[str, Any]] = []
        now = time.monotonic()
        idle_timeout_s = _chat_idle_timeout_s()
        for row in rows[:4096]:
            shared_live = bool(row.get("shared_live"))
            members = (
                [dict(item) for item in row.get("members") or [] if isinstance(item, dict)]
                if shared_live
                else []
            )
            activation_generation = int(row["activation_generation"])
            running = bool(row["running"])
            active = (bool(row.get("group_busy")) if shared_live else bool(row.get("busy"))) or int(
                row.get("terminal_count") or 0
            ) > 0
            active = active or int(row.get("operation_count") or 0) > 0
            active = active or bool(row.get("workspace_suspended"))
            deadline = row.get("idle_deadline_monotonic")
            # A reconstructed generation-zero entry is unknown while merely
            # RUNNING.  Its empty local token set nevertheless needs one
            # durable recovery window: a broker may have restarted after a
            # terminal cleared the database deadline, and reporting nothing
            # forever would leak that container in RUNNING. The control plane
            # accepts this generation-zero idle window only when no attempt
            # owns the sandbox and its deadline is still NULL, so repeated
            # heartbeats neither stop an active turn nor extend the window.
            reconstructed_idle = (
                activation_generation == 0 and running and not active and deadline is None
            )
            activity_known = (
                activation_generation > 0 or active or deadline is not None or reconstructed_idle
            )
            idle_remaining = None
            if activity_known and deadline is not None:
                idle_remaining = max(0.0, float(deadline) - now)
            elif reconstructed_idle:
                idle_remaining = idle_timeout_s
            observations.append(
                {
                    "thread_id": str(row["thread_id"]),
                    "sandbox_id": str(row["sandbox_id"]),
                    "container_id": str(row["container_id"]),
                    "generation": int(row["generation"]),
                    "activation_generation": activation_generation,
                    "running": running,
                    "activity_active": active if activity_known else None,
                    "idle_remaining_seconds": idle_remaining,
                    "shared_live": shared_live,
                    "members": members,
                }
            )
        return observations
    except (KeyError, TypeError, ValueError, SandboxError, SessionSupervisorError) as exc:
        print(
            f"agent runner: retained sandbox observation unavailable: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None
    finally:
        if client is not None:
            client.close()


def _stop_retained_sandboxes(commands: list[dict[str, Any]]) -> None:
    """Apply durable settled-idle stop commands without breaking host beats."""
    client: SessionSupervisorClient | None = None
    try:
        client = _session_supervisor_from_env()
        for command in commands:
            try:
                if command.get("shared_live"):
                    client.stop_shared_idle(
                        str(command["thread_id"]),
                        generation=int(command["generation"]),
                        reason=str(command.get("reason") or "idle"),
                    )
                else:
                    client.stop_idle(
                        str(command["thread_id"]),
                        generation=int(command["generation"]),
                        activation_generation=int(command["activation_generation"]),
                        reason=str(command.get("reason") or "idle"),
                    )
            except (KeyError, TypeError, ValueError, SessionSupervisorError) as exc:
                # A stale command is expected during a racing acquire. The
                # next heartbeat reports fresh Docker state and the durable
                # predicate decides again; never fail host liveness for it.
                print(
                    f"agent runner: retained idle stop was not applied: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
    except (SandboxError, SessionSupervisorError) as exc:
        print(
            f"agent runner: retained idle stop unavailable: {exc}",
            file=sys.stderr,
            flush=True,
        )
    finally:
        if client is not None:
            client.close()


def _handle_host_residencies_lost(
    residents: ResidentPool,
    lost: list[dict[str, Any]],
) -> None:
    """Fence both legacy runner entries and supervisor-owned shared members."""
    residents.handle_lost(lost)
    client: SessionSupervisorClient | None = None
    try:
        client = _session_supervisor_from_env()
        shared_members = {
            str(member.get("thread_id"))
            for row in client.list()
            if row.get("shared_live")
            for member in row.get("members") or []
            if isinstance(member, dict) and member.get("thread_id")
        }
        for item in lost:
            thread_id = str(item.get("thread_id") or "")
            generation = item.get("generation")
            if thread_id not in shared_members or not isinstance(generation, int):
                continue
            with contextlib.suppress(SessionSupervisorError):
                client.shared_residency_lost(thread_id, generation=generation)
    except (SandboxError, SessionSupervisorError) as exc:
        print(
            f"agent runner: shared residency cleanup unavailable: {exc}",
            file=sys.stderr,
            flush=True,
        )
    finally:
        if client is not None:
            client.close()


def _prepare_retained_checkout(
    *,
    job: ClaimedJob,
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
) -> str:
    """Clone outside the chat, scrub credentials, then stream a normalized tar."""
    with tempfile.TemporaryDirectory(prefix="freeinference-source-") as source:
        base_sha = prepare_worktree(
            workdir=source,
            repo=job.repo,
            base_sha=job.base_sha,
            clone_token=job.clone_token,
            remote_base=repo_clone_base_url(clone_token=job.clone_token),
        )
        checkout_output_branch(source, job.thread_id or job.job_id)
        archive = build_source_archive(
            source,
            uid=manager.backend.uid,
            gid=manager.backend.gid,
        )
        manager.import_archive(spec, CHAT_WORKDIR, archive)
    return base_sha


def _import_retained_base(
    *,
    job: ClaimedJob,
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
) -> str:
    """Fetch a follow-up base outside the sandbox and import objects only."""
    with tempfile.TemporaryDirectory(prefix="freeinference-base-") as source:
        resolved = prepare_worktree(
            workdir=source,
            repo=job.repo,
            base_sha=job.base_sha,
            clone_token=job.clone_token,
            remote_base=repo_clone_base_url(clone_token=job.clone_token),
        )
        bundle = build_base_bundle(source, commit=resolved)
        manager.import_base_bundle(
            spec,
            bundle,
            commit=resolved,
            env=dict(_SAFE_DIRECTORY_ENV),
        )
    return resolved


def _retained_head(backend: RetainedChatBackend) -> str:
    process = backend.spawn(
        SandboxSpec(
            argv=["git", "rev-parse", "HEAD"],
            workdir=CHAT_WORKDIR,
            env=dict(_SAFE_DIRECTORY_ENV),
        )
    )
    return _read_bounded(process, deadline_s=_GIT_TIMEOUT_S, max_bytes=65536).strip()


def _verified_native_session_id(
    *,
    previous_session_id: str | None,
    observed_session_id: str | None,
    has_context: bool,
) -> str:
    """Return the exact CLI locator only when native continuity was proved.

    A successful exit is not enough for a retained follow-up: some CLIs can
    recover from an invalid ``resume`` argument by creating a fresh session.
    Accepting that process would silently turn RESTORED/LIVE into transcript-
    free context loss. Every supported adapter reports its native locator, so
    resumed turns must report the same one they were asked to continue.
    """
    observed = str(observed_session_id or "").strip()
    if not observed:
        raise SandboxError("Agent CLI completed without a native session locator")
    previous = str(previous_session_id or "").strip()
    if has_context and observed != previous:
        raise SandboxError(
            "Agent CLI resumed a different native session "
            f"(expected {previous!r}, observed {observed!r})"
        )
    return observed


def _shared_group_manifest(reservation: dict[str, Any], *, thread_id: str) -> list[dict[str, Any]]:
    """Project the durable member manifest onto the broker's strict wire shape."""
    raw = reservation.get("group_members")
    members = [dict(item) for item in raw or [] if isinstance(item, dict)]
    if not members:
        members = [dict(reservation)]
    allowed = {
        "thread_id",
        "native_session_id",
        "context_status",
        "activation_generation",
        "process_generation",
        "residency_generation",
        "resident_state",
        "resident_slot_id",
        "last_settled_job_id",
    }
    projected = [
        {key: value for key, value in member.items() if key in allowed} for member in members
    ]
    if not any(member.get("thread_id") == thread_id for member in projected):
        raise SandboxError("shared sandbox manifest omitted the claimed chat")
    for member in projected:
        if member.get("resident_slot_id") is None:
            raise SandboxError("shared sandbox manifest contains an unassigned resident slot")
    return projected


def _stream_shared_agent_turn(
    *,
    supervisor: SessionSupervisorClient,
    job: ClaimedJob,
    control: ControlPlane,
    heart: Heartbeater,
    lease_token: str,
    timeout_s: float,
) -> tuple[int, str, list[str], dict[str, Any] | None]:
    """Relay exactly one broker-framed member turn under its attempt fence."""
    stream = supervisor.start_shared_turn(
        job.thread_id or "",
        token=lease_token,
        prompt=job.task_prompt,
        model=job.model,
        reasoning_effort=job.reasoning_effort,
    )
    frames: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def pump() -> None:
        try:
            for frame in stream.frames():
                frames.put(frame)
        except Exception as exc:
            frames.put({"kind": "client_error", "detail": str(exc)[:1000]})
        finally:
            frames.put(None)

    reader = threading.Thread(target=pump, daemon=True, name="shared-agent-stream")
    reader.start()
    deadline = time.monotonic() + timeout_s
    tail: list[str] = []
    tool_errors: list[str] = []
    denied_hosts: set[str] = set()
    result_frame: dict[str, Any] | None = None
    forced_exit_code: int | None = None
    interrupt_deadline: float | None = None
    try:
        while True:
            if heart.lease_lost:
                with contextlib.suppress(Exception):
                    supervisor.interrupt_shared(job.thread_id or "", token=lease_token)
                raise LeaseLost("lease lost while the shared Agent CLI was running")
            now = time.monotonic()
            if heart.cancel_requested and forced_exit_code is None:
                with contextlib.suppress(Exception):
                    supervisor.interrupt_shared(job.thread_id or "", token=lease_token)
                control.append_event(NormalizedEvent("lifecycle", {"phase": "cancelled_by_owner"}))
                forced_exit_code = 130
                interrupt_deadline = now + _SHARED_TURN_INTERRUPT_DRAIN_S
            if forced_exit_code is None and now > deadline:
                with contextlib.suppress(Exception):
                    supervisor.interrupt_shared(job.thread_id or "", token=lease_token)
                control.append_event(
                    NormalizedEvent("error", {"text": f"agent exceeded {timeout_s:.0f}s"})
                )
                forced_exit_code = 124
                interrupt_deadline = now + _SHARED_TURN_INTERRUPT_DRAIN_S
            if interrupt_deadline is not None and now > interrupt_deadline:
                return forced_exit_code or 1, "".join(tail), tool_errors, result_frame
            try:
                frame = frames.get(timeout=_POLL_INTERVAL_S)
            except queue.Empty:
                continue
            if frame is None:
                break
            kind = frame.get("kind")
            if kind == "event":
                raw_line = frame.get("raw_line")
                if isinstance(raw_line, str):
                    tail.append(raw_line)
                    del tail[:-40]
                event_type = str(frame.get("event_type") or "")
                payload = frame.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                if event_type == "tool_result" and payload.get("is_error"):
                    content = str(payload.get("content") or "")
                    tool_errors.append(content[:200])
                    blocked = detect_blocked_egress(content)
                    if blocked and blocked not in denied_hosts:
                        denied_hosts.add(blocked)
                        control.append_event(NormalizedEvent("error", {"host": blocked}))
                if event_type and event_type != LINE:
                    control.append_event(NormalizedEvent(event_type, payload))
                continue
            if kind == "result":
                result_frame = frame
                stderr = str(frame.get("stderr") or "")
                if stderr.strip():
                    tail.append(stderr[-2000:])
                continue
            if kind == "error":
                result_frame = frame
                detail = str(frame.get("detail") or "shared turn failed")
                tail.append(detail[-2000:])
                return forced_exit_code or 1, "".join(tail), tool_errors, result_frame
            if kind == "client_error":
                if result_frame is not None:
                    break
                raise SessionSupervisorError(502, str(frame.get("detail") or "shared turn failed"))
            raise SessionSupervisorError(502, "shared turn returned an unknown frame")
        if result_frame is None:
            raise SessionSupervisorError(502, "shared turn ended without a result")
        if forced_exit_code is not None:
            return forced_exit_code, "".join(tail), tool_errors, result_frame
        status = str(result_frame.get("status") or "failed")
        exit_code = int(result_frame.get("exit_code") or 0)
        if status == "cancelled":
            exit_code = 130
        elif status != "completed" and exit_code == 0:
            exit_code = 1
        if tool_errors:
            control.append_event(
                NormalizedEvent(
                    "error",
                    {
                        "detail": f"{len(tool_errors)} tool call(s) failed",
                        "first": tool_errors[0],
                    },
                )
            )
        return exit_code, "".join(tail), tool_errors, result_frame
    finally:
        stream.close()


def _run_shared_live_attempt(
    *,
    runtime: AgentRuntime,
    job: ClaimedJob,
    control: ControlPlane,
    heart: Heartbeater,
    backend: SandboxBackend,
    host: str | None,
    gateway_base_url: str,
    agent_timeout_s: float,
    finish_attempt: Any,
) -> int:
    """Deliver one chat turn to its supervisor-owned resident CLI member."""
    if not isinstance(backend, ContainerBackend):
        finish_attempt("failed", "shared live chats require the container or kata backend")
        return 2
    if not host or not job.thread_id:
        finish_attempt("failed", "shared live chats require an enrolled host and thread id")
        return 2
    driver = driver_for(runtime)
    if getattr(driver, "launch_live", None) is None:
        finish_attempt("failed", f"runtime {job.runtime!r} has no shared live transport")
        return 2

    manager = ChatSandboxManager(backend)
    stamp = backend.version_stamp()
    existing = job.thread_sandbox or {}
    if existing and existing.get("shared_live") is not True:
        finish_attempt(
            "failed", "a legacy retained chat cannot be upgraded to shared-live in place"
        )
        return 2
    if not existing and not str(stamp.get("sandbox_image_digest") or "").strip():
        finish_attempt("failed", "shared live chat creation requires an inspected image digest")
        return 2
    if existing and existing.get("runtime_id") != job.runtime:
        finish_attempt(
            "failed",
            f"this shared chat is pinned to runtime {existing.get('runtime_id')!r}",
        )
        return 2
    if existing and existing.get("execution_profile") != job.execution_profile:
        finish_attempt("failed", "shared chat execution profile cannot change in place")
        return 2

    capacity = int(
        existing.get("resident_slot_capacity")
        or os.environ.get("AGENT_SHARED_RESIDENT_CAPACITY")
        or 8
    )
    if not 2 <= capacity <= 128:
        finish_attempt("failed", "shared resident capacity must be between 2 and 128")
        return 2
    sandbox_id = str(existing.get("sandbox_id") or f"asbx_{secrets.token_hex(16)}")
    origin_thread_id = str(existing.get("origin_thread_id") or job.thread_id)
    seed_spec = ChatSandboxSpec(
        thread_id=origin_thread_id,
        sandbox_id=sandbox_id,
        generation=int(existing.get("generation") or 1),
        runtime_id=str(existing.get("runtime_id") or job.runtime),
        runtime_version=(existing.get("runtime_version") if existing else job.runtime_version),
        sandbox_image_digest=(
            existing.get("sandbox_image_digest")
            if existing
            else (stamp.get("sandbox_image_digest") or None)
        ),
        shared_live=True,
        resident_slot_capacity=capacity,
    )
    if existing:
        if existing.get("host") != host:
            finish_attempt("failed", "shared chat was claimed on the wrong host")
            return 2
        if existing.get("container_name") != seed_spec.container_name:
            finish_attempt("failed", "shared chat container identity does not match its labels")
            return 2

    try:
        reservation = control.reserve_chat_sandbox(
            {
                "sandbox_id": seed_spec.sandbox_id,
                "container_name": seed_spec.container_name,
                "runtime_id": seed_spec.runtime_id,
                "runtime_version": seed_spec.runtime_version,
                "sandbox_image_digest": seed_spec.sandbox_image_digest,
                "execution_profile": job.execution_profile,
                "host": host,
                "shared_live": True,
                "resident_slot_capacity": capacity,
                "memory_limit": seed_spec.memory_limit,
                "cpu_limit": seed_spec.cpu_limit,
                "stop_timeout_seconds": seed_spec.stop_timeout_s,
            }
        )
    except LeaseLost:
        return 3
    except Exception as exc:
        finish_attempt("failed", f"could not reserve shared chat sandbox: {exc}")
        return 2

    spec = ChatSandboxSpec(
        thread_id=str(reservation.get("origin_thread_id") or origin_thread_id),
        sandbox_id=str(reservation["sandbox_id"]),
        generation=int(reservation["generation"]),
        runtime_id=str(reservation["runtime_id"]),
        runtime_version=reservation.get("runtime_version"),
        sandbox_image_digest=reservation.get("sandbox_image_digest"),
        shared_live=True,
        resident_slot_capacity=int(reservation.get("resident_slot_capacity") or capacity),
        memory_limit=str(reservation.get("memory_limit") or seed_spec.memory_limit),
        cpu_limit=str(reservation.get("cpu_limit") or seed_spec.cpu_limit),
        stop_timeout_s=int(reservation.get("stop_timeout_seconds") or seed_spec.stop_timeout_s),
    )
    activation_generation = int(reservation["activation_generation"])
    has_context = bool(reservation.get("last_settled_job_id"))
    native_session_id = str(reservation.get("native_session_id") or "").strip() or None
    manifest = _shared_group_manifest(reservation, thread_id=job.thread_id)
    supervisor: SessionSupervisorClient | None = None
    lease_token: str | None = None
    settled = False
    abandoned = False
    turn_admitted = False
    terminals_suspended = False
    context_invalid = False
    failure_detail: str | None = None
    context_status = "restored" if has_context else "new"
    idle_timeout_s = _chat_idle_timeout_s()

    def abandon_before_finish(*, lost: bool, detail: str | None) -> None:
        nonlocal abandoned
        if abandoned or settled:
            return
        if supervisor is not None and lease_token is not None:
            with contextlib.suppress(Exception):
                supervisor.abandon_shared(job.thread_id or "", token=lease_token, lost=lost)
        with contextlib.suppress(Exception):
            control.abandon_chat_sandbox(
                sandbox_generation=spec.generation,
                activation_generation=activation_generation,
                lost=lost,
                detail=detail,
                idle_timeout_seconds=idle_timeout_s,
            )
        abandoned = True

    try:
        supervisor = _session_supervisor_from_env()
        try:
            lease = supervisor.acquire_shared(
                spec,
                thread_id=job.thread_id,
                activation_generation=activation_generation,
                owner=f"attempt:{job.attempt_id}",
                has_settled_context=has_context,
                members=manifest,
                expected_container_id=reservation.get("container_id"),
            )
        except SessionSupervisorError as exc:
            context_invalid = has_context and exc.status_code == 410
            raise
        lease_token = str(lease["token"])
        if int(lease["activation_generation"]) != activation_generation:
            raise SandboxError("shared supervisor returned the wrong activation generation")
        if reservation.get("container_id") not in {None, lease.get("container_id")}:
            raise SandboxError("shared supervisor returned a replacement chat container")
        context_status = str(lease.get("context_status") or context_status)
        if context_status not in {"new", "live", "restored"}:
            raise SandboxError(f"shared supervisor returned invalid context {context_status!r}")
        running = control.mark_chat_sandbox_running(
            sandbox_generation=spec.generation,
            activation_generation=activation_generation,
            container_id=str(lease["container_id"]),
            context_status=context_status,
        )
        retained = RetainedChatBackend(manager, spec)
        control.append_event(
            NormalizedEvent(
                "lifecycle",
                {
                    "phase": "started",
                    "runtime": job.runtime,
                    "attempt_no": job.attempt_no,
                    "context_status": context_status,
                    "resident_state": lease.get("resident_state"),
                    "workspace_shared": True,
                    "container_id": lease["container_id"],
                    "activation_generation": activation_generation,
                    **retained.sandbox_metadata(),
                },
            )
        )
        control.suspend_terminals("workspace_preparing")
        terminals_suspended = True

        if not has_context:
            manager.reset_unsettled_state(spec)
            base_sha = _prepare_retained_checkout(job=job, manager=manager, spec=spec)
            reused_workspace = False
            if job.setup_script:
                setup = run_setup(
                    script=job.setup_script,
                    workdir=CHAT_WORKDIR,
                    repo=job.repo,
                    backend=retained,
                    cache=None,
                )
                control.append_event(
                    NormalizedEvent(
                        "lifecycle",
                        {"phase": "setup", "cached": False, "ran": setup.ran},
                    )
                )
                if setup.exit_code != 0:
                    failure_detail = f"setup failed ({setup.exit_code}): {setup.detail}"
                    abandon_before_finish(lost=False, detail=failure_detail)
                    finish_attempt("failed", failure_detail, base_sha=base_sha)
                    return 1
            manager.prepare_shared_workspace(spec)
        else:
            if not native_session_id:
                context_invalid = True
                raise SandboxError("shared chat has no native Agent CLI session locator")
            if not _resident_base_ready(CHAT_WORKDIR, job, retained):
                imported_base = _import_retained_base(job=job, manager=manager, spec=spec)
                if not _resident_base_ready(CHAT_WORKDIR, job, retained):
                    context_invalid = True
                    raise SandboxError("shared chat cannot resolve its pinned base commit")
                control.append_event(
                    NormalizedEvent(
                        "lifecycle",
                        {
                            "phase": "base_imported",
                            "base_sha": imported_base,
                            "workspace_head_preserved": True,
                        },
                    )
                )
            base_sha = job.base_sha or _retained_head(retained)
            reused_workspace = True
        control.append_event(
            NormalizedEvent(
                "lifecycle",
                {
                    "phase": "checked_out",
                    "base_sha": base_sha,
                    "reused": reused_workspace,
                    "storage": "docker_writable_layer",
                    "workspace_shared": True,
                },
            )
        )

        version_problem = runtime_version_unavailable(job, retained)
        if version_problem is not None:
            failure_detail = version_problem
            abandon_before_finish(lost=False, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 1
        if not retained.has_binary(runtime.binary):
            failure_detail = f"runtime binary {runtime.binary!r} is not installed"
            abandon_before_finish(lost=False, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 2

        if has_context:
            manager.require_network_phase(spec, "agent")
        else:
            manager.transition_to_agent_network(spec)
        control.resume_terminals()
        terminals_suspended = False

        slot_id = int(running.get("resident_slot_id") or reservation.get("resident_slot_id") or 0)
        launch = ResidentLaunch(
            thread_id=job.thread_id,
            runtime_id=job.runtime,
            runtime_version=job.runtime_version,
            native_session_id=native_session_id,
            model=job.model,
            reasoning_effort=job.reasoning_effort,
            gateway_base_url=gateway_base_url,
            credential=None,
            mcp_server_ids=tuple(job.mcp_servers),
            # The MCP relay token is attempt-fenced. It is process-fixed only
            # when this turn actually exposes MCP endpoints; carrying an
            # otherwise-unused fresh token here would restart every ordinary
            # no-MCP chat and falsely erase HOT continuity.
            mcp_base_url=job.mcp_base_url if job.mcp_servers else "",
            mcp_credential=job.mcp_token if job.mcp_servers else "",
            slot_id=slot_id,
            residency_generation=int(running.get("residency_generation") or 0),
        )
        resident_descriptor: dict[str, Any] | None = None
        if lease.get("resident_state") == "live":
            try:
                resident_descriptor = supervisor.ensure_shared_resident(
                    job.thread_id,
                    token=lease_token,
                    launch=launch,
                )
            except SessionSupervisorError:
                # A process-fixed model/MCP setting changed, the process died,
                # or this broker restarted. Rotate only this member's grant and
                # resume its exact native session below.
                resident_descriptor = None
        if resident_descriptor is None:
            residency = control.establish_shared_residency(
                sandbox_id=spec.sandbox_id,
                runtime_id=job.runtime,
                runtime_version=job.runtime_version,
            )
            credential = str(residency.get("credential") or "")
            if not credential:
                raise SandboxError("control plane returned no shared residency credential")
            grant_model = residency.get("model")
            if grant_model not in {None, job.model}:
                raise SandboxError("shared residency grant model does not match this turn")
            launch = replace(
                launch,
                credential=credential,
                residency_generation=int(residency["generation"]),
            )
            resident_descriptor = supervisor.ensure_shared_resident(
                job.thread_id,
                token=lease_token,
                launch=launch,
            )
        context_status = str(resident_descriptor.get("context_status") or context_status)
        if context_status not in {"new", "live", "restored"}:
            raise SandboxError(
                f"shared supervisor returned invalid resident context {context_status!r}"
            )
        process_generation = int(resident_descriptor["process_generation"])
        control.record_chat_process(
            sandbox_generation=spec.generation,
            activation_generation=activation_generation,
            process_generation=process_generation,
        )
        if context_status == "live":
            control.report_continuity("hot", context_status="live")
        elif has_context:
            control.report_continuity("warm_native", context_status="restored")
        else:
            control.report_context_status("new")

        control.admit_chat_turn(
            sandbox_generation=spec.generation,
            activation_generation=activation_generation,
        )
        turn_admitted = True
        sampler = ResourceSampler(None)
        result_frame: dict[str, Any] | None = None
        try:
            exit_code, tail, tool_errors, result_frame = _stream_shared_agent_turn(
                supervisor=supervisor,
                job=job,
                control=control,
                heart=heart,
                lease_token=lease_token,
                timeout_s=agent_timeout_s,
            )
        finally:
            # Container totals include concurrent siblings and still remain
            # unusable here.  The broker instead returns UID-filtered lifecycle
            # events from the Kata guest; even a process shorter than one stats
            # tick is represented.  If the stream failed before that result,
            # retain the explicit zero-coverage fallback.
            with contextlib.suppress(Exception):
                fallback = sampler.stop()
                observed = result_frame.get("resources") if isinstance(result_frame, dict) else None
                control.report_resources(observed if isinstance(observed, dict) else fallback)
        if exit_code == 130:
            failure_detail = "cancelled by owner after shared-session admission"
            abandon_before_finish(lost=True, detail=failure_detail)
            finish_attempt("cancelled", "cancelled by owner", base_sha=base_sha)
            return 0
        if exit_code == 124:
            failure_detail = "shared Agent CLI turn exceeded its execution timeout"
            abandon_before_finish(lost=True, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 1
        if exit_code != 0:
            failure_detail = f"agent exited {exit_code}: {tail[-500:]}"
            abandon_before_finish(lost=True, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 1
        if tool_errors:
            # Match the existing conservative no-change rule only after a
            # publication snapshot can attribute the shared diff. A successful
            # shared turn may race a sibling, so tool errors alone do not prove
            # this member produced no useful state.
            control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {"phase": "shared_tool_errors_observed", "count": len(tool_errors)},
                )
            )

        local_settle = supervisor.settle_shared(
            job.thread_id,
            token=lease_token,
            job_id=job.job_id,
            context_status=context_status,
        )
        settled_session_id = str(local_settle.get("native_session_id") or "").strip()
        if not settled_session_id:
            raise SandboxError("shared supervisor settled without a native session locator")
        control.settle_chat_sandbox(
            sandbox_generation=spec.generation,
            activation_generation=activation_generation,
            context_status=context_status,
            native_session_id=settled_session_id,
            base_sha=base_sha,
            idle_timeout_seconds=idle_timeout_s,
        )
        settled = True
        control.append_event(
            NormalizedEvent(
                "lifecycle",
                {
                    "phase": "shared_workspace_settled",
                    "workspace_shared": True,
                    "publication": "pending_snapshot",
                },
            )
        )
        finish_attempt(
            "succeeded",
            "agent completed (shared workspace; publication snapshot pending)",
            base_sha=base_sha,
        )
        return 0
    except LeaseLost:
        failure_detail = "attempt lease lost during shared live execution"
        return 3
    except (DriverError, SandboxError, SessionSupervisorError, WorktreeError) as exc:
        failure_detail = str(exc)
        abandon_before_finish(lost=turn_admitted or context_invalid, detail=failure_detail)
        with contextlib.suppress(Exception):
            control.append_event(NormalizedEvent("error", {"text": failure_detail[:1000]}))
            finish_attempt("failed", failure_detail[:1000])
        return 2
    except Exception as exc:
        failure_detail = f"shared live chat failed unexpectedly: {exc}"
        abandon_before_finish(lost=turn_admitted, detail=failure_detail)
        with contextlib.suppress(Exception):
            control.append_event(NormalizedEvent("error", {"text": failure_detail[:1000]}))
            finish_attempt("failed", failure_detail[:1000])
        return 2
    finally:
        if not settled and not abandoned:
            abandon_before_finish(lost=turn_admitted or context_invalid, detail=failure_detail)
        if supervisor is not None:
            supervisor.close()
        if terminals_suspended:
            with contextlib.suppress(Exception):
                control.resume_terminals()


def _run_retained_attempt(
    *,
    runtime: AgentRuntime,
    job: ClaimedJob,
    control: ControlPlane,
    heart: Heartbeater,
    backend: SandboxBackend,
    host: str | None,
    gateway_base_url: str,
    agent_timeout_s: float,
    finish_attempt: Any,
) -> int:
    """Run one turn in its thread's Docker-owned retained container.

    This is intentionally capability-gated to the five pinned native-session
    drivers. A runtime that cannot prove native resume is left on the legacy
    path before it creates a sandbox row; an existing retained row that loses
    that capability fails loudly instead of rebuilding from transcript.
    """
    if not isinstance(backend, ContainerBackend):
        finish_attempt("failed", "restartable chats require the container or kata backend")
        return 2
    if not host or not job.thread_id:
        finish_attempt("failed", "restartable chats require an enrolled host and thread id")
        return 2
    if driver_for(runtime).capabilities().continuity == "replay":
        finish_attempt(
            "failed",
            f"runtime {job.runtime!r} has not passed the restartable native-resume gate",
        )
        return 2

    manager = ChatSandboxManager(backend)
    stamp = backend.version_stamp()
    existing = job.thread_sandbox or {}
    if not existing and not str(stamp.get("sandbox_image_digest") or "").strip():
        # Heartbeats may degrade an unavailable image inspection to an empty
        # stamp, but a new retained chat cannot: its writable layer is the
        # only local copy and every retry must recreate from the exact image
        # that first owned it. Falling back to a mutable tag here would make
        # the supposedly immutable container identity change across a
        # pre-creation retry.
        finish_attempt(
            "failed",
            "restartable chat creation requires an inspected sandbox image digest",
        )
        return 2
    if existing and existing.get("runtime_id") != job.runtime:
        finish_attempt(
            "failed",
            "this restartable chat is pinned to runtime "
            f"{existing.get('runtime_id')!r}; switch Agent CLI by starting a new chat",
        )
        return 2
    if existing and existing.get("execution_profile") != job.execution_profile:
        finish_attempt("failed", "retained chat execution profile cannot change in place")
        return 2
    sandbox_id = str(existing.get("sandbox_id") or f"asbx_{secrets.token_hex(16)}")
    seed_spec = ChatSandboxSpec(
        thread_id=job.thread_id,
        sandbox_id=sandbox_id,
        generation=int(existing.get("generation") or 1),
        runtime_id=str(existing.get("runtime_id") or job.runtime),
        runtime_version=(existing.get("runtime_version") if existing else job.runtime_version),
        sandbox_image_digest=(
            existing.get("sandbox_image_digest")
            if existing
            else (stamp.get("sandbox_image_digest") or None)
        ),
    )
    if existing:
        if existing.get("host") != host:
            finish_attempt("failed", "retained chat was claimed on the wrong host")
            return 2
        if existing.get("container_name") != seed_spec.container_name:
            finish_attempt("failed", "retained chat container identity does not match its labels")
            return 2

    try:
        reservation = control.reserve_chat_sandbox(
            {
                "sandbox_id": seed_spec.sandbox_id,
                "container_name": seed_spec.container_name,
                "runtime_id": seed_spec.runtime_id,
                "runtime_version": seed_spec.runtime_version,
                "sandbox_image_digest": (
                    existing.get("sandbox_image_digest")
                    if existing
                    else (stamp.get("sandbox_image_digest") or None)
                ),
                "execution_profile": (
                    str(existing.get("execution_profile")) if existing else job.execution_profile
                ),
                "host": host,
            }
        )
    except LeaseLost:
        return 3
    except Exception as exc:
        finish_attempt("failed", f"could not reserve retained chat sandbox: {exc}")
        return 2

    spec = ChatSandboxSpec(
        thread_id=job.thread_id,
        sandbox_id=str(reservation["sandbox_id"]),
        generation=int(reservation["generation"]),
        runtime_id=str(reservation["runtime_id"]),
        runtime_version=reservation.get("runtime_version"),
        sandbox_image_digest=reservation.get("sandbox_image_digest"),
    )
    activation_generation = int(reservation["activation_generation"])
    has_context = bool(reservation.get("last_settled_job_id"))
    native_session_id = reservation.get("native_session_id")
    supervisor: SessionSupervisorClient | None = None
    lease_token: str | None = None
    settled = False
    abandoned = False
    turn_admitted = False
    terminals_suspended = False
    failure_detail: str | None = None
    context_invalid = False
    context_status = "restored" if has_context else "new"
    idle_timeout_s = _chat_idle_timeout_s()

    def abandon_before_finish(*, lost: bool, detail: str | None) -> None:
        """Quiesce this activation before its attempt becomes terminal."""
        nonlocal abandoned
        if abandoned or settled:
            return
        if supervisor is not None and lease_token is not None:
            with contextlib.suppress(Exception):
                supervisor.abandon(job.thread_id, token=lease_token, lost=lost)
        with contextlib.suppress(Exception):
            control.abandon_chat_sandbox(
                sandbox_generation=spec.generation,
                activation_generation=activation_generation,
                lost=lost,
                detail=detail,
                idle_timeout_seconds=idle_timeout_s,
            )
        abandoned = True

    try:
        supervisor = _session_supervisor_from_env()
        try:
            lease = supervisor.acquire(
                spec,
                activation_generation=activation_generation,
                owner=f"attempt:{job.attempt_id}",
                has_settled_context=has_context,
                expected_container_id=reservation.get("container_id"),
            )
        except SessionSupervisorError as exc:
            context_invalid = has_context and exc.status_code == 410
            raise
        lease_token = str(lease["token"])
        if int(lease["activation_generation"]) != activation_generation:
            raise SandboxError("Session Supervisor returned the wrong activation generation")
        if reservation.get("container_id") not in {None, lease.get("container_id")}:
            raise SandboxError("Session Supervisor returned a replacement chat container")
        context_status = str(lease.get("context_status") or context_status)
        if context_status not in {"new", "live", "restored"}:
            raise SandboxError(f"Session Supervisor returned invalid context {context_status!r}")
        control.mark_chat_sandbox_running(
            sandbox_generation=spec.generation,
            activation_generation=activation_generation,
            container_id=str(lease["container_id"]),
            context_status=context_status,
        )
        retained = RetainedChatBackend(manager, spec)
        control.append_event(
            NormalizedEvent(
                "lifecycle",
                {
                    "phase": "started",
                    "runtime": job.runtime,
                    "attempt_no": job.attempt_no,
                    "context_status": context_status,
                    "container_id": lease["container_id"],
                    "activation_generation": activation_generation,
                    **retained.sandbox_metadata(),
                },
            )
        )
        control.suspend_terminals("workspace_preparing")
        terminals_suspended = True

        if not has_context:
            # A checkout or setup failure before prompt admission is retryable
            # on the same immutable chat/container identity. Remove any
            # partial import from that failed preparation so Docker's archive
            # extraction cannot merge it into the next clean checkout.
            manager.reset_unsettled_state(spec)
            base_sha = _prepare_retained_checkout(job=job, manager=manager, spec=spec)
            reused_workspace = False
        else:
            if not native_session_id:
                context_invalid = True
                raise SandboxError("retained chat has no native Agent CLI session locator")
            if not _resident_base_ready(CHAT_WORKDIR, job, retained):
                imported_base = _import_retained_base(job=job, manager=manager, spec=spec)
                if not _resident_base_ready(CHAT_WORKDIR, job, retained):
                    context_invalid = True
                    raise SandboxError(
                        "retained chat cannot resolve its pinned base commit after import"
                    )
                control.append_event(
                    NormalizedEvent(
                        "lifecycle",
                        {
                            "phase": "base_imported",
                            "base_sha": imported_base,
                            "workspace_head_preserved": True,
                        },
                    )
                )
            base_sha = job.base_sha or _retained_head(retained)
            reused_workspace = True
        control.append_event(
            NormalizedEvent(
                "lifecycle",
                {
                    "phase": "checked_out",
                    "base_sha": base_sha,
                    "reused": reused_workspace,
                    "storage": "docker_writable_layer",
                },
            )
        )

        if job.setup_script and not has_context:
            setup = run_setup(
                script=job.setup_script,
                workdir=CHAT_WORKDIR,
                repo=job.repo,
                backend=retained,
                # A retained chat already keeps its dependency tree. Sharing a
                # host tar cache would reintroduce the host-state dependency
                # this mode removes.
                cache=None,
            )
            control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {"phase": "setup", "cached": False, "ran": setup.ran},
                )
            )
            if setup.exit_code != 0:
                failure_detail = f"setup failed ({setup.exit_code}): {setup.detail}"
                abandon_before_finish(lost=False, detail=failure_detail)
                finish_attempt("failed", failure_detail, base_sha=base_sha)
                return 1

        version_problem = runtime_version_unavailable(job, retained)
        if version_problem is not None:
            failure_detail = version_problem
            abandon_before_finish(lost=False, detail=failure_detail)
            finish_attempt("failed", version_problem, base_sha=base_sha)
            return 1
        if not retained.has_binary(runtime.binary):
            failure_detail = f"runtime binary {runtime.binary!r} is not installed"
            abandon_before_finish(lost=False, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 2

        process = supervisor.process_started(job.thread_id, token=lease_token)
        process_generation = int(process["process_generation"])
        control.record_chat_process(
            sandbox_generation=spec.generation,
            activation_generation=activation_generation,
            process_generation=process_generation,
        )
        resident = (
            ResidentEntry(
                thread_id=job.thread_id,
                generation=spec.generation,
                session_id=str(native_session_id),
                workdir=CHAT_WORKDIR,
                state_dir=CHAT_STATE_DIR,
                runtime_id=job.runtime,
                model=job.model,
            )
            if has_context
            else None
        )
        live_out: dict[str, Any] = {}
        control.admit_chat_turn(
            sandbox_generation=spec.generation,
            activation_generation=activation_generation,
        )
        turn_admitted = True
        # Setup egress is removed exactly once and never restored. Keep every
        # failure that is still classified as a safe pre-admission retry on
        # the setup network; otherwise a transient control-plane error after
        # this irreversible transition would retry checkout/setup on the
        # agent-only network and fail for an unrelated reason. From this point
        # onward the durable admission receipt makes every failure conservative
        # (STOPPED_DIRTY/LOST), even if stdin was not reached.
        if has_context:
            manager.require_network_phase(spec, "agent")
        else:
            manager.transition_to_agent_network(spec)

        # Never unfreeze a user terminal while setup egress is still present.
        control.resume_terminals()
        terminals_suspended = False
        exit_code, tail, tool_errors = run_agent(
            runtime,
            job=job,
            workdir=CHAT_WORKDIR,
            gateway_base_url=gateway_base_url,
            control=control,
            heart=heart,
            timeout_s=agent_timeout_s,
            backend=retained,
            mcp_config=RuntimeMCPConfig(
                server_ids=tuple(job.mcp_servers),
                base_url=job.mcp_base_url,
                credential=job.mcp_token,
            ),
            state_dir=CHAT_STATE_DIR,
            resident=resident,
            live_out=live_out,
            context_status=context_status,
            strict_native_resume=has_context,
            export_checkpoint=False,
        )
        live = live_out.pop("session", None)
        if live is not None:
            # The reconnectable bridge is the later HOT optimization. Until it
            # owns the CLI independently of a runner replica, close cleanly and
            # prove native restore on the next turn (warm_native).
            with contextlib.suppress(Exception):
                live.close()
        next_session_id = live_out.get("session_id") or getattr(runtime, "session_id", None)
        if exit_code == 130:
            # Cancellation currently uses the retained process kill backstop,
            # which stops the whole activation. There is no clean context
            # boundary to capture after that stop, so do not pretend a patch
            # exec can run or that the session remains resumable.
            failure_detail = "cancelled by owner after retained-session admission"
            abandon_before_finish(lost=True, detail=failure_detail)
            finish_attempt("cancelled", "cancelled by owner", base_sha=base_sha)
            return 0
        if exit_code == 124:
            failure_detail = "retained agent turn exceeded its execution timeout"
            abandon_before_finish(lost=True, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 1
        if exit_code == 0:
            next_session_id = _verified_native_session_id(
                previous_session_id=(str(native_session_id) if native_session_id else None),
                observed_session_id=(str(next_session_id) if next_session_id else None),
                has_context=has_context,
            )

        control.suspend_terminals("workspace_finalizing")
        terminals_suspended = True
        patch = build_patch(CHAT_WORKDIR, retained, base_sha=job.base_sha or base_sha)
        if patch.strip():
            control.save_artifact("patch", patch)
            control.append_event(
                NormalizedEvent("diff", {"bytes": len(patch.encode()), "stored": True})
            )
        else:
            control.append_event(NormalizedEvent("diff", {"bytes": 0, "stored": False}))

        save_workspace_snapshot(
            control,
            workdir=CHAT_WORKDIR,
            patch=patch,
            retained_backend=retained,
        )

        if exit_code != 0:
            failure_detail = f"agent exited {exit_code}: {tail[-500:]}"
            abandon_before_finish(lost=True, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 1
        if not patch.strip() and tool_errors:
            failure_detail = (
                f"agent produced no changes after {len(tool_errors)} failed tool call(s): "
                f"{tool_errors[0]}"
            )
            abandon_before_finish(lost=True, detail=failure_detail)
            finish_attempt("failed", failure_detail, base_sha=base_sha)
            return 1

        # The local lifecycle owner must accept the exact activation before
        # the control plane advertises a settled/resumable boundary. If the
        # broker restarted and forgot this lease token, fail closed as LOST;
        # writing the durable row first would let a failed job masquerade as a
        # clean native checkpoint.
        supervisor.settle(
            job.thread_id,
            token=lease_token,
            job_id=job.job_id,
            context_status=context_status,
        )
        control.settle_chat_sandbox(
            sandbox_generation=spec.generation,
            activation_generation=activation_generation,
            context_status=context_status,
            native_session_id=str(next_session_id),
            idle_timeout_seconds=idle_timeout_s,
        )
        settled = True
        finish_attempt(
            "succeeded",
            "agent completed" if patch.strip() else "agent completed (no changes)",
            base_sha=base_sha,
        )
        terminals_suspended = False
        return 0
    except LeaseLost:
        failure_detail = "attempt lease lost during retained chat execution"
        return 3
    except (DriverError, SandboxError, SessionSupervisorError, WorktreeError) as exc:
        failure_detail = str(exc)
        abandon_before_finish(
            lost=turn_admitted or context_invalid,
            detail=failure_detail,
        )
        with contextlib.suppress(Exception):
            control.append_event(NormalizedEvent("error", {"text": failure_detail[:1000]}))
        with contextlib.suppress(Exception):
            finish_attempt("failed", failure_detail[:1000])
        return 2
    except Exception as exc:
        failure_detail = f"retained chat failed unexpectedly: {exc}"
        abandon_before_finish(lost=turn_admitted, detail=failure_detail)
        with contextlib.suppress(Exception):
            control.append_event(NormalizedEvent("error", {"text": failure_detail[:1000]}))
            finish_attempt("failed", failure_detail[:1000])
        return 2
    finally:
        if not settled and not abandoned:
            abandon_before_finish(
                lost=turn_admitted or context_invalid,
                detail=failure_detail,
            )
        if supervisor is not None:
            supervisor.close()
        if terminals_suspended:
            with contextlib.suppress(Exception):
                control.resume_terminals()


def run_once(
    *,
    base_url: str,
    gateway_url: str = "",
    dispatcher_token: str,
    worker_id: str,
    workdir: str,
    lease_ttl: float = DEFAULT_LEASE_TTL_S,
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    generic_command: str | None = None,
    backend: SandboxBackend | None = None,
    workspace_root: str | None = None,
    host: str | None = None,
    residents: ResidentPool | None = None,
) -> int:
    """Claim one job, run it, and report the outcome. Returns a process exit code.

    ``backend`` decides where the agent actually executes. When this function
    builds it, it is also preflighted here — a misconfigured host then fails
    without first taking a job off the queue and burning one of its attempts.

    A caller that *supplies* a backend has already preflighted it, and must
    not have it re-checked per call. Preflight spawns several ``docker``
    subprocesses (version, one network inspect per phase) and logs the
    shared-kernel warning, so running it per claim meant a standing runner
    polling every 5s spent ~17k probes and 17k warning lines a day doing
    nothing — found by watching an idle runner, not by reading it.
    """
    # Docker resolves bind sources in the daemon's namespace, not relative to
    # the runner container.  Keep the one-shot path honest: ``--workdir .``
    # inside the runner image used to become ``/app`` only after a job had
    # already been claimed, and every sandbox command then failed with exit
    # 125.  Normalising first also makes the path that preflight proves exactly
    # the path later passed to ``docker run --mount``.
    workdir = str(pathlib.Path(workdir).resolve())
    if backend is None:
        backend = build_backend_from_env()
        preflight_root = None if _restartable_mode_covers_every_runtime() else workdir
        if preflight_root is not None:
            # The normal clone path creates this directory after claim.  A
            # bind probe has to run before claim, so materialise only the
            # trusted operator-selected root first.  This also matters under
            # Docker Desktop/Colima: a child created inside the outer runner
            # becomes visible to the daemon, while the same child created by
            # the macOS client may exist only in its file-sharing namespace.
            try:
                pathlib.Path(preflight_root).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SandboxError(
                    f"could not prepare one-shot workdir {preflight_root!r}: {exc}"
                ) from exc
        backend.preflight(workdir_root=preflight_root)

    attestation = _claim_attestation(backend)
    job = claim(
        base_url=base_url,
        dispatcher_token=dispatcher_token,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
        host=host,
        attestation=attestation,
    )
    if job is None:
        print("no queued agent job; nothing to do")
        return 0

    restartable = _restartable_runtime_enabled(job)
    # Long-lived runners give every job a durable worktree.  The browser and
    # terminal broker address this same directory after the agent exits;
    # follow-up jobs receive their own worktree and are rehydrated through the
    # existing, validated context-patch path.
    #
    # A restartable chat is the inverse: its only durable workspace is the
    # retained container's writable layer. Creating a host worktree here would
    # silently retain a second source of truth and defeat the Docker-only
    # ownership invariant even if it were never mounted.
    if workspace_root is not None and not restartable:
        durable = workspace_path(workspace_root, job.job_id)
        durable.mkdir(parents=True, exist_ok=True)
        workdir = str(durable)

    print(f"claimed {job.job_id} attempt {job.attempt_no} runtime={job.runtime}")
    control = ControlPlane(base_url, job.worker_token)
    control.job_id = job.job_id
    heart = Heartbeater(control, ttl=lease_ttl, interval=HEARTBEAT_INTERVAL_S)
    terminals_suspended = False

    def finish_attempt(
        state: str,
        detail: str | None = None,
        *,
        base_sha: str | None = None,
    ) -> None:
        """Finish through the gateway, which also resumes retained terminals."""
        nonlocal terminals_suspended
        if base_sha is None:
            control.finish(state, detail)
        else:
            control.finish(state, detail, base_sha=base_sha)
        terminals_suspended = False

    try:
        runtime = get_runtime(job.runtime, generic_command=generic_command)
    except KeyError as exc:
        finish_attempt("failed", str(exc))
        control.close()
        return 2

    if restartable:
        heart.start()
        try:
            if _shared_live_runtime_enabled(job):
                return _run_shared_live_attempt(
                    runtime=runtime,
                    job=job,
                    control=control,
                    heart=heart,
                    backend=backend,
                    host=host,
                    gateway_base_url=gateway_url,
                    agent_timeout_s=agent_timeout_s,
                    finish_attempt=finish_attempt,
                )
            return _run_retained_attempt(
                runtime=runtime,
                job=job,
                control=control,
                heart=heart,
                backend=backend,
                host=host,
                gateway_base_url=gateway_url,
                agent_timeout_s=agent_timeout_s,
                finish_attempt=finish_attempt,
            )
        finally:
            heart.stop()
            control.close()

    # Session persistence, for runtimes whose driver has real continuity. The
    # state dir is a sibling of the worktree — never inside it, so session
    # files cannot leak into a patch — and per job, like the worktree itself.
    state_dir: str | None = None
    context_state: HarnessState | None = None
    if driver_for(runtime).capabilities().continuity != "replay":
        state_path = pathlib.Path(f"{workdir}.state")
        state_path.mkdir(parents=True, exist_ok=True)
        state_dir = str(state_path)
        if job.context_checkpoint:
            context_state = state_from_checkpoint(job.context_checkpoint)

    # The top rung: this thread may already be resident here (design §7).
    # Reusing the *workspace* needs the same harness and a base the tree can
    # resolve; riding the *live process* additionally needs the residency's
    # grant, its model and an MCP-less turn — run_agent gates that part and
    # falls to a restart in the same tree when it fails.
    resident: ResidentEntry | None = None
    if residents is not None and job.thread_id:
        entry = residents.lookup(job.thread_id)
        if entry is not None:
            # What ends a residency at lookup is a harness switch (another
            # CLI cannot read this session) and a base the tree cannot
            # resolve; model switches and MCP turns reuse the workspace and
            # are gated off the live process further down, in run_agent.
            if entry.runtime_id != job.runtime:
                unusable_reason = "harness_switch"
            elif not _resident_base_ready(entry.workdir, job, backend):
                unusable_reason = "base_unresolvable"
            else:
                unusable_reason = None
                resident = entry
                workdir = entry.workdir
                state_dir = entry.state_dir
                context_state = None
            if unusable_reason is not None:
                # First-class row, not a silent demotion: the staging
                # acceptance spent an hour blind on exactly this branch —
                # every turn quietly cold with promotion still succeeding.
                control.append_event(
                    NormalizedEvent(
                        "lifecycle",
                        {"phase": "residency_unusable", "reason": unusable_reason},
                    )
                )
                # End the residency and run the ladder from COLD — the
                # checkpoint it exported is the memory this turn resumes.
                residents.drop(job.thread_id, release=True)

    # Ask the backend, not this host: with an isolated backend the agent CLIs
    # live in the sandbox image, so probing the runner's own PATH would refuse
    # every job on a correctly configured machine.
    if not backend.has_binary(runtime.binary):
        # Fail the job explicitly rather than letting it hang in `running`
        # until the reaper eventually gives up on it.
        control.append_event(
            NormalizedEvent("error", {"text": f"runtime binary {runtime.binary!r} missing"})
        )
        finish_attempt("failed", f"runtime binary {runtime.binary!r} is not installed")
        control.close()
        return 2

    heart.start()
    # Set only by the succeeded finish. The finally below redeems it: a
    # residency this attempt established, refreshed or borrowed survives
    # *only* a confirmed success — every other exit (failure, cancel, a lease
    # lost anywhere between promotion and finish, an exception a handler
    # swallows) drops it, so a retry can never resume a session this attempt
    # already advanced (review: the post-promotion lease-loss leak).
    residency_confirmed = False
    # The receipt: the generation this attempt borrowed or established. The
    # finally redeems exactly it — a stale cleanup must never end a successor
    # generation another turn established after ours.
    residency_generation_held: int | None = resident.generation if resident else None
    try:
        # A replacement attempt may inherit live user shells from the expired
        # owner. Freeze their complete process trees before checkout or setup;
        # stdin fencing alone cannot stop a watcher or background process.
        terminals_suspended = True
        control.suspend_terminals("workspace_preparing")
        control.append_event(
            NormalizedEvent(
                "lifecycle",
                {
                    "phase": "started",
                    "runtime": job.runtime,
                    "attempt_no": job.attempt_no,
                    **backend.sandbox_metadata(),
                },
            )
        )

        # Give the agent something to work on. Without this the self-hosted
        # runner ran every job in an empty directory: the agent had no code to
        # read, and the patch it produced was necessarily empty.
        #
        # A resident turn skips all of it: the live tree *is* the state the
        # conversation has been building, its base was verified resolvable at
        # lookup, and re-running checkout, context patch or setup over it
        # would be repaving a road with traffic on it.
        try:
            if resident is not None:
                base_sha = job.base_sha
                reused_workspace = True
                checked_out = None
            elif (checked_out := existing_checkout_sha(workdir, job.repo)) is None:
                reused_workspace = False
                base_sha = prepare_worktree(
                    workdir=workdir,
                    repo=job.repo,
                    base_sha=job.base_sha,
                    clone_token=job.clone_token,
                    remote_base=repo_clone_base_url(clone_token=job.clone_token),
                )
                if workspace_root is not None:
                    checkout_output_branch(workdir, job.thread_id or job.job_id)
            else:
                reused_workspace = True
                if workspace_root is not None:
                    # A retry of this same job must preserve edits (and local
                    # commits) already made in its durable worktree.  Fresh
                    # follow-up jobs use a different directory, so there is no
                    # cross-turn base drift to repair here.
                    base_sha = job.base_sha or checked_out
                else:
                    # An externally prepared worktree (the Actions dogfood
                    # checks the repository out itself) may be on an unrelated
                    # ref, so the one-shot path still aligns to the pinned base.
                    base_sha = align_existing_checkout(
                        workdir, checked_out=checked_out, base_sha=job.base_sha
                    )
            # The agent owns this tree from here on, so it must be able to
            # write to it — and git must not see it as another user's repo.
            backend.adopt_workdir(workdir)
        except WorktreeError as exc:
            control.append_event(NormalizedEvent("error", {"text": str(exc)}))
            finish_attempt("failed", str(exc))
            return 2
        control.append_event(
            NormalizedEvent(
                "lifecycle",
                {
                    "phase": "checked_out",
                    "base_sha": base_sha,
                    # A resident turn skipped the clone entirely — the tree is
                    # the one this conversation has been building. Reporting
                    # the same milestone either way told the owner their
                    # thread had been rebuilt when it had not, and contradicted
                    # the continuity badge on the very same turn.
                    "reused": reused_workspace,
                },
            )
        )

        # A durable workspace already contains the preceding turn's edits.
        # Re-applying the stored context patch would duplicate them (and often
        # fail halfway through), so patch rehydration is only for fresh/legacy
        # worktrees.
        if job.context_patch and not reused_workspace:
            try:
                apply_context_patch(workdir, job.context_patch)
            except WorktreeError as exc:
                control.append_event(NormalizedEvent("error", {"text": str(exc)}))
                finish_attempt("failed", str(exc), base_sha=base_sha)
                return 2
            control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {"phase": "context_restored", "parent_job_id": job.parent_job_id},
                )
            )

        # Setup runs before the agent and under its own egress tier: it needs
        # a package registry, and the turn that follows — the one driven by
        # untrusted model output — does not.
        if job.setup_script and resident is None:
            setup = run_setup(
                script=job.setup_script,
                workdir=workdir,
                repo=job.repo,
                backend=backend,
                cache=build_cache_from_env(),
            )
            control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {
                        "phase": "setup",
                        "cached": setup.restored_from_cache,
                        "ran": setup.ran,
                    },
                )
            )
            if setup.exit_code != 0:
                # A job whose dependencies did not install cannot do the work,
                # and letting the agent start anyway produces a confusing
                # failure much later, in the model's voice rather than the
                # installer's.
                finish_attempt(
                    "failed",
                    f"setup failed ({setup.exit_code}): {setup.detail}",
                    base_sha=base_sha,
                )
                return 1

        # Before the agent, and reported rather than raised: an attempt that
        # dies on a lease timeout tells an operator nothing, and this one has a
        # specific, actionable cause.
        version_problem = runtime_version_unavailable(job, backend)
        if version_problem is not None:
            finish_attempt("failed", version_problem, base_sha=base_sha)
            return 1

        # Everything before this point is trusted workspace preparation.
        # Retained terminals resume only for the agent execution window.
        control.resume_terminals()
        terminals_suspended = False

        live_out: dict[str, Any] = {}
        try:
            exit_code, tail, tool_errors = run_agent(
                runtime,
                job=job,
                workdir=workdir,
                # The gateway, not the control plane. Everything above this
                # line spoke to the control plane over `base_url`; the
                # sandbox's model calls go somewhere else entirely, and
                # conflating the two is what this argument exists to stop.
                gateway_base_url=gateway_url,
                control=control,
                heart=heart,
                timeout_s=agent_timeout_s,
                backend=backend,
                # Opaque ids, straight from the claim. The runner still never
                # learns where an upstream server is or what credential
                # reaches it — the relay resolves both. What it does carry
                # now is the relay's own address and token, which are this
                # deployment's, not an upstream's.
                mcp_config=RuntimeMCPConfig(
                    server_ids=tuple(job.mcp_servers),
                    base_url=job.mcp_base_url,
                    credential=job.mcp_token,
                ),
                state=context_state,
                state_dir=state_dir,
                resident=resident,
                residents=residents,
                live_out=live_out,
            )
        except BaseException:
            # A lease lost — or anything else — mid-hot-turn must not leave a
            # dead residency beating: the pool would keep reporting it, the
            # control plane would keep its lease alive, and affinity would
            # keep steering the thread's turns at a workspace whose turn just
            # aborted. Drop by our own (thread, generation) and re-raise; the
            # outer handlers own the attempt's fate.
            if resident is not None and residents is not None and job.thread_id:
                residents.drop(job.thread_id, release=True, generation=residency_generation_held)
            raise
        # A settled turn's workspace is worth keeping — and only a settled
        # one: promotion happens here, before finish, while the attempt still
        # earns the residency; the receipt is redeemed in the finally below.
        # A failed turn keeps nothing; its thread continues COLD off its
        # checkpoint (or the previous turn's).
        if exit_code == 0 and state_dir is not None:
            promoted = _promote_to_resident(
                control,
                runtime=runtime,
                job=job,
                backend=backend,
                workdir=workdir,
                state_dir=state_dir,
                residents=residents,
                session_id=live_out.get("session_id"),
            )
            if promoted is not None:
                residency_generation_held = promoted
            live = live_out.pop("session", None)
            if live is not None:
                if promoted is not None and residents is not None and job.thread_id:
                    # The pool owns the process from here: retire, evict,
                    # sweep and shutdown all close it. Attach is generation-
                    # scoped and closes the session itself on a mismatch.
                    residents.attach_session(job.thread_id, promoted, live)
                else:
                    # Survived its turn but has no residency to live in.
                    with contextlib.suppress(Exception):
                        live.close()
        elif resident is not None and residents is not None and job.thread_id:
            residents.drop(job.thread_id, release=True, generation=residency_generation_held)

        # Freeze both interactive input and already-running terminal process
        # trees before taking the patch and workspace snapshot. The finish
        # endpoint resumes them immediately before the terminal state change.
        terminals_suspended = True
        control.suspend_terminals("workspace_finalizing")
        patch = build_patch(workdir, backend, base_sha=job.base_sha or base_sha)
        if patch.strip():
            control.save_artifact("patch", patch)
            control.append_event(
                NormalizedEvent("diff", {"bytes": len(patch.encode()), "stored": True})
            )
        else:
            control.append_event(NormalizedEvent("diff", {"bytes": 0, "stored": False}))

        save_workspace_snapshot(control, workdir=workdir, patch=patch)

        if exit_code == 130:
            finish_attempt("cancelled", "cancelled by owner", base_sha=base_sha)
            return 0

        if exit_code != 0:
            finish_attempt(
                "failed",
                f"agent exited {exit_code}: {tail[-500:]}",
                base_sha=base_sha,
            )
            return 1

        # The publisher runs outside the sandbox and drives publish/*; the
        # runner's job ends at a validated patch. Leaving the job `running`
        # would be a lie, so report success and let the publisher take it from
        # here when a patch exists.
        if not patch.strip():
            # No patch is only success if nothing went wrong. A denied write
            # followed by an agent claiming it succeeded must not be recorded
            # as a clean run — the owner would see "succeeded" on a job that
            # did nothing and never learn why.
            if tool_errors:
                finish_attempt(
                    "failed",
                    f"agent produced no changes after {len(tool_errors)} failed "
                    f"tool call(s): {tool_errors[0]}",
                    base_sha=base_sha,
                )
                return 1
            finish_attempt("succeeded", "agent completed (no changes)", base_sha=base_sha)
            residency_confirmed = True
            return 0

        finish_attempt("succeeded", "agent completed", base_sha=base_sha)
        residency_confirmed = True
        return 0
    except WorktreeError as exc:
        # Patch generation raises this on a Git failure, timeout or byte cap.
        # Left to escape, the job stayed `running` until its lease expired and
        # was retried — repeating a deterministic failure until the attempt
        # budget ran out, with nothing in the record saying why.
        print(f"patch generation failed: {exc}", file=sys.stderr)
        with contextlib.suppress(Exception):
            control.append_event(NormalizedEvent("error", {"text": str(exc)}))
            finish_attempt("failed", str(exc), base_sha=base_sha)
        return 2
    except LeaseLost as exc:
        print(f"lease lost, stopping: {exc}", file=sys.stderr)
        return 3
    finally:
        if (
            not residency_confirmed
            and residency_generation_held is not None
            and residents is not None
            and job.thread_id
        ):
            with contextlib.suppress(Exception):
                residents.drop(job.thread_id, release=True, generation=residency_generation_held)
        # A surprising runner-side exception must not strand retained user
        # terminals in a suspended state. A stale attempt cannot resume them:
        # the worker endpoint fences this call against its live lease.
        if terminals_suspended:
            with contextlib.suppress(Exception):
                control.resume_terminals()
        if workspace_root is not None:
            with contextlib.suppress(OSError):
                os.utime(workdir, None)
        heart.stop()
        control.close()


#: Set by SIGTERM. A container being replaced gets one, and until this existed
#: it killed the process outright — Python's default handler terminates, and
#: only ``KeyboardInterrupt`` (SIGINT) was caught. Every `docker compose up -d`
#: that recreated a runner therefore abandoned whatever job it was running.
#:
#: That is not only a deploy problem, but the deploy is where it bites on a
#: schedule: since CD, every merge to dev recreates these containers.
_STOPPING = threading.Event()


def _drain_on_sigterm() -> None:
    """Finish the job in hand on SIGTERM, then exit; SIGINT still stops now.

    The split is deliberate. `docker stop` and `compose up -d` send SIGTERM and
    then wait `stop_grace_period` — that wait is exactly the room to let a job
    end, and the container is being replaced anyway, so exiting after it is
    right. A person pressing Ctrl-C means *now*, and turning that into a
    several-minute wait would be its own surprise.

    The lease machinery remains the backstop, not the plan: a job outlasting
    the grace period is SIGKILLed, its lease expires, and the control plane
    supersedes the attempt — which is what happened to every job through a
    deploy before this, and is exactly the outcome this narrows to the rare
    case.
    """

    def handler(signum: int, frame: object) -> None:
        _STOPPING.set()
        print(
            "agent runner: SIGTERM received; finishing the current job, then exiting",
            file=sys.stderr,
            flush=True,
        )

    # Only the main thread may install one; tests and embedders that call
    # `serve` from elsewhere get the old behaviour rather than an exception.
    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGTERM, handler)


def _broker_url_from_env() -> str | None:
    """This host's broker address, **as the control plane will reach it**.

    Defaults to the same variable `workspace_broker_client` uses, so the
    address a host *reports* cannot drift from the one it *serves* — a row
    pointing at a broker this machine is not running answers 409 for every file
    and terminal request on its jobs. That is right whenever both readers sit
    on the same network, which is every co-located deployment.

    It stops being right the moment the control plane is on another machine.
    `AGENT_WORKSPACE_BROKER_URL` is a compose service name; it resolves here and
    nowhere else. Reported verbatim, the control plane stores an address it
    cannot resolve, and the first call that needs the broker — a terminal
    suspend, a file listing — fails with a DNS error surfaced as 503. Every
    attempt then dies at the same point until the job runs out of them, which
    is what happened to the first real job on this deployment.

    So the two questions are separated, and only when a deployment says so:

    * `AGENT_WORKSPACE_BROKER_URL` — how *this runner* reaches its own broker.
    * `AGENT_WORKSPACE_BROKER_ADVERTISE_URL` — how *the control plane* reaches
      it, when that is a different address (a reverse tunnel's far end).

    Unset, the second is the first, and the drift the original comment warns
    about remains impossible without somebody explicitly asking for it.
    """
    advertised = (os.environ.get("AGENT_WORKSPACE_BROKER_ADVERTISE_URL") or "").strip()
    if advertised:
        return advertised
    return (os.environ.get("AGENT_WORKSPACE_BROKER_URL") or "").strip() or None


def _runner_version() -> str:
    """This runner's version, for the pool listing. Empty if unknown.

    Best effort: it is an operator-facing label, not a compatibility gate, and
    a missing version must never stop a host enrolling.
    """
    return (os.environ.get("AGENT_RUNNER_VERSION") or "").strip()


def _housekeeping(root: pathlib.Path, workspace_ttl_s: float) -> None:
    """Reclaim disk between jobs.

    A snapshot cache on the same host as every job worktree must not be the
    thing that fills the disk. Called from both the working path and the
    draining one — a drained machine is often drained *because* something is
    wrong with it, and it is the state a host sits in for hours.

    Every step suppresses its own failure: housekeeping must never be what
    stops a runner serving.
    """
    cache = build_cache_from_env()
    if cache is not None:
        with contextlib.suppress(Exception):
            cache.purge_expired()
    with contextlib.suppress(Exception):
        purge_stale_workspaces(root, ttl_seconds=workspace_ttl_s)


def run_forever(
    *,
    base_url: str,
    gateway_url: str = "",
    dispatcher_token: str,
    worker_id: str,
    workdir_root: str,
    lease_ttl: float = DEFAULT_LEASE_TTL_S,
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    generic_command: str | None = None,
    idle_sleep_s: float = 5.0,
    backend: SandboxBackend | None = None,
    workspace_ttl_s: float = DEFAULT_WORKSPACE_TTL_S,
    host: str | None = None,
) -> int:
    """Claim and run jobs until interrupted — the long-lived runner.

    Each agent job gets a stable directory under ``workdir_root``.  Job and
    user-terminal containers still remain disposable isolation boundaries, but
    the mounted repository persists so Files, Git and the user terminal all see
    the same worktree.  The validated job id keeps neighbouring workspaces
    disjoint even when runners share a host; follow-up turns are rehydrated into
    their own tree through the existing patch flow.

    A failure in one job must not take the runner down: the store already
    records the outcome against that job, and a runner that exits on the first
    bad job turns one broken repository into an outage for every queued job.
    """
    backend = backend or build_backend_from_env()
    root = pathlib.Path(workdir_root)
    root.mkdir(parents=True, exist_ok=True)
    # Pass the workdir root so a backend that bind-mounts it can prove the
    # mount works now, rather than failing every job with an opaque error.
    # Restartable chats keep the repository in the sandbox container's Docker
    # writable layer. There is no host worktree to bind-mount, and requiring
    # one here would defeat the Docker-only storage topology this mode exists
    # to provide (it also fails for a runner that itself runs in Docker, where
    # its filesystem path does not exist in the host daemon's namespace).
    restartable_only = _restartable_mode_covers_every_runtime()
    backend.preflight(workdir_root=None if restartable_only else str(root))
    # Whether this runner can measure at all, checked once and said out loud.
    # The first sampler assumed it ran on the host and read cgroupfs through a
    # pid; deployed as a container beside its sandboxes, every read failed and
    # the only symptom was grey text under every turn. An assumption about the
    # deployment that nothing checks is how that happens twice, so this is the
    # check. It is a warning, never fatal: a runner that cannot measure must
    # still run jobs.
    if getattr(backend, "measurable", True):
        unmeasurable = daemon_reachable()
        if unmeasurable:
            print(
                f"resource measurement is unavailable: {unmeasurable}. Turns will run "
                "normally and report no resource usage.",
                file=sys.stderr,
            )
    # The sandbox must be able to reach the gateway it will be handed —
    # `gateway_url`, not the control plane this runner claims against. Checking
    # the wrong one proves the runner's own connectivity and says nothing about
    # the sandbox's, which is the connection that actually fails per job.
    checker = getattr(backend, "check_gateway_reachable", None)
    if checker is not None:
        checker(gateway_url or base_url)
    where = f" on host {host}" if host else " (no host reported)"

    # Join the pool, if this deployment uses one. `None` means it does not —
    # neither a stored credential nor an enrollment token — which is the
    # single-host shape that predates the pool and has to keep working.
    #
    # The heartbeat runs whether or not jobs do. A host with nothing to do
    # must still report, or the reaper marks it offline and stops sending it
    # work: a pool that empties whenever it is idle.
    initial_stamper = getattr(backend, "version_stamp", None)
    initial_stamp: dict[str, Any] = initial_stamper() if initial_stamper is not None else {}
    host_capabilities = _shared_live_host_capabilities(backend, initial_stamp)
    identity = ensure_enrolled(
        base_url=base_url,
        host=host or worker_id,
        broker_url=_broker_url_from_env(),
        slots_total=int(os.environ.get("AGENT_HOST_SLOTS") or 0) or None,
        capabilities=host_capabilities,
        runner_version=_runner_version(),
    )
    if _retained_lifecycle_enabled() and identity is None:
        raise RuntimeError(
            "restartable/shared-live sandbox mode requires an enrolled host identity; "
            "configure AGENT_ENROLL_TOKEN or a persisted host credential before "
            "enabling retained chats"
        )
    pool: HostHeartbeat | None = None
    residents: ResidentPool | None = None
    if identity is not None:
        # Collected once — the image and runtime are pinned for this process's
        # lifetime — and repeated on every beat so a control plane that lost
        # the row (or predates the columns) converges on the truth (issue #54).
        # ``getattr`` for the same reason as ``check_gateway_reachable`` above:
        # a deployment-owned backend that predates the hook must keep running.
        stamper = getattr(backend, "version_stamp", None)
        stamp: dict[str, Any] = stamper() if stamper is not None else initial_stamp
        # Residency needs an enrolled identity twice over: establish/release
        # authenticate with the host credential, and the beats ride the same
        # heartbeat. A pool-less runner (the pre-enrollment shape) simply
        # never keeps a sandbox — every turn is the ladder from COLD.
        residents = ResidentPool(
            establish=lambda **kw: establish_residency(base_url=base_url, identity=identity, **kw),
            release=lambda thread_id, generation: release_residency(
                base_url=base_url,
                identity=identity,
                thread_id=thread_id,
                generation=generation,
            ),
            ttl_s=float(os.environ.get("AGENT_RESIDENCY_TTL_S") or 1800.0),
        )
        pool = HostHeartbeat(
            base_url=base_url,
            identity=identity,
            broker_url=_broker_url_from_env(),
            slots_total=int(os.environ.get("AGENT_HOST_SLOTS") or 0) or None,
            capabilities=host_capabilities,
            runner_version=_runner_version(),
            sandbox_runtime=stamp.get("sandbox_runtime"),
            sandbox_image_digest=stamp.get("sandbox_image_digest"),
            runtime_versions=stamp.get("runtime_versions"),
            runtime_versions_available=stamp.get("runtime_versions_available"),
            residency_beats=residents.beats,
            retained_sandbox_beats=(
                _retained_sandbox_heartbeat_rows if _retained_lifecycle_enabled() else None
            ),
            on_residencies_lost=lambda lost: _handle_host_residencies_lost(residents, lost),
            on_retained_sandboxes_stop=(
                _stop_retained_sandboxes if _retained_lifecycle_enabled() else None
            ),
        )
        pool.start()
        where = f" on host {identity.host} (enrolled)"

    # The durable sandbox row is pinned to the enrolled host identity. The
    # configured value is only an enrollment hint; after enrollment the
    # server-returned identity is authoritative.
    claim_host = identity.host if identity is not None else host

    _drain_on_sigterm()

    print(
        f"agent runner {worker_id} started{where} (sandbox backend: {backend.name})",
        flush=True,
    )

    while True:
        # Checked here, at the top, for the same reason the pool's answer is:
        # between jobs is the only moment leaving costs nothing. A job already
        # running is not interrupted — the signal handler only sets the flag.
        if _STOPPING.is_set():
            print("agent runner: stopping after the current job", file=sys.stderr, flush=True)
            if residents is not None:
                residents.close_all()
            if pool is not None:
                pool.stop()
            return 0

        # **Read the pool's answer before asking for work, not after.** A
        # runner that claims first and checks second spends an attempt the
        # operator has already said should not start. `draining` is a human
        # saying "finish what you have"; a removed credential is the pool
        # saying this machine is gone.
        if pool is not None and not pool.claimable:
            # Draining or gone either way: hot sandboxes are the opposite of
            # "finish what you have and stop", so quiesce them now. Idempotent
            # — after the first pass there is nothing left to close.
            if residents is not None:
                residents.close_all()
            if pool.removed.is_set():
                print(
                    "agent runner: this host's credential was rejected; not claiming further work",
                    file=sys.stderr,
                    flush=True,
                )
                pool.stop()
                return 0
            # Draining. Idle rather than exit: the operator may undrain, and a
            # process that exited would need starting again by hand.
            #
            # Housekeeping still runs. `continue` would skip the snapshot-cache
            # purge and the stale-workspace sweep at the bottom of this loop,
            # and a machine is most often drained *because* something is wrong
            # with it — leaving the one state where nothing reclaims disk to be
            # the state a machine sits in for hours.
            _housekeeping(root, workspace_ttl_s)
            time.sleep(idle_sleep_s)
            continue
        try:
            run_once(
                gateway_url=gateway_url,
                base_url=base_url,
                # **The host's own credential when it has one.** The dispatcher
                # token is shared by the whole pool, so it says "a runner" and
                # not "which"; the control plane reads the host from the
                # credential rather than from the request body, and that is
                # what makes draining, pinning and revoking mean anything
                # against a machine that has been taken over. Enrolling a host
                # and then claiming with the shared token would leave every
                # per-host control decorative.
                dispatcher_token=(pool.credential if pool is not None else dispatcher_token),
                worker_id=worker_id,
                workdir=str(root),
                lease_ttl=lease_ttl,
                agent_timeout_s=agent_timeout_s,
                generic_command=generic_command,
                backend=backend,
                workspace_root=str(root),
                host=claim_host,
                residents=residents,
            )
        except KeyboardInterrupt:
            # Stop reporting before returning. The heartbeat is a daemon
            # thread, so the process exits either way — but a machine that
            # keeps saying "still here" while its runner shuts down stays
            # `active` in the pool until the reaper's timeout, and jobs are
            # dispatched to it in the meantime.
            if residents is not None:
                residents.close_all()
            if pool is not None:
                pool.stop()
            return 0
        except ClaimUnreachable as exc:
            # Raised only by the claim call, so this really does mean no job was
            # taken. Normal at startup: compose starts the runner and the
            # gateway together, and the runner wins the race about half the
            # time. A transport failure *after* a claim falls through to the
            # handler below, which does not claim otherwise.
            print(
                f"agent runner: gateway unreachable, retrying: {exc}", file=sys.stderr, flush=True
            )
        except Exception as exc:
            # Keep serving: the store owns this job's outcome, and one bad
            # repository must not stop every other queued job.
            print(f"agent runner: job failed unexpectedly: {exc}", file=sys.stderr, flush=True)
        if residents is not None:
            # Local TTL, beside the other housekeeping: an idle resident past
            # its lease is released and torn down without waiting for a beat
            # to be refused.
            residents.sweep()
        _housekeeping(root, workspace_ttl_s)

        # `run_once` returns 0 with nothing claimed too; sleeping only when the
        # queue was empty would need a separate signal, and a short sleep after
        # any job is harmless next to a job's own runtime.
        time.sleep(idle_sleep_s)


def _env_float(name: str, fallback: float) -> float:
    """Read a float setting, falling back rather than crash-looping on a typo."""
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        print(f"ignoring {name}={raw!r}: not a number", file=sys.stderr)
        return fallback


def _chat_idle_timeout_s() -> float:
    """Read the one product-level chat inactivity clock.

    ``AGENT_SESSION_IDLE_TIMEOUT_S`` shipped with the restartable preview and
    remains a compatibility alias.  The canonical name says what this value
    actually controls: the delay before an otherwise-settled chat container
    is stopped.  Agent-turn and terminal limits are independent watchdogs and
    must never be used to decide that a healthy chat is inactive.
    """
    if os.environ.get("AGENT_CHAT_IDLE_TIMEOUT_S"):
        return _env_float("AGENT_CHAT_IDLE_TIMEOUT_S", DEFAULT_CHAT_IDLE_TIMEOUT_S)
    return _env_float("AGENT_SESSION_IDLE_TIMEOUT_S", DEFAULT_CHAT_IDLE_TIMEOUT_S)


def default_worker_id() -> str:
    """Identify this runner, distinctly from its replicas.

    ``lease_owner`` is how an operator answers "which runner has this job" and
    "which one is stuck". Replicas share an environment, so a plain
    ``AGENT_WORKER_ID`` makes every one of them report the same name — the
    question stops being answerable exactly when a second replica makes it
    worth asking. The hostname is unique per container, so it is appended
    rather than replaced: the configured value still groups a fleet.

    Not a correctness fix — fencing is on ``(attempt_id, lease_generation)``,
    never on this string.
    """
    base = (os.environ.get("AGENT_WORKER_ID") or "runner").strip() or "runner"
    host = socket.gethostname().strip()
    return f"{base}-{host}" if host and not base.endswith(host) else base


def default_runner_host() -> str | None:
    """Name the *machine* this runner sits on, for the host pool.

    Explicit configuration only. ``socket.gethostname()`` is what
    :func:`default_worker_id` uses and is exactly wrong here: inside a
    container it returns the container id, so every replica would enter the
    pool as its own "host" and every restart would add another one — an
    operator picking a machine would be choosing between rows of hex.

    Unset means this runner joins no pool and stays invisible to the switch.
    That is also the compatible default: with no host pinned, an unreporting
    runner claims exactly as it always has.
    """
    return (os.environ.get("AGENT_RUNNER_HOST") or "").strip() or None


def build_parser() -> argparse.ArgumentParser:
    """Build the runner CLI parser.

    Separate from :func:`main` so the deployment configuration can be checked
    against the flags that actually exist, rather than discovering a typo when
    a runner container crash-loops.
    """
    parser = argparse.ArgumentParser(description="Run queued agent jobs.")
    # **Two origins, and after the split they are two services.** The runner
    # claims work, reports events and renews its lease against this *control
    # plane*; the sandbox calls models against the *gateway*. They were one
    # value while the agent lived inside the gateway, and pointing that single
    # value at either service after the split breaks whichever half it is not.
    #
    # `AGENT_CONTROL_PLANE_URL` falls back to the gateway URL so a host that
    # has not been reconfigured keeps behaving exactly as it did — wrong in the
    # same way as before rather than newly broken, which is the only safe
    # default while both shapes are deployed.
    parser.add_argument(
        "--base-url",
        default=(
            os.environ.get("AGENT_CONTROL_PLANE_URL")
            or os.environ.get("AGENT_GATEWAY_URL")
            or os.environ.get("FREEINFERENCE_BASE_URL", "")
        ),
        help="Control plane: claim, heartbeat, events, artifacts, terminals.",
    )
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("AGENT_GATEWAY_URL") or os.environ.get("FREEINFERENCE_BASE_URL", ""),
        help="Inference gateway, as the sandbox reaches it. Model calls only.",
    )
    parser.add_argument("--worker-id", default=default_worker_id())
    parser.add_argument(
        "--host",
        default=default_runner_host(),
        help=(
            "Machine this runner sits on (AGENT_RUNNER_HOST). Shared by every "
            "replica here; joins the pool an admin can switch between."
        ),
    )
    parser.add_argument("--workdir", default=".")
    # Read from the environment the way --base-url and --workdir-root already
    # do. The compose overlay sets AGENT_LEASE_TTL and AGENT_TIMEOUT_S, and
    # neither reached the runner: every self-hosted job used the built-in
    # defaults regardless of what the operator configured, silently.
    parser.add_argument(
        "--lease-ttl",
        type=float,
        default=_env_float("AGENT_LEASE_TTL", DEFAULT_LEASE_TTL_S),
    )
    parser.add_argument(
        "--agent-timeout",
        type=float,
        default=_env_float("AGENT_TIMEOUT_S", DEFAULT_AGENT_TIMEOUT_S),
    )
    parser.add_argument("--generic-command", default=os.environ.get("AGENT_GENERIC_COMMAND"))
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep claiming jobs instead of exiting after one (self-hosted runner).",
    )
    parser.add_argument(
        "--workdir-root",
        default=os.environ.get("AGENT_WORKDIR_ROOT", "/var/lib/hybridinference/agent-jobs"),
        help="Where per-job worktrees are retained in --loop mode.",
    )
    parser.add_argument(
        "--workspace-ttl",
        type=float,
        default=_env_float("AGENT_WORKSPACE_TTL_S", DEFAULT_WORKSPACE_TTL_S),
        help="Seconds to retain an idle job workspace (0 disables cleanup).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by the runner workflow and the self-hosted service."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # In the source repo, importing sandbox configured root logging as a side
    # effect; the host shim removed that on purpose. This is the sanctioned
    # replacement (MIGRATION.md deferred-behaviour register, D6 row) — without
    # it every INFO event a runner emits goes nowhere.
    configure_process_logging()

    dispatcher_token = os.environ.get("AGENT_DISPATCHER_TOKEN", "")
    if not args.base_url or not dispatcher_token:
        parser.error(
            "AGENT_CONTROL_PLANE_URL (or AGENT_GATEWAY_URL) and AGENT_DISPATCHER_TOKEN are required"
        )

    if args.loop:
        return run_forever(
            base_url=args.base_url,
            gateway_url=args.gateway_url or args.base_url,
            dispatcher_token=dispatcher_token,
            worker_id=args.worker_id,
            workdir_root=args.workdir_root,
            lease_ttl=args.lease_ttl,
            agent_timeout_s=args.agent_timeout,
            generic_command=args.generic_command,
            workspace_ttl_s=args.workspace_ttl,
            host=args.host,
        )

    return run_once(
        base_url=args.base_url,
        gateway_url=args.gateway_url or args.base_url,
        dispatcher_token=dispatcher_token,
        worker_id=args.worker_id,
        workdir=args.workdir,
        lease_ttl=args.lease_ttl,
        agent_timeout_s=args.agent_timeout,
        generic_command=args.generic_command,
        host=args.host,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ClaimedJob",
    "ControlPlane",
    "LeaseLost",
    "WorktreeError",
    "build_patch",
    "checkout_output_branch",
    "existing_checkout_sha",
    "main",
    "prepare_worktree",
    "run_once",
    "save_workspace_snapshot",
]
