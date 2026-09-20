"""What a connection may dial, and where its credential may travel.

A user-supplied URL turns this service into a request forwarder, so every
address it might reach is checked before anything is sent. The checks are
deliberately in one module because the failure mode of scattering them is the
one that matters: a redirect, an OAuth metadata document, a token endpoint and
a legacy SSE ``message`` endpoint are four different places a URL can come from,
and a policy applied at three of them is a policy with a hole.

Three rules shape the code:

**Resolve, then check every address.** A hostname is not an address. The check
runs over the addresses the resolver returned, so a name that resolves to both
a public address and ``127.0.0.1`` is refused rather than raced.

**A credential is bound to an origin.** It is attached when the request goes to
the origin it belongs to and never when it does not, which is what stops a
redirect -- or a compromised metadata document -- from collecting it.

**Private space is reached through a connector, not through a hole.** A
deployment may allow specific private ranges for its own connector bridges by
configuration, and nothing else. The default is public-only.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

#: The addresses each name was validated at in the current task, for the dial
#: that follows the check. A context variable rather than a module-level map:
#: two jobs may reach the same hostname through different connections, and a
#: pin recorded by one request must not become another request's destination.
#:
#: Keyed by every spelling the dialler might use. A name is validated as the
#: text the user typed and dialled after ``httpcore`` has encoded it, so a host
#: with a non-ASCII label arrives at the connection as its IDNA form -- and a
#: pin stored under the other spelling is a pin that is never found, which
#: silently restores the lookup this exists to remove.
_ACTIVE_PINS: ContextVar[Mapping[str, tuple[str, ...]] | None] = ContextVar(
    "mcp_dial_pins", default=None
)

#: Headers a user may not set on their own connection. The list is about
#: control, not secrecy: each of these either re-targets the request, strips
#: our credential, or asks an intermediary to do something on the caller's
#: behalf.
FORBIDDEN_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "host",
        "connection",
        "content-length",
        "cookie",
        "cookie2",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "via",
    }
)

#: Prefixes a user may not set: forwarding headers let a caller claim an
#: identity it does not have, and ``x-`` platform headers are ours.
FORBIDDEN_HEADER_PREFIXES: tuple[str, ...] = ("x-forwarded-", "x-real-ip", "forwarded")

#: Addresses that are always refused, whatever the deployment policy says.
#: Link-local covers the cloud metadata services (``169.254.169.254`` and
#: ``fd00:ec2::254``); the rest is the platform's own plumbing.
ALWAYS_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
)

#: Refused unless the deployment explicitly widens the policy for connector
#: traffic. Loopback is in neither list's default allowance: a remote
#: connection that reaches loopback reaches *this* service.
PRIVATE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)

#: Redirects are followed only to these schemes, and only this many times. A
#: redirect chain is a way to spend our credential on somebody else's host, so
#: the limit is a control rather than a robustness measure.
MAX_REDIRECTS = 3
ALLOWED_SCHEMES = frozenset({"https"})
#: Plain HTTP is refused for a remote server: the credential would travel in
#: the clear. A deployment may allow it for a connector bridge on a private
#: network, which is a deliberate, reviewable choice.
ALLOWED_SCHEMES_INSECURE = frozenset({"https", "http"})


class UncheckedTarget(OSError):
    """A name nothing in this task validated was about to be dialled.

    Raised instead of dialling by name. A miss is what a rebinding name is
    trying to cause, and a fallback to the resolver turns "the check did not
    cover this" into "the check did not happen" -- silently, at the socket,
    which is exactly the failure the pin exists to prevent. Loud here means a
    missing guard is a bug that shows up in the first test that dials.
    """


class EndpointRejected(ValueError):
    """Raised when an address may not be dialled, with an actionable reason."""

    def __init__(self, message: str, *, kind: str = "blocked_target") -> None:
        """Record the diagnosis kind alongside the message."""
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class EgressPolicy:
    """One deployment's answer to "what may a connection reach".

    ``allow_private`` is for a user connector bridge that genuinely lives on a
    private network; it is still bounded by :data:`ALWAYS_BLOCKED_NETWORKS`, so
    widening it cannot reach the metadata service.
    """

    allow_private: bool = False
    allow_plain_http: bool = False
    #: Extra hosts, exact matches only, that bypass the address checks. For a
    #: deployment whose connector gateway resolves to a private address the
    #: operator has decided to trust.
    allowed_hosts: frozenset[str] = frozenset()
    max_redirects: int = MAX_REDIRECTS
    #: Hosts a redirect may land on, on top of the request's own origin. Empty
    #: means "the origin only".
    allowed_redirect_hosts: frozenset[str] = frozenset()

    @property
    def schemes(self) -> frozenset[str]:
        """The URL schemes this policy accepts."""
        return ALLOWED_SCHEMES_INSECURE if self.allow_plain_http else ALLOWED_SCHEMES


@dataclass(frozen=True)
class NormalizedEndpoint:
    """A validated endpoint: the URL as it will be dialled, and its origin."""

    url: str
    scheme: str
    host: str
    port: int
    origin: str
    path: str = ""
    #: The addresses the name resolved to at validation time. Recorded so a
    #: caller can pin the connection to them and defeat a rebinding answer.
    addresses: tuple[str, ...] = field(default_factory=tuple)

    def same_origin(self, other: str) -> bool:
        """Whether ``other`` has this endpoint's scheme, host and port."""
        try:
            parts = urlsplit(other)
        except ValueError:
            return False
        port = parts.port or _default_port(parts.scheme)
        return (
            parts.scheme == self.scheme
            and (parts.hostname or "").lower() == self.host.lower()
            and port == self.port
        )


