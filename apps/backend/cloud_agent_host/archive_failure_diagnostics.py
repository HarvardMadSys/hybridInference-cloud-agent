"""Bounded descriptions of archive failures, safe to cross the broker API.

An archive failure can carry text a storage provider wrote, so its message
never leaves the host. What does is recorded here: the phase, a bounded type
name, and an error code and HTTP status that pass a strict token check. The
supervisor attaches them to the exception it raises, and the sandbox manager
answers with :func:`archive_failure_http_detail`.

A clean stop's two steps after Docker stop are logged the same way when they
fail (:func:`logged_stopped_archive_failure`): the final archive, and then the
release of the group bridge that marks it unfinished. A failed release names
which Docker step failed, read from the fixed sentences the release raises;
Docker's own output is never logged.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent_host.chat_sandbox_models import RetainedChatLost
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.sandbox_deletion_diagnostics import _container, _render, _safe_id

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox_models import ChatSandboxSpec

logger = get_logger(__name__)

_ARCHIVE_FAILURE_DIAGNOSTICS_ATTR = "_cloud_agent_archive_failure_diagnostics"
_ARCHIVE_ERROR_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ArchiveFailurePhase = Literal["initial", "fail_stop_final"]


@dataclass(frozen=True)
class _ArchiveFailureDiagnostic:
    """One bounded archive error description safe to cross the broker API."""

    phase: _ArchiveFailurePhase
    error_type: str
    error_code: str | None = None
    http_status: int | None = None

    def render(self) -> str:
        error_type = self.error_type if _ARCHIVE_ERROR_TOKEN.fullmatch(self.error_type) else "Error"
        parts = [f"phase={self.phase}", f"type={error_type}"]
        if self.error_code is not None and _ARCHIVE_ERROR_TOKEN.fullmatch(self.error_code):
            parts.append(f"code={self.error_code}")
        if (
            self.http_status is not None
            and not isinstance(self.http_status, bool)
            and 100 <= self.http_status <= 599
        ):
            parts.append(f"status={self.http_status}")
        return f"archive failure ({', '.join(parts)})"


def _archive_failure_diagnostic(
    phase: _ArchiveFailurePhase,
    error: BaseException,
) -> _ArchiveFailureDiagnostic:
    """Classify an exception without copying any provider-controlled message."""
    current: BaseException | None = error
    selected = error
    error_code: str | None = None
    http_status: int | None = None
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        selected = current
        try:
            response = getattr(current, "response", None)
        except Exception:
            response = None
        if isinstance(response, Mapping):
            error_payload = response.get("Error")
            metadata = response.get("ResponseMetadata")
            candidate_code = (
                error_payload.get("Code") if isinstance(error_payload, Mapping) else None
            )
            candidate_status = (
                metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
            )
            if isinstance(candidate_code, str) and _ARCHIVE_ERROR_TOKEN.fullmatch(candidate_code):
                error_code = candidate_code
            if (
                isinstance(candidate_status, int)
                and not isinstance(candidate_status, bool)
                and 100 <= candidate_status <= 599
            ):
                http_status = candidate_status
            if error_code is not None or http_status is not None:
                break
        current = current.__cause__
        if current is None and not selected.__suppress_context__:
            current = selected.__context__
    error_type = type(selected).__name__
    if _ARCHIVE_ERROR_TOKEN.fullmatch(error_type) is None:
        error_type = "Error"
    return _ArchiveFailureDiagnostic(
        phase=phase,
        error_type=error_type,
        error_code=error_code,
        http_status=http_status,
    )


def _safe_error_type(error: BaseException) -> str:
    """Return only a bounded Python type name, never an exception message."""
    name = type(error).__name__
    return name if _ARCHIVE_ERROR_TOKEN.fullmatch(name) else "Error"


def _record_archive_failure_diagnostics(
    error: BaseException,
    *,
    initial: BaseException,
    fail_stop_final: BaseException | None = None,
) -> None:
    """Attach only safe fields before ``raise ... from`` replaces a cause chain."""
    diagnostics = [_archive_failure_diagnostic("initial", initial)]
    if fail_stop_final is not None:
        diagnostics.append(_archive_failure_diagnostic("fail_stop_final", fail_stop_final))
    setattr(error, _ARCHIVE_FAILURE_DIAGNOSTICS_ATTR, tuple(diagnostics))


def archive_failure_http_detail(error: BaseException) -> str | None:
    """Return a bounded broker response for a recorded initial archive failure."""
    diagnostics = getattr(error, _ARCHIVE_FAILURE_DIAGNOSTICS_ATTR, None)
    if not isinstance(diagnostics, tuple) or not diagnostics:
        return None
    if not all(isinstance(item, _ArchiveFailureDiagnostic) for item in diagnostics):
        return None
    return "; ".join(item.render() for item in diagnostics)


#: How the exact network release (``ChatSandboxManager.stop``) says it failed,
#: by the start of its message, and which step that was. A message that only
#: begins with one of these goes on with Docker's own output, never logged.
NETWORK_RELEASE_FAILURES: tuple[tuple[str, str], ...] = (
    ("Docker network inspect failed: ", "inspect"),
    ("Docker returned an invalid network inspection", "inspect"),
    ("chat sandbox inspect failed: ", "inspect"),
    ("Docker returned an invalid chat sandbox inspection", "inspect"),
    ("Docker returned invalid group-network endpoints", "inspect"),
    ("inactive group network could not be removed: ", "remove"),
    ("Docker lifecycle command failed: ", "docker_command"),
    ("refusing to remove a network outside this chat/generation", "ownership"),
    ("retained chat is attached to network ", "foreign_network"),
    ("Docker name is occupied by a container outside this chat/generation", "identity"),
    ("chat sandbox restarted during network release", "restarted"),
    ("stopped chat sandbox network identity changed during release", "network_identity"),
    ("group network reappeared during stopped intent cleanup", "network_identity"),
    ("stopped chat sandbox network intent could not be cleared", "network_intent"),
    ("stopped chat sandbox still retains its group network intent", "network_intent"),
    ("refusing to release networking from a running chat sandbox", "restarted"),
    ("chat sandbox could not be stopped: ", "stop"),
    ("chat sandbox stopped into unexpected state ", "stop"),
    ("refusing to treat a paused chat sandbox as cleanly stopped", "stop"),
)
#: ``container <id> could not leave network '<name>': <stderr>``.
_DISCONNECT = re.compile(r"container [0-9a-f]{1,12} could not leave network ")
#: ``group network '<name>' contains an unexpected peer``.
_UNEXPECTED_PEER = re.compile(r"group network .* contains an unexpected peer")

_StoppedArchivePhase = Literal["final_archive", "network_release"]
_PHASE_EVENTS: dict[str, tuple[str, str]] = {
    "final_archive": ("sandbox_final_archive_failed", "stopped sandbox final archive failed: %s"),
    "network_release": (
        "sandbox_final_network_release_failed",
        "stopped sandbox network release failed: %s",
    ),
}


def network_release_category(error: BaseException) -> str:
    """Which step of the exact network release failed, or ``unknown``."""
    if isinstance(error, RetainedChatLost):
        return "identity"
    text = str(error)
    if _DISCONNECT.match(text):
        return "disconnect"
    if _UNEXPECTED_PEER.fullmatch(text):
        return "unexpected_peer"
    for prefix, category in NETWORK_RELEASE_FAILURES:
        if text.startswith(prefix):
            return category
    return "unknown"


def _command_failure(error: BaseException) -> dict[str, Any]:
    """The timeout or operating-system error number under a Docker command failure."""
    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            break
        if isinstance(current, subprocess.TimeoutExpired):
            return {"timeout": True}
        if isinstance(current, OSError) and isinstance(current.errno, int):
            return {"errno": current.errno}
        current = current.__cause__
    return {}


def _failure_fields(phase: str, error: BaseException) -> dict[str, Any]:
    if phase == "final_archive":
        diagnostic = _archive_failure_diagnostic("fail_stop_final", error)
        return {
            "error": diagnostic.error_type,
            "code": diagnostic.error_code,
            "status": diagnostic.http_status,
            # The field this failure was logged with before it named its phase.
            "detail": diagnostic.render(),
        }
    return {
        "error": _safe_error_type(error),
        "category": network_release_category(error),
        **_command_failure(error),
    }


@contextmanager
def logged_stopped_archive_failure(
    phase: _StoppedArchivePhase, spec: ChatSandboxSpec, container_id: str
) -> Iterator[None]:
    """Log a failure of one step after a clean Docker stop, then raise it unchanged."""
    try:
        yield
    except Exception as error:
        event, message = _PHASE_EVENTS[phase]
        fields: dict[str, Any] = {
            "phase": phase,
            "sandbox_id": _safe_id(spec.sandbox_id),
            "generation": spec.generation,
            "container": _container(container_id),
        }
        try:
            fields.update(_failure_fields(phase, error))
        except Exception:
            fields["error"] = "unclassified"
        shown = {key: value for key, value in fields.items() if key != "detail"}
        summary = " ".join(f"{key}={_render(value)}" for key, value in shown.items())
        logger.error(message, summary, extra={"event": event, **fields})
        raise


__all__ = [
    "NETWORK_RELEASE_FAILURES",
    "archive_failure_http_detail",
    "logged_stopped_archive_failure",
    "network_release_category",
]
