"""Resolve the sandbox manager that owns a retained chat group.

Every executable attempt belongs to an enrolled host. The attempt records that
host and the host heartbeat records its sandbox manager URL, so files, terminals, Git,
fork, snapshots, and native-session operations all route through the same
durable ownership record. There is deliberately no process-global fallback:
using one would make a missing host fence silently address another machine's
container.

**The token is still global.** Host rows carry a URL, not a credential; every
sandbox manager in the pool authenticates the same `AGENT_SANDBOX_MANAGER_TOKEN`. That
is what the phase-G task asked for and no more: per-host sandbox manager credentials are
a secret-distribution design (issue one at enrollment, rotate it, get it onto
the machine) rather than a routing change, and doing half of it here would look
like the problem was solved. Until then, a compromised host's sandbox manager token
opens every other host's sandbox manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from cloud_agent.logging_utils import get_logger
from cloud_agent.sandbox_manager_client import (
    SandboxManagerClient,
    SandboxManagerError,
)
from cloud_agent.settings import setting_env

if TYPE_CHECKING:
    from cloud_agent.ports import HostPoolRepository

logger = get_logger(__name__)

#: Hosts a sandbox manager URL may name. Empty means "any", which is the shape a
#: single-host deployment runs in and the shape this had before the check.
#: An entry is either a bare hostname, which admits that host on any scheme and
#: port as it always did, or a ``scheme://host:port`` origin, which admits
#: exactly that origin.
ENV_SANDBOX_MANAGER_HOSTS = "AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS"


class SandboxManagerUrlRefused(ValueError):
    """A host reported a sandbox manager address this deployment will not talk to."""


def _origin(scheme: str, hostname: str, port: int | None) -> str:
    """Render one origin canonically, with its default port made explicit."""
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{host}:{port or (443 if scheme == 'https' else 80)}"


def _allowlisted(entry: str, hostname: str, origin: str) -> bool:
    """Whether one allowlist entry admits this sandbox manager address."""
    if "://" not in entry:
        return entry.lower() == hostname
    try:
        parsed = urlsplit(entry)
        port = parsed.port
    except ValueError:
        return False  # An entry that is not an origin admits nothing.
    if parsed.hostname is None or parsed.path not in ("", "/"):
        return False
    return _origin(parsed.scheme.lower(), parsed.hostname, port) == origin


#: Addresses no sandbox manager lives at, whatever the allowlist says.
#: Link-local holds the cloud metadata services (169.254.169.254, and AWS's
#: fd00:ec2::254 over IPv6), and the control plane's probe of a sandbox manager
#: carries the pool's token: aimed there, it is a credentialed request to the
#: control plane's own instance identity. Loopback and private ranges stay
#: allowed, because single-host and private pools put their sandbox managers
#: exactly there.
_NEVER_SANDBOX_MANAGER_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)


def _literal_address(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a numeric host names, in any spelling the resolver accepts.

    ``None`` for a name: what a name resolves to is the allowlist's business.
    """
    name = hostname.rstrip(".")
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(name)
    except ValueError:
        try:
            # What the resolver applies to a numeric host, including shorthand
            # spellings ``ip_address`` refuses: "2852039166", "0xa9fea9fe",
            # "169.254.43518" all name 169.254.169.254.
            address = ipaddress.IPv4Address(socket.inet_aton(name))
        except (OSError, ValueError):
            return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def validate_sandbox_manager_url(url: str | None) -> str | None:
    """Return ``url`` if this deployment may send its sandbox manager token there.

    **A host proposes its own sandbox manager address, and the control plane then sends
    the pool's shared token to it.** So an address accepted here is an address
    that receives a credential good for every sandbox manager in the pool — a
    compromised host, or one with a typo, could name anything and be handed it.

    The checks are structural:

    * **Scheme.** `http`/`https` only. A `file:` or `gopher:` URL is not a
      sandbox manager, and the client would do something surprising with it.
    * **An origin and nothing else.** Requests are built by appending
      ``/workspaces/...`` to this address, so a query or fragment would swallow
      that path and send the credentialed request somewhere else on the host,
      a path would choose a different service behind it, and credentials in
      the authority are a second secret no sandbox manager needs. Each is refused; a
      single trailing slash is allowed.
    * **Never a link-local address**, written in any spelling: that is where
      cloud metadata services answer (see ``_NEVER_SANDBOX_MANAGER_NETWORKS``).
    * **Allowlist**, when configured. Names or exact origins, not patterns: a
      pattern is a thing to get subtly wrong, and the set of machines in a pool
      is small and known. A bare name admits that host on any port, as it
      always did; an origin entry also pins the scheme and port, which is what
      tells apart sandbox managers that share one tunnel address. Unconfigured means any
      other host, which is what this did before — the check is opt-in so that turning
      it on is a deployment's decision rather than something that breaks every
      existing pool on upgrade. It is also the only check on what a *name*
      resolves to.

    Raises:
        SandboxManagerUrlRefused: If the URL is unusable, names a link-local
            address, or names a host outside the allowlist. Refused rather than
            dropped: a host whose address is ignored looks enrolled and
            silently never serves a workspace.
    """
    if url is None or not url.strip():
        return None
    candidate = url.strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        raise SandboxManagerUrlRefused("sandbox_manager_url must be a valid http(s) URL") from None
    try:
        port = parsed.port
    except ValueError:
        raise SandboxManagerUrlRefused(
            f"sandbox_manager_url has an invalid port, got {candidate!r}"
        ) from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SandboxManagerUrlRefused(
            f"sandbox_manager_url must be an http(s) URL, got {candidate!r}"
        )
    if "@" in parsed.netloc:
        raise SandboxManagerUrlRefused("sandbox_manager_url must not carry credentials")
    if (
        parsed.path not in ("", "/")
        or "?" in candidate
        or "#" in candidate
        or "\\" in candidate
        or any(character.isspace() for character in candidate)
    ):
        raise SandboxManagerUrlRefused(
            f"sandbox_manager_url must be a scheme://host:port origin with no path, query or "
            f"fragment, got {candidate!r}"
        )
    if not parsed.hostname.isprintable():
        # No name or address contains one, and the resolver stops reading a
        # host at a NUL: what it would dial is not what was checked below.
        raise SandboxManagerUrlRefused(
            "sandbox_manager_url host must not contain control characters"
        )
    address = _literal_address(parsed.hostname)
    if address is not None and any(
        address in network for network in _NEVER_SANDBOX_MANAGER_NETWORKS
    ):
        raise SandboxManagerUrlRefused(
            f"sandbox_manager_url host {parsed.hostname!r} is a link-local address, "
            "where no sandbox manager runs"
        )

    allowed = [
        entry.strip()
        for entry in (setting_env(ENV_SANDBOX_MANAGER_HOSTS) or "").split(",")
        if entry.strip()
    ]
    # Both names resolve to the same Compose service during its rename.
    if "workspace-broker" in allowed:
        allowed.append("sandbox-manager")
    origin = _origin(parsed.scheme, parsed.hostname, port)
    if allowed and not any(_allowlisted(entry, parsed.hostname, origin) for entry in allowed):
        raise SandboxManagerUrlRefused(
            f"sandbox_manager_url {origin!r} is not in {ENV_SANDBOX_MANAGER_HOSTS}"
        )
    return candidate


