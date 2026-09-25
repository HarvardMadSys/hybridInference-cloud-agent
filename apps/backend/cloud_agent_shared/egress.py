"""The sandbox network: one boundary for every task.

Every sandbox group gets a routable bridge of its own, carved from this
deployment's address pool, and the host enforces one fixed rule set on it:

- public IPv4 unicast on TCP 80 and 443;
- the one named DNS resolver, TCP/UDP 53;
- the group's own two relays (inference and MCP), by exact address and port;
- nothing else: not the host, not private, link-local (cloud metadata), CGNAT,
  multicast or reserved space, not another group, not other ports, not UDP 443,
  not IPv6, and no connection started from outside.

Setup and the agent share it. There is no second mode to select, per phase,
per task or per deployment, and a host that cannot prove the rules are in
place runs nothing.

**The rules live on the host, not in the sandbox.** They are one table of this
deployment's in the host's nftables, installed by a trusted helper before any
group network exists (:mod:`cloud_agent_host.network_guard`). A drop in any
base chain on a hook is final, so Docker's own accept rules cannot undo it, and
the table never flushes or edits anyone else's. Traffic is recognised by the
bridge interface it arrives on: every group bridge is named with this
deployment's prefix, so the rules cover each new group without being rewritten.

**Two layers, because Linux has two paths.** Routed traffic (to the internet,
the host, another bridge) crosses the ``inet`` table's forward and input hooks.
Frames between the sandbox and a relay on the same bridge reach those hooks
only when the kernel's bridge netfilter is loaded, so a ``bridge`` table polices
them directly; when bridge netfilter is loaded, the ``inet`` table applies the
same relay exceptions and lets nothing else through.

**Ports, not protocols.** TCP 80/443 is a port rule. Nothing inspects what
travels inside, so this is not a promise that only HTTP leaves, and it does not
stop a sandbox sending the repository or its own short-lived tokens to a public
host. Those tokens are refused at the public entrances of the gateway and the
control plane; that, not this module, is what stops their direct replay.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from cloud_agent_shared.errors import SandboxError

PUBLIC_TCP_PORTS: tuple[int, ...] = (80, 443)
DNS_PORT = 53

#: Every IPv4 block that is not ordinary global unicast (IANA's special-purpose
#: registry): "this network", private and CGNAT space, loopback, link-local and
#: with it cloud metadata, IETF protocol assignments, documentation and
#: benchmarking ranges, deprecated 6to4 relays, multicast, and reserved space up
#: to broadcast. A block here is refused whatever port is asked for.
PROTECTED_IPV4: tuple[str, ...] = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)

#: A group bridge is a /29: its gateway, the sandbox and the two relays.
GROUP_PREFIX_LENGTH = 29

#: Every table this code installs starts with this, so a deployment can find
#: its neighbours' address pools without being told about them.
TABLE_PREFIX = "cloud_agent_"

_INTERFACE_NAME_MAX = 15  # IFNAMSIZ less the terminating NUL.
_NAMESPACE_DIGITS = 5
_BRIDGE_PREFIX = "ca"
_GROUP_DIGITS = _INTERFACE_NAME_MAX - len(_BRIDGE_PREFIX) - _NAMESPACE_DIGITS
_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


class SandboxNetworkError(SandboxError):
    """The sandbox network cannot be configured safely."""


@dataclass(frozen=True)
class RelayEndpoint:
    """A fixed platform service the sandbox reaches on its own bridge."""

    host: str
    port: int


@dataclass(frozen=True)
class SandboxNetwork:
    """This deployment's one network configuration."""

    namespace: str
    pool: ipaddress.IPv4Network
    dns_server: ipaddress.IPv4Address
    relay_network: str
    relays: tuple[RelayEndpoint, ...] = ()
    extra_protected: tuple[ipaddress.IPv4Network, ...] = ()

    @property
    def interface_prefix(self) -> str:
        """The bridge-name prefix that marks this deployment's groups."""
        digest = hashlib.sha256(self.namespace.encode()).hexdigest()
        return _BRIDGE_PREFIX + digest[:_NAMESPACE_DIGITS]

    def bridge_name(self, network_name: str) -> str:
        """The host interface name for one group network, at most 15 bytes."""
        digest = hashlib.sha256(network_name.encode()).hexdigest()
        return self.interface_prefix + digest[:_GROUP_DIGITS]

    @property
    def table(self) -> str:
        """The nftables table name, one per deployment on a host."""
        return TABLE_PREFIX + self.interface_prefix

    @property
    def relay_hosts(self) -> tuple[str, ...]:
        """The relay aliases a group bridge carries, in configuration order."""
        return tuple(dict.fromkeys(relay.host for relay in self.relays))

    @property
    def protected(self) -> tuple[ipaddress.IPv4Network, ...]:
        """Built-in non-public space plus the deployment's own additions."""
        built_in = tuple(ipaddress.IPv4Network(block) for block in PROTECTED_IPV4)
        return tuple(ipaddress.collapse_addresses((*built_in, *self.extra_protected)))


