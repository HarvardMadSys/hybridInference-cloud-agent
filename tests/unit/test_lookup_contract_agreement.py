"""The access-error contract is handled by the gateway consumer.

Declared refusal payloads go through the real client and access dependency.
The schema also rejects unknown refusal types. Live gateway checks remain
necessary to detect a contract and consumer that have drifted together.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import HTTPException
from jsonschema import Draft202012Validator, ValidationError

from cloud_agent.api.deps import resolve_agent_access
from cloud_agent.api.errors import AgentConfigurationError
from cloud_agent.gateway import GatewayClient

CONTRACT = (
    Path(__file__).resolve().parents[2] / "gateway_contracts" / "internal-lookups.openapi.yaml"
)


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