ENV_SANDBOX_MANAGER_TOKEN = "AGENT_SANDBOX_MANAGER_TOKEN"


def sandbox_manager_configuration_error(host: dict[str, Any] | None) -> str | None:
    """Describe an unusable sandbox manager configuration without exposing URLs or secrets."""
    if host is None:
        return (
            "The owning host is no longer registered. Restore that host's registration "
            "and sandbox manager before retrying deletion."
        )
    try:
        url = validate_sandbox_manager_url(host.get("sandbox_manager_url"))
    except SandboxManagerUrlRefused:
        return (
            "The owning host's sandbox manager URL is invalid or refused by the host allowlist. "
            "Check its advertised URL and the Sandbox manager host allowlist in Admin Settings."
        )
    if not url:
        return (
            "The owning host has not advertised a sandbox manager URL. Configure its "
            "advertised URL and send a new host heartbeat before retrying deletion."
        )
    if not (setting_env(ENV_SANDBOX_MANAGER_TOKEN) or "").strip():
        return (
            "The control plane has no sandbox manager credential. Configure Sandbox manager "
            "credential in Admin Settings to match the owning host's sandbox manager."
        )
    return None


#: `{host: (url, pool)}` — one connection pool per enrolled host. A sandbox manager
#: client is built per request — one per file read, one per terminal keystroke
#: — so a pool on the instance would be a pool of one; it has to outlive the
#: object that uses it.
#:
#: **Keyed by host, not by URL.** A heartbeat overwrites the address
#: (`host_pool.py`: `sandbox_manager_url = EXCLUDED.sandbox_manager_url`), so a rebuilt machine
#: that comes back on a different one would leave its old pool in a URL-keyed
#: dictionary for the life of the process — and with
#: `AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS` unset, `validate_sandbox_manager_url` accepts any
#: host, which makes the URL a value the far side chooses. Growth keyed on
#: that is not growth this deployment controls. The host set is. This mirrors
#: `_PROBE_CACHE` below, which keys by host and re-checks when the URL moves,
#: for the same reason.
#:
#: **This is not the process-global fallback the module docstring refuses.**
#: That rule is about *which* sandbox manager a request reaches, and the host row still
#: decides that: nothing is looked up here without a URL that resolution
#: already produced. What this removes is the TCP connect in front of a sandbox manager
#: the row had already named.
_POOLS: dict[str, tuple[str, httpx.AsyncClient]] = {}


