"""The host network guard, against a model of nft rather than a kernel.

The model applies exactly the transactions the guard sends and answers its
JSON listings the way nft prints them, so installation, verification, relay
bookkeeping and the neighbour check are exercised end to end in-process. The
external tier runs the same guard against a real kernel.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import pytest

from cloud_agent_host.network_guard import HostNetworkGuard, NetworkGuardError
from cloud_agent_shared.egress import SandboxNetwork, network_from_env

_ENV = {
    "AGENT_SANDBOX_NAMESPACE": "stack-a",
    "AGENT_EGRESS_CHAT_NETWORK_POOL": "10.240.0.0/16",
    "AGENT_EGRESS_DNS_SERVER": "1.1.1.1",
    "AGENT_RELAY_NETWORK": "agent-relays",
    "AGENT_GATEWAY_URL": "http://agent-gateway:8080",
    "AGENT_SANDBOX_MCP_URL": "http://agent-mcp:8000",
}
_IMAGE = "sha256:" + "a" * 64


class FakeNft:
    """A tiny nft: tables, chains, rule counts and sets, per family."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.tables: dict[tuple[str, str], dict[str, Any]] = {}
        self._next_handle = 1
        #: What ``ip -4 -j route show table all`` answers on this host.
        self.routes: list[dict[str, Any]] = [
            {"dst": "default", "gateway": "192.168.1.1", "dev": "eth0", "table": "main"},
            {"dst": "192.168.1.0/24", "dev": "eth0", "table": "main"},
            {"type": "local", "dst": "192.168.1.10", "dev": "eth0", "table": "local"},
            {"dst": "172.17.0.0/16", "dev": "docker0", "table": "main"},
        ]

    # The guard's runner signature.
    def __call__(self, argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, stdin))
        if argv[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, _IMAGE + "\n", "")
        entrypoint = argv[argv.index("--entrypoint") + 1]
        args = argv[argv.index("--entrypoint") + 3 :]
        if entrypoint == "ip":
            assert args == ["-4", "-j", "route", "show", "table", "all"], args
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.routes), "")
        try:
            if args[:2] == ["-f", "-"]:
                self.apply(stdin or "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            assert args[0] == "-j"
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.listing(args[1:])), "")
        except KeyError as exc:
            return subprocess.CompletedProcess(argv, 1, "", f"No such file or directory: {exc}")

    def table(self, family: str, name: str) -> dict[str, Any]:
        return self.tables[(family, name)]

    def apply(self, text: str) -> None:
        for line in text.splitlines():
            words = line.split()
            verb, kind, family, name = words[0], words[1], words[2], words[3]
            if (verb, kind) == ("add", "table"):
                if (family, name) not in self.tables:
                    self.tables[(family, name)] = {
                        "handle": self._next_handle,
                        "chains": {},
                        "rules": {},
                        "sets": {},
                    }
                    self._next_handle += 1
                continue
            table = self.table(family, name)
            if (verb, kind) == ("add", "set"):
                table["sets"].setdefault(words[4], [])
            elif (verb, kind) == ("add", "chain"):
                hook = re.search(r"hook (\w+)", line)
                table["chains"].setdefault(
                    words[4],
                    {"hook": hook.group(1) if hook else None, "policy": "accept" if hook else None},
                )
                table["rules"].setdefault(words[4], 0)
            elif (verb, kind) == ("flush", "chain"):
                table["rules"][words[4]] = 0
            elif (verb, kind) == ("add", "rule"):
                table["rules"][words[4]] += 1
            elif (verb, kind) == ("flush", "set"):
                table["sets"][words[4]] = []
            elif kind == "element":
                elements = [
                    self._element(raw) for raw in line.split("{", 1)[1].rstrip(" }").split(",")
                ]
                current = table["sets"][words[4]]
                for element in elements:
                    if verb == "add" and element not in current:
                        current.append(element)
                    elif verb == "delete":
                        current.remove(element)
            else:
                raise AssertionError(f"unmodelled nft line: {line}")

    @staticmethod
    def _element(raw: str) -> Any:
        raw = raw.strip()
        if " . " in raw:
            bridge, address, port = (part.strip() for part in raw.split(" . "))
            return {"concat": [bridge.strip('"'), address, int(port)]}
        if "/" in raw:
            address, length = raw.split("/")
            return {"prefix": {"addr": address, "len": int(length)}}
        return raw

    def listing(self, args: list[str]) -> dict[str, Any]:
        if args[:2] == ["list", "tables"]:
            return {
                "nftables": [
                    {"table": {"family": family, "name": name, "handle": body["handle"]}}
                    for (family, name), body in self.tables.items()
                    if family == args[2]
                ]
            }
        if args[:2] == ["list", "set"]:
            family, name, set_name = args[2:5]
            elements = self.table(family, name)["sets"][set_name]
            return {"nftables": [{"set": {"name": set_name, "elem": list(elements)}}]}
        assert args[:2] == ["list", "table"]
        body = self.table(args[2], args[3])
        items: list[dict[str, Any]] = [{"table": {"name": args[3]}}]
        for chain, meta in body["chains"].items():
            items.append({"chain": {"name": chain, **{k: v for k, v in meta.items() if v}}})
            items.extend({"rule": {"chain": chain}} for _ in range(body["rules"][chain]))
        return {"nftables": items}


