"""Advertised optional capabilities have a working handler on the shipping host."""

import json
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_mcp_transports import _client, _config

from cloud_agent.mcp import protocol, transports
from cloud_agent.mcp.jsonrpc import decode


@pytest.mark.asyncio
async def test_handshake_does_not_offer_an_unimplemented_elicitation_form():
    wire = AsyncMock()
    wire.request.return_value = {"protocolVersion": "2025-11-25", "capabilities": {}}
    await protocol.initialize(wire)
    advertised = wire.request.call_args.args[1]["capabilities"]
    assert advertised == {"roots": {"listChanged": False}}
    assert protocol.request_meta("2026-07-28")[protocol.META_CLIENT_CAPABILITIES] == advertised


def test_diagnostics_report_unimplemented_optional_handlers_truthfully():
    states = protocol.support_states({"elicitation": {"form": {}}, "roots": {}, "sampling": {}})
    assert states["elicitation"] == "unsupported"
    assert states["sampling"] == "unsupported"
    assert states["roots"] == "platform-bridge"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_type", [transports.StreamableHttpTransport, transports.LegacySseTransport]
)
async def test_advertised_roots_request_returns_the_empty_authorized_set(transport_type):
    async with _client(lambda request: None) as client:
        wire = transport_type(_config(), client)
        request = decode('{"jsonrpc":"2.0","id":7,"method":"roots/list"}')[0]
        answer = json.loads(await wire._answer_server_request(request))
        assert answer == {"jsonrpc": "2.0", "id": 7, "result": {"roots": []}}
