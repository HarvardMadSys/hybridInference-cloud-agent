"""Owner-facing view construction for the agent surface.

Store rows become API models here and nowhere else: the job response, its
attempt summaries, the per-attempt resource rollup, the event envelope and the
deployment's egress posture. Split out of the aggregator so the job, thread,
catalog, workspace and config routers share one definition of what a job looks
like to its owner.

``_EVENT_PAGE_SIZE`` and ``_SAFE_EVENT_TYPE`` travel with the event envelope:
``service_jobs`` pages the same events this module renders, and the aggregator's
SSE writer validates event names against the same pattern.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cloud_agent.domain.execution import SUCCEEDED
from cloud_agent.schemas import (
    BASE_REF_METADATA_KEY,
    EVENT_TYPE_PATTERN,
    AgentAttemptSummary,
    AgentJobEvent,
    AgentJobResponse,
    AttemptResources,
    McpBindingSummary,
    ProcessObservation,
    ToolCallResources,
)
from cloud_agent_contracts.egress import (
    EgressPolicyError,
    build_policy_from_env as build_egress_policy,
)
from cloud_agent_git.patch_gate import branch_name_for

if TYPE_CHECKING:
    from typing import Any


_EVENT_PAGE_SIZE = 500


# The schema constrains event_type on the way in, but the SSE writer must be
# safe on its own: rows written before that constraint existed (or by any
# future non-HTTP writer) must not be able to break out of the ``event:`` field
# and inject frames into the owner's stream.
_SAFE_EVENT_TYPE = re.compile(EVENT_TYPE_PATTERN)


def _iso(value: Any) -> str | None:
    """Render a timestamp column as ISO-8601 (or None)."""
    return value.isoformat() if value is not None else None


def _egress_tiers() -> tuple[str | None, str | None]:
    """Report the deployment's per-phase egress posture, if it has one."""
    try:
        policy = build_egress_policy()
    except EgressPolicyError:
        # A misconfigured policy is the runner's problem to refuse at preflight,
        # not a reason to fail an owner reading their own job.
        return None, None
    return policy.tier_for("setup").value, policy.tier_for("agent").value


def _attempt_duration_ms(telemetry: dict[str, Any] | None) -> int | None:
    """How long the selected attempt ran, or None while it still is."""
    if not telemetry:
        return None
    started, finished = telemetry.get("started_at"), telemetry.get("finished_at")
    if started is None or finished is None:
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _attempt_resources(telemetry: dict[str, Any] | None) -> AttemptResources | None:
    """Shape the resource rollup, or None when no accounting record was reported.

    A row whose counters are all null is still returned: it carries
    ``coverage`` and is how a reader learns the turn was not measured, which
    is a different statement from a turn that used nothing.
    """
    if not telemetry or telemetry.get("sampler_version") is None:
        return None
    return AttemptResources(
        cpu_usec=telemetry.get("cpu_usec"),
        mem_peak_bytes=telemetry.get("mem_peak_bytes"),
        mem_avg_bytes=telemetry.get("mem_avg_bytes"),
        io_read_bytes=telemetry.get("io_read_bytes"),
        io_write_bytes=telemetry.get("io_write_bytes"),
        net_rx_bytes=telemetry.get("net_rx_bytes"),
        net_tx_bytes=telemetry.get("net_tx_bytes"),
        sample_count=telemetry.get("sample_count") or 0,
        coverage=float(telemetry.get("coverage") or 0.0),
        sampler_version=telemetry.get("sampler_version"),
        tool_time_ms=telemetry.get("tool_time_ms"),
        tool_calls=[ToolCallResources(**call) for call in telemetry.get("tool_calls") or []],
        tool_calls_truncated=bool(telemetry.get("tool_calls_truncated")),
        process_observer=telemetry.get("process_observer"),
        process_observation_complete=bool(telemetry.get("process_observation_complete")),
        process_events_lost=int(telemetry.get("process_events_lost") or 0),
        processes_truncated=bool(telemetry.get("processes_truncated")),
        processes=[ProcessObservation(**process) for process in telemetry.get("processes") or []],
    )


def _attempt_summary_response(telemetry: dict[str, Any]) -> AgentAttemptSummary:
    """Shape one attempt without losing its identity at the API boundary."""
    return AgentAttemptSummary(
        attempt_id=telemetry["attempt_id"],
        attempt_no=telemetry["attempt_no"],
        continuity_mode=telemetry.get("continuity_mode"),
        context_status=telemetry.get("context_status"),
        duration_ms=_attempt_duration_ms(telemetry),
    )


