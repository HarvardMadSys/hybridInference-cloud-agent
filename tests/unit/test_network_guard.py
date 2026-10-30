"""The host network guard, against a model of nft rather than a kernel.

The model applies exactly the transactions the guard sends and answers its
JSON listings the way nft prints them, so installation, verification, relay
bookkeeping and the neighbour check are exercised end to end in-process. The
external tier runs the same guard against a real kernel.
"""

from __future__ import annotations

import io
import json
import logging
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

import pytest

from cloud_agent_host.network_guard import GUARD_RECHECK_S, HostNetworkGuard, NetworkGuardError
from cloud_agent_shared.egress import SandboxNetwork, network_from_env

if TYPE_CHECKING:
    from collections.abc import Callable

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
        #: Every ``nft -f -`` transaction applied, in order.
        self.applied: list[str] = []
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
        assert entrypoint == "python3" and args[0] == "-c", argv
        return self.run_script(argv, args[1], stdin)

    def nft(self, argv: list[str], *, input: str | None = None, **_kwargs: Any) -> Any:
        """One ``nft`` invocation inside the helper, against this model."""
        assert argv[0] == "nft", argv
        try:
            if argv[1:3] == ["-f", "-"]:
                self.apply(input or "")
                self.applied.append(input or "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            assert argv[1] == "-j", argv
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.listing(argv[2:])), "")
        except KeyError as exc:
            return subprocess.CompletedProcess(argv, 1, "", f"No such file or directory: {exc}")

    def run_script(
        self, argv: list[str], script: str, stdin: str | None
    ) -> subprocess.CompletedProcess[str]:
        """Run the helper's own script, its ``nft`` calls answered by this model."""
        output, error = io.StringIO(), io.StringIO()
        saved = (subprocess.run, sys.stdin, sys.stdout, sys.stderr)
        subprocess.run = self.nft  # type: ignore[assignment]
        sys.stdin, sys.stdout, sys.stderr = io.StringIO(stdin or ""), output, error
        try:
            exec(script, {"__name__": "__main__"})
            code = 0
        except SystemExit as exit_:
            code = int(exit_.code or 0)
        finally:
            subprocess.run, sys.stdin, sys.stdout, sys.stderr = saved  # type: ignore[assignment]
        return subprocess.CompletedProcess(argv, code, output.getvalue(), error.getvalue())

    def transactions(self) -> list[str]:
        return list(self.applied)

    def requests(self) -> list[dict[str, Any]]:
        """What each privileged helper was asked to do, in order."""
        return [
            json.loads(stdin or "{}")
            for argv, stdin in self.calls
            if "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "python3"
        ]

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
        for set_name, elements in body["sets"].items():
            items.append({"set": {"name": set_name, "elem": list(elements)}})
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
        ("--entrypoint", "python3"),
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
    installs = len(nft.transactions())
    guard.ensure()
    assert len(nft.transactions()) == installs, "a fresh check is reused"
    guard.ensure(max_age_s=0)
    assert len(nft.transactions()) == installs + 1


def test_activation_after_an_idle_gap_rechecks_routes_and_refuses_overlap(monkeypatch) -> None:
    """Skipping empty-host sweeps never carries an expired proof into a start."""
    from cloud_agent_host import network_guard

    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    before = len(nft.calls)
    now = network_guard.time.monotonic()
    monkeypatch.setattr(network_guard.time, "monotonic", lambda: now + 3600)
    nft.routes.append({"dst": "10.240.0.0/16", "dev": "vpn0"})

    with pytest.raises(NetworkGuardError, match="overlaps the host route"):
        guard.ensure()

    assert len(nft.calls) == before + 2
    assert guard._verified_at is None


def test_ensure_installs_and_reads_back_in_one_helper_and_keeps_routes_unprivileged() -> None:
    """A helper container costs far more than its nft calls, so a check is one helper.

    The install used to take a helper of its own before the read-back's.
    """
    from cloud_agent_shared.egress import render_ruleset

    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    assert [argv[argv.index("--entrypoint") + 1] for argv, _ in nft.calls] == ["python3", "ip"]
    command, stdin = nft.calls[0]
    assert json.loads(stdin or "{}") == {
        "install": render_ruleset(guard.network),
        "reads": [
            ["list", "table", "inet", guard.network.table],
            ["list", "table", "bridge", guard.network.table],
            ["list", "set", "inet", guard.network.table, "protected4"],
            ["list", "set", "inet", guard.network.table, "pool4"],
            ["list", "tables", "inet"],
        ],
    }
    assert nft.transactions() == [render_ruleset(guard.network)], "installed before the reads"
    assert command[command.index("--cap-add") + 1] == "NET_ADMIN"
    assert "--cap-add" not in nft.calls[1][0]


def test_a_relay_update_reads_compares_and_writes_in_one_helper() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    bridge = guard.network.bridge_name("freeinference-chat-net-x")
    calls, transactions = len(nft.calls), len(nft.transactions())
    guard.sync_relays(bridge, {("10.240.0.3", 8080)})
    assert len(nft.calls) == calls + 1, "the write rides the read's helper"
    assert len(nft.transactions()) == transactions + 1
    argv, stdin = nft.calls[-1]
    assert argv[argv.index("--entrypoint") + 1] == "python3"
    assert argv[argv.index("--cap-add") + 1] == "NET_ADMIN"
    assert json.loads(stdin or "{}") == {
        "reads": [],
        "relays": {
            "table": guard.network.table,
            "families": ["inet", "bridge"],
            "bridge": bridge,
            "wanted": [["10.240.0.3", 8080]],
        },
    }
    # Without a network to remember it by, the kernel is read again, and an
    # unchanged set costs no transaction.
    guard.sync_relays(bridge, {("10.240.0.3", 8080)})
    assert len(nft.calls) == calls + 2
    assert len(nft.transactions()) == transactions + 1


def test_the_helper_renders_a_relay_update_exactly_as_the_shared_renderer_does() -> None:
    """The helper computes the update from what each table holds, then renders it."""
    from cloud_agent_shared.egress import render_relay_update

    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    bridge = guard.network.bridge_name("freeinference-chat-net-x")
    guard.sync_relays(bridge, {("10.240.0.3", 8080), ("10.240.0.4", 8000)})
    # The bridge table has lost one exception the inet table still holds.
    nft.table("bridge", guard.network.table)["sets"]["relays"].remove(
        {"concat": [bridge, "10.240.0.4", 8000]}
    )

    guard.sync_relays(bridge, {("10.240.0.5", 8080), ("10.240.0.4", 8000)})

    assert nft.transactions()[-1] == render_relay_update(
        guard.network,
        remove={
            "inet": [(bridge, "10.240.0.3", 8080)],
            "bridge": [(bridge, "10.240.0.3", 8080)],
        },
        add=[(bridge, "10.240.0.4", 8000), (bridge, "10.240.0.5", 8080)],
    )
    assert guard.relay_endpoints(bridge) == {("10.240.0.5", 8080), ("10.240.0.4", 8000)}


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
    assert nft.table("inet", guard.network.table)["rules"]["from_group"] == 9


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
    transactions = len(nft.transactions())
    guard.sync_relays(bridge, {("10.240.0.5", 8080), ("10.240.0.4", 8000)})
    assert len(nft.transactions()) == transactions, "no transaction when nothing changed"
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


