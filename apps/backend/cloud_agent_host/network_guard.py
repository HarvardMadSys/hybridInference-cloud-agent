"""Install, verify and maintain this deployment's sandbox network rules.

The rules themselves are :mod:`cloud_agent_shared.egress`. This module puts
them on the host and keeps the per-group relay exceptions current.

**Who can touch the host's rules.** The runner and the sandbox manager run in
containers with the Docker socket, not in the host's network namespace, and
hold no ``NET_ADMIN``. They change host rules through a short-lived helper:
this process's own image, started in the host's network namespace under plain
runc (a Kata VM would edit its own kernel, not the host's), with ``NET_ADMIN``
and no other capability, a read-only root and no-new-privileges, running
``nft``. The helper keeps nothing; the kernel holds the rules, so they outlive
the process that installed them and every restart of it. A host reboot clears
them, and nothing starts a group until they are back.

**Verified, not assumed.** After every install the tables are read back and
compared with what was rendered: every chain, its hook and every rule count,
the protected set, the pool registration. A table someone flushed, a set that
lost its elements, a chain that is present but empty, all fail the check, and
a failed check means no new group network and no activation.

**One helper per step.** Starting a helper container costs far more than the
nft calls it makes, and every activation waits for it. So an install and its
read-back run in one helper, and a bridge's relay update reads, compares and
writes in one helper. A relay update that would change nothing is skipped when
this guard wrote or read those exact exceptions for that exact Docker network
within the last :data:`HELD_RELAYS_TRUST_S`, and no reinstall or failed check
has happened since. Every read-back of the tables (the sweep's, every few
seconds while groups run) confirms or forgets those remembered exceptions, so
the memory is never trusted longer than the rules themselves are, and a stalled
sweep sends the next update back to the kernel.

**A reinstalled table holds no exceptions.** A table someone deleted (a
firewall reload's ``flush ruleset``) is installed again with empty relay and
SSH sets. Every read-back reports what each bridge still holds
(:class:`HeldExceptions`), so the sweep can give a group that kept running its
exceptions back: its relays from what Docker reports, and its SSH targets as
this guard last wrote them for that same Docker network.

**Neighbours.** Every deployment registers its address pool in its own table.
The check runs after the install, so of two deployments starting together with
overlapping pools at least the later one sees the other and refuses. The pool
must also stay clear of every network the host already routes: Docker takes an
explicit subnet as given, so nothing else would stop a group bridge from
drawing the host's own traffic for those addresses.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cloud_agent_host.logging_utils import get_logger
from cloud_agent_shared.egress import (
    RELAY_FAMILIES,
    SSH_TARGET_FAMILIES,
    SSH_TARGETS_SET,
    TABLE_PREFIX,
    SandboxNetwork,
    render_ruleset,
)
from cloud_agent_shared.errors import SandboxError

logger = get_logger(__name__)

#: How long a successful verification is trusted before activation checks
#: again. Group creation and every activation call :meth:`ensure`; reads of
#: the kernel in between would put a container start on every command. The
#: background sweep does not wait this long: :meth:`recheck` reads the kernel
#: on every pass.
GUARD_RECHECK_S = 60.0
#: How old a verification may get before the sweep renews it. Earlier than
#: :data:`GUARD_RECHECK_S` by more than a sweep pass and a renewal take, so an
#: activation always finds a verification it may still trust and never pays
#: the install and read-back itself.
GUARD_RENEW_S = 45.0
#: How long after a kernel read showed them held a remembered relay update is
#: trusted: two passes of the running-group sweep, which reads the tables back
#: every five seconds. A relay update was an activation's one read of the
#: kernel, so a flushed table refused it; the memory may skip that read only
#: while the sweep keeps reading in its place, not while a long sweep step
#: (a final-archive upload) holds the sweep back.
HELD_RELAYS_TRUST_S = 10.0

_HELPER_TIMEOUT_S = 60
_RULE = re.compile(r"^add rule (\w+) (\S+) (\w+) ")
_BASE_CHAIN = re.compile(r"^add chain (\w+) (\S+) (\w+) \{ type filter hook (\w+) ")

# One guard step in one disposable helper: an optional install, then the reads
# in order, then optionally one bridge's relay update and one bridge's SSH
# target update, each computed from what the kernel holds at that moment and
# rendered as ``render_relay_update`` renders it. Every nft call is checked,
# and the first failure ends the helper with nft's own message and no output.
# No shell, persistent helper or cached kernel state is involved.
_NFT_BATCH = """
import json
import subprocess
import sys

