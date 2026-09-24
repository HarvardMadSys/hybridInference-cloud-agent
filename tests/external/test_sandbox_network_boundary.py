"""The one sandbox network, on a real kernel with real traffic.

Groups are built through the product path — ``ChatSandboxManager``, the host
rule guard, real bridges, the guest check — and every claim is a packet:

* each refusal has a listening target, and a positive control from the host's
  own namespace proves the target was there to refuse;
* a restriction removed on purpose must be seen to open the path it guarded,
  so a test that cannot tell open from closed fails instead of passing;
* a live, two-way flow must stop when protection returns, while a control flow
  beside it keeps going.

The private target is a routed network namespace behind a veth pair, not a
Docker network: Docker's own inter-bridge isolation would refuse a sandbox's
connection to another Docker network with or without these rules, and prove
nothing about the lab LAN or a cloud metadata service, which only these rules
stand in front of.

Opt-in twice — the ``external`` marker and ``CLOUD_AGENT_NETWORK_TEST=1`` —
because it installs nftables tables on the Docker host (in a namespace of its
own, removed afterwards) and runs a privileged helper for the routed target.
Point it at an independent test machine, never at one serving a deployment.

    CLOUD_AGENT_NETWORK_TEST=1 uv run pytest -m external \\
        tests/external/test_sandbox_network_boundary.py

``CLOUD_AGENT_NETWORK_TEST_RUNTIME=io.containerd.kata.v2`` runs the sandboxes
under Kata, which is the configuration the acceptance is about; plain runc
evidence is not Kata evidence. ``CLOUD_AGENT_NETWORK_TEST_IMAGE`` names the
sandbox image (default: a small image this module builds, with python3, curl
and nft), ``CLOUD_AGENT_NETWORK_TEST_HELPER_IMAGE`` the image carrying ``nft``
and ``ip`` for host-side helpers (default: the same), and
``CLOUD_AGENT_NETWORK_TEST_DNS`` / ``_POOL`` / ``_ROUTED_NET`` override the
resolver (default: the one Docker gives containers), the group pool
(``10.243.0.0/16``) and the routed target's /30 (``10.97.254.0/30``). The
public checks need the machine to reach the internet.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cloud_agent_host.chat_sandbox import (
    CHAT_NAMESPACE_LABEL,
    ChatSandboxManager,
    ChatSandboxSpec,
)
from cloud_agent_host.network_guard import NetworkGuardError
from cloud_agent_host.sandbox import ContainerBackend
from cloud_agent_shared.egress import SandboxNetwork, network_from_env

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(
        os.environ.get("CLOUD_AGENT_NETWORK_TEST") != "1",
        reason="changes the Docker host's firewall; set CLOUD_AGENT_NETWORK_TEST=1 to run",
    ),
]

BUILT_IMAGE = "cloud-agent-network-test:local"
GATEWAY_PORT = 8080
GATEWAY_OTHER_PORT = 9999
MCP_PORT = 8000
LISTEN_PORT = 8088
#: Host-side ports, randomised per run so a leftover or a real service on the
#: test machine does not answer in their place.
HOST_PORT = 20000 + secrets.randbelow(4000)
PUBLISHED_PORT = HOST_PORT + 4000
DNS_QUERY = (
    b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
)

# An echo server on every port given: a TCP accept for a probe, and a two-way
# flow for the live-connection checks.
ECHO = """
import socket, sys, threading
def echo(conn):
    with conn:
        while data := conn.recv(4096):
            conn.sendall(data)
def serve(port):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(64)
    while True:
        threading.Thread(target=echo, args=(server.accept()[0],), daemon=True).start()
ports = [int(port) for port in sys.argv[1:]]
for port in ports[1:]:
    threading.Thread(target=serve, args=(port,), daemon=True).start()
