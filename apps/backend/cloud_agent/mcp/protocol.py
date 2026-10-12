"""The MCP surfaces the platform speaks, and how versions are negotiated.

The relay is a *host* now, not a forwarder. It has to know what
``initialize`` agreed to, walk a paginated catalogue, correlate a progress
notification with the call that caused it, and answer a server that asks the
client a question. That is what this module decides; the transport that carries
the bytes is someone else's job, injected here as :class:`ProtocolTransport`.

**Versions are negotiated, never assumed.** A server is asked what it supports
and the newest revision this platform implements is chosen. Sending a modern
header to a legacy server produces an answer that looks like a protocol error
from the server's side, which is the least debuggable failure in the whole
feature; the profile table makes the differences explicit instead.

**A catalogue is discovered, not trusted.** Tools, resources and prompts are
read with pagination, kept with the schema they were advertised under, and
recorded as a snapshot bound to a config revision. Anything a server adds later
appears in a *new* revision, which is what stops a silent widening of what an
agent may call.

**Names are ours to choose.** A CLI constrains tool names far more tightly than
MCP does, so the platform presents a namespaced, sanitized name and keeps the
mapping. The server's own name is what travels upstream; the presented name is
only ever a handle.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from cloud_agent.mcp.jsonrpc import JsonRpcError
from cloud_agent.mcp.models import (
    PREFERRED_PROTOCOL_VERSION,
    STATELESS_PROTOCOL_VERSIONS,
    SUPPORTED_PROTOCOL_VERSIONS,
    Capability,
    CapabilityKind,
    CapabilitySnapshot,
)

#: Protocol methods.
INITIALIZE = "initialize"
INITIALIZED = "notifications/initialized"
PING = "ping"
TOOLS_LIST = "tools/list"
TOOLS_CALL = "tools/call"
RESOURCES_LIST = "resources/list"
RESOURCES_READ = "resources/read"
RESOURCES_TEMPLATES_LIST = "resources/templates/list"
PROMPTS_LIST = "prompts/list"
PROMPTS_GET = "prompts/get"
RESOURCES_SUBSCRIBE = "resources/subscribe"
RESOURCES_UNSUBSCRIBE = "resources/unsubscribe"
#: Mandatory for a stateless-era server, and the only way to read its
#: capabilities: there is no handshake to declare them in.
SERVER_DISCOVER = "server/discover"
#: The stateless era's replacement for the GET stream and for
#: ``resources/subscribe``.
SUBSCRIPTIONS_LISTEN = "subscriptions/listen"
PROGRESS = "notifications/progress"
CANCELLED = "notifications/cancelled"
MESSAGE = "notifications/message"
TOOLS_LIST_CHANGED = "notifications/tools/list_changed"
RESOURCES_LIST_CHANGED = "notifications/resources/list_changed"
PROMPTS_LIST_CHANGED = "notifications/prompts/list_changed"

#: The client-side methods a server may call on us in the handshake era.
SAMPLING_CREATE_MESSAGE = "sampling/createMessage"
ELICITATION_CREATE = "elicitation/create"
ROOTS_LIST = "roots/list"

#: ``_meta`` keys the stateless era reserves. The version and the client's
#: capabilities travel on every request, because a server is forbidden from
#: inferring either from an earlier one.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_LOG_LEVEL = "io.modelcontextprotocol/logLevel"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_PROGRESS_TOKEN = "progressToken"

#: The result discriminator the stateless era requires on every result.
RESULT_TYPE_COMPLETE = "complete"
RESULT_TYPE_INPUT_REQUIRED = "input_required"

#: A page of a catalogue is bounded so one server cannot make a single
#: discovery read forever. Generous: real servers paginate in tens.
MAX_PAGES = 64

#: Tool names presented to a CLI. The intersection of what the coding agents
#: accept: letters, digits, underscore and hyphen, bounded length. Anything
#: outside is replaced rather than rejected, so a server with a dotted tool
#: name is still usable.
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]")
# The merged endpoint is named "session" by every adapter. Reserve the longest
# native prefix (Claude/Codex) so a bridge need not truncate the stable digest.
MAX_PRESENTED_NAME = 64 - len("mcp__session__")
_HASH_NAMESPACE = "mcp_h"
NAMESPACE_SEPARATOR = "__"


@dataclass(frozen=True)
class ProtocolProfile:
    """What one protocol revision does differently from the others."""

    version: str
    #: Whether an ``initialize`` handshake establishes the session.
    handshake: bool
    #: Whether the server may assign a session id the client must echo.
    session_header: bool
    #: Whether a separate GET stream carries server-initiated messages.
    server_stream: bool
    #: Whether a POST may answer with an SSE stream rather than JSON.
    streaming_reply: bool
    #: Whether the client declares capabilities once, at initialize.
    capabilities_at_handshake: bool

    def as_dict(self) -> dict[str, Any]:
        """Render for diagnostics."""
        return {
            "version": self.version,
            "handshake": self.handshake,
            "session_header": self.session_header,
            "server_stream": self.server_stream,
            "streaming_reply": self.streaming_reply,
            "capabilities_at_handshake": self.capabilities_at_handshake,
        }


#: The revisions this platform implements. The 2024-11-05 entry is the legacy
#: HTTP+SSE shape: handshake, no session header, a separate server stream, and
#: a JSON reply to a POST rather than a stream.
PROFILES: Mapping[str, ProtocolProfile] = {
    "2024-11-05": ProtocolProfile(
        version="2024-11-05",
        handshake=True,
        session_header=False,
        server_stream=True,
        streaming_reply=False,
        capabilities_at_handshake=True,
    ),
    "2025-06-18": ProtocolProfile(
        version="2025-06-18",
        handshake=True,
        session_header=True,
        server_stream=True,
        streaming_reply=True,
        capabilities_at_handshake=True,
    ),
    "2025-11-25": ProtocolProfile(
        version="2025-11-25",
        handshake=True,
        session_header=True,
        server_stream=True,
        streaming_reply=True,
        capabilities_at_handshake=True,
    ),
    # Not a newer number but a different shape: no handshake, no session, no
    # GET stream, no SSE resumability, and a server that may not send a request
    # of its own -- it answers with ``InputRequiredResult`` instead.
    "2026-07-28": ProtocolProfile(
        version="2026-07-28",
        handshake=False,
        session_header=False,
        server_stream=False,
        streaming_reply=True,
        capabilities_at_handshake=False,
    ),
}


def is_stateless(version: str) -> bool:
    """Whether a revision uses the per-request metadata shape."""
    return version in STATELESS_PROTOCOL_VERSIONS


def request_meta(
    version: str,
    *,
    progress_token: str | int | None = None,
    log_level: str | None = None,
) -> dict[str, Any]:
    """Build the ``_meta`` object a stateless-era request must carry.

    Capabilities are re-declared on every request rather than remembered,
    because the revision says a server MUST NOT infer them from a prior one.
    That is also why this is built per request rather than cached.
    """
    meta: dict[str, Any] = {
        META_PROTOCOL_VERSION: version,
        META_CLIENT_INFO: {"name": "hybridinference-cloud-agent", "version": "1"},
        META_CLIENT_CAPABILITIES: client_capabilities(),
    }
    if progress_token is not None:
        meta[META_PROGRESS_TOKEN] = progress_token
    if log_level is not None:
        meta[META_LOG_LEVEL] = log_level
    return meta


#: SEP-2243 copies an annotated argument into ``Mcp-Param-<name>``. The server
#: rejects the call when the header is missing or disagrees with the body.
_PARAM_HEADER_PREFIX = "Mcp-Param-"
_BASE64_SENTINEL_PREFIX = "=?base64?"
_BASE64_SENTINEL_SUFFIX = "?="
_MAX_SAFE_INTEGER = (1 << 53) - 1
_HEADER_TYPES = frozenset({"string", "integer", "boolean"})
_MISSING = object()


def _is_tchar(char: str) -> bool:
    """Whether ``char`` is one HTTP token character."""
    if "0" <= char <= "9" or "A" <= char <= "Z" or "a" <= char <= "z":
        return True
    return char in "!#$%&'*+-.^_`|~"


def _header_name_ok(name: str) -> bool:
    """Whether an ``x-mcp-header`` value is a legal HTTP field-name."""
    return bool(name) and all(_is_tchar(char) for char in name)


def _requires_base64(value: str) -> bool:
    """Whether a string must travel as the SEP-2243 base64 sentinel."""
    if not value:
        return False
    if value[0] in " \t" or value[-1] in " \t":
        return True
    if any(ord(char) < 0x20 or ord(char) > 0x7E for char in value):
        return True
    return value.startswith(_BASE64_SENTINEL_PREFIX) and value.endswith(_BASE64_SENTINEL_SUFFIX)


def _encode_header_value(value: str) -> str:
    """Encode one header value the way the stateless revision requires."""
    if not _requires_base64(value):
        return value
    encoded = base64.b64encode(value.encode()).decode("ascii")
    return f"{_BASE64_SENTINEL_PREFIX}{encoded}{_BASE64_SENTINEL_SUFFIX}"


def _primitive_header(value: Any, expected_type: str) -> str | None:
    """Render one annotated argument, or ``None`` when it is not that primitive."""
    if expected_type == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        return None
    if expected_type == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and (not math.isfinite(value) or value != math.trunc(value)):
            return None
        number = int(value)
        if number < -_MAX_SAFE_INTEGER or number > _MAX_SAFE_INTEGER:
            return None
        return str(number)
    if expected_type == "string" and isinstance(value, str):
        return _encode_header_value(value)
    return None


def _argument_at(arguments: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    """Read one nested argument, or :data:`_MISSING` when the path is absent."""
    current: Any = arguments
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def annotated_param_headers(
    schema: Mapping[str, Any], arguments: Mapping[str, Any]
) -> dict[str, str]:
    """Mirror ``x-mcp-header`` arguments into the headers the server compares.

    The values come from the body this platform is about to send. A header the
    caller attached is not a source: the server refuses the call unless the
    header decodes to that body value, so the body is the one copy.
    """
    headers: dict[str, str] = {}
    claimed: set[str] = set()

    def walk(properties: Any, prefix: tuple[str, ...]) -> None:
        if not isinstance(properties, Mapping):
            return
        for name, prop in properties.items():
            if not isinstance(name, str) or not isinstance(prop, Mapping):
                continue
            path = (*prefix, name)
            header = prop.get("x-mcp-header")
            prop_type = prop.get("type")
            if (
                isinstance(header, str)
                and prop_type in _HEADER_TYPES
                and _header_name_ok(header)
                and header.casefold() not in claimed
            ):
                claimed.add(header.casefold())
                value = _argument_at(arguments, path)
                if value is not _MISSING and value is not None:
                    encoded = _primitive_header(value, str(prop_type))
                    if encoded is not None:
                        headers[f"{_PARAM_HEADER_PREFIX}{header}"] = encoded
            nested = prop.get("properties")
            if isinstance(nested, Mapping):
                walk(nested, path)

    walk(schema.get("properties"), ())
    return headers


def request_headers(
    method: str,
    params: Mapping[str, Any] | None,
    *,
    input_schema: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """The metadata headers a stateless-era HTTP request must repeat.

    They mirror body fields the server already has, which the revision requires
    so an intermediary can route without parsing the body. Tool arguments marked
    ``x-mcp-header`` are copied from the body, because a server on the
    2026-07-28 revision rejects the call when those headers are absent.
    """
    headers = {"mcp-method": method}
    if isinstance(params, Mapping):
        name = params.get("name") or params.get("uri")
        if isinstance(name, str) and name:
            headers["mcp-name"] = name
        arguments = params.get("arguments")
        if (
            method == TOOLS_CALL
            and isinstance(input_schema, Mapping)
            and isinstance(arguments, Mapping)
        ):
            headers.update(annotated_param_headers(input_schema, arguments))
    return headers


def result_type(result: Any) -> str:
    """Read a result's discriminator, defaulting to ``complete``.

    The default is the specification's own backward-compatibility rule: a
    result without the field predates it and means the call finished.
    """
    if isinstance(result, Mapping):
        value = result.get("resultType")
        if isinstance(value, str) and value:
            return value
    return RESULT_TYPE_COMPLETE


def input_required(result: Any) -> bool:
    """Whether a server is asking for input before it can answer."""
    return result_type(result) == RESULT_TYPE_INPUT_REQUIRED


def input_requests(result: Any) -> Mapping[str, Any]:
    """The requests a server embedded in an ``input_required`` result."""
    if not isinstance(result, Mapping):
        return {}
    requests = result.get("inputRequests")
    return requests if isinstance(requests, Mapping) else {}


def continuation_params(
    *,
    input_responses: Mapping[str, Any],
    request_state: str | None,
    base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the parameters for retrying a multi-round-trip call.

    ``requestState`` is echoed exactly and never invented: it is an opaque value
    the server produced, and adding one the server did not send is how a client
    talks a server into accepting state it never issued.
    """
    params: dict[str, Any] = dict(base or {})
    params["inputResponses"] = dict(input_responses)
    if request_state is not None:
        params["requestState"] = request_state
    return params


