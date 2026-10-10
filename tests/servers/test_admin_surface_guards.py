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

from fastapi.routing import APIRoute

from cloud_agent.api.deps import verify_admin_access

#: The one guard an admin route may authenticate with, named by the call
#: itself: a prefix check here would pass whatever the next router put under
#: ``/admin``, which is the failure this test exists to catch.
GATE = verify_admin_access.__name__


def _flatten(routes: list) -> list[APIRoute]:
    """Resolve the lazy include wrappers ``include_router`` leaves behind.

    FastAPI mounts routers as ``_IncludedRouter`` wrappers whose ``routes`` are
    more wrappers, so filtering ``app.routes`` for ``APIRoute`` finds none of
    them — the same silence this test exists to catch elsewhere. Each wrapper
    names the router it wraps, which is the real route table.
    """
    resolved: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            resolved.append(route)
            continue
        original = getattr(route, "original_router", None)
        if original is not None and original is not route:
            resolved.extend(_flatten(original.routes))
    return resolved


def _dependency_names(route: APIRoute) -> set[str]:
    """Every callable a route's dependency tree names, router dependencies included.

    ``route.dependencies`` holds the ``Depends`` objects the router declared
    and ``route.dependant`` the ones the handler did; walking only the second
    is how a router-level guard would be missed here.
    """
    names = {getattr(dependency.dependency, "__name__", "") for dependency in route.dependencies}
    stack = list(getattr(route.dependant, "dependencies", []))
    while stack:
        current = stack.pop()
        names.add(getattr(current.call, "__name__", ""))
        stack.extend(getattr(current, "dependencies", None) or [])
    return names


def _admin_routes() -> list[APIRoute]:
    """Every served route whose path is under ``/admin``."""
    from cloud_agent.app import create_app

    return [route for route in _flatten(create_app().routes) if route.path.startswith("/admin")]


def test_every_admin_route_authenticates_the_admin_gate():
    """A route under ``/admin`` without the gate is a finding, not a style note."""
    unguarded = [
        f"{sorted(route.methods)} {route.path}"
        for route in _admin_routes()
        if GATE not in _dependency_names(route)
    ]
    assert not unguarded, (
        "these /admin routes do not authenticate verify_admin_access — add it "
        f"to the router's dependencies so the next route inherits it: {unguarded}"
    )


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