def test_failed_batch_readback_invalidates_an_earlier_success() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    failures = 0

    def run(argv, stdin):
        nonlocal failures
        if argv[argv.index("--entrypoint") + 1] == "python3":
            failures += 1
            return subprocess.CompletedProcess(argv, 1, "", "table disappeared")
        return nft(argv, stdin)

    guard._run = run
    with pytest.raises(NetworkGuardError, match="table disappeared"):
        guard.ensure(max_age_s=0)
    with pytest.raises(NetworkGuardError, match="table disappeared"):
        guard.ensure()
    assert failures == 2


def test_a_missing_relay_set_in_either_family_prevents_updates() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    del nft.table("bridge", guard.network.table)["sets"]["relays"]
    bridge = guard.network.bridge_name("freeinference-chat-net-x")
    transactions = len(nft.transactions())
    with pytest.raises(NetworkGuardError, match="No such file"):
        guard.sync_relays(bridge, {("10.240.0.3", 8080)})
    assert len(nft.transactions()) == transactions
    assert nft.table("inet", guard.network.table)["sets"]["relays"] == []


def test_batch_helper_installs_then_reads_without_a_shell(monkeypatch) -> None:
    from cloud_agent_host.network_guard import _NFT_BATCH

    commands = [["list", "table", "inet", "example"], ["list", "tables", "inet"]]
    calls: list[tuple[list[str], str | None]] = []
    listings = [{"nftables": [{"table": {"name": "example"}}]}, {"nftables": []}]

    def run(argv, *, input=None, capture_output, text):
        assert capture_output and text
        calls.append((argv, input))
        if argv[1:3] == ["-f", "-"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(listings[len(calls) - 2]), "")

    output = io.StringIO()
    monkeypatch.setattr(subprocess, "run", run)
    request = {"install": "add table inet example\n", "reads": commands}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, "stdout", output)
    exec(_NFT_BATCH, {})
    assert calls == [
        (["nft", "-f", "-"], "add table inet example\n"),
        *((["nft", "-j", *command], None) for command in commands),
    ]
    assert json.loads(output.getvalue()) == {"listings": listings}


def test_batch_helper_fails_without_partial_output_when_a_read_fails(monkeypatch) -> None:
    from cloud_agent_host.network_guard import _NFT_BATCH

    calls: list[list[str]] = []

    def run(argv, *, input=None, capture_output, text):
        calls.append(argv)
        if len(calls) == 2:
            return subprocess.CompletedProcess(argv, 1, "", "table disappeared")
        return subprocess.CompletedProcess(argv, 0, '{"nftables": []}', "")

    output, error = io.StringIO(), io.StringIO()
    monkeypatch.setattr(subprocess, "run", run)
    request = {"reads": [["list", "tables", "inet"]] * 3}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", error)
    with pytest.raises(SystemExit) as failure:
        exec(_NFT_BATCH, {})
    assert failure.value.code == 1
    assert len(calls) == 2
    assert output.getvalue() == ""
    assert error.getvalue() == "table disappeared"


def test_a_failed_install_ends_the_helper_before_any_read(monkeypatch) -> None:
    from cloud_agent_host.network_guard import _NFT_BATCH

    calls: list[list[str]] = []

    def run(argv, *, input=None, capture_output, text):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "Error: Could not process rule")

    output, error = io.StringIO(), io.StringIO()
    monkeypatch.setattr(subprocess, "run", run)
    request = {"install": "add table inet example\n", "reads": [["list", "tables", "inet"]]}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", error)
    with pytest.raises(SystemExit):
        exec(_NFT_BATCH, {})
    assert calls == [["nft", "-f", "-"]]
    assert output.getvalue() == ""
    assert error.getvalue() == "Error: Could not process rule"


@pytest.mark.parametrize("output", ["not json", "{}", "[]", '[{"nftables": []}]', "[{}, {}]"])
def test_missing_or_malformed_batch_results_are_refused(output) -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()

    def run(argv, stdin):
        return subprocess.CompletedProcess(argv, 0, output, "")

    guard._run = run
    with pytest.raises(NetworkGuardError, match="nft returned"):
        guard.relay_endpoints(guard.network.bridge_name("freeinference-chat-net-x"))


def test_the_sweep_sees_a_flushed_ruleset_on_its_next_pass_and_reinstalls_it() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    installs = len(nft.transactions())
    nft.tables.clear()  # `flush ruleset`, from a firewall reload
    guard.ensure()
    assert len(nft.transactions()) == installs, "an activation still trusts the last minute"

    guard.recheck()

    assert {("inet", guard.network.table), ("bridge", guard.network.table)} <= set(nft.tables)
    assert len(nft.transactions()) == installs + 1


def test_an_intact_ruleset_costs_the_sweep_one_read_and_no_install() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    calls = len(nft.calls)

    guard.recheck()

    assert [argv[argv.index("--entrypoint") + 1] for argv, _ in nft.calls[calls:]] == ["python3"]
    assert json.loads(nft.calls[-1][1] or "{}") == {
        "reads": [
            ["list", "table", "inet", guard.network.table],
            ["list", "table", "bridge", guard.network.table],
            ["list", "set", "inet", guard.network.table, "protected4"],
            ["list", "set", "inet", guard.network.table, "pool4"],
        ]
    }


def test_a_sweep_that_cannot_reinstall_the_rules_raises_and_trusts_nothing() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    nft.table("inet", guard.network.table)["rules"]["from_group"] = 0

    def run(argv, stdin):
        if "install" in json.loads(stdin or "{}"):
            return subprocess.CompletedProcess(argv, 1, "", "Error: Could not process rule")
        return nft(argv, stdin)

    guard._run = run
    with pytest.raises(NetworkGuardError, match="Could not process rule"):
        guard.recheck()
    # The activation path no longer trusts the earlier success either.
    with pytest.raises(NetworkGuardError, match="Could not process rule"):
        guard.ensure()


def test_a_verification_past_its_minute_takes_the_full_check(monkeypatch) -> None:
    from cloud_agent_host import network_guard

    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    installs = len(nft.transactions())
    now = network_guard.time.monotonic()
    monkeypatch.setattr(
        network_guard.time, "monotonic", lambda: now + network_guard.GUARD_RECHECK_S
    )

    guard.recheck()

    assert len(nft.transactions()) == installs + 1
    assert nft.calls[-1][0][nft.calls[-1][0].index("--entrypoint") + 1] == "ip"


def test_the_managers_host_rule_check_reads_the_kernel_every_time() -> None:
    from types import SimpleNamespace

    from cloud_agent_host.chat_sandbox import ChatSandboxManager

    nft = FakeNft()
    guard = _guard(nft)
    manager = ChatSandboxManager(SimpleNamespace(network_guard=lambda: guard))  # type: ignore[arg-type]
    manager.network.verify_host_rules()
    nft.tables.clear()

    manager.network.verify_host_rules()

    assert ("inet", guard.network.table) in nft.tables


