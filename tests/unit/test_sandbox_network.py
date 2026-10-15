"""The one sandbox network: its configuration and the rules it renders.

These check the pure half, what the host is told to install. Whether a kernel
then enforces it is the external tier's job: real bridges, real nftables,
listening targets and positive controls (``tests/external``).
"""

from __future__ import annotations

import ipaddress

import pytest

from cloud_agent_shared.egress import (
    PROTECTED_IPV4,
    PUBLIC_TCP_PORTS,
    RelayEndpoint,
    SandboxNetworkError,
    network_from_env,
    render_relay_update,
    render_ruleset,
)
from cloud_agent_shared.errors import SandboxError

_ENV = {
    "AGENT_SANDBOX_NAMESPACE": "freeinference-cloud-agent-sandbox-staging",
    "AGENT_EGRESS_CHAT_NETWORK_POOL": "10.240.0.0/16",
    "AGENT_EGRESS_DNS_SERVER": "1.1.1.1",
    "AGENT_RELAY_NETWORK": "agent-relays",
    "AGENT_GATEWAY_URL": "http://agent-gateway:8080",
    "AGENT_SANDBOX_MCP_URL": "http://agent-mcp:8000",
}


def _network(**overrides: str):
    return network_from_env({**_ENV, **overrides})


def _rules(text: str, chain: str) -> list[str]:
    marker = f" {chain} "
    return [line for line in text.splitlines() if line.startswith("add rule") and marker in line]


def test_a_complete_environment_is_one_network() -> None:
    network = _network()
    assert network.pool == ipaddress.IPv4Network("10.240.0.0/16")
    assert network.dns_server == ipaddress.IPv4Address("1.1.1.1")
    assert network.relay_network == "agent-relays"
    assert network.relays == (
        RelayEndpoint("agent-gateway", 8080),
        RelayEndpoint("agent-mcp", 8000),
    )
    assert network.relay_hosts == ("agent-gateway", "agent-mcp")


def test_a_configuration_error_is_a_sandbox_error() -> None:
    """Runner and sandbox manager startup already refuse on SandboxError; no new type."""
    assert issubclass(SandboxNetworkError, SandboxError)


def test_bridge_names_fit_the_kernel_and_carry_the_deployment_prefix() -> None:
    network = _network()
    name = network.bridge_name("freeinference-chat-net-" + "a" * 24)
    assert len(name) <= 15
    assert name.startswith(network.interface_prefix)
    assert network.bridge_name("x") != network.bridge_name("y")


def test_two_deployments_on_one_host_do_not_share_a_prefix_or_table() -> None:
    staging = _network()
    prod = _network(AGENT_SANDBOX_NAMESPACE="freeinference-cloud-agent-sandbox-prod")
    assert staging.interface_prefix != prod.interface_prefix
    assert staging.table != prod.table


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("AGENT_SANDBOX_NAMESPACE", ""),
        ("AGENT_EGRESS_CHAT_NETWORK_POOL", ""),
        ("AGENT_EGRESS_CHAT_NETWORK_POOL", "8.8.0.0/16"),
        ("AGENT_EGRESS_CHAT_NETWORK_POOL", "10.240.0.0/30"),
        ("AGENT_EGRESS_CHAT_NETWORK_POOL", "10.240.0.1/16"),
        ("AGENT_EGRESS_CHAT_NETWORK_POOL", "fd00::/64"),
        ("AGENT_EGRESS_DNS_SERVER", ""),
        ("AGENT_EGRESS_DNS_SERVER", "dns.example"),
        ("AGENT_EGRESS_DNS_SERVER", "2606:4700::1111"),
        ("AGENT_EGRESS_DNS_SERVER", "10.240.3.4"),
        ("AGENT_EGRESS_DNS_SERVER", "127.0.0.53"),
        ("AGENT_RELAY_NETWORK", ""),
        ("AGENT_EGRESS_PROTECTED_CIDRS", "140.247.0.0/33"),
        ("AGENT_EGRESS_PROTECTED_CIDRS", "2001:db8::/32"),
        ("AGENT_GATEWAY_URL", "agent-gateway:8080"),
        ("AGENT_GATEWAY_URL", "http://10.0.0.5:8080"),
        ("AGENT_SANDBOX_MCP_URL", "ftp://agent-mcp:8000"),
    ],
)
def test_an_unsafe_or_incomplete_setting_is_refused(var: str, value: str) -> None:
    with pytest.raises(SandboxNetworkError):
        _network(**{var: value})