def _job_response(
    job: dict[str, Any],
    usage: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    attempt_telemetry: list[dict[str, Any]] | None = None,
) -> AgentJobResponse:
    """Shape a store job row for the owner-facing API."""
    usage = usage or {}
    setup_tier, agent_tier = _egress_tiers()
    metadata = dict(job.get("metadata") or {})
    base_ref = metadata.pop(BASE_REF_METADATA_KEY, None)
    if not isinstance(base_ref, str):
        base_ref = None
    sandbox = job.get("thread_sandbox") or {}
    native_fork_available = bool(
        sandbox.get("shared_live") is True
        and sandbox.get("native_fork_capable") is True
        and sandbox.get("native_state") == "ready"
        and sandbox.get("fork_operation_id") is None
        and sandbox.get("active_attempt_id") is None
        and bool(sandbox.get("last_settled_job_id"))
        and job.get("state") == SUCCEEDED
        and isinstance(job.get("native_turn_no"), int)
        and not isinstance(job.get("native_turn_no"), bool)
        and int(job["native_turn_no"]) > 0
    )
    return AgentJobResponse(
        id=job["id"],
        thread_id=job.get("thread_id"),
        parent_job_id=job.get("parent_job_id"),
        turn_no=job.get("turn_no") or 1,
        repo=job["repo"],
        task_prompt=job["task_prompt"],
        title=job.get("title"),
        title_source=job.get("title_source"),
        runtime=job["runtime"],
        model=job["model"],
        execution_profile=job.get("execution_profile", "trusted-v1"),
        reasoning_effort=job.get("reasoning_effort"),
        base_ref=base_ref,
        base_sha=job["base_sha"],
        output_branch=branch_name_for(
            sandbox.get("origin_thread_id") or job.get("thread_id") or job["id"]
        ),
        state=job["state"],
        conversation_state=job.get("conversation_state"),
        cancel_requested=job["cancel_requested"],
        current_attempt_id=job["current_attempt_id"],
        published_pr_url=job["published_pr_url"],
        published_commit_sha=job.get("published_commit_sha"),
        detail=job["detail"],
        forked_from_job_id=job.get("fork_source_job_id"),
        metadata=metadata or None,
        mcp_bindings=[McpBindingSummary(**entry) for entry in (job.get("mcp_bindings") or [])],
        skills=job.get("skills") or [],
        attachments=job.get("attachments") or [],
        created_at=_iso(job["created_at"]),
        updated_at=_iso(job["updated_at"]),
        pinned_at=_iso(job.get("pinned_at")),
        archived_at=_iso(job.get("archived_at")),
        spent_usd=usage.get("spent_usd"),
        tokens_in=usage.get("tokens_in"),
        tokens_out=usage.get("tokens_out"),
        model_calls=usage.get("model_calls"),
        setup_egress_tier=setup_tier,
        agent_egress_tier=agent_tier,
        telemetry_attempt_id=(telemetry or {}).get("attempt_id"),
        continuity_mode=(telemetry or {}).get("continuity_mode"),
        context_status=(telemetry or {}).get("context_status"),
        resident_state=sandbox.get("resident_state"),
        sandbox_id=sandbox.get("sandbox_id"),
        workspace_shared=bool(sandbox.get("shared_live")),
        native_state=sandbox.get("native_state"),
        fork_anchor_job_id=sandbox.get("fork_anchor_job_id"),
        native_fork_available=native_fork_available,
        workspace_publication_state=sandbox.get("workspace_publication_state"),
        workspace_epoch=sandbox.get("workspace_epoch"),
        started_at=_iso((telemetry or {}).get("started_at")),
        finished_at=_iso((telemetry or {}).get("finished_at")),
        duration_ms=_attempt_duration_ms(telemetry),
        resources=_attempt_resources(telemetry),
        attempt_telemetry=[
            _attempt_summary_response(attempt) for attempt in (attempt_telemetry or [])
        ],
    )


def _event_response(event: dict[str, Any]) -> AgentJobEvent:
    """Shape a store event row for the API."""
    return AgentJobEvent(
        id=event["id"],
        attempt_id=event["attempt_id"],
        seq=event["seq"],
        event_type=event["event_type"],
        payload=event["payload"],
        created_at=_iso(event["created_at"]),
    )