serve(ports[0])
"""

# One verdict per target: open, timeout, or refused with the errno it got.
PROBE = """
import errno, json, socket, sys
query = bytes.fromhex(sys.argv[2])
verdicts = {}
for host, port, proto in json.loads(sys.argv[1]):
    key = "%s:%d/%s" % (host, port, proto)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        if proto == "tcp":
            socket.create_connection((host, port), timeout=4).close()
        else:
            udp = socket.socket(family, socket.SOCK_DGRAM)
            udp.settimeout(4)
            udp.connect((host, port))
            udp.send(query)
            udp.recv(512)
        verdicts[key] = "open"
    except TimeoutError:
        verdicts[key] = "timeout"
    except OSError as exc:
        verdicts[key] = "refused:" + errno.errorcode.get(exc.errno or 0, str(exc.errno))
print(json.dumps(verdicts))
"""

# A two-way flow: one byte out and back every 100 ms, a line per tick. It
# says "ready" once it has echoed ten in a row, so the caller changes the
# rules while traffic is demonstrably flowing.
FLOW = """
import socket, sys, time
conn = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=4)
conn.settimeout(0.5)
good = 0
for tick in range(int(sys.argv[3])):
    try:
        conn.sendall(b"x")
        ok = conn.recv(1) == b"x"
    except OSError:
        ok = False
    good = good + 1 if ok else 0
    print(("1" if ok else "0") + (" ready" if good == 10 else ""), flush=True)
    time.sleep(0.1)
"""


def _docker(*argv: str, check: bool = True, timeout: float = 180, stdin: str | None = None):
    result = subprocess.run(
        ["docker", *argv], input=stdin, capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(argv[:4])} failed: {result.stderr.strip()[:400]}")
    return result


def _build_image() -> str:
    with tempfile.TemporaryDirectory(prefix="ca-net-image-") as directory:
        Path(directory, "Dockerfile").write_text(
            "FROM debian:bookworm-slim\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    python3 curl ca-certificates iproute2 nftables \\\n"
            " && rm -rf /var/lib/apt/lists/*\n"
        )
        _docker("build", "-q", "-t", BUILT_IMAGE, directory, timeout=900)
    return BUILT_IMAGE


def _engine_resolver(image: str) -> str:
    """The resolver this engine already hands containers, as dev.sh reads it."""
    answer = _docker(
        "run", "--rm", "--network", "bridge", "--entrypoint", "cat", image, "/etc/resolv.conf"
    ).stdout
    for line in answer.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "nameserver":
            with contextlib.suppress(ValueError):
                address = ipaddress.IPv4Address(parts[1])
                if not address.is_loopback:
                    return str(address)
    return "1.1.1.1"


@dataclass
class Lab:
    """Everything one run creates, and the handles the checks need."""

    token: str
    image: str
    helper: str
    runtime: str | None
    network: SandboxNetwork
    manager: ChatSandboxManager
    groups: dict[str, ChatSandboxSpec]
    routed: str
    host_port: int = HOST_PORT
    published_port: int = PUBLISHED_PORT
    relays: dict[str, str] = field(default_factory=dict)
    addresses: dict[str, str] = field(default_factory=dict)
    containers: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    listeners: list[subprocess.Popen[str]] = field(default_factory=list)

    @property
    def guard(self):
        return self.manager.backend.network_guard()

    def bridge(self, group: str) -> str:
        return self.network.bridge_name(self.manager.network_for(self.groups[group]))

    def probe(self, group: str, targets: list[tuple[str, int, str]]) -> dict[str, str]:
        """Verdicts as the sandbox user sees them, through the product exec path."""
        result = self.manager.exec_capture(
            self.groups[group],
            ["python3", "-c", PROBE, json.dumps(targets), DNS_QUERY.hex()],
            workdir="/",
            timeout_s=120,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return json.loads(result.stdout)

    def control(self, targets: list[tuple[str, int, str]]) -> dict[str, str]:
        """The same probe from the host's own namespace, which no rule here filters."""
        answer = _docker(
            "run", "--rm", "--network", "host", "--entrypoint", "python3", self.helper,
            "-c", PROBE, json.dumps(targets), DNS_QUERY.hex(),
        )  # fmt: skip
        return json.loads(answer.stdout)

    def nft(self, *argv: str, check: bool = True):
        return _docker(
            "run", "--rm", "--network", "host", "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
            "--entrypoint", "nft", self.helper, *argv, check=check,
        )  # fmt: skip

    def exec_background(self, group: str, argv: list[str]) -> subprocess.Popen[str]:
        command = self.manager.build_exec_command(self.groups[group], argv, workdir="/")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
        self.listeners.append(process)
        return process