# ── a turn's child, on its host service's verification ──────────────────


def _host_guard(monkeypatch) -> tuple[FakeNft, HostNetworkGuard]:
    """The host service's guard after its startup check, its image found as deployed."""
    nft = FakeNft()
    monkeypatch.setenv("HOSTNAME", "runner-1")
    guard = HostNetworkGuard(_network(), run=nft)
    guard.ensure(max_age_s=0)
    return nft, guard


def _child_guard(host: FakeNft) -> tuple[FakeNft, HostNetworkGuard]:
    """A turn's child's guard: helper runs of its own, on the host's kernel."""
    nft = FakeNft()
    nft.tables = host.tables
    return nft, HostNetworkGuard(_network(), run=nft)


def _steps(calls: list[tuple[list[str], str | None]]) -> list[str]:
    """Each Docker process a guard ran: the inspect for its image, or a helper's entrypoint."""
    return [
        argv[argv.index("--entrypoint") + 1] if "--entrypoint" in argv else "inspect"
        for argv, _stdin in calls
    ]


def test_a_handover_reuses_a_recent_verification_and_renews_an_older_one(monkeypatch) -> None:
    """Never one per turn, and never one so old that the child's start would outlast it."""
    from cloud_agent_host import network_guard

    nft, guard = _host_guard(monkeypatch)
    handed = guard.handover()
    made = handed["verified_at"]
    monkeypatch.setattr(
        network_guard.time, "monotonic", lambda: made + network_guard.GUARD_RENEW_S - 0.001
    )

    assert guard.handover() == handed == {"verified_at": made, "helper_image": _IMAGE}
    assert _steps(nft.calls) == ["inspect", "python3", "ip"], "only the startup check ran"

    later = made + network_guard.GUARD_RENEW_S + 0.001
    monkeypatch.setattr(network_guard.time, "monotonic", lambda: later)

    assert guard.handover() == {"verified_at": later, "helper_image": _IMAGE}
    assert _steps(nft.calls) == ["inspect", "python3", "ip", "python3", "ip"]


@pytest.mark.parametrize("failure", ["nft", "unexpected"])
def test_a_check_that_fails_at_a_handover_is_handed_over_as_its_reason(failure) -> None:
    """For the child to decline with: a raise would strand a turn that never started."""
    runs: list[list[str]] = []

    def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        runs.append(argv)
        if failure == "unexpected":
            raise RuntimeError("the Docker client crashed")
        return subprocess.CompletedProcess(argv, 1, "", "Error: Could not process rule")

    guard = HostNetworkGuard(_network(), helper_image=_IMAGE, run=run)

    handed = guard.handover()

    assert list(handed) == ["error"]
    assert ("Could not process rule" if failure == "nft" else "client crashed") in handed["error"]
    guard.handover()
    assert len(runs) == 2, "nothing was trusted, so the next handover checks again"


def test_a_child_adopts_its_hosts_verification_and_starts_no_helper_for_it(monkeypatch) -> None:
    """Nor the inspect that finds the helpers' image: the host hands that over as well."""
    host_nft, host = _host_guard(monkeypatch)
    handed = json.loads(json.dumps(host.handover()))  # as the turn payload carries it
    nft, child = _child_guard(host_nft)

    assert child.adopt(handed) is True

    assert child.ensure() is None, "trusted as the host's own guard trusts it"
    assert nft.calls == []
    bridge = child.network.bridge_name("freeinference-chat-net-x")
    child.sync_relays(bridge, {("10.240.0.3", 8080)})
    assert _steps(nft.calls) == ["python3"]
    assert _IMAGE in nft.calls[0][0]


def test_an_adopted_verification_lapses_when_the_hosts_own_does(monkeypatch) -> None:
    """A minute after the host made it, as for every activation; not after the child took it."""
    from cloud_agent_host import network_guard

    host_nft, host = _host_guard(monkeypatch)
    handed = host.handover()
    made = handed["verified_at"]
    nft, child = _child_guard(host_nft)
    monkeypatch.setattr(
        network_guard.time, "monotonic", lambda: made + network_guard.GUARD_RECHECK_S - 1
    )
    child.adopt(handed)
    assert child.ensure() is None
    assert nft.calls == []
    monkeypatch.setattr(
        network_guard.time, "monotonic", lambda: made + network_guard.GUARD_RECHECK_S + 0.001
    )

    child.ensure()

    assert _steps(nft.calls) == ["python3", "ip"], "the full check, on the image it was handed"


_NOW = 10_000.0


@pytest.mark.parametrize(
    ("verification", "reason"),
    [
        pytest.param(None, "without verifying", id="missing"),
        pytest.param("2026-10-21T05:00:00Z", "without verifying", id="not-a-mapping"),
        pytest.param({}, "unreadable", id="empty"),
        pytest.param(
            {"error": "nft failed: Error: Could not process rule"},
            "could not verify the sandbox network rules: nft failed",
            id="failed",
        ),
        pytest.param(
            {"error": "nft failed", "verified_at": _NOW - 1, "helper_image": _IMAGE},
            "could not verify",
            id="failed-with-a-time",
        ),
        pytest.param({"verified_at": "recently", "helper_image": _IMAGE}, "unreadable", id="text"),
        pytest.param({"verified_at": True, "helper_image": _IMAGE}, "unreadable", id="bool"),
        pytest.param({"verified_at": 10**400, "helper_image": _IMAGE}, "unreadable", id="huge"),
        pytest.param({"verified_at": float("nan"), "helper_image": _IMAGE}, "unreadable", id="nan"),
        pytest.param({"verified_at": _NOW + 1, "helper_image": _IMAGE}, "unreadable", id="future"),
        pytest.param({"verified_at": _NOW - 1}, "unreadable", id="no-image"),
        pytest.param({"verified_at": _NOW - 1, "helper_image": "runner:1"}, "unreadable", id="tag"),
        pytest.param(
            {"verified_at": 1.0, "helper_image": "runner:1"}, "unreadable", id="tag-past-the-window"
        ),
    ],
)
def test_a_child_refuses_a_verification_it_cannot_trust_and_holds_nothing(
    monkeypatch, verification, reason
) -> None:
    from cloud_agent_host import network_guard

    monkeypatch.setattr(network_guard.time, "monotonic", lambda: _NOW)
    monkeypatch.setenv("HOSTNAME", "runner-1")
    nft = FakeNft()
    child = HostNetworkGuard(_network(), run=nft)

    with pytest.raises(NetworkGuardError, match=reason):
        child.adopt(verification)

    assert nft.calls == []
    child.ensure()
    assert _steps(nft.calls) == ["inspect", "python3", "ip"], "nothing adopted, nothing trusted"