#: Closes in flight for pools a moved host left behind. Held so the garbage
#: collector cannot cancel one part-way; discarded as each finishes.
_RETIRING: set[asyncio.Task[None]] = set()


def _retire(pool: httpx.AsyncClient) -> None:
    """Close a pool its host moved away from, without blocking the caller.

    Nothing else here needs awaiting, and making the factory async just for
    this would push ``await`` into every caller — and into every test that
    substitutes a stub for it. An idle httpx pool does not expire its own
    sockets until it is used again, so a replaced pool that is never closed
    keeps them until the process ends; this is what stops that.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - every caller is async today
        return
    task = loop.create_task(pool.aclose())
    _RETIRING.add(task)
    task.add_done_callback(_RETIRING.discard)


def _pool_for(host: str, url: str) -> httpx.AsyncClient:
    """Return this host's pool, replacing it when the host moved."""
    existing = _POOLS.get(host)
    if existing is not None:
        cached_url, pool = existing
        if cached_url == url and not pool.is_closed:
            return pool
        # The address changed, or shutdown closed the pool under us. Either
        # way the old one is unusable and must not be left holding sockets.
        del _POOLS[host]
        _retire(pool)
    # No default timeout: every borrowed call passes its own, because the
    # deadlines differ per operation and one client cannot carry them all.
    pool = httpx.AsyncClient()
    _POOLS[host] = (url, pool)
    return pool


async def close_sandbox_manager_clients() -> None:
    """Close every sandbox manager pool this process opened; called at shutdown.

    Failures are swallowed deliberately: this runs while the application is
    already going away, and a sandbox manager that has stopped answering must not turn
    shutdown into a traceback.
    """
    retiring = list(_RETIRING)
    pools = [pool for _, pool in _POOLS.values()]
    _POOLS.clear()
    for pool in pools:
        with contextlib.suppress(Exception):
            await pool.aclose()
    if retiring:
        await asyncio.gather(*retiring, return_exceptions=True)


def _client_for(host: dict[str, Any]) -> SandboxManagerClient | None:
    """Apply current address policy before lending a credential or cached pool."""
    name = str(host.get("host") or "").strip()
    try:
        url = validate_sandbox_manager_url(host.get("sandbox_manager_url"))
    except SandboxManagerUrlRefused:
        # Host rows can predate an administrator narrowing the allowlist.
        # Refusing new clients is not enough if a pooled connection remains
        # available for that same host; retire it and its cached health proof.
        existing = _POOLS.pop(name, None)
        if existing is not None:
            _retire(existing[1])
        _PROBE_CACHE.pop(name, None)
        logger.warning(
            "agent_sandbox_manager_policy_refused",
            extra={"event": "agent_sandbox_manager_policy_refused", "host": name},
        )
        return None
    if not url:
        return None
    token = (setting_env(ENV_SANDBOX_MANAGER_TOKEN) or "").strip()
    if not token:
        return None
    if not name:
        # Nothing to bound a pool by, so this row does not get one: a
        # connection per call is what it had before, and that is better than a
        # dictionary keyed on whatever happened to be in the row.
        return SandboxManagerClient(url, token)
    return SandboxManagerClient(url, token, client=_pool_for(name, url))


def sandbox_manager_for_host(host: dict[str, Any]) -> SandboxManagerClient | None:
    """Reach a registered Host for administrative readiness and inventory checks."""
    return _client_for(host)


async def sandbox_manager_for_attempt(
    store: HostPoolRepository | None, attempt_id: int | None
) -> SandboxManagerClient | None:
    """Resolve the enrolled host sandbox manager holding ``attempt_id``'s container."""
    if store is not None and attempt_id is not None:
        host = await store.host_for_attempt(attempt_id=attempt_id)
        if host is not None:
            return _client_for(host)
    return None


async def sandbox_manager_for_job(
    store: HostPoolRepository | None, job_id: str | None
) -> SandboxManagerClient | None:
    """Resolve the sandbox manager for a job, via its current attempt's host."""
    if store is not None and job_id:
        host = await store.host_for_job(job_id=job_id)
        if host is not None:
            return _client_for(host)
    return None


