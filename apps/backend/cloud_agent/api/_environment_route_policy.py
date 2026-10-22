"""Sensitive request policy shared by environment-bearing API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from cloud_agent.api._sensitive_routes import SensitiveRoute
from cloud_agent.environments import (
    EnvironmentAccessRevoked,
    EnvironmentBusy,
    EnvironmentConflict,
    EnvironmentEncryptionError,
    EnvironmentForbidden,
    EnvironmentNotFound,
)
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent_shared.environment import EnvironmentInputError

if TYPE_CHECKING:
    from fastapi import HTTPException


class EnvironmentSensitiveRoute(SensitiveRoute):
    """Preserve environment-specific refusals without echoing credential input."""

    def validation_response(self, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        errors = []
        for error in exc.errors():
            message = error["msg"]
            cause = error.get("ctx", {}).get("error")
            if isinstance(cause, EnvironmentInputError):
                message = str(cause)
            elif error["type"] in {"value_error", "assertion_error"}:
                message = "Invalid field value"
            errors.append({"loc": error["loc"], "type": error["type"], "msg": message})
        detail: Any = errors
        if any(
            "execution_profile is server-owned" in error.get("msg", "") for error in exc.errors()
        ):
            detail = "execution_profile is server-owned"
        return JSONResponse(status_code=422, content={"detail": detail})

    def http_exception_response(self, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    def domain_response(self, exc: Exception) -> JSONResponse | None:
        if isinstance(exc, EnvironmentNotFound):
            return JSONResponse(
                status_code=404, content={"detail": "Environment or run not found."}
            )
        if isinstance(exc, EnvironmentForbidden):
            return JSONResponse(status_code=403, content={"detail": str(exc)})
        if isinstance(exc, EnvironmentAccessRevoked):
            return JSONResponse(status_code=409, content={"detail": str(exc)})
        if isinstance(exc, SandboxManagerError):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})
        if isinstance(exc, asyncpg.UniqueViolationError):
            return JSONResponse(
                status_code=409,
                content={"detail": "Configuration conflicts with an existing object."},
            )
        if isinstance(exc, (EnvironmentConflict, EnvironmentBusy)):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": str(exc),
                    **(
                        {"current_revision": exc.current_revision}
                        if isinstance(exc, EnvironmentConflict) and exc.current_revision is not None
                        else {}
                    ),
                },
            )
        if isinstance(exc, EnvironmentEncryptionError):
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Environment encryption is unavailable. Contact the deployment administrator."
                },
            )
        if isinstance(exc, ValidationError):
            return JSONResponse(
                status_code=422,
                content={"detail": "Invalid environment variable or script"},
            )
        if isinstance(exc, EnvironmentInputError):
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        return None


__all__ = ["EnvironmentSensitiveRoute"]
