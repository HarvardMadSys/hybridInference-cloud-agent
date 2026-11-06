"""The MCP relay's route table, frozen from before ``api/mcp_relay.py`` was split.

The relay's helpers moved into ``mcp/relay_surface.py`` and private
``api/_mcp_relay_*`` modules; its two route functions stayed. The split was
meant to change nothing a sandbox can see, and the relay listener publishes no
OpenAPI document that ``apps/backend/openapi.json`` would pin, so this pins
what the router served: each route's method and path, its handler's name and
module, the response model and status code FastAPI derives, its body model,
query and header parameters, and the dependencies that run before it.
"""

from __future__ import annotations

from typing import Any

from fastapi.routing import APIRoute

from cloud_agent.api import mcp_relay
from cloud_agent.app import create_mcp_app

_ROUTE = tuple[
    str,
    str,
    str,
    str,
    str,
    int | None,
    str | None,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]

#: (method, path, handler, handler module, response model, status code, body
#: model, query parameters, header parameters, dependencies), in router order.
ROUTES: tuple[_ROUTE, ...] = (
    (
        "POST",
        "/v1/agent/mcp",
        "mcp_host_post",
        "cloud_agent.api.mcp_relay",
        "typing.Any",
        None,
        None,
        (),
        ("authorization",),
        ("get_storage",),
    ),
    (
        "GET",
        "/v1/agent/mcp",
        "mcp_host_get",
        "cloud_agent.api.mcp_relay",
        "typing.Any",
        None,
        None,
        (),
        (),
        (),
    ),
)


def _body_model(route: APIRoute) -> str | None:
    if route.body_field is None:
        return None
    annotation: Any = route.body_field.field_info.annotation
    return getattr(annotation, "__name__", repr(annotation))


def _served(route: APIRoute) -> _ROUTE:
    (method,) = route.methods
    return (
        method,
        route.path,
        route.name,
        route.endpoint.__module__,
        str(route.response_model),
        route.status_code,
        _body_model(route),
        tuple(param.name for param in route.dependant.query_params),
        tuple(param.alias for param in route.dependant.header_params),
        tuple(
            getattr(dep.call, "__name__", repr(dep.call)) for dep in route.dependant.dependencies
        ),
    )


def test_the_router_serves_exactly_the_frozen_routes_in_order() -> None:
    routes = mcp_relay.router.routes
    assert all(isinstance(route, APIRoute) for route in routes)
    assert [_served(route) for route in routes if isinstance(route, APIRoute)] == list(ROUTES)


def test_the_relay_listener_serves_the_router_and_its_health_check_only() -> None:
    """The second listener publishes exactly these operations, under these ids."""
    paths = create_mcp_app().openapi()["paths"]
    served = sorted(
        (path, method.upper(), operation["operationId"])
        for path, item in paths.items()
        for method, operation in item.items()
    )
    assert served == [
        ("/healthz", "GET", "healthz_healthz_get"),
        ("/v1/agent/mcp", "GET", "mcp_host_get_v1_agent_mcp_get"),
        ("/v1/agent/mcp", "POST", "mcp_host_post_v1_agent_mcp_post"),
    ]
