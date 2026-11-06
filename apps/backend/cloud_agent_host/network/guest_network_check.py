"""The in-guest check that a started sandbox sits behind its group's network rules.

:class:`GuestNetworkCheckMixin` is the part of
:class:`~cloud_agent_host.network.chat_sandbox_network.ChatSandboxNetwork` that
runs :data:`_GUEST_NETWORK_CHECK` inside a freshly started guest, as the
sandbox user, and stops the sandbox when the guest does not see the boundary
the agent will: a default route, the one resolver, the relays, and the host
and platform addresses refused by the rules themselves.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent_host.container.chat_sandbox_models import RetainedChatFailStopped
from cloud_agent_host.container.sandbox import KATA_RUNTIME, SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.container.chat_sandbox_models import ChatSandboxSpec
    from cloud_agent_host.container.sandbox import ContainerBackend
    from cloud_agent_host.network.chat_sandbox_network import ChatNetworkContainers
    from cloud_agent_shared.egress import SandboxNetwork

# Run inside a guest as the sandbox user after every cold start, with $1 the
# resolver the guest should use and $2 the relays as JSON. Prints the problems
# it found as a JSON list; an empty list is a pass. Every probe has a short
# bound and nothing here leaves the host: the resolver answers one query for the
# root's name servers, and the boundary probes aim at the host itself and at the
# two platform addresses (cloud metadata, and Azure's WireServer on a public
# address), which the host rules must refuse at once with their own ICMP
# "administratively prohibited". A host without the rules would answer "port
# unreachable" for itself instead, and route the others onwards.

_GUEST_NETWORK_CHECK = """
import errno, json, socket, struct, sys
resolver, relays = sys.argv[1], json.loads(sys.argv[2])
problems = []
gateway = None
for row in [line.split() for line in open('/proc/net/route').read().splitlines()[1:]]:
    if len(row) > 2 and row[1] == '00000000':
        gateway = socket.inet_ntoa(struct.pack('<L', int(row[2], 16)))
        break
if gateway is None:
    problems.append('no default route')
servers = [line.split()[1] for line in open('/etc/resolv.conf')
           if line.startswith('nameserver') and len(line.split()) > 1]
if servers != [resolver]:
    problems.append('resolver is %r, expected %r' % (servers, [resolver]))
query = bytes.fromhex('4321 0100 0001 0000 0000 0000 00 0002 0001'.replace(' ', ''))
try:
    dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dns.settimeout(3)
    dns.connect((resolver, 53))
    dns.send(query)
    answer = dns.recv(512)
    if len(answer) < 12 or answer[:2] != query[:2] or not answer[2] & 0x80 or answer[3] & 0x0F:
        problems.append('resolver %s did not answer the query usefully' % resolver)
except OSError as exc:
    problems.append('resolver %s did not answer: %s' % (resolver, exc))
for host, port in relays:
    try:
        socket.create_connection((host, port), timeout=5).close()
    except OSError as exc:
        problems.append('relay %s:%d unreachable: %s' % (host, port, exc))
def refused_by_rules(label, attempt):
    try:
        attempt()
    except TimeoutError:
        problems.append('%s was not refused within 2s' % label)
    except OSError as exc:
        if exc.errno != errno.EHOSTUNREACH:
            name = errno.errorcode.get(exc.errno or 0, str(exc.errno))
            problems.append('%s was refused by %s, not by the network rules' % (label, name))
    else:
        problems.append("%s answered; the network rules are not in this guest's path" % label)
def host_udp():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.settimeout(2)
    probe.connect((gateway, 9))
    probe.send(b'x')
    probe.recv(1)
if gateway is not None:
    refused_by_rules('the host at %s:9/udp' % gateway, host_udp)
for label, address in (
    ('the metadata address 169.254.169.254:80', '169.254.169.254'),
    ('the platform address 168.63.129.16:80', '168.63.129.16'),
):
    refused_by_rules(
        label, lambda address=address: socket.create_connection((address, 80), timeout=2).close()
    )
print(json.dumps(problems))
"""


class GuestNetworkCheckMixin:
    """Prove a started guest is behind the group's rules; part of ``ChatSandboxNetwork``."""

    _containers: ChatNetworkContainers

    if TYPE_CHECKING:

        @property
        def backend(self) -> ContainerBackend:
            """Return the owning manager's deployment configuration."""
            ...

        def _network(self) -> SandboxNetwork:
            """This deployment's network; only group-network work needs it."""
            ...

    def _verify_guest_network(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str | None = None,
        defer_network_release: bool = False,
    ) -> None:
        """Check, from inside the guest, that it sits behind the group's rules.

        The guard proves the host's rules exist; this proves the guest is
        behind them the way the agent will be: a default route, the one
        resolver answering a real query, the relay accepting on its service
        port, and the host and the metadata address refused by the rules
        themselves. It runs as the sandbox user, so it sees what the agent
        sees. Reaching the public internet is deliberately not a condition: an
        outside outage is not a broken boundary. A failure stops the sandbox,
        which releases the bridge and keeps the workspace.
        """
        network = self._network()
        expected_resolver = (
            str(network.dns_server) if self.backend.runtime == KATA_RUNTIME else "127.0.0.11"
        )
        relays = [[relay.host, relay.port] for relay in network.relays]
        result = self._containers.exec_capture(
            spec,
            [
                "/usr/bin/python3",
                "-I",
                "-c",
                _GUEST_NETWORK_CHECK,
                expected_resolver,
                json.dumps(relays),
            ],
            # The workspace is writable by the repository and every agent turn.
            # Isolated Python from / never imports a planted json.py or
            # socket.py in place of the standard library, so the check can
            # neither be spoofed nor stall start-up behind its timeout.
            workdir="/",
            timeout_s=45,
        )
        try:
            problems = json.loads(result.stdout.decode(errors="replace").strip() or "null")
        except json.JSONDecodeError:
            problems = None
        if result.returncode == 0 and problems == []:
            return
        detail = (
            "; ".join(map(str, problems))
            if isinstance(problems, list)
            else result.stderr.decode(errors="replace").strip()[:300]
        )
        try:
            stopped = self._containers.stop(
                spec,
                expected_container_id=expected_container_id,
                release_networks=not defer_network_release,
            )
        except SandboxError as exc:
            raise SandboxError(
                f"group network check failed inside the sandbox ({detail}), and stopping "
                f"it failed too: {exc}"
            ) from exc
        raise RetainedChatFailStopped(
            f"group network check failed inside the sandbox: {detail}",
            stopped,
        )


__all__ = ["GuestNetworkCheckMixin"]
