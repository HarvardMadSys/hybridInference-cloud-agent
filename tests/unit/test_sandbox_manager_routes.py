"""The sandbox manager's route table, frozen from before its routers were split out.

``create_app`` used to declare every route in one closure; the routes now live
in one module per topic under ``cloud_agent_host.manager_api``. The split was
meant to change nothing a caller can see, so this pins what the closure served:
each route's method and path, its handler's name, the response model FastAPI
derives from it, its body model and query parameters, the token check that runs
before any of them, and the order of the patterns that can match one URL.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from cloud_agent_host.container.sandbox import ProcessBackend
from cloud_agent_host.sandbox_manager import create_app

_ANY = "dict[str, typing.Any]"

#: (method, path, handler, response model, body model, query parameters), in
#: the order the closure registered them.
ROUTES: tuple[tuple[str, str, str, str, str | None, tuple[str, ...]], ...] = (
    # Added on dev by #1110 (sandbox incidents), after this list was frozen.
    ("POST", "/sandbox-incidents/confirm", "confirm_incidents", "dict[str, int]", "dict", ()),
    ("POST", "/profiling-policy", "profiling_policy", _ANY, "dict", ()),
    ("POST", "/host-settings", "host_settings", _ANY, "dict", ()),
    (
        "POST",
        "/sandbox-archive-settings",
        "sandbox_archive_settings",
        "dict[str, object]",
        "dict",
        (),
    ),
    ("GET", "/health", "health", _ANY, None, ()),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/acquire",
        "acquire_shared_chat_sandbox",
        _ANY,
        "SharedSandboxAcquireRequest",
        (),
    ),
    (
        "POST",
        "/environment-snapshots/capture",
        "capture_installation_snapshot",
        _ANY,
        "EnvironmentSnapshotCaptureRequest",
        (),
    ),
    ("POST", "/environment-snapshots/images", "environment_snapshot_images", _ANY, None, ()),
    (
        "POST",
        "/environment-snapshots/delete",
        "delete_environment_snapshots",
        _ANY,
        "EnvironmentSnapshotDeleteRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/register",
        "register_shared_chat_sandbox",
        _ANY,
        "SharedSandboxIdentityRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/archive-initial/status",
        "shared_initial_archive_status",
        _ANY,
        "SharedInitialArchiveStatusRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/archive-initial",
        "capture_shared_initial_archive",
        _ANY,
        "SharedInitialArchiveRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/resident",
        "ensure_shared_resident",
        _ANY,
        "SharedResidentLaunchRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/turn",
        "run_shared_turn",
        "None",
        "SharedTurnRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/interrupt",
        "interrupt_shared_turn",
        _ANY,
        "SharedLeaseRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/input",
        "inject_shared_input",
        _ANY,
        "SharedInputRequest",
        (),
    ),
    (
        "GET",
        "/shared-sandboxes/{thread_id}/questions",
        "pending_shared_questions",
        _ANY,
        None,
        ("owner",),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/questions",
        "answer_shared_question",
        _ANY,
        "SharedQuestionAnswerRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/residency-lost",
        "shared_residency_lost",
        _ANY,
        "SharedResidencyLostRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/settle",
        "settle_shared_chat_sandbox",
        _ANY,
        "SharedSettleRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/abandon",
        "abandon_shared_chat_sandbox",
        _ANY,
        "SharedAbandonRequest",
        (),
    ),
    ("GET", "/shared-sandboxes/{thread_id}", "shared_chat_sandbox_status", _ANY, None, ()),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/stop-idle",
        "stop_idle_shared_chat_sandbox",
        _ANY,
        "SharedIdleStopRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/admin-delete/prepare",
        "prepare_sandbox_archive",
        _ANY,
        "SandboxArchivePrepareRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/admin-delete/upload",
        "upload_sandbox_archive",
        _ANY,
        "SandboxArchiveUploadRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/admin-delete/commit",
        "commit_sandbox_archive_deletion",
        _ANY,
        "SandboxArchiveCommitRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/admin-delete/discard",
        "discard_sandbox_without_archive",
        _ANY,
        "SandboxArchiveDiscardRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/admin-delete/abort",
        "abort_sandbox_archive",
        _ANY,
        "SandboxArchivePrepareRequest",
        (),
    ),
    (
        "POST",
        "/shared-sandboxes/{thread_id}/fork",
        "fork_shared_chat_session",
        _ANY,
        "SharedForkRequest",
        (),
    ),
    ("GET", "/sandboxes", "list_chat_sandboxes", _ANY, None, ("thread_id",)),
    ("GET", "/workspaces/{workspace_id}/files", "files", _ANY, None, ("path",)),
    (
        "PUT",
        "/workspaces/{workspace_id}/files",
        "write_file",
        _ANY,
        "FileWriteRequest",
        ("path",),
    ),
    ("GET", "/workspaces/{workspace_id}/archive", "workspace_archive", "None", None, ()),
    (
        "GET",
        "/workspaces/{workspace_id}/environment/status",
        "environment_status",
        _ANY,
        None,
        ("binding_id",),
    ),
    (
        "POST",
        "/workspaces/{workspace_id}/environment/cancel",
        "environment_cancel",
        "dict[str, bool]",
        None,
        ("activation_owner",),
    ),
    (
        "POST",
        "/workspaces/{workspace_id}/environment/prepare",
        "prepare_environment",
        _ANY,
        "EnvironmentPrepareRequest",
        (),
    ),
    (
        "PUT",
        "/workspaces/{workspace_id}/ssh-access",
        "sync_ssh_access",
        _ANY,
        "SshAccessRequest",
        (),
    ),
    (
        "POST",
        "/workspaces/{workspace_id}/terminals",
        "create_terminal",
        _ANY,
        "TerminalCreateRequest",
        (),
    ),
    (
        "GET",
        "/workspaces/{workspace_id}/terminals/{terminal_id}/output",
        "terminal_output",
        _ANY,
        None,
        (),
    ),
    (
        "POST",
        "/workspaces/{workspace_id}/terminals/suspend",
        "suspend_terminals",
        _ANY,
        "TerminalCoordinationRequest",
        (),
    ),
    (
        "POST",
        "/workspaces/{workspace_id}/terminals/resume",
        "resume_terminals",
        _ANY,
        "TerminalCoordinationRequest",
        (),
    ),
    (
        "POST",
        "/workspaces/{workspace_id}/terminals/resume-settled",
        "resume_settled_terminals",
        _ANY,
        "TerminalCoordinationRequest",
        (),
    ),
    ("GET", "/workspaces/{workspace_id}/terminals", "list_terminals", _ANY, None, ()),
    (
        "GET",
        "/workspaces/{workspace_id}/terminals/{terminal_id}/stream",
        "stream_terminal",
        "None",
        None,
        ("after",),
    ),
    (
        "POST",
        "/workspaces/{workspace_id}/terminals/{terminal_id}/input",
        "terminal_input",
        _ANY,
        "TerminalInputRequest",
        (),
    ),
    (
        "POST",
        "/workspaces/{workspace_id}/terminals/{terminal_id}/resize",
        "resize_terminal",
        _ANY,
        "TerminalResizeRequest",
        (),
    ),
    (
        "DELETE",
        "/workspaces/{workspace_id}/terminals/{terminal_id}",
        "delete_terminal",
        _ANY,
        None,
        (),
    ),
    ("GET", "/workspaces/{workspace_id}/git", "git", _ANY, None, ("base_sha", "metadata")),
)


class _Supervisor:
    """Enough of a supervisor to build the app; no route here reaches it."""

    archive_policy = None
    idle_timeout_s = 30.0


def _routes() -> list[APIRoute]:
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox-manager-secret",
        shared_session_supervisor=_Supervisor(),  # type: ignore[arg-type]
    )
    return [route for route in app.routes if isinstance(route, APIRoute)]


def _body_model(route: APIRoute) -> str | None:
    if route.body_field is None:
        return None
    annotation: Any = route.body_field.field_info.annotation
    return getattr(annotation, "__name__", repr(annotation))


def _served(route: APIRoute) -> tuple[str, str, str, str, str | None, tuple[str, ...]]:
    (method,) = route.methods
    return (
        method,
        route.path,
        route.name,
        str(route.response_model),
        _body_model(route),
        tuple(param.name for param in route.dependant.query_params),
    )


def test_the_app_serves_exactly_the_frozen_routes() -> None:
    served = [_served(route) for route in _routes()]

    assert len(served) == len(set(served))
    assert set(served) == set(ROUTES)


def test_every_route_checks_the_token_before_anything_else() -> None:
    for route in _routes():
        dependencies = route.dependant.dependencies
        assert [getattr(d.call, "__name__", None) for d in dependencies] == ["authenticate"]
        assert [p.alias for p in dependencies[0].header_params] == ["x-agent-workspace-token"]


def _pattern(path: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$")


def _sample(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "sample", path)


def test_patterns_that_can_match_one_url_keep_their_order() -> None:
    """Starlette answers a wrong method with the first partial match's ``Allow``."""
    served = [(method, path) for method, path, *_ in map(_served, _routes())]
    frozen = [(method, path) for method, path, *_ in ROUTES]
    for index, earlier in enumerate(frozen):
        for later in frozen[index + 1 :]:
            if _pattern(earlier[1]).match(_sample(later[1])) or _pattern(later[1]).match(
                _sample(earlier[1])
            ):
                assert served.index(earlier) < served.index(later), (earlier, later)


@pytest.mark.asyncio
async def test_a_caller_without_the_token_is_refused_on_every_route() -> None:
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox-manager-secret",
        shared_session_supervisor=_Supervisor(),  # type: ignore[arg-type]
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        for method, path, *_ in ROUTES:
            url = re.sub(r"\{[^}]+\}", "athr_source", path)
            for headers in ({}, {"X-Agent-Workspace-Token": "wrong"}):
                response = await client.request(method, url, headers=headers)
                assert response.status_code == 401, (method, path, response.text)
                assert response.json() == {"detail": "invalid sandbox manager credential"}
