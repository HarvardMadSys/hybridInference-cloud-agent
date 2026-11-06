"""How the relay's merged MCP surface answers: reply envelopes, the handshake, catalogue entries.

Split from :mod:`cloud_agent.api.mcp_relay`, which routes each JSON-RPC message
the merged endpoint receives. Nothing here reads a request, a credential or the
store: these build what goes back to the sandbox once the relay has decided.
"""

from __future__ import annotations

from typing import Any, Protocol

from cloud_agent.mcp import protocol
from cloud_agent.mcp.jsonrpc import METHOD_NOT_FOUND
from cloud_agent.mcp.models import CapabilityKind


def error_reply(request_id: Any, message: str, code: int = METHOD_NOT_FOUND) -> dict[str, Any]:
    """Build a JSON-RPC error reply for one request id.

    Answered as a *protocol* error rather than an HTTP failure on purpose: the
    transport hop succeeded and the call did not, which is a thing models
    handle. A 403 here would look to the CLI like a broken connection and take
    the whole server down for the rest of the job.
    """
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def result_reply(request_id: Any, result: Any, *, stateless: bool) -> dict[str, Any]:
    """Build a JSON-RPC result reply, discriminated when the client is modern.

    The stateless revision requires every result to carry ``resultType``. A
    client that asked in that shape treats a result without one as complete
    anyway, but sending it costs nothing and keeps the reply honest about which
    contract it was built for.
    """
    if stateless and isinstance(result, dict):
        result = {**result, "resultType": protocol.RESULT_TYPE_COMPLETE}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class Catalogued(Protocol):
    """What a listing needs of the resolved binding it renders a tool from.

    Narrowed to a protocol so the renderer cannot reach for more of a
    connection than a name and an id, and so a test can hand it a stand-in.
    """

    @property
    def alias(self) -> str:
        """The binding alias the capability is presented under."""
        ...

    @property
    def connection_id(self) -> int:
        """The connection the capability belongs to."""
        ...


def resource_entry(capability: Any, uri: str) -> dict[str, Any]:
    """Render one resource the way a client reads it."""
    schema = capability.input_schema if isinstance(capability.input_schema, dict) else {}
    uri_field = "uriTemplate" if capability.kind is CapabilityKind.RESOURCE_TEMPLATE else "uri"
    entry: dict[str, Any] = {uri_field: uri, "name": capability.name}
    if capability.description:
        entry["description"] = capability.description
    if schema.get("mimeType"):
        entry["mimeType"] = schema["mimeType"]
    return entry


def prompt_entry(presented: str, capability: Any) -> dict[str, Any]:
    """Render one prompt, under the name this session will call it by."""
    schema = capability.input_schema if isinstance(capability.input_schema, dict) else {}
    entry: dict[str, Any] = {"name": presented}
    if capability.description:
        entry["description"] = capability.description
    if isinstance(schema.get("arguments"), list):
        entry["arguments"] = schema["arguments"]
    return entry


def tool_entry(presented: str, authorized: Catalogued, capability: Any) -> dict[str, Any]:
    """Render one merged catalogue entry the way a client reads it."""
    return {
        "name": presented,
        "description": capability.description
        or f"{capability.name} on the {authorized.alias} MCP server",
        "inputSchema": capability.input_schema
        if isinstance(capability.input_schema, dict)
        else {"type": "object", "properties": {}},
    }


async def initialize_result(payload: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    """Answer the handshake for the merged surface.

    The requested revision is echoed when this platform implements it, because
    the client decides in the handshake era and a server that answers with
    something else is asking the client to disconnect. When it is not one we
    implement, the newest we do is offered instead: a client that understands
    the list will pick from it, and one that does not is at least told what it
    is talking to rather than being driven with a guessed header.

    **Every surface this endpoint answers is advertised.** The three families
    are declared together because the client's next request comes from here: a
    handshake that named only ``tools`` would leave ``resources/list`` and
    ``prompts/list`` never asked for, and a capability the platform serves but
    does not claim is indistinguishable from one it does not have. The two
    sub-flags are the honest part of the claim -- ``listChanged`` is false
    because this surface is request/response and cannot push, and
    ``subscribe`` is false because it holds no resource subscription open
    between calls.
    """
    params = payload.get("params")
    requested = str(params.get("protocolVersion") or "") if isinstance(params, dict) else ""
    agreed = requested if requested in protocol.PROFILES else protocol.PREFERRED_PROTOCOL_VERSION
    aliases = sorted(str(row.get("alias") or "") for row in identity.get("mcp_bindings") or [])
    return {
        "protocolVersion": agreed,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
        },
        "serverInfo": {"name": "hybridinference-cloud-agent", "version": "1"},
        "instructions": (
            "Use the exact tool names returned by tools/list. This session's connections: "
            + (", ".join(alias for alias in aliases if alias) or "none")
            + ". Calls requiring confirmation wait for the owner in Approvals or the conversation's "
            "Tools panel. An approval expiry means the call was not sent; ask the owner to review "
            "the request or permissions before retrying."
        ),
    }