def _network(**overrides: str) -> SandboxNetwork:
    return network_from_env({**_ENV, **overrides})


def _guard(nft: FakeNft, network: SandboxNetwork | None = None) -> HostNetworkGuard:
    return HostNetworkGuard(network or _network(), helper_image=_IMAGE, run=nft)


def test_the_helper_is_the_host_kernel_with_only_net_admin() -> None:
    nft = FakeNft()
    _guard(nft).ensure()
    argv = nft.calls[0][0]
    assert argv[:3] == ["docker", "run", "--rm"]
    for flag, value in (
        ("--network", "host"),
        ("--runtime", "runc"),
        ("--cap-drop", "ALL"),
        ("--cap-add", "NET_ADMIN"),
        ("--security-opt", "no-new-privileges"),
        ("--pull", "never"),
        ("--entrypoint", "nft"),
    ):
        assert argv[argv.index(flag) + 1] == value, flag
    assert "--read-only" in argv
    assert argv.count("--cap-add") == 1
    assert "--privileged" not in argv


def test_ensure_installs_both_tables_and_trusts_the_result_for_a_while() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    network = guard.network
    guard.ensure()
    assert {("inet", network.table), ("bridge", network.table)} <= set(nft.tables)
    installs = sum(1 for _, stdin in nft.calls if stdin)
    guard.ensure()
    assert sum(1 for _, stdin in nft.calls if stdin) == installs, "a fresh check is reused"
    guard.ensure(max_age_s=0)
    assert sum(1 for _, stdin in nft.calls if stdin) == installs + 1


def test_a_flushed_chain_is_detected_and_repaired() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    nft.table("inet", guard.network.table)["rules"]["from_group"] = 0
    # A verification alone sees it...
    from cloud_agent_shared.egress import render_ruleset

    with pytest.raises(NetworkGuardError, match="from_group"):
        guard._verify(render_ruleset(guard.network))
    # ...and the next ensure reinstalls it.
    guard.ensure(max_age_s=0)
    assert nft.table("inet", guard.network.table)["rules"]["from_group"] == 8


def test_a_chain_without_its_hook_fails_verification() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    nft.table("inet", guard.network.table)["chains"]["forward"]["hook"] = "output"
    from cloud_agent_shared.egress import render_ruleset

    with pytest.raises(NetworkGuardError, match="not hooked"):
        guard._verify(render_ruleset(guard.network))


def test_a_changed_protected_set_fails_verification() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    protected = nft.table("inet", guard.network.table)["sets"]["protected4"]
    protected.remove({"prefix": {"addr": "169.254.0.0", "len": 16}})
    from cloud_agent_shared.egress import render_ruleset

    with pytest.raises(NetworkGuardError, match="protects"):
        guard._verify(render_ruleset(guard.network))


def test_a_later_overlapping_neighbour_refuses_and_the_earlier_keeps_running() -> None:
    nft = FakeNft()
    earlier = _guard(nft)
    earlier.ensure()
    later = _guard(
        nft,
        _network(
            AGENT_SANDBOX_NAMESPACE="stack-b", AGENT_EGRESS_CHAT_NETWORK_POOL="10.240.128.0/17"
        ),
    )
    with pytest.raises(NetworkGuardError, match="overlaps"):
        later.ensure()
    earlier.ensure(max_age_s=0)  # does not raise


@pytest.mark.parametrize(
    "route",
    [
        {"dst": "10.240.0.0/16", "dev": "eth1", "table": "main"},  # the lab LAN itself
        {"dst": "10.0.0.0/8", "dev": "tun0", "table": "main"},  # a VPN around it
        {"dst": "10.240.3.0/24", "dev": "wg0", "table": "51820"},  # inside it, any table
        {"type": "unreachable", "dst": "10.240.9.0/24", "table": "main"},
    ],
)
def test_a_pool_that_shares_addresses_with_a_host_route_is_refused(route) -> None:
    """Docker would take the subnet as given and steal the host's traffic for it."""
    nft = FakeNft()
    nft.routes.append(route)
    guard = _guard(nft)
    with pytest.raises(NetworkGuardError, match="overlaps the host route"):
        guard.ensure()
    # A refusal is never remembered as a pass: the next activation asks again.
    with pytest.raises(NetworkGuardError, match="overlaps the host route"):
        guard.ensure()


