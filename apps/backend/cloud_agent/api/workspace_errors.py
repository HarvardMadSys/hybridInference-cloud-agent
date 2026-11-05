"""The sandbox manager's failures, as the bounded owner-facing errors.

`SandboxManagerError` carries the sandbox manager's private vocabulary; these two turn
it into what an owner may see — one message, one error type, a status chosen
from a small set — and recognize the shared-only sandbox manager's way of saying a chat
has no retained workspace yet.

They lived in `api/_workspace_access.py`, and moved here when
`application/session_fork.py` used them. That flow now hands the host's failure
back as `ForkHostFailed` and the Fork route maps it here, so only the API
imports this module; it imports only FastAPI and the sandbox manager client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from cloud_agent.sandbox_manager.sandbox_manager_client import SandboxManagerError


def _sandbox_manager_http_error(exc: SandboxManagerError) -> HTTPException:
    """Map private sandbox manager failures onto bounded owner-facing errors."""
    missing_workspace = _workspace_not_materialized(exc)
    status = (
        409
        if missing_workspace
        else exc.status_code
        if exc.status_code in {400, 404, 408, 409, 413, 429}
        else 503
    )
    message = "This chat has no retained workspace." if missing_workspace else exc.message
    return HTTPException(
        status_code=status,
        detail={"error": {"type": "workspace_unavailable", "message": message}},
    )


def _workspace_not_materialized(exc: SandboxManagerError) -> bool:
    """Recognize the shared-only sandbox manager's missing-workspace response."""
    return exc.status_code == 404 and exc.message == "shared chat workspace is not materialized"
