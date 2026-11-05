"""What every Sandbox Manager router shares: its dependencies and request helpers.

``create_app`` builds one :class:`ManagerContext` and hands it to each router
explicitly. The helpers here are the steps several routes take the same way:
admitting a workspace operation, mapping a supervisor failure to a status, and
naming a member's terminal namespace.
"""

from __future__ import annotations

import contextlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import Header, HTTPException

from cloud_agent_host.chat_sandbox import ChatSandboxSpec, RetainedChatLost, SandboxRestoreFailed
from cloud_agent_host.operation_call import closed
from cloud_agent_host.retained_workspace import RetainedWorkspaceError
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost
from cloud_agent_host.shared_session_state import SharedMemberManifest

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import ThreadPoolExecutor

    from cloud_agent_host.chat_sandbox import ChatSandboxManager, RetainedChatBackend
    from cloud_agent_host.environment_preparation import EnvironmentPreparation
    from cloud_agent_host.manager_api.calls import _CommandSlots, _EndpointLifecycleCalls
    from cloud_agent_host.manager_api.models import SharedSandboxIdentityRequest
    from cloud_agent_host.manager_api.terminal_manager import _TerminalManager
    from cloud_agent_host.operation_call import SandboxManagerCall
    from cloud_agent_host.sandbox_archive_runtime import SandboxArchiveController
    from cloud_agent_host.sandbox_archives import SandboxArchiveService
    from cloud_agent_host.shared_session_state import SharedSessionDescriptor
    from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor
    from cloud_agent_shared.sandbox_manager_wire import SharedMember


def authenticator(expected_token: str | None) -> Callable[..., None]:
    """The dependency every route runs first: the caller holds the shared token."""

    def authenticate(x_agent_workspace_token: str | None = Header(None)) -> None:
        # Bytes, not str: compare_digest raises TypeError for a non-ASCII str,
        # so any caller could turn this refusal into a 500.
        if (
            not expected_token
            or not x_agent_workspace_token
            or not secrets.compare_digest(expected_token.encode(), x_agent_workspace_token.encode())
        ):
            raise HTTPException(status_code=401, detail="invalid sandbox manager credential")

    return authenticate


def supervisor_error(exc: Exception, *, safe_detail: str | None = None) -> HTTPException:
    """The status a supervisor failure answers with, and its detail."""
    if isinstance(exc, SandboxRestoreFailed):
        # The chat is intact in its checkpoint; the caller hands the turn back.
        return HTTPException(
            status_code=424,
            detail=safe_detail if safe_detail is not None else str(exc)[:300],
        )
    if isinstance(exc, RetainedChatLost):
        return HTTPException(
            status_code=410,
            detail=safe_detail if safe_detail is not None else str(exc),
        )
    if isinstance(exc, SessionBusy):
        return HTTPException(
            status_code=409,
            detail=safe_detail if safe_detail is not None else str(exc),
        )
    if isinstance(exc, SessionLeaseLost):
        return HTTPException(
            status_code=409,
            detail=safe_detail if safe_detail is not None else str(exc),
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=422,
            detail=safe_detail if safe_detail is not None else str(exc),
        )
    return HTTPException(
        status_code=503,
        detail=safe_detail if safe_detail is not None else str(exc)[:300],
    )


def shared_spec(body: SharedSandboxIdentityRequest) -> ChatSandboxSpec:
    """The retained-container spec a group identity names."""
    return ChatSandboxSpec(
        thread_id=body.origin_thread_id,
        sandbox_id=body.sandbox_id,
        generation=body.generation,
        runtime_id=body.runtime_id,
        runtime_version=body.runtime_version,
        sandbox_image_digest=body.sandbox_image_digest,
        memory_limit=body.memory_limit,
        cpu_limit=body.cpu_limit,
        disk_limit_mib=body.disk_limit_mib,
        stop_timeout_s=body.stop_timeout_s,
        shared_live=True,
        resident_slot_capacity=body.resident_slot_capacity,
    )


def shared_manifest(members: list[SharedMember]) -> list[SharedMemberManifest]:
    """The supervisor's manifest of a group's members."""
    return [SharedMemberManifest(**member.model_dump()) for member in members]


class SupervisorStart:
    """Start the shared supervisor's reconciliation once, after its archive policy is known."""

    def __init__(self, supervisor: SharedChatSessionSupervisor) -> None:
        self._supervisor = supervisor
        self._lock = threading.Lock()
        self._ready = threading.Event()

    def __call__(self) -> None:
        """Start reconciliation unless it has started already."""
        with self._lock:
            if self._ready.is_set():
                return
            self._supervisor.start()
            self._ready.set()

    @property
    def ready(self) -> bool:
        """Whether reconciliation has started."""
        return self._ready.is_set()


@dataclass
class ManagerSettings:
    """The runtime limits the sandbox manager applies, and its own local values.

    ``local`` is fixed at startup from the environment; ``applied`` is replaced
    whole by every authenticated settings push, and each request reads it anew.
    """

    local: dict[str, float]
    applied: dict[str, float] = field(init=False)

    def __post_init__(self) -> None:
        self.applied = dict(self.local)


