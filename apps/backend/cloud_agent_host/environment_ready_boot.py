"""Answer an environment boot that is already prepared, without a start or a proof.

Every turn with an environment binding asks the supervisor to prepare it, and
so does every terminal, action and Fork that requires it. For a running group
whose receipt already says this exact boot is prepared, the full path starts
nothing, but still proves the group network and, under Kata, rewrites the
guest's network files, all to return that receipt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent_host.container.chat_sandbox import RetainedChatBackend
from cloud_agent_host.environment_preparation import prepared_in
from cloud_agent_host.group_lifecycle import Operation
from cloud_agent_host.session_lifecycle import SessionBusy

if TYPE_CHECKING:
    from cloud_agent_host.shared_session_state import SharedSessionState


def ready_boot(
    state: SharedSessionState,
    thread_id: str,
    environment: Any,
    preparer: Any,
    activation_owner: str | None,
) -> dict[str, Any] | None:
    """This exact boot's ready receipt, or ``None`` to prepare as before.

    A ready receipt runs nothing in the guest, and whatever runs there next
    proves the network itself: a turn's CLI after the runner's own proof, a
    terminal or an action at its admission, a Fork at its own start. So this
    answer needs neither the start nor the proof of the full path. It needs
    the group's exact container, running the boot the receipt prepared, read
    after the receipt, while the group's own inspection, the member, its
    activation's lease and the group's lifecycle are the ones the reads began
    under. A start finishing meanwhile replaces that inspection without a new
    lifecycle generation, and its boot is then the one to prepare. Pending
    lifecycle work, or anything unreadable, takes the full path, which then
    answers or fails as it always has.
    """
    with state.lock:
        group, member = state.member(thread_id)
        observed = group.inspection
        generation = group.lifecycle_generation
        if not observed.running or not _admitted(group):
            return None
    try:
        backend = RetainedChatBackend(state.manager, group.spec)
        receipt = preparer.status(backend, environment.binding_id)
        # Docker is asked only once the receipt could answer.
        if not receipt.get("ready") or receipt.get("container_id") != observed.container_id:
            return None
        current = state.manager.inspect(group.spec.container_name)
    except Exception:
        return None
    with state.lock:
        if (
            current is None
            or current.container_id != observed.container_id
            or not prepared_in(receipt, current)
            or group.inspection != observed
            or state.members.get(thread_id) is not group
            or group.members.get(thread_id) is not member
            or group.lifecycle_generation != generation
            or (activation_owner is not None and member.lease_token != activation_owner)
            or not _admitted(group)
        ):
            return None
        state.accept_inspection_locked(group, current)
    return receipt


def _admitted(group: Any) -> bool:
    """Whether the group is still its container's, started cleanly, and open to mutations."""
    if group.container_lost or group.start_failed:
        return False
    try:
        group.lifecycle.admit(Operation.MUTATION)
    except SessionBusy:
        return False
    return True