def test_default_routes_and_this_deployments_own_bridges_are_not_conflicts() -> None:
    nft = FakeNft()
    own = _network().bridge_name("freeinference-chat-net-x")
    nft.routes += [
        {"dst": "0.0.0.0/1", "dev": "tun0", "table": "main"},
        {"dst": "128.0.0.0/1", "dev": "tun0", "table": "main"},
        {"dst": "default", "dev": "wg0", "table": "51820"},
        {"dst": "10.240.0.0/29", "dev": own, "table": "main"},
        {"type": "local", "dst": "10.240.0.1", "dev": own, "table": "local"},
        {"type": "broadcast", "dst": "10.240.0.7", "dev": own, "table": "local"},
    ]
    _guard(nft).ensure()


def test_the_host_routes_are_read_without_any_capability() -> None:
    nft = FakeNft()
    _guard(nft).ensure()
    reads = [argv for argv, _ in nft.calls if "ip" in argv and "--entrypoint" in argv]
    reads = [argv for argv in reads if argv[argv.index("--entrypoint") + 1] == "ip"]
    assert reads
    for argv in reads:
        assert "--cap-add" not in argv
        assert argv[argv.index("--network") + 1] == "host"
        assert argv[argv.index("--cap-drop") + 1] == "ALL"


def test_neighbours_with_separate_pools_both_run() -> None:
    nft = FakeNft()
    _guard(nft).ensure()
    _guard(
        nft,
        _network(AGENT_SANDBOX_NAMESPACE="stack-b", AGENT_EGRESS_CHAT_NETWORK_POOL="10.241.0.0/16"),
    ).ensure()


def test_relay_exceptions_follow_the_bridge_exactly() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    bridge = guard.network.bridge_name("freeinference-chat-net-x")
    other = guard.network.bridge_name("freeinference-chat-net-y")
    guard.sync_relays(bridge, {("10.240.0.3", 8080), ("10.240.0.4", 8000)})
    guard.sync_relays(other, {("10.240.0.11", 8080)})
    guard.sync_relays(bridge, {("10.240.0.5", 8080), ("10.240.0.4", 8000)})
    assert guard.relay_endpoints(bridge) == {("10.240.0.5", 8080), ("10.240.0.4", 8000)}
    assert guard.relay_endpoints(other) == {("10.240.0.11", 8080)}, "another bridge is untouched"
    calls = len(nft.calls)
    guard.sync_relays(bridge, {("10.240.0.5", 8080), ("10.240.0.4", 8000)})
    assert not any(stdin for _, stdin in nft.calls[calls:]), "no transaction when nothing changed"
    guard.ensure(max_age_s=0)
    assert guard.relay_endpoints(bridge), "a reinstall keeps live exceptions"
    guard.sync_relays(bridge, set())
    assert guard.relay_endpoints(bridge) == set()


def test_a_relay_missing_from_one_table_is_restored_in_that_table() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    bridge = guard.network.bridge_name("freeinference-chat-net-x")
    guard.sync_relays(bridge, {("10.240.0.3", 8080)})
    nft.table("bridge", guard.network.table)["sets"]["relays"].clear()
    assert guard.relay_endpoints(bridge) == set()
    guard.sync_relays(bridge, {("10.240.0.3", 8080)})
    assert guard.relay_endpoints(bridge) == {("10.240.0.3", 8080)}


def test_another_deployments_bridge_is_refused() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    foreign = _network(AGENT_SANDBOX_NAMESPACE="stack-b").bridge_name("freeinference-chat-net-x")
    with pytest.raises(NetworkGuardError, match="not one of this deployment's bridges"):
        guard.sync_relays(foreign, set())


def test_the_helper_image_is_this_containers_own(monkeypatch) -> None:
    nft = FakeNft()
    monkeypatch.setenv("HOSTNAME", "abc123")
    guard = HostNetworkGuard(_network(), run=nft)
    guard.ensure()
    inspect = nft.calls[0][0]
    assert inspect[1:3] == ["container", "inspect"] and inspect[-1] == "abc123"
    assert _IMAGE in nft.calls[1][0]


def test_an_image_that_is_not_an_immutable_id_is_refused(monkeypatch) -> None:
    def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "some-tag:latest\n", "")

    monkeypatch.setenv("HOSTNAME", "abc123")
    with pytest.raises(NetworkGuardError, match="own image"):
        HostNetworkGuard(_network(), run=run).ensure()


def test_a_failing_helper_reports_nft_stderr_and_trusts_nothing() -> None:
    def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "Error: Could not process rule")

    guard = HostNetworkGuard(_network(), helper_image=_IMAGE, run=run)
    with pytest.raises(NetworkGuardError, match="Could not process rule"):
        guard.ensure()
    with pytest.raises(NetworkGuardError):
        guard.ensure()  # no cached success to fall back on
