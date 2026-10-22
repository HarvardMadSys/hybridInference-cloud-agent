"""Composable response policy for routes that receive sensitive input."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from fastapi import Request, Response


def no_store(response: Response) -> Response:
    """Mark a successful or locally translated response as non-cacheable."""
    response.headers["Cache-Control"] = "no-store"
    return response


def no_store_exception(exc: HTTPException) -> HTTPException:
    """Add ``no-store`` without discarding challenges or other HTTP headers."""
    exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
    return exc


class SensitiveRoute(APIRoute):
    """Sanitize validation failures and apply ``no-store`` to every answer.

    Feature subclasses keep their wire-specific validation text and domain
    mappings in small hooks. This class owns the security-sensitive mechanics:
    every generated handler is wrapped, HTTP exception headers survive, and
    both successful and translated responses receive the cache policy.
    """

    validation_exceptions: ClassVar[tuple[type[Exception], ...]] = (RequestValidationError,)

    def validation_response(self, exc: Exception) -> Response:
        """Return the feature's fixed, input-free validation response."""
        raise NotImplementedError

    def domain_response(self, exc: Exception) -> Response | None:
        """Translate a known domain exception, or decline with ``None``."""
        return None

    def http_exception_response(self, exc: HTTPException) -> Response | None:
        """Optionally render an HTTP exception locally instead of re-raising it."""
        return None

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap FastAPI's handler once, while preserving subclass/mixin order."""
        handler = super().get_route_handler()

        async def sensitive(request: Request) -> Response:
            try:
                response = await handler(request)
            except self.validation_exceptions as exc:
                response = self.validation_response(exc)
            except HTTPException as exc:
                translated = self.http_exception_response(exc)
                if translated is None:
                    no_store_exception(exc)
                    raise
                response = translated
            except Exception as exc:
                translated = self.domain_response(exc)
                if translated is None:
                    raise
                response = translated
            return no_store(response)

        return sensitive


__all__ = ["SensitiveRoute", "no_store", "no_store_exception"]
