"""The host service's own control-plane calls, and the client they keep alive.

A claim, the wait between claims and a turn handed back are the host's calls,
made from its claim loop under the enrolled host's credential or the turn's
own token before any child runs the turn. The attempt-scoped calls a turn makes
are :mod:`cloud_agent_host.control_plane_client`'s.

Each of these used to open a connection, and build an SSL context, for itself:
the claim, the wait and the environment-test claim made three in every idle
cycle. The claim loop now holds one :func:`keepalive_client` for them, and the
environment-test worker one of its own.
"""

from __future__ import annotations

from typing import Any

import httpx

from cloud_agent_host.claimed_job import ClaimUnreachable, WorkWaitUnsupported
from cloud_agent_host.control_plane_client import _log_control_failure
from cloud_agent_host.logging_utils import get_logger

logger = get_logger(__name__)

#: Added to a wait's own timeout for the client's, so the control plane always
#: answers first.
_WORK_WAIT_GRACE_S = 10.0
#: The control plane's uvicorn closes a connection after five idle seconds
#: (``--timeout-keep-alive`` at its default, which
#: ``deploy/control-plane/Dockerfile`` leaves), and a request sent on a
#: connection as the server closes it is lost with it. Kept a second shorter,
#: it is always this side that lets a connection go. The sizes are httpx's own.
_CONTROL_PLANE_LIMITS = httpx.Limits(
    max_connections=100, max_keepalive_connections=20, keepalive_expiry=4.0
)


def keepalive_client(**kwargs: Any) -> httpx.Client:
    """A client for the host's own control-plane calls that keeps its connection."""
    return httpx.Client(limits=_CONTROL_PLANE_LIMITS, **kwargs)


def _post(client: httpx.Client | None, url: str, **kwargs: Any) -> httpx.Response:
    """POST on ``client``, or on a connection opened for this request alone."""
    if client is None:
        return httpx.post(url, **kwargs)
    return client.post(url, **kwargs)


def claim_payload(
    *,
    base_url: str,
    host_credential: str,
    worker_id: str,
    lease_ttl: float,
    attestation: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Claim the next queued job and return the control plane's raw answer.

    The host service hands this document to the child that will run the turn,
    unread beyond the few fields it needs to account for the claim. Parsing it
    into a :class:`ClaimedJob` is the child's job, in the process that will
    actually use the credentials it carries.

    ``client`` is the host service's keep-alive client; without one the claim
    opens a connection for itself.
    """
    payload: dict[str, Any] = {"worker_id": worker_id, "lease_ttl_seconds": lease_ttl}
    if attestation:
        payload["attestation"] = attestation
    try:
        response = _post(
            client,
            f"{base_url.rstrip('/')}/v1/agent/worker/claim",
            json=payload,
            headers={"Authorization": f"Bearer {host_credential}"},
            timeout=30.0,
        )
    except httpx.TransportError as exc:
        # Tagged at the one place where "the gateway is unreachable" also means
        # "no job was claimed". The same error from a later call — an event, an
        # group snapshot, a terminal transition — happens with an attempt already
        # running, and reporting *that* as an unreached gateway would hide the
        # job an operator has to go look at.
        raise ClaimUnreachable(str(exc)) from exc
    response.raise_for_status()
    body = response.json()
    return body if body else None


def wait_for_work(
    *,
    base_url: str,
    host_credential: str,
    since: str | None,
    timeout_s: float,
    client: httpx.Client | None = None,
) -> str:
    """Wait until the control plane says there may be work, and return its token.

    Answers at once when ``since`` is stale and at the latest after
    ``timeout_s``; the host claims after either. Raises
    :class:`WorkWaitUnsupported` for a control plane without the endpoint.
    ``client`` is the one the claims use, so the claim after a wait goes out
    on the connection the wait held open.
    """
    try:
        response = _post(
            client,
            f"{base_url.rstrip('/')}/v1/agent/worker/wait",
            json={"since": since, "timeout_seconds": timeout_s},
            headers={"Authorization": f"Bearer {host_credential}"},
            timeout=timeout_s + _WORK_WAIT_GRACE_S,
        )
    except httpx.TransportError as exc:
        raise ClaimUnreachable(str(exc)) from exc
    if response.status_code == 404:
        raise WorkWaitUnsupported(response.text[:200])
    response.raise_for_status()
    return str(response.json()["token"])


def release_turn(
    *,
    base_url: str,
    worker_token: str,
    job_id: str,
    reason: str,
    client: httpx.Client | None = None,
) -> bool:
    """Hand a claimed turn back to the queue, having proved it never began.

    Called only where this host can show the turn was not started — the child
    could not be spawned, or exited without taking delivery. The control plane
    checks the same thing from its own side and refuses otherwise, so a host
    that is wrong about it cannot requeue work that ran.

    Returns whether the turn went back. ``False`` is not an error path worth
    retrying: the lease is the backstop it always was, and a turn this call
    could not release is one the control plane has already decided about.
    ``client`` is the host service's keep-alive client, which sends this
    turn's own token with this request alone.
    """
    try:
        response = _post(
            client,
            f"{base_url.rstrip('/')}/v1/agent/worker/jobs/{job_id}/release",
            json={"reason": reason},
            headers={"Authorization": f"Bearer {worker_token}"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        _log_control_failure("release", job_id, exc)
        return False
    if response.status_code >= 400:
        logger.warning(
            "agent_turn_release_refused",
            extra={
                "event": "agent_turn_release_refused",
                "job_id": job_id,
                "status": response.status_code,
            },
        )
        return False
    return True
