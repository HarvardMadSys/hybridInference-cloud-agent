"""Install, verify and maintain this deployment's sandbox network rules.

The rules themselves are :mod:`cloud_agent_shared.egress`. This module puts
them on the host and keeps the per-group relay exceptions current.

**Who can touch the host's rules.** The runner and the workspace broker run in
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
from typing import Any

from cloud_agent_host.logging_utils import get_logger
from cloud_agent_shared.egress import (
    RELAY_FAMILIES,
    TABLE_PREFIX,
    SandboxNetwork,
    render_relay_update,
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

_HELPER_TIMEOUT_S = 60
_RULE = re.compile(r"^add rule (\w+) (\S+) (\w+) ")
_BASE_CHAIN = re.compile(r"^add chain (\w+) (\S+) (\w+) \{ type filter hook (\w+) ")

# Reads still execute independently and in order, but share one disposable
# helper. No shell, persistent helper or cached kernel state is involved.
_NFT_LIST_BATCH = """
import json
import subprocess
import sys

listings = []
for args in json.load(sys.stdin):
    result = subprocess.run(["nft", "-j", *args], capture_output=True, text=True)
    if result.returncode:
        sys.stderr.write(result.stderr or result.stdout)
        sys.exit(1)
    listings.append(json.loads(result.stdout))
json.dump(listings, sys.stdout)
"""

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

    # ── public surface ──────────────────────────────────────────────────

    def ensure(self, *, max_age_s: float = GUARD_RECHECK_S) -> None:
        """Install (idempotently) and verify, at most once per ``max_age_s``."""
        with self._lock:
            now = time.monotonic()
            if self._verified_at is not None and now - self._verified_at < max_age_s:
                return
            self._verified_at = None
            rendered = render_ruleset(self.network)
            self._nft(["-f", "-"], stdin=rendered)
            listings = self._list_many([*self._verification_reads(), ["list", "tables", "inet"]])
            self._verify(rendered, listings=listings[:-1])
            self._refuse_overlapping_pools(listing=listings[-1])
            self._refuse_overlapping_routes()
            self._verified_at = time.monotonic()

    def recheck(self) -> None:
        """Read this deployment's tables from the kernel now, and repair them if changed.

        :meth:`ensure` trusts a verification for a minute, which suits
        activations but not the background sweep: a flush of the host's
        ruleset (an ``nftables.service`` reload, say) would leave running
        groups without their table for up to that long. So the sweep reads
        the tables back on every pass, in one helper run and without
        reinstalling, and only a difference (or a failed read) costs the full
        :meth:`ensure`: reinstall, read back, and the neighbour and route
        checks. That raises when the rules still cannot be shown, which is
        what stops the running groups. A verification past its minute takes
        the full path regardless, so those checks keep their cadence.
        """
        with self._lock:
            verified_at = self._verified_at
        if verified_at is not None and time.monotonic() - verified_at < GUARD_RECHECK_S:
            try:
                self._verify(render_ruleset(self.network))
                return
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
        self.ensure()

    def invalidate(self) -> None:
        """Forget the last verification, so the next activation reads the kernel."""
        with self._lock:
            self._verified_at = None

    def sync_relays(self, bridge: str, endpoints: set[tuple[str, int]]) -> None:
        """Make one bridge's relay exceptions exactly ``endpoints``, in both tables."""
        if not bridge.startswith(self.network.interface_prefix):
            raise NetworkGuardError(f"{bridge!r} is not one of this deployment's bridges")
        with self._lock:
            wanted = {(bridge, address, port) for address, port in endpoints}
            remove: dict[str, list[tuple[str, str, int]]] = {}
            add: set[tuple[str, str, int]] = set()
            for family, elements in self._relay_sets().items():
                current = {element for element in elements if element[0] == bridge}
                if doomed := sorted(current - wanted):
                    remove[family] = doomed
                add |= wanted - current
            if not remove and not add:
                return
            self._nft(
                ["-f", "-"],
                stdin=render_relay_update(self.network, remove=remove, add=sorted(add)),
            )

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

    def _list_many(self, commands: list[list[str]]) -> list[list[Any]]:
        if not commands:
            return []
        result = self._helper("python3", ["-c", _NFT_LIST_BATCH], stdin=json.dumps(commands))
        try:
            payloads = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise NetworkGuardError("nft returned unreadable JSON") from exc
        if not isinstance(payloads, list) or len(payloads) != len(commands):
            raise NetworkGuardError("nft returned an unexpected listing")
        listings: list[list[Any]] = []
        for payload in payloads:
            items = payload.get("nftables") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise NetworkGuardError("nft returned an unexpected listing")
            listings.append(items)
        return listings

    def _nft(
        self, args: list[str], *, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self._helper("nft", args, stdin=stdin)

    def _helper(
        self,
        entrypoint: str,
        args: list[str],
        *,
        stdin: str | None = None,
        net_admin: bool = True,
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
                f"{entrypoint} {' '.join(args[:4])} failed: "
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


__all__ = ["GUARD_RECHECK_S", "HostNetworkGuard", "NetworkGuardError"]
