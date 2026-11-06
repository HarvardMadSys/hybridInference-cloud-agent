"""A workspace operation's admission may stand on its group's recent network proof.

Every workspace operation (a file listing or preview, a save, a download, a
Git inspection) is admitted by the supervisor, and admission proves the group
network: it reconciles the bridge and its relays and rewrites the Kata guest's
relay names and resolver with a guest-root exec. On a running container that
is most of what a file read costs, and the browser asks for many in a row.

A turn's commands already stand on one proof for :data:`TURN_NETWORK_PROOF_S`
(:meth:`RetainedChatBackend.require_network`). An operation's admission stands
on one the same way, within limits that keep it exactly as strong:

* only a proof an admission of this group made, in this lifecycle generation:
  a stop, a lifecycle check, an archive or a lost container moves the
  generation on and the next admission proves again;
* only for the exact container it proved, still running, not restarted, on
  exactly the bridge object it was proved on, with no proof of it failed
  since and no start of it unproven
  (:func:`~cloud_agent_host.container.chat_sandbox_exec.standing_network_proof`);
* only while the host guard can still show its rules, which it checks again
  once its own verification lapses;
* and every other admission check (membership, the lifecycle fences, the
  start fence and the group's identity fence) runs exactly as before.

The container is inspected afresh for every admission, so a stopped one is
started and proved as before. Terminal admissions, preparations and turns do
not stand on an earlier proof.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from cloud_agent_host.container.chat_sandbox import RetainedChatLost
from cloud_agent_host.container.chat_sandbox_exec import standing_network_proof
from cloud_agent_host.container.retained_backend import TURN_NETWORK_PROOF_S
from cloud_agent_host.supervisor.group_lifecycle import Operation

if TYPE_CHECKING:
    from cloud_agent_host.supervisor.group_lifecycle_controller import GroupLifecycleController
    from cloud_agent_host.supervisor.shared_session_state import _Group, _SameRun


def admit_running(
    lifecycle: GroupLifecycleController,
    group: _Group,
    *,
    start_seq: int | _SameRun | None,
    reuse: bool,
) -> float:
    """Start the group if stopped and prove its network, or stand on a proof that holds.

    ``reuse`` lets a workspace operation stand on the group's standing proof.
    Returns when the proof the admission stands on began (monotonic), and
    records it for the group's lifecycle generation either way.
    """
    with lifecycle.state.lock:
        generation = group.life.generation
        recorded = group.network_proof
        reuse = reuse and not group.life.last_start_failed
    if reuse and recorded is not None and recorded[0] == generation:
        standing = _standing_proof(lifecycle, group, generation)
        if standing is not None:
            return standing
    proved_from = time.monotonic()
    lifecycle.ensure_running(group, start_seq=start_seq)
    with lifecycle.state.lock:
        group.network_proof = (generation, proved_from)
    return proved_from


def _standing_proof(
    lifecycle: GroupLifecycleController, group: _Group, generation: int
) -> float | None:
    """When the proof the running container may still be admitted on began, if any."""
    current = lifecycle.state.manager.inspect(group.spec.container_name)
    if current is None:
        return None
    proved_from = standing_network_proof(
        lifecycle.state.manager, group.spec, current, max_age_s=TURN_NETWORK_PROOF_S
    )
    if proved_from is None:
        return None
    with lifecycle.state.lock:
        if group.life.generation != generation or group.life.last_start_failed:
            return None
        # The group's identity fence: a different container is lost, as on a start.
        lifecycle.state.accept_inspection_locked(group, current)
        group.network_proof = (generation, proved_from)
    return proved_from


def ensure_running(
    lifecycle: GroupLifecycleController, group: _Group, *, start_seq: int | _SameRun | None
) -> None:
    """Start the group if it is stopped, and prove its network for this request."""
    current = lifecycle.state.manager.inspect(group.spec.container_name)
    if current is None:
        raise RetainedChatLost("shared sandbox container is missing")
    with lifecycle.state.lock:
        group.lifecycle.admit(Operation.RUN)
    # The start runs Docker without the lock and revalidates under it.
    inspection = lifecycle.start_and_prove(group, start_seq=start_seq)
    with lifecycle.state.lock:
        group.inspection = inspection


def backend_proved_at(group: _Group, network_proved_at: float | None) -> float | None:
    """The proof time an admitted operation's backend stands on: never after its admission's.

    An admission that stood on an earlier proof hands its commands that
    proof's age, so they prove again a minute after it, not after the request.
    """
    recorded = group.network_proof
    if network_proved_at is None or recorded is None:
        return network_proved_at
    return min(network_proved_at, recorded[1])


__all__ = ["admit_running", "backend_proved_at", "ensure_running"]