request = json.load(sys.stdin)


def nft(args, stdin=None):
    result = subprocess.run(["nft", *args], input=stdin, capture_output=True, text=True)
    if result.returncode:
        sys.stderr.write(result.stderr or result.stdout)
        sys.exit(1)
    return result.stdout


def fail(message):
    sys.stderr.write(message)
    sys.exit(1)


if request.get("install"):
    nft(["-f", "-"], request["install"])
answer = {"listings": [json.loads(nft(["-j", *args])) for args in request.get("reads", [])]}


def converge(spec, name, label):
    table, bridge, families = spec["table"], spec["bridge"], spec["families"]
    wanted = {(bridge, address, int(port)) for address, port in spec["wanted"]}
    held = {}
    for family in families:
        listing = json.loads(nft(["-j", "list", "set", family, table, name]))
        found = [item["set"] for item in listing.get("nftables", []) if "set" in item]
        if not found:
            fail("%s %s has no set '%s'" % (family, table, name))
        elements = found[0].get("elem") or []
        held[family] = set()
        for element in elements if isinstance(elements, list) else [elements]:
            value = element.get("elem", {}).get("val", element) if isinstance(element, dict) else element
            parts = value.get("concat") if isinstance(value, dict) else None
            if not isinstance(parts, list) or len(parts) != 3:
                fail("unreadable %s set element: %r" % (label, element))
            if str(parts[0]) == bridge:
                held[family].add((str(parts[0]), str(parts[1]), int(parts[2])))

    def render(elements):
        return ", ".join('"%s" . %s . %d' % element for element in sorted(elements))

    lines = []
    for family in families:
        if held[family] - wanted:
            doomed = render(held[family] - wanted)
            lines.append("delete element %s %s %s { %s }" % (family, table, name, doomed))
    add = set()
    for family in families:
        add |= wanted - held[family]
    if add:
        for family in families:
            lines.append("add element %s %s %s { %s }" % (family, table, name, render(add)))
    update = "\\n".join(lines) + ("\\n" if lines else "")
    if update:
        nft(["-f", "-"], update)
    return update


if request.get("relays"):
    answer["relay_update"] = converge(request["relays"], "relays", "relay")
if request.get("ssh_targets"):
    answer["ssh_update"] = converge(request["ssh_targets"], "ssh_targets", "SSH target")
json.dump(answer, sys.stdout)
"""


@dataclass(frozen=True)
class HeldExceptions:
    """What one read-back of this deployment's tables showed each bridge holding."""

    #: Relay exceptions per bridge, held in every relay family.
    relays: dict[str, frozenset[tuple[str, int]]]
    #: SSH exceptions per bridge.
    ssh_targets: dict[str, frozenset[tuple[str, int]]]


@dataclass(frozen=True)
class _HeldRelays:
    """Relay exceptions this guard last wrote or read back for one bridge."""

    #: The Docker network the exceptions were synced for. A bridge removed and
    #: created again keeps its interface name but never its network id.
    network_id: str
    endpoints: frozenset[tuple[str, int]]
    #: Monotonic time of the write or read-back that last showed them held.
    confirmed_at: float


Runner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


class NetworkGuardError(SandboxError):
    """The host's sandbox rules are missing, wrong, or cannot be changed."""


def _run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=_HELPER_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NetworkGuardError(f"network guard helper could not run: {exc}") from exc


