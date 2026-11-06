"""A flow's domain refusal reaches the owner as the same HTTP answer it always had.

``cloud_agent.api.errors`` is the one place ``Refused`` and ``Unavailable`` turn
into status codes, headers and the ``{"error": {"type", "message"}}`` envelope.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api._environment_route_policy import EnvironmentSensitiveRoute
from cloud_agent.api.errors import as_http_exception, install_domain_errors
from cloud_agent.app import create_app, install_admin_no_store
from cloud_agent.domain.errors import Refused, Unavailable


@pytest.mark.parametrize(
    ("kind", "status"),
    [("invalid", 400), ("forbidden", 403), ("not_found", 404), ("conflict", 409)],
)
def test_a_refusal_answers_its_kinds_status_in_the_envelope(kind, status) -> None:
    answer = as_http_exception(Refused(kind, "invalid_request", "No."))
    assert answer.status_code == status
    assert answer.detail == {"error": {"type": "invalid_request", "message": "No."}}
    assert answer.headers is None


def test_an_outage_is_a_503_and_a_type_may_name_its_own_status() -> None:
    assert as_http_exception(Unavailable("sandbox_host_unavailable", "Down.")).status_code == 503
    receipt = as_http_exception(Unavailable("invalid_native_fork_receipt", "Mismatch."))
    assert receipt.status_code == 502


def test_details_sit_between_type_and_message_and_retry_after_is_a_header() -> None:
    answer = as_http_exception(
        Refused(
            "conflict",
            "model_connection_unusable",
            "Changed.",
            details={"reason": "changed"},
            retry_after_seconds=2,
        )
    )
    assert list(answer.detail["error"]) == ["type", "reason", "message"]
    assert answer.headers == {"Retry-After": "2"}


def test_an_untyped_outage_is_its_message_alone() -> None:
    answer = as_http_exception(Unavailable(None, "Model connection keys cannot be read"))
    assert (answer.status_code, answer.detail) == (503, "Model connection keys cannot be read")


def test_the_main_listener_answers_domain_errors() -> None:
    handlers = create_app().exception_handlers
    assert Refused in handlers and Unavailable in handlers


def _refusing_app() -> FastAPI:
    app = FastAPI()
    plain = APIRouter()
    sensitive = APIRouter(route_class=EnvironmentSensitiveRoute)

    async def refuse() -> None:
        raise Refused("conflict", "fork_in_progress", "Wait.", retry_after_seconds=2)

    plain.add_api_route("/plain", refuse, methods=["POST"])
    plain.add_api_route("/admin/plain", refuse, methods=["POST"])
    sensitive.add_api_route("/sensitive", refuse, methods=["POST"])
    app.include_router(plain)
    app.include_router(sensitive)
    install_admin_no_store(app)
    install_domain_errors(app)
    return app


@pytest.mark.parametrize(
    ("path", "no_store"), [("/plain", False), ("/admin/plain", True), ("/sensitive", True)]
)
async def test_a_route_answers_a_refusal_with_its_own_cache_policy(path, no_store) -> None:
    transport = ASGITransport(app=_refusing_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(path)
    assert response.status_code == 409
    assert response.json() == {
        "detail": {"error": {"type": "fork_in_progress", "message": "Wait."}}
    }
    assert response.headers["retry-after"] == "2"
    assert (response.headers.get("cache-control") == "no-store") is no_store
