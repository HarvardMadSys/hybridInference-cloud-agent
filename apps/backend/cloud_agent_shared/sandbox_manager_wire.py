"""How a retained group is described to the Sandbox Manager, by either caller.

The control plane and the runner both send a group's identity and its member
manifest; the Sandbox Manager validates them against the bounds here. This is
the one definition of those fields and their bounds, and ``member_manifest()``
projects durable member records onto exactly those fields. A new member field
also needs the SQL projections that read members for the senders, the worker
API's ``WorkerSandboxMember``, and the supervisor's ``SharedMemberManifest``,
which a test holds to ``MEMBER_FIELDS``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

ContextStatus = Literal["new", "live", "restored", "lost"]
ResidentState = Literal["stopped", "starting", "live", "restarting", "lost"]


class SharedMember(BaseModel):
    """Authoritative durable identity for one member alias."""

    thread_id: str = Field(..., min_length=1, max_length=256)
    native_session_id: str | None = Field(None, max_length=512)
    context_status: ContextStatus = "new"
    activation_generation: int = Field(0, ge=0)
    process_generation: int = Field(0, ge=0)
    residency_generation: int = Field(0, ge=0)
    resident_state: ResidentState = "stopped"
    resident_slot_id: int = Field(..., ge=0, le=127)
    last_settled_job_id: str | None = Field(None, max_length=256)


#: The fields of one member on the wire, in declaration order.
MEMBER_FIELDS = tuple(SharedMember.model_fields)


def member_manifest(members: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project durable member records onto the wire's member fields.

    A field a record lacks is left out, so the receiver's default applies.
    """
    return [{key: member[key] for key in MEMBER_FIELDS if key in member} for member in members]


class SharedGroupIdentity(BaseModel):
    """Immutable group identity plus its current member manifest."""

    sandbox_id: str = Field(..., min_length=1, max_length=256)
    generation: int = Field(..., ge=1)
    origin_thread_id: str = Field(..., min_length=1, max_length=256)
    runtime_id: str = Field(..., min_length=1, max_length=128)
    runtime_version: str | None = Field(None, max_length=128)
    sandbox_image_digest: str | None = Field(None, max_length=512)
    expected_container_id: str | None = Field(None, min_length=1, max_length=256)
    resident_slot_capacity: int = Field(8, ge=2, le=128)
    memory_limit: str = Field("4g", min_length=1, max_length=32)
    cpu_limit: str = Field("2", min_length=1, max_length=32)
    disk_limit_mib: int = Field(0, ge=0, le=16777216, strict=True)
    stop_timeout_s: int = Field(10, ge=1, le=300)
    members: list[SharedMember] = Field(..., min_length=1, max_length=128)
    #: The control plane's start fence for the request; see ``_Group.start_seq``.
    start_seq: int | None = Field(None, ge=0)
