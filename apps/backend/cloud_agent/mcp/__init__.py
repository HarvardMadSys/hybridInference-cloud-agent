"""Generic MCP access: user connections, protocol hosting and tool authorization.

This package supersedes the provider-whitelisted shape that
``cloud_agent/mcp_catalogue.py`` and ``cloud_agent/mcp_registry.py`` started
with. The design it implements is
``docs/specs/2026-09-11-per-user-mcp-v1-design.zh.md``: a person adds a
standard MCP server -- remote URL, imported configuration or a stdio launch
spec -- the platform discovers what it offers, and any coding agent on the
platform can use the parts that person authorized for that session.

Modules are layered so that each one can be tested by calling it:

``models``
    The domain records: connections, credentials, capability snapshots,
    thread bindings, invocations and connectors.
``errors``
    A credential write that matched no row, so the connection insert rolls back.
``net_policy``
    What a connection is allowed to dial, and where a credential may travel.
``config_import``
    Parsing the several ``mcpServers`` dialects into one connection draft.
``launch``
    Turning a stdio draft into a pinned, shell-free launch plan.
``oauth``
    Discovery, registration, PKCE authorization and token-response contracts.
``refresh``
    Renewing a stored credential exactly once, under the issuer's rotation
    rules and fenced against a disconnect.
``jsonrpc``
    JSON-RPC framing, request correlation and the errors a peer can act on.
``sse``
    Server-sent event framing, including the CRLF and fragmentation cases a
    naive split gets wrong.
``protocol``
    Version profiles, negotiation, discovery and the tools / resources /
    prompts surface.
``authorization``
    Whether one call is permitted, refused, or held for the owner.
``invocations``
    The persisted state machine behind a held call, and its timeout contract.
``registry``
    Resolving a thread's bindings into the servers a job may reach.
``transports``
    Streamable HTTP, the legacy SSE shape, and the era detection between them.
``host``
    The protocol host the relay drives: connect, discover, call, bound.

Nothing here talks to the network or the database at import time.
"""

from __future__ import annotations

__all__: list[str] = []