@pytest.mark.parametrize(
    "age", [pytest.param(GUARD_RECHECK_S, id="at-the-window"), pytest.param(90.0, id="past-it")]
)
def test_a_child_slow_to_start_adopts_nothing_of_a_sound_verification_and_says_why(
    monkeypatch, caplog, age
) -> None:
    """The host's check aged out on the way, nothing more: no reason to decline the turn.

    Neither its time nor its helper image is adopted, so the child's own check
    is the full one every child made before the host handed its check over.
    """
    from cloud_agent_host import network_guard

    monkeypatch.setattr(network_guard.time, "monotonic", lambda: _NOW)
    monkeypatch.setenv("HOSTNAME", "runner-1")
    nft = FakeNft()
    child = HostNetworkGuard(_network(), run=nft)
    handed_image = "sha256:" + "b" * 64

    with caplog.at_level(logging.WARNING):
        adopted = child.adopt({"verified_at": _NOW - age, "helper_image": handed_image})

    assert adopted is False
    assert nft.calls == []
    (warned,) = [
        record
        for record in caplog.records
        if record.getMessage() == "network_guard_verification_aged_out"
    ]
    assert warned.age_s == age
    assert f"rules {age:.0f}s ago" in warned.detail
    child.ensure()
    assert _steps(nft.calls) == ["inspect", "python3", "ip"], "nothing adopted, nothing trusted"
    assert not [argv for argv, _stdin in nft.calls if handed_image in argv]


# ── remembered relay exceptions ─────────────────────────────────────────

_NET = "net-1111"


def _synced_guard() -> tuple[FakeNft, HostNetworkGuard, str]:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    bridge = guard.network.bridge_name("freeinference-chat-net-x")
    guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)
    return nft, guard, bridge


def test_an_unchanged_update_for_the_same_network_costs_no_helper() -> None:
    """Every turn and workspace command re-syncs its bridge; unchanged, that is free."""
    nft, guard, bridge = _synced_guard()
    calls = len(nft.calls)

    guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)

    assert len(nft.calls) == calls


@pytest.mark.parametrize("change", ["network", "endpoints", "no_network", "invalidated"])
def test_anything_but_the_same_update_for_the_same_network_reads_the_kernel(change) -> None:
    nft, guard, bridge = _synced_guard()
    endpoints, network_id = {("10.240.0.3", 8080)}, _NET
    if change == "network":
        network_id = "net-2222"  # the bridge was removed and created again
    elif change == "endpoints":
        endpoints = {("10.240.0.9", 8080)}
    elif change == "no_network":
        network_id = None
    else:
        guard.invalidate()
    calls = len(nft.calls)

    guard.sync_relays(bridge, endpoints, network_id=network_id)

    assert len(nft.calls) == calls + 1
    assert guard.relay_endpoints(bridge) == endpoints


def test_an_update_for_another_network_on_the_same_bridge_name_is_written() -> None:
    """A removed bridge's exceptions dropped elsewhere are restored for its successor."""
    nft, guard, bridge = _synced_guard()
    nft.table("inet", guard.network.table)["sets"]["relays"].clear()
    nft.table("bridge", guard.network.table)["sets"]["relays"].clear()

    guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id="net-2222")

    assert guard.relay_endpoints(bridge) == {("10.240.0.3", 8080)}


def test_the_sweep_forgets_an_exception_the_kernel_lost_and_the_next_update_restores_it():
    nft, guard, bridge = _synced_guard()
    nft.table("bridge", guard.network.table)["sets"]["relays"].clear()

    guard.recheck()  # the sweep's read-back: chains intact, so no reinstall
    calls = len(nft.calls)
    guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)

    assert len(nft.calls) == calls + 1
    assert guard.relay_endpoints(bridge) == {("10.240.0.3", 8080)}


def test_the_sweep_forgets_an_exception_someone_else_added_for_the_bridge() -> None:
    nft, guard, bridge = _synced_guard()
    for family in ("inet", "bridge"):
        nft.table(family, guard.network.table)["sets"]["relays"].append(
            {"concat": [bridge, "10.240.0.66", 22]}
        )

    guard.recheck()
    guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)

    assert guard.relay_endpoints(bridge) == {("10.240.0.3", 8080)}


def test_a_reinstall_after_a_flushed_ruleset_forgets_what_it_no_longer_holds() -> None:
    nft, guard, bridge = _synced_guard()
    nft.tables.clear()  # `flush ruleset`, from a firewall reload

    guard.recheck()  # the read-back fails, so the sweep reinstalls: empty relay sets
    calls = len(nft.calls)
    guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)

    assert len(nft.calls) == calls + 1
    assert guard.relay_endpoints(bridge) == {("10.240.0.3", 8080)}


def test_a_reinstall_that_finds_the_exceptions_still_held_keeps_them_remembered() -> None:
    nft, guard, bridge = _synced_guard()

    guard.ensure(max_age_s=0)
    calls = len(nft.calls)
    guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)

    assert len(nft.calls) == calls


def test_a_failed_check_forgets_every_remembered_update() -> None:
    nft, guard, bridge = _synced_guard()
    nft.table("inet", guard.network.table)["rules"]["from_group"] = 0

    def run(argv, stdin):
        if "install" in json.loads(stdin or "{}"):
            return subprocess.CompletedProcess(argv, 1, "", "Error: Could not process rule")
        return nft(argv, stdin)

    guard._run = run
    with pytest.raises(NetworkGuardError):
        guard.recheck()
    guard._run = nft
    calls = len(nft.calls)

    guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)

    assert len(nft.calls) == calls + 1


def test_a_failed_update_is_never_remembered() -> None:
    nft, guard, bridge = _synced_guard()
    del nft.table("bridge", guard.network.table)["sets"]["relays"]
    with pytest.raises(NetworkGuardError, match="No such file"):
        guard.sync_relays(bridge, {("10.240.0.9", 8080)}, network_id=_NET)
    nft.table("bridge", guard.network.table)["sets"]["relays"] = []
    calls = len(nft.calls)

    guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)

    assert len(nft.calls) == calls + 1, "the earlier success was forgotten when the update began"


def test_an_unreadable_relay_element_in_a_read_back_forgets_and_fails_nothing() -> None:
    nft, guard, bridge = _synced_guard()
    nft.table("inet", guard.network.table)["sets"]["relays"].append({"prefix": "garbage"})

    guard.recheck()  # the rules themselves are intact: no error, no reinstall

    with pytest.raises(NetworkGuardError, match="unreadable relay set element"):
        guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)


def test_a_remembered_update_is_trusted_no_longer_than_the_rules_are(monkeypatch) -> None:
    from cloud_agent_host import network_guard

    nft, guard, bridge = _synced_guard()
    start = network_guard.time.monotonic()
    monkeypatch.setattr(
        network_guard.time, "monotonic", lambda: start + network_guard.GUARD_RECHECK_S
    )
    calls = len(nft.calls)

    guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)

    assert len(nft.calls) == calls + 1


