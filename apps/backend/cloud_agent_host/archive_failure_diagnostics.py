"""Bounded descriptions of archive failures, safe to cross the broker API.

An archive failure can carry text a storage provider wrote, so its message
never leaves the host. What does is recorded here: the phase, a bounded type
name, and an error code and HTTP status that pass a strict token check. The
supervisor attaches them to the exception it raises, and the sandbox manager
answers with :func:`archive_failure_http_detail`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

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


__all__ = ["archive_failure_http_detail"]
