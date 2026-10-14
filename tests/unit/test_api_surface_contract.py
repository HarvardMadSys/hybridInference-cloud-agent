"""The agent API's surface, pinned: coverage, order, and the served contract.

Three properties the R3 split established and nothing would notice breaking:

* every route the surface routers in `routes.SURFACE_ROUTERS` declare is served
  under the agent prefix, and each surface is mounted exactly once;
* no two of those surfaces shadow each other — the mount order in
  `api/routes.py` is only safe because their patterns do not overlap (design
  §6.4), so a new pattern that could match a sibling's URL has to fail here
  rather than change which handler answers;
* the OpenAPI document the app produces is the one committed as
  `gateway_contracts/agent-control-plane.openapi.json`, so that a change to
  the published shape is a diff somebody chose rather than a drift.

What this file deliberately does **not** pin: absolute route positions, the
number of `_IncludedRouter` wrappers, or any function's AST. All three changed
at every step of the split while the API stayed identical, so a test that
asserted them would have been rewritten each time and would have proved nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.routing import APIRoute

from cloud_agent.api import routes
from cloud_agent.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_FILE = REPO_ROOT / "gateway_contracts" / "agent-control-plane.openapi.json"

#: The prefix every surface router carries, and this test's scope.
AGENT_PREFIX = "/v1/agent"

_METHODS = ("get", "post", "put", "patch", "delete")


def _surface_routes() -> list[tuple[APIRouter, APIRoute]]:
    """Every route the surfaces declare, with the surface it came from.

    The router object is the identity and not its prefix: every surface
    carries ``/v1/agent``, so a comparison that tells them apart by prefix
    tells nothing apart at all.
    """
    found: list[tuple[APIRouter, APIRoute]] = []
    for surface in routes.SURFACE_ROUTERS:
        for route in surface.routes:
            assert isinstance(route, APIRoute), f"{surface.prefix} holds a {type(route).__name__}"
            found.append((surface, route))
    return found


def _registrations(surfaces: list[tuple[APIRouter, APIRoute]]) -> list[tuple[str, str]]:
    """Every ``(path, method)`` declared, **duplicates included**.

    A list, not a set: collapsing the duplicates is what hid them. ``route.path``
    carries the router's prefix, so these are full paths.
    """
    return [
        (route.path, method)
        for _surface, route in surfaces
        for method in route.methods
        if method != "HEAD"
    ]


def _duplicate_registrations(surfaces: list[tuple[APIRouter, APIRoute]]) -> list[str]:
    """The ``(path, method)`` pairs more than one surface router registers."""
    seen: set[tuple[str, str]] = set()
    duplicates: set[str] = set()
    for path, method in _registrations(surfaces):
        if (path, method) in seen:
            duplicates.add(f"{method} {path}")
        seen.add((path, method))
    return sorted(duplicates)


def _shadowing_pairs(surfaces: list[tuple[APIRouter, APIRoute]]) -> list[str]:
    """Routes of two *different* surfaces whose patterns can match one URL.

    Method-agnostic on purpose: `Allow` and FastAPI's static-over-parameter
    preference both follow pattern order, so an overlap is a hazard even when
    the methods differ. Routes within one surface are not compared — an
    ordering inside a single router is that router's own business.
    """
    sample = lambda route: re.sub(r"\{[^}]+\}", "sample", route.path)  # noqa: E731
    overlapping: list[str] = []
    for left_surface, left in surfaces:
        for right_surface, right in surfaces:
            if left_surface is right_surface:
                continue
            if left.path_regex.match(sample(right)) or right.path_regex.match(sample(left)):
                overlapping.append(f"{left.path} [{left.endpoint.__name__}]")
                overlapping.append(f"{right.path} [{right.endpoint.__name__}]")
    return sorted(set(overlapping))


def _fake_surface(*paths: str) -> APIRouter:
    """A router shaped like a surface, for the negative cases below."""
    router = APIRouter(prefix=AGENT_PREFIX)
    for path in paths:
        router.add_api_route(path, lambda: None, methods=["GET"])
    return router


def _fake_surfaces(*routers: APIRouter) -> list[tuple[APIRouter, APIRoute]]:
    return [(router, route) for router in routers for route in router.routes]


def test_every_agent_route_comes_from_a_surface_router() -> None:
    """What the app publishes under the agent prefix is what the surfaces declare.

    The served side is read from the OpenAPI document and not from
    ``app.routes``: on FastAPI 0.141.1 the app holds lazy ``_IncludedRouter``
    wrappers rather than ``APIRoute``s, and the flattening that would see
    through them is a property of this version rather than of the API — the
    kind of check that has to be rewritten when the framework changes and
    proves nothing about the surface in the meantime.
    """
    document = create_app().openapi()
    served = {
        (path, method.upper())
        for path, item in document["paths"].items()
        if path.startswith(AGENT_PREFIX)
        for method in item
        if method in _METHODS
    }
    declared = set(_registrations(_surface_routes()))

    # The app also mounts routers this tuple does not own — hosts, usage, the
    # MCP relay, the admin surfaces — so the surfaces are a subset of what is
    # served. That each of them still *is* served is what this asserts: a
    # router dropped from the tuple, or mounted under a doubled prefix, stops
    # being covered here (``/v1/agent/v1/agent/...`` is what that looked like).
    # That none of them was lost outright is the contract file's job below: the
    # document it pins is the whole published surface.
    assert declared <= served, f"declared but not served: {sorted(declared - served)}"


def test_no_surface_router_registers_a_route_twice() -> None:
    """Two surfaces claiming one method and path is a silent overwrite.

    Counted over the raw registrations, not by comparing two sets: the previous
    version of this check asserted ``len(declared) == len(declared)``, because
    both sides were the same deduplicated comprehension. A duplicate collapses
    into the set and the lengths stay equal however many routers contributed it.
    """
    duplicates = _duplicate_registrations(_surface_routes())
    assert not duplicates, (
        "two surface routers register the same method and path, so the mount order "
        "in api/routes.py decides which handler answers:\n" + "\n".join(duplicates)
    )


def test_the_duplicate_check_sees_a_duplicate() -> None:
    """Negative case for the check above, which passes on the tree either way."""
    surfaces = _fake_surfaces(_fake_surface("/jobs"), _fake_surface("/jobs"))
    assert _duplicate_registrations(surfaces) == [f"GET {AGENT_PREFIX}/jobs"]


def test_no_two_surfaces_shadow_each_other() -> None:
    """No surface's pattern can match another surface's URL.

    The pairs are told apart by router identity. They used to be skipped by
    comparing prefixes — and every surface carries ``/v1/agent``, so every
    pair was skipped and the loop below compared nothing.

    The overlaps between the *other* routers the app mounts (`mcp/catalog`
    against `mcp/{server_name}`, for one) predate this split and are not this
    test's subject — the surfaces in ``SURFACE_ROUTERS`` are.
    """
    overlapping = _shadowing_pairs(_surface_routes())

    assert not overlapping, (
        "these patterns can match one URL, so the mount order in api/routes.py now "
        "decides which handler answers; prove the order is safe before adding one:\n"
        + "\n".join(overlapping)
    )


def test_the_shadow_check_sees_an_overlap_between_two_surfaces() -> None:
    """Negative case: two surfaces, same prefix, one pattern matching the other."""
    surfaces = _fake_surfaces(_fake_surface("/jobs/archived"), _fake_surface("/jobs/{job_id}"))

    assert _shadowing_pairs(surfaces), (
        "an overlap between two routers that share the /v1/agent prefix must not be "
        "skipped, which is what comparing prefixes did"
    )


def test_the_shadow_check_sees_same_path_with_different_methods() -> None:
    """Separate methods still make the 405 Allow header depend on mount order."""
    get_surface = _fake_surface("/jobs")
    post_surface = APIRouter(prefix=AGENT_PREFIX)
    post_surface.add_api_route("/jobs", lambda: None, methods=["POST"])
    surfaces = _fake_surfaces(get_surface, post_surface)

    assert not _duplicate_registrations(surfaces)
    assert _shadowing_pairs(surfaces), (
        "different methods at the same path are not duplicate registrations, "
        "but the first surface decides the Allow header for an unsupported method"
    )


def test_host_drain_contract_publishes_the_required_epoch_cas() -> None:
    document = create_app().openapi()
    schemas = document["components"]["schemas"]
    host = schemas["AgentRunnerHost"]
    acquire = schemas["AcquireHostDrainFenceRequest"]

    assert "drain_fence_epoch" in host["required"]
    assert host["properties"]["drain_fence_epoch"]["format"] == "uuid"
    assert "expected_fence_epoch" in acquire["required"]
    assert acquire["properties"]["expected_fence_epoch"]["format"] == "uuid"


def test_the_served_openapi_is_the_committed_contract() -> None:
    """A shape change is a deliberate edit to the contract file, not a drift."""
    committed = json.loads(CONTRACT_FILE.read_text())
    served = create_app().openapi()

    assert served == committed, (
        "the served OpenAPI no longer matches "
        f"{CONTRACT_FILE.relative_to(REPO_ROOT)}.\n"
        "If the change is intended, regenerate it with:\n"
        '    python -c "import json,pathlib;from cloud_agent.app import create_app;'
        f"pathlib.Path('{CONTRACT_FILE.relative_to(REPO_ROOT)}').write_text("
        "json.dumps(create_app().openapi(), sort_keys=True, indent=2, "
        'ensure_ascii=False) + \\"\\n\\")"\n'
        "and say in the pull request what moved."
    )
