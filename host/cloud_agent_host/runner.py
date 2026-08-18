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
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

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
from cloud_agent_host.logging_utils import configure_process_logging
from cloud_agent_host.patch_gate import branch_name_for, validate_patch
from cloud_agent_host.residency import ResidentEntry, ResidentPool
from cloud_agent_host.runtimes import (
    EMPTY_RUNTIME_MCP_CONFIG,
    AgentRuntime,
    NormalizedEvent,
    RuntimeMCPConfig,
    get_runtime,
)
from cloud_agent_host.sandbox import (
    SandboxBackend,
    SandboxSpec,
    build_backend_from_env,
)
from cloud_agent_host.setup import build_cache_from_env, run_setup
from cloud_agent_host.workspace_paths import purge_stale_workspaces, workspace_path
from cloud_agent_host.workspace_snapshot import build_workspace_snapshot

DEFAULT_LEASE_TTL_S = 120.0
HEARTBEAT_INTERVAL_S = 30.0
DEFAULT_AGENT_TIMEOUT_S = 3600.0
DEFAULT_WORKSPACE_TTL_S = 7 * 24 * 60 * 60
# How often the control checks run while the agent is silent.
_POLL_INTERVAL_S = 0.5
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
    # The MCP credential, and the relay it opens. A *second* pair, because
    # tools and inference are two services now: a leaked MCP token buys no
    # inference and a leaked grant buys no tools. Empty means this run has no
    # MCP, which the runtime refuses rather than resolving against the
    # inference address.
    mcp_token: str = ""
    mcp_base_url: str = ""
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

    def report_continuity(self, mode: str) -> None:
        """Record this attempt's continuity rung. Best-effort observability."""
        try:
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/continuity", {"mode": mode})
        except LeaseLost:
            raise
        except Exception:
            pass

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


def save_workspace_snapshot(control: ControlPlane, *, workdir: str, patch: str) -> bool:
    """Store the changed-file overlay without making it job-critical.

    The patch remains the authoritative publishing artifact. Files are a
    convenience view, so a local read error or an oversized artifact must not
    turn an otherwise completed agent run into a failed job. Lease loss is
    different: the attempt is fenced and must stop immediately.
    """
    try:
        content = build_workspace_snapshot(workdir, patch)
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