def _ipv4_network(raw: str, *, var: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(raw.strip(), strict=True)
    except ValueError as exc:
        raise SandboxNetworkError(f"{var}={raw!r} is not an IPv4 network: {exc}") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise SandboxNetworkError(f"{var}={raw!r} is not IPv4; sandbox networks are IPv4 only")
    return network


def _relay(raw: str, *, var: str) -> RelayEndpoint | None:
    if not raw.strip():
        return None
    parsed = urlparse(raw.strip())
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if parsed.scheme not in ("http", "https") or not host:
        raise SandboxNetworkError(f"{var}={raw!r} does not name a relay host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # A group bridge gives each relay a fresh address; only a name the
        # sandbox resolves on its own bridge survives that.
        raise SandboxNetworkError(f"{var}={raw!r} names a relay by IP; use its network alias")
    if not _HOSTNAME.fullmatch(host):
        raise SandboxNetworkError(f"{var}={raw!r} does not name a relay host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return RelayEndpoint(host=host, port=port)


def network_from_env(env: dict[str, str] | None = None) -> SandboxNetwork:
    """Read and validate the deployment's network configuration."""
    source = env if env is not None else dict(os.environ)

    namespace = (source.get("AGENT_SANDBOX_NAMESPACE") or "").strip()
    if not namespace:
        raise SandboxNetworkError("AGENT_SANDBOX_NAMESPACE must name this deployment")

    pool_raw = (source.get("AGENT_EGRESS_CHAT_NETWORK_POOL") or "").strip()
    if not pool_raw:
        raise SandboxNetworkError(
            "AGENT_EGRESS_CHAT_NETWORK_POOL must name this deployment's group address pool"
        )
    pool = _ipv4_network(pool_raw, var="AGENT_EGRESS_CHAT_NETWORK_POOL")
    if not pool.is_private or pool.prefixlen > GROUP_PREFIX_LENGTH:
        raise SandboxNetworkError(
            f"AGENT_EGRESS_CHAT_NETWORK_POOL={pool} must be RFC1918 space holding at "
            f"least one /{GROUP_PREFIX_LENGTH}"
        )

    dns_raw = (source.get("AGENT_EGRESS_DNS_SERVER") or "").strip()
    try:
        dns_server = ipaddress.IPv4Address(dns_raw)
    except ValueError as exc:
        raise SandboxNetworkError(
            f"AGENT_EGRESS_DNS_SERVER={dns_raw!r} must be one IPv4 resolver address"
        ) from exc
    if dns_server in pool or dns_server.is_loopback or dns_server.is_unspecified:
        raise SandboxNetworkError(
            f"AGENT_EGRESS_DNS_SERVER={dns_server} must be reachable from a group bridge"
        )

    relay_network = (source.get("AGENT_RELAY_NETWORK") or "").strip()
    if not relay_network:
        raise SandboxNetworkError(
            "AGENT_RELAY_NETWORK must name the network the two relays are discovered on"
        )

    extra = tuple(
        _ipv4_network(item, var="AGENT_EGRESS_PROTECTED_CIDRS")
        for item in re.split(r"[\s,]+", source.get("AGENT_EGRESS_PROTECTED_CIDRS") or "")
        if item
    )

    relays: list[RelayEndpoint] = []
    for var in ("AGENT_GATEWAY_URL", "AGENT_SANDBOX_MCP_URL"):
        if relay := _relay(source.get(var) or "", var=var):
            relays.append(relay)
    return SandboxNetwork(
        namespace=namespace,
        pool=pool,
        dns_server=dns_server,
        relay_network=relay_network,
        relays=tuple(dict.fromkeys(relays)),
        extra_protected=extra,
    )


def render_ruleset(network: SandboxNetwork) -> str:
    """The whole rule set as one atomic ``nft -f`` transaction.

    Safe to re-run while groups are live: tables, sets and chains are declared
    with ``add``; the chains, the protected set and the pool registration are
    replaced wholesale; the relay sets, which change as relays attach to and
    leave group bridges, are left as they are.
    """
    table = network.table
    bridges = f'"{network.interface_prefix}*"'
    pool = str(network.pool)
    dns = str(network.dns_server)
    protected = ", ".join(str(block) for block in network.protected)
    ports = ", ".join(str(port) for port in PUBLIC_TCP_PORTS)
    dns_accept = [
        f"ip daddr {dns} udp dport {DNS_PORT} accept",
        f"ip daddr {dns} tcp dport {DNS_PORT} accept",
    ]
    chains: dict[str, tuple[str, list[str]]] = {
        "forward": (
            "type filter hook forward priority filter - 10; policy accept;",
            [
                f"iifname {bridges} oifname {bridges} jump within_groups",
                f"iifname {bridges} jump from_group",
                f"oifname {bridges} jump to_group",
            ],
        ),
        "within_groups": (
            "",
            [
                # Seen only when bridge netfilter is loaded; the bridge table
                # enforces the same pairs when it is not.
                "iifname . ip daddr . tcp dport @relays accept",
                "iifname . ip saddr . tcp sport @relays accept",
                "counter drop",
            ],
        ),
        "from_group": (
            "",
            [
                "meta nfproto ipv6 counter drop",
                f"ip saddr != {pool} counter drop",
                *dns_accept,
                "ip daddr @protected4 counter reject with icmp type admin-prohibited",
                f"tcp dport {{ {ports} }} accept",
                "meta l4proto tcp counter reject with tcp reset",
                "counter reject with icmp type admin-prohibited",
            ],
        ),
        "to_group": (
            "",
            [
                "meta nfproto ipv6 counter drop",
                f"ip daddr != {pool} counter drop",
                # Path-MTU and unreachable errors for a live connection. They
                # carry no stream of their own and may come from any router.
                "ct state related meta l4proto icmp accept",
                f"ip saddr {dns} ct state established accept",
                "ip saddr @protected4 counter drop",
                "ct state established accept",
                "counter drop",
            ],
        ),
        "input": (
            "type filter hook input priority filter - 10; policy accept;",
            [f"iifname {bridges} jump to_host"],
        ),
        "to_host": (
            "",
            [
                "meta nfproto ipv6 counter drop",
                # Replies to connections the host itself opened into a group.
                "ct state established,related accept",
                # Only reachable when the resolver listens on the host itself:
                # input sees nothing but traffic addressed to a local address.
                *dns_accept,
                "meta l4proto tcp counter reject with tcp reset",
                "counter reject with icmp type admin-prohibited",
            ],
        ),
    }
    lines = [
        f"add table inet {table}",
        f"add set inet {table} pool4 {{ type ipv4_addr; flags interval; }}",
        f"add set inet {table} protected4 {{ type ipv4_addr; flags interval; auto-merge; }}",
        f"add set inet {table} relays {{ type ifname . ipv4_addr . inet_service; }}",
    ]
    for name, (header, _) in chains.items():
        lines.append(
            f"add chain inet {table} {name} {{ {header} }}"
            if header
            else f"add chain inet {table} {name}"
        )
    for name, (_, rules) in chains.items():
        lines.append(f"flush chain inet {table} {name}")
        lines.extend(f"add rule inet {table} {name} {rule}" for rule in rules)
    lines += [
        f"flush set inet {table} pool4",
        f"add element inet {table} pool4 {{ {pool} }}",
        f"flush set inet {table} protected4",
        f"add element inet {table} protected4 {{ {protected} }}",
        f"add table bridge {table}",
        f"add set bridge {table} relays {{ type ifname . ipv4_addr . inet_service; }}",
        f"add chain bridge {table} forward "
        "{ type filter hook forward priority filter - 10; policy accept; }",
        f"add chain bridge {table} within_group",
        f"flush chain bridge {table} forward",
        f"add rule bridge {table} forward meta ibrname {bridges} jump within_group",
        f"flush chain bridge {table} within_group",
        f"add rule bridge {table} within_group ether type arp accept",
        f"add rule bridge {table} within_group meta ibrname . ip daddr . tcp dport @relays accept",
        f"add rule bridge {table} within_group meta ibrname . ip saddr . tcp sport @relays accept",
        f"add rule bridge {table} within_group counter drop",
    ]
    return "\n".join(lines) + "\n"


RELAY_FAMILIES = ("inet", "bridge")


def render_relay_update(
    network: SandboxNetwork,
    *,
    remove: dict[str, list[tuple[str, str, int]]],
    add: list[tuple[str, str, int]],
) -> str:
    """One transaction that swaps relay exceptions in both relay sets.

    ``remove`` is per family because each set is read back separately: an
    element one of them has already lost must not be deleted from it, or the
    whole transaction fails.
    """

    def render(elements: list[tuple[str, str, int]]) -> str:
        return ", ".join(f'"{bridge}" . {address} . {port}' for bridge, address, port in elements)

    lines: list[str] = []
    for family in RELAY_FAMILIES:
        if doomed := remove.get(family):
            lines.append(f"delete element {family} {network.table} relays {{ {render(doomed)} }}")
    if add:
        for family in RELAY_FAMILIES:
            lines.append(f"add element {family} {network.table} relays {{ {render(add)} }}")
    return "\n".join(lines) + ("\n" if lines else "")


__all__ = [
    "DNS_PORT",
    "GROUP_PREFIX_LENGTH",
    "PROTECTED_IPV4",
    "PUBLIC_TCP_PORTS",
    "RELAY_FAMILIES",
    "TABLE_PREFIX",
    "RelayEndpoint",
    "SandboxNetwork",
    "SandboxNetworkError",
    "network_from_env",
    "render_relay_update",
    "render_ruleset",
]