def test_there_is_no_mode_to_select() -> None:
    """The retired tier variables change nothing, rather than being honoured."""
    retired = {
        "AGENT_EGRESS_SETUP_TIER": "full",
        "AGENT_EGRESS_AGENT_TIER": "custom",
        "AGENT_SANDBOX_ALLOW_OPEN_NETWORK": "1",
        "AGENT_SANDBOX_NETWORK": "bridge",
        "AGENT_EGRESS_ALLOWLIST": "example.com",
    }
    assert network_from_env({**_ENV, **retired}) == _network()


def test_every_protected_block_is_outside_global_unicast() -> None:
    # The 6to4 relay anycast block is still "global" to Python, but RFC 7526
    # deprecated it and nothing legitimate should be reached there any more.
    # Azure's WireServer is a platform service on a public address.
    deliberate = {"192.88.99.0/24", "168.63.129.16/32"}
    for block in PROTECTED_IPV4:
        network = ipaddress.IPv4Network(block)
        if block in deliberate or network.is_multicast:
            # Python calls multicast "global"; it is still not unicast.
            continue
        assert not network.network_address.is_global, block
        assert not network[-1].is_global, block


@pytest.mark.parametrize("address", ["1.1.1.1", "8.8.8.8", "140.82.114.5", "151.101.0.223"])
def test_ordinary_public_hosts_are_not_protected(address: str) -> None:
    network = _network()
    assert not any(ipaddress.IPv4Address(address) in block for block in network.protected)


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "172.17.0.1",
        "192.168.1.1",
        "224.0.0.1",
        "255.255.255.255",
        "0.0.0.0",
        "127.0.0.1",
        "168.63.129.16",
        "100.100.100.200",
    ],
)
def test_the_usual_internal_targets_are_protected(address: str) -> None:
    network = _network()
    assert any(ipaddress.IPv4Address(address) in block for block in network.protected)


def test_the_installed_table_refuses_azures_platform_address() -> None:
    """WireServer is public address space, so only an explicit entry covers it."""
    network = _network()
    assert ipaddress.IPv4Address("168.63.129.16").is_global
    elements = next(
        line
        for line in render_ruleset(network).splitlines()
        if line.startswith(f"add element inet {network.table} protected4 ")
    )
    assert "168.63.129.16/32" in elements.split("{", 1)[1].rstrip(" }").split(", ")
    # Still refused when an operator's own additions collapse around it.
    widened = _network(AGENT_EGRESS_PROTECTED_CIDRS="168.63.0.0/16")
    assert any(ipaddress.IPv4Address("168.63.129.16") in b for b in widened.protected)


def test_operator_additions_only_widen_protection() -> None:
    network = _network(AGENT_EGRESS_PROTECTED_CIDRS="140.247.0.0/16, 128.103.0.0/16")
    blocks = network.protected
    assert any(ipaddress.IPv4Address("140.247.1.1") in block for block in blocks)
    for built_in in PROTECTED_IPV4:
        assert any(ipaddress.IPv4Network(built_in).subnet_of(block) for block in blocks)


def test_the_rules_never_touch_anything_but_this_deployment() -> None:
    network = _network()
    text = render_ruleset(network)
    assert "flush ruleset" not in text
    assert "delete " not in text
    for line in text.splitlines():
        assert f" {network.table}" in line, line
    flushed = [line.split()[-1] for line in text.splitlines() if line.startswith("flush set")]
    assert flushed == ["pool4", "protected4"], "relay exceptions must survive a reinstall"


