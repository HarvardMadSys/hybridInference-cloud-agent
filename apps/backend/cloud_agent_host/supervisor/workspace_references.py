"""Terminal and workspace-operation references: each keeps its group awake until it closes.

Opening one admits it like any workspace mutation and proves the group network,
or stands on a proof that still holds
(:func:`~cloud_agent_host.supervisor.shared_admission_proof.admit_running`).
"""

from __future__ import annotations

import logging
import math
import secrets
from typing import TYPE_CHECKING

from cloud_agent_host.container.chat_sandbox import (
    RetainedChatBackend,
)
from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.session_lifecycle import (
    SessionLeaseLost,
)
from cloud_agent_host.supervisor.group_lifecycle import Operation
from cloud_agent_host.supervisor.shared_admission_proof import admit_running, backend_proved_at

if TYPE_CHECKING:
    from cloud_agent_host.supervisor.group_lifecycle_controller import GroupLifecycleController
    from cloud_agent_host.supervisor.shared_session_state import (
        SharedSessionDescriptor,
        SharedSessionState,
        _Group,
        _Member,
    )

# The supervisor's operational log names are stable: this component logs
# under the supervisor's logger, as it did before it was split out.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")


class WorkspaceReferences:
    """Open and close the references that keep a group running."""

    def __init__(self, state: SharedSessionState, *, lifecycle: GroupLifecycleController) -> None:
        self._state = state
        self._manager = state.manager
        self._lifecycle = lifecycle

    def operation_opened(
        self, thread_id: str, *, start_seq: int | None = None
    ) -> tuple[str, SharedSessionDescriptor]:
        """Reference-count one shared workspace operation without serializing it.

        ``start_seq`` is the fence the control plane took for this request.
        """
        return self._open_reference(thread_id, start_seq=start_seq, terminal=False)

    def terminal_opened(
        self, thread_id: str, *, start_seq: int | None = None
    ) -> tuple[str, SharedSessionDescriptor]:
        """Reference-count one member-owned PTY against the shared group.

        ``start_seq`` is the fence the control plane took for this request.
        """
        return self._open_reference(thread_id, start_seq=start_seq, terminal=True)

    def _open_reference(
        self, thread_id: str, *, start_seq: int | None, terminal: bool
    ) -> tuple[str, SharedSessionDescriptor]:
        """Admit one terminal or workspace operation; its token keeps the group awake.

        Admission proves the group network, or an operation's stands on a proof
        that still holds (:func:`admit_running`); the caller runs on that proof.
        """
        kind = "terminal" if terminal else "operation"
        with self._state.lock:
            group, member = self._state.member(thread_id)
        self._lifecycle.recover_start_archive_completion(group)
        with self._state.lock:
            self._state.wait_for_start_locked(group, retry_failed=True)
            current_group, current_member = self._state.member(thread_id)
            if current_group is not group or current_member is not member:
                raise SessionLeaseLost(f"shared membership changed before {kind} admission")
            group.lifecycle.admit(Operation.MUTATION)
            lifecycle_generation = group.lifecycle_generation
        admit_running(self._lifecycle, group, start_seq=start_seq, reuse=not terminal)
        with self._state.lock:
            self._state.wait_for_start_locked(group)
            current_group, current_member = self._state.member(thread_id)
            if (
                current_group is not group
                or current_member is not member
                or group.lifecycle_generation != lifecycle_generation
            ):
                raise SessionLeaseLost(f"shared sandbox lifecycle changed during {kind} admission")
            group.lifecycle.admit(Operation.MUTATION)
            self._state.join_run_locked(group, start_seq)
            token = secrets.token_urlsafe(24)
            tokens = group.terminal_tokens if terminal else group.operation_tokens
            tokens[token] = thread_id
            group.idle_deadline = None
            return token, self._state.descriptor(group, member)

    def terminal_closed(
        self,
        thread_id: str,
        token: str,
        *,
        last_activity_monotonic: float | None = None,
    ) -> SharedSessionDescriptor | None:
        """Release one PTY and refresh the group's single activity clock.

        Returns ``None`` for an opener that is no longer a member.
        """
        with self._state.lock:
            group, member = self._token_opener_locked(thread_id, token, terminal=True)
            if group.terminal_tokens.get(token) != thread_id:
                raise SessionLeaseLost("shared terminal lifecycle token is stale")
            now = self._state.clock()
            activity_at = now
            if last_activity_monotonic is not None:
                activity_at = float(last_activity_monotonic)
                if activity_at < 0 or not math.isfinite(activity_at):
                    raise ValueError("terminal last activity timestamp is invalid")
                activity_at = min(activity_at, now)
            # Validation comes first: a malformed close request is retryable
            # and must not consume the reference that keeps this group awake.
            group.terminal_tokens.pop(token, None)
            self._state.arm_idle_if_quiescent(group, last_activity=activity_at)
            return None if member is None else self._state.descriptor(group, member)

    def operation_closed(self, thread_id: str, token: str) -> SharedSessionDescriptor | None:
        """Release one workspace operation and refresh the group activity clock.

        Returns ``None`` for an opener that is no longer a member.
        """
        with self._state.lock:
            group, member = self._token_opener_locked(thread_id, token, terminal=False)
            if group.operation_tokens.get(token) != thread_id:
                raise SessionLeaseLost("shared workspace operation token is stale")
            group.operation_tokens.pop(token, None)
            self._state.arm_idle_if_quiescent(group)
            return None if member is None else self._state.descriptor(group, member)

    def _token_opener_locked(
        self, thread_id: str, token: str, *, terminal: bool
    ) -> tuple[_Group, _Member | None]:
        """Find the group a terminal or operation token was opened in, and its opener.

        The token is what keeps its group awake, and only its own close can
        release it. A later manifest can drop the opener while the token is
        open -- a deleted chat whose resident slot a Fork reclaimed -- so an
        opener that is no longer a member is found through its token instead.
        """
        group = self._state.members.get(thread_id)
        if group is not None:
            return group, group.members[thread_id]
        for candidate in self._state.groups.values():
            tokens = candidate.terminal_tokens if terminal else candidate.operation_tokens
            if tokens.get(token) == thread_id:
                return candidate, None
        raise SandboxError(f"chat {thread_id!r} has no shared sandbox membership")

    def backend(
        self, thread_id: str, *, network_proved_at: float | None = None
    ) -> RetainedChatBackend:
        """Return the group's one backend; its commands may stand on an admission's proof."""
        with self._state.lock:
            group, _ = self._state.member(thread_id)
            proved_at = backend_proved_at(group, network_proved_at)
            return RetainedChatBackend(self._manager, group.spec, network_proved_at=proved_at)


__all__ = [
    "WorkspaceReferences",
]
