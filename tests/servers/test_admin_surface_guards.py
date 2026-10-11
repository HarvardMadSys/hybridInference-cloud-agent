"""Every ``/admin`` path is behind ``verify_admin_access``, by construction.

The admin routers declare their guard on the router, so a route added to an
admin surface inherits it — but nothing above the router enforces that a *new*
admin router declares one at all. This walks the assembled application, not
the source modules: it sees the routes ``create_app`` actually serves, prefix
composition included, and fails naming the exact path that would answer
without the guard.

The owner surface has its own registry-driven check in
``test_owner_revalidation.py``; the two agree that a browser session reaches
exactly the surfaces mounted on the main application.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute

from cloud_agent.api.deps import verify_admin_access


def _flatten(routes: list[Any]) -> list[Any]:
    """Read served paths and dependency trees, including include-time settings.

    Reading ``original_router.routes`` loses prefixes and dependencies supplied
    by ``include_router``. FastAPI's effective contexts preserve both, even for
    nested includes and routes omitted from OpenAPI.
    """
    resolved: list[Any] = []
    for route in routes:
        if isinstance(route, APIRoute):
            resolved.append(route)
            continue
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            resolved.extend(
                context for context in contexts() if isinstance(context.original_route, APIRoute)
            )
    return resolved


def _has_admin_gate(route: Any) -> bool:
    """Require the real guard callable, including inherited dependencies."""
    stack = list(getattr(route.dependant, "dependencies", []))
    while stack:
        current = stack.pop()
        if current.call is verify_admin_access:
            return True
        stack.extend(getattr(current, "dependencies", None) or [])
    return False


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def _admin_routes(app: FastAPI | None = None) -> list[Any]:
    """Every served route whose path is under ``/admin``."""
    if app is None:
        from cloud_agent.app import create_app

        app = create_app()
    return [route for route in _flatten(app.routes) if _is_admin_path(route.path)]


def test_every_admin_route_authenticates_the_admin_gate():
    """A route under ``/admin`` without the gate is a finding, not a style note."""
    unguarded = [
        f"{sorted(route.methods)} {route.path}"
        for route in _admin_routes()
        if not _has_admin_gate(route)
    ]
    assert not unguarded, (
        "these /admin routes do not authenticate verify_admin_access — add it "
        f"to the router's dependencies so the next route inherits it: {unguarded}"
    )


def _included_routers(routes: list[Any]) -> list[Any]:
    included: list[Any] = []
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if candidates is not None:
            included.append(route)
            included.extend(_included_routers(candidates()))
    return included


def test_admin_routers_guard_future_routes():
    """Existing per-handler guards cannot protect a new handler by default."""
    from cloud_agent.app import create_app

    included = _included_routers(create_app().routes)
    assert included, "the route walk cannot inspect included routers"
    unguarded = []
    for route in included:
        paths = [
            context.path
            for context in route.effective_candidates()
            if isinstance(getattr(context, "original_route", None), APIRoute)
            and _is_admin_path(context.path)
        ]
        dependencies = [*route.include_context.dependencies, *route.original_router.dependencies]
        if not any(dependency.dependency is verify_admin_access for dependency in dependencies):
            unguarded.extend(paths)
    assert not unguarded, f"these admin surfaces need a router-level guard: {unguarded}"


def test_the_admin_prefix_has_routes():
    """The walk above is not answering an empty question.

    A refactor that stopped mounting the admin routers, or that changed how
    ``include_router`` wraps them, would leave the first test passing over
    zero routes — the same silence as no test. The surfaces listed are the
    ones the frontend's admin pages call today; if one moves, the move is a
    frontend change too, and this names it.
    """
    paths = {route.path for route in _admin_routes()}
    assert paths, "the /admin routers are not mounted, or the walk cannot see them"
    for expected in (
        "/admin/agent/runs",
        "/admin/agent/runner-hosts",
        "/admin/agent/audit-log",
        "/admin/agent/usage",
        "/admin/agent/steps",
        "/admin/agent/conversations",
    ):
        assert expected in paths, f"{expected} is no longer served; the admin UI calls it"


def test_nested_includes_preserve_the_served_path_and_guard():
    """The mount supplies the admin prefix and permission, not the leaf router."""
    leaf = APIRouter()

    @leaf.get("/probe", include_in_schema=False)
    async def probe():
        return {}

    parent = APIRouter()
    parent.include_router(leaf, prefix="/agent")
    app = FastAPI()
    app.include_router(parent, prefix="/admin", dependencies=[Depends(verify_admin_access)])

    routes = _flatten(app.routes)
    matching = [route for route in routes if route.path == "/admin/agent/probe"]
    assert len(matching) == 1
    assert _has_admin_gate(matching[0])


def test_an_unguarded_route_under_an_include_prefix_is_detected():
    leaf = APIRouter()

    @leaf.get("/probe")
    async def probe():
        return {}

    app = FastAPI()
    app.include_router(leaf, prefix="/admin")
    routes = _admin_routes(app)
    assert len(routes) == 1
    assert routes[0].path == "/admin/probe"
    assert not _has_admin_gate(routes[0])


def test_a_dependency_named_like_the_guard_does_not_count():
    async def impostor():
        return "not-an-admin"

    impostor.__name__ = verify_admin_access.__name__
    app = FastAPI()

    @app.get("/admin/probe", dependencies=[Depends(impostor)])
    async def probe():
        return {}

    assert not _has_admin_gate(_admin_routes(app)[0])