@dataclass
class ManagerContext:
    """The dependencies one sandbox manager app's routers share."""

    supervisor: SharedChatSessionSupervisor
    supervisor_start: SupervisorStart
    archive_service: SandboxArchiveService | None
    archive_controller: SandboxArchiveController | None
    retained_manager: ChatSandboxManager | None
    terminals: _TerminalManager
    endpoint_calls: _EndpointLifecycleCalls
    command_pool: ThreadPoolExecutor
    command_slots: _CommandSlots
    environment_preparation: EnvironmentPreparation
    settings: ManagerSettings
    authenticate: Callable[..., None]
    archive_boundary: SandboxArchiveService | SandboxArchiveController | None = field(init=False)

    def __post_init__(self) -> None:
        self.archive_boundary = self.archive_service or self.archive_controller

    def require_shared_supervisor(self) -> SharedChatSessionSupervisor:
        """The supervisor, once a dynamic archive policy has let it start."""
        if self.archive_controller is not None and not self.supervisor_start.ready:
            raise HTTPException(
                status_code=503,
                detail="sandbox archive policy has not been confirmed",
            )
        return self.supervisor

    def retained_owner(self, workspace_id: str) -> SharedChatSessionSupervisor:
        """Resolve the only supported workspace shape: a shared chat member."""
        owner = self.require_shared_supervisor()
        if not workspace_id.startswith("athr_") or not owner.owns(workspace_id):
            raise HTTPException(status_code=404, detail="shared chat workspace is not materialized")
        return owner

    def retained_operation(
        self,
        workspace_id: str,
        operation: Callable[[RetainedChatBackend, SandboxManagerCall], dict[str, Any]],
        *,
        call: SandboxManagerCall | None = None,
        start_seq: int | None = None,
    ) -> dict[str, Any]:
        """Open a workspace, run one operation, and answer its measurement call.

        ``call`` is this request's measurement fragment, if the inherited
        selection named one. It gates nothing: the workspace is admitted, which
        proves its network or stands on a recent proof, and the operation runs
        exactly as before. Only the timings are conditional, and the answer is
        merged into the body so a caller can tell a producer that collected
        nothing from one that never reported at all.
        """
        active = call if call is not None else closed()
        owner = self.retained_owner(workspace_id)
        token: str | None = None
        try:
            # Admission proves the group network for this operation, or stands
            # on a proof under a minute old (shared_admission_proof). A command
            # the operation spawns stands on that proof rather than making
            # another; its clock is the proof's, never later than this one.
            admitted_from = time.monotonic()
            with active.stage("admission"):
                token, _ = owner.operation_opened(workspace_id, start_seq=start_seq)
            retained = owner.backend(workspace_id, network_proved_at=admitted_from)
            return active.answer(operation(retained, active))
        except RetainedWorkspaceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        finally:
            if token is not None:
                with contextlib.suppress(SandboxError):
                    owner.operation_closed(workspace_id, token)

    async def owner_command(
        self,
        workspace_id: str,
        operation: Callable[[RetainedChatBackend, SandboxManagerCall], dict[str, Any]],
        *,
        start_seq: int | None = None,
    ) -> dict[str, Any]:
        """Run an owner's workspace download or Git inspection on the command pool."""
        give_back = self.command_slots.take(workspace_id)

        def run() -> dict[str, Any]:
            try:
                return self.retained_operation(workspace_id, operation, start_seq=start_seq)
            finally:
                give_back()

        try:
            return await self.endpoint_calls.run_on(self.command_pool, run)
        except HTTPException:
            # Refused before the pool ran it. A command that ran gave its slot
            # back already, and a second give-back changes nothing.
            give_back()
            raise

    def ensure_workspace(self, workspace_id: str) -> SharedSessionDescriptor:
        """Validate a member alias and return its shared-group descriptor."""
        owner = self.retained_owner(workspace_id)
        try:
            return owner.status(workspace_id)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc

    def terminal_scope(self, workspace_id: str) -> str:
        """Map any member URL onto the one terminal namespace for its group.

        The public/control-plane route stays chat-scoped so ownership is still
        checked against the addressed member.  The shell processes themselves
        operate on the group's single ``/workspace`` as the group UID, though,
        so capacity and suspension must be group-scoped too.  Otherwise a fork
        sibling could keep writing through a PTY while another member had
        installed the documented workspace preparation barrier.
        """
        return str(self.ensure_workspace(workspace_id).origin_thread_id)

    def keystroke_scope(self, workspace_id: str) -> str:
        """The same namespace for a terminal's input and resize, from memory.

        Both reach only a terminal already open there, and the browser sends
        one per keystroke: inspecting the container for each would put a
        Docker CLI process in front of every echo. A container that died, or
        that its group lost, has already ended its terminals with EOF, and
        they refuse the keystroke themselves.
        """
        owner = self.retained_owner(workspace_id)
        try:
            # The group's origin chat names the namespace and is fixed with the
            # group: the member's backend carries it in its spec, from memory.
            return owner.backend(workspace_id).spec.thread_id
        except SandboxError as exc:
            raise supervisor_error(exc) from exc


__all__ = [
    "ManagerContext",
    "ManagerSettings",
    "SupervisorStart",
    "authenticator",
    "shared_manifest",
    "shared_spec",
    "supervisor_error",
]
