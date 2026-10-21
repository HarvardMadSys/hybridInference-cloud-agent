"""Both senders describe a retained group the way the Sandbox Manager reads it."""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_agent.application import session_fork
from cloud_agent.sandbox_manager_client import SandboxManagerClient, identity_from_row
from cloud_agent_host.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.sandbox_manager import SharedForkRequest, SharedSandboxIdentityRequest
from cloud_agent_host.session_supervisor_client import _group_identity
from cloud_agent_host.shared_session_supervisor import SharedMemberManifest
from cloud_agent_shared.sandbox_manager_wire import MEMBER_FIELDS, member_manifest

#: One member as the control plane's sandbox read shape carries it, with the
#: fields only the control plane uses.
_DURABLE_MEMBER = {
    "thread_id": "athr_origin",
    "native_session_id": "native-origin",
    "native_state": "ready",
    "context_status": "live",
    "activation_generation": 3,
    "process_generation": 2,
    "residency_generation": 5,
    "resident_state": "live",
    "resident_slot_id": 0,
    "active_attempt_id": 41,
    "admitted_job_id": "ajob_admitted",
    "admitted_at": None,
    "last_settled_job_id": "ajob_settled",
    "forked_from_thread_id": None,
    "fork_anchor_job_id": None,
}

#: The group as the control plane reads it.
_ROW = {
    "sandbox_id": "asbx_shared",
    "generation": 2,
    "origin_thread_id": "athr_origin",
    "runtime_id": "codex",
    "runtime_version": "0.145.0",
    "sandbox_image_digest": "sha256:image",
    "container_id": "container-shared",
    "resident_slot_capacity": 8,
    "memory_limit": "4g",
    "cpu_limit": "2",
    "disk_limit_mib": 16384,
    "stop_timeout_seconds": 10,
    "start_seq": 7,
    "group_members": [_DURABLE_MEMBER],
}


def test_the_supervisor_keeps_exactly_the_wire_member_fields():
    assert tuple(field.name for field in fields(SharedMemberManifest)) == MEMBER_FIELDS


def test_a_manifest_carries_only_the_wire_fields():
    (member,) = member_manifest([_DURABLE_MEMBER])
    assert tuple(member) == MEMBER_FIELDS
    assert member["residency_generation"] == 5
    # A field the record lacks is left to the receiver's default.
    (partial,) = member_manifest([{"thread_id": "athr_origin", "resident_slot_id": 0}])
    assert partial == {"thread_id": "athr_origin", "resident_slot_id": 0}


def test_the_control_planes_identity_is_one_the_sandbox_manager_accepts():
    identity = identity_from_row(_ROW, members=member_manifest(_ROW["group_members"]))

    request = SharedSandboxIdentityRequest.model_validate(identity)

    assert request.expected_container_id == "container-shared"
    assert request.stop_timeout_s == 10
    assert request.start_seq == 7
    assert request.members[0].model_dump() == member_manifest([_DURABLE_MEMBER])[0]


def test_the_runners_identity_is_one_the_sandbox_manager_accepts():
    spec = ChatSandboxSpec(
        thread_id="athr_origin",
        sandbox_id="asbx_shared",
        generation=2,
        runtime_id="codex",
        runtime_version="0.145.0",
        sandbox_image_digest="sha256:image",
        disk_limit_mib=16384,
        shared_live=True,
        resident_slot_capacity=8,
    )
    identity = _group_identity(
        spec,
        members=member_manifest([_DURABLE_MEMBER]),
        expected_container_id="container-shared",
    )

    request = SharedSandboxIdentityRequest.model_validate(identity)

    assert request.origin_thread_id == "athr_origin"
    assert request.disk_limit_mib == 16384
    assert request.members[0].thread_id == "athr_origin"


#: The Fork's source as the store reads it now. The group has moved on from
#: the generation and container the Fork recorded.
_THREAD_SANDBOX = {
    **_ROW,
    "thread_id": "athr_origin",
    "generation": 7,
    "container_id": "container-current",
    "resident_slot_id": 0,
}

#: The Fork's own ``agent_session_forks`` row. Its ``generation`` counts the
#: row and starts at 1; the group generation it recorded is ``sandbox_generation``.
_FORK_ROW = {
    "id": "asfork_1",
    "generation": 1,
    "sandbox_id": "asbx_shared",
    "sandbox_generation": 5,
    "container_id": "container-recorded",
    "runtime_id": "codex",
    "runtime_version": "0.145.0",
    "source_thread_id": "athr_origin",
    "source_native_session_id": "native-origin",
    "target_thread_id": "athr_target",
    "target_resident_slot_id": 1,
    "state": "preparing",
}

#: The Fork as ``_session_fork_details`` returns it: its row, with the thread
#: sandbox's fields under every key the row lacks.
_FORK_OPERATION = {
    **_THREAD_SANDBOX,
    **_FORK_ROW,
    "thread_sandbox": _THREAD_SANDBOX,
    "source_job": {"user_id": "owner", "model": "model-1"},
    "source_native_turn_no": 1,
}


@pytest.fixture
def fork_bodies(monkeypatch) -> list[dict[str, Any]]:
    """Every body a Fork sends the Sandbox Manager, through the real client."""
    bodies: list[dict[str, Any]] = []
    sandbox_manager = SandboxManagerClient("http://sandbox_manager", "secret")

    async def request(method, thread_id, action, *, json=None, timeout=None):
        bodies.append(json)
        return {"operation_id": json["operation_id"]}

    monkeypatch.setattr(sandbox_manager, "_shared_sandbox_request", request)
    monkeypatch.setattr(
        session_fork, "sandbox_manager_for_job", AsyncMock(return_value=sandbox_manager)
    )
    return bodies


async def _materialize(operation: dict[str, Any]) -> None:
    """Take ``operation`` through every store step to the host's receipt."""
    residencies = {
        role: {
            "thread_id": thread_id,
            "user_id": "owner",
            "model": "model-1",
            "generation": 2,
            "establishing_attempt_id": f"fork-{role}:asfork_1",
        }
        for role, thread_id in (("source", "athr_origin"), ("target", "athr_target"))
    }
    store = SimpleNamespace(
        host_pool=object(),
        grants=SimpleNamespace(link_residency_grant=AsyncMock(return_value=True)),
        session_fork=SimpleNamespace(
            claim_session_fork_materialization=AsyncMock(return_value=True),
            establish_session_fork_residencies=AsyncMock(return_value=residencies),
            record_session_fork_materialized=AsyncMock(return_value={"state": "materialized"}),
        ),
    )
    grants = SimpleNamespace(
        mint=AsyncMock(return_value={"grant_id": "grant-1", "token": "token-1"}),
        revoke=AsyncMock(),
    )
    await session_fork.materialize_session_fork(
        operation, job_id="ajob_source", job_store=store, grants=grants
    )


async def test_the_fork_sends_the_group_generation_and_container_it_recorded(fork_bodies):
    """Not its row's own ``generation``, and not the container the group holds now."""
    await _materialize(_FORK_OPERATION)

    (body,) = fork_bodies
    request = SharedForkRequest.model_validate(body)
    assert request.generation == 5
    assert request.expected_container_id == "container-recorded"