def _networks(elements: list[Any]) -> list[ipaddress.IPv4Network]:
    """Read an interval set's elements as networks, however nft prints them."""
    found: list[ipaddress.IPv4Network] = []
    for element in elements:
        value = (
            element.get("elem", {}).get("val", element) if isinstance(element, dict) else element
        )
        if isinstance(value, str):
            found.append(ipaddress.IPv4Network(value))
        elif isinstance(value, dict) and "prefix" in value:
            prefix = value["prefix"]
            found.append(ipaddress.IPv4Network(f"{prefix['addr']}/{prefix['len']}"))
        elif isinstance(value, dict) and "range" in value:
            first, last = (ipaddress.IPv4Address(bound) for bound in value["range"])
            found.extend(ipaddress.summarize_address_range(first, last))
        else:
            raise NetworkGuardError(f"unreadable interval set element: {element!r}")
    return list(ipaddress.collapse_addresses(found))


def _relay_elements(elements: list[Any]) -> set[tuple[str, str, int]]:
    found: set[tuple[str, str, int]] = set()
    for element in elements:
        value = (
            element.get("elem", {}).get("val", element) if isinstance(element, dict) else element
        )
        parts = value.get("concat") if isinstance(value, dict) else None
        if not isinstance(parts, list) or len(parts) != 3:
            raise NetworkGuardError(f"unreadable relay set element: {element!r}")
        bridge, address, port = parts
        found.add((str(bridge), str(address), int(port)))
    return found


def _set_elements_in(listing: list[Any], name: str) -> list[Any]:
    """One set's elements in a ``list table`` listing; none if it holds none."""
    for item in listing:
        found = item.get("set") if isinstance(item, dict) else None
        if isinstance(found, dict) and found.get("name") == name:
            raw = found.get("elem") or []
            return raw if isinstance(raw, list) else [raw]
    return []


def _held_in(
    listings: dict[str, list[Any]], families: tuple[str, ...], name: str
) -> dict[str, frozenset[tuple[str, int]]]:
    """The ``address . port`` elements each bridge holds in set ``name`` of every family."""
    per_family: list[dict[str, set[tuple[str, int]]]] = []
    for family in families:
        found: dict[str, set[tuple[str, int]]] = {}
        for bridge, address, port in _relay_elements(_set_elements_in(listings[family], name)):
            found.setdefault(bridge, set()).add((address, port))
        per_family.append(found)
    bridges = set.intersection(*(set(found) for found in per_family))
    return {
        bridge: frozenset(set.intersection(*(found[bridge] for found in per_family)))
        for bridge in bridges
    }


def _read_exceptions(tables: list[list[Any]]) -> HeldExceptions | None:
    """What ``list table`` listings of :data:`RELAY_FAMILIES`, in order, show each bridge holding.

    ``None`` when an element cannot be read: nothing is concluded from it.
    """
    try:
        listings = dict(zip(RELAY_FAMILIES, tables, strict=True))
        return HeldExceptions(
            relays=_held_in(listings, RELAY_FAMILIES, "relays"),
            ssh_targets=_held_in(listings, SSH_TARGET_FAMILIES, SSH_TARGETS_SET),
        )
    except (NetworkGuardError, TypeError, ValueError, AttributeError, KeyError):
        return None


