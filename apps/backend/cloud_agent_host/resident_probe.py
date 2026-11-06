"""Why a turn could not borrow its chat's live CLI process, for the startup measurement.

When the lease says a member's resident is ``live``, the runner first asks the
sandbox manager to reuse it with a probe that carries no credential. A refused
probe makes the runner rotate the residency and reopen the CLI -- the
``resume_running`` startup path, seconds slower than ``reuse_live``. Both
sides of that exchange live here so they cannot drift apart: the supervisor
names the differing fields in its refusal (:func:`probe_refusal_message`) and
the runner copies only that field list into the measurement
(:func:`probe_refusal_fields`). The rest of an error message is free text,
and a measurement is not redacted, so it never reaches the record.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from cloud_agent_host.sandbox_manager_client import SandboxManagerError
from cloud_agent_shared import operation_profiling
from cloud_agent_shared.sandbox_manager_compat import SANDBOX_MANAGER_PRODUCER

if TYPE_CHECKING:
    from cloud_agent_host.resident_transport import ResidentLaunch

_REFUSAL_FIELDS = re.compile(r"\(differs: ([a-z_]+(?:,[a-z_]+)*)\)")
_LEASE_STATES = frozenset({"starting", "restarting", "stopped", "live", "lost"})


def probe_refusal_message(current: Any, launch: ResidentLaunch, same_residency: bool) -> str:
    """The refusal of a credential-less probe, naming what differs by field name only.

    ``residency_generation`` means the process holds an older residency grant
    than the activation; any other name is a process-fixed launch setting that
    changed. The values stay out: several of these fields are credentials.
    """
    names = getattr(current, "incompatible_fields", None)
    fields = list(names(launch)) if callable(names) else []
    if not same_residency:
        fields.append("residency_generation")
    differs = ",".join(fields) or "unknown"
    return f"resident process restart requires a renewable credential (differs: {differs})"


def probe_refusal_fields(message: str) -> str:
    """The process-fixed fields a refused probe named, or ``other`` when it named none.

    ``other`` is a refusal that is not a field mismatch -- the process died,
    the lease was lost, the sandbox manager restarted.
    """
    found = _REFUSAL_FIELDS.search(message or "")
    return found.group(1) if found else "other"


def probe_lease_state(value: Any) -> str:
    """A lease's resident state from a known vocabulary, for a measurement fact."""
    state = str(value or "")
    if not state:
        return "absent"
    return state if state in _LEASE_STATES else "other"


def probe_live_resident(
    measurement: operation_profiling.Measurement | None,
    supervisor: Any,
    *,
    lease: dict[str, Any],
    thread_id: str,
    token: str,
    launch: ResidentLaunch,
) -> tuple[dict[str, Any] | None, str | None]:
    """Borrow the member's live process if the lease says it has one.

    Returns the sandbox manager's descriptor and the fragment call id it
    answered, or ``(None, None)`` when there is no live process to borrow or it
    refused: a process-fixed model/MCP setting changed, the process died, or
    the sandbox manager restarted. The caller then rotates only this member's
    grant and resumes its exact native session. Either way the outcome is kept
    on the startup measurement, because a probe that keeps failing turns every
    follow-up into a process restart.
    """
    if lease.get("resident_state") != "live":
        operation_profiling.set_attributes(
            measurement,
            resident_probe="skipped",
            resident_probe_lease_state=probe_lease_state(lease.get("resident_state")),
        )
        return None, None
    call_id = operation_profiling.expect_fragment(
        measurement, SANDBOX_MANAGER_PRODUCER, "resident_start"
    )
    # Timed here, not by the sandbox manager: a refused probe answers with an
    # error, which carries no profiling fragment.
    started = measurement.offset_us() if measurement is not None else 0
    try:
        descriptor = supervisor.ensure_shared_resident(
            thread_id,
            token=token,
            launch=launch,
            profiling=operation_profiling.measurement_context(measurement, call_id),
        )
    except SandboxManagerError as exc:
        operation_profiling.set_attributes(
            measurement,
            resident_probe="refused",
            resident_probe_us=measurement.offset_us() - started if measurement is not None else 0,
            resident_probe_status=exc.status_code,
            resident_probe_differs=probe_refusal_fields(exc.message),
        )
        return None, None
    operation_profiling.set_attributes(measurement, resident_probe="accepted")
    return descriptor, call_id


__all__ = [
    "probe_lease_state",
    "probe_live_resident",
    "probe_refusal_fields",
    "probe_refusal_message",
]