def _key(host: str, port: int, proto: str = "tcp") -> str:
    return f"{host}:{port}/{proto}"


def _refused(verdict: str) -> bool:
    """An immediate refusal. A timeout is not one: it says nothing about why."""
    return verdict.startswith("refused:")


@pytest.fixture(scope="module")
def lab():
    runtime = os.environ.get("CLOUD_AGENT_NETWORK_TEST_RUNTIME") or None
    image = os.environ.get("CLOUD_AGENT_NETWORK_TEST_IMAGE") or ""
    helper = os.environ.get("CLOUD_AGENT_NETWORK_TEST_HELPER_IMAGE") or ""
    if not image or not helper:
        built = _build_image()
        image, helper = image or built, helper or built
    token = secrets.token_hex(3)
    namespace = f"ca-net-{token}"
    relay_network = f"{namespace}-relays"
    gateway, mcp = f"{namespace}-gateway", f"{namespace}-mcp"
    network = network_from_env(
        {
            "AGENT_SANDBOX_NAMESPACE": namespace,
            "AGENT_EGRESS_CHAT_NETWORK_POOL": os.environ.get(
                "CLOUD_AGENT_NETWORK_TEST_POOL", "10.243.0.0/16"
            ),
            "AGENT_EGRESS_DNS_SERVER": os.environ.get("CLOUD_AGENT_NETWORK_TEST_DNS")
            or _engine_resolver(helper),
            "AGENT_RELAY_NETWORK": relay_network,
            "AGENT_GATEWAY_URL": f"http://{gateway}:{GATEWAY_PORT}",
            "AGENT_SANDBOX_MCP_URL": f"http://{mcp}:{MCP_PORT}",
        }
    )
    manager = ChatSandboxManager(
        ContainerBackend(
            image=image,
            runtime=runtime,
            sandbox_network=network,
            sandbox_namespace=namespace,
            network_helper_image=helper,
        )
    )
    routed_net = ipaddress.IPv4Network(
        os.environ.get("CLOUD_AGENT_NETWORK_TEST_ROUTED_NET", "10.97.254.0/30")
    )
    host_end, routed = (str(address) for address in routed_net.hosts())
    groups = {
        name: ChatSandboxSpec(
            thread_id=f"athr_net_{token}_{name}",
            sandbox_id=f"asbx_net_{token}_{name}",
            generation=1,
            runtime_id="network-test",
            memory_limit="512m",
            cpu_limit="1",
            resident_slot_capacity=2,
        )
        for name in ("a", "b")
    }
    lab = Lab(token, image, helper, runtime, network, manager, groups, routed)
    lab.tables = [network.table]
    try:
        _docker("network", "create", "--internal", relay_network)
        lab.networks.append(relay_network)
        for name, ports in ((gateway, (GATEWAY_PORT, GATEWAY_OTHER_PORT)), (mcp, (MCP_PORT,))):
            _docker(
                "run", "-d", "--name", name, "--hostname", name, "--network", relay_network,
                "--network-alias", name, "--entrypoint", "python3", helper,
                "-c", ECHO, *map(str, ports),
            )  # fmt: skip
            lab.containers.append(name)
        lab.relays = {"gateway": gateway, "mcp": mcp}

        # The relays' own way out, as a stack's `<project>_internal` network is
        # for its SSH tunnels. Named to sort after `freeinference-chat-net-*`,
        # as that network does: at equal gateway priority Docker would then
        # hand a relay's default route to the first group bridge it joined.
        upstream = f"zz-{namespace}-upstream"
        _docker("network", "create", upstream)
        lab.networks.append(upstream)
        for name in (gateway, mcp):
            _docker("network", "connect", upstream, name)
        lab.addresses["upstream-gateway"] = json.loads(
            _docker("network", "inspect", upstream).stdout
        )[0]["IPAM"]["Config"][0]["Gateway"]

        # The host itself, listening on every address it has.
        host_listener = f"{namespace}-host"
        _docker(
            "run", "-d", "--name", host_listener, "--network", "host",
            "--entrypoint", "python3", helper, "-c", ECHO, str(HOST_PORT),
        )  # fmt: skip
        lab.containers.append(host_listener)

        # A Docker service with a published port, reached through the host.
        docker_lan = f"{namespace}-lan"
        _docker("network", "create", docker_lan)
        lab.networks.append(docker_lan)
        published = f"{namespace}-published"
        _docker(
            "run", "-d", "--name", published, "--network", docker_lan,
            "-p", f"{PUBLISHED_PORT}:80",
            "--entrypoint", "python3", helper, "-c", ECHO, "80",
        )  # fmt: skip
        lab.containers.append(published)

        # The routed private target: a namespace behind a veth pair, on the
        # allowed port 80, so the protected ranges are all that stand between.
        routed_helper = f"{namespace}-routed"
        netns, outer, inner = f"canet{token}", f"cav{token}", f"cap{token}"
        _docker(
            "run", "-d", "--name", routed_helper, "--privileged", "--network", "host",
            "--entrypoint", "sh", helper, "-ec",
            f"ip netns add {netns}; "
            f"ip link add {outer} type veth peer name {inner}; "
            f"ip link set {inner} netns {netns}; "
            f"ip addr add {host_end}/{routed_net.prefixlen} dev {outer}; ip link set {outer} up; "
            f"ip netns exec {netns} ip addr add {routed}/{routed_net.prefixlen} dev {inner}; "
            f"ip netns exec {netns} ip link set {inner} up; "
            f"ip netns exec {netns} ip link set lo up; "
            f"ip netns exec {netns} ip route add default via {host_end}; "
            f"trap 'ip link del {outer}; ip netns del {netns}' TERM; "
            f'ip netns exec {netns} python3 -c "$0" 80 & wait',
            ECHO,
        )  # fmt: skip
        lab.containers.append(routed_helper)

        for spec in groups.values():
            manager.create(spec)
            manager.start(spec)
            lab.containers.append(spec.container_name)
        relay_networks = json.loads(_docker("inspect", gateway).stdout)[0]["NetworkSettings"][
            "Networks"
        ]
        for name, spec in groups.items():
            bridge = manager.network_for(spec)
            inspected = json.loads(_docker("inspect", spec.container_name).stdout)[0]
            lab.addresses[name] = inspected["NetworkSettings"]["Networks"][bridge]["IPAddress"]
            lab.addresses[f"gateway-{name}"] = relay_networks[bridge]["IPAddress"]
            lab.exec_background(name, ["python3", "-c", ECHO, str(LISTEN_PORT)])
        route = _docker(
            "run", "--rm", "--network", "host", "--entrypoint", "ip", helper,
            "-4", "route", "get", "1.1.1.1",
        ).stdout.split()  # fmt: skip
        lab.addresses["host"] = route[route.index("src") + 1]
        ipam = json.loads(_docker("network", "inspect", manager.network_for(groups["a"])).stdout)[0]
        config = ipam["IPAM"]["Config"][0]
        lab.addresses["bridge-gateway"] = config.get("Gateway") or str(
            next(ipaddress.IPv4Network(config["Subnet"]).hosts())
        )
        time.sleep(1)
        yield lab
    finally:
        for process in lab.listeners:
            process.kill()
        _teardown(lab)