def test_the_sweeps_renewal_keeps_a_held_update_trusted(monkeypatch) -> None:
    from cloud_agent_host import network_guard

    nft, guard, bridge = _synced_guard()
    start = network_guard.time.monotonic()
    monkeypatch.setattr(
        network_guard.time, "monotonic", lambda: start + network_guard.GUARD_RENEW_S
    )
    installs = len(nft.transactions())
    guard.recheck()  # old enough to renew: reinstall, and the read-back confirms
    assert len(nft.transactions()) == installs + 1
    monkeypatch.setattr(
        network_guard.time,
        "monotonic",
        lambda: start + network_guard.GUARD_RENEW_S + network_guard.HELD_RELAYS_TRUST_S - 1,
    )
    calls = len(nft.calls)

    guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)

    assert len(nft.calls) == calls


def test_a_flushed_table_refuses_an_activation_once_the_sweep_falls_behind(monkeypatch) -> None:
    """A relay update was an activation's one read of the kernel; memory may not hide a flush.

    The verification is still trusted, but no read-back has confirmed the
    remembered update for two sweep passes (a final-archive upload holds the
    sweep back), so the update reads the kernel and finds the table gone.
    """
    from cloud_agent_host import network_guard

    nft, guard, bridge = _synced_guard()
    start = network_guard.time.monotonic()
    nft.tables.clear()  # `flush ruleset`, from a firewall reload
    monkeypatch.setattr(
        network_guard.time, "monotonic", lambda: start + network_guard.HELD_RELAYS_TRUST_S
    )

    with pytest.raises(NetworkGuardError):
        guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)


def test_ssh_targets_are_exact_per_bridge_and_routed_only() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    bridge = guard.network.bridge_name("freeinference-chat-net-x")
    other = guard.network.bridge_name("freeinference-chat-net-y")
    calls = len(nft.calls)

    guard.sync_ssh_targets(bridge, {("93.184.216.34", 22), ("93.184.216.35", 2222)})
    guard.sync_ssh_targets(other, {("93.184.216.36", 22)})

    assert len(nft.calls) == calls + 2, "one helper reads, compares and writes"
    assert nft.requests()[-2] == {
        "reads": [],
        "ssh_targets": {
            "table": guard.network.table,
            "families": ["inet"],
            "bridge": bridge,
            "wanted": [["93.184.216.34", 22], ["93.184.216.35", 2222]],
        },
    }
    assert "ssh_targets" not in nft.table("bridge", guard.network.table)["sets"]
    guard.sync_ssh_targets(bridge, {("93.184.216.35", 2222)})
    assert guard.ssh_target_endpoints(bridge) == {("93.184.216.35", 2222)}
    assert guard.ssh_target_endpoints(other) == {("93.184.216.36", 22)}


def test_ssh_targets_survive_a_reinstall_of_the_rules() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    bridge = guard.network.bridge_name("freeinference-chat-net-x")
    guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)})
    nft.table("inet", guard.network.table)["rules"]["from_group"] = 0

    guard.ensure(max_age_s=0)

    assert guard.ssh_target_endpoints(bridge) == {("93.184.216.34", 22)}


def test_releasing_a_bridge_drops_its_relays_and_ssh_targets_in_one_helper() -> None:
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    bridge = guard.network.bridge_name("freeinference-chat-net-x")
    other = guard.network.bridge_name("freeinference-chat-net-y")
    guard.sync_relays(bridge, {("10.240.0.3", 8080)})
    guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)})
    guard.sync_ssh_targets(other, {("93.184.216.36", 22)})
    calls = len(nft.calls)

    guard.release_bridge(bridge)

    assert len(nft.calls) == calls + 1
    assert guard.relay_endpoints(bridge) == set()
    assert guard.ssh_target_endpoints(bridge) == set()
    assert guard.ssh_target_endpoints(other) == {("93.184.216.36", 22)}


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (("2001:db8::1", 22), "IPv4"),
        (("ssh.example.com", 22), "IPv4"),
        (("93.184.216.34", 0), "port"),
        (("93.184.216.34", 65536), "port"),
    ],
)
def test_an_ssh_target_must_be_an_ipv4_address_and_port(target, message) -> None:
    nft = FakeNft()
    guard = _guard(nft)
    bridge = guard.network.bridge_name("freeinference-chat-net-x")
    with pytest.raises(NetworkGuardError, match=message):
        guard.sync_ssh_targets(bridge, {target})
    with pytest.raises(NetworkGuardError, match="not one of this deployment"):
        guard.sync_ssh_targets("eth0", set())


# ── what a reinstalled table lost ───────────────────────────────────────


def test_every_read_back_reports_what_each_bridge_holds() -> None:
    """A flushed table is reinstalled empty, and the sweep is told so."""
    nft, guard, bridge = _synced_guard()
    guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)}, network_id=_NET)

    held = guard.recheck()

    assert held is not None
    assert held.relays == {bridge: frozenset({("10.240.0.3", 8080)})}
    assert held.ssh_targets == {bridge: frozenset({("93.184.216.34", 22)})}

    nft.tables.clear()  # `flush ruleset`, from a firewall reload
    reinstalled = guard.recheck()

    assert reinstalled is not None
    assert reinstalled.relays == {} and reinstalled.ssh_targets == {}


def test_a_relay_held_in_only_one_family_is_reported_lost() -> None:
    nft, guard, bridge = _synced_guard()
    nft.table("bridge", guard.network.table)["sets"]["relays"].clear()

    held = guard.recheck()

    assert held is not None and bridge not in held.relays


def test_an_unreadable_element_reports_nothing_rather_than_failing_the_check() -> None:
    nft, guard, _bridge = _synced_guard()
    nft.table("inet", guard.network.table)["sets"]["relays"].append({"prefix": "garbage"})

    assert guard.recheck() is None


def test_ssh_targets_come_back_only_to_the_network_they_were_written_for() -> None:
    nft, guard, bridge = _synced_guard()
    guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)}, network_id=_NET)
    nft.tables.clear()
    guard.recheck()

    guard.restore_ssh_targets(bridge, network_id="net-2222")  # a successor bridge

    assert guard.ssh_target_endpoints(bridge) == set()
    assert guard.written_ssh_targets(bridge) is None, "a stale memory is forgotten"

    guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)}, network_id=_NET)
    nft.tables.clear()
    guard.recheck()
    guard.restore_ssh_targets(bridge, network_id=_NET)

    assert guard.ssh_target_endpoints(bridge) == {("93.184.216.34", 22)}


def test_a_restore_writes_the_latest_selection_not_an_earlier_one() -> None:
    """An owner's change synced after the loss is what comes back, never what it replaced."""
    nft, guard, bridge = _synced_guard()
    guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)}, network_id=_NET)
    nft.tables.clear()
    guard.recheck()
    guard.sync_ssh_targets(bridge, set(), network_id=_NET)  # the owner turned it off

    guard.restore_ssh_targets(bridge, network_id=_NET)

    assert guard.ssh_target_endpoints(bridge) == set()


