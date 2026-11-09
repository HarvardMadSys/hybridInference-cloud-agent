"""Reconciliation reads a final marker that came with a checkpoint as absent.

A checkpoint is ``docker commit`` of a stopped container, so its image carries
that container's root-owned final-archive marker, of an earlier generation of
the same group. Every archive reader already treats such a marker as absent
(``carried_by_checkpoint``); reconciliation, which reads the marker first to
decide whether a final archive is owed, did not. A container restored from a
checkpoint and left stopped before its first start wrote its own marker then
owed the earlier container's final archive, which the upload refused as not
its own, every pass, forever. Staging had four such containers.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from tests.support.archive_policy import hooked_supervisor
from tests.unit.test_shared_session_supervisor import _Manager, _spec

from cloud_agent_host.container.chat_sandbox import ChatSandboxStatus
from cloud_agent_host.container.sandbox import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.container.chat_sandbox_models import ChatSandboxSpec

#: The restored generation; the checkpoint was taken of generation 1.
_RESTORED = 2


def _marker(*, generation: int, sandbox_id: str = "asbx_shared") -> bytes:
    """A final marker as the archive writes it, of ``generation``'s container."""
    return json.dumps(
        {
            "format_version": 1,
            "namespace_sha256": "0" * 64,
            "sandbox_id": sandbox_id,
            "generation": generation,
            "container_id": "container-checkpoint-source",
            "state": "uploaded",
            "policy": {"enabled": True, "revision": 1},
        },
        sort_keys=True,
    ).encode()


def _restored_spec() -> ChatSandboxSpec:
    return replace(_spec(), generation=_RESTORED)


def _reconciled(payload: bytes, *, status: ChatSandboxStatus) -> tuple[list[str], Any, Any]:
    """Reconcile twice over one stopped container holding ``payload``.

    Returns the final archives attempted, the supervisor and the manager. An
    attempt is refused, as the archive refuses a marker that is not its own.
    """

    class Manager(_Manager):
        def read_stopped_final_archive_marker_optional(
            self, _spec: ChatSandboxSpec, *, expected_container_id: str
        ) -> bytes | None:
            del expected_container_id
            return payload

    manager = Manager()
    spec = _restored_spec()
    manager.items[spec.container_name] = replace(
        manager._inspection(spec, running=False), status=status
    )
    attempts: list[str] = []

    def stopped(group_spec: ChatSandboxSpec, *_args: object) -> None:
        attempts.append(group_spec.sandbox_id)
        raise SandboxError("final archive marker does not match the stopped sandbox")

    supervisor = hooked_supervisor(manager, stopped=stopped)
    supervisor.reconcile()
    supervisor.reconcile()
    return attempts, supervisor, manager


def test_a_restored_container_never_started_owes_no_final_archive() -> None:
    attempts, supervisor, manager = _reconciled(
        _marker(generation=1), status=ChatSandboxStatus.CREATED
    )

    assert attempts == []
    # Nothing is owed, so the group starts like any created one.
    spec = _restored_spec()
    token, _ = supervisor.operation_opened(spec.thread_id)
    assert manager.items[spec.container_name].running is True
    supervisor.operation_closed(spec.thread_id, token)


def test_a_stopped_container_with_a_carried_marker_is_treated_as_markerless() -> None:
    """Not the earlier container's archive: the handling for no marker at all."""
    attempts, _, _ = _reconciled(_marker(generation=1), status=ChatSandboxStatus.STOPPED)

    assert attempts == []


@pytest.mark.parametrize(
    "payload",
    [
        # This generation's marker of another container: still refused.
        _marker(generation=_RESTORED),
        # Another group's marker: still refused.
        _marker(generation=1, sandbox_id="asbx_other"),
        b"not json",
        b"[]",
        json.dumps({"sandbox_id": "asbx_shared", "generation": "1"}).encode(),
    ],
    ids=["same-generation", "other-group", "not-json", "not-an-object", "generation-not-int"],
)
def test_any_other_marker_is_left_to_the_archives_checks(payload: bytes) -> None:
    attempts, _, _ = _reconciled(payload, status=ChatSandboxStatus.CREATED)

    assert attempts[:1] == ["asbx_shared"]