def _teardown(lab: Lab) -> None:
    if lab.containers:
        _docker("rm", "-f", *lab.containers, check=False)
    labelled = _docker(
        "ps", "-aq", "--filter", f"label={CHAT_NAMESPACE_LABEL}={lab.network.namespace}",
        check=False,
    ).stdout.split()  # fmt: skip
    if labelled:
        _docker("rm", "-f", *labelled, check=False)
    groups = _docker(
        "network", "ls", "-q", "--filter",
        f"label={CHAT_NAMESPACE_LABEL}={lab.network.namespace}", check=False,
    ).stdout.split()  # fmt: skip
    for network in [*groups, *lab.networks]:
        _docker("network", "rm", network, check=False)
    for table in lab.tables:
        for family in ("inet", "bridge"):
            lab.nft("delete", "table", family, table, check=False)


# ── what a sandbox can reach ──────────────────────────────────────────────


def test_the_public_web_answers_with_verified_tls(lab: Lab) -> None:
    for url in ("https://example.com", "http://example.com"):
        fetched = lab.manager.exec_capture(
            lab.groups["a"],
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20", url],
            workdir="/",
            timeout_s=60,
        )
        assert fetched.stdout.decode().strip() == "200", fetched.stderr.decode(errors="replace")
    verdicts = lab.probe("a", [("1.1.1.1", 443, "tcp")])
    assert verdicts[_key("1.1.1.1", 443)] == "open"


