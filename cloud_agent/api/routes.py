"""Agent-sandbox job API (``/v1/agent/jobs``).

Two audiences, two authentication schemes:

- **Owners** (users, via this service's session — the ``HttpOnly`` cookie
  ``/v1/session/login`` sets, or the same token as a bearer): create, inspect,
  list, cancel jobs, browse the retained workspace, and subscribe to the event stream. Every
  read and mutation is scoped to ``user_id`` so one user can never touch
  another's job. The same dependency also requires
  the Gateway's explicit ``agent.use`` permission.
- **Workers** (the sandbox runner, via a per-attempt capability token minted at
  claim time): report events, renew the lease, and drive fenced Agent-turn
  transitions. The token carries the
  ``(job_id, attempt_id, lease_generation)`` fence, so a worker whose lease was
  reaped is rejected by the store even though its token still parses. Those
  rejections surface as **409**, the agreed signal for "you lost the lease,
  stop now".

The SSE endpoint streams the append-only event log with ``Last-Event-ID``
resume. It deliberately sets ``text/event-stream``, which the timeout
middleware recognizes to apply the long stream cap instead of the 120s request
timeout (the #865/#889 lesson: long-lived SSE must not inherit the short cap).
Because that cap is finite by design, the stream is resumable: clients
reconnect with ``Last-Event-ID`` and continue from the global cursor.
"""

from __future__ import annotations

from fastapi import APIRouter

