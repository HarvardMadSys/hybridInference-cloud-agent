"""The trusted host runner that schedules turns into retained chat sandboxes.

It runs outside the chat container, claims one fenced attempt at a time, and
streams the member Agent CLI's output through the workspace broker. Its shape
follows from the adjudicated security model rather than convenience:

- **Narrow credentials.** The enrolled host claims with its own identity. Only
  a member-scoped inference grant and an attempt-fenced MCP token enter the
  sandbox; worker-control and repository credentials stay in this process.
- **Snapshot-out, not push.** The runner never pushes. The trusted publisher
  captures and validates a quiescent shared-workspace snapshot, then pushes it.
  That is why neither the runner nor the sandbox needs a git write credential.
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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import httpx

from cloud_agent_host.chat_sandbox import (
    CHAT_WORKDIR,
    ChatSandboxManager,
    ChatSandboxSpec,
    RetainedChatBackend,
)
from cloud_agent_host.drivers import (
    LINE,
    DriverError,
    driver_for,
)
from cloud_agent_host.enrollment import HostHeartbeat, ensure_enrolled
from cloud_agent_host.host_capacity import HostCapacityCollector, capacity_enabled
from cloud_agent_host.host_runtime import (
    HANDOVER_ACCEPTED,
    HANDOVER_DECLINED,
    HANDOVER_TIMEOUT_S,
    HOST_CONCURRENCY_CAPABILITY,
    LAUNCH_NOT_STARTED,
    LAUNCH_STARTED,
    LAUNCH_UNCERTAIN,
    LaunchResult,
    TurnPool,
    decode_turn,
    default_slots,
    encode_turn,
    read_handover,
    requested_slots,
    write_handover,
)
from cloud_agent_host.logging_utils import configure_process_logging, get_logger
from cloud_agent_host.patch_gate import branch_name_for
from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_host.resources import shared_member_resource_rollup
from cloud_agent_host.runtimes import (
    AgentRuntime,
    NormalizedEvent,
    get_runtime,
    registered_runtimes,
)
from cloud_agent_host.sandbox import (
    ContainerBackend,
    SandboxBackend,
    SandboxError,
    SandboxSpec,
    build_backend_from_env,
)
from cloud_agent_host.session_lifecycle import DEFAULT_CHAT_IDLE_TIMEOUT_S
from cloud_agent_host.session_supervisor_client import (
    SessionSupervisorClient,
    SessionSupervisorError,
)
from cloud_agent_host.setup import run_setup
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_PROTOCOL,
    SHARED_SANDBOX_RUNTIME_PINS,
    shared_deployment_capability,
)
from cloud_agent_host.source_archive import build_base_bundle, build_source_archive

DEFAULT_LEASE_TTL_S = 120.0
HEARTBEAT_INTERVAL_S = 30.0
# Heartbeats use a short per-request timeout and retry promptly, so a slow
# control request cannot consume the remaining lease in normal cadence sleeps.
# Idempotent lifecycle receipts reuse the same short retry interval.
_CONTROL_RETRY_INTERVAL_S = 5.0
_HEARTBEAT_TIMEOUT_S = 10.0
DEFAULT_AGENT_TIMEOUT_S = 3600.0
# How often the control checks run while the agent is silent.
_POLL_INTERVAL_S = 0.5
# The broker closes guest telemetry and enforces the member activation
# boundary after an interrupt. Keep draining its terminal frame so a cancel or
# timeout cannot strand the member lock or discard the already-closed resource
# report. The broker client itself has the same 180 second request ceiling.
_SHARED_TURN_INTERRUPT_DRAIN_S = 180.0
_GIT_TIMEOUT_S = 300.0

logger = get_logger(__name__)


def _log_control_failure(operation: str, job_id: str, exc: Exception) -> None:
    """Keep a diagnosable failure without logging tokens or arbitrary response bodies."""
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    logger.warning(
        "agent_control_failure operation=%s job_id=%s error_type=%s status_code=%s",
        operation,
        job_id,
        type(exc).__name__,
        status,
    )


def _retry_lifecycle_receipt(operation: str, job_id: str, call: Callable[[], Any]) -> Any:
    """Retry only idempotent lifecycle receipts, never prompt delivery."""
    for attempt in range(3):
        try:
            return call()
        except (httpx.TransportError, httpx.HTTPStatusError, SessionSupervisorError) as exc:
            status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else exc.status_code
                if isinstance(exc, SessionSupervisorError)
                else None
            )
            if attempt == 2 or (status is not None and status not in {408, 502, 503, 504}):
                raise
            _log_control_failure(operation, job_id, exc)
            time.sleep(_CONTROL_RETRY_INTERVAL_S)
    raise AssertionError("lifecycle retry exhausted without a result")


# Both are interpolated into a URL and handed to git, which reads a leading
# `-` as an option even where an operand belongs. Validate the shape here as
# well as at the API edge: this trusted process handles clone credentials.
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
    """What the control plane handed the enrolled host."""

    job_id: str
    thread_id: str
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
    parent_job_id: str | None = None
    turn_no: int = 1
    # MCP connection aliases this job was granted. Aliases only: the runner
    # learns nothing about where they actually are or how they authenticate,
    # because the sandbox reaches them through this service's MCP-only relay
    # rather than directly.
    mcp_bindings: list[str] = field(default_factory=list)
    # The CLI version the control plane stamped on this attempt, resolved from
    # the thread's pin, the deployment's selection, or this host's default.
    # ``None`` from a control plane that predates selection — the image's
    # symlinked default then applies, which is what that control plane means.
    runtime_version: str | None = None
    # Durable shared-container membership. It is absent only until the first
    # claimed turn reserves its sandbox; every executable claim uses this
    # topology and remains pinned to the owning host afterwards.
    thread_sandbox: dict[str, Any] | None = None

    @classmethod
    def from_response(cls, body: dict[str, Any]) -> ClaimedJob:
        """Build from the claim endpoint's response."""
        return cls(
            job_id=body["job_id"],
            thread_id=body["thread_id"],
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
            sandbox_token=body["sandbox_token"],
            mcp_token=body.get("mcp_token") or "",
            mcp_base_url=body.get("mcp_base_url") or "",
            clone_token=body.get("clone_token") or None,
            mcp_bindings=list(body.get("mcp_bindings") or []),
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

    def _post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        response = self._client.post(
            f"{self._base}{path}",
            json=payload or {},
            timeout=self._client.timeout if timeout is None else timeout,
        )
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
            f"/v1/agent/worker/jobs/{self.job_id}/heartbeat",
            {"lease_ttl_seconds": ttl},
            timeout=_HEARTBEAT_TIMEOUT_S,
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
        """Report one closed attempt's resource and timing telemetry. Best-effort.

        Swallowed like every other observability write: a turn that ran must
        not fail because the platform could not record its telemetry.
        """
        try:
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/resources", rollup)
        except LeaseLost:
            raise
        except Exception:
            pass

    def report_turn_resources(self, payload: dict[str, Any]) -> None:
        """Report one cumulative whole-turn member execution snapshot.

        The same method serves a running ``collecting`` checkpoint and the
        closing ``final`` snapshot: the body carries its own state and
        ``report_seq``, and the endpoint treats a resend at the same sequence
        as the same report. Swallowed like every other observability write --
        a turn that ran must not fail because the platform could not record
        its telemetry -- with the loss of the lease still fatal.

        There is deliberately no retry here. The payload is a cumulative
        snapshot with a monotonic ``report_seq``, so a checkpoint lost to a
        transport error is superseded by the next one: retrying would upload
        a stale picture while the current one is already being assembled.
        """
        try:
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/turn-resources", payload)
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
            timeout=10.0,
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
            timeout=10.0,
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
        """Freeze owner PTYs before workspace preparation or group snapshot capture."""
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
        delay = self._interval
        last_success = time.monotonic()
        failures = 0
        while not self._stop.wait(delay):
            try:
                reply = self._control.heartbeat(self._ttl)
            except LeaseLost as exc:
                # The fence was lost; this does not imply a replacement exists.
                _log_control_failure("heartbeat_fenced", self._control.job_id, exc)
                self.lease_lost = True
                return
            except Exception as exc:
                # A transient control-plane blip must not kill a healthy run;
                # the next request still has to fit inside the original lease.
                failures += 1
                delay = min(self._interval, _CONTROL_RETRY_INTERVAL_S)
                # The aggregate retry line below already carries the count and
                # timing, so log the exception detail once per outage instead of
                # once per retry.
                if failures == 1:
                    _log_control_failure("heartbeat", self._control.job_id, exc)
                logger.warning(
                    "agent_heartbeat_retry job_id=%s consecutive_failures=%s "
                    "seconds_since_ack=%.3f retry_in_seconds=%.3f",
                    self._control.job_id,
                    failures,
                    time.monotonic() - last_success,
                    delay,
                )
                continue
            if failures:
                logger.info(
                    "agent_heartbeat_recovered job_id=%s failed_requests=%s seconds_since_ack=%.3f",
                    self._control.job_id,
                    failures,
                    time.monotonic() - last_success,
                )
            failures = 0
            last_success = time.monotonic()
            delay = self._interval
            if reply.get("cancel_requested"):
                self.cancel_requested = True
                return


def claim(
    *,
    base_url: str,
    host_credential: str,
    worker_id: str,
    lease_ttl: float,
    attestation: dict[str, Any] | None = None,
) -> ClaimedJob | None:
    """Claim the next queued job with this enrolled host's credential.

    The credential names the machine, so the body cannot claim as another
    host. It remains in the trusted runner and never enters the agent process.
    """
    return claim_payload(
        base_url=base_url,
        host_credential=host_credential,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
        attestation=attestation,
    )


def claim_payload(
    *,
    base_url: str,
    host_credential: str,
    worker_id: str,
    lease_ttl: float,
    attestation: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Claim the next queued job and return the control plane's raw answer.

    The host service hands this document to the child that will run the turn,
    unread beyond the few fields it needs to account for the claim. Parsing it
    into a :class:`ClaimedJob` is the child's job, in the process that will
    actually use the credentials it carries.
    """
    payload: dict[str, Any] = {"worker_id": worker_id, "lease_ttl_seconds": lease_ttl}
    if attestation:
        payload["attestation"] = attestation
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/v1/agent/worker/claim",
            json=payload,
            headers={"Authorization": f"Bearer {host_credential}"},
            timeout=30.0,
        )
    except httpx.TransportError as exc:
        # Tagged at the one place where "the gateway is unreachable" also means
        # "no job was claimed". The same error from a later call — an event, an
        # group snapshot, a terminal transition — happens with an attempt already
        # running, and reporting *that* as an unreached gateway would hide the
        # job an operator has to go look at.
        raise ClaimUnreachable(str(exc)) from exc
    response.raise_for_status()
    body = response.json()
    return body if body else None


def release_turn(*, base_url: str, worker_token: str, job_id: str, reason: str) -> bool:
    """Hand a claimed turn back to the queue, having proved it never began.

    Called only where this host can show the turn was not started — the child
    could not be spawned, or exited without taking delivery. The control plane
    checks the same thing from its own side and refuses otherwise, so a host
    that is wrong about it cannot requeue work that ran.

    Returns whether the turn went back. ``False`` is not an error path worth
    retrying: the lease is the backstop it always was, and a turn this call
    could not release is one the control plane has already decided about.
    """
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/v1/agent/worker/jobs/{job_id}/release",
            json={"reason": reason},
            headers={"Authorization": f"Bearer {worker_token}"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        _log_control_failure("release", job_id, exc)
        return False
    if response.status_code >= 400:
        logger.warning(
            "agent_turn_release_refused",
            extra={
                "event": "agent_turn_release_refused",
                "job_id": job_id,
                "status": response.status_code,
            },
        )
        return False
    return True


def _run_git(
    args: list[str],
    *,
    cwd: str | None = None,
    env_extra: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
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


def _auth_env(clone_token: str | None) -> dict[str, str]:
    """Carry the clone credential in git's environment, never in argv.

    ``GIT_CONFIG_COUNT`` sets config for this invocation only. That matters
    twice over: the token never lands in ``.git/config`` (which is about to be
    archived into the chat container), and it never appears in process
    arguments (where another user on the runner host could see it in ``ps``
    during the fetch).
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

    No repository credential may reach the sandbox, and this tree is about to
    be streamed into it. Asserting the property beats assuming it: git writes
    several files during a fetch, and a future authentication change could
    quietly start recording the token in one of them.
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
    safe because later Git inspection runs inside the retained container.

    A shallow single-commit fetch, with no remote recorded. Not adding a remote
    is the point — it is what keeps the credential out of ``.git/config``, and
    the runner has no reason to offer the agent a push path it must not use.
    """
    parts = repo.split("/") if repo else []
    if (
        not _REPO_SLUG.fullmatch(repo or "")
        or len(parts) != 2
        or any(part in {".", ".."} for part in parts)
    ):
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


def checkout_output_branch(workdir: str, branch_id: str) -> str:
    """Put a fresh durable worktree on the publisher's local output branch."""
    branch = branch_name_for(branch_id)
    switched = _run_git(["checkout", "--quiet", "-B", branch, "HEAD"], cwd=workdir)
    if switched.returncode != 0:
        raise WorktreeError(
            f"could not create workspace branch {branch}: {switched.stderr.strip()[:200]}"
        )
    return branch


def _read_bounded(process: Any, *, deadline_s: float, max_bytes: int) -> str:
    """Drain a sandbox process's stdout under a deadline and a byte cap.

    This step still runs git against a worktree the agent owned for the whole
    run, including its ``.git/config``. A ``diff.external`` (or a textconv
    filter, or ``core.fsmonitor``) that never returns would otherwise block
    here forever — and because the heartbeat thread keeps renewing the lease
    until the enclosing ``finally``, the attempt never expires, the reaper
    never takes it, and this runner claims nothing further. One hostile
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


def _resident_base_ready(workdir: str, job: ClaimedJob, backend: SandboxBackend) -> bool:
    """Is the turn's pinned base resolvable in the retained workspace?

    Probed **inside the sandbox**, exactly like ``build_patch`` and for the
    same reason: this tree — ``.git/config`` included — has belonged to the
    agent since import, and trusted-runner git against it would execute
    whatever the agent left in ``diff.external`` or ``core.fsmonitor`` with
    the runner's privileges. A missing object is imported as a credential-free
    Git bundle built in a temporary trusted clone; clone credentials never
    enter the retained container or its agent-controlled filesystem.
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


def _shared_live_ready_file() -> str:
    """The deployment-readiness marker this runner was started with.

    Written by ``sandbox_host.sh`` — ``pending:<sha>`` while a rollout is still
    checking the host, the bare ``<sha>`` once it has passed — and mounted
    read-only into every replica. Read on every beat for the exact-SHA
    capability, and watched between beats so the control plane's rollout fence
    hears about that SHA as soon as it is published rather than at the next
    scheduled heartbeat.
    """
    return (os.environ.get("AGENT_SHARED_LIVE_READY_FILE") or "").strip()


def _shared_live_host_capabilities(
    backend: SandboxBackend,
    stamp: dict[str, Any],
) -> list[str]:
    """Advertise shared-v1 only when this exact host can honor its contract."""
    if not isinstance(backend, ContainerBackend):
        return []
    # One child process runs one claimed turn, and the host claims only while
    # it is under its limit, so slots can never outnumber the processes that
    # honour them. What can still be wrong is the floor: shared native forks
    # need a sibling running beside its parent, and a limit of one would
    # serialize them. The default is validated here because the deployment
    # check attests through this function before any turn has been claimed.
    try:
        default_slots()
    except ValueError:
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
    for runtime_id in registered_runtimes():
        try:
            capabilities = driver_for(get_runtime(runtime_id)).capabilities()
        except KeyError:
            return []
        if (
            capabilities.resident_transport != "supervisor_live"
            or capabilities.fork != "at_anchor"
            or not capabilities.concurrent_shared_workspace
        ):
            return []
    capabilities = [SHARED_SANDBOX_CAPABILITY, NATIVE_FORK_AT_ANCHOR_CAPABILITY]
    deployment_sha = (os.environ.get("AGENT_DEPLOYMENT_SHA") or "").strip()
    ready_file = _shared_live_ready_file()
    if deployment_sha and ready_file:
        try:
            ready_sha = pathlib.Path(ready_file).read_text().strip()
        except OSError:
            ready_sha = ""
        if secrets.compare_digest(ready_sha, deployment_sha):
            capabilities.append(shared_deployment_capability(deployment_sha))
    return capabilities


def _shared_live_deployment_ready(
    backend: SandboxBackend,
    stamp: dict[str, Any],
) -> bool:
    """Whether this replica may poll, including against the previous API.

    The target API enforces the exact-SHA capability at claim time, but the
    previous API in a parallel rollout cannot know that new field yet. A
    pending runner must therefore refrain from polling locally until the host
    checks publish its exact marker. It still enrolls and heartbeats with the
    base protocol capabilities so those checks can inspect the replacement.
    """
    deployment_sha = (os.environ.get("AGENT_DEPLOYMENT_SHA") or "").strip()
    if not deployment_sha:
        return False
    return shared_deployment_capability(deployment_sha) in _shared_live_host_capabilities(
        backend, stamp
    )


def _claim_attestation(backend: SandboxBackend) -> dict[str, Any]:
    """Build the exact-replica proof required for every claim."""
    stamper = getattr(backend, "version_stamp", None)
    stamp: dict[str, Any] = stamper() if stamper is not None else {}
    # ``version_stamp`` proves the immutable image. This separately proves the
    # polling process has the broker, slots and driver surfaces
    # needed to use it; a sibling replica's host heartbeat cannot stand in for
    # those process-local facts during a rolling deployment.
    stamp["runner_capabilities"] = _shared_live_host_capabilities(backend, stamp)
    return stamp


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
        for row in rows[:4096]:
            if row.get("shared_live") is not True:
                raise ValueError("workspace broker returned a non-shared sandbox")
            members = [dict(item) for item in row.get("members") or [] if isinstance(item, dict)]
            activation_generation = int(row["activation_generation"])
            running = bool(row["running"])
            active = bool(row.get("group_busy")) or int(row.get("terminal_count") or 0) > 0
            active = active or int(row.get("operation_count") or 0) > 0
            active = active or bool(row.get("workspace_suspended"))
            deadline = row.get("idle_deadline_monotonic")
            # A generation-zero RUNNING entry with no local deadline was just
            # reconstructed after a broker restart. Its empty in-memory token
            # set is not proof that the group has been idle: the broker may
            # have restarted while a terminal had cleared the durable
            # deadline. Report activity as unknown. The control plane installs
            # one recovery deadline only when its durable deadline is also
            # NULL; unlike reporting a fresh full timeout on every heartbeat,
            # that window cannot be extended forever by healthy heartbeats.
            activity_known = activation_generation > 0 or active or deadline is not None
            idle_remaining = None
            if activity_known and deadline is not None:
                idle_remaining = max(0.0, float(deadline) - now)
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
                    "shared_live": True,
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
                client.stop_shared_idle(
                    str(command["thread_id"]),
                    generation=int(command["generation"]),
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


def _handle_host_residencies_lost(lost: list[dict[str, Any]]) -> None:
    """Fence supervisor-owned shared members whose renewable grant was lost."""
    client: SessionSupervisorClient | None = None
    try:
        client = _session_supervisor_from_env()
        shared_members = {
            str(member.get("thread_id"))
            for row in client.list()
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
        checkout_output_branch(source, job.thread_id)
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


#: The top-level keys a turn summary must carry before this host will post it.
#: The control plane owns the authoritative schema; this is only the check
#: that says "a report, not a fragment", so a producer bug cannot post a body
#: that the endpoint would reject as malformed. A rejected report is worse
#: than an absent one: it reads as a platform error instead of a measurement
#: that was never taken.
_TURN_RESOURCES_KEYS = (
    "measurement_id",
    "report_seq",
    "state",
    "window",
    "cpu",
    "memory",
    "storage_io",
)


def _report_checkpoint_quietly(control: Any, payload: dict[str, Any]) -> None:
    """Report one running checkpoint, never letting it disturb the stream.

    A checkpoint exists so a crashed turn keeps the work it already did. That
    makes it strictly optional at the time it is taken: if the report cannot
    be delivered, the next checkpoint and the final snapshot carry the same
    cumulative state, so a failure here must not surface as a turn error.
    """
    with contextlib.suppress(Exception):
        control.report_turn_resources(payload)


def _reportable_turn_resources(payload: Any) -> dict[str, Any] | None:
    """Return the snapshot to post, or None when there is nothing honest to send.

    ``None`` is a real answer, not a failure: a turn whose broker returned no
    summary was not measured, and a synthesized empty snapshot would store
    "this turn used nothing" -- a claim nobody made. The attempt's legacy
    resource row already says "no member totals were attributed", so silence
    here leaves the two consistent.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != 1:
        return None
    if any(key not in payload for key in _TURN_RESOURCES_KEYS):
        return None
    return payload


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
        job.thread_id,
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
                    supervisor.interrupt_shared(job.thread_id, token=lease_token)
                raise LeaseLost("lease lost while the shared Agent CLI was running")
            now = time.monotonic()
            if heart.cancel_requested and forced_exit_code is None:
                with contextlib.suppress(Exception):
                    supervisor.interrupt_shared(job.thread_id, token=lease_token)
                control.append_event(NormalizedEvent("lifecycle", {"phase": "cancelled_by_owner"}))
                forced_exit_code = 130
                interrupt_deadline = now + _SHARED_TURN_INTERRUPT_DRAIN_S
            if forced_exit_code is None and now > deadline:
                with contextlib.suppress(Exception):
                    supervisor.interrupt_shared(job.thread_id, token=lease_token)
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
            if kind == "resource_checkpoint":
                # A running checkpoint, not a terminal frame. It is reported
                # as `collecting` so a crash keeps the work measured up to
                # here; the closing snapshot carries the same counters and the
                # higher sequence, which is what supersedes this one.
                checkpoint = _reportable_turn_resources(frame.get("turn_resources"))
                if checkpoint is not None:
                    _report_checkpoint_quietly(control, checkpoint)
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
                # The result may already have crossed the wire before the
                # transport failed. Preserve that terminal frame rather than
                # converting a completed activation into an opaque 502.
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
        finish_attempt("failed", "the control plane returned a non-shared sandbox mapping")
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
    stage = "supervisor_acquire"
    context_status = "restored" if has_context else "new"
    idle_timeout_s = _chat_idle_timeout_s()

    def abandon_before_finish(*, lost: bool, detail: str | None) -> None:
        nonlocal abandoned
        if abandoned or settled:
            return
        if supervisor is not None and lease_token is not None:
            try:
                supervisor.abandon_shared(job.thread_id, token=lease_token, lost=lost)
            except Exception as exc:
                _log_control_failure("supervisor_abandon", job.job_id, exc)
        try:
            control.abandon_chat_sandbox(
                sandbox_generation=spec.generation,
                activation_generation=activation_generation,
                lost=lost,
                detail=detail,
                idle_timeout_seconds=idle_timeout_s,
            )
        except Exception as exc:
            _log_control_failure("sandbox_abandon", job.job_id, exc)
        abandoned = True

    def report_failure() -> None:
        # Telemetry availability must never gate the authoritative transition.
        # Keep trying finish even when the error-event endpoint is unavailable.
        detail = (failure_detail or "shared live execution failed")[:1000]
        try:
            control.append_event(NormalizedEvent("error", {"text": detail}))
        except Exception as exc:
            _log_control_failure("error_event", job.job_id, exc)
        try:
            finish_attempt("failed", detail)
        except Exception as exc:
            _log_control_failure("finish", job.job_id, exc)

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
        stage = "sandbox_running"
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
        stage = "terminal_suspend"
        control.suspend_terminals("workspace_preparing")
        terminals_suspended = True

        if not has_context:
            stage = "workspace_reset"
            manager.reset_unsettled_state(spec)
            stage = "checkout"
            base_sha = _prepare_retained_checkout(job=job, manager=manager, spec=spec)
            reused_workspace = False
            if job.setup_script:
                stage = "setup"
                setup = run_setup(
                    script=job.setup_script,
                    workdir=CHAT_WORKDIR,
                    backend=retained,
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
            stage = "workspace_permissions"
            manager.prepare_shared_workspace(spec)
        else:
            stage = "native_workspace_resume"
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

        stage = "runtime_preflight"
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

        stage = "agent_network"
        if has_context:
            manager.require_network_phase(spec, "agent")
        else:
            manager.transition_to_agent_network(spec)
        stage = "terminal_resume"
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
            mcp_server_ids=tuple(job.mcp_bindings),
            # The MCP relay token is attempt-fenced. It is process-fixed only
            # when this turn actually exposes MCP endpoints; carrying an
            # otherwise-unused fresh token here would restart every ordinary
            # no-MCP chat and falsely erase HOT continuity.
            mcp_base_url=job.mcp_base_url if job.mcp_bindings else "",
            mcp_credential=job.mcp_token if job.mcp_bindings else "",
            slot_id=slot_id,
            residency_generation=int(running.get("residency_generation") or 0),
        )
        stage = "resident_start"
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

        stage = "prompt_admission"
        _retry_lifecycle_receipt(
            "prompt_admission_retry",
            job.job_id,
            lambda: control.admit_chat_turn(
                sandbox_generation=spec.generation,
                activation_generation=activation_generation,
            ),
        )
        turn_admitted = True
        result_frame: dict[str, Any] | None = None
        try:
            stage = "turn_stream"
            exit_code, tail, tool_errors, result_frame = _stream_shared_agent_turn(
                supervisor=supervisor,
                job=job,
                control=control,
                heart=heart,
                lease_token=lease_token,
                timeout_s=agent_timeout_s,
            )
        finally:
            # Container totals include concurrent siblings and cannot be
            # attributed to this member. The broker instead returns UID-scoped
            # guest process telemetry and per-turn tool windows; if the stream
            # died before a terminal frame, retain an explicit zero-coverage
            # row rather than inventing a share of the container total.
            with contextlib.suppress(Exception):
                fallback = shared_member_resource_rollup()
                observed = result_frame.get("resources") if isinstance(result_frame, dict) else None
                control.report_resources(observed if isinstance(observed, dict) else fallback)
            # The whole-turn member window travels with the same terminal
            # frame. It is posted here, before the terminal transition below,
            # because the report is fenced on this attempt's live lease -- the
            # same reason the legacy rollup above is posted here. A frame
            # without the key is a turn nobody measured, and is left absent
            # rather than filled with zeros.
            with contextlib.suppress(Exception):
                # The broker ships the legacy rollup and the turn summary
                # inside one ``resources`` object; reading the frame's top
                # level found nothing and silently reported no turn at all.
                turn_resources = (
                    (result_frame.get("resources") or {}).get("turn_resources")
                    if isinstance(result_frame, dict)
                    and isinstance(result_frame.get("resources"), dict)
                    else None
                )
                reportable = _reportable_turn_resources(turn_resources)
                if reportable is not None:
                    control.report_turn_resources(reportable)
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
        # A terminal native API error can leave its history and process
        # intact. Only a broker result emitted after activation cleanup proves
        # this boundary; process death, timeout and lost streams remain LOST.
        settled_failure = (
            exit_code != 0
            and isinstance(result_frame, dict)
            and result_frame.get("kind") == "result"
            and result_frame.get("status") == "failed"
            and result_frame.get("native_turn_settled") is True
        )
        if exit_code != 0 and not settled_failure:
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

        stage = "settlement"
        local_settle = _retry_lifecycle_receipt(
            "supervisor_settlement_retry",
            job.job_id,
            lambda: supervisor.settle_shared(
                job.thread_id,
                token=lease_token,
                job_id=job.job_id,
                context_status=context_status,
            ),
        )
        settled_session_id = _verified_native_session_id(
            previous_session_id=native_session_id,
            observed_session_id=local_settle.get("native_session_id"),
            has_context=has_context,
        )
        _retry_lifecycle_receipt(
            "control_settlement_retry",
            job.job_id,
            lambda: control.settle_chat_sandbox(
                sandbox_generation=spec.generation,
                activation_generation=activation_generation,
                context_status=context_status,
                native_session_id=settled_session_id,
                base_sha=base_sha,
                idle_timeout_seconds=idle_timeout_s,
            ),
        )
        settled = True
        if settled_failure:
            # Record the native turn ordinal before failing the job, so later
            # retries and historical forks keep matching the CLI transcript.
            finish_attempt(
                "failed", f"agent failed at native turn boundary: {tail[-500:]}", base_sha=base_sha
            )
            return 1
        try:
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
        except LeaseLost:
            raise
        except Exception as exc:
            # Both authoritative settlements succeeded. A missing UI notice
            # must not turn that success into a failed terminal transition.
            _log_control_failure("completion_event", job.job_id, exc)
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
        _log_control_failure(stage, job.job_id, exc)
        abandon_before_finish(lost=turn_admitted or context_invalid, detail=failure_detail)
        report_failure()
        return 2
    except Exception as exc:
        failure_detail = f"shared live chat failed unexpectedly: {exc}"
        _log_control_failure(stage, job.job_id, exc)
        abandon_before_finish(lost=turn_admitted, detail=failure_detail)
        report_failure()
        return 2
    finally:
        if not settled and not abandoned:
            abandon_before_finish(lost=turn_admitted or context_invalid, detail=failure_detail)
        if supervisor is not None:
            supervisor.close()
        if terminals_suspended:
            with contextlib.suppress(Exception):
                control.resume_terminals()


def run_once(
    *,
    base_url: str,
    gateway_url: str = "",
    host_credential: str,
    worker_id: str,
    lease_ttl: float = DEFAULT_LEASE_TTL_S,
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    backend: SandboxBackend | None = None,
    host: str | None = None,
) -> int:
    """Claim and deliver one turn through the shared retained-session protocol.

    Claim and execution in one process. The deployed host service separates
    them — it claims, and a child process of its own runs the turn — but a
    single-turn path is what local tooling and the tests that exercise the
    protocol end to end use, and it is the same execution either way.

    A runner-created backend is preflighted before claim. The retained
    container owns the only workspace, so there is deliberately no host
    worktree argument or bind-mount fallback.
    """
    if backend is None:
        backend = build_backend_from_env()
        backend.preflight()

    job = claim(
        base_url=base_url,
        host_credential=host_credential,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
        attestation=_claim_attestation(backend),
    )
    if job is None:
        print("no queued agent job; nothing to do")
        return 0

    return run_turn(
        job,
        base_url=base_url,
        gateway_url=gateway_url,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
        agent_timeout_s=agent_timeout_s,
        backend=backend,
        host=host,
    )


def run_turn(
    job: ClaimedJob,
    *,
    base_url: str,
    gateway_url: str = "",
    worker_id: str,
    lease_ttl: float = DEFAULT_LEASE_TTL_S,
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    backend: SandboxBackend | None = None,
    host: str | None = None,
) -> int:
    """Deliver one already-claimed turn, and report its outcome.

    Everything after the claim: the lease renewal, the retained session, the
    agent, and the terminal transition. This is what the host service starts a
    child process for, and the boundary is the claim precisely because it is
    the last point at which a turn can be handed back to the queue unspent.
    """
    if backend is None:
        backend = build_backend_from_env()
        backend.preflight()

    print(f"claimed {job.job_id} attempt {job.attempt_no} runtime={job.runtime}")
    logger.info(
        "agent_attempt_claimed job_id=%s attempt_id=%s worker_id=%s host=%s",
        job.job_id,
        job.attempt_id,
        worker_id,
        host,
    )
    control = ControlPlane(base_url, job.worker_token)
    control.job_id = job.job_id
    heart = Heartbeater(control, ttl=lease_ttl, interval=HEARTBEAT_INTERVAL_S)

    def finish_attempt(
        state: str,
        detail: str | None = None,
        *,
        base_sha: str | None = None,
    ) -> None:
        if base_sha is None:
            control.finish(state, detail)
        else:
            control.finish(state, detail, base_sha=base_sha)

    try:
        runtime = get_runtime(job.runtime)
    except KeyError as exc:
        finish_attempt("failed", str(exc))
        control.close()
        return 2

    heart.start()
    try:
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
    except LeaseLost as exc:
        _log_control_failure("attempt_dispatch_fenced", job.job_id, exc)
        return 3
    except Exception as exc:
        # Preparation before the shared handler (image inspection, manifest
        # validation, etc.) can fail after claim. Report a terminal outcome
        # before finally stops renewal, instead of leaving a running attempt
        # that will later be misreported as a lease failure.
        _log_control_failure("attempt_dispatch", job.job_id, exc)
        try:
            finish_attempt("failed", f"attempt dispatch failed ({type(exc).__name__})")
        except Exception as finish_exc:
            _log_control_failure("finish", job.job_id, finish_exc)
        return 2
    finally:
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
    and terminal request on its jobs. That is right only when both readers
    share a network, as in the integrated local Compose stack. Merely running
    separate Compose projects on one physical machine does not make their DNS
    or networks shared.

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


def _start_host_capacity_collector(backend: SandboxBackend) -> HostCapacityCollector | None:
    """Start sampling this host's load, or return ``None`` when disabled.

    Sampling is best effort by construction: it observes a machine, it never
    gates a claim, and a deployment that has not mounted the host's proc files
    gets "not measured" rather than a fabricated reading. That is why a failure
    here is a log line and not a startup error — a runner that cannot report
    CPU must still be able to run jobs.

    One collector per process, on its own daemon thread. Three replicas on one
    machine therefore sample independently and each keeps its own CPU baseline;
    the control plane keeps one latest report per host and never adds replicas
    together, because they are three views of one machine, not three machines.
    """
    if not capacity_enabled():
        logger.info(
            "agent_host_capacity_disabled",
            extra={"event": "agent_host_capacity_disabled"},
        )
        return None
    collector = HostCapacityCollector(
        # The same namespace the sandbox manager stamps on every container it
        # creates, read off the same backend object, so a host can never count
        # a sibling deployment's containers as its own.
        namespace=getattr(backend, "sandbox_namespace", "") or "",
        docker_binary=getattr(backend, "docker_binary", "docker"),
    )
    collector.start()
    return collector


def _spawn_turn(
    payload: dict[str, Any],
    *,
    base_url: str,
    gateway_url: str,
    worker_id: str,
    lease_ttl: float,
    agent_timeout_s: float,
    host: str,
) -> LaunchResult:
    """Start a child for one claimed turn and find out whether it took it.

    The turn travels on the child's standard input, because it carries this
    attempt's credentials and both ``/proc/<pid>/cmdline`` and
    ``/proc/<pid>/environ`` are readable by anyone who can see the process. The
    child's answer comes back on a descriptor of its own, because its standard
    output is the container log and a reader that stopped draining that would
    block the turn it was waiting for.

    Every path that returns ``LAUNCH_NOT_STARTED`` has shown the child never
    had the turn: the spawn itself failed, delivery failed, or the child closed
    its answer without giving one, which is what exiting does for it. A child
    that answered owns the turn, and a child that answered nothing and is still
    alive is not evidence of anything — the turn stays claimed and its lease
    remains the backstop.
    """
    read_fd, write_fd = os.pipe()
    argv = [
        sys.executable,
        "-m",
        "cloud_agent_host.runner",
        "--turn",
        "--handover-fd",
        str(write_fd),
        "--base-url",
        base_url,
        "--gateway-url",
        gateway_url,
        "--worker-id",
        worker_id,
        "--host",
        host,
        "--lease-ttl",
        str(lease_ttl),
        "--agent-timeout",
        str(agent_timeout_s),
    ]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            pass_fds=(write_fd,),
        )
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        return LaunchResult(
            outcome=LAUNCH_NOT_STARTED, detail=f"could not start a turn process: {exc}"
        )
    # This process's copy, closed so the read below ends at end-of-file when
    # the child exits rather than waiting out the handover deadline.
    os.close(write_fd)
    try:
        assert process.stdin is not None
        # Underscore-prefixed keys are this host's own bookkeeping; what the
        # child parses is the control plane's answer and nothing else.
        process.stdin.write(
            encode_turn({key: value for key, value in payload.items() if not key.startswith("_")})
        )
        process.stdin.close()
    except OSError as exc:
        os.close(read_fd)
        _reap_unstarted(process)
        return LaunchResult(
            outcome=LAUNCH_NOT_STARTED, detail=f"the turn could not be delivered: {exc}"
        )

    answer = read_handover(read_fd)
    if answer is None:
        return LaunchResult(
            outcome=LAUNCH_UNCERTAIN,
            handle=process,
            detail=(
                f"no answer within {HANDOVER_TIMEOUT_S:.0f}s; the turn stays claimed "
                "because silence is not proof that it did not start"
            ),
        )
    if answer.split(" ", 1)[0] == HANDOVER_ACCEPTED:
        return LaunchResult(outcome=LAUNCH_STARTED, handle=process)
    _reap_unstarted(process)
    detail = answer or "the turn process exited without taking delivery"
    if answer.split(" ", 1)[0] == HANDOVER_DECLINED:
        detail = answer
    return LaunchResult(outcome=LAUNCH_NOT_STARTED, detail=detail)


def _reap_unstarted(process: subprocess.Popen[bytes]) -> None:
    """Collect a child that never took its turn, without waiting on it forever.

    It has already answered — or closed its answer — so it is on its way out.
    A child that somehow is not gets killed: it holds no turn, so there is
    nothing for it to finish, and leaving it would leak a process per failure.
    """
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)


def _serve_turn(
    *,
    handover_fd: int,
    base_url: str,
    gateway_url: str,
    worker_id: str,
    lease_ttl: float,
    agent_timeout_s: float,
    host: str | None,
) -> int:
    """Run the one turn delivered on standard input, or decline having none.

    The answer is sent **after everything that can still be declined and
    before anything that cannot**. That ordering is the whole contract in both
    directions: the host treats a child that exits without answering as one
    that never had the turn and requeues it unspent, so everything this
    process could fail at without consequence belongs above the answer, and
    everything a user or an operator could notice belongs below it.

    Preflight is above it for exactly that reason. It observes Docker, the
    image and the network and changes nothing, so failing it means the turn
    has not begun — and answering first would instead strand that turn behind
    a lease it will never use, having spent one of its attempts.
    """
    # Drained first, and before anything slow, so the host is never left
    # blocked writing a long prompt into a pipe this process has not read yet.
    raw = sys.stdin.read()

    # Then the read-only probe of Docker, the image and the network. It runs
    # before the payload is even looked at so that a turn process started with
    # nothing on its input still proves this machine could have run one, which
    # is what the deployment check needs of it.
    try:
        backend = build_backend_from_env()
        backend.preflight()
    except Exception as exc:
        # Non-zero: the deployment check reads this exit status, and an image
        # that cannot run turns has to fail a rollout rather than pass it. For
        # a real turn the host puts it straight back and, after a run of these,
        # stops claiming and says why — so a machine in this state costs the
        # queue nothing while somebody fixes it.
        write_handover(handover_fd, f"{HANDOVER_DECLINED} {exc}")
        return 2

    payload = decode_turn(raw)
    if payload is None:
        write_handover(handover_fd, f"{HANDOVER_DECLINED} no turn was delivered")
        return 0
    try:
        job = ClaimedJob.from_response(payload)
    except (KeyError, TypeError, ValueError) as exc:
        write_handover(handover_fd, f"{HANDOVER_DECLINED} unusable turn ({type(exc).__name__})")
        return 0

    # Installed before the answer, so there is no window in which this process
    # owns a turn and SIGTERM would still kill it outright. From here the turn
    # is this process's: SIGTERM means the container is being replaced, the
    # turn still runs to its end, and the grace period bounds that exactly as
    # it did when a replica ran one job at a time.
    _drain_on_sigterm()
    write_handover(handover_fd, f"{HANDOVER_ACCEPTED} {job.job_id}")
    return run_turn(
        job,
        base_url=base_url,
        gateway_url=gateway_url,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
        agent_timeout_s=agent_timeout_s,
        backend=backend,
        host=host,
    )


def run_forever(
    *,
    base_url: str,
    gateway_url: str = "",
    worker_id: str,
    lease_ttl: float = DEFAULT_LEASE_TTL_S,
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    idle_sleep_s: float = 5.0,
    backend: SandboxBackend | None = None,
    host: str | None = None,
    spawn_turn: Callable[[dict[str, Any]], LaunchResult] | None = None,
    tick_s: float | None = None,
) -> int:
    """Serve this host: claim turns up to its limit, and run each in its own child.

    One process per machine. It enrolls, heartbeats and samples capacity — the
    things a host does once — and it claims a turn only when it is under the
    limit an operator set on the admin Servers page. There is no pool of idle
    processes to keep at a size: the limit is a number this loop compares
    against what it is already running, so raising it lets the next poll take
    more and lowering it simply stops taking any until the count has come down.
    Turns already running are never interrupted by a change.

    The limit arrives on the heartbeat answer and is this host's own default
    until one has been set. It is reported back as ``slots_total`` on every
    beat, and the control plane admits claims against the lower of that and the
    operator's number, so a number this host has not yet confirmed can only
    ever admit less work, never more.

    The host runner owns scheduling and attestation only. The workspace broker
    owns every retained container and resident Agent CLI process, so this loop
    never creates or bind-mounts a host worktree.
    """
    backend = backend or build_backend_from_env()
    backend.preflight()

    checker = getattr(backend, "check_gateway_reachable", None)
    if checker is not None:
        checker(gateway_url or base_url)

    stamper = getattr(backend, "version_stamp", None)
    stamp: dict[str, Any] = stamper() if stamper is not None else {}
    capabilities = _shared_live_host_capabilities(backend, stamp)
    if SHARED_SANDBOX_CAPABILITY not in capabilities:
        raise RuntimeError(
            "this runner cannot attest shared-sandbox-v1; verify the container "
            "backend, broker URL/token, AGENT_HOST_SLOTS, image labels, "
            "runtime pins, and live-session driver capabilities"
        )
    if not (os.environ.get("AGENT_DEPLOYMENT_SHA") or "").strip():
        raise RuntimeError(
            "shared-live sandbox execution requires AGENT_DEPLOYMENT_SHA; "
            "sandbox_host.sh manages it together with the ready marker"
        )
    if not host:
        raise RuntimeError(
            "shared-live sandbox execution requires AGENT_RUNNER_HOST; every turn "
            "this machine runs reports the same enrolled host name"
        )
    try:
        host_default = default_slots()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    # Advertised beside the shared-live capabilities, never instead of them: it
    # says this runner applies ``slots_desired``, which is what lets the admin
    # page tell a host that will honour a new number from one that needs a
    # redeploy to change.
    host_capabilities = [*capabilities, HOST_CONCURRENCY_CAPABILITY]
    identity = ensure_enrolled(
        base_url=base_url,
        host=host,
        broker_url=_broker_url_from_env(),
        slots_total=host_default,
        capabilities=host_capabilities,
        runner_version=_runner_version(),
    )
    if identity is None:
        raise RuntimeError(
            "shared-live sandbox execution requires an enrolled host identity; "
            "configure AGENT_ENROLL_TOKEN or a persisted host credential"
        )

    launch = spawn_turn or (
        lambda payload: _spawn_turn(
            payload,
            base_url=base_url,
            gateway_url=gateway_url or base_url,
            worker_id=str(payload.get("_worker_id") or worker_id),
            lease_ttl=lease_ttl,
            agent_timeout_s=agent_timeout_s,
            host=host,
        )
    )
    turns = TurnPool(launch=launch)
    # The limit this host is willing to serve right now. Held across beats so
    # a control plane that is briefly unreachable leaves the host running what
    # it last confirmed rather than falling back to a default nobody chose.
    limit = host_default

    capacity = _start_host_capacity_collector(backend)
    pool = HostHeartbeat(
        base_url=base_url,
        identity=identity,
        broker_url=_broker_url_from_env(),
        # Read per beat: the limit is what this host will actually allow, and
        # the control plane admits claims against the lower of it and the
        # operator's number.
        slots_total=lambda: limit,
        launch_error=lambda: turns.launch_error,
        capabilities=lambda: [
            *_shared_live_host_capabilities(backend, stamp),
            HOST_CONCURRENCY_CAPABILITY,
        ],
        runner_version=_runner_version(),
        sandbox_runtime=stamp.get("sandbox_runtime"),
        sandbox_image_digest=stamp.get("sandbox_image_digest"),
        runtime_versions=stamp.get("runtime_versions"),
        runtime_versions_available=stamp.get("runtime_versions_available"),
        readiness_marker=_shared_live_ready_file(),
        retained_sandbox_beats=_retained_sandbox_heartbeat_rows,
        # Read per beat, so the age it carries is the age it had when the beat
        # was built. ``None`` before the first sample, and once the newest one
        # is older than the protocol allows: an unsent observation reads as
        # stale on the page, which is the truth.
        capacity_beats=None if capacity is None else lambda: capacity.latest()[0],
        on_residencies_lost=_handle_host_residencies_lost,
        on_retained_sandboxes_stop=_stop_retained_sandboxes,
    )
    pool.start()
    _drain_on_sigterm()

    def stop_all() -> None:
        """Stop heartbeating and sampling, in that order.

        The sampler never joins a heartbeat in flight, so the pool is stopped
        first and the thread that fed it second; neither call can block on the
        other, which is what keeps a shut-down runner from waiting on a Docker
        daemon it no longer needs.
        """
        pool.stop()
        if capacity is not None:
            capacity.stop()

    print(
        f"agent runner {worker_id} serving host {identity.host} (enrolled; "
        f"sandbox backend: {backend.name}; up to {limit} turn(s) at once until the "
        "control plane says otherwise)",
        flush=True,
    )

    idle_wait = tick_s if tick_s is not None else idle_sleep_s
    immediate = False
    claims = 0
    try:
        while True:
            for finished in turns.reap():
                logger.info(
                    "agent_turn_finished",
                    extra={
                        "event": "agent_turn_finished",
                        "job_id": finished.job_id,
                        "attempt_id": finished.attempt_id,
                        "exit_code": finished.exit_code,
                    },
                )
            if _STOPPING.is_set():
                print(
                    "agent runner: stopping; turns already running finish first",
                    file=sys.stderr,
                    flush=True,
                )
                break
            if pool.removed.is_set():
                print(
                    "agent runner: this host's credential was rejected; not claiming further work",
                    file=sys.stderr,
                    flush=True,
                )
                break

            # Whatever the last successful beat carried, including nothing:
            # clearing the number on the admin page is an instruction like any
            # other, and a host that only followed numbers it could parse went
            # on serving the old one until its container was replaced.
            #
            # No special case is needed to survive an outage. A failed beat
            # raises before it reads the answer, so ``slots_desired`` keeps the
            # value it last carried and this follows it unchanged.
            confirmed = requested_slots(identity.slots_desired)
            target = host_default if confirmed is None else confirmed
            if target != limit:
                logger.info(
                    "agent_host_concurrency_changed",
                    extra={
                        "event": "agent_host_concurrency_changed",
                        "host": identity.host,
                        "was": limit,
                        "now": target,
                        "source": "host_default" if confirmed is None else "control_plane",
                        "running": turns.running(),
                    },
                )
                limit = target
                # The scheduler admits against the number on the host row, so
                # a change this host has applied should be there now rather
                # than up to an interval from now.
                pool.wake()

            if not pool.claimable or not turns.may_claim(limit):
                time.sleep(idle_wait)
                continue
            # Keep the old control plane safe during a parallel rollout. It
            # does not enforce this generation's exact-SHA attestation, so this
            # host stays idle until its own checks complete.
            if not _shared_live_deployment_ready(backend, stamp):
                time.sleep(idle_wait)
                continue

            # A distinct lease owner per turn. Fencing never reads this
            # string, but "which turn is stuck" stops being answerable the
            # moment two concurrent turns report the same name — which is
            # exactly when it is worth asking.
            claims += 1
            turn_worker_id = f"{worker_id}-{claims}"
            try:
                payload = claim_payload(
                    base_url=base_url,
                    host_credential=pool.credential,
                    worker_id=turn_worker_id,
                    lease_ttl=lease_ttl,
                    attestation=_claim_attestation(backend),
                )
            except ClaimUnreachable as exc:
                print(
                    f"agent runner: control plane unreachable, retrying: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(idle_wait)
                continue
            except Exception as exc:
                print(
                    f"agent runner: claim failed unexpectedly: {exc}", file=sys.stderr, flush=True
                )
                time.sleep(idle_wait)
                continue

            if payload is None:
                time.sleep(idle_wait)
                continue

            _hand_over_turn(payload, turns=turns, base_url=base_url, worker_id=turn_worker_id)
    except KeyboardInterrupt:
        immediate = True
    turns.shutdown(immediate=immediate)
    stop_all()
    return 0


def _hand_over_turn(
    payload: dict[str, Any], *, turns: TurnPool, base_url: str, worker_id: str
) -> None:
    """Give one claimed turn to a child, and requeue it only if it never began.

    The requeue is the narrow case and is kept narrow deliberately. A turn the
    child took is the child's — if it then dies, the lease expires and the
    control plane decides under the retained-session rules, which can end a
    conversation rather than replay an admitted prompt. Handing such a turn
    back from here would be this host overruling that judgement with a guess.
    """
    job_id = str(payload.get("job_id") or "")
    # Carried beside the turn rather than in it: the child logs under the same
    # name the claim recorded as this turn's lease owner. Stripped before
    # delivery, so the payload the child parses is the control plane's answer
    # and nothing this host added to it.
    payload = {**payload, "_worker_id": worker_id}
    try:
        result = turns.launch(payload)
    except Exception as exc:
        # Whatever went wrong here, this host cannot say the turn did not
        # start — and an exception escaping the loop would end the service,
        # leaving every turn it was running to its lease. Treat it as the
        # ambiguous case it is.
        logger.warning(
            "agent_turn_handover_failed",
            exc_info=True,
            extra={"event": "agent_turn_handover_failed", "job_id": job_id},
        )
        result = LaunchResult(LAUNCH_UNCERTAIN, None, f"{type(exc).__name__}: {exc}")
    if result.outcome == LAUNCH_STARTED:
        return
    if result.outcome == LAUNCH_UNCERTAIN:
        logger.warning(
            "agent_turn_handover_uncertain",
            extra={
                "event": "agent_turn_handover_uncertain",
                "job_id": job_id,
                "detail": result.detail,
            },
        )
        return
    # Provably never started: back to the queue, with no attempt spent and no
    # wait for a lease that nothing is holding.
    worker_token = str(payload.get("worker_token") or "")
    released = bool(worker_token) and release_turn(
        base_url=base_url,
        worker_token=worker_token,
        job_id=job_id,
        reason="launch_failed",
    )
    logger.warning(
        "agent_turn_not_started",
        extra={
            "event": "agent_turn_not_started",
            "job_id": job_id,
            "detail": result.detail,
            "requeued": released,
        },
    )


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

    This controls the delay before an otherwise-settled chat container is
    stopped. Agent-turn and terminal limits are independent watchdogs and must
    never be used to decide that a healthy chat is inactive.
    """
    return _env_float("AGENT_CHAT_IDLE_TIMEOUT_S", DEFAULT_CHAT_IDLE_TIMEOUT_S)


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

    The retained-only service requires this value (or enrollment supplies a
    configured name); an unreported host cannot claim shared work.
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
        help="Control plane: claim, heartbeat, events, lifecycle, terminals.",
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
    # The compose overlay sets AGENT_LEASE_TTL and AGENT_TIMEOUT_S, and
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
    # Not operator flags. The host service starts each turn's child with these
    # and the exact configuration it was given itself; the turn arrives on that
    # child's standard input and its answer goes back on the descriptor named
    # here. Started by hand, a turn process reads an empty standard input,
    # declines, and exits.
    parser.add_argument("--turn", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--handover-fd", type=int, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by the self-hosted runner service and local tools."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # In the source repo, importing sandbox configured root logging as a side
    # effect; the host shim removed that on purpose. This is the sanctioned
    # replacement (MIGRATION.md deferred-behaviour register, D6 row) — without
    # it every INFO event a runner emits goes nowhere.
    configure_process_logging()

    if not args.base_url:
        parser.error("AGENT_CONTROL_PLANE_URL (or AGENT_GATEWAY_URL) is required")

    if args.turn:
        if args.handover_fd is None:
            parser.error("--turn requires --handover-fd; the host service supplies both")
        return _serve_turn(
            handover_fd=args.handover_fd,
            base_url=args.base_url,
            gateway_url=args.gateway_url or args.base_url,
            worker_id=args.worker_id,
            lease_ttl=args.lease_ttl,
            agent_timeout_s=args.agent_timeout,
            host=args.host,
        )
    return run_forever(
        base_url=args.base_url,
        gateway_url=args.gateway_url or args.base_url,
        worker_id=args.worker_id,
        lease_ttl=args.lease_ttl,
        agent_timeout_s=args.agent_timeout,
        host=args.host,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ClaimedJob",
    "ControlPlane",
    "LeaseLost",
    "WorktreeError",
    "checkout_output_branch",
    "main",
    "prepare_worktree",
    "run_forever",
    "run_once",
    "run_turn",
]