def test_names_resolve_only_through_the_named_resolver(lab: Lab) -> None:
    resolver = str(lab.network.dns_server)
    resolved = lab.manager.exec_capture(
        lab.groups["a"],
        ["python3", "-c", "import socket; print(socket.gethostbyname('example.com'))"],
        workdir="/",
    )
    assert resolved.returncode == 0, resolved.stderr.decode(errors="replace")
    targets = [(resolver, 53, "udp"), (resolver, 53, "tcp")]
    others = [("8.8.8.8", 53, "udp"), ("8.8.8.8", 53, "tcp")]
    verdicts = lab.probe("a", targets + others)
    assert all(verdicts[_key(*target)] == "open" for target in targets), verdicts
    assert all(verdicts[_key(*target)] != "open" for target in others), verdicts
    # An immediate refusal is the rules' answer; that the other resolver was
    # there to answer is what the control shows. Without it, say so.
    if lab.control([("8.8.8.8", 53, "tcp")])[_key("8.8.8.8", 53)] != "open":
        pytest.skip("8.8.8.8:53 is unreachable from this host, so its refusal proves nothing")
    assert all(_refused(verdicts[_key(*target)]) for target in others), verdicts


def test_other_public_ports_and_quic_are_refused(lab: Lab) -> None:
    # UDP gets no answer from a QUIC server to this payload with or without the
    # rules; only the rules answer at once, so "refused" is theirs.
    targets = [("1.1.1.1", 853, "tcp"), ("1.1.1.1", 443, "udp")]
    verdicts = lab.probe("a", targets)
    assert all(verdict != "open" for verdict in verdicts.values()), verdicts
    if lab.control([("1.1.1.1", 853, "tcp")])[_key("1.1.1.1", 853)] != "open":
        pytest.skip("1.1.1.1:853 is unreachable from this host, so its refusal proves nothing")
    assert all(_refused(verdict) for verdict in verdicts.values()), verdicts


# ── what it cannot ─────────────────────────────────────────────────────────


def test_the_host_and_everything_behind_it_are_refused(lab: Lab) -> None:
    targets = [
        (lab.addresses["host"], lab.host_port, "tcp"),  # the host, any of its addresses
        (lab.addresses["bridge-gateway"], lab.host_port, "tcp"),  # ... its side of the bridge
        (lab.addresses["host"], lab.published_port, "tcp"),  # a published port, via DNAT
        (lab.routed, 80, "tcp"),  # a private network the host routes to
    ]
    controls = lab.control(targets)
    assert all(verdict == "open" for verdict in controls.values()), controls
    verdicts = lab.probe("a", [*targets, ("169.254.169.254", 80, "tcp")])
    assert all(_refused(verdict) for verdict in verdicts.values()), verdicts


