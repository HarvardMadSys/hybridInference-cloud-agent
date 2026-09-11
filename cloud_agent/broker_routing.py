"""Resolve the workspace broker that owns a retained chat group.

Every executable attempt belongs to an enrolled host. The attempt records that
host and the host heartbeat records its broker URL, so files, terminals, Git,
fork, snapshots, and native-session operations all route through the same
durable ownership record. There is deliberately no process-global fallback:
using one would make a missing host fence silently address another machine's
container.

**The token is still global.** Host rows carry a URL, not a credential; every
broker in the pool authenticates the same `AGENT_WORKSPACE_BROKER_TOKEN`. That
is what the phase-G task asked for and no more: per-host broker credentials are
a secret-distribution design (issue one at enrollment, rotate it, get it onto
the machine) rather than a routing change, and doing half of it here would look
like the problem was solved. Until then, a compromised host's broker token
opens every other host's broker.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from cloud_agent_host.workspace_broker_client import (
    WorkspaceBrokerClient,
    WorkspaceBrokerError,
)

if TYPE_CHECKING:
    from cloud_agent.ports import HostPoolRepository

#: Hosts a broker URL may name. Empty means "any", which is the shape a
#: single-host deployment runs in and the shape this had before the check.
ENV_BROKER_HOSTS = "AGENT_BROKER_URL_ALLOWED_HOSTS"


class BrokerUrlRefused(ValueError):
    """A host reported a broker address this deployment will not talk to."""


def validate_broker_url(url: str | None) -> str | None:
    """Return ``url`` if this deployment may send its broker token there.

    **A host proposes its own broker address, and the control plane then sends
    the pool's shared token to it.** So an address accepted here is an address
    that receives a credential good for every broker in the pool — a
    compromised host, or one with a typo, could name anything and be handed it.

    Two checks, both structural:

    * **Scheme.** `http`/`https` only. A `file:` or `gopher:` URL is not a
      broker, and the client would do something surprising with it.
    * **Host allowlist**, when configured. Names, not patterns: a pattern is a
      thing to get subtly wrong, and the set of machines in a pool is small
      and known. Unconfigured means any host, which is what this did before —
      the check is opt-in so that turning it on is a deployment's decision
      rather than something that breaks every existing pool on upgrade.

    Raises:
        BrokerUrlRefused: If the URL is unusable or names a host outside the
            allowlist. Refused rather than dropped: a host whose address is
            ignored looks enrolled and silently never serves a workspace.
    """
    if url is None or not url.strip():
        return None
    candidate = url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BrokerUrlRefused(f"broker_url must be an http(s) URL, got {candidate!r}")

    allowed = [
        entry.strip()
        for entry in (os.environ.get(ENV_BROKER_HOSTS) or "").split(",")
        if entry.strip()
    ]
    if allowed and parsed.hostname not in allowed:
        raise BrokerUrlRefused(f"broker_url host {parsed.hostname!r} is not in {ENV_BROKER_HOSTS}")
    return candidate


ENV_BROKER_TOKEN = "AGENT_WORKSPACE_BROKER_TOKEN"


def _client_for(host: dict[str, Any]) -> WorkspaceBrokerClient | None:
    """Build a client for one host row, or ``None`` when it cannot be reached."""
    url = (host.get("broker_url") or "").strip()
    if not url:
        return None
    token = (os.environ.get(ENV_BROKER_TOKEN) or "").strip()
    if not token:
        return None
    return WorkspaceBrokerClient(url, token)


async def broker_for_attempt(
    store: HostPoolRepository | None, attempt_id: int | None
) -> WorkspaceBrokerClient | None:
    """Resolve the enrolled host broker holding ``attempt_id``'s container."""
    if store is not None and attempt_id is not None:
        host = await store.host_for_attempt(attempt_id=attempt_id)
        if host is not None:
            return _client_for(host)
    return None


async def broker_for_job(
    store: HostPoolRepository | None, job_id: str | None
) -> WorkspaceBrokerClient | None:
    """Resolve the broker for a job, via its current attempt's host."""
    if store is not None and job_id:
        host = await store.host_for_job(job_id=job_id)
        if host is not None:
            return _client_for(host)
    return None


async def hydrate_shared_broker(
    job: dict[str, Any], broker: WorkspaceBrokerClient | None
) -> WorkspaceBrokerClient | None:
    """Restore a shared group's aliases before a generic workspace request.

    The broker can reconstruct a Docker group from immutable labels after a
    restart, but forked thread ids are database-owned aliases.  Re-registering
    the current bounded manifest makes Files/Terminal/Git work for a child even
    when no subsequent Agent turn has happened yet.
    """
    sandbox = job.get("thread_sandbox")
    if not isinstance(sandbox, dict) or sandbox.get("shared_live") is not True:
        return None
    if broker is not None:
        await broker.register_shared_sandbox(sandbox)
    return broker


#: How long a probe result is reused before the next heartbeat re-checks.
#: Heartbeats arrive every 30s per host; probing each one would make the
#: control plane's liveness path depend on a second service's, and probing only
#: at enrolment would never notice a token that was already wrong when the host
#: enrolled — which is how the first remote host was misconfigured.
BROKER_PROBE_INTERVAL_S = 60.0

#: `{host: (monotonic_deadline, url_probed)}`. In-process and deliberately not
#: persisted: a control plane that restarts should re-probe, and the answer is
#: about right now rather than about history.
_PROBE_CACHE: dict[str, tuple[float, str]] = {}


async def probe_broker(host: str, broker_url: str | None, *, force: bool = False) -> str | None:
    """Check that this deployment can actually use ``broker_url``.

    Returns ``None`` when it can, or a short reason when it cannot.

    The reason this exists: a runner reports the address *it* uses, and until
    something tries it from here, nothing knows whether it means anything on
    this machine. On the first remote host it did not — a compose service name
    that resolved nowhere — and the way that surfaced was a job failing at its
    first terminal call, three attempts, six minutes, with `503` as the only
    clue. The same probe also catches a broker token that does not match,
    because `/health` is authenticated.

    Re-probes at most every ``BROKER_PROBE_INTERVAL_S`` per host, and always
    when the reported URL changes — a changed address is exactly when the
    answer is most likely to be different.
    """
    url = (broker_url or "").strip()
    if not url:
        return "host did not advertise a workspace broker URL"

    now = time.monotonic()
    cached = _PROBE_CACHE.get(host)
    if not force and cached is not None and cached[0] > now and cached[1] == url:
        return None

    client = _client_for({"broker_url": url})
    if client is None:
        return "control plane has no workspace broker credential"

    try:
        await client.health()
    except WorkspaceBrokerError as exc:
        _PROBE_CACHE.pop(host, None)
        return f"{exc.message} ({url})"
    except Exception as exc:  # pragma: no cover - defensive
        _PROBE_CACHE.pop(host, None)
        return f"{type(exc).__name__}: {exc} ({url})"

    _PROBE_CACHE[host] = (now + BROKER_PROBE_INTERVAL_S, url)
    return None
