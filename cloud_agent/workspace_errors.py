"""The workspace broker's failures, as the bounded owner-facing errors.

`WorkspaceBrokerError` carries the broker's private vocabulary; these two turn
it into what an owner may see — one message, one error type, a status chosen
from a small set — and recognize the shared-only broker's way of saying a chat
has no retained workspace yet.

They lived in `api/_workspace_access.py`. `application/session_fork.py` needs
them, and a flow reaching into the adapter layer would invert the direction
those two packages are given, so they belong to neither: this module imports
only FastAPI and the broker client, and both sides import it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from cloud_agent.workspace_broker_client import WorkspaceBrokerError


def _workspace_broker_http_error(exc: WorkspaceBrokerError) -> HTTPException:
    """Map private broker failures onto bounded owner-facing errors."""
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


def _workspace_not_materialized(exc: WorkspaceBrokerError) -> bool:
    """Recognize the shared-only broker's missing-workspace response."""
    return exc.status_code == 404 and exc.message == "shared chat workspace is not materialized"