class HostNetworkGuard:
    """This deployment's rules on this host, through the trusted helper."""

    def __init__(
        self,
        network: SandboxNetwork,
        *,
        docker_binary: str = "docker",
        helper_image: str | None = None,
        run: Runner | None = None,
    ) -> None:
        self.network = network
        self.docker_binary = docker_binary
        self._helper_image = helper_image
        self._run = run or _run
        self._lock = threading.RLock()
        self._verified_at: float | None = None
        self._held: dict[str, _HeldRelays] = {}
        #: The SSH exceptions this guard last wrote for each bridge, with the
        #: Docker network they were written for. Unlike ``_held`` this is not
        #: a shortcut but what a running group gets back when a reinstalled
        #: table lost it, so a failed check does not forget it.
        self._ssh_written: dict[str, tuple[str, frozenset[tuple[str, int]]]] = {}
        #: The relay exceptions this guard last wrote for each bridge. Kept
        #: for the same reason: it is what says that a read-back lost one,
        #: whatever ports the relays share.
        self._relays_written: dict[str, frozenset[tuple[str, int]]] = {}

    # ── public surface ──────────────────────────────────────────────────

    def ensure(self, *, max_age_s: float = GUARD_RECHECK_S) -> HeldExceptions | None:
        """Install (idempotently) and verify, at most once per ``max_age_s``.

        Returns what the read-back showed each bridge holding, or ``None``
        when the last verification still stood and nothing was read.
        """
        with self._lock:
            now = time.monotonic()
            if self._verified_at is not None and now - self._verified_at < max_age_s:
                return None
            self._verified_at = None
            # Remembered exceptions survive only if the read-back after this
            # install shows them still held.
            held, self._held = self._held, {}
            rendered = render_ruleset(self.network)
            read_at = time.monotonic()
            listings = self._batch(
                [*self._verification_reads(), ["list", "tables", "inet"]], install=rendered
            )["listings"]
            self._verify(rendered, listings=listings[:-1])
            self._refuse_overlapping_pools(listing=listings[-1])
            self._refuse_overlapping_routes()
            self._held = held
            self._observe_relays(listings[: len(RELAY_FAMILIES)], read_at=read_at)
            self._verified_at = time.monotonic()
            return _read_exceptions(listings[: len(RELAY_FAMILIES)])

    def recheck(self) -> HeldExceptions | None:
        """Read this deployment's tables from the kernel now, and repair them if changed.

        :meth:`ensure` trusts a verification for a minute, which suits
        activations but not the background sweep: a flush of the host's
        ruleset (an ``nftables.service`` reload, say) would leave running
        groups without their table for up to that long. So the sweep reads
        the tables back on every pass, in one helper run and without
        reinstalling, and only a difference (or a failed read) costs the full
        :meth:`ensure`: reinstall, read back, and the neighbour and route
        checks. That raises when the rules still cannot be shown, which is
        what stops the running groups. A verification past
        :data:`GUARD_RENEW_S` takes the full path regardless, so those checks
        keep their cadence and an activation never finds the verification
        lapsed.

        Returns what the read-back showed each bridge holding, as
        :meth:`ensure` does: a table reinstalled here, or by another process
        since, holds none of the running groups' exceptions.
        """
        with self._lock:
            verified_at = self._verified_at
        if verified_at is not None and time.monotonic() - verified_at < GUARD_RENEW_S:
            try:
                read_at = time.monotonic()
                listings = self._list_many(self._verification_reads())
                self._verify(render_ruleset(self.network), listings=listings)
                with self._lock:
                    self._observe_relays(listings[: len(RELAY_FAMILIES)], read_at=read_at)
                return _read_exceptions(listings[: len(RELAY_FAMILIES)])
            except NetworkGuardError as exc:
                logger.warning(
                    "network_guard_rules_changed",
                    extra={
                        "event": "network_guard_rules_changed",
                        "table": self.network.table,
                        "detail": str(exc)[:300],
                    },
                )
                self.invalidate()
        return self.ensure(max_age_s=GUARD_RENEW_S)

    def invalidate(self) -> None:
        """Forget the last verification, so the next activation reads the kernel."""
        with self._lock:
            self._verified_at = None
            self._held = {}

    def sync_relays(
        self,
        bridge: str,
        endpoints: set[tuple[str, int]],
        *,
        network_id: str | None = None,
    ) -> None:
        """Make one bridge's relay exceptions exactly ``endpoints``, in both tables.

        ``network_id`` names the Docker network the bridge belongs to. With
        it, an update this guard already made, or saw held, for that network
        within :data:`HELD_RELAYS_TRUST_S` is not made again; without it the
        kernel is always read and corrected.
        """
        self._require_own_bridge(bridge)
        wanted = frozenset((str(address), int(port)) for address, port in endpoints)
        with self._lock:
            held = self._held.pop(bridge, None)
            now = time.monotonic()
            self._relays_written.pop(bridge, None)
            if (
                network_id
                and held is not None
                and held.network_id == network_id
                and held.endpoints == wanted
                # Never trusted longer than the rules themselves are, nor
                # longer than the sweep's reads of the kernel keep up.
                and self._verified_at is not None
                and now - self._verified_at < GUARD_RECHECK_S
                and now - held.confirmed_at < HELD_RELAYS_TRUST_S
            ):
                self._held[bridge] = held
                self._relays_written[bridge] = wanted
                return
            written_at = time.monotonic()
            self._batch(
                [],
                relays={
                    "table": self.network.table,
                    "families": list(RELAY_FAMILIES),
                    "bridge": bridge,
                    "wanted": sorted([address, port] for address, port in wanted),
                },
            )
            self._relays_written[bridge] = wanted
            if network_id:
                self._held[bridge] = _HeldRelays(network_id, wanted, written_at)

    def sync_ssh_targets(
        self,
        bridge: str,
        endpoints: set[tuple[str, int]],
        *,
        network_id: str | None = None,
    ) -> None:
        """Make one bridge's SSH exceptions exactly ``endpoints``.

        Always read from the kernel and corrected in one helper. These change
        only when an owner's SSH selection does and are synced only for groups
        that have one, so there is no per-activation cost to save by
        remembering them. With ``network_id``, the Docker network the bridge
        belongs to, a successful write is kept for :meth:`restore_ssh_targets`.
        """
        self._require_own_bridge(bridge)
        with self._lock:
            self._ssh_written.pop(bridge, None)
            update = self._target_update(bridge, endpoints)
            self._batch([], ssh_targets=update)
            if network_id:
                self._ssh_written[bridge] = (
                    network_id,
                    frozenset((address, port) for address, port in update["wanted"]),
                )

    def written_relays(self, bridge: str) -> frozenset[tuple[str, int]] | None:
        """The relay exceptions this guard last wrote for one bridge, if any."""
        with self._lock:
            return self._relays_written.get(bridge)

    def written_ssh_targets(self, bridge: str) -> tuple[str, frozenset[tuple[str, int]]] | None:
        """The SSH exceptions this guard last wrote for one bridge, and for which network."""
        with self._lock:
            return self._ssh_written.get(bridge)

    def restore_ssh_targets(self, bridge: str, *, network_id: str) -> None:
        """Write back the SSH exceptions this guard last wrote for this exact network.

        Only for that network: a bridge removed and created again keeps its
        name, and its successor gets its own SSH targets from its own turn.
        The write is whatever is remembered when it runs, so an owner's change
        synced meanwhile is never undone.
        """
        self._require_own_bridge(bridge)
        with self._lock:
            written = self._ssh_written.get(bridge)
            if written is None:
                return
            if written[0] != network_id:
                del self._ssh_written[bridge]
                return
            self._batch([], ssh_targets=self._target_update(bridge, set(written[1])))

    def release_bridge(self, bridge: str) -> None:
        """Drop every exception a removed bridge had, relays and SSH alike, in one helper."""
        self._require_own_bridge(bridge)
        with self._lock:
            self._held.pop(bridge, None)
            self._ssh_written.pop(bridge, None)
            self._relays_written.pop(bridge, None)
            self._batch(
                [],
                relays={
                    "table": self.network.table,
                    "families": list(RELAY_FAMILIES),
                    "bridge": bridge,
                    "wanted": [],
                },
                ssh_targets=self._target_update(bridge, set()),
            )

    def ssh_target_endpoints(self, bridge: str) -> set[tuple[str, int]]:
        """The SSH exceptions the kernel currently holds for one bridge."""
        table = self.network.table
        with self._lock:
            (listing,) = self._list_many([["list", "set", "inet", table, SSH_TARGETS_SET]])
        elements = _relay_elements(self._set_elements(listing, "inet", table, SSH_TARGETS_SET))
        return {(address, port) for owner, address, port in elements if owner == bridge}

    def _require_own_bridge(self, bridge: str) -> None:
        if not bridge.startswith(self.network.interface_prefix):
            raise NetworkGuardError(f"{bridge!r} is not one of this deployment's bridges")

    def _target_update(self, bridge: str, endpoints: set[tuple[str, int]]) -> dict[str, Any]:
        wanted: set[tuple[str, int]] = set()
        for address, port in endpoints:
            try:
                parsed = ipaddress.IPv4Address(address)
            except ValueError as exc:
                raise NetworkGuardError(f"SSH target {address!r} is not an IPv4 address") from exc
            if not 0 < int(port) < 65536:
                raise NetworkGuardError(f"SSH target port {port!r} is out of range")
            wanted.add((str(parsed), int(port)))
        return {
            "table": self.network.table,
            "families": list(SSH_TARGET_FAMILIES),
            "bridge": bridge,
            "wanted": sorted([address, port] for address, port in wanted),
        }

    def relay_endpoints(self, bridge: str) -> set[tuple[str, int]]:
        """The exceptions both tables currently hold for one bridge."""
        with self._lock:
            held = [
                {(address, port) for owner, address, port in elements if owner == bridge}
                for elements in self._relay_sets().values()
            ]
        return held[0] & held[1]

    # ── verification ────────────────────────────────────────────────────

    def _verification_reads(self) -> list[list[str]]:
        table = self.network.table
        return [
            *(["list", "table", family, table] for family in RELAY_FAMILIES),
            ["list", "set", "inet", table, "protected4"],
            ["list", "set", "inet", table, "pool4"],
        ]

    def _verify(self, rendered: str, *, listings: list[list[Any]] | None = None) -> None:
        table = self.network.table
        if listings is None:
            listings = self._list_many(self._verification_reads())
        expected_rules: dict[tuple[str, str], int] = {}
        expected_hooks: dict[tuple[str, str], str] = {}
        for line in rendered.splitlines():
            if match := _RULE.match(line):
                family, _, chain = match.groups()
                expected_rules[(family, chain)] = expected_rules.get((family, chain), 0) + 1
            if match := _BASE_CHAIN.match(line):
                family, _, chain, hook = match.groups()
                expected_hooks[(family, chain)] = hook

        for family, listing in zip(RELAY_FAMILIES, listings[:-2], strict=True):
            chains = {
                item["chain"]["name"]: item["chain"]
                for item in listing
                if isinstance(item, dict) and "chain" in item
            }
            rules: dict[str, int] = {}
            for item in listing:
                if isinstance(item, dict) and "rule" in item:
                    rules[item["rule"]["chain"]] = rules.get(item["rule"]["chain"], 0) + 1
            for (rule_family, chain), count in expected_rules.items():
                if rule_family != family:
                    continue
                if chain not in chains:
                    raise NetworkGuardError(f"{family} {table} is missing chain {chain!r}")
                if rules.get(chain, 0) != count:
                    raise NetworkGuardError(
                        f"{family} {table} chain {chain!r} holds {rules.get(chain, 0)} rules, "
                        f"expected {count}"
                    )
            for (hook_family, chain), hook in expected_hooks.items():
                if hook_family != family:
                    continue
                observed = chains.get(chain) or {}
                if observed.get("hook") != hook or observed.get("policy") != "accept":
                    raise NetworkGuardError(
                        f"{family} {table} chain {chain!r} is not hooked on {hook!r}"
                    )

        protected = _networks(self._set_elements(listings[-2], "inet", table, "protected4"))
        if protected != list(ipaddress.collapse_addresses(self.network.protected)):
            raise NetworkGuardError(f"inet {table} protects {protected}, not the configured set")
        pool = _networks(self._set_elements(listings[-1], "inet", table, "pool4"))
        if pool != [self.network.pool]:
            raise NetworkGuardError(f"inet {table} registers pool {pool}, not {self.network.pool}")

    def _refuse_overlapping_pools(self, *, listing: list[Any]) -> None:
        """Refuse to run beside an earlier deployment whose pool overlaps ours.

        Earlier is the table the kernel created first (a lower handle). The
        later one refuses; the earlier one keeps running, so a neighbour
        started with a copied pool cannot take a working deployment down with
        it. Both are checked after installing, so two starting together cannot
        both miss each other.
        """
        own = self.network.table
        tables: dict[str, int] = {}
        for item in listing:
            table = item.get("table") if isinstance(item, dict) else None
            if isinstance(table, dict) and str(table.get("name", "")).startswith(TABLE_PREFIX):
                tables[str(table["name"])] = int(table.get("handle", 0))
        if own not in tables:
            raise NetworkGuardError(f"inet {own} disappeared while it was being checked")
        earlier = [name for name, handle in tables.items() if name != own and handle <= tables[own]]
        listings = self._list_many([["list", "set", "inet", name, "pool4"] for name in earlier])
        for name, listing in zip(earlier, listings, strict=True):
            for neighbour in _networks(self._set_elements(listing, "inet", name, "pool4")):
                if neighbour.overlaps(self.network.pool):
                    raise NetworkGuardError(
                        f"this deployment's pool {self.network.pool} overlaps {neighbour}, "
                        f"registered earlier by {name} on this host; give each deployment "
                        "its own AGENT_EGRESS_CHAT_NETWORK_POOL"
                    )

    def _refuse_overlapping_routes(self) -> None:
        """Refuse a pool that shares addresses with a network the host routes.

        Docker checks an explicit subnet against other Docker networks only,
        never against the host's own routes. A group bridge inside a LAN, a VPN
        route or anything else the host already reaches would add a more
        specific route and draw the host's traffic for those addresses into a
        sandbox bridge. The default route, and catch-alls wider than a /8 such
        as a full-tunnel VPN's two /1 halves, claim no particular network; this
        deployment's own bridges are its pool at work.
        """
        pool = self.network.pool
        for route in self._host_routes():
            destination = str(route.get("dst") or "")
            if not destination or destination == "default":
                continue
            try:
                network = ipaddress.IPv4Network(destination, strict=False)
            except ValueError:
                continue
            device = str(route.get("dev") or "")
            if network.prefixlen < 8 or device.startswith(self.network.interface_prefix):
                continue
            if network.overlaps(pool):
                where = f"on {device}" if device else f"({route.get('type') or 'route'})"
                raise NetworkGuardError(
                    f"this deployment's pool {pool} overlaps the host route {network} "
                    f"{where}; a group bridge there would take the host's traffic for those "
                    "addresses. Give AGENT_EGRESS_CHAT_NETWORK_POOL a range no host network uses"
                )

    def _host_routes(self) -> list[dict[str, Any]]:
        """Every IPv4 route the host has, in every table; reading needs no privilege."""
        result = self._helper("ip", ["-4", "-j", "route", "show", "table", "all"], net_admin=False)
        try:
            routes = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise NetworkGuardError("ip returned an unreadable route listing") from exc
        if not isinstance(routes, list):
            raise NetworkGuardError("ip returned an unexpected route listing")
        return [route for route in routes if isinstance(route, dict)]

    # ── nft through the helper ─────────────────────────────────────────

    def _relay_sets(self) -> dict[str, set[tuple[str, str, int]]]:
        table = self.network.table
        listings = self._list_many(
            [["list", "set", family, table, "relays"] for family in RELAY_FAMILIES]
        )
        return {
            family: _relay_elements(self._set_elements(listing, family, table, "relays"))
            for family, listing in zip(RELAY_FAMILIES, listings, strict=True)
        }

    @staticmethod
    def _set_elements(listing: list[Any], family: str, table: str, name: str) -> list[Any]:
        for item in listing:
            if isinstance(item, dict) and "set" in item:
                elements = item["set"].get("elem") or []
                return elements if isinstance(elements, list) else [elements]
        raise NetworkGuardError(f"{family} {table} has no set {name!r}")

    def _observe_relays(self, tables: list[list[Any]], *, read_at: float) -> None:
        """Confirm or forget remembered exceptions against one read-back of both tables.

        ``tables`` are the ``list table`` listings of :data:`RELAY_FAMILIES`, in
        order, read starting at ``read_at``. An exception is confirmed only
        when every table holds exactly what was remembered for its bridge.
        Anything unreadable forgets everything, so the next update reads the
        kernel itself.
        """
        try:
            per_family = [
                _relay_elements(_set_elements_in(listing, "relays")) for listing in tables
            ]
        except (NetworkGuardError, TypeError, ValueError, AttributeError):
            self._held = {}
            return
        confirmed: dict[str, _HeldRelays] = {}
        for bridge, held in self._held.items():
            if per_family and all(
                {(address, port) for owner, address, port in elements if owner == bridge}
                == held.endpoints
                for elements in per_family
            ):
                confirmed[bridge] = _HeldRelays(held.network_id, held.endpoints, read_at)
        self._held = confirmed

    def _list_many(self, commands: list[list[str]]) -> list[list[Any]]:
        if not commands:
            return []
        return self._batch(commands)["listings"]

    def _batch(
        self,
        reads: list[list[str]],
        *,
        install: str | None = None,
        relays: dict[str, Any] | None = None,
        ssh_targets: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one guard step in one helper; see :data:`_NFT_BATCH`."""
        request: dict[str, Any] = {"reads": reads}
        if install is not None:
            request["install"] = install
        if relays is not None:
            request["relays"] = relays
        if ssh_targets is not None:
            request["ssh_targets"] = ssh_targets
        result = self._helper("python3", ["-c", _NFT_BATCH], stdin=json.dumps(request), label="nft")
        try:
            answer = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise NetworkGuardError("nft returned unreadable JSON") from exc
        payloads = answer.get("listings") if isinstance(answer, dict) else None
        if not isinstance(payloads, list) or len(payloads) != len(reads):
            raise NetworkGuardError("nft returned an unexpected listing")
        listings: list[list[Any]] = []
        for payload in payloads:
            items = payload.get("nftables") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise NetworkGuardError("nft returned an unexpected listing")
            listings.append(items)
        if relays is not None and not isinstance(answer.get("relay_update"), str):
            raise NetworkGuardError("nft returned no relay update")
        if ssh_targets is not None and not isinstance(answer.get("ssh_update"), str):
            raise NetworkGuardError("nft returned no SSH target update")
        return {**answer, "listings": listings}

    def _helper(
        self,
        entrypoint: str,
        args: list[str],
        *,
        stdin: str | None = None,
        net_admin: bool = True,
        label: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            self.docker_binary,
            "run",
            "--rm",
            *(["--interactive"] if stdin is not None else []),
            "--pull",
            "never",
            # The host's namespace and kernel: never a VM, whatever the daemon's
            # default runtime is, or the rules would land in a guest.
            "--network",
            "host",
            "--runtime",
            "runc",
            "--cap-drop",
            "ALL",
            *(["--cap-add", "NET_ADMIN"] if net_admin else []),
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--user",
            "0:0",
            "--entrypoint",
            entrypoint,
            self._image(),
            *args,
        ]
        result = self._run(command, stdin)
        if result.returncode != 0:
            raise NetworkGuardError(
                f"{label or ' '.join([entrypoint, *args[:4]])} failed: "
                + (result.stderr or result.stdout).strip()[:400]
            )
        return result

    def _image(self) -> str:
        """This process's own image: the same trusted build runs the helper."""
        if self._helper_image:
            return self._helper_image
        container = os.environ.get("HOSTNAME", "")
        inspected = self._run(
            [self.docker_binary, "container", "inspect", "--format", "{{.Image}}", container],
            None,
        )
        image = inspected.stdout.strip()
        if inspected.returncode != 0 or not image.startswith("sha256:"):
            raise NetworkGuardError(
                "the network guard runs its helper from this container's own image, and "
                f"could not inspect container {container!r}: "
                + (inspected.stderr or inspected.stdout).strip()[:200]
            )
        self._helper_image = image
        return image


__all__ = ["GUARD_RECHECK_S", "HeldExceptions", "HostNetworkGuard", "NetworkGuardError"]