def test_other_groups_are_unreachable_in_both_directions(lab: Lab) -> None:
    to_b = (lab.addresses["b"], LISTEN_PORT, "tcp")
    to_a = (lab.addresses["a"], LISTEN_PORT, "tcp")
    assert lab.control([to_a, to_b]) == {_key(*to_a): "open", _key(*to_b): "open"}
    # Dropped rather than rejected between groups, so the control above is
    # what makes the silence mean something.
    assert lab.probe("a", [to_b])[_key(*to_b)] != "open"
    assert lab.probe("b", [to_a])[_key(*to_a)] != "open"


def test_a_relay_answers_only_on_its_service_port_and_its_own_bridge(lab: Lab) -> None:
    own = lab.addresses["gateway-a"]
    other_bridge = lab.addresses["gateway-b"]
    assert lab.control([(own, GATEWAY_OTHER_PORT, "tcp")])[_key(own, GATEWAY_OTHER_PORT)] == "open"
    verdicts = lab.probe(
        "a",
        [
            (lab.relays["gateway"], GATEWAY_PORT, "tcp"),
            (lab.relays["mcp"], MCP_PORT, "tcp"),
            (own, GATEWAY_OTHER_PORT, "tcp"),
            (other_bridge, GATEWAY_PORT, "tcp"),
        ],
    )
    assert verdicts[_key(lab.relays["gateway"], GATEWAY_PORT)] == "open"
    assert verdicts[_key(lab.relays["mcp"], MCP_PORT)] == "open"
    assert verdicts[_key(own, GATEWAY_OTHER_PORT)] != "open", verdicts
    assert verdicts[_key(other_bridge, GATEWAY_PORT)] != "open", verdicts


def test_ipv6_goes_nowhere(lab: Lab) -> None:
    verdict = lab.probe("a", [("2606:4700:4700::1111", 443, "tcp")])
    assert verdict[_key("2606:4700:4700::1111", 443)] != "open"


def test_the_sandbox_user_cannot_touch_its_own_network(lab: Lab) -> None:
    checked = lab.manager.exec_capture(
        lab.groups["a"],
        [
            "python3",
            "-c",
            "import json, os\n"
            "status = dict(line.split(':', 1) for line in open('/proc/self/status') if ':' in line)\n"
            "print(json.dumps({'caps': int(status['CapEff'], 16),"
            " 'resolv': os.access('/etc/resolv.conf', os.W_OK),"
            " 'hosts': os.access('/etc/hosts', os.W_OK)}))",
        ],
        workdir="/",
    )
    assert json.loads(checked.stdout) == {"caps": 0, "resolv": False, "hosts": False}
    edited = lab.manager.exec_capture(
        lab.groups["a"], ["nft", "flush", "ruleset"], workdir="/", timeout_s=30
    )
    assert edited.returncode != 0


# ── the checks can fail, and live traffic obeys the rules ─────────────────


def test_removing_the_protected_ranges_opens_the_private_target(lab: Lab) -> None:
    """The mutation: without one restriction the same probe must get through.

    Then the guard notices the tampering on its next verification and puts the
    restriction back, which is what a rule reinstall after tampering must do.
    """
    target = (lab.routed, 80, "tcp")
    lab.nft("flush", "set", "inet", lab.network.table, "protected4")
    try:
        assert lab.probe("a", [target])[_key(*target)] == "open"
    finally:
        lab.guard.invalidate()
        lab.guard.ensure(max_age_s=0)
    assert _refused(lab.probe("a", [target])[_key(*target)])


