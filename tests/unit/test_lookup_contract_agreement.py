"""The lookup responses this service reads are ones the gateway declares.

Written after the same drift was found twice. `GrantClient.mint` sent a field
the gateway had stopped declaring; this document declared an endpoint the
gateway had stopped serving (`/internal/mcp-registry`) and did **not** declare
`aliases`, which the catalog had grown. Each repository tested its own half,
and the halves disagreed for as long as nobody looked.

The reads are named here rather than discovered. Discovering them would mean
parsing `.get("...")` calls out of two modules, which is a fragile way to learn
something a short list says plainly — and the list failing to match a rename is
the same signal, arriving in the same place.

**What this cannot do.** The contract file is a transcription of the gateway's
routers: a second record, not a second source. When it and the client are stale
in the same direction — which is exactly what happened to `allowed_mcp` — this
passes. Comparing it against a real gateway response is what catches that, and
it needs both repositories at once; `docs/smoke.md` records the last such run
and what it covered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import HTTPException
from jsonschema import Draft202012Validator, ValidationError

from cloud_agent.api.deps import resolve_agent_access
from cloud_agent.api.errors import AgentConfigurationError
from cloud_agent.gateway import GatewayClient

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "internal-lookups.openapi.yaml"

#: Field → the code that reads it. A read that is not here is one nobody
#: checked against the gateway.
CATALOG_READS = {
    "models": "GatewayClient.model_catalog_with_aliases",
    "aliases": "GatewayClient.model_catalog_with_aliases",
}


ACCESS_READS = {
    "user_id": "GatewayClient.agent_access — exact subject binding",
    "allowed": "GatewayClient.agent_access — explicit current decision",
    "permissions": "owner/admin dependencies and session.me — Agent capabilities",
}


def _declared(path: str) -> set[str]:
    document: dict[str, Any] = yaml.safe_load(CONTRACT.read_text())
    schema = document["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    return set(schema["properties"])


@pytest.mark.parametrize(
    ("path", "reads"),
    [
        ("/internal/model-catalog", CATALOG_READS),
        ("/internal/users/{user_id}/agent-access", ACCESS_READS),
    ],
)
def test_every_field_this_service_reads_is_declared(path: str, reads: dict[str, str]) -> None:
    """A field read here is one the gateway's contract says exists."""
    undeclared = sorted(set(reads) - _declared(path))
    assert not undeclared, "\n".join(
        [f"{CONTRACT.name} does not declare these, but this service reads them:"]
        + [f"  {field} — {reads[field]}" for field in undeclared]
    )


def test_the_registry_endpoint_is_not_still_documented() -> None:
    """`/internal/mcp-registry` left with the ownership amendment.

    A documented endpoint the gateway does not serve is worse than an
    undocumented one: it reads as available, and the code that was written
    against it 404s at runtime with the contract file agreeing it should have
    worked. One such method survived here until it was deleted.
    """
    document = yaml.safe_load(CONTRACT.read_text())
    assert "/internal/mcp-registry" not in document["paths"]


def test_access_error_schema_rejects_an_unknown_refusal_type():
    document = yaml.safe_load(CONTRACT.read_text())
    schema = document["paths"]["/internal/users/{user_id}/agent-access"]["get"]["responses"]["403"][
        "content"
    ]["application/json"]["schema"]
    validator = Draft202012Validator({**schema, "components": document["components"]})
    validator.validate({"error": {"type": "subject_unavailable", "message": "Denied"}})
    with pytest.raises(ValidationError):
        validator.validate({"error": {"type": "browser_check", "message": "Denied"}})


@pytest.mark.parametrize(
    "status, schema_name",
    [
        (403, "SubjectUnavailableError"),
        (404, "InternalApiNotConfiguredError"),
        (503, "AgentAccessUnavailableError"),
    ],
)
async def test_consumer_handles_the_error_types_declared_by_the_contract(status, schema_name):
    document = yaml.safe_load(CONTRACT.read_text())
    endpoint = document["paths"]["/internal/users/{user_id}/agent-access"]["get"]
    schema = endpoint["responses"][str(status)]["content"]["application/json"]["schema"]
    assert schema == {"$ref": f"#/components/schemas/{schema_name}"}
    declared = document["components"]["schemas"][schema_name]["allOf"][1]["properties"]["error"][
        "properties"
    ]["type"]["enum"]
    assert len(declared) == 1
    payload = {"error": {"type": declared[0], "message": "Contract refusal"}}
    Draft202012Validator({**schema, "components": document["components"]}).validate(payload)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload))
    ) as http:
        gateway = GatewayClient("https://gateway.example", "test-dispatch", client=http)
        if status == 403:
            decision = await resolve_agent_access(gateway, "subject")
            assert not decision.allowed
        else:
            with pytest.raises(HTTPException) as raised:
                await resolve_agent_access(gateway, "subject")
            assert raised.value.status_code == 503
            assert isinstance(raised.value, AgentConfigurationError) is (status == 404)
