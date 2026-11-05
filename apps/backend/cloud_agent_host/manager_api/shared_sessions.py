"""The shared chat sessions' lifecycle: leases, residents, questions, Fork and listing."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Query

from cloud_agent_host.drivers import DriverError
from cloud_agent_host.manager_api.context import shared_manifest, shared_spec, supervisor_error
from cloud_agent_host.manager_api.models import (  # noqa: TC001 — FastAPI resolves the request models.
    SharedAbandonRequest,
    SharedForkRequest,
    SharedIdleStopRequest,
    SharedInputRequest,
    SharedLeaseRequest,
    SharedQuestionAnswerRequest,
    SharedResidencyLostRequest,
    SharedResidentLaunchRequest,
    SharedSandboxAcquireRequest,
    SharedSandboxIdentityRequest,
    SharedSettleRequest,
)
from cloud_agent_host.manager_api.routing import route
from cloud_agent_host.resident_launch_config import resident_launch
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_shared.sandbox_manager_wire import member_manifest

if TYPE_CHECKING:
    from starlette.datastructures import State

    from cloud_agent_host.archive.sandbox_checkpoints import SandboxCheckpointStore
    from cloud_agent_host.manager_api.context import ManagerContext


class SharedSessionRoutes:
    """One member's lease and resident process, and the group's observation."""

    def __init__(self, context: ManagerContext, state: State) -> None:
        self._context = context
        self._state = state

    @route("POST", "/shared-sandboxes/{thread_id}/acquire")
    async def acquire_shared_chat_sandbox(
        self, thread_id: str, body: SharedSandboxAcquireRequest
    ) -> dict[str, Any]:
        """Lease one member while preserving sibling concurrency."""
        owner = self._context.require_shared_supervisor()
        try:
            lease = await self._context.endpoint_calls.run(
                owner.acquire,
                shared_spec(body),
                thread_id=thread_id,
                owner=body.owner,
                activation_generation=body.activation_generation,
                has_settled_context=body.has_settled_context,
                manifest=shared_manifest(body.members),
                expected_container_id=body.expected_container_id,
                profiling=body.profiling,
                start_seq=body.start_seq,
                restore=None if body.restore is None else body.restore.require_supported(),
                **(
                    {"environment_snapshot": body.environment_snapshot}
                    if body.environment_snapshot is not None
                    else {}
                ),
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(lease)

    @route("POST", "/shared-sandboxes/{thread_id}/register")
    async def register_shared_chat_sandbox(
        self, thread_id: str, body: SharedSandboxIdentityRequest
    ) -> dict[str, Any]:
        """Restore DB-owned member aliases without waking the container."""
        owner = self._context.require_shared_supervisor()
        if thread_id != body.origin_thread_id:
            raise HTTPException(status_code=422, detail="shared registration must name the origin")
        try:
            descriptors = await self._context.endpoint_calls.run(
                owner.register_manifest,
                shared_spec(body),
                shared_manifest(body.members),
                expected_container_id=body.expected_container_id,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return {
            "ok": True,
            "sandbox_id": body.sandbox_id,
            "member_count": len(descriptors),
        }

    @route("POST", "/shared-sandboxes/{thread_id}/resident")
    async def ensure_shared_resident(
        self, thread_id: str, body: SharedResidentLaunchRequest
    ) -> dict[str, Any]:
        """Open or reuse one member's resident process."""
        owner = self._context.require_shared_supervisor()
        try:
            descriptor = await self._context.endpoint_calls.run(
                owner.ensure_resident,
                thread_id,
                body.token,
                resident_launch(thread_id, body.launch),
                profiling=body.profiling,
            )
        except (DriverError, SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @route("POST", "/shared-sandboxes/{thread_id}/interrupt")
    async def interrupt_shared_turn(
        self, thread_id: str, body: SharedLeaseRequest
    ) -> dict[str, Any]:
        """Interrupt one exact member turn."""
        owner = self._context.require_shared_supervisor()
        try:
            await self._context.endpoint_calls.run(owner.interrupt, thread_id, body.token)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return {"ok": True}

    @route("POST", "/shared-sandboxes/{thread_id}/input")
    async def inject_shared_input(self, thread_id: str, body: SharedInputRequest) -> dict[str, Any]:
        """Give the member's turn a message for the model's next step, or say why not.

        Accepted means the turn has it; the turn's own stream reports where the
        model got it. A refusal is an answer, not an error: the message stays
        waiting on the control plane and goes when the turn ends.
        """
        supervisor = self._context.require_shared_supervisor()
        try:
            outcome = await self._context.endpoint_calls.run(
                supervisor.inject_input,
                thread_id,
                owner=body.owner,
                message_id=body.message_id,
                text=body.text,
            )
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return {"accepted": outcome.accepted, "reason": outcome.reason}

    @route("GET", "/shared-sandboxes/{thread_id}/questions")
    async def pending_shared_questions(
        self,
        thread_id: str,
        owner: str = Query(..., min_length=1, max_length=128),
    ) -> dict[str, Any]:
        """List native questions raised by the named running attempt."""
        supervisor = self._context.require_shared_supervisor()
        try:
            questions = await self._context.endpoint_calls.run(
                supervisor.pending_questions, thread_id, owner=owner
            )
        except (SandboxError, DriverError) as exc:
            raise supervisor_error(exc) from exc
        return {"questions": questions}

    @route("POST", "/shared-sandboxes/{thread_id}/questions")
    async def answer_shared_question(
        self, thread_id: str, body: SharedQuestionAnswerRequest
    ) -> dict[str, Any]:
        """Deliver a response only to the native request that raised it."""
        supervisor = self._context.require_shared_supervisor()
        try:
            outcome = await self._context.endpoint_calls.run(
                supervisor.answer_question,
                thread_id,
                owner=body.owner,
                request_id=body.request_id,
                answers=body.answers,
                reject=body.reject,
            )
        except (SandboxError, DriverError) as exc:
            raise supervisor_error(exc) from exc
        return {"accepted": outcome.accepted, "reason": outcome.reason}

    @route("POST", "/shared-sandboxes/{thread_id}/residency-lost")
    async def shared_residency_lost(
        self, thread_id: str, body: SharedResidencyLostRequest
    ) -> dict[str, Any]:
        """Close the exact member process whose renewable grant was rejected."""
        owner = self._context.require_shared_supervisor()
        try:
            descriptor = await self._context.endpoint_calls.run(
                owner.residency_lost,
                thread_id,
                generation=body.generation,
            )
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @route("POST", "/shared-sandboxes/{thread_id}/settle")
    async def settle_shared_chat_sandbox(
        self, thread_id: str, body: SharedSettleRequest
    ) -> dict[str, Any]:
        """Release a successful member activation while keeping its process."""
        owner = self._context.require_shared_supervisor()
        try:
            descriptor = await self._context.endpoint_calls.run(
                owner.settle,
                thread_id,
                body.token,
                job_id=body.job_id,
                context_status=body.context_status,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @route("POST", "/shared-sandboxes/{thread_id}/abandon")
    async def abandon_shared_chat_sandbox(
        self, thread_id: str, body: SharedAbandonRequest
    ) -> dict[str, Any]:
        """Release one failed member without stopping healthy siblings."""
        owner = self._context.require_shared_supervisor()
        try:
            descriptor = await self._context.endpoint_calls.run(
                owner.abandon,
                thread_id,
                body.token,
                lost=body.lost,
            )
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @route("GET", "/shared-sandboxes/{thread_id}")
    async def shared_chat_sandbox_status(self, thread_id: str) -> dict[str, Any]:
        """Describe one member and its group."""
        owner = self._context.require_shared_supervisor()
        try:
            descriptor = await self._context.endpoint_calls.run(owner.status, thread_id)
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @route("POST", "/shared-sandboxes/{thread_id}/stop-idle")
    async def stop_idle_shared_chat_sandbox(
        self, thread_id: str, body: SharedIdleStopRequest
    ) -> dict[str, Any]:
        """Apply a group- and activation-fenced stop."""
        owner = self._context.require_shared_supervisor()
        try:
            descriptor = await self._context.endpoint_calls.run(
                owner.stop_if_idle,
                thread_id,
                generation=body.generation,
                activation_generation=body.activation_generation,
                reason=body.reason,
                member_activation_generations=body.member_activation_generations,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(descriptor)

    @route("POST", "/shared-sandboxes/{thread_id}/fork")
    async def fork_shared_chat_session(
        self, thread_id: str, body: SharedForkRequest
    ) -> dict[str, Any]:
        """Fork one member's native session into a new member of the same group."""
        owner = self._context.require_shared_supervisor()
        calls = self._context.endpoint_calls
        try:
            # The target already has a durable slot reservation, but Fork
            # itself installs its native identity. Reconciling that placeholder
            # first would occupy the identity with None, or overwrite a child
            # recovered after a lost receipt. Keep local live-child ownership
            # intact so fork_native can recover it under the existing fences.
            source_members = [
                member for member in body.members if member.thread_id != body.target_thread_id
            ]
            await calls.run(
                owner.register_manifest,
                shared_spec(body),
                shared_manifest(source_members),
                expected_container_id=body.expected_container_id,
            )
            result = await calls.run(
                owner.fork_native,
                thread_id,
                start_seq=body.start_seq,
                operation_id=body.operation_id,
                source_native_session_id=body.source_native_session_id,
                source_native_turn_no=body.source_native_turn_no,
                target_thread_id=body.target_thread_id,
                target_slot_id=body.target_slot_id,
                source_launch=resident_launch(thread_id, body.source_launch),
                target_launch=resident_launch(body.target_thread_id, body.target_launch),
            )
        except (DriverError, SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc
        return asdict(result)

    @route("GET", "/sandboxes")
    async def list_chat_sandboxes(
        self,
        thread_id: str | None = Query(None, min_length=1, max_length=256),
    ) -> dict[str, Any]:
        """Return Docker-observed state for all groups or one member's group.

        The full listing is a complete observation, not an ownership mutation. The
        control plane keeps the durable thread/container mapping and accepts
        only exact identity matches; the sandbox manager supplies the fact it alone
        can see: whether each retained Docker container is currently running.
        Reconciliation also makes a sandbox manager restart self-healing because it
        rebuilds the supervisor's in-memory index from Docker labels first.
        A member query inspects only its known group without host reconciliation.
        """
        try:
            owner = self._context.require_shared_supervisor()
            shared_descriptors = (
                await self._context.endpoint_calls.run(owner.list, background_recovery=True)
                if thread_id is None
                else await self._context.endpoint_calls.run(owner.list_for_thread, thread_id)
            )
        except SandboxError as exc:
            raise supervisor_error(exc) from exc
        rows: list[dict[str, Any]] = []
        shared_groups: dict[str, list[dict[str, Any]]] = {}
        for descriptor in shared_descriptors:
            shared_groups.setdefault(descriptor.sandbox_id, []).append(asdict(descriptor))
        for members in shared_groups.values():
            origin = next(
                (row for row in members if row["thread_id"] == row["origin_thread_id"]),
                members[0],
            )
            origin = dict(origin)
            # Descriptors count each member's PTYs; the heartbeat observes the
            # whole shared container, including terminals opened by a fork.
            origin["terminal_count"] = sum(row["terminal_count"] for row in members)
            origin["shared_live"] = True
            origin["members"] = member_manifest(members)
            rows.append(origin)
        resource_collector = self._state.sandbox_resource_collector
        if resource_collector is not None and thread_id is None:
            # The listing already proved each group's labels and exact ID.
            # Synchronization and cache reads perform no Docker stats I/O.
            # A partial listing must not evict unrelated collection targets.
            resource_collector.sync_targets(rows)
        self._context.incidents.annotate(rows, shared_groups, complete=thread_id is None)
        checkpoints: SandboxCheckpointStore | None = self._state.sandbox_checkpoint_store
        for row in rows:
            row["resource_usage"] = (
                None
                if resource_collector is None or not row["running"]
                else resource_collector.latest(row["container_id"])
            )
            # The latest checkpoint repeats on every beat; the control plane
            # records it idempotently, so a lost beat needs no retry state.
            receipt = None if checkpoints is None else checkpoints.latest(row["sandbox_id"])
            row["checkpoint"] = None if receipt is None else receipt.as_dict()
        return {"sandboxes": rows}


__all__ = ["SharedSessionRoutes"]