#: Optional capabilities, and what a missing handler means. Kept as data so a
#: diagnostic can say *which* feature is unavailable rather than collapsing to
#: "unsupported".
OPTIONAL_CAPABILITY_HANDLERS: Mapping[str, str] = {
    "elicitation": "the server may ask your user a question mid-call",
    "sampling": "the server may ask this platform to run a model call",
    "roots": "the server may ask which workspace roots are exposed",
    "resources.subscribe": "the server may push resource updates",
    "tasks": "the server may run long-lived asynchronous work",
    "apps": "the server may render an interactive application",
}


class ProtocolError(RuntimeError):
    """Raised when a version cannot be agreed or a reply is malformed."""

    def __init__(self, message: str, *, kind: str = "protocol_incompatible") -> None:
        """Record the diagnosis kind alongside the message."""
        super().__init__(message)
        self.kind = kind


@runtime_checkable
class ProtocolTransport(Protocol):
    """One bidirectional connection to a server, as the protocol layer needs it.

    Only two operations: send a request and await its reply, or send a
    notification. Streaming, framing, resumption and credentials belong to the
    implementation, which is what lets the protocol logic be tested against a
    dictionary.
    """

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """Send a request and return its result, raising on a JSON-RPC error."""
        ...

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """Send a notification, which expects no reply."""
        ...