def test_public_ports_and_only_the_named_resolver_are_opened() -> None:
    text = render_ruleset(_network())
    outbound = _rules(text, "from_group")
    ports = ", ".join(str(port) for port in PUBLIC_TCP_PORTS)
    assert any(f"tcp dport {{ {ports} }} accept" in line for line in outbound)
    dns = [line for line in outbound if "dport 53" in line]
    assert len(dns) == 2 and all("ip daddr 1.1.1.1 " in line for line in dns)
    # Checked before the protected set: a resolver on a private address works.
    first_protected = next(i for i, line in enumerate(outbound) if "@protected4" in line)
    assert all(outbound.index(line) < first_protected for line in dns)
    # And the chain ends refusing everything else.
    assert "reject" in outbound[-1]


def test_every_rule_set_path_refuses_ipv6_and_foreign_sources() -> None:
    text = render_ruleset(_network())
    for chain in ("from_group", "to_group", "to_host"):
        assert "meta nfproto ipv6 counter drop" in _rules(text, chain)[0]
    assert any(
        "ip saddr != 10.240.0.0/16 counter drop" in line for line in _rules(text, "from_group")
    )


def test_the_host_is_refused_except_for_a_local_resolver() -> None:
    to_host = _rules(render_ruleset(_network(AGENT_EGRESS_DNS_SERVER="192.168.5.1")), "to_host")
    assert [line for line in to_host if "dport 53" in line] == [
        line for line in to_host if "ip daddr 192.168.5.1 " in line
    ]
    assert "reject" in to_host[-1]


def test_established_traffic_is_rechecked_against_the_protected_set() -> None:
    """No blanket ct accept: a newly protected destination stops a live flow."""
    text = render_ruleset(_network())
    inbound = _rules(text, "to_group")
    drop = next(i for i, line in enumerate(inbound) if "ip saddr @protected4" in line)
    accept = next(
        i
        for i, line in enumerate(inbound)
        if line.endswith("ct state established accept") and "saddr 1.1.1.1" not in line
    )
    assert drop < accept
    assert not any("ct state established" in line for line in _rules(text, "from_group"))


def test_relays_are_exact_in_both_the_routed_and_the_bridged_path() -> None:
    text = render_ruleset(_network())
    assert "add set inet" in text and "relays { type ifname . ipv4_addr . inet_service; }" in text
    bridged = [line for line in text.splitlines() if line.startswith("add rule bridge")]
    assert any("meta ibrname . ip daddr . tcp dport @relays accept" in line for line in bridged)
    assert bridged[-1].endswith("counter drop")


def test_relay_updates_touch_only_the_relay_sets() -> None:
    network = _network()
    bridge = network.bridge_name("freeinference-chat-net-x")
    text = render_relay_update(
        network,
        remove={"inet": [(bridge, "10.240.0.3", 8080)]},
        add=[(bridge, "10.240.0.4", 8080)],
    )
    lines = text.splitlines()
    assert lines[0].startswith(f"delete element inet {network.table} relays")
    assert sum(line.startswith("add element") for line in lines) == 2
    assert all(f'"{bridge}"' in line for line in lines)
    assert render_relay_update(network, remove={}, add=[]) == ""


def test_rendering_is_deterministic() -> None:
    assert render_ruleset(_network()) == render_ruleset(_network())


def test_ssh_targets_sit_between_the_protected_set_and_the_public_ports() -> None:
    network = _network()
    text = render_ruleset(network)
    assert (
        f"add set inet {network.table} ssh_targets "
        "{ type ifname . ipv4_addr . inet_service; }" in text
    )
    outbound = _rules(text, "from_group")
    protected = next(i for i, line in enumerate(outbound) if "@protected4" in line)
    ssh = next(i for i, line in enumerate(outbound) if "@ssh_targets" in line)
    public = next(i for i, line in enumerate(outbound) if "tcp dport {" in line)
    # Protected space stays refused even if an element names it, and the
    # exception is exact: this bridge, this address, this port.
    assert protected < ssh < public
    assert outbound[ssh].endswith("iifname . ip daddr . tcp dport @ssh_targets accept")
    # Routed traffic only: the bridged path never consults it.
    bridged = [line for line in text.splitlines() if line.startswith("add rule bridge")]
    assert not any("ssh_targets" in line for line in bridged)
