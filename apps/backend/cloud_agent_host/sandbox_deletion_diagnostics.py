"""What a Host logs when it refuses to discard a stopped sandbox.

A refusal reaches the control plane as one status and one sentence. Several
lifecycle barriers share a sentence ("sandbox lifecycle archive must finish
before deletion" covers seven), and the group's state is gone once the next
listing retries its recovery. So the first refusal logs the group's barriers,
its counts and its identity checks, read under the supervisor lock right after
the refusal.

Only fixed values are logged: a reason from the sentences the host itself
raises, booleans, counts, generations, short container and request digests,
and ids that have the shape of ids. No exception text, path, credential or
workspace content is logged; an unknown reason is logged as ``unrecognized``.
A filesystem error writing a receipt is logged too, by its error number; it
is not a refusal, and the Sandbox Manager still answers it with 500.
The state is read after the refusal, not at it: a recovery running in the
background may change it in between, and nothing here says which barrier
caused the refusal when more than one is set.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

from cloud_agent_host.chat_sandbox_models import RetainedChatLost, SandboxRestoreFailed
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost, StaleStartFence

if TYPE_CHECKING:
    from pathlib import Path

    from cloud_agent_host.sandbox_deletion_backup import ArchiveIdentity
    from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor, _Group

logger = get_logger(__name__)

#: The sentences a discard can be refused with, as the host raises them.
#: ``tests/unit/test_sandbox_discard_diagnostics.py`` checks each against the
#: host source, so a reworded refusal is logged as ``unrecognized`` only
#: until that test fails.
KNOWN_REASONS = frozenset(
    {
        # The deletion receipts.
        "sandbox discard does not upload an archive",
        "sandbox discard request identity changed",
        "sandbox archive request cannot be discarded",
        "sandbox discard target is no longer absent",
        "sandbox archive receipt is unreadable",
        "sandbox archive receipt changed",
        # The supervisor's archive and deletion fences.
        "sandbox archive generation is stale",
        "another sandbox archive is in progress",
        "sandbox archive requires an idle group",
        "shared sandbox container identity was lost",
        "sandbox lifecycle archive must finish before deletion",
        "shared sandbox lifecycle check is in progress",
        "shared workspace snapshot capture is in progress",
        "shared workspace stop is in progress",
        "shared workspace start is in progress",
        "shared workspace final archive is blocked by native flush failure",
        "shared workspace final archive retry is pending",
        "shared workspace orphan finalization is pending",
        "shared workspace start archive completion is pending",
        "shared sandbox lifecycle changed during observation",
        "shared sandbox container identity changed",
        "sandbox archive identity changed",
        "sandbox archive request is stale",
        "sandbox deletion requires an idle group",
        "shared workspace creation is in progress",
        "archived sandbox identity changed during deletion",
        # Docker's exact-container checks.
        "sandbox archive requires this deployment's ownership label",
        "sandbox archive requires a stopped or dead container",
        "archived sandbox removal failed",
        "archived sandbox still exists after removal",
        "chat sandbox exact container is missing",
        "chat sandbox container identity changed",
    }
)

#: ``chat 'athr_…' has no shared sandbox membership`` names the chat; the id
#: is logged as its own field instead.
_NO_MEMBERSHIP = re.compile(r"chat '[^']*' has no shared sandbox membership")

#: The group state any of which refuses a deletion
#: (``_require_deletion_lifecycle_admission``, ``_require_mutation_admission``).
_BARRIERS = (
    "container_lost",
    "snapshotting",
    "starting",
    "stopping",
    "initial_archive_preservation_pending",
    "stop_archive_pending",
    "stop_archive_blocked",
    "orphan_finalization_pending",
    "orphan_marker_unreadable",
    "start_archive_completion_pending",
)

#: Every receipt one deletion request can have written.
RECEIPTS = (
    "prepare",
    "upload",
    "commit",
    "deleted",
    "abort",
    "discard",
    "discarded",
)

_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_HEX = re.compile(r"[0-9a-f]{12,128}")


def refusal_reason(exc: BaseException) -> str:
    """The fixed sentence ``exc`` was raised with, or ``unrecognized``."""
    text = str(exc)
    if text in KNOWN_REASONS:
        return text
    if _NO_MEMBERSHIP.fullmatch(text):
        return "chat has no shared sandbox membership"
    return "unrecognized"


def _status(exc: BaseException) -> int:
    """The status the Sandbox Manager answers ``exc`` with (``supervisor_error``)."""
    if isinstance(exc, SandboxRestoreFailed):
        return 424
    if isinstance(exc, RetainedChatLost):
        return 410
    if isinstance(exc, (SessionBusy, SessionLeaseLost)):
        return 409
    if isinstance(exc, ValueError):
        return 422
    if isinstance(exc, OSError):
        # No route handles it: the app's last-resort handler answers 500.
        return 500
    return 503


def _error(exc: BaseException) -> str:
    for kind in (
        StaleStartFence,
        SessionBusy,
        SessionLeaseLost,
        SandboxRestoreFailed,
        RetainedChatLost,
        SandboxError,
        ValueError,
        OSError,
    ):
        if isinstance(exc, kind):
            return kind.__name__
    return "other"


def _safe_id(value: object) -> str:
    return value if isinstance(value, str) and _ID.fullmatch(value) else "invalid"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _container(value: object) -> str:
    """A Docker id's short form, as ``docker ps`` prints it."""
    return value[:12] if isinstance(value, str) and _HEX.fullmatch(value) else "invalid"


