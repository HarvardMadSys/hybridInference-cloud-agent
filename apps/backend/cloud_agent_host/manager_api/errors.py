"""How the sandbox manager answers a request no route answered itself."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from cloud_agent_host.runtimes import RuntimeEffortUnsupportedError, RuntimeMCPUnavailableError

if TYPE_CHECKING:
    from fastapi import FastAPI, Request


async def safe_environment_validation(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Name a rejected field and why, never the value it carried."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                for error in exc.errors()
                if error["type"] != "value_error"
            ]
            or "Invalid sandbox manager request fields or environment limits"
        },
    )


async def runtime_refused(_request: Request, exc: Exception) -> JSONResponse:
    """Answer a runtime's deliberate refusal with the runtime's own reason.

    A runtime refuses a launch it cannot run as configured: MCP servers
    with no relay to reach them, or an effort its CLI cannot express. The
    message is written to be read, but these are not driver or sandbox
    errors, so no route mapped them. Each left as a plain-text 500, and
    the runner could only say the supervisor "rejected the lifecycle
    transition".
    """
    return JSONResponse(status_code=422, content={"detail": str(exc)[:300]})


async def unexpected_failure(request: Request, exc: Exception) -> JSONResponse:
    """Answer a failure no route anticipated in a body both clients can read.

    Starlette's plain-text ``Internal Server Error`` has no ``detail``, so
    the cause never left this process. The exception's type and the route
    are enough to find it in this log; Starlette still re-raises after
    answering, so the traceback is logged here. The message stays here
    too: a sandbox manager failure can reach the owner's browser through the
    control plane, which shows only what is safe to show.
    """
    route = getattr(request.scope.get("route"), "path", None) or "an unknown route"
    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                "sandbox manager failed unexpectedly "
                f"({type(exc).__name__} in {request.method} {route})"
            )
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register the sandbox manager's answers for validation, refusals and the unexpected."""
    app.add_exception_handler(RequestValidationError, safe_environment_validation)  # type: ignore[arg-type]
    for refusal in (RuntimeMCPUnavailableError, RuntimeEffortUnsupportedError):
        app.add_exception_handler(refusal, runtime_refused)
    app.add_exception_handler(Exception, unexpected_failure)


__all__ = ["install_error_handlers"]
