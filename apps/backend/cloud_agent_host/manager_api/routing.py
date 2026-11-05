"""Routes declared on a class, bound to one app's dependencies.

Each topic's routes are methods of a class built with that app's
:class:`~cloud_agent_host.manager_api.context.ManagerContext`, so a handler
reaches its dependencies through ``self`` rather than through a closure over
``create_app``. :func:`route` records a method's verb and path where the
method is written; :func:`bind` adds one instance's routes to a router.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import APIRouter, Depends

if TYPE_CHECKING:
    from collections.abc import Callable

_Endpoint = TypeVar("_Endpoint", bound="Callable[..., Any]")

#: Where :func:`route` records a method's verb and path.
_ROUTE_ATTRIBUTE = "__sandbox_manager_route__"


def route(method: str, path: str) -> Callable[[_Endpoint], _Endpoint]:
    """Declare that the decorated method answers ``method`` requests for ``path``."""

    def declare(endpoint: _Endpoint) -> _Endpoint:
        setattr(endpoint, _ROUTE_ATTRIBUTE, (method, path))
        return endpoint

    return declare


def bind(router: APIRouter, routes: object, *, authenticate: Callable[..., None]) -> None:
    """Add every route ``routes`` declares to ``router``, in the order its class declares them.

    ``create_app`` passes the app's own router, so the app keeps one flat route
    table: on this FastAPI an included ``APIRouter`` becomes a lazy wrapper
    instead. ``authenticate`` runs before anything else a route depends on, as
    a per-route dependency would: a caller without the token learns nothing
    about the request's shape.
    """
    for name, member in vars(type(routes)).items():
        declared = getattr(member, _ROUTE_ATTRIBUTE, None)
        if declared is not None:
            method, path = declared
            router.add_api_route(
                path,
                getattr(routes, name),
                methods=[method],
                dependencies=[Depends(authenticate)],
            )


__all__ = ["bind", "route"]
