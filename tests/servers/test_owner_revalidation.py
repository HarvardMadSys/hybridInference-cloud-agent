"""Every browser read/write requires a current, explicit Gateway permission."""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any

import pytest
from fastapi import Depends, FastAPI, Header
from httpx import ASGITransport, AsyncClient
from tests.servers.test_admin_surface_guards import _flatten

from cloud_agent import identity
from cloud_agent.api._owner_auth import (
    authenticate_agent_owner,
    require_agent_owner,
)
from cloud_agent.api.deps import (
    AGENT_ACCESS_ALLOW_TTL_S,
    AgentAccessAdmission,
    get_gateway_client,
    verify_admin_access,
)
from cloud_agent.api.hosts import authenticate_host
from cloud_agent.api.service_jobs import verify_oncall_dispatch
from cloud_agent.api.worker import _worker_claims, verify_claiming_host
from cloud_agent.gateway import GatewayError

pytestmark = pytest.mark.asyncio
_OWNER = "user_1"


class _AccessGateway:
    def __init__(
        self,
        permissions: list[str] | None = None,
        error: int | None = None,
        error_type: str | None = None,
    ):
        self.permissions = permissions if permissions is not None else ["agent.use"]
        self.error = error
        self.error_type = error_type
        self.asked: list[str] = []

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        self.asked.append(user_id)
        if self.error is not None:
            raise GatewayError("unavailable", status_code=self.error, error_type=self.error_type)
        return {
            "user_id": user_id,
            "allowed": "agent.use" in self.permissions,
            "permissions": self.permissions,
        }


def _app(gateway: _AccessGateway, role: str = "admin") -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    @app.post("/probe")
    async def owner(user: dict[str, Any] = Depends(require_agent_owner)):
        return user

    @app.get("/admin/probe")
    @app.post("/admin/probe")
    async def admin(actor: str = Depends(verify_admin_access)):
        return {"actor": actor}

    app.dependency_overrides[authenticate_agent_owner] = lambda: {
        "user_id": _OWNER,
        "role": role,
        "authenticated": True,
    }
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    return app


@pytest.mark.parametrize("role", ["free", "pro", "internal", "admin", "custom"])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_gateway_allowed_owners_work_independently_of_role(role, method):
    gateway = _AccessGateway()
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway, role)), base_url="https://test"
    ) as client:
        response = await client.request(method, "/probe")
    assert response.status_code == 200
    assert response.json()["agent_access"] == {"allowed": True, "permissions": ["agent.use"]}
    assert response.json()["role"] == role
    # No admin flag rides along with the owner identity: admin authority is
    # answered by verify_admin_access on the admin surface, never by a field
    # an owner route could branch on.
    assert "is_admin" not in response.json()
    assert gateway.asked == [_OWNER]


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_an_old_admin_session_cannot_bypass_a_denial(method):
    gateway = _AccessGateway([])
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        response = await client.request(method, "/probe")
    assert response.status_code == 403
    assert gateway.asked == [_OWNER]