from cloud_agent.api._job_views import (
    _EVENT_PAGE_SIZE,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _SAFE_EVENT_TYPE,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _attempt_duration_ms,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _attempt_resources,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _attempt_summary_response,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _egress_tiers,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _event_response,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _iso,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _job_response,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
from cloud_agent.api._owner_auth import (
    _owned_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
    authenticate_agent_owner,  # noqa: F401 — re-exported: the identity the host namespace kept.
    require_agent_owner,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
from cloud_agent.api._workspace_access import (
    _TERMINAL_ID_PATTERN,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _workspace_broker_for_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _workspace_broker_http_error,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _workspace_id,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _workspace_not_materialized,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _workspace_not_ready,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
from cloud_agent.api.catalog import (
    _raise_session_mutation_error,  # noqa: F401 — re-exported: the identity the host namespace kept.
    archive_agent_sessions,  # noqa: F401 — re-exported: the identity the host namespace kept.
    delete_agent_sessions,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_agent_conversations,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_agent_projects,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_agent_sessions,  # noqa: F401 — re-exported: the identity the host namespace kept.
)

# The owner surface's second batch moved to its own modules (R3-d of #225), and
# with it the last handlers this module declared. What is left is the aggregate:
# the router, the leaves below, and the aliases earlier rounds kept so that
# ``routes.X`` resolves to the identity it always did. R5 deletes the aliases once
# every caller imports the new home.
from cloud_agent.api.config import (
    _mcp_connections_for_user,  # noqa: F401 — re-exported: the identity the host namespace kept.
    get_agent_config,  # noqa: F401 — re-exported: the identity the host namespace kept.
)

# The owner surface's first batch moved to its own modules (R3-c of #225). The
# aggregator keeps binding the same objects, so a test, a ``Depends`` key or an
# importer that still says ``routes.X`` resolves to the identity it always did.
# These aliases exist for the transition; R5 deletes them once every caller
# imports the new home.
from cloud_agent.api.jobs import (
    _job_usage,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _set_agent_thread_archived,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _set_agent_thread_pinned,  # noqa: F401 — re-exported: the identity the host namespace kept.
    archive_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
    cancel_agent_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
    create_agent_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
    get_agent_attempt_telemetry,  # noqa: F401 — re-exported: the identity the host namespace kept.
    get_agent_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_agent_jobs,  # noqa: F401 — re-exported: the identity the host namespace kept.
    pin_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
    rename_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
    restore_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
    unpin_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
from cloud_agent.api.source_control import (
    _connect_github_for_user,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _mcp_integrations,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _oauth_error,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _source_control_providers,  # noqa: F401 — re-exported: the identity the host namespace kept.
    connect_github,  # noqa: F401 — re-exported: the identity the host namespace kept.
    connect_github_integration,  # noqa: F401 — re-exported: the identity the host namespace kept.
    connect_gitlab_integration,  # noqa: F401 — re-exported: the identity the host namespace kept.
    disconnect_github,  # noqa: F401 — re-exported: the identity the host namespace kept.
    disconnect_github_integration,  # noqa: F401 — re-exported: the identity the host namespace kept.
    disconnect_gitlab_integration,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_repo_branches,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_source_control_integrations,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
from cloud_agent.api.threads import (
    _KEEPALIVE_EVERY_N_POLLS,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _POLL_INTERVAL_S,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _SSE_HEADERS,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _sse_frame,  # noqa: F401 — re-exported: the identity the host namespace kept.
    get_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
    get_agent_thread_stats,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_agent_job_events,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_agent_thread_events,  # noqa: F401 — re-exported: the identity the host namespace kept.
    stream_agent_job_events,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
from cloud_agent.api.turns import (
    _sandbox_context_lost_error,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _session_archived_error,  # noqa: F401 — re-exported: the identity the host namespace kept.
    create_agent_follow_up,  # noqa: F401 — re-exported: the identity the host namespace kept.
    fork_agent_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
)

# The worker surface moved to its own module (R3-b of #225). The aggregator
# keeps binding the same objects, so a test, a ``Depends`` key or an importer
# that still says ``routes.X`` resolves to the identity it always did. These
# aliases exist for the transition; R5 deletes them once every caller imports
# the new home.
from cloud_agent.api.worker import (
    _CLONE_SCOPE,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _HOST_TOKEN_HINT,  # noqa: F401 — re-exported: the identity the host namespace kept.
    MAX_TURN_RESOURCES_BODY_BYTES,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _lease_lost,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _match_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _mint_sandbox_grant,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _restore_terminal_suspension,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _turn_summary_conflict,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _worker_claims,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _worker_thread_id,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _worker_workspace_broker,  # noqa: F401 — re-exported: the identity the host namespace kept.
    verify_claiming_host,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_abandon_thread_sandbox,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_admit_thread_sandbox_turn,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_append_event,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_claim,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_establish_shared_sandbox_residency,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_finish,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_heartbeat,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_list_attachments,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_mark_thread_sandbox_running,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_read_attachment,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_record_thread_sandbox_process,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_release,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_report_continuity,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_report_resources,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_report_turn_resources,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_reserve_thread_sandbox,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_resume_terminals,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_settle_thread_sandbox,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_suspend_terminals,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
from cloud_agent.api.workspace import (
    _require_mutable_workspace_ready,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _require_workspace_entitlement,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _terminal_broker_http_error,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _terminal_owner_workspace,  # noqa: F401 — re-exported: the identity the host namespace kept.
    create_agent_job_terminal,  # noqa: F401 — re-exported: the identity the host namespace kept.
    delete_agent_job_terminal,  # noqa: F401 — re-exported: the identity the host namespace kept.
    get_agent_job_git_workspace,  # noqa: F401 — re-exported: the identity the host namespace kept.
    get_agent_job_workspace_file,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_agent_job_terminals,  # noqa: F401 — re-exported: the identity the host namespace kept.
    resize_agent_job_terminal,  # noqa: F401 — re-exported: the identity the host namespace kept.
    run_agent_job_terminal_command,  # noqa: F401 — re-exported: the identity the host namespace kept.
    stream_agent_job_terminal,  # noqa: F401 — re-exported: the identity the host namespace kept.
    write_agent_job_terminal_input,  # noqa: F401 — re-exported: the identity the host namespace kept.
    write_agent_job_workspace_file,  # noqa: F401 — re-exported: the identity the host namespace kept.
)

# The shared helpers moved to their own modules (R3-a, and the flows in R4).
# The aggregator keeps binding the same objects, so a test, a ``Depends`` key or
# an importer that still says ``routes.X`` resolves to the identity it always
# did. These aliases exist for the transition; R5 deletes them once every caller
# imports the new home.
from cloud_agent.application.job_admission import (
    ENV_SANDBOX_MCP_URL,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _binding_alias,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _create_job_as,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _idempotency_conflict,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _JobCreationStorage,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _require_offered_effort,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _require_registered_runtime,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _require_resolvable_model,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _resolve_attachment_selection,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _resolve_mcp_bindings,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _resolve_skill_selection,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
from cloud_agent.application.session_fork import (
    SESSION_FORK_COORDINATOR_LEASE_S,  # noqa: F401 — re-exported: the identity the host namespace kept.
)

router = APIRouter()


# ── Owner endpoints ────────────────────────────────────────────────────


# ── Worker endpoints (capability-token authenticated) ──────────────────


from cloud_agent.api import (
    catalog,
    config,
    jobs,
    source_control,
    threads,
    turns,
    worker,
    workspace,
)

#: The leaf routers this aggregator mounts, in the order the concerns first
#: appear. Each carries ``/v1/agent`` and this router carries none, so FastAPI
#: composes one prefix and not two. Tests walk these rather than
#: ``router.routes``, which holds lazy include wrappers rather than routes.
SURFACE_ROUTERS = (
    config.router,
    source_control.router,
    jobs.router,
    catalog.router,
    turns.router,
    threads.router,
    workspace.router,
    worker.router,
)

for _surface in SURFACE_ROUTERS:
    router.include_router(_surface)