def client_capabilities() -> dict[str, Any]:
    """What this platform declares it can do.

    Declared honestly and narrowly: a capability listed here is one a handler
    exists for, because declaring ``sampling`` without one turns a server's
    legitimate request into a hang. Roots are answered with an empty authorized
    set. Elicitation needs a user interaction handler and is not advertised.
    """
    return {
        "roots": {"listChanged": False},
    }


def negotiate_version(server_supported: Iterable[str]) -> str:
    """Choose the newest revision both sides implement, or refuse.

    Preference follows this platform's own order rather than the server's: a
    server that lists an older revision first is stating its own preference,
    not ours, and honouring it would pick a legacy transport on purpose.
    """
    offered = {str(version) for version in server_supported if version}
    for candidate in (PREFERRED_PROTOCOL_VERSION, *SUPPORTED_PROTOCOL_VERSIONS):
        if candidate in offered:
            return candidate
    supported = ", ".join(SUPPORTED_PROTOCOL_VERSIONS)
    seen = ", ".join(sorted(offered)) or "nothing"
    raise ProtocolError(
        f"this server speaks {seen}, which this platform does not implement. "
        f"Supported: {supported}."
    )


@dataclass(frozen=True)
class Handshake:
    """What was agreed with a server, whichever era agreed it.

    A handshake-era server agrees through ``initialize``; a stateless-era one
    through ``server/discover``. Both end up here, so everything downstream --
    the catalogue walk, the support report, the session records -- has one
    shape to read rather than two.
    """

    protocol_version: str
    server_name: str = ""
    server_version: str = ""
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    instructions: str = ""

    @property
    def profile(self) -> ProtocolProfile:
        """The profile for the negotiated revision."""
        return PROFILES.get(self.protocol_version) or PROFILES[PREFERRED_PROTOCOL_VERSION]