@pytest.mark.parametrize(
    "status, error_type, expected",
    [
        (401, None, 503),
        (403, None, 503),
        (403, "browser_check", 503),
        (403, "subject_unavailable", 403),
        (404, None, 503),
        (503, None, 503),
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_unavailable_policy_never_falls_back_to_session_role(
    status, error_type, expected, method
):
    gateway = _AccessGateway(error=status, error_type=error_type)
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        response = await client.request(method, "/probe")
    assert response.status_code == expected


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


async def test_a_withdrawal_stops_admitting_requests_once_the_kept_grant_expires():
    gateway = _AccessGateway()
    clock = _Clock()
    app = _app(gateway)
    app.state.agent_access_admission = AgentAccessAdmission(clock=clock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        assert (await client.get("/probe")).status_code == 200
        gateway.permissions = []
        # Inside the bound, the grant the Gateway gave a moment ago admits.
        clock.now += AGENT_ACCESS_ALLOW_TTL_S - 0.5
        assert (await client.get("/probe")).status_code == 200
        assert gateway.asked == [_OWNER]
        # Past it, the Gateway is asked again, and its denial is never kept.
        clock.now += 1
        assert (await client.get("/probe")).status_code == 403
        assert (await client.post("/probe")).status_code == 403
    assert gateway.asked == [_OWNER, _OWNER, _OWNER]


async def test_a_grant_is_used_at_once_after_a_denial():
    gateway = _AccessGateway(permissions=[])
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        assert (await client.get("/probe")).status_code == 403
        gateway.permissions = ["agent.use"]
        assert (await client.get("/probe")).status_code == 200
    assert gateway.asked == [_OWNER, _OWNER]


async def test_an_unavailable_gateway_is_asked_again_and_admits_nothing():
    gateway = _AccessGateway(error=503)
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        assert (await client.get("/probe")).status_code == 503
        assert (await client.get("/probe")).status_code == 503
    assert gateway.asked == [_OWNER, _OWNER]


async def test_the_admin_surface_asks_the_gateway_on_every_request(admin_env):
    gateway = _AccessGateway(permissions=["agent.use", "agent.admin"])
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        client.cookies.set(identity.SESSION_COOKIE, identity.mint_session(user_id=_OWNER))
        assert (await client.get("/probe")).status_code == 200
        assert (await client.get("/admin/probe")).status_code == 200
        gateway.permissions = ["agent.use"]
        assert (await client.get("/admin/probe")).status_code == 403
    assert gateway.asked == [_OWNER, _OWNER, _OWNER]


def _gates(route: Any) -> list[Any]:
    """Every dependency callable reachable from a route, inherited ones included."""
    calls: list[Any] = []
    stack = list(getattr(route.dependant, "dependencies", []))
    while stack:
        dependant = stack.pop()
        call = getattr(dependant, "call", None)
        if call is not None:
            calls.append(call)
        stack.extend(getattr(dependant, "dependencies", None) or [])
    return calls


#: Gates that re-ask the gateway before they allow a request. Compared by
#: identity, so a dependency that merely shares the name does not count.
#: verify_admin_access is a browser gate too: its session path requires a fresh
#: agent.admin decision, even though ADMIN_TOKEN needs no user lookup.
REVALIDATING_GATES = (require_agent_owner, verify_admin_access)

#: What a machine-to-machine route authenticates with instead. These carry
#: their own capability credential and have no browser session behind them to
#: have gone stale, so revalidating a *user* on them would be checking the
#: wrong thing. Named by the call each route actually makes, not by URL prefix:
#: a prefix exempts whatever is put under it, which is the same "the next one
#: falls off the list" failure this test exists to catch.
MACHINE_GATES = (verify_claiming_host, authenticate_host, verify_oncall_dispatch)

#: Routes that answer without a session or a machine credential, on purpose.
#: Keyed by method and served path, so nothing inherits an exemption by sharing
#: a prefix, and each one says why it needs none.
PUBLIC_ROUTES = {
    ("GET", "/healthz"): "liveness only; it reads nothing about anyone",
    ("GET", "/v1/agent/site-config"): "the deployment's public appearance, read before sign-in",
    ("GET", "/v1/session/login"): "starts sign-in, so there is no session yet",
    ("GET", "/v1/session/callback"): "finishes sign-in, creating the session",
    ("POST", "/v1/session/logout"): "only ends the session it was sent with",
    ("GET", "/v1/session/me"): "asks the gateway for a fresh decision in its handler",
    ("POST", "/v1/agent/host/register"): "redeems a one-time enrollment token",
    ("POST", "/v1/compute/node-registrations"): "redeems a node's one-time registration token",
    ("GET", "/v1/agent/mcp/oauth/callback/{provider}"): (
        "finishes only the OAuth flow the signed-in account started"
    ),
}


def _calls_worker_claims(endpoint: Any) -> bool:
    """Whether the handler itself verifies a worker token.

    Worker routes check their capability token in the handler rather than
    through a dependency, so the dependency tree cannot see it. A call is
    required, not a mention: a comment or a docstring naming the gate verifies
    nothing.
    """
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    except (OSError, TypeError):  # pragma: no cover - defensive
        return False
    calls = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_worker_claims"
        for node in ast.walk(tree)
    )
    return calls and getattr(endpoint, "__globals__", {}).get("_worker_claims") is _worker_claims


def _unguarded(routes: list[Any]) -> list[str]:
    """``METHOD path`` for each route no gate covers and no exemption names."""
    unguarded: list[str] = []
    for route in routes:
        calls = _gates(route)
        if any(call is gate for call in calls for gate in (*REVALIDATING_GATES, *MACHINE_GATES)):
            continue
        if _calls_worker_claims(route.endpoint):
            continue
        unguarded.extend(
            f"{method} {route.path}"
            for method in sorted(route.methods)
            if (method, route.path) not in PUBLIC_ROUTES
        )
    return unguarded


def _served_routes() -> list[Any]:
    """Every route the main application serves, with include-time settings."""
    from cloud_agent.app import create_app

    return _flatten(create_app().routes)


async def test_every_route_a_session_can_reach_revalidates():
    """No route gets to skip this by being added later, or by being a read.

    Walks the application ``create_app`` assembles, every route and every
    method, rather than a list of routers: usage, skills, uploads and the MCP
    connections are mounted directly on the app, and a hand-kept list is the
    thing the next router falls off. A route wired to
    ``authenticate_agent_owner`` directly, or to nothing at all, is one that
    never re-asks.
    """
    routes = _served_routes()
    served = {(method, route.path) for route in routes for method in route.methods}
    # The walk is not answering an empty question: owner reads and writes from
    # surfaces mounted in different places are all in it.
    for expected in (
        ("POST", "/v1/agent/jobs"),
        ("GET", "/v1/agent/jobs/{job_id}"),
        ("GET", "/v1/agent/mcp/connections"),
        ("GET", "/v1/agent/jobs/{job_id}/usage"),
        ("GET", "/v1/agent/skills"),
        ("POST", "/v1/agent/uploads"),
    ):
        assert expected in served, f"{expected} is not served; the walk cannot see it"
    stale = sorted(set(PUBLIC_ROUTES) - served)
    assert not stale, f"exempted but no longer served; drop the exemption: {stale}"

    unguarded = _unguarded(routes)
    assert not unguarded, (
        "these routes authenticate nothing that can go stale — wire them to a "
        f"revalidating gate or to a machine credential: {unguarded}"
    )


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/agent/stale-session")
    async def stale_session(user: dict[str, Any] = Depends(authenticate_agent_owner)):
        return user

    async def impostor() -> dict[str, Any]:
        return {}

    impostor.__name__ = require_agent_owner.__name__

    @app.post("/v1/agent/impostor", dependencies=[Depends(impostor)])
    async def impersonated():
        return {}

    @app.post("/v1/agent/worker/jobs/{job_id}/mentioned")
    async def mentioned(job_id: str, authorization: str | None = Header(None)):
        """Claims to be checked by _worker_claims, which it never calls."""
        return {"job_id": job_id}

    @app.post("/v1/agent/worker/jobs/{job_id}/checked")
    async def checked(job_id: str, authorization: str | None = Header(None)):
        claims = _worker_claims(authorization)
        return {"job_id": job_id, "claims": claims}

    return app


async def test_the_walk_sees_a_read_a_name_and_a_mention_for_what_they_are():
    """Negative cases for the check above, which passes on the tree either way.

    A read behind the non-revalidating gate, a dependency named like the
    revalidating one, and a worker route that only mentions its gate are all
    unguarded; a worker route that calls the real gate is not.
    """
    unguarded = _unguarded(_flatten(_probe_app().routes))

    assert sorted(unguarded) == [
        "GET /v1/agent/stale-session",
        "POST /v1/agent/impostor",
        "POST /v1/agent/worker/jobs/{job_id}/mentioned",
    ]


@pytest.fixture()
def admin_env(monkeypatch):
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "admin-test-secret-long-enough-for-hs256")
    monkeypatch.setenv("ADMIN_TOKEN", "the-machine-token")


@pytest.mark.parametrize("role", ["free", "admin"])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "permissions, expected", [([], 403), (["agent.use"], 403), (["agent.use", "agent.admin"], 200)]
)
async def test_admin_permission_is_scoped_separately(
    admin_env, role, method, permissions, expected
):
    gateway = _AccessGateway(permissions)
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        client.cookies.set(identity.SESSION_COOKIE, identity.mint_session(user_id=_OWNER))
        response = await client.request(method, "/admin/probe")
    assert response.status_code == expected
    assert gateway.asked == [_OWNER]
    if expected == 200:
        assert response.json()["actor"] == f"admin:{_OWNER}"


async def test_the_machine_token_stays_independent_of_gateway_permissions(admin_env):
    gateway = _AccessGateway(error=503)
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        response = await client.post(
            "/admin/probe", headers={"Authorization": "Bearer the-machine-token"}
        )
    assert response.status_code == 200
    assert response.json()["actor"] == "admin-token"
    assert gateway.asked == []


@pytest.fixture(autouse=True)
def _configured_identity_issuer(monkeypatch):
    monkeypatch.setenv("GATEWAY_ISSUER", "https://gateway.example")
