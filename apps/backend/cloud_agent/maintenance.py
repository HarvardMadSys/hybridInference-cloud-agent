"""Maintenance mode: while a database move runs, writes get a clear answer, not a failure.

Every control-plane process polls ``agent_maintenance`` every two seconds
(:func:`watch_forever`) and reports the workflow it has seen in its instance
report, so the move fences the database only after every process stopped
accepting writes. :class:`MaintenanceGuard` then answers each write request
with 503, ``Retry-After`` and the reason. Reads keep working: the database is
read-only, not gone. The deployment agents' own routes are exempt, because
they carry the move's progress and results.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

POLL_INTERVAL_S = 2.0
RETRY_AFTER_S = 30
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Routes that keep writing during maintenance, by exact prefix.
EXEMPT_PREFIXES = ("/v1/agent/deploy/", "/v1/session/logout")

_state: dict[str, Any] = {"active": False}
_progress: dict[str, dict[str, Any]] = {}


def current() -> dict[str, Any]:
    """What this process last read: ``active``, and the workflow, kind, start and detail."""
    return dict(_state)


def apply(row: dict[str, Any] | None) -> None:
    """Adopt a maintenance row read from the database."""
    global _state
    if row is None or row.get("state") != "migrating":
        _state = {"active": False}
        _progress.clear()
        return
    started = row.get("started_at")
    _state = {
        "active": True,
        "kind": row.get("kind"),
        "workflow_id": row.get("workflow_id"),
        "detail": row.get("detail") or "",
        "since": started.isoformat() if isinstance(started, datetime) else started,
    }


def seen_workflow() -> str | None:
    """The maintenance workflow this process is refusing writes for, for its instance report."""
    return _state.get("workflow_id") if _state.get("active") else None


async def watch_forever(repository: Any, interval_s: float = POLL_INTERVAL_S) -> None:
    """Keep this process's view of maintenance current; a failed read keeps the last view."""
    from cloud_agent.logging_utils import get_logger

    logger = get_logger(__name__)
    while True:
        try:
            apply(await repository.current())
        except Exception as exc:
            logger.warning("maintenance state read failed (%s)", type(exc).__name__)
        await asyncio.sleep(interval_s)


def record_progress(agent_id: str, operations: list[dict[str, Any]]) -> None:
    """Keep the last operations an agent reported while the database cannot store them.

    Held by the one process that received the report, and lost when it
    restarts: the page says so, and the agent's own journal stays the record.
    """
    _progress[agent_id] = {
        "operations": operations[-5:],
        "received_at": datetime.now(UTC).isoformat(),
    }


def progress() -> dict[str, dict[str, Any]]:
    """What this process has heard from agents during maintenance."""
    return dict(_progress) if _state.get("active") else {}


def _response_body() -> bytes:
    return json.dumps(
        {
            "detail": (
                "The service is in maintenance while its database moves; changes are "
                "refused until it finishes. Reading keeps working."
            ),
            "maintenance": {k: v for k, v in _state.items() if k != "detail"},
        }
    ).encode()


def install_read_only_handler(app: Any) -> None:
    """Answer a write that reached a fenced database with the same 503, not a 500.

    Covers what the guard cannot see: a read that also writes, and a request
    in the seconds before this process notices maintenance.
    """
    import asyncpg
    from starlette.responses import Response

    async def refuse(_request: Any, _exc: Exception) -> Response:
        return Response(
            _response_body(),
            status_code=503,
            media_type="application/json",
            headers={"Retry-After": str(RETRY_AFTER_S), "Cache-Control": "no-store"},
        )

    app.add_exception_handler(asyncpg.exceptions.ReadOnlySQLTransactionError, refuse)


class MaintenanceGuard:
    """Refuse writes with 503 while this process sees maintenance."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the application."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass reads, exempt routes and everything outside maintenance."""
        if (
            scope["type"] != "http"
            or not _state.get("active")
            or scope.get("method", "GET") in _SAFE_METHODS
            or str(scope.get("path", "")).startswith(EXEMPT_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return
        body = _response_body()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(RETRY_AFTER_S).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = [
    "EXEMPT_PREFIXES",
    "MaintenanceGuard",
    "apply",
    "current",
    "install_read_only_handler",
    "progress",
    "record_progress",
    "seen_workflow",
    "watch_forever",
]