async def initialize(transport: ProtocolTransport) -> Handshake:
    """Perform the handshake era's ``initialize`` and return what was agreed.

    A server that answers with a revision this platform did not offer is
    refused rather than accepted: the specification says the client decides,
    so an answer naming something else means the two ends disagree about the
    protocol, and continuing would fail later in a way that looks like our bug.
    """
    result = await transport.request(
        INITIALIZE,
        {
            "protocolVersion": PREFERRED_PROTOCOL_VERSION,
            "capabilities": client_capabilities(),
            "clientInfo": {"name": "hybridinference-cloud-agent", "version": "1"},
        },
    )
    if not isinstance(result, Mapping):
        raise ProtocolError("the server's initialize reply is not an object")
    agreed = str(result.get("protocolVersion") or "")
    if agreed not in PROFILES:
        raise ProtocolError(
            f"the server chose protocol version {agreed!r}, which this platform does not implement"
        )
    if is_stateless(agreed):
        # A stateless server does not answer ``initialize``; reaching here means
        # the two ends read the same number differently, and continuing would
        # produce requests missing the metadata it requires.
        raise ProtocolError(f"the server agreed to {agreed}, which has no handshake to agree to")
    server_info = result.get("serverInfo")
    info = server_info if isinstance(server_info, Mapping) else {}
    capabilities = result.get("capabilities")
    return Handshake(
        protocol_version=agreed,
        server_name=str(info.get("name") or ""),
        server_version=str(info.get("version") or ""),
        capabilities=capabilities if isinstance(capabilities, Mapping) else {},
        instructions=str(result.get("instructions") or ""),
    )


