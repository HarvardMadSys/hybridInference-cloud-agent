"""Shared security mechanics for routes that accept sensitive request bodies."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict

from cloud_agent.api._sensitive_routes import SensitiveRoute


class _RedactedRoute(SensitiveRoute):
    def validation_response(self, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Invalid secret request."})


class _SecretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: str


def _app() -> FastAPI:
    app = FastAPI()
    router = APIRouter(route_class=_RedactedRoute)

    @router.post("/secrets")
    async def save_secret(body: _SecretRequest) -> dict[str, bool]:
        return {"saved": bool(body.credential)}

    @router.get("/challenge")
    async def challenge() -> None:
        raise HTTPException(
            401,
            "Authenticate",
            headers={"WWW-Authenticate": 'Bearer realm="agent"'},
        )

    app.include_router(router)
    return app


async def test_validation_redaction_and_successes_are_never_cacheable() -> None:
    secret = "private-credential-sentinel"
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        saved = await client.post("/secrets", json={"credential": secret})
        unknown = await client.post("/secrets", json={"credential": "ok", "unknown": secret})
        malformed = await client.post(
            "/secrets",
            content=b'{"credential":"private-malformed-sentinel"',
            headers={"Content-Type": "application/json"},
        )

    assert saved.status_code == 200
    assert saved.headers["cache-control"] == "no-store"
    for refused in (unknown, malformed):
        assert refused.status_code == 422
        assert refused.json() == {"detail": "Invalid secret request."}
        assert refused.headers["cache-control"] == "no-store"
        assert "private" not in refused.text


async def test_http_exception_headers_survive_the_cache_policy() -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/challenge")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="agent"'
    assert response.headers["cache-control"] == "no-store"