def _resident_base_ready(workdir: str, job: ClaimedJob) -> bool:
    """Make the turn's pinned base resolvable in the resident tree.

    A publish since the last turn moved ``base_sha`` to a commit this tree
    has never fetched; ``build_patch`` diffs against it later, so it must
    exist locally before the turn is allowed to run hot.
    """
    if not job.base_sha:
        return True
    if not _COMMIT_SHA.fullmatch(job.base_sha):
        return False
    probe = _run_git(["cat-file", "-e", f"{job.base_sha}^{{commit}}"], cwd=workdir)
    if probe.returncode == 0:
        return True
    url = f"https://github.com/{job.repo}.git"
    fetch = _run_git(
        ["fetch", "--quiet", "--depth", "1", url, "--end-of-options", job.base_sha],
        cwd=workdir,
        env_extra=_auth_env(job.clone_token),
    )
    if fetch.returncode != 0:
        return False
    _assert_no_credential_on_disk(workdir, job.clone_token)
    return _run_git(["cat-file", "-e", f"{job.base_sha}^{{commit}}"], cwd=workdir).returncode == 0


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
    # Hermetic environment, and the *backend's* idea of it rather than the
    # runner's. For an isolated backend the runner's PATH and HOME name paths
    # inside the runner's own filesystem: forwarding HOME=/root into a
    # container that runs unprivileged makes every agent CLI fail on startup
    # trying to write its config. Inheriting the whole environment would be
    # worse still — it would hand the agent whatever the host is carrying.
    base_env = dict(backend.base_env())
    base_env.update(_SAFE_DIRECTORY_ENV)

    driver = driver_for(runtime)
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
        spawn=lambda argv, env: backend.spawn(
            SandboxSpec(argv=argv, workdir=workdir, env=env, state_dir=state_dir)
        ),
        base_env=base_env,
        # The runner is the only source of this value. Repository contents
        # never become runtime MCP configuration, and adapters without a
        # gateway-mediated path reject a non-empty set.
        mcp_config=mcp_config,
        state_dir=state_dir,
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
                    spawn_live=lambda argv, env: backend.spawn(
                        SandboxSpec(
                            argv=argv,
                            workdir=workdir,
                            env=env,
                            state_dir=state_dir,
                            live_input=True,
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
                turn = live.start_turn(TurnInput(prompt=job.task_prompt, model=job.model))
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
                turn_input = TurnInput(prompt=job.task_prompt, model=job.model)
                # ``hot`` only when the process predates the turn — a fresh
                # spawn is warm_native whatever transport it runs on, and a
                # label that overclaims makes the hit-rate lie.
                control.report_continuity("hot" if was_alive else "warm_native")
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
            # Fall through to the ladder in the same tree.
            control.append_event(
                NormalizedEvent("lifecycle", {"phase": "warm_fallback", "detail": str(exc)[:200]})
            )
            session = None
        else:
            turn_input = TurnInput(prompt=job.task_prompt, model=job.model)
            control.report_continuity("warm_native")
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
            turn_input = TurnInput(prompt=job.task_prompt, model=job.model)
            control.report_continuity("cold_native")
    if session is None:
        session = driver.launch(context)
        turn_input = TurnInput(prompt=conversation_prompt(job), model=job.model)
        control.report_continuity("replay")
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
            if heart.cancel_requested:
                turn.cancel()
                control.append_event(NormalizedEvent("lifecycle", {"phase": "cancelled_by_owner"}))
                return 130, "".join(tail), tool_errors
            if time.monotonic() > deadline:
                turn.cancel()
                control.append_event(
                    NormalizedEvent("error", {"text": f"agent exceeded {timeout_s:.0f}s"})
                )
                return 124, "".join(tail), tool_errors

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
        exit_code = result.exit_code
        if live_out is not None and result.status == "completed":
            # What the caller needs to keep the thread resident: the announced
            # session id (captured by the session's own runtime — for a borrowed
            # live session that is a previous turn's instance, not this turn's),
            # and the process itself when it survived its turn.
            live_out["session_id"] = getattr(session.runtime, "session_id", None)
            if bool(getattr(session, "process_alive", lambda: False)()):
                live_out["session"] = session
        if result.status == "completed" and state_dir is not None:
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
    if backend is None:
        backend = build_backend_from_env()
        backend.preflight()

    job = claim(
        base_url=base_url,
        dispatcher_token=dispatcher_token,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
        host=host,
    )
    if job is None:
        print("no queued agent job; nothing to do")
        return 0

    # Long-lived runners give every job a durable worktree.  The browser and
    # terminal broker address this same directory after the agent exits;
    # follow-up jobs receive their own worktree and are rehydrated through the
    # existing, validated context-patch path.
    if workspace_root is not None:
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
            # Every resident turn is a fresh process with a fresh environment
            # (design §13, measured), so a model switch or an MCP grant needs
            # nothing the restart does not already provide — the live-process
            # era's gates on both are gone. What still ends a residency is a
            # harness switch (another CLI cannot read this session) and a
            # base the tree cannot fetch.
            usable = entry.runtime_id == job.runtime and _resident_base_ready(entry.workdir, job)
            if usable:
                resident = entry
                workdir = entry.workdir
                state_dir = entry.state_dir
                context_state = None
            else:
                # A restart-shaped turn (model switch, MCP, unfetchable base):
                # end the residency and run the ladder from COLD — the
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
            NormalizedEvent("lifecycle", {"phase": "checked_out", "base_sha": base_sha})
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
    backend.preflight(workdir_root=str(root))
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
    identity = ensure_enrolled(
        base_url=base_url,
        host=host or worker_id,
        broker_url=_broker_url_from_env(),
        slots_total=int(os.environ.get("AGENT_HOST_SLOTS") or 0) or None,
        runner_version=_runner_version(),
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
        stamp: dict[str, Any] = stamper() if stamper is not None else {}
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
            runner_version=_runner_version(),
            sandbox_runtime=stamp.get("sandbox_runtime"),
            sandbox_image_digest=stamp.get("sandbox_image_digest"),
            runtime_versions=stamp.get("runtime_versions"),
            residency_beats=residents.beats,
            on_residencies_lost=residents.handle_lost,
        )
        pool.start()
        where = f" on host {identity.host} (enrolled)"

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
                host=host,
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