def _flows(lab: Lab, targets: dict[str, tuple[str, int]], change) -> dict[str, list[str]]:
    """Run one flow per target, apply ``change`` once all are flowing."""
    ticks: dict[str, list[str]] = {name: [] for name in targets}
    ready = {name: threading.Event() for name in targets}
    marks: dict[str, int] = {}

    def read(name: str, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            ticks[name].append(line.split()[0])
            if line.rstrip().endswith("ready"):
                ready[name].set()

    threads = []
    for name, (host, port) in targets.items():
        process = lab.exec_background("a", ["python3", "-c", FLOW, host, str(port), "80"])
        thread = threading.Thread(target=read, args=(name, process), daemon=True)
        thread.start()
        threads.append(thread)
    for name, event in ready.items():
        assert event.wait(30), f"{name} never started flowing: {ticks[name]}"
    change()
    marks.update({name: len(values) for name, values in ticks.items()})
    for thread in threads:
        thread.join(60)
    return {name: values[marks[name] :] for name, values in ticks.items()}


def test_a_live_flow_stops_when_its_destination_becomes_protected(lab: Lab) -> None:
    """No blanket established-accept: protection applies to flows already open."""
    lab.nft("flush", "set", "inet", lab.network.table, "protected4")
    try:
        after = _flows(
            lab,
            {"private": (lab.routed, 80), "relay": (lab.relays["gateway"], GATEWAY_PORT)},
            lambda: (lab.guard.invalidate(), lab.guard.ensure(max_age_s=0)),
        )
    finally:
        lab.guard.invalidate()
        lab.guard.ensure(max_age_s=0)
    # A second's grace for packets already in flight when the rules changed.
    assert after["private"][10:] and set(after["private"][10:]) == {"0"}, after["private"]
    assert set(after["relay"]) == {"1"}, after["relay"]


def test_a_withdrawn_relay_exception_stops_its_live_flow(lab: Lab) -> None:
    bridge = lab.bridge("a")
    held = lab.guard.relay_endpoints(bridge)
    mcp = {(address, port) for address, port in held if port == MCP_PORT}
    try:
        after = _flows(
            lab,
            {
                "gateway": (lab.relays["gateway"], GATEWAY_PORT),
                "mcp": (lab.relays["mcp"], MCP_PORT),
            },
            lambda: lab.guard.sync_relays(bridge, mcp),
        )
    finally:
        lab.manager.require_network(lab.groups["a"])
    assert after["gateway"][10:] and set(after["gateway"][10:]) == {"0"}, after["gateway"]
    assert set(after["mcp"]) == {"1"}, after["mcp"]
    # And lifecycle put the exception back from what Docker reports.
    assert lab.guard.relay_endpoints(bridge) == held
    target = (lab.relays["gateway"], GATEWAY_PORT, "tcp")
    assert lab.probe("a", [target])[_key(*target)] == "open"


# ── the relays' own way out ────────────────────────────────────────────────


def _default_route(container: str) -> str:
    return _docker("exec", container, "ip", "-4", "route", "show", "default").stdout.strip()


def test_a_new_network_at_equal_priority_does_take_a_relays_route(lab: Lab) -> None:
    """The takeover the relays' gateway priority prevents is real on this Docker.

    Without this, a check that the route stays put could pass only because
    this Docker picks default routes some other way.
    """
    probe = f"aa-{lab.network.namespace}-probe"
    _docker("network", "create", probe)
    lab.networks.append(probe)
    gateway = json.loads(_docker("network", "inspect", probe).stdout)[0]["IPAM"]["Config"][0][
        "Gateway"
    ]
    relay = lab.relays["mcp"]
    _docker("network", "connect", probe, relay)
    try:
        assert f"via {gateway} " in _default_route(relay) + " "
    finally:
        _docker("network", "disconnect", probe, relay)
    assert f"via {lab.addresses['upstream-gateway']} " in _default_route(relay) + " "


def test_relays_keep_their_way_out_while_groups_come_and_go(lab: Lab) -> None:
    """A group bridge never becomes a relay's default route (review of #331).

    The relay's own long-lived connection to a private address beyond the
    host — its SSH session, in a deployment — must survive a group joining
    and leaving, and the relay being attached to a bridge again.
    """
    upstream = lab.addresses["upstream-gateway"]
    for relay in lab.relays.values():
        assert f"via {upstream} " in _default_route(relay) + " ", relay
    flow = subprocess.Popen(
        ["docker", "exec", lab.relays["gateway"], "python3", "-c", FLOW, lab.routed, "80", "150"],
        stdout=subprocess.PIPE,
        text=True,
    )
    lab.listeners.append(flow)
    assert flow.stdout is not None
    ticks: list[str] = []
    for line in flow.stdout:
        ticks.append(line.split()[0])
        if line.rstrip().endswith("ready"):
            break
    assert ticks and ticks[-1] == "1", f"the relay's upstream flow never started: {ticks}"
    started = len(ticks)

    visitor = ChatSandboxSpec(
        thread_id=f"athr_net_{lab.token}_c",
        sandbox_id=f"asbx_net_{lab.token}_c",
        generation=1,
        runtime_id="network-test",
        memory_limit="512m",
        cpu_limit="1",
        resident_slot_capacity=2,
    )
    lab.manager.create(visitor)
    lab.containers.append(visitor.container_name)
    lab.manager.start(visitor)
    lab.manager.stop(visitor)
    _docker(
        "network", "disconnect", lab.manager.network_for(lab.groups["a"]), lab.relays["gateway"]
    )
    lab.manager.require_network(lab.groups["a"])

    ticks.extend(line.split()[0] for line in flow.stdout)
    flow.wait(60)
    assert set(ticks[started:]) == {"1"}, f"the relay's upstream flow broke: {ticks[started:]}"
    for relay in lab.relays.values():
        assert f"via {upstream} " in _default_route(relay) + " ", relay
    target = (lab.relays["gateway"], GATEWAY_PORT, "tcp")
    assert lab.probe("a", [target])[_key(*target)] == "open"


# ── lifecycle and neighbours ───────────────────────────────────────────────


def test_a_stopped_group_leaves_no_exception_and_comes_back_guarded(lab: Lab) -> None:
    spec = lab.groups["b"]
    bridge = lab.bridge("b")
    lab.manager.stop(spec)
    assert lab.guard.relay_endpoints(bridge) == set()
    listed = _docker("network", "ls", "-q", "--filter", f"name={lab.manager.network_for(spec)}")
    assert listed.stdout.strip() == ""
    lab.manager.start(spec)  # the guest check runs again on this cold start
    verdicts = lab.probe(
        "b", [(lab.relays["gateway"], GATEWAY_PORT, "tcp"), (lab.routed, 80, "tcp")]
    )
    assert verdicts[_key(lab.relays["gateway"], GATEWAY_PORT)] == "open"
    assert _refused(verdicts[_key(lab.routed, 80)])


def test_an_overlapping_neighbour_is_refused_without_harming_this_one(lab: Lab) -> None:
    neighbour = network_from_env(
        {
            "AGENT_SANDBOX_NAMESPACE": f"{lab.network.namespace}-neighbour",
            "AGENT_EGRESS_CHAT_NETWORK_POOL": str(lab.network.pool),
            "AGENT_EGRESS_DNS_SERVER": str(lab.network.dns_server),
            "AGENT_RELAY_NETWORK": lab.network.relay_network,
            "AGENT_GATEWAY_URL": f"http://{lab.relays['gateway']}:{GATEWAY_PORT}",
            "AGENT_SANDBOX_MCP_URL": f"http://{lab.relays['mcp']}:{MCP_PORT}",
        }
    )
    lab.tables.append(neighbour.table)
    backend = ContainerBackend(
        image=lab.image,
        sandbox_network=neighbour,
        sandbox_namespace=neighbour.namespace,
        network_helper_image=lab.helper,
    )
    with pytest.raises(NetworkGuardError):
        backend.network_guard().ensure(max_age_s=0)
    lab.guard.invalidate()
    lab.guard.ensure(max_age_s=0)
    verdicts = lab.probe(
        "a", [(lab.relays["gateway"], GATEWAY_PORT, "tcp"), (lab.routed, 80, "tcp")]
    )
    assert verdicts[_key(lab.relays["gateway"], GATEWAY_PORT)] == "open"
    assert _refused(verdicts[_key(lab.routed, 80)])
