"""The MCP owner API's route table, frozen from before its routes were split out.

``cloud_agent.api.mcp_connections`` used to declare every connection, OAuth and
invocation route itself; the OAuth and invocation routes now live in sibling
modules whose routes it copies into its one router, as it already did for the
thread bindings. The split was meant to change nothing a caller can see, so
this pins what the router served: each route's methods and path, its handler's
name, its response model and status code, its body model and query
parameters, the dependencies that run before it, and the order of the
patterns that can match one URL.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi.routing import APIRoute

from cloud_agent.api.mcp_connections import router

_P = "/v1/agent/mcp"
_OWNER = ("require_agent_owner", "get_storage")
_ANY = "dict[str, Any]"

#: (methods, path, handler, response model, status code, body model, query
#: parameters, dependencies), in the order the router registered them.
ROUTES: tuple[tuple[Any, ...], ...] = (
    (
        ("GET",),
        f"{_P}/catalog",
        "list_catalog",
        _ANY,
        None,
        None,
        ("search", "offset", "limit", "recommended"),
        ("require_agent_owner",),
    ),
    (
        ("GET",),
        f"{_P}/connections",
        "list_connections",
        "ConnectionsResponse",
        None,
        None,
        (),
        _OWNER,
    ),
    (
        ("POST",),
        f"{_P}/connections",
        "create_connection",
        "ConnectionView",
        201,
        "CreateConnectionRequest",
        (),
        _OWNER,
    ),
    (
        ("POST",),
        f"{_P}/connections/import",
        "import_connections",
        "ImportPreview",
        None,
        "ImportRequest",
        (),
        _OWNER,
    ),
    (
        ("GET",),
        f"{_P}/connections/{{connection_id}}",
        "get_connection",
        "ConnectionView",
        None,
        None,
        (),
        _OWNER,
    ),
    (
        ("PATCH",),
        f"{_P}/connections/{{connection_id}}",
        "update_connection",
        "ConnectionView",
        None,
        "UpdateConnectionRequest",
        (),
        _OWNER,
    ),
    (
        ("PUT",),
        f"{_P}/connections/{{connection_id}}/enabled",
        "set_connection_enabled",
        "ConnectionView",
        None,
        "EnableConnectionRequest",
        (),
        _OWNER,
    ),
    (
        ("DELETE",),
        f"{_P}/connections/{{connection_id}}",
        "delete_connection",
        "None",
        204,
        None,
        (),
        _OWNER,
    ),
    (
        ("POST",),
        f"{_P}/connections/{{connection_id}}/secret",
        "set_connection_secret",
        "ConnectionView",
        None,
        "SecretRequest",
        (),
        _OWNER,
    ),
    (
        ("POST",),
        f"{_P}/connections/{{connection_id}}/test",
        "test_connection",
        "TestResult",
        None,
        None,
        (),
        _OWNER,
    ),
    (
        ("GET",),
        f"{_P}/connections/{{connection_id}}/capabilities",
        "get_capabilities",
        "list[dict[str, Any]]",
        None,
        None,
        (),
        _OWNER,
    ),
    (
        ("POST",),
        f"{_P}/connections/{{connection_id}}/authorize",
        "authorize_connection",
        "AuthorizeResponse",
        None,
        "AuthorizeRequest | None",
        (),
        _OWNER,
    ),
    (
        ("GET",),
        f"{_P}/oauth/callback/{{provider}}",
        "oauth_callback",
        "None",
        None,
        None,
        ("state", "code", "error"),
        ("get_storage", "_callback_session_user"),
    ),
    (
        ("GET",),
        f"{_P}/invocations",
        "list_invocations",
        "InvocationsResponse",
        None,
        None,
        ("job_id", "thread_id", "pending_only", "limit", "before_id"),
        _OWNER,
    ),
    (
        ("POST",),
        f"{_P}/invocations/{{invocation_id}}/decision",
        "decide_invocation",
        "InvocationView",
        None,
        "DecisionRequest",
        (),
        _OWNER,
    ),
    (
        ("GET",),
        f"{_P}/threads/{{thread_id}}/bindings",
        "get_thread_bindings",
        "BindingsResponse",
        None,
        None,
        (),
        _OWNER,
    ),
    (
        ("PUT",),
        f"{_P}/threads/{{thread_id}}/bindings",
        "replace_thread_bindings",
        "BindingsResponse",
        None,
        "ReplaceBindingsRequest",
        (),
        _OWNER,
    ),
    (
        ("PUT",),
        f"{_P}/threads/{{thread_id}}/bindings/{{connection_id}}",
        "update_thread_binding",
        "BindingsResponse",
        None,
        "BindingRequest",
        (),
        _OWNER,
    ),
)

#: The dependency the OAuth callback reads its browser's session through, and
#: what it in turn depends on.
CALLBACK_SESSION_DEPENDENCIES = ("get_storage",)


def _routes() -> list[APIRoute]:
    routes = list(router.routes)
    assert all(isinstance(route, APIRoute) for route in routes)
    return routes  # type: ignore[return-value]


def _name(annotation: Any) -> str:
    """A type as its names spell it, whichever module now defines it."""
    if isinstance(annotation, type):
        return annotation.__name__
    return re.sub(r"(?:\b\w+\.)+(\w+)", r"\1", str(annotation))


def _body_model(route: APIRoute) -> str | None:
    if route.body_field is None:
        return None
    return _name(route.body_field.field_info.annotation)


def _served(route: APIRoute) -> tuple[Any, ...]:
    return (
        tuple(sorted(route.methods)),
        route.path,
        route.name,
        _name(route.response_model),
        route.status_code,
        _body_model(route),
        tuple(param.name for param in route.dependant.query_params),
        tuple(dependency.call.__name__ for dependency in route.dependant.dependencies),
    )


def test_the_router_serves_exactly_the_frozen_routes_in_order() -> None:
    assert [_served(route) for route in _routes()] == list(ROUTES)


def test_every_route_keeps_its_tag_and_stays_in_the_schema() -> None:
    for route in _routes():
        assert route.tags == ["agent-mcp"]
        assert route.include_in_schema


def test_the_callback_still_reads_the_session_through_the_store() -> None:
    (callback,) = [route for route in _routes() if route.name == "oauth_callback"]
    session = callback.dependant.dependencies[1]
    assert session.call.__name__ == "_callback_session_user"
    assert tuple(dep.call.__name__ for dep in session.dependencies) == (
        CALLBACK_SESSION_DEPENDENCIES
    )
    assert [param.name for param in session.query_params] == []
