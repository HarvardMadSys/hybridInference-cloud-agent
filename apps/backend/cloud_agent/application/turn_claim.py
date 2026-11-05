"""Hand a claimed turn over to the host that took it, or refuse it first.

``claim_job`` gives a host the oldest runnable turn and a lease on it. Before
the host learns anything about the turn it is checked again here, in an order
that decides which refusal wins:

1. the owner may still use Agent at all;
2. the turn's repository is still entitled;
3. the host can apply the chat's environment (and which snapshot it may start
   from);
4. the host can apply the group's SSH access;
5. only then are the turn's credentials minted: the runner's control token,
   the owner's model connection and the read-only clone token;
6. its MCP aliases are resolved and its prompt is assembled.

A refusal has already acted on the claim when it comes back: a check that a
retry may pass (an unreadable store, a host without a capability, a deployment
without its key) hands the claim back with no attempt spent; one no host
change can satisfy ends the turn with a reason its owner can read. The route
answers a :class:`ClaimRefusal` with its error and a :class:`ClaimHandover`
with its response; anything else that goes wrong is raised for the route to
hand back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import httpx

from cloud_agent import memory_tool, web_search
from cloud_agent.agent_memory import claim_agent_memories
from cloud_agent.application.claim_credentials import mint_clone_token
from cloud_agent.application.model_connections import claim_model_connection
from cloud_agent.application.ssh_servers import build_ssh_access, servers_for_thread, ssh_key_cipher
from cloud_agent.credential_cipher import CredentialCipherError
from cloud_agent.domain.errors import Refused, Unavailable
from cloud_agent.domain.execution import FAILED, RUNNING
from cloud_agent.entitlement import RepoAccessUnavailable, RepoNotAllowed, require_entitled_repo
from cloud_agent.environments import EnvironmentAccessRevoked, EnvironmentEncryptionError
from cloud_agent.execution_profiles import ExecutionProfile
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.registry import resolve_for_thread
from cloud_agent.mcp.sandbox_servers import split_bindings
from cloud_agent.owner_access import (
    AGENT_ACCESS_WITHDRAWN,
    AgentAccessUnavailable,
    owner_may_use_agent,
)
from cloud_agent.prompt_injections import claim_prompt
from cloud_agent.sandbox_manager.sandbox_manager_client import SandboxManagerError
from cloud_agent.schemas import WorkerClaimResponse
from cloud_agent.tokens import SCOPE_FULL, SCOPE_MCP, mint_worker_token
from cloud_agent_shared.environment import (
    ENVIRONMENT_CAPABILITY,
    ENVIRONMENT_INFERENCE_CAPABILITY,
    environment_payload,
    uses_inference_names,
)
from cloud_agent_shared.environment_snapshots import ENVIRONMENT_SNAPSHOT_CAPABILITY
from cloud_agent_shared.runtime_catalog import runtimes_supporting_mcp
from cloud_agent_shared.ssh_access import SSH_ACCESS_CAPABILITY

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cloud_agent.ports import (
        AgentMemoryRepository,
        AttachmentRepository,
        AttemptCommands,
        HostPoolRepository,
        McpConnectionRepository,
        ModelConnectionRepository,
        SourceControlRepository,
        SshServerRepository,
    )
    from cloud_agent.ports.environment_snapshots import EnvironmentSnapshotRepository
    from cloud_agent.ports.environments import EnvironmentExecutionRepository
    from cloud_agent.schemas import WorkerClaimAttestation
    from cloud_agent_shared.environment import BoundEnvironment
    from cloud_agent_shared.model_connections import ModelConnectionAccess
    from cloud_agent_shared.sandbox_mcp import SandboxMcpServer
    from cloud_agent_shared.ssh_access import SshAccess

#: The claim route's logger name, which the deployment's log pipeline selects
#: on: these records were the route's own before they moved here.
logger = get_logger("cloud_agent.api.routes")

#: How long a handed-back claim waits before it is eligible again.
_RETRY_AFTER_S = 30

ClaimAction = Literal["release", "fail"]


@dataclass(frozen=True, slots=True)
class ClaimHandover:
    """The claimed turn, checked and credentialed, as the host receives it."""

    response: WorkerClaimResponse


@dataclass(frozen=True, slots=True)
class ClaimRefusal:
    """A claim refused at hand-over, after ``action`` was already taken on it.

    ``release`` handed the claim back to the queue with no attempt spent;
    ``fail`` ended the turn with the reason ``error`` carries. ``error`` is
    what the host is answered with.
    """

    action: ClaimAction
    error: Refused | Unavailable


class ClaimStorage(Protocol):
    """The capabilities a claim's hand-over reads and acts on."""

    agent_memory: AgentMemoryRepository
    attachments: AttachmentRepository
    attempt_commands: AttemptCommands
    host_pool: HostPoolRepository
    mcp: McpConnectionRepository
    model_connections: ModelConnectionRepository
    source_control: SourceControlRepository

    @property
    def environments(self) -> EnvironmentExecutionRepository:
        """Environment persistence needed by this flow."""
        ...

    environment_snapshots: EnvironmentSnapshotRepository
    ssh_servers: SshServerRepository