async def discover_server(transport: ProtocolTransport) -> Handshake:
    """Read a stateless-era server's capabilities through ``server/discover``.

    The revision requires every server to implement this and forbids them from
    declaring capabilities anywhere else, so it is both the version check and
    the capability source. A server whose advertised list does not include a
    revision this platform implements is refused here rather than at its first
    tool call.
    """
    result = await transport.request(SERVER_DISCOVER, None)
    if not isinstance(result, Mapping):
        raise ProtocolError("the server's discovery result is not an object")
    supported = result.get("supportedVersions")
    versions = [str(item) for item in supported] if isinstance(supported, list) else []
    # A server that names no versions is taken at its word only when it is
    # answering the revision we sent, which the transport has already done.
    agreed = negotiate_version(versions) if versions else PREFERRED_PROTOCOL_VERSION
    result_meta = result.get("_meta")
    meta = result_meta if isinstance(result_meta, Mapping) else {}
    server_info = meta.get(META_SERVER_INFO)
    info = server_info if isinstance(server_info, Mapping) else {}
    capabilities = result.get("capabilities")
    return Handshake(
        protocol_version=agreed,
        server_name=str(info.get("name") or ""),
        server_version=str(info.get("version") or ""),
        capabilities=capabilities if isinstance(capabilities, Mapping) else {},
        instructions=str(result.get("instructions") or ""),
    )


