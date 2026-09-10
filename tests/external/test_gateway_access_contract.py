"""Validate a running Gateway's actual refusal against the schema and consumer.

Set AGENT_CONTRACT_GATEWAY_URL and AGENT_CONTRACT_DISPATCH_TOKEN, then run this
file with ``pytest -m external``. The probe only reads a randomly named missing
account. No Gateway imports, database access, account creation or mutation.
"""

import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator

from cloud_agent.api.deps import resolve_agent_access
from cloud_agent.gateway import GatewayClient

pytestmark = [pytest.mark.external, pytest.mark.asyncio]


async def test_gateway_refusal_matches_contract_and_agent():
    base = os.environ.get("AGENT_CONTRACT_GATEWAY_URL", "").rstrip("/")
    token = os.environ.get("AGENT_CONTRACT_DISPATCH_TOKEN", "")
    if not base or not token:
        pytest.fail("Set AGENT_CONTRACT_GATEWAY_URL and AGENT_CONTRACT_DISPATCH_TOKEN")
    subject = f"contract-missing-{uuid4().hex}"
    path = f"/internal/users/{subject}/agent-access"
    contract = Path(__file__).resolve().parents[2] / "contracts/internal-lookups.openapi.yaml"
    document = yaml.safe_load(contract.read_text())
    schema = document["paths"]["/internal/users/{user_id}/agent-access"]["get"]["responses"]["403"][
        "content"
    ]["application/json"]["schema"]
    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.get(base + path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403
        Draft202012Validator({**schema, "components": document["components"]}).validate(
            response.json()
        )
        decision = await resolve_agent_access(GatewayClient(base, token, client=http), subject)
        assert not decision.allowed
        assert decision.permissions == []