@pytest.mark.parametrize("ending", ["released", "failed_write", "no_network"])
def test_nothing_is_restored_once_the_written_targets_are_no_longer_known(ending) -> None:
    nft, guard, bridge = _synced_guard()
    if ending == "released":
        guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)}, network_id=_NET)
        guard.release_bridge(bridge)
    elif ending == "failed_write":
        guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)}, network_id=_NET)
        del nft.table("inet", guard.network.table)["sets"]["ssh_targets"]
        with pytest.raises(NetworkGuardError):
            guard.sync_ssh_targets(bridge, {("93.184.216.35", 22)}, network_id=_NET)
        nft.table("inet", guard.network.table)["sets"]["ssh_targets"] = []
    else:
        guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)})

    guard.restore_ssh_targets(bridge, network_id=_NET)

    assert guard.written_ssh_targets(bridge) is None
    assert guard.ssh_target_endpoints(bridge) == (
        {("93.184.216.34", 22)} if ending == "no_network" else set()
    )


class _HeldHelper:
    """The guard's runner, holding a helper run open until told to finish it."""

    def __init__(self, nft: FakeNft) -> None:
        self.nft = nft
        self.hold = False
        self.entered = threading.Event()
        self.finish = threading.Event()

    def __call__(self, argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        if self.hold:
            self.hold = False
            self.entered.set()
            assert self.finish.wait(5)
        return self.nft(argv, stdin)


def _finishes_within(seconds: float, call: Any) -> bool:
    done = threading.Event()

    def run() -> None:
        call()
        done.set()

    threading.Thread(target=run, daemon=True).start()
    return done.wait(seconds)


def test_a_fresh_check_does_not_wait_behind_another_bridges_helper() -> None:
    """Every workspace request checks the rules; one stop's helper must not hold them all."""
    nft, guard, bridge = _synced_guard()
    other = guard.network.bridge_name("freeinference-chat-net-y")
    helper = _HeldHelper(nft)
    guard._run = helper
    helper.hold = True
    releasing = threading.Thread(target=guard.release_bridge, args=(other,), daemon=True)
    releasing.start()
    assert helper.entered.wait(5), "the other bridge's helper never started"
    calls = len(nft.calls)

    def fresh_checks() -> None:
        assert guard.ensure() is None
        guard.sync_relays(bridge, {("10.240.0.3", 8080)}, network_id=_NET)
        assert guard.written_relays(bridge) == frozenset({("10.240.0.3", 8080)})

    try:
        assert _finishes_within(1, fresh_checks), "a fresh check waited for the helper"
    finally:
        helper.finish.set()
        releasing.join(5)
    assert len(nft.calls) == calls + 1, "only the held helper ran"


def test_a_check_during_a_reinstall_waits_for_its_verification() -> None:
    """Nothing is trusted while the rules are being installed and read back."""
    nft = FakeNft()
    guard = _guard(nft)
    helper = _HeldHelper(nft)
    guard._run = helper
    helper.hold = True
    installing = threading.Thread(target=guard.ensure, daemon=True)
    installing.start()
    assert helper.entered.wait(5), "the install never started"
    try:
        assert not _finishes_within(0.2, guard.ensure), "a check trusted an unfinished install"
    finally:
        helper.finish.set()
        installing.join(5)
    calls = len(nft.calls)
    assert guard.ensure() is None
    assert len(nft.calls) == calls


def test_a_check_after_the_sweep_found_the_rules_changed_trusts_nothing() -> None:
    """The sweep reads without the lock; what it found must not wait on another bridge's helper."""
    nft, guard, _bridge = _synced_guard()
    other = guard.network.bridge_name("freeinference-chat-net-y")
    helper = _HeldHelper(nft)
    guard._run = helper
    helper.hold = True
    releasing = threading.Thread(target=guard.release_bridge, args=(other,), daemon=True)
    releasing.start()
    assert helper.entered.wait(5), "the other bridge's helper never started"
    nft.table("inet", guard.network.table)["rules"]["from_group"] = 0  # a flushed chain
    sweeping = threading.Thread(target=guard.recheck, daemon=True)
    sweeping.start()
    # The sweep reads the changed table, then waits for the lock to repair it.
    deadline = time.monotonic() + 2
    while guard._verified_at is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert not _finishes_within(0.2, guard.ensure), "a check trusted a changed table"
    finally:
        helper.finish.set()
        releasing.join(5)
        sweeping.join(5)
    assert nft.table("inet", guard.network.table)["rules"]["from_group"] == 9


def test_a_sweep_during_another_checks_reinstall_reads_what_it_left() -> None:
    """The sweep restores lost exceptions from its read-back, so it must get one."""
    nft, guard, _bridge = _synced_guard()
    helper = _HeldHelper(nft)
    guard._run = helper
    helper.hold = True
    installing = threading.Thread(target=guard.ensure, kwargs={"max_age_s": 0}, daemon=True)
    installing.start()
    assert helper.entered.wait(5), "the reinstall never started"
    swept: list[Any] = []
    sweeping = threading.Thread(target=lambda: swept.append(guard.recheck()), daemon=True)
    sweeping.start()
    time.sleep(0.2)  # the sweep finds the verification cleared by the reinstall
    helper.finish.set()
    installing.join(5)
    sweeping.join(5)
    assert swept, "the sweep never finished"
    assert swept[0] is not None, "the sweep read nothing to restore from"


# ── updates of different bridges, side by side ──────────────────────────

_X = _network().bridge_name("freeinference-chat-net-x")
_Y = _network().bridge_name("freeinference-chat-net-y")
#: Every kind of update of one bridge the guard makes.
_KINDS = ["relays", "ssh", "restore", "release"]
#: The helpers of an install, which runs alone.
_EXCLUSIVE = {"install", "routes"}
#: Helpers no exclusion is asserted about: the sweep's read-back runs beside
#: updates by design, and the image is looked up under a lock of its own.
_UNCHECKED = {"read", "inspect"}


def _helper_name(argv: list[str], stdin: str | None) -> str:
    """What one of the guard's Docker processes does: the bridge it updates, or its kind."""
    if "--entrypoint" not in argv:
        return "inspect"
    if argv[argv.index("--entrypoint") + 1] == "ip":
        return "routes"
    request = json.loads(stdin or "{}")
    if "install" in request:
        return "install"
    update = request.get("relays") or request.get("ssh_targets")
    return str(update["bridge"]) if update else "read"


class _Call(Future[Any]):
    """A guard call on a thread of its own: what it returns or raises, and the thread."""

    def __init__(self, call: Callable[[], Any]) -> None:
        super().__init__()
        self.thread = threading.Thread(target=self._run, args=(call,), daemon=True)

    def _run(self, call: Callable[[], Any]) -> None:
        try:
            self.set_result(call())
        except Exception as exc:
            self.set_exception(exc)


def _in_thread(call: Callable[[], Any]) -> _Call:
    """Start ``call`` on a thread of its own."""
    started = _Call(call)
    started.thread.start()
    return started


class _HeldHelpers:
    """The guard's runner, holding every helper it starts until the test lets them all go.

    It shows each exclusion by what happened, never by a pause. A helper that
    starts beside one the guard's lock must keep it from is recorded in
    :attr:`overlaps`, and a thread that has to wait in one of the guard's
    locks is recorded as parked (:meth:`watch`). So a test waits until the
    second caller has either parked or started its helper, and only then
    lets the first one finish.

    Route reads and plain read-backs pass at once. Only the holding overlaps:
    the model of nft runs one helper at a time, because it swaps the process's
    streams while a helper's script runs.
    """

    def __init__(self, run: Any) -> None:
        self.run = run
        #: Every helper started, named by :func:`_helper_name`, in order.
        self.started: list[str] = []
        #: The helpers started and not yet finished.
        self.running: Counter[str] = Counter()
        #: Each helper that started beside one it must never run beside.
        self.overlaps: list[str] = []
        self._parked: set[threading.Thread] = set()
        self._released = False
        self._changed = threading.Condition()
        self._model = threading.Lock()

    def __call__(self, argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        name = _helper_name(argv, stdin)
        with self._changed:
            if name not in _UNCHECKED:
                running = {other for other, count in self.running.items() if count}
                updates = running - _EXCLUSIVE - _UNCHECKED
                beside = running & _EXCLUSIVE
                beside |= updates if name in _EXCLUSIVE else updates & {name}
                self.overlaps += [f"{name} beside {other}" for other in sorted(beside)]
            self.started.append(name)
            self.running[name] += 1
            self._changed.notify_all()
            if name not in ("routes", "read"):
                assert self._changed.wait_for(lambda: self._released, 5), f"{name} held too long"
        try:
            with self._model:
                return self.run(argv, stdin)
        finally:
            with self._changed:
                self.running[name] -= 1
                self._changed.notify_all()

    def watch(self, lock: Any) -> None:
        """Record each thread that has to wait in ``lock``, a guard's helper lock.

        It waits in two ways: for its bridge's turn, a plain lock, and on its
        condition, for the installs and updates it must not run beside. Turns
        are handed out watched (:class:`_WatchedTurns`), and a wait on the
        condition whose predicate does not yet hold is a thread about to block.
        """
        lock._bridges = _WatchedTurns(self)
        wait_for = lock._changed.wait_for

        def waiting_for(predicate: Callable[[], bool], timeout: float | None = None) -> bool:
            if not predicate():
                self.park()
            return wait_for(predicate, timeout)

        lock._changed.wait_for = waiting_for

    def park(self) -> None:
        """Record that the calling thread has to wait in one of the guard's locks."""
        with self._changed:
            self._parked.add(threading.current_thread())
            self._changed.notify_all()

    def parked(self, call: _Call) -> bool:
        return call.thread in self._parked

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5) -> bool:
        """Whether ``predicate`` comes to hold within ``timeout``, a bound and not a pause.

        Checked again at every helper start, finish and park, and every 10 ms
        for what changes without a word to this runner, such as a call returning.
        """
        deadline = time.monotonic() + timeout
        with self._changed:
            while not predicate():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._changed.wait(min(left, 0.01))
            return True

    def release(self) -> None:
        """Let every helper held now, and every later one, finish."""
        with self._changed:
            self._released = True
            self._changed.notify_all()


class _WatchedLock:
    """A plain lock that tells the held helpers when a thread has to wait for it."""

    def __init__(self, lock: Any, helpers: _HeldHelpers) -> None:
        self._lock = lock
        self._helpers = helpers

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self._helpers.park()
            self._lock.acquire()

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


class _WatchedTurns(dict[str, tuple[Any, int]]):
    """A helper lock's bridge turns, each handed out as a :class:`_WatchedLock`."""

    def __init__(self, helpers: _HeldHelpers) -> None:
        super().__init__()
        self._helpers = helpers

    def get(self, name: str, default: tuple[Any, int]) -> tuple[Any, int]:
        turn, users = super().get(name, default)
        if not isinstance(turn, _WatchedLock):
            turn = _WatchedLock(turn, self._helpers)
        return turn, users


def _hold(guard: HostNetworkGuard) -> _HeldHelpers:
    """Hold every helper the guard starts from now on, and watch who waits in its locks."""
    helpers = _HeldHelpers(guard._run)
    guard._run = helpers
    helpers.watch(guard._lock)
    guard._image_lock = _WatchedLock(guard._image_lock, helpers)
    return helpers


def _ssh_guard() -> tuple[FakeNft, HostNetworkGuard]:
    """A verified guard that wrote SSH targets for both bridges, on :data:`_NET`."""
    nft = FakeNft()
    guard = _guard(nft)
    guard.ensure()
    for bridge in (_X, _Y):
        guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)}, network_id=_NET)
    return nft, guard


