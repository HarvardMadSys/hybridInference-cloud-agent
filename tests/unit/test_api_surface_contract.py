"""The agent API's surface, pinned: coverage, order, and the served contract.

Three properties the R3 split established and nothing would notice breaking:

* every route the application serves under the agent prefix comes from one of
  the eight surface routers, and each surface is mounted exactly once;
* no two of those surfaces shadow each other — the mount order in
  `api/routes.py` is only safe because their patterns do not overlap (design
  §6.4), so a new pattern that could match a sibling's URL has to fail here
  rather than change which handler answers;
* the OpenAPI document the app produces is the one committed as
  `contracts/agent-control-plane.openapi.json`, key-sorted, so that a change to
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

from fastapi.routing import APIRoute

from cloud_agent.api import routes
from cloud_agent.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_FILE = REPO_ROOT / "contracts" / "agent-control-plane.openapi.json"

#: The prefix every surface router carries, and this test's scope.
AGENT_PREFIX = "/v1/agent"

_METHODS = ("get", "post", "put", "patch", "delete")


def _surface_routes() -> list[tuple[str, APIRoute]]:
    """Every route the eight surfaces declare, with the surface it came from."""
    found: list[tuple[str, APIRoute]] = []
    for surface in routes.SURFACE_ROUTERS:
        for route in surface.routes:
            assert isinstance(route, APIRoute), f"{surface.prefix} holds a {type(route).__name__}"
            found.append((surface.prefix, route))
    return found


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
    declared = {
        (route.path, method)
        for _prefix, route in _surface_routes()
        for method in route.methods
        if method != "HEAD"
    }

    # The app also mounts routers this tuple does not own — hosts, usage, the
    # MCP relay, the admin surfaces — so the surfaces are a subset of what is
    # served. That each of the eight still *is* served is what this asserts: a
    # router dropped from the tuple, or mounted under a doubled prefix, stops
    # being covered here (``/v1/agent/v1/agent/...`` is what that looked like).
    # That none of them was lost outright is the contract file's job below: the
    # document it pins is the whole published surface.
    assert declared <= served, f"declared but not served: {sorted(declared - served)}"
    assert len(declared) == len(
        {(route.path, method) for _prefix, route in _surface_routes() for method in route.methods}
    ), "two surface routers declare the same method and path"


def test_no_two_surfaces_shadow_each_other() -> None:
    """No surface's pattern can match another surface's URL.

    Method-agnostic on purpose: `Allow` and FastAPI's static-over-parameter
    preference both follow pattern order, so an overlap is a hazard even when
    the methods differ. The overlaps between the *other* routers the app mounts
    (`mcp/catalog` against `mcp/{server_name}`, for one) predate this split and
    are not this test's subject — these eight are.
    """
    sample = lambda route: re.sub(r"\{[^}]+\}", "sample", route.path)  # noqa: E731
    overlapping: list[str] = []
    for prefix, left in _surface_routes():
        for other_prefix, right in _surface_routes():
            if left is right or left.path == right.path or prefix == other_prefix:
                continue
            if left.path_regex.match(sample(right)) or right.path_regex.match(sample(left)):
                overlapping.append(f"{prefix}{left.path} [{left.endpoint.__name__}]")
                overlapping.append(f"{other_prefix}{right.path} [{right.endpoint.__name__}]")

    assert not overlapping, (
        "these patterns can match one URL, so the mount order in api/routes.py now "
        "decides which handler answers; prove the order is safe before adding one:\n"
        + "\n".join(sorted(set(overlapping)))
    )


def test_the_served_openapi_is_the_committed_contract() -> None:
    """A shape change is a deliberate edit to the contract file, not a drift."""
    committed = json.loads(CONTRACT_FILE.read_text())
    served = create_app().openapi()

    assert served == committed, (
        "the served OpenAPI no longer matches "
        f"{CONTRACT_FILE.relative_to(REPO_ROOT)}.\n"
        "If the change is intended, regenerate it with:\n"
        '    python -c "import json,pathlib;from cloud_agent.app import create_app;'
        "pathlib.Path('contracts/agent-control-plane.openapi.json').write_text("
        "json.dumps(create_app().openapi(), sort_keys=True, indent=2, "
        'ensure_ascii=False) + "\\n")"\n'
        "and say in the pull request what moved."
    )


def test_the_contract_file_is_key_sorted() -> None:
    """Key-sorted, so a diff is the change and not the insertion order."""
    text = CONTRACT_FILE.read_text()
    document = json.loads(text)

    assert text == json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n", (
        "the contract file is not in the canonical key-sorted form; the "
        "regeneration command is in test_the_served_openapi_is_the_committed_contract"
    )
    operations = sum(
        len([method for method in item if method in _METHODS])
        for item in document["paths"].values()
    )
    assert operations > 0 and document["paths"], "the contract file lost its paths"
