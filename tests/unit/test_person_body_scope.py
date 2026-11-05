"""The body check reads who a route serves: a person's body is refused, a host's is not.

A route an owner calls refuses a string holding NUL or half a surrogate pair,
in a key as in a value. A route nobody signs in to, as a host's, is left to
its own model, which keeps what a host reports.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from cloud_agent.api._owner_auth import authenticate_agent_owner
from cloud_agent.api._parameters import refuse_unstorable_parameters
from cloud_agent.app import install_admin_no_store


@pytest.fixture
def client() -> TestClient:
    app = FastAPI(dependencies=[Depends(refuse_unstorable_parameters)])
    # The application's own answers, which keep a repeated surrogate encodable.
    install_admin_no_store(app)
    app.dependency_overrides[authenticate_agent_owner] = lambda: {"user_id": "owner"}

    @app.post("/owner")
    async def owner(
        body: dict[str, Any], _user: dict[str, Any] = Depends(authenticate_agent_owner)
    ) -> dict[str, Any]:
        return body

    @app.post("/host")
    async def host(body: dict[str, Any]) -> dict[str, Any]:
        return body

    return TestClient(app)


def _post(client: TestClient, path: str, body: Any, kind: str = "application/json") -> Any:
    return client.post(path, content=json.dumps(body).encode(), headers={"Content-Type": kind})


@pytest.mark.parametrize(
    ("body", "loc", "kind"),
    [
        ({"prompt": "a\x00b"}, ["body", "prompt"], "string_pattern_mismatch"),
        (
            {"goal": {"objective": ["ok", "c\ud83d"]}},
            ["body", "goal", "objective", 1],
            "string_unicode",
        ),
        ({"tools": {"run\x00": True}}, ["body", "tools", "run\x00"], "string_pattern_mismatch"),
    ],
)
def test_a_persons_body_holding_unstorable_text_is_refused(
    client: TestClient, body: dict[str, Any], loc: list[Any], kind: str
):
    response = _post(client, "/owner", body)

    assert response.status_code == 422
    assert [(error["loc"], error["type"]) for error in response.json()["detail"]] == [(loc, kind)]


def test_a_host_body_and_a_clean_body_pass(client: TestClient):
    assert _post(client, "/host", {"content": "cat\x00/proc/self/cmdline"}).status_code == 200
    assert _post(client, "/owner", {"prompt": "fix it"}).status_code == 200
    assert (
        _post(client, "/owner", {"prompt": "a\x00b"}, "application/merge-patch+json").status_code
        == 422
    )