def _default_port(scheme: str) -> int:
    """The port a scheme implies when the URL does not name one."""
    return 443 if scheme == "https" else 80


def normalize_url(raw: str) -> NormalizedEndpoint:
    """Parse a URL into the shape the policy checks, rejecting malformed input.

    Deliberately does no network work: this is the syntax half, so a caller can
    validate a wall of user input without resolving anything.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise EndpointRejected("a connection needs a URL", kind="unreachable")
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise EndpointRejected(f"that URL cannot be parsed: {exc}") from exc
    if not parts.scheme or not parts.hostname:
        raise EndpointRejected("that URL needs a scheme and a host")
    if parts.username or parts.password:
        # Credentials in a URL would be replayed into logs, the database and
        # every error message. There is a field for the secret.
        raise EndpointRejected("put the credential in the connection's auth settings, not the URL")
    try:
        port = parts.port
    except ValueError as exc:
        raise EndpointRejected(f"that URL has an invalid port: {exc}") from exc
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    resolved_port = port or _default_port(scheme)
    netloc = host if port is None else f"{host}:{port}"
    path = parts.path or "/"
    return NormalizedEndpoint(
        url=urlunsplit((scheme, netloc, path, parts.query, "")),
        scheme=scheme,
        host=host,
        port=resolved_port,
        origin=f"{scheme}://{netloc}",
        path=path,
    )


def _blocked_reason(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, policy: EgressPolicy
) -> str | None:
    """Why one address is refused, or ``None`` when it may be dialled."""
    for network in ALWAYS_BLOCKED_NETWORKS:
        if address.version == network.version and address in network:
            return f"{address} is in a range this platform never dials ({network})"
    if policy.allow_private:
        return None
    for network in PRIVATE_NETWORKS:
        if address.version == network.version and address in network:
            return (
                f"{address} is a private or loopback address. Connect this server through "
                "a user connector instead of a public URL."
            )
    return None


def check_addresses(
    host: str,
    addresses: Iterable[str],
    policy: EgressPolicy,
) -> tuple[str, ...]:
    """Validate every address a host resolved to, returning them unchanged.

    Every address is checked rather than the first: a name answering with one
    public and one private address is a rebinding attempt whether or not that
    was the intent, and accepting it because the resolver happened to return
    the public one first is a race the attacker wins by answering twice.

    The checked answer is also *recorded* for the dial that follows, because a
    check the connection does not have to honour is decoration: the second
    lookup is the one a rebinding name answers differently. What is recorded is
    the first validated address, and it is recorded for this task only -- two
    requests may reach one hostname through different connections, and one
    request's answer must not become another's destination.
    """
    if host.lower() in policy.allowed_hosts:
        resolved = tuple(addresses)
        _pin(host, resolved)
        return resolved
    resolved = tuple(addresses)
    if not resolved:
        raise EndpointRejected(f"{host} did not resolve to any address", kind="unreachable")
    for raw in resolved:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise EndpointRejected(f"{host} resolved to something that is not an address") from exc
        # An IPv4-mapped IPv6 address (``::ffff:127.0.0.1``) is the classic way
        # to slip a loopback past an IPv4-only check, so it is unwrapped first.
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        reason = _blocked_reason(address, policy)
        if reason is not None:
            raise EndpointRejected(reason)
    _pin(host, resolved)
    return resolved


def dial_spellings(host: str) -> tuple[str, ...]:
    """Every spelling of ``host`` a dialler might ask for.

    The checked spelling is the one in the URL; the dialled one is what the
    connection layer builds from it, which is the UTS-46 form for a host with a
    non-ASCII label. Both are recorded so the lookup cannot miss on the
    difference -- and a lookup that misses is indistinguishable, at the socket,
    from a name nothing checked.
    """
    candidate = host.strip().strip(".").lower()
    if not candidate:
        return ()
    if _as_address(candidate) is not None:
        return (candidate,)
    encoded = _encoded_host(candidate)
    if encoded is None or encoded == candidate:
        return (candidate,)
    return (candidate, encoded)


def _encoded_host(host: str) -> str | None:
    """The form the connection layer dials, which is what the pin is found under.

    **Not** ``str.encode("idna")``. That is the 2003 encoding, and it is not
    what this stack uses: it maps ``faß.example`` to ``fass.example`` while
    httpx and anyio both apply the 2008/UTS-46 rule and dial
    ``xn--fa-hia.example``. A pin recorded under the wrong spelling is never
    found, and a pin that is never found is the lookup this exists to remove --
    so the spelling is asked of httpx, the component that builds the
    connection, rather than derived here a second way.
    """
    import httpx

    try:
        return httpx.URL(f"https://{host}/").raw_host.decode("ascii").lower()
    except Exception:
        return None


def dial_host(host: str) -> str:
    """The host to hand the resolver, which is the one that will be dialled.

    ``socket.getaddrinfo`` applies the **2003** IDNA codec to a non-ASCII name,
    and the connection layer applies UTS-46: asking for ``faß.example`` resolves
    ``fass.example`` and then dials ``xn--fa-hia.example``, so the addresses
    that pass the check belong to a domain nobody connects to. The whole chain
    -- resolve, check, dial -- has to name one host, and the one it has to name
    is the dialler's.
    """
    spellings = dial_spellings(host)
    return spellings[-1] if spellings else host


def _as_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``host`` as an IP address, or ``None`` when it is a name."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _pin(host: str, addresses: tuple[str, ...]) -> None:
    """Record every address the next dial for ``host`` in this task may use.

    All of them, not the first: a dual-stack name whose IPv6 route is missing
    has to be able to fall back to the IPv4 address that was checked in the
    same answer. Both are already validated, so trying the next one is not a
    widening -- it is what the check covered.
    """
    if not addresses:
        return
    current = dict(_ACTIVE_PINS.get() or {})
    for spelling in dial_spellings(host):
        current[spelling] = tuple(addresses)
    _ACTIVE_PINS.set(current)


def pinned_addresses(host: str) -> tuple[str, ...]:
    """The addresses ``host`` was validated at in this task, if it was.

    Empty means nothing here has checked that name, so a caller that dials by
    name is doing what it did before this pin existed rather than something
    new: the pin only ever narrows where a checked name may land.
    """
    pins = _ACTIVE_PINS.get() or {}
    for spelling in dial_spellings(host):
        if spelling in pins:
            return pins[spelling]
    return ()


def validate_endpoint(raw: str, policy: EgressPolicy) -> NormalizedEndpoint:
    """Validate a URL's scheme against ``policy``, without resolving it.

    Kept separate from :func:`check_addresses` so that the syntax decision can
    be made while a user is still typing, and the address decision -- which
    costs a DNS lookup -- only when a connection is actually tested or used.
    """
    endpoint = normalize_url(raw)
    if endpoint.scheme not in policy.schemes:
        allowed = ", ".join(sorted(policy.schemes))
        raise EndpointRejected(f"{endpoint.scheme} is not allowed here; use one of: {allowed}")
    return endpoint


def validate_redirect(
    from_endpoint: NormalizedEndpoint,
    location: str,
    policy: EgressPolicy,
    *,
    hops: int,
    credential_origin: str,
) -> NormalizedEndpoint:
    """Validate one redirect hop, and say whether the credential may follow.

    A redirect that leaves the credential's origin is refused outright rather
    than followed without the header: following it would make this service an
    open request forwarder for anyone who can add a connection, and the honest
    answer to "your server redirected me somewhere else" is a diagnosis.
    """
    if hops >= policy.max_redirects:
        raise EndpointRejected(f"more than {policy.max_redirects} redirects")
    target = validate_endpoint(location, policy)
    permitted = {credential_origin} | set(policy.allowed_redirect_hosts)
    if target.origin not in permitted:
        raise EndpointRejected(
            f"the server redirected to {target.origin}, which is not the origin this "
            "connection's credential belongs to"
        )
    if target.origin != from_endpoint.origin and target.origin != credential_origin:
        raise EndpointRejected(f"the server redirected to {target.origin}")
    return target


def sanitize_headers(headers: Mapping[str, str], *, max_bytes: int = 8192) -> dict[str, str]:
    """Strip headers a connection may not set, and bound what is left.

    Case-insensitive, because ``Host`` and ``host`` are the same header, and a
    deny-list that only knows one spelling is not a deny-list.
    """
    cleaned: dict[str, str] = {}
    total = 0
    for raw_name, value in headers.items():
        name = (raw_name or "").strip()
        lowered = name.lower()
        if not name or lowered in FORBIDDEN_HEADER_NAMES:
            continue
        if any(lowered.startswith(prefix) for prefix in FORBIDDEN_HEADER_PREFIXES):
            continue
        if not isinstance(value, str):
            raise EndpointRejected(f"header {name!r} must have a string value")
        if "\n" in value or "\r" in value:
            # Header injection: the value would split into a second header, or
            # end the request entirely.
            raise EndpointRejected(f"header {name!r} contains a line break")
        total += len(name) + len(value)
        if total > max_bytes:
            raise EndpointRejected("this connection sets more header data than a request may carry")
        cleaned[name] = value
    return cleaned


def resource_covers(resource: str | None, target: str) -> bool:
    """Whether an OAuth ``resource`` value covers the URL being dialled.

    Compared by origin first and path prefix second, which is what RFC 8707
    asks for and what stops a token minted for one server from being replayed
    against another on the same host.
    """
    if not resource:
        return True
    try:
        declared = normalize_url(resource)
        actual = normalize_url(target)
    except EndpointRejected:
        return False
    if declared.origin != actual.origin:
        return False
    if declared.path in ("", "/"):
        return True
    return actual.path.startswith(declared.path.rstrip("/") + "/") or actual.path == declared.path


def policy_from_environment() -> EgressPolicy:
    """Build the deployment's egress policy from its two switches.

    Read here rather than at each call site because there are now two of them --
    the connection API that tests a server and the relay host that dials one --
    and a policy applied at one of two doors is not a policy. Both default off:
    private space is reached through a connector, and plain HTTP would put the
    credential on the wire in the clear.
    """
    import os

    return EgressPolicy(
        allow_private=os.environ.get("AGENT_MCP_ALLOW_PRIVATE_TARGETS", "").lower()
        in ("1", "true", "yes"),
        allow_plain_http=os.environ.get("AGENT_MCP_ALLOW_PLAIN_HTTP", "").lower()
        in ("1", "true", "yes"),
    )


def parse_header_pairs(raw: Any) -> dict[str, str]:
    """Accept the several shapes a user's header list arrives in.

    Imported configuration uses a JSON object; a hand-written form sometimes
    sends ``[{"name": ..., "value": ...}]``. Both are accepted so that an
    import failure never looks like an unsupported server.
    """
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        pairs: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise EndpointRejected("each header must be an object with a name and a value")
            name = item.get("name")
            if not name:
                raise EndpointRejected("each header must have a name")
            pairs[str(name)] = str(item.get("value", ""))
        return pairs
    raise EndpointRejected("headers must be an object or a list of name/value pairs")


__all__ = [
    "ALLOWED_SCHEMES",
    "ALLOWED_SCHEMES_INSECURE",
    "ALWAYS_BLOCKED_NETWORKS",
    "FORBIDDEN_HEADER_NAMES",
    "FORBIDDEN_HEADER_PREFIXES",
    "MAX_REDIRECTS",
    "PRIVATE_NETWORKS",
    "EgressPolicy",
    "EndpointRejected",
    "NormalizedEndpoint",
    "UncheckedTarget",
    "check_addresses",
    "dial_host",
    "dial_spellings",
    "normalize_url",
    "parse_header_pairs",
    "pinned_addresses",
    "policy_from_environment",
    "resource_covers",
    "sanitize_headers",
    "validate_endpoint",
    "validate_redirect",
]