def _update(kind: str, guard: HostNetworkGuard, bridge: str) -> None:
    if kind == "relays":
        guard.sync_relays(bridge, {("10.240.0.3", 8080)})
    elif kind == "ssh":
        guard.sync_ssh_targets(bridge, {("93.184.216.35", 22)}, network_id=_NET)
    elif kind == "restore":
        guard.restore_ssh_targets(bridge, network_id=_NET)
    else:
        guard.release_bridge(bridge)


@pytest.mark.parametrize("kind", _KINDS)
def test_updates_of_different_bridges_run_their_helpers_together(kind) -> None:
    """Groups starting together on different bridges do not queue behind each other.

    Each keeps its own record, as if they had run in turn.
    """
    nft, guard = _ssh_guard()
    helpers = _hold(guard)

    first = _in_thread(lambda: guard.sync_relays(_X, {("10.240.0.3", 8080)}, network_id=_NET))
    assert helpers.wait_for(lambda: helpers.running[_X] == 1)
    second = _in_thread(lambda: _update(kind, guard, _Y))
    try:
        assert helpers.wait_for(lambda: helpers.running[_Y] or helpers.parked(second))
        assert not helpers.parked(second), "one bridge's update waited for the other's helper"
        assert helpers.running[_X] == helpers.running[_Y] == 1
    finally:
        helpers.release()
    first.result(5)
    second.result(5)
    calls = len(nft.calls)

    guard.sync_relays(_X, {("10.240.0.3", 8080)}, network_id=_NET)

    assert len(nft.calls) == calls, "the update made beside another bridge's is remembered"
    assert helpers.overlaps == []
    assert guard._lock._bridges == {}, "a bridge's turn is kept only while an update needs it"


def test_updates_of_one_bridge_take_turns_so_its_record_is_what_the_kernel_holds() -> None:
    """A restore of a lost selection never lands after the owner's change that replaced it."""
    _nft, guard = _ssh_guard()
    helpers = _hold(guard)

    restore = _in_thread(lambda: guard.restore_ssh_targets(_X, network_id=_NET))
    assert helpers.wait_for(lambda: helpers.running[_X] == 1)
    change = _in_thread(lambda: _update("ssh", guard, _X))
    try:
        assert helpers.wait_for(lambda: helpers.parked(change) or helpers.started.count(_X) == 2)
    finally:
        helpers.release()
    restore.result(5)
    change.result(5)

    assert helpers.overlaps == [], "two updates of one bridge ran their helpers together"
    assert guard.ssh_target_endpoints(_X) == {("93.184.216.35", 22)}
    assert guard.written_ssh_targets(_X) == (_NET, frozenset({("93.184.216.35", 22)}))


