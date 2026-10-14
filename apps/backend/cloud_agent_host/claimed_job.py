"""Claim payloads and lease failures at the host's control-plane boundary.

These values describe the attempt handed to a host before execution begins.
They depend on shared plugin validation, without loading the runner's sandbox,
process, heartbeat, or publication orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cloud_agent_shared.environment import BoundEnvironment
from cloud_agent_shared.plugins import MAX_SANDBOX_PLUGINS, validate_sandbox_plugins


class LeaseLost(Exception):
    """Raised when this attempt no longer owns the job and must stop."""


class ClaimUnreachable(Exception):
    """The gateway could not be reached *to claim*, so no job was taken.

    Distinct from a transport failure later in the run: that one leaves an
    attempt running until its lease expires, and an operator needs to be sent
    to the job rather than told nothing was claimed.
    """


class WorkWaitUnsupported(Exception):
    """The control plane has no wait endpoint, so an idle host polls instead."""


@dataclass
class ClaimedJob:
    """What the control plane handed the enrolled host."""

    job_id: str
    thread_id: str
    attempt_id: int
    attempt_no: int
    #: Null for a temporary workspace, which starts empty instead of cloning.
    repo: str | None
    base_sha: str | None
    task_prompt: str
    runtime: str
    model: str
    worker_token: str
    # The attempt's gateway grant. It does not enter the sandbox: each member's
    # CLI runs on its own residency grant from `establish_shared_residency`.
    sandbox_token: str
    execution_profile: str = "trusted-v1"
    # The MCP credential. A *second* credential, because tools and inference
    # are two services now: a leaked MCP token buys no inference and a leaked
    # grant buys no tools. The relay it opens is this host's own, from
    # `_sandbox_mcp_url`, not anything the claim says.
    mcp_token: str = ""
    # How hard the model should think on this turn, in the runtime's own
    # vocabulary. None is Auto: no flag, whatever the CLI and model default to.
    # Defaulted rather than required, because absent and Auto are the same
    # state — there is nothing a caller could pass to mean "unknown".
    reasoning_effort: str | None = None
    # Shell run in the sandbox before the agent, on the group's one network.
    # Optional, so it carries a default rather than forcing every caller to
    # pass one.
    setup_script: str | None = None
    environment: BoundEnvironment | None = field(default=None, repr=False)
    # Explicit CLI plugins installed in this chat's private member state.
    plugins: list[dict[str, str]] = field(default_factory=list)
    # Read-only, single-repo, short-lived: what the runner clones with. Never
    # put in .git/config or the sandbox environment.
    clone_token: str | None = None
    # A separate repository-scoped contents/PR write token for sandbox gh.
    github_token: str | None = field(default=None, repr=False)
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
    # How many uploads this whole conversation holds, counted by the control
    # plane. Zero means skip the fetch, which is every turn of every task that
    # attached nothing. Absent from a control plane that predates uploads, and
    # absent means the same thing as none.
    thread_attachment_count: int = 0
    # How many this turn itself added. A retained container already holds the
    # earlier turns' files, so only a new container or a turn that attached
    # something needs the import to run.
    turn_attachment_count: int = 0

    @classmethod
    def from_response(cls, body: dict[str, Any]) -> ClaimedJob:
        """Build from the claim endpoint's response."""
        raw_plugins = body.get("plugins", [])
        if not isinstance(raw_plugins, list) or len(raw_plugins) > MAX_SANDBOX_PLUGINS:
            raise ValueError("claim plugins must be a bounded list")
        plugins = validate_sandbox_plugins(body["runtime"], raw_plugins)
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
            environment=BoundEnvironment.model_validate(body["environment"])
            if body.get("environment")
            else None,
            plugins=plugins,
            runtime=body["runtime"],
            model=body["model"],
            execution_profile=body.get("execution_profile") or "trusted-v1",
            # Absent from a control plane that predates the control, which is
            # the same thing as Auto — no separate "unknown" state to carry.
            reasoning_effort=body.get("reasoning_effort"),
            worker_token=body["worker_token"],
            sandbox_token=body["sandbox_token"],
            mcp_token=body.get("mcp_token") or "",
            clone_token=body.get("clone_token") or None,
            github_token=body.get("github_token") or None,
            mcp_bindings=list(body.get("mcp_bindings") or []),
            runtime_version=body.get("runtime_version") or None,
            thread_sandbox=(
                dict(body["thread_sandbox"])
                if isinstance(body.get("thread_sandbox"), dict)
                else None
            ),
            thread_attachment_count=int(body.get("thread_attachment_count") or 0),
            turn_attachment_count=int(body.get("turn_attachment_count") or 0),
        )
