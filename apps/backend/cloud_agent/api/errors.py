"""Public error categories, and the one mapping of domain refusals onto HTTP.

Application flows raise ``Refused`` and ``Unavailable``
(``cloud_agent.domain.errors``) whether or not a request is behind them. The
API answers them here: a refusal's kind picks 400, 403, 404 or 409, an outage
is a 503, and a type with a status of its own names it in ``_STATUS_BY_TYPE``.
The body is the ``{"error": {"type", "message"}}`` envelope every typed refusal
uses, under FastAPI's ``detail``.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException

from cloud_agent.domain.errors import Refused, Unavailable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI, Request, Response


class AgentConfigurationError(HTTPException):
    """The deployment cannot verify access until its configuration is corrected."""

    def __init__(self) -> None:
        """Return a stable category without exposing configuration or credentials."""
        super().__init__(
            status_code=503,
            detail={
                "error": {
                    "type": "agent_configuration_error",
                    "message": "Agent sign-in is not configured correctly. Contact the service administrator.",
                }
            },
        )


def typed_error(status_code: int, error_type: str, message: str, **headers: str) -> HTTPException:
    """An ``HTTPException`` in the ``{"error": {"type", "message"}}`` envelope."""
    return HTTPException(
        status_code=status_code,
        detail={"error": {"type": error_type, "message": message}},
        headers=headers or None,
    )


_STATUS_BY_KIND = {"invalid": 400, "forbidden": 403, "not_found": 404, "conflict": 409}

#: The types whose status is not their kind's: the host answered a native Fork
#: with a receipt for something else, which is a bad gateway, not an outage.
_STATUS_BY_TYPE = {"invalid_native_fork_receipt": 502}


def as_http_exception(exc: Refused | Unavailable) -> HTTPException:
    """The ``HTTPException`` the API answers a domain refusal or outage with."""
    default = _STATUS_BY_KIND[exc.kind] if isinstance(exc, Refused) else 503
    status = _STATUS_BY_TYPE.get(exc.type or "", default)
    detail: Any
    if exc.type is None:
        detail = exc.message
    else:
        details = exc.details if isinstance(exc, Refused) else {}
        detail = {"error": {"type": exc.type, **details, "message": exc.message}}
    headers = (
        None if exc.retry_after_seconds is None else {"Retry-After": str(exc.retry_after_seconds)}
    )
    return HTTPException(status_code=status, detail=detail, headers=headers)


async def raising_http(
    handler: Callable[[Request], Awaitable[Response]], request: Request
) -> Response:
    """Run a route handler, raising a domain error as the ``HTTPException`` it maps to.

    For route classes that treat an ``HTTPException`` themselves (``no-store``,
    a local rendering): a domain error reaches them in the same form.
    """
    try:
        return await handler(request)
    except (Refused, Unavailable) as exc:
        raise as_http_exception(exc) from exc


def install_domain_errors(app: FastAPI) -> None:
    """Answer a domain error as the app answers the ``HTTPException`` it maps to.

    The app's own ``HTTPException`` handler renders it, so whatever policy that
    handler applies (``no-store`` on admin paths) applies here too.
    """

    async def answer(request: Request, exc: Exception) -> Response:
        handler = app.exception_handlers.get(StarletteHTTPException, http_exception_handler)
        response = handler(request, as_http_exception(cast("Refused | Unavailable", exc)))
        return await response if inspect.isawaitable(response) else response

    app.add_exception_handler(Refused, answer)
    app.add_exception_handler(Unavailable, answer)