@dataclass(frozen=True, slots=True)
class _Credentials:
    """What the claim mints once every check has passed."""

    worker_token: str
    model_connection: ModelConnectionAccess
    clone_token: str | None
    mcp_token: str


async def hand_over_claim(
    store: ClaimStorage,
    claim: dict[str, Any],
    *,
    app_credentials: Any | None,
    owners: Any,
    attestation: WorkerClaimAttestation | None,
    host: str,
) -> ClaimHandover | ClaimRefusal:
    """Check, credential and describe one claimed turn for the host that took it.

    Every step that refuses has already released or ended the claim and raises
    a domain error: an outage after handing the claim back, a refusal after
    ending the turn. Any other exception is the caller's to hand back.
    """
    try:
        response = await _hand_over(
            store,
            claim,
            app_credentials=app_credentials,
            owners=owners,
            attestation=attestation,
            host=host,
        )
    except Unavailable as exc:
        return ClaimRefusal("release", exc)
    except Refused as exc:
        return ClaimRefusal("fail", exc)
    return ClaimHandover(response)


async def _hand_over(
    store: ClaimStorage,
    claim: dict[str, Any],
    *,
    app_credentials: Any | None,
    owners: Any,
    attestation: WorkerClaimAttestation | None,
    host: str,
) -> WorkerClaimResponse:
    """Run the hand-over's steps in order and build the host's claim response."""
    warn_on_unbounded_untrusted_group(claim, attestation, host=host)
    fence = claim_fence(claim)
    # Whether the owner may still use Agent at all comes first: it is the one
    # check every turn passes here, including the ones no request admitted.
    await require_owner_agent_access(store, owners, claim)
    await require_entitled_repository(store, claim, app_credentials=app_credentials)
    capabilities = attestation.runner_capabilities if attestation else []
    environment, environment_snapshot = await claim_environment(
        store, claim, capabilities, attestation
    )
    ssh_state, ssh_access = await claim_ssh_access(store, claim, capabilities)
    credentials = await _mint_credentials(
        store, claim, fence, capabilities, app_credentials=app_credentials
    )
    mcp_aliases, sandbox_mcp_servers = await mcp_bindings(store, claim)
    # Counted for the whole conversation, not this turn: a rebuilt sandbox has
    # to restore every upload the chat ever carried, and a host that reads zero
    # here skips the fetch entirely — which is every turn that attached nothing.
    thread_attachment_count = await store.attachments.count_thread_attachments(
        thread_id=str(claim["thread_id"])
    )
    task_prompt, context_injections = await _task_prompt(store, claim, ssh_state)
    return WorkerClaimResponse(
        thread_attachment_count=thread_attachment_count,
        turn_attachment_count=len(claim.get("attachments") or []),
        job_id=claim["id"],
        thread_id=str(claim["thread_id"]),
        parent_job_id=claim.get("parent_job_id"),
        turn_no=claim.get("turn_no") or 1,
        attempt_id=claim["attempt_id"],
        attempt_no=claim["attempt_no"],
        repo=claim["repo"],
        base_sha=claim["base_sha"],
        task_prompt=task_prompt,
        context_injections=context_injections,
        setup_script=claim.get("setup_script"),
        environment=(
            environment_payload(environment, capabilities) if environment is not None else None
        ),
        environment_snapshot=environment_snapshot,
        ssh_access=ssh_access,
        model_connection=credentials.model_connection,
        plugins=claim.get("plugins") or [],
        runtime=claim["runtime"],
        model=claim["model"],
        execution_profile=claim.get("execution_profile", "trusted-v1"),
        reasoning_effort=claim.get("reasoning_effort"),
        runtime_version=claim.get("runtime_version"),
        thread_sandbox=claim.get("thread_sandbox"),
        worker_token=credentials.worker_token,
        # COMPAT(remove-after=2026-11-30): as at its mint in ``_mint_credentials``.
        mcp_token=credentials.mcp_token,
        clone_token=credentials.clone_token,
        # Aliases, because that is what the runtime config addresses: the relay
        # path segment is the binding alias, and the runner learns nothing about
        # where the connection points.
        mcp_bindings=mcp_aliases,
        sandbox_mcp_servers=sandbox_mcp_servers,
        metadata=claim["metadata"],
    )