async def hydrate_shared_sandbox_manager(
    job: dict[str, Any], sandbox_manager: SandboxManagerClient | None
) -> SandboxManagerClient | None:
    """Restore a shared group's aliases before a generic workspace request.

    The sandbox manager can reconstruct a Docker group from immutable labels after a
    restart, but forked thread ids are database-owned aliases.  Re-registering
    the current bounded manifest makes Files/Terminal/Git work for a child even
    when no subsequent Agent turn has happened yet.
    """
    sandbox = job.get("thread_sandbox")
    if not isinstance(sandbox, dict) or sandbox.get("shared_live") is not True:
        return None
    if sandbox_manager is not None:
        await sandbox_manager.register_shared_sandbox(sandbox)
    return sandbox_manager


#: How long a probe result is reused before the next heartbeat re-checks.
#: Heartbeats arrive every 30s per host; probing each one would make the
#: control plane's liveness path depend on a second service's, and probing only
#: at enrolment would never notice a token that was already wrong when the host
#: enrolled — which is how the first remote host was misconfigured.
SANDBOX_MANAGER_PROBE_INTERVAL_S = 60.0

#: `{host: (monotonic_deadline, url_probed)}`. In-process and deliberately not
#: persisted: a control plane that restarts should re-probe, and the answer is
#: about right now rather than about history.
_PROBE_CACHE: dict[str, tuple[float, str]] = {}

#: Probe answers about this deployment's own configuration. They say nothing
#: about what the advertised address is or what answers there.
PROBE_NO_URL = "host did not advertise a sandbox manager URL"
PROBE_NO_CREDENTIAL = "control plane has no sandbox manager credential"
PROBE_REFUSED_BY_POLICY = "sandbox manager is unavailable under the current configuration"
#: What a host is told about every other probe failure.
PROBE_FAILED_FOR_HOST = (
    "the control plane could not reach or authenticate to the advertised sandbox manager; "
    "the control-plane log has the detail"
)


def probe_error_for_host(error: str | None) -> str | None:
    """Reduce a probe failure to what the host that advertised the address may see.

    The full reason names the dial error, or quotes the ``detail`` of whatever
    answered at the address. Handed back to the host that chose the address,
    it would make the control plane a reachability and content oracle for its
    own network; the operator reads it in the control-plane log instead.
    Answers about this deployment's configuration pass through unchanged.
    """
    if error is None or error in (PROBE_NO_URL, PROBE_NO_CREDENTIAL, PROBE_REFUSED_BY_POLICY):
        return error
    return PROBE_FAILED_FOR_HOST


async def probe_sandbox_manager(
    host: str, sandbox_manager_url: str | None, *, force: bool = False
) -> str | None:
    """Check that this deployment can actually use ``sandbox_manager_url``.

    Returns ``None`` when it can, or a short reason when it cannot.

    The reason this exists: a runner reports the address *it* uses, and until
    something tries it from here, nothing knows whether it means anything on
    this machine. On the first remote host it did not — a compose service name
    that resolved nowhere — and the way that surfaced was a job failing at its
    first terminal call, three attempts, six minutes, with `503` as the only
    clue. The same probe also catches a sandbox manager token that does not match,
    because `/health` is authenticated.

    Re-probes at most every ``SANDBOX_MANAGER_PROBE_INTERVAL_S`` per host, and always
    when the reported URL changes — a changed address is exactly when the
    answer is most likely to be different.
    """
    url = (sandbox_manager_url or "").strip()
    if not url:
        return PROBE_NO_URL

    # Revalidate even a cached successful probe after a live policy change.
    client = _client_for({"host": host, "sandbox_manager_url": url})
    if client is None:
        if not (setting_env(ENV_SANDBOX_MANAGER_TOKEN) or "").strip():
            return PROBE_NO_CREDENTIAL
        return PROBE_REFUSED_BY_POLICY

    now = time.monotonic()
    cached = _PROBE_CACHE.get(host)
    if not force and cached is not None and cached[0] > now and cached[1] == url:
        return None

    try:
        await client.health()
    except SandboxManagerError as exc:
        _PROBE_CACHE.pop(host, None)
        return f"{exc.message} ({url})"
    except Exception as exc:  # pragma: no cover - defensive
        _PROBE_CACHE.pop(host, None)
        return f"{type(exc).__name__}: {exc} ({url})"

    _PROBE_CACHE[host] = (now + SANDBOX_MANAGER_PROBE_INTERVAL_S, url)
    return None