@pytest.mark.parametrize("kind", _KINDS)
def test_an_install_waits_for_an_update_under_way(kind) -> None:
    _nft, guard = _ssh_guard()
    helpers = _hold(guard)

    update = _in_thread(lambda: _update(kind, guard, _X))
    assert helpers.wait_for(lambda: helpers.running[_X] == 1)
    install = _in_thread(lambda: guard.ensure(max_age_s=0))
    try:
        assert helpers.wait_for(lambda: helpers.parked(install) or helpers.running["install"])
    finally:
        helpers.release()
    update.result(5)
    install.result(5)
    assert helpers.overlaps == [], "an install ran beside an update"
    assert helpers.started == [_X, "install", "routes"]


@pytest.mark.parametrize("kind", _KINDS)
def test_an_update_waits_for_an_install_and_everything_checked_after_it(kind) -> None:
    """The table is installed and read back before any bridge's exceptions change."""
    _nft, guard = _ssh_guard()
    helpers = _hold(guard)

    install = _in_thread(lambda: guard.ensure(max_age_s=0))
    assert helpers.wait_for(lambda: helpers.running["install"] == 1)
    update = _in_thread(lambda: _update(kind, guard, _X))
    try:
        assert helpers.wait_for(lambda: helpers.parked(update) or helpers.running[_X])
    finally:
        helpers.release()
    install.result(5)
    update.result(5)
    assert helpers.overlaps == [], "an update ran beside an install"
    assert helpers.started == ["install", "routes", _X]


def test_an_install_that_waits_holds_back_the_updates_that_come_after_it() -> None:
    """A stream of updates on other bridges cannot keep an install waiting."""
    _nft, guard = _ssh_guard()
    helpers = _hold(guard)

    first = _in_thread(lambda: guard.sync_relays(_X, {("10.240.0.3", 8080)}))
    assert helpers.wait_for(lambda: helpers.running[_X] == 1)
    install = _in_thread(lambda: guard.ensure(max_age_s=0))
    assert helpers.wait_for(lambda: helpers.parked(install) or helpers.running["install"])
    later = _in_thread(lambda: guard.sync_relays(_Y, {("10.240.0.11", 8080)}))
    try:
        assert helpers.wait_for(lambda: helpers.parked(later) or helpers.running[_Y])
    finally:
        helpers.release()
    for call in (first, install, later):
        call.result(5)
    assert helpers.overlaps == []
    assert helpers.parked(later), "an update overtook an install waiting for the one before it"
    assert helpers.started == [_X, "install", "routes", _Y]


def test_a_handover_checks_the_rules_inside_its_own_exclusive_hold() -> None:
    """The hold is re-entrant, so the check runs; no update starts until the handover is made."""
    nft = FakeNft()
    guard = _guard(nft)
    helpers = _hold(guard)

    handover = _in_thread(guard.handover)
    assert helpers.wait_for(lambda: helpers.running["install"] or helpers.parked(handover))
    assert not helpers.parked(handover), "the handover waited on its own hold"
    update = _in_thread(lambda: guard.sync_relays(_X, {("10.240.0.3", 8080)}))
    try:
        assert helpers.wait_for(lambda: helpers.parked(update) or helpers.running[_X])
    finally:
        helpers.release()
    handed = handover.result(5)
    update.result(5)
    assert helpers.overlaps == [], "an update ran inside a handover"
    assert set(handed) == {"verified_at", "helper_image"}
    assert helpers.started == ["install", "routes", _X]


def test_the_ssh_targets_written_for_a_bridge_wait_only_for_that_bridges_update() -> None:
    """The sweep never reads an update's gap between forgetting one record and keeping the next."""
    _nft, guard = _ssh_guard()
    helpers = _hold(guard)

    change = _in_thread(lambda: _update("ssh", guard, _X))
    assert helpers.wait_for(lambda: helpers.running[_X] == 1)
    other = _in_thread(lambda: guard.written_ssh_targets(_Y))
    own = _in_thread(lambda: guard.written_ssh_targets(_X))
    try:
        assert helpers.wait_for(
            lambda: (other.done() or helpers.parked(other)) and (own.done() or helpers.parked(own))
        )
        assert not helpers.parked(other), "waited for another bridge's update"
        assert helpers.parked(own) and not own.done(), "read while its bridge's update ran"
    finally:
        helpers.release()
    change.result(5)
    assert other.result(5) == (_NET, frozenset({("93.184.216.34", 22)}))
    assert own.result(5) == (_NET, frozenset({("93.184.216.35", 22)}))


def test_updates_that_start_before_the_image_is_known_inspect_this_container_once(
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOSTNAME", "runner-1")
    nft = FakeNft()
    _guard(nft).ensure()  # the tables, as an earlier process left them
    guard = HostNetworkGuard(_network(), run=nft)
    helpers = _hold(guard)

    first = _in_thread(lambda: guard.sync_relays(_X, {("10.240.0.3", 8080)}))
    assert helpers.wait_for(lambda: helpers.running["inspect"] == 1)
    second = _in_thread(lambda: guard.sync_relays(_Y, {("10.240.0.11", 8080)}))
    try:
        assert helpers.wait_for(
            lambda: helpers.parked(second) or helpers.started.count("inspect") == 2
        )
    finally:
        helpers.release()
    first.result(5)
    second.result(5)
    assert helpers.started.count("inspect") == 1, "each update inspected the container"
    assert sorted(helpers.started) == sorted(["inspect", _X, _Y])


def test_the_helper_lock_nests_only_its_exclusive_side() -> None:
    """Re-entrant where a handover needs it; any other nesting raises rather than deadlock."""
    from cloud_agent_host.network_guard import _HelperLock

    lock = _HelperLock()
    helpers = _HeldHelpers(run=None)
    helpers.watch(lock)

    def nest(outer: Callable[[], Any], inner: Callable[[], Any]) -> None:
        with outer(), inner():
            pass

    for outer, inner, outcome in [
        (lock.exclusive, lock.exclusive, type(None)),
        (lock.exclusive, lambda: lock.bridge(_X), RuntimeError),
        (lambda: lock.bridge(_X), lock.exclusive, RuntimeError),
        (lambda: lock.bridge(_X), lambda: lock.bridge(_Y), RuntimeError),
    ]:
        attempt = _in_thread(lambda outer=outer, inner=inner: nest(outer, inner))
        assert helpers.wait_for(lambda attempt=attempt: attempt.done() or helpers.parked(attempt))
        assert not helpers.parked(attempt), "a nested hold waited on its own thread"
        assert isinstance(attempt.exception(), outcome)
    assert (lock._owner, lock._waiting, lock._updating, lock._bridges) == (None, 0, set(), {})