def support_states(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    """Say, per optional capability, whether we can honour it.

    Recorded rather than acted on: a server that offers sampling to a platform
    with no sampling handler must be told "unsupported" in the connection's
    diagnostics, because the alternative is a job that hangs at the one call
    that needed it.
    """
    states: dict[str, Any] = {}
    for name, description in OPTIONAL_CAPABILITY_HANDLERS.items():
        root, _, child = name.partition(".")
        declared = capabilities.get(root)
        offered = isinstance(declared, Mapping) and (not child or child in declared)
        if not offered:
            continue
        # Only roots has a handler. Elicitation forms and model sampling need
        # user interaction and authorization paths this host does not provide.
        if name == "roots":
            states[name] = "platform-bridge"
        else:
            states[name] = "unsupported"
        states[f"{name}.detail"] = description
    return states


# --------------------------------------------------------------------------
# Catalogue discovery
# --------------------------------------------------------------------------


def _page_items(result: Any, key: str) -> tuple[list[Mapping[str, Any]], str | None]:
    """Pull one page's items and next cursor out of a reply."""
    if not isinstance(result, Mapping):
        raise ProtocolError(f"the server's {key} reply is not an object")
    items = result.get(key, [])
    if not isinstance(items, list):
        raise ProtocolError(f"the server's {key} reply does not contain a list")
    cursor = result.get("nextCursor")
    return [item for item in items if isinstance(item, Mapping)], (str(cursor) if cursor else None)


async def _collect(
    transport: ProtocolTransport,
    method: str,
    key: str,
    *,
    params: Mapping[str, Any] | None = None,
) -> list[Mapping[str, Any]]:
    """Walk a paginated list method to its end.

    The cursor loop is bounded twice: by the page count, and by a cursor that
    repeats. A server that answers the same cursor forever would otherwise make
    discovery -- which runs on a user's request -- never return.
    """
    collected: list[Mapping[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(MAX_PAGES):
        page_params = dict(params or {})
        if cursor:
            page_params["cursor"] = cursor
        result = await transport.request(method, page_params or None)
        items, cursor = _page_items(result, key)
        collected.extend(items)
        if not cursor:
            return collected
        if cursor in seen_cursors:
            raise ProtocolError(
                f"the server repeated the pagination cursor on {method}; refusing to loop"
            )
        seen_cursors.add(cursor)
    raise ProtocolError(f"the server paginated {method} beyond {MAX_PAGES} pages")


def _tool(item: Mapping[str, Any]) -> Capability:
    """Build a tool capability from one catalogue entry."""
    schema = item.get("inputSchema")
    annotations = item.get("annotations")
    return Capability(
        kind=CapabilityKind.TOOL,
        name=str(item.get("name") or ""),
        description=str(item.get("description") or ""),
        input_schema=schema if isinstance(schema, Mapping) else None,
        annotations=annotations if isinstance(annotations, Mapping) else {},
    )


def _resource(item: Mapping[str, Any]) -> Capability:
    """Build a resource capability. ``uri`` is the server's own identifier."""
    return Capability(
        kind=CapabilityKind.RESOURCE,
        name=str(item.get("uri") or ""),
        description=str(item.get("name") or item.get("description") or ""),
        input_schema={
            "mimeType": item.get("mimeType"),
            "size": item.get("size"),
        },
    )


def _resource_template(item: Mapping[str, Any]) -> Capability:
    """Build a resource-template capability."""
    return Capability(
        kind=CapabilityKind.RESOURCE_TEMPLATE,
        name=str(item.get("uriTemplate") or ""),
        description=str(item.get("name") or item.get("description") or ""),
        input_schema={
            "mimeType": item.get("mimeType"),
        },
    )


def _prompt(item: Mapping[str, Any]) -> Capability:
    """Build a prompt capability, keeping its declared arguments."""
    arguments = item.get("arguments")
    return Capability(
        kind=CapabilityKind.PROMPT,
        name=str(item.get("name") or ""),
        description=str(item.get("description") or ""),
        input_schema={"arguments": arguments} if isinstance(arguments, list) else None,
    )


async def discover(
    transport: ProtocolTransport,
    handshake: Handshake,
    *,
    connection_id: str,
    config_revision: int,
    capability_revision: int,
) -> CapabilitySnapshot:
    """Read every surface the server declared, and record what it offered.

    Only the surfaces the server advertised are read. Asking for
    ``resources/list`` on a server that declared no resources is a protocol
    error from its point of view and produces a failed discovery for a server
    that is working correctly.
    """
    capabilities = handshake.capabilities
    tools: tuple[Capability, ...] = ()
    if isinstance(capabilities.get("tools"), Mapping):
        tools = tuple(_tool(item) for item in await _collect(transport, TOOLS_LIST, "tools"))

    resources: tuple[Capability, ...] = ()
    templates: tuple[Capability, ...] = ()
    if isinstance(capabilities.get("resources"), Mapping):
        resources = tuple(
            _resource(item) for item in await _collect(transport, RESOURCES_LIST, "resources")
        )
        try:
            templates = tuple(
                _resource_template(item)
                for item in await _collect(transport, RESOURCES_TEMPLATES_LIST, "resourceTemplates")
            )
        except JsonRpcError:
            # Templates are optional even when resources are supported: the
            # method is allowed to be absent, and losing it must not lose the
            # concrete resources that were already read.
            templates = ()

    prompts: tuple[Capability, ...] = ()
    if isinstance(capabilities.get("prompts"), Mapping):
        prompts = tuple(
            _prompt(item) for item in await _collect(transport, PROMPTS_LIST, "prompts")
        )

    return CapabilitySnapshot(
        connection_id=connection_id,
        capability_revision=capability_revision,
        config_revision=config_revision,
        protocol_version=handshake.protocol_version,
        server_name=handshake.server_name,
        server_version=handshake.server_version,
        tools=tools,
        resources=resources,
        resource_templates=templates,
        prompts=prompts,
        support=support_states(capabilities),
    )


# --------------------------------------------------------------------------
# Namespacing
# --------------------------------------------------------------------------


def sanitize_name(raw: str) -> str:
    """Make one component safe for a CLI's tool-name grammar."""
    cleaned = _NAME_SAFE.sub("_", (raw or "").strip())
    cleaned = cleaned.strip("_")
    return cleaned or "tool"


def presented_name(alias: str, capability: str) -> str:
    """Build a stable CLI name from the complete alias and upstream name.

    Ordinary short names retain ``alias__tool``. Any lossy normalization,
    truncation or ambiguous alias gets a digest of the original pair, even if
    it is currently the only tool. A numeric suffix based on catalogue order
    would let a cached name reach a different tool after a sibling disappears.
    """
    candidate = f"{sanitize_name(alias)}{NAMESPACE_SEPARATOR}{sanitize_name(capability)}"
    if (
        candidate == f"{alias}{NAMESPACE_SEPARATOR}{capability}"
        and NAMESPACE_SEPARATOR not in alias
        and alias != _HASH_NAMESPACE
        and len(candidate) <= MAX_PRESENTED_NAME
    ):
        return candidate
    digest = hashlib.sha256(json.dumps([alias, capability]).encode()).hexdigest()[:32]
    # Reserve a namespace that survives native CLI normalization. A leading
    # underscore alone is not sufficient: pi strips it before registering tools.
    prefix = f"{_HASH_NAMESPACE}{NAMESPACE_SEPARATOR}"
    budget = MAX_PRESENTED_NAME - len(prefix) - len(digest) - len(NAMESPACE_SEPARATOR)
    return f"{prefix}{candidate[:budget]}{NAMESPACE_SEPARATOR}{digest}"


def build_namespace(
    alias: str,
    capabilities: Sequence[Capability],
) -> dict[str, str]:
    """Map presented names to the server's own names, for one binding.

    Returned as presented -> original. The reverse direction is what a call
    needs and is built by inverting this; keeping one direction as the stored
    artefact means a duplicate can never silently overwrite an entry.
    """
    mapping: dict[str, str] = {}
    for capability in capabilities:
        name = presented_name(alias, capability.name)
        mapping[name] = capability.name
    return mapping


__all__ = [
    "CANCELLED",
    "ELICITATION_CREATE",
    "INITIALIZE",
    "INITIALIZED",
    "MAX_PAGES",
    "MAX_PRESENTED_NAME",
    "MESSAGE",
    "META_CLIENT_CAPABILITIES",
    "META_CLIENT_INFO",
    "META_PROTOCOL_VERSION",
    "META_SERVER_INFO",
    "NAMESPACE_SEPARATOR",
    "OPTIONAL_CAPABILITY_HANDLERS",
    "PING",
    "PROFILES",
    "PROGRESS",
    "PROMPTS_GET",
    "PROMPTS_LIST",
    "RESOURCES_LIST",
    "RESOURCES_READ",
    "RESOURCES_SUBSCRIBE",
    "RESOURCES_TEMPLATES_LIST",
    "RESOURCES_UNSUBSCRIBE",
    "RESULT_TYPE_COMPLETE",
    "RESULT_TYPE_INPUT_REQUIRED",
    "ROOTS_LIST",
    "SAMPLING_CREATE_MESSAGE",
    "SERVER_DISCOVER",
    "SUBSCRIPTIONS_LISTEN",
    "TOOLS_CALL",
    "TOOLS_LIST",
    "Handshake",
    "ProtocolError",
    "ProtocolProfile",
    "ProtocolTransport",
    "build_namespace",
    "client_capabilities",
    "continuation_params",
    "discover",
    "discover_server",
    "initialize",
    "input_requests",
    "input_required",
    "is_stateless",
    "negotiate_version",
    "presented_name",
    "request_headers",
    "request_meta",
    "result_type",
    "sanitize_name",
    "support_states",
]
