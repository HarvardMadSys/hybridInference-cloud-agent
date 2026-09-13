"""The sandbox-facing listener exposes the relay and nothing else.

The remote-runner stack tunnels a sandbox's MCP address to the control plane's
port, which serves every route the service has: jobs, sessions, host
registration, admin. Each of those refuses an `mcp`-scoped token, so nothing
was exploitable — but the whole trusted control plane sat one authorization
bug away from code nobody here wrote. Reachability and authorization are
different defences, and only one of them was doing any work.

`create_mcp_app` is the structural version: an application that does not
*contain* the other routes. That is stronger than each of them refusing,
because it cannot be undone by a router added tomorrow.

Which is the property this file checks. **The comparison is against
`create_app`'s actual route table, not a list written here** — a hardcoded
list of forbidden paths would pass forever while a new router quietly appeared
on both.
"""

from __future__ import annotations

import pytest

from cloud_agent.app import create_app, create_mcp_app

#: The relay's own prefix, and the liveness path a tunnel health check needs.
_MCP_PREFIX = "/v1/agent/mcp"
_ALWAYS = {"/healthz"}


def _paths(app) -> set[str]:
    """Every path the application actually serves.

    Read from the OpenAPI schema rather than by walking ``app.routes``. This
    FastAPI version wraps each ``include_router`` in an opaque
    ``_IncludedRouter`` instead of flattening its routes, so the obvious
    traversal returns four docs endpoints and ``/healthz`` — for *both*
    applications, which makes an isolation check pass by seeing nothing at
    all. It did, until a deliberately naive "is the relay even here" assertion
    caught it.
    """
    return set(app.openapi().get("paths", {}))


@pytest.fixture(scope="module")
def mcp_paths() -> set[str]:
    return _paths(create_mcp_app())


@pytest.fixture(scope="module")
def control_plane_paths() -> set[str]:
    return _paths(create_app())


def test_every_route_is_the_relay_or_liveness(mcp_paths: set[str]) -> None:
    """Nothing else is mounted, whatever else the service grows."""
    strays = sorted(
        path for path in mcp_paths if not path.startswith(_MCP_PREFIX) and path not in _ALWAYS
    )
    assert not strays, (
        "the sandbox-facing listener exposes routes outside the MCP relay: "
        f"{strays}. A sandbox reaches this port directly."
    )


def test_the_control_planes_own_routes_are_absent(
    mcp_paths: set[str], control_plane_paths: set[str]
) -> None:
    """Derived from the real route table, so a new router cannot slip past.

    This is the assertion a list of forbidden paths could not make: it asks
    what `create_app` actually serves today and requires that none of it —
    beyond the relay itself — is reachable here.
    """
    trusted = {
        path
        for path in control_plane_paths
        if not path.startswith(_MCP_PREFIX) and path not in _ALWAYS
    }
    assert trusted, "sanity: the control plane should have routes of its own"
    leaked = sorted(trusted & mcp_paths)
    assert not leaked, f"reachable from a sandbox: {leaked}"


def test_the_relay_itself_is_actually_there(mcp_paths: set[str]) -> None:
    """An empty application would pass every check above."""
    relay = sorted(path for path in mcp_paths if path.startswith(_MCP_PREFIX))
    assert relay, "the listener serves no relay routes at all"


def _relay_paths(paths: set[str]) -> set[str]:
    """The relay's own routes, told apart from the management surface by shape.

    Both live under the same prefix, and they must not: the relay's routes are
    ``/<alias>`` — one path parameter — while connection management, OAuth
    callbacks and invocation decisions are fixed literals. Deriving the
    difference from the route table rather than listing paths here is what
    keeps this honest when either surface grows.
    """
    prefix = _MCP_PREFIX + "/"
    return {
        path
        for path in paths
        if path.startswith(prefix)
        and "/" not in path[len(prefix) :]
        and path[len(prefix) :].startswith("{")
    }


def test_the_two_applications_agree_on_the_relay(
    mcp_paths: set[str], control_plane_paths: set[str]
) -> None:
    """The relay is the same router in both, not a divergent copy.

    The main application keeps serving it — a co-located deployment has no
    tunnel and reaches the relay on the ordinary port — so the two must not
    drift into two implementations with one set of tests.

    Only the relay is compared. The management surface is deliberately on the
    control plane alone, which the test below states as its own property rather
    than as an absence here.
    """
    assert _relay_paths(mcp_paths) == _relay_paths(control_plane_paths)
    assert _relay_paths(mcp_paths), "sanity: the relay must have routes of its own"


def test_connection_management_is_not_reachable_from_a_sandbox(mcp_paths: set[str]) -> None:
    """Adding a connection or approving a call is owner work, not sandbox work.

    The relay listener exists so that a sandbox reaches one surface and nothing
    else. An approval endpoint there would let the least trusted component in
    the system decide what it is allowed to do.
    """
    prefix = _MCP_PREFIX + "/"
    relay = _relay_paths(mcp_paths)
    management = {path for path in mcp_paths if path.startswith(prefix) and path not in relay}
    assert not management, f"the sandbox listener serves management routes: {sorted(management)}"


def test_the_sandbox_tunnel_lands_on_the_relay_listener() -> None:
    """The separation only holds if the tunnel actually points at it.

    A listener the deployment does not use is a comment. The remote-runner
    stack forwards the sandbox's MCP address to the control-plane host, and
    the port it names decides what a sandbox can reach — 8000 serves the whole
    control plane, 8001 serves the relay and nothing else.
    """
    import re
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    host_stack = yaml.safe_load(
        (root / "deploy" / "host" / "docker-compose.sandbox.yml").read_text()
    )
    tunnel = host_stack["services"]["mcp-tunnel"]["environment"]["TUNNEL_REMOTE_PORT"]
    default = re.search(r":-(\d+)", str(tunnel))
    assert default, f"the MCP tunnel's remote port has no default: {tunnel!r}"

    control_plane = yaml.safe_load(
        (root / "deploy" / "control-plane" / "docker-compose.yml").read_text()
    )
    served = control_plane["services"]["mcp-relay"]["command"]
    assert default.group(1) in served, (
        f"the sandbox's tunnel forwards to port {default.group(1)}, which is not "
        f"the port the MCP-only listener serves ({served})"
    )


def test_the_relay_listener_runs_no_background_work() -> None:
    """The reaper and the publisher belong to exactly one process.

    Only visible once this is deployed as a second container against the same
    database — which is precisely how it is deployed. Two copies of the reaper
    renew every live grant twice per interval, doubling the load this service
    puts on the gateway, and two publishers race for the same job.

    Checked at the lifespan the application was *built with*, because that is
    the decision: `create_mcp_app` passing `background=True` would be invisible
    in the route table, which is all the other tests here look at.
    """
    import inspect

    source = inspect.getsource(create_mcp_app)
    assert "background=False" in source, (
        "the relay listener shares the control plane's lifespan and would "
        "start a second reaper and publisher against the same database"
    )

    # And the parameter is real rather than accepted and ignored.
    from cloud_agent.app import lifespan

    assert "background" in inspect.signature(lifespan).parameters
    body = inspect.getsource(lifespan)
    assert "if background:" in body, "the flag is taken but never consulted"