def claim_fence(claim: dict[str, Any]) -> dict[str, Any]:
    """The ``(job_id, attempt_id, lease_generation)`` fence of a claim."""
    # Named, so the lease-fence check finds the dict the ``**fence`` calls
    # spread and can see it still carries the generation.
    fence = {
        "job_id": claim["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    return fence


async def _hand_back(store: ClaimStorage, claim: dict[str, Any], error: Unavailable) -> Unavailable:
    """Return the claim to the queue, spending no attempt, and give back ``error``."""
    await store.attempt_commands.release_claim(
        **claim_fence(claim), retry_after_seconds=_RETRY_AFTER_S
    )
    return error


async def _end_turn(
    store: ClaimStorage, claim: dict[str, Any], *, detail: str, event: str, error: Refused
) -> Refused:
    """Fail the claimed turn with the owner-facing ``detail``, log ``event``, give back ``error``."""
    await store.attempt_commands.transition(
        **claim_fence(claim),
        from_states=(RUNNING,),
        to_state=FAILED,
        detail=detail,
    )
    logger.warning(event, extra={"event": event, "job_id": claim["id"]})
    return error


def warn_on_unbounded_untrusted_group(
    claim: dict[str, Any], attestation: WorkerClaimAttestation | None, *, host: str
) -> None:
    """Log a new untrusted group handed to a host that sets no disk quota.

    Recommended, not required: such a group's writable layer holds its
    workspace, survives ``docker stop``, and shares one disk with every other
    group on the host. The claim still goes ahead; the admin hosts page shows
    the same warning. A runner from before the report attests nothing and is
    not named here.
    """
    if attestation is None or attestation.sandbox_disk_limit_mib != 0:
        return
    if claim.get("execution_profile") != ExecutionProfile.UNTRUSTED_KATA_V1:
        return
    if isinstance(claim.get("thread_sandbox"), dict):
        return
    logger.warning(
        "agent_untrusted_sandbox_without_disk_quota",
        extra={
            "event": "agent_untrusted_sandbox_without_disk_quota",
            "host": host,
            "job_id": claim["id"],
        },
    )


async def require_owner_agent_access(
    store: ClaimStorage,
    owners: Any,
    claim: dict[str, Any],
) -> None:
    """Ask the account directory again whether this job's owner may still use Agent.

    Admission was decided when the job was written, and a job can sit in the
    queue long after that — or be written by a goal or a waiting message with
    no request behind it at all.

    **A denial ends the job rather than handing it back.** Released, it would be
    eligible again at the next claim and refused again, every time; failed, it
    carries a reason the owner can read, and the owner's goal and waiting
    messages pause on it the way they do for any failed turn.

    **No answer hands it back**, with the same delay as the other transient
    claim failures: an unreadable account store is not a verdict on the owner,
    and a released claim spends no attempt.

    Raises:
        Refused: after ending the job when access was withdrawn.
        Unavailable: after releasing the claim when access could not be confirmed.
    """
    try:
        permitted = await owner_may_use_agent(owners, user_id=str(claim["user_id"]))
    except AgentAccessUnavailable as exc:
        await store.attempt_commands.release_claim(
            **claim_fence(claim), retry_after_seconds=_RETRY_AFTER_S
        )
        logger.warning(
            "agent_job_owner_access_unavailable",
            extra={"event": "agent_job_owner_access_unavailable", "job_id": claim["id"]},
        )
        raise Unavailable(
            "agent_access_unavailable",
            "Could not confirm the owner's Agent access; the job has been "
            "returned to the queue and no attempt was spent.",
        ) from exc
    if permitted:
        return
    raise await _end_turn(
        store,
        claim,
        detail=AGENT_ACCESS_WITHDRAWN,
        event="agent_job_owner_access_withdrawn",
        error=Refused("forbidden", "agent_access_denied", AGENT_ACCESS_WITHDRAWN),
    )


async def require_entitled_repository(
    store: ClaimStorage, claim: dict[str, Any], *, app_credentials: Any | None
) -> None:
    """Re-check the turn's repository with the same check its creation used.

    Re-checked at claim time, not just at create time. A row written before
    this check existed — or while the allowlist was wider — must not be able
    to produce a credential now. **And before any credential is minted.**
    Nothing after it is asked for until the job is known to be runnable.

    The allowlist-only variant this replaced meant a job entitled by its
    owner's own GitHub connection passed creation and was then refused at
    claim — the two gates disagreeing about what the word entitled means. A
    temporary workspace has no repository to be entitled to; who may use Agent
    at all was checked before this.

    Raises:
        Unavailable: after releasing the claim when the lookup could not answer.
        Refused: after ending the job when the repository is not allowed.
    """
    if claim["repo"] is None:
        return
    try:
        await require_entitled_repo(
            claim["repo"],
            claim["user_id"],
            store=store.source_control,
            app_credentials=app_credentials,
        )
    except RepoAccessUnavailable as exc:
        raise await _hand_back(
            store, claim, Unavailable("source_control_unavailable", str(exc))
        ) from exc
    except RepoNotAllowed as exc:
        raise await _end_turn(
            store,
            claim,
            detail=str(exc),
            event="agent_job_repo_not_allowed",
            error=Refused("forbidden", "repo_not_allowed", str(exc)),
        ) from exc


async def claim_environment(
    store: ClaimStorage,
    claim: dict[str, Any],
    capabilities: Sequence[str],
    attestation: WorkerClaimAttestation | None,
) -> tuple[BoundEnvironment | None, dict[str, Any] | None]:
    """The chat's required environment and the snapshot it may start from.

    Both are ``None`` for a chat with no environment binding, or one whose
    environment requires nothing.

    Raises:
        Unavailable: after releasing the claim when this host or deployment
            cannot apply the environment.
        Refused: after ending the job when its team environment was revoked.
    """
    if not claim.get("environment_binding_id"):
        return None, None
    try:
        environment = await store.environments.environment_for_claim(
            claim["user_id"], claim["environment_binding_id"], claim["id"]
        )
        if not environment.required:
            return None, None
        await _require_environment_host(store, claim, environment, capabilities)
    except (SandboxManagerError, EnvironmentEncryptionError):
        raise await _hand_back(
            store,
            claim,
            Unavailable(None, "Waiting for environment-capable host or available encryption"),
        ) from None
    except EnvironmentAccessRevoked as exc:
        # The chat carries a team's Secrets its owner may no longer use. No
        # host change makes it runnable again, so it fails rather than waits.
        raise await _end_turn(
            store,
            claim,
            detail=str(exc),
            event="agent_job_team_environment_revoked",
            error=Refused("forbidden", "team_environment_revoked", str(exc)),
        ) from exc
    return environment, await _environment_snapshot(
        store, claim, environment, capabilities, attestation
    )


async def _require_environment_host(
    store: ClaimStorage,
    claim: dict[str, Any],
    environment: BoundEnvironment,
    capabilities: Sequence[str],
) -> None:
    """Require a runner and a sandbox manager that can both apply ``environment``.

    Raises:
        SandboxManagerError: when either of them cannot.
    """
    if ENVIRONMENT_CAPABILITY not in capabilities:
        raise SandboxManagerError(503, "Host cannot execute environment configuration")
    if uses_inference_names(environment) and ENVIRONMENT_INFERENCE_CAPABILITY not in capabilities:
        raise SandboxManagerError(503, "Host cannot isolate inference variables")
    from cloud_agent.sandbox_manager.sandbox_manager_routing import sandbox_manager_for_job

    try:
        broker = await sandbox_manager_for_job(store.host_pool, claim["id"])
        if broker is None:
            raise SandboxManagerError(503, "Sandbox manager is unavailable")
        await broker.require_environment_capability(environment)
    except (SandboxManagerError, httpx.HTTPError):
        raise SandboxManagerError(503, "Sandbox manager must be upgraded") from None


async def _environment_snapshot(
    store: ClaimStorage,
    claim: dict[str, Any],
    environment: BoundEnvironment,
    capabilities: Sequence[str],
    attestation: WorkerClaimAttestation | None,
) -> dict[str, Any] | None:
    """The installation snapshot a new workspace may start from, if any."""
    if not (
        environment.setup_script
        and environment.setup_fingerprint
        # Only a workspace this turn creates can start from a snapshot.
        and not claim.get("thread_sandbox")
        and ENVIRONMENT_SNAPSHOT_CAPABILITY in capabilities
    ):
        return None
    return await store.environment_snapshots.snapshot_for_claim(
        user_id=claim["user_id"],
        binding_id=claim["environment_binding_id"],
        base_image=attestation.sandbox_image_digest if attestation else None,
    )


async def claim_ssh_access(
    store: ClaimStorage, claim: dict[str, Any], capabilities: Sequence[str]
) -> tuple[dict[str, Any] | None, SshAccess | None]:
    """The group's SSH state and the access the host applies from it.

    The state is ``None`` when none of the group's chats ever selected a
    server, and then there is no access either. Settled before any credential
    is minted, like the environment: a host that cannot apply it must not
    start the turn at all.

    Raises:
        Unavailable: after releasing the claim when this host or deployment
            cannot apply the access.
    """
    ssh_state = await store.ssh_servers.ssh_group_state(thread_id=str(claim["thread_id"]))
    if ssh_state is None:
        return None, None
    try:
        if SSH_ACCESS_CAPABILITY not in capabilities:
            raise SandboxManagerError(503, "Host cannot apply SSH access")
        ssh_access = build_ssh_access(ssh_state, ssh_key_cipher())
    except (SandboxManagerError, CredentialCipherError, ValueError):
        raise await _hand_back(
            store,
            claim,
            Unavailable(None, "Waiting for an SSH-capable host or available encryption"),
        ) from None
    return ssh_state, ssh_access


async def _mint_credentials(
    store: ClaimStorage,
    claim: dict[str, Any],
    fence: dict[str, Any],
    capabilities: Sequence[str],
    *,
    app_credentials: Any | None,
) -> _Credentials:
    """Mint the turn's credentials, now that it is known to be runnable.

    Raises:
        Unavailable: after releasing the claim, from the model connection or
            the clone token.
        Refused: after ending the job when its model connection cannot run it.
    """
    # The runner keeps the full-scope control credential; it never enters the
    # sandbox, whose CLI runs on its residency's model connection key
    # (``worker_establish_shared_sandbox_residency``). ``SCOPE_FULL`` is this
    # service's own credential, signed with this service's own secret.
    worker_token = mint_worker_token(**fence, scope=SCOPE_FULL)
    # The owner's own connection is the turn's only inference source.
    model_connection = await claim_model_connection(store, claim, capabilities)
    # Read-only, the runner's. The sandbox's GitHub placeholder comes with its residency.
    clone_token = await mint_clone_token(
        store.attempt_commands, claim, app_credentials=app_credentials
    )
    # COMPAT(remove-after=2026-11-30): no host reads the claim's mcp_token, as
    # none since #974 does: each member's CLI reaches the MCP relay with its
    # residency's mcp_credential. Then this attempt-scoped token, its claim
    # field and the relay's SCOPE_MCP branch go together.
    mcp_token = mint_worker_token(**fence, scope=SCOPE_MCP)
    return _Credentials(
        worker_token=worker_token,
        model_connection=model_connection,
        clone_token=clone_token,
        mcp_token=mcp_token,
    )


async def mcp_bindings(
    store: ClaimStorage, claim: dict[str, Any]
) -> tuple[list[str], list[SandboxMcpServer]]:
    """The relay aliases the turn's runtime addresses, and the sandbox's own servers.

    Resolved at every claim, including existing conversations and native
    resumes. Loading the bindings adds newly connected servers while retaining
    explicit opt-outs, independently of whether anyone opened the Tools UI. A
    runtime that cannot use MCP gets neither.
    """
    if claim["runtime"] not in runtimes_supporting_mcp():
        return [], []
    # Relay aliases, and the sandbox servers the host runs itself.
    mcp_aliases, sandbox_mcp_servers = split_bindings(
        await resolve_for_thread(store.mcp, thread_id=str(claim["thread_id"]))
    )
    if web_search.configured():
        # A non-secret marker makes the runtime load the merged MCP endpoint
        # even when this conversation selected no user-owned connections.
        mcp_aliases.append(web_search.RUNTIME_SERVER_ID)
    # The memory tools are served on the same endpoint, to every task.
    mcp_aliases.append(memory_tool.RUNTIME_SERVER_ID)
    return mcp_aliases, sandbox_mcp_servers


async def _task_prompt(
    store: ClaimStorage, claim: dict[str, Any], ssh_state: dict[str, Any] | None
) -> tuple[str, list[dict[str, Any]]]:
    """The task prompt the agent receives, and what each wrapper added to it.

    Attachments are named inside the user task, then the skill selection
    wraps the whole thing: the files are context for the task, and the
    skills are guidance about how to approach it. The goal goes outermost
    of all, and reaches human and automatic turns alike — the turn that set
    the objective is the first one that should be working toward it. The
    goal preamble is the same for every runtime and every turn. What each
    wrapper added is recorded by size for the Context view (never its text).
    """
    # Durable memories are only offered where the memory tools can be reached.
    durable_memories = (
        await claim_agent_memories(store.agent_memory, claim["user_id"], claim["repo"] or None)
        if claim["runtime"] in runtimes_supporting_mcp()
        else None
    )
    return claim_prompt(
        claim["task_prompt"],
        attachments=claim.get("attachments"),
        memories=durable_memories,
        skills=claim.get("skills"),
        ssh_servers=servers_for_thread(ssh_state, str(claim["thread_id"])),
        goal=claim.get("goal"),
        plugins=claim.get("plugins") or [],
    )


__all__ = [
    "ClaimAction",
    "ClaimHandover",
    "ClaimRefusal",
    "ClaimStorage",
    "claim_environment",
    "claim_fence",
    "claim_ssh_access",
    "hand_over_claim",
    "mcp_bindings",
    "require_entitled_repository",
    "require_owner_agent_access",
    "warn_on_unbounded_untrusted_group",
]
