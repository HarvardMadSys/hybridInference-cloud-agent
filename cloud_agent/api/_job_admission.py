"""Admission checks a submitted job or follow-up passes before it is written.

Model resolution, runtime registration, reasoning-effort availability, skill
selection, MCP binding resolution and the idempotency fence: the rules that
decide whether a submission is accepted at all. The create-job and follow-up
entry points share them, which is why they have one home rather than one copy
per router module.

This is a transitional home. The design leaves the question of which of these
rules become an application-layer flow to R4; until then this module is the
admission seam and holds no routing of its own.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from fastapi import HTTPException

from cloud_agent.domain.requests import request_fingerprint
from cloud_agent.entitlement import RepoNotAllowed, require_entitled_repo
from cloud_agent.execution_profiles import (
    RESERVED_EXECUTION_METADATA_KEYS,
    default_execution_profile,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.binding_aliases import binding_aliases
from cloud_agent.mcp.models import ALIAS_PATTERN, ToolPolicy
from cloud_agent.mcp.registry import resolve_connection
from cloud_agent.schemas import (
    BASE_REF_METADATA_KEY,
    PLATFORM_REASONING_EFFORTS,
    RESET_REASONING_EFFORT,
)
from cloud_agent.skills.models import SkillError
from cloud_agent.skills.selection import resolve_skills
from cloud_agent.visible_models import (
    agent_canonical_model,
    agent_model_reasoning_efforts,
    agent_visible_models,
)
from cloud_agent_host.runtimes import (
    registered_runtimes,
    runtime_reasoning_efforts,
    runtimes_supporting_mcp,
)

if TYPE_CHECKING:
    from typing import Any

    from cloud_agent.ports import (
        ConversationCommands,
        McpConnectionRepository,
        SkillRepository,
        SourceControlRepository as SourceControlPort,
    )
    from cloud_agent.schemas import AgentJobCreate


#: The moved helpers keep logging under the aggregator's logger name: the
#: deployment's log pipeline selects on ``cloud_agent.api.routes``, and a
#: relocation must not move this module's records to a logger nobody watches.
logger = get_logger("cloud_agent.api.routes")


class _JobCreationStorage(Protocol):
    """What creating a job reaches for: the write surface, and MCP resolution."""

    conversation_commands: ConversationCommands
    mcp: McpConnectionRepository
    skills: SkillRepository
    source_control: SourceControlPort


#: The MCP relay's address **as a sandbox can reach it** — a host-local relay,
#: not this process's own bind address, which is why it is configured rather
#: than derived. Unset means this deployment offers no MCP: the claim carries
#: no address, and a runtime asked for servers anyway refuses rather than
#: resolving them against the inference gateway, which does not answer for MCP.
ENV_SANDBOX_MCP_URL = "AGENT_SANDBOX_MCP_URL"


async def _require_resolvable_model(
    model: str,
    *,
    gateway: Any,
    user: dict[str, Any],
) -> str:
    """Resolve the requested model to its canonical id, or refuse the job.

    Fail-fast on a model the job's first call would 404 on: rejecting here
    costs a 400, while accepting costs a claim, a clone and an attempt before
    the sandbox's first model call dies with an error that reads like a broken
    model.

    **Returns the canonical id, and the caller stores that.** Validating an
    alias and then recording the alias was the bug this signature exists to
    prevent: the gateway mints a grant against canonical ids, so a job carrying
    `claude-3-opus-latest` produced a grant whose `allowed_models` matched
    nothing. The job was created, and died at its first inference call with a
    404 from a model that plainly exists.
    """
    canonical = await agent_canonical_model(model, gateway, user_id=user["user_id"])
    if canonical is not None:
        return canonical
    visible = await agent_visible_models(gateway, user_id=user["user_id"])
    raise HTTPException(
        status_code=400,
        detail={
            "error": {
                "type": "model_not_available",
                "message": (
                    f"Model {model!r} is not available to agent jobs on this deployment. "
                    f"Available: {', '.join(visible[:20]) or 'none'}."
                ),
            }
        },
    )


async def _resolve_mcp_bindings(
    requested: list[Any] | None, *, runtime: str, store: McpConnectionRepository, user_id: str
) -> list[dict[str, Any]]:
    """Settle which MCP connections a new session may use, or refuse to create it.

    Resolved against the *creating user's* connections. That is the signature
    this whole feature turns on: a connection somebody else added is unknown
    here, and there is no deployment-wide list to fall back from.

    Omitted choices inherit the owner's connected servers at claim time with
    confirmation required. Entries supplied here are explicit conversation
    overrides and remain authoritative when defaults are synchronized.

    Every failure is a refusal rather than a silent drop: a job is submitted
    because someone wants a task done, and a task written around a tool the
    agent turns out not to have does not fail -- it quietly produces the wrong
    work, having spent the budget.
    """
    if not requested:
        return []
    if runtime not in runtimes_supporting_mcp():
        supported = ", ".join(runtimes_supporting_mcp()) or "none"
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "invalid_request",
                    "message": (
                        f"the {runtime!r} runtime cannot be given MCP connections on this "
                        f"deployment. Runtimes that can: {supported}."
                    ),
                }
            },
        )
    prepared: list[dict[str, Any]] = []
    aliases: list[tuple[int, str | None, str]] = []
    for entry in requested:
        connection_id = int(getattr(entry, "connection_id", 0) or 0)
        row = await store.get_mcp_connection(connection_id=connection_id, user_id=user_id)
        if row is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request",
                        "message": f"you have no MCP connection with id {connection_id}.",
                    }
                },
            )
        resolved = await resolve_connection(store, row=row)
        if not resolved.usable:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request",
                        "message": (
                            f"the {resolved.connection.label!r} MCP connection cannot be "
                            f"used: {resolved.error}"
                        ),
                    }
                },
            )
        snapshot = resolved.snapshot
        available = {capability.name for capability in (snapshot.tools if snapshot else ())}
        selected = {
            str(name): str(policy) for name, policy in (getattr(entry, "tools", None) or {}).items()
        }
        unknown = sorted(set(selected) - available)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request",
                        "message": (
                            f"{resolved.connection.label!r} does not offer: "
                            f"{', '.join(unknown)}. Run a connection test to refresh what "
                            "it advertises."
                        ),
                    }
                },
            )
        rules: dict[str, Any] = {}
        for name, policy in selected.items():
            try:
                parsed = ToolPolicy(policy)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": {
                            "type": "invalid_request",
                            "message": (
                                f"unknown tool policy {policy!r}; use deny, confirm or allow."
                            ),
                        }
                    },
                ) from exc
            capability = next(
                (item for item in (snapshot.tools if snapshot else ()) if item.name == name),
                None,
            )
            rules[name] = {
                "policy": parsed.value,
                "fingerprint": capability.fingerprint() if capability else "",
            }
        try:
            other_rules = {
                kind: {
                    str(name): ToolPolicy(str(policy)).value
                    for name, policy in (getattr(entry, kind, None) or {}).items()
                }
                for kind in ("resources", "prompts")
            }
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request",
                        "message": "Unknown MCP capability policy; use deny, confirm or allow.",
                    }
                },
            ) from exc
        has_selection = any(rule["policy"] != "deny" for rule in rules.values()) or any(
            policy != "deny" for policies in other_rules.values() for policy in policies.values()
        )
        prepared.append(
            {
                "connection_id": connection_id,
                "alias": "",
                "enabled": bool(getattr(entry, "enabled", True)) and has_selection,
                "config_revision": resolved.connection.config_revision,
                "authorization_epoch": resolved.connection.authorization_epoch,
                "capability_revision": resolved.connection.capability_revision,
                "policy_revision": 1,
                "rules": rules,
                "resource_rules": other_rules["resources"],
                "prompt_rules": other_rules["prompts"],
            }
        )
        aliases.append((connection_id, getattr(entry, "alias", None), _binding_alias(row)))
    try:
        for binding, alias in zip(prepared, binding_aliases(aliases), strict=True):
            binding["alias"] = alias
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"type": "invalid_request", "message": str(exc)}},
        ) from exc
    return prepared


def _binding_alias(row: dict[str, Any]) -> str:
    """A stable, displayable prefix for one connection's tool names.

    Derived from the template when there is one, and from the connection id
    otherwise. Never from the label: a label is free text the owner can edit,
    and an alias that changed under a rename would look to a running session
    like its tools had disappeared.
    """
    template = str(row.get("template") or "").strip()
    if template and re.fullmatch(ALIAS_PATTERN, template):
        return template
    return f"mcp{row['id']}"


def _require_registered_runtime(runtime: str) -> None:
    """400 unless ``runtime`` names a registered harness.

    The registry is the trust boundary (design D2): what may run is what this
    deployment registered, never whatever string a request carried.
    """
    known = registered_runtimes()
    if runtime not in known:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "invalid_request",
                    "message": (
                        f"unknown runtime {runtime!r}; this deployment registers: "
                        + ", ".join(known)
                    ),
                }
            },
        )


async def _require_offered_effort(
    effort: str | None,
    *,
    runtime: str,
    model: str,
    gateway: Any,
    user: dict[str, Any],
) -> str | None:
    """400 unless ``effort`` is one this runtime *and* this model both accept.

    The offered set is the intersection, and it is checked rather than clamped.
    Clamping looks kinder and is worse: glm-5.3 has no ``medium``, so a
    ``medium`` quietly served as ``high`` would bill and behave as a depth
    nobody chose while reporting the one they did — the same lie as a flag that
    resolves to nothing, which is what this whole control exists to end.

    Args:
        effort: The requested level, or ``None`` for Auto.
        runtime: The harness this turn will run on.
        model: The canonical model id, already resolved.
        gateway: Client for the gateway's internal contracts.
        user: The authenticated owner.

    Returns:
        The value to store: ``None`` for Auto, else the validated level.

    Raises:
        HTTPException: 400 when the pair does not offer this value.
    """
    if effort is None or effort == RESET_REASONING_EFFORT:
        # The sentinel means unset wherever it appears. On a follow-up it also
        # has to be told apart from "inherit" (that caller checks it before
        # getting here); on a create there is nothing to inherit, so the two
        # spellings are the same answer and neither is a 400.
        return None
    runtime_levels = runtime_reasoning_efforts().get(runtime, [])
    # The model's entry is read as a yes/no — does it take the parameter at
    # all — and not for its contents. Per-model domains were tried and
    # withdrawn: the providers' documented lists disagreed with what their
    # APIs accept, so the levels are this platform's and the same everywhere.
    takes_effort = bool(
        (await agent_model_reasoning_efforts(gateway, user_id=user["user_id"])).get(model)
    )
    offered = (
        [level for level in PLATFORM_REASONING_EFFORTS if level in runtime_levels]
        if takes_effort
        else []
    )
    if effort in offered:
        return effort
    raise HTTPException(
        status_code=400,
        detail={
            "error": {
                "type": "reasoning_effort_not_available",
                "message": (
                    f"Reasoning effort {effort!r} is not available for {runtime!r} "
                    f"with {model!r}. Available: {', '.join(offered) or 'none'}."
                ),
            }
        },
    )


async def _resolve_skill_selection(
    store: SkillRepository, *, user_id: str, skill_ids: list[int] | None
) -> list[dict[str, Any]] | None:
    """Resolve owned instruction snapshots before persisting a submission."""
    try:
        return await resolve_skills(store, user_id=user_id, skill_ids=skill_ids)
    except SkillError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"type": "invalid_skill_selection", "message": str(exc)}},
        ) from exc


async def _create_job_as(
    body: AgentJobCreate,
    user: dict[str, Any],
    *,
    job_store: _JobCreationStorage,
    app_credentials: Any | None,
    gateway: Any,
    reserved_metadata: dict[str, Any] | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Create one job for ``user``, shared by the owner and service doors.

    Extracted so the on-call service surface (``api/service_jobs.py``) creates
    jobs through exactly the checks the owner route runs — model resolution,
    repo entitlement, ref pinning — rather than through a parallel path that
    drifts. ``reserved_metadata`` is stamped *after* caller metadata for the
    same reason ``BASE_REF_METADATA_KEY`` is popped first: a display fact the
    platform asserts must not be spoofable by the request body.
    """
    # A resend is answered before the checks below, every one of which can
    # change between two sends of one submission: a model can be retired, an
    # entitlement revoked, a tool server disconnected. They held when the first
    # send created the job, and running them again on the resend is how a lost
    # response became a 4xx instead of the job it had already made.
    fingerprint = (
        request_fingerprint(
            "create",
            body.model_dump(mode="json"),
        )
        if client_request_id is not None
        else None
    )
    if client_request_id is not None:
        replayed = await job_store.conversation_commands.replayed_submission(
            user_id=user["user_id"],
            client_request_id=client_request_id,
            fingerprint=fingerprint,
        )
        if replayed is not None:
            if replayed.get("error") == "client_request_conflict":
                raise _idempotency_conflict()
            return replayed
    # Fail here, not at claim time. An unregistered runtime used to travel all
    # the way into a queued row and burn a claim before anything noticed —
    # the composer only offers registered ids, so an arbitrary string can only
    # arrive from a direct API caller, and it deserves a 400, not a dead job.
    # The check lives in the helper rather than in the owner route because the
    # service door creates jobs through here too: a check only one door runs
    # is precisely the drift this extraction exists to prevent.
    _require_registered_runtime(body.runtime)
    # Canonical from here on. The grant the gateway mints names canonical
    # ids, so a job that stored the alias would carry a scope matching
    # nothing — created successfully, dead at its first inference call.
    model = await _require_resolvable_model(body.model, gateway=gateway, user=user)
    # Checked against the *resolved* model, not the requested spelling: the
    # domain belongs to the model the turn will actually call.
    reasoning_effort = await _require_offered_effort(
        body.reasoning_effort, runtime=body.runtime, model=model, gateway=gateway, user=user
    )
    # The requester chooses the repository and the platform later mints a real
    # installation token for it. Without this the two combine into a confused
    # deputy: name any repository the App reaches, and read it back through
    # your own job's events and patch.
    try:
        await require_entitled_repo(
            body.repo,
            user["user_id"],
            store=job_store.source_control,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc
    # A branch is resolved to the commit it points at *now*. The job stores the
    # sha: a branch moves, so a job that recorded "dev" would silently mean a
    # different tree by the time it ran, and the publisher applies its patch
    # onto a pinned commit.
    base_sha = body.base_sha
    if base_sha is None and body.base_ref and app_credentials is not None:
        try:
            base_sha = await app_credentials.resolve_ref(body.repo, body.base_ref)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request",
                        "message": f"could not resolve {body.base_ref!r} in {body.repo}: {exc}",
                    }
                },
            ) from exc

    metadata = dict(body.metadata or {})
    # Reserved display fact: caller metadata must not be able to claim the
    # job was pinned from a different branch than the one the API resolved.
    metadata.pop(BASE_REF_METADATA_KEY, None)
    # Execution policy and its evidence are server-owned facts. Opaque
    # metadata must not be able to manufacture a contradictory audit trail.
    for key in RESERVED_EXECUTION_METADATA_KEYS:
        metadata.pop(key, None)
    if body.base_ref:
        metadata[BASE_REF_METADATA_KEY] = body.base_ref
    for key, value in (reserved_metadata or {}).items():
        metadata[key] = value
    # Shared-live is enforced globally at claim time, not represented as an
    # optional per-job requirement. Job metadata therefore remains caller data
    # plus server-owned execution facts, without a hidden rollout switch.
    mcp_bindings = await _resolve_mcp_bindings(
        body.mcp_bindings, runtime=body.runtime, store=job_store.mcp, user_id=user["user_id"]
    )
    try:
        execution_profile = default_execution_profile().value
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": {"type": "invalid_configuration", "message": str(exc)}},
        ) from exc
    skills = await _resolve_skill_selection(
        job_store.skills, user_id=user["user_id"], skill_ids=body.skill_ids
    )
    job = await job_store.conversation_commands.create_job(
        user_id=user["user_id"],
        repo=body.repo,
        task_prompt=body.task_prompt,
        runtime=body.runtime,
        model=model,
        execution_profile=execution_profile,
        reasoning_effort=reasoning_effort,
        base_sha=base_sha,
        setup_script=body.setup_script,
        metadata=metadata or None,
        mcp_bindings=mcp_bindings,
        **({"skills": skills} if skills is not None else {}),
        client_request_id=client_request_id,
        # Over the request as sent, not as resolved: a branch that moved
        # between two sends of one submission is still one submission. The
        # same value the replay lookup above used, so the pre-check and the
        # unique index cannot disagree about what was sent.
        client_request_fingerprint=fingerprint,
    )
    if job.get("error") == "client_request_conflict":
        raise _idempotency_conflict()
    logger.info(
        "agent_job_created",
        extra={"event": "agent_job_created", "job_id": job["id"], "runtime": body.runtime},
    )
    return job


def _idempotency_conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "idempotency_conflict",
                "message": "This Idempotency-Key was already used for a different submission.",
            }
        },
    )
