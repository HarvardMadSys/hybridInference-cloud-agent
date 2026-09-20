"""The mint request carries the fields the gateway declares, and no others.

`GrantClient.mint` sent `allowed_mcp` on every call. The gateway had stopped
declaring that field when the ownership amendment moved MCP here, and pydantic
ignores what it does not know — so the request looked accepted, the scope it
named was never requested of anything, and both repositories' suites stayed
green. Nothing failed, because nothing was checking that the two ends of one
request agreed.

An extra field is the quiet half of the failure. The loud half arrives the day
the gateway sets `extra="forbid"` and every mint starts returning 422.

`contracts/inference-grants.openapi.yaml` is the transcription of the gateway's
router. Comparing against it is not the client checked against itself: the
schema is edited when the gateway's contract changes, the client when this
service's needs change, and this test is where those two edits have to meet.

It would not have caught the original drift on its own — the contract file
declared `allowed_mcp` too, stale in the same direction. That is the honest
limit of a transcription: it is a second record, not a second source. It
catches the next drift, which is what a test can do here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from cloud_agent.grants import GrantClient

CONTRACT = (
    Path(__file__).resolve().parents[2] / "gateway_contracts" / "inference-grants.openapi.yaml"
)


def _declared_fields() -> set[str]:
    document = yaml.safe_load(CONTRACT.read_text())
    schema = document["paths"]["/internal/agent-grants"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    return set(schema["properties"])


class _Recorder:
    """Captures the body instead of sending it."""

    def __init__(self) -> None:
        self.body: dict[str, Any] | None = None

    async def post(self, url: str, *, json: Any = None, **_kwargs: Any) -> Any:
        self.body = json

        class _Response:
            status_code = 200

            @staticmethod
            def json() -> dict[str, Any]:
                return {"grant_id": "agr_1", "token": "agr.a.b", "allowed_models": []}

        return _Response()


async def _mint_body(**kwargs: Any) -> dict[str, Any]:
    recorder = _Recorder()
    client = GrantClient("https://gw.test", "tok", client=recorder)
    await client.mint(user_id="user_1", job_id="job_1", attempt_id="1", **kwargs)
    assert recorder.body is not None
    return recorder.body


@pytest.mark.asyncio
async def test_the_request_names_no_field_the_gateway_ignores() -> None:
    """Every key sent is one the gateway's contract declares."""
    body = await _mint_body(models=["glm-5.1"], ttl_seconds=120)
    undeclared = sorted(set(body) - _declared_fields())
    assert not undeclared, (
        f"the gateway does not declare {undeclared}; pydantic drops what it does "
        "not know, so these travel on every mint and scope nothing"
    )


@pytest.mark.asyncio
async def test_the_required_fields_are_always_sent() -> None:
    """The idempotency key and subject go on every mint, defaults or not."""
    document = yaml.safe_load(CONTRACT.read_text())
    schema = document["paths"]["/internal/agent-grants"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    body = await _mint_body()
    missing = sorted(set(schema["required"]) - set(body))
    assert not missing, f"the gateway requires {missing}, and mint does not send them"


@pytest.mark.asyncio
async def test_mcp_scope_is_not_requested_of_the_gateway() -> None:
    """Named on its own, because this is the one that was wrong.

    MCP is decided here now — by the relay's scope check, the attempt fence and
    the local registry. A grant that carried a tool scope would put the
    decision back in a service that has no registry to decide it with.
    """
    body = await _mint_body(models=["glm-5.1"])
    assert not [key for key in body if "mcp" in key.lower()]
