"""The values an Admin profiling command produces.

These live outside the port because they are data rather than capability: a
result is what a caller reads, and the port is what a caller calls. Keeping them
in the port module would also mean two classes in one port file, which the
storage boundary's own test refuses -- and that test is right, because "one
capability per file" is what makes the port directory readable as an inventory.

The declarations are here rather than in the repository because the *control
plane* owns them: the repository returns them, the routes and the service read
them, and neither should have to import the other side's implementation to name
a type they both speak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

#: Why a Start or a Stop was refused. Declared here because the Admin route
#: translates each one into a status code, and a name invented at the route
#: would be a second vocabulary for the same refusal.
CONTROL_REFUSED_NOT_ADMIN_MODE = "not_admin_mode"
CONTROL_REFUSED_STALE_REVISION = "stale_revision"
CONTROL_REFUSED_ALREADY_ACTIVE = "already_active"
CONTROL_REFUSED_UNKNOWN_RUN = "unknown_run"
CONTROL_REFUSED_ALREADY_FINISHED = "run_already_finished"
CONTROL_REFUSED_CONFLICTING_REQUEST = "request_id_conflict"
CONTROL_REFUSED_INVALID = "invalid_request"

#: What a Start or Stop did. ``applied`` means this call changed the control
#: row; ``replayed`` means the same request id had already changed it and the
#: original result is being returned. They are different answers on purpose: an
#: operator looking at an audit trail has to be able to tell a retry from a
#: second command.
CONTROL_OUTCOME_APPLIED = "applied"
CONTROL_OUTCOME_REPLAYED = "replayed"
CONTROL_OUTCOME_REFUSED = "refused"


@dataclass(frozen=True)
class ControlCommandResult:
    """The single answer a Start or a Stop produces.

    ``kind`` is one of :data:`CONTROL_OUTCOME_APPLIED`,
    :data:`CONTROL_OUTCOME_REPLAYED` or :data:`CONTROL_OUTCOME_REFUSED`. A
    refusal still carries the revision the deployment is on, because the most
    common refusal is "somebody else moved it" and the caller needs the number
    it must re-read before trying again.
    """

    kind: str
    revision: int
    run_id: str | None = None
    reason: str = ""
    detail: str = ""
    #: The run's absolute expiry, for a Start that created one.
    expires_at: datetime | None = None
    #: The control row as it stands after this call, applied or refused. The
    #: Admin route answers with this so a refused request is still a successful
    #: read of the truth.
    state: Mapping[str, Any] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        """Return whether this call is the one that changed the control row."""
        return self.kind == CONTROL_OUTCOME_APPLIED

    @property
    def accepted(self) -> bool:
        """Return whether the request was accepted, first time or replayed."""
        return self.kind in {CONTROL_OUTCOME_APPLIED, CONTROL_OUTCOME_REPLAYED}


__all__ = [
    "CONTROL_OUTCOME_APPLIED",
    "CONTROL_OUTCOME_REFUSED",
    "CONTROL_OUTCOME_REPLAYED",
    "CONTROL_REFUSED_ALREADY_ACTIVE",
    "CONTROL_REFUSED_ALREADY_FINISHED",
    "CONTROL_REFUSED_CONFLICTING_REQUEST",
    "CONTROL_REFUSED_INVALID",
    "CONTROL_REFUSED_NOT_ADMIN_MODE",
    "CONTROL_REFUSED_STALE_REVISION",
    "CONTROL_REFUSED_UNKNOWN_RUN",
    "ControlCommandResult",
]