def _group_state(
    supervisor: SharedChatSessionSupervisor, group: _Group, identity: ArchiveIdentity
) -> dict[str, Any]:
    """The fields of ``group`` a deletion's fences read. The caller holds the lock."""
    try:
        bridge = supervisor._manager.network.network_for(group.spec) in group.inspection.networks
    except Exception:
        bridge = None
    request = group.archive_request_id
    archive_request = "none" if request is None else "other"
    if request == identity.request_id:
        archive_request = "this"
    members = group.members.values()
    return {
        "group_generation": group.spec.generation,
        "container_matches": group.inspection.container_id == identity.expected_container_id,
        "container_running": group.inspection.running,
        "bridge_attached": bridge,
        "archive_request": archive_request,
        "lifecycle_check": group.lifecycle_check is not None,
        "barriers": [name for name in _BARRIERS if getattr(group, name)],
        "operations": len(group.operation_tokens),
        "terminals": len(group.terminal_tokens),
        "members": len(group.members),
        "leased_members": sum(member.lease_token is not None for member in members),
        "active_turns": sum(member.current_turn is not None for member in members),
    }


def _state(supervisor: SharedChatSessionSupervisor, identity: ArchiveIdentity) -> dict[str, Any]:
    with supervisor._lock:
        group = supervisor._members.get(identity.thread_id)
        if group is None:
            return {"member_known": False}
        return {"member_known": True, **_group_state(supervisor, group, identity)}


def _render(value: object) -> str:
    """One ``key=value`` value for the text format; a sentence is quoted."""
    if isinstance(value, list):
        return ",".join(map(str, value)) or "none"
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return str(value).lower()
    text = str(value)
    return f'"{text}"' if " " in text else text


def log_discard_refusal(
    supervisor: SharedChatSessionSupervisor,
    identity: ArchiveIdentity,
    *,
    sandbox_id: str,
    directory: Path,
    exc: BaseException,
) -> None:
    """Log one refused discard with the group state its fences read; never raise."""
    fields: dict[str, Any] = {
        "reason": refusal_reason(exc),
        "error": _error(exc),
        "status": _status(exc),
        **({"errno": exc.errno} if isinstance(exc, OSError) and isinstance(exc.errno, int) else {}),
        "thread_id": _safe_id(identity.thread_id),
        "sandbox_id": _safe_id(sandbox_id),
        "request_digest": _digest(identity.request_id),
        "generation": identity.generation,
        "container": _container(identity.expected_container_id),
    }
    try:
        fields["receipts"] = [name for name in RECEIPTS if (directory / f"{name}.json").exists()]
        fields.update(_state(supervisor, identity))
        fields["state"] = "after_refusal"
    except Exception as state_error:
        fields["state"] = "unavailable"
        fields["state_error"] = _error(state_error)
    summary = " ".join(f"{key}={_render(value)}" for key, value in fields.items())
    logger.warning(
        "sandbox discard refused: %s",
        summary,
        extra={"event": "sandbox_discard_refused", **fields},
    )
