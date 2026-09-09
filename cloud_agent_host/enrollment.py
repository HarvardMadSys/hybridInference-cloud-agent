"""Joining the pool, and staying in it.

The control plane has had `/v1/agent/host/register` and
`/v1/agent/host/heartbeat` since phase G. Nothing on this side called either,
so every consequence that hangs off a host row was inert:

* **capacity** — `slots_total` was never reported, so the scheduler had no
  number to respect and a host took work without limit;
* **offline** — the reaper moves hosts that stop heartbeating to `offline`,
  which with no heartbeats meant either every host or none, depending on
  whether a row existed at all;
* **drain and remove** — an operator's decision the runner never read;
* **broker address** — the row carries the `broker_url` from which file,
  terminal and git requests are served, and a row without one answers 409 for
  every one of them.

**The credential is per host.** It opens heartbeat and claim for exactly one
machine, and removing the host revokes it. There is no pool-wide claim secret.

Enrollment is a one-time token spent for a lasting credential. A machine being
set up has none yet, which is the whole reason the exchange exists; the token
names the host it may register as, so a typo cannot revoke a live machine's
credential.

**The credential is written to a file, never logged and never passed in
`argv`.** `/proc/<pid>/cmdline` is world-readable, and this host runs untrusted
repository code next door.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cloud_agent_host.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = get_logger(__name__)

ENV_ENROLL_TOKEN = "AGENT_ENROLL_TOKEN"
ENV_HOST_CREDENTIAL = "AGENT_HOST_CREDENTIAL"
ENV_CREDENTIAL_FILE = "AGENT_HOST_CREDENTIAL_FILE"

#: Where a credential is kept when the deployment names no path. Host
#: infrastructure state is the only host bind in the retained topology.
DEFAULT_CREDENTIAL_FILE = "/var/lib/hybridinference/agent-host-credential"

#: Fallback cadence, used only until a heartbeat answers. The control plane
#: tells us its own interval, and following that rather than a local constant
#: is what keeps "missed heartbeats" meaning the same thing on both sides.
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0

#: A heartbeat is a small write; something is wrong long before this expires.
_TIMEOUT_S = 15.0

#: The one status in which this runner may ask for work — a **mirror** of
#: ``cloud_agent.host_pool.CLAIMABLE_STATUSES``, which the control plane
#: enforces and the schema's CHECK constraint bounds.
#:
#: Mirrored rather than imported because this package runs on the runner host,
#: which has no control-plane code and no database. That makes drift possible,
#: so ``test_host_enrollment.py`` asserts the two agree — and it exists because
#: this constant was once ``"active"``, a status the schema forbids outright.
#: Nothing caught it: every test built its own ``HostIdentity`` with that same
#: invented value, so the fixtures agreed with the bug rather than with the
#: server.
CLAIMABLE_STATUSES: tuple[str, ...] = ("online",)

#: What a host with a stored credential assumes about itself before its first
#: heartbeat answers. ``online`` matches the schema's column default, so the
#: assumption agrees with what the control plane will say about a row that
#: predates enrollment.
#:
#: Optimistic on purpose, and safe because the control plane gates claims on
#: the same status independently (``AgentJobStore.claim_job``): the worst a
#: wrong guess costs is one refused claim, while guessing ``enrolling`` would
#: idle every restarted host for a whole heartbeat interval.
DEFAULT_STATUS = "online"


class EnrollmentError(RuntimeError):
    """This machine cannot join, or cannot stay in, the pool."""


class HostRemoved(EnrollmentError):
    """The control plane no longer recognises this host.

    Distinct from a network failure on purpose: a removed host must stop
    claiming, while an unreachable control plane means keep the work already
    in flight and try again.
    """


@dataclass
class HostIdentity:
    """What this machine is in the pool, and what it authenticates with."""

    host: str
    credential: str
    status: str = DEFAULT_STATUS
    #: Seconds between heartbeats, as the control plane last reported them.
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    slots_total: int | None = None
    capabilities: list[str] = field(default_factory=list)


def credential_path() -> Path:
    """Where this deployment keeps its host credential."""
    return Path(os.environ.get(ENV_CREDENTIAL_FILE) or DEFAULT_CREDENTIAL_FILE)


def load_credential() -> str:
    """Return the stored credential, or an empty string.

    The environment wins over the file, so a deployment that injects the
    credential by another mechanism needs no file at all.
    """
    from_env = (os.environ.get(ENV_HOST_CREDENTIAL) or "").strip()
    if from_env:
        return from_env
    path = credential_path()
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def store_credential(credential: str) -> None:
    """Persist a credential for the next start.

    Mode 0600 before anything is written, not after: a credential that exists
    world-readable for even an instant on a machine running untrusted code has
    been readable.
    """
    path = credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create then replace, so a concurrent reader never sees a
    # half-written file and no umask can widen the mode.
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, credential.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _post(url: str, body: dict[str, Any], *, bearer: str = "") -> dict[str, Any]:
    """POST JSON and return the parsed answer.

    Raises:
        HostRemoved: On 401/403 — the credential is not, or is no longer, one
            this control plane accepts.
        EnrollmentError: On anything else.
    """
    payload = json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if bearer:
        request.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace")
        if exc.code in (401, 403):
            raise HostRemoved(f"{exc.code} from {url}: {detail}") from exc
        raise EnrollmentError(f"{exc.code} from {url}: {detail}") from exc
    # `OSError`, not `urllib.error.URLError`. urlopen wraps failures it meets
    # while *establishing* a connection, but a peer that resets an already-open
    # socket surfaces raw from http.client while the response status line is
    # being read:
    #
    #     File ".../http/client.py", line 297, in _read_status
    #     ConnectionResetError: [Errno 104] Connection reset by peer
    #
    # which is an OSError and is not a URLError. It escaped this wrapper, and
    # therefore escaped the caller's `except EnrollmentError` — killing the
    # heartbeat thread outright on 2026-08-06, the moment a deploy restarted the
    # control plane. The runner went on claiming work perfectly well while the
    # admin page showed the host offline for as long as the process lived.
    # URLError and TimeoutError are both OSError subclasses, so this is wider
    # than what it replaces rather than different from it.
    except (OSError, ValueError) as exc:
        raise EnrollmentError(f"{url} unreachable: {exc}") from exc


def register(
    *,
    base_url: str,
    host: str,
    enroll_token: str,
    broker_url: str | None = None,
    slots_total: int | None = None,
    capabilities: list[str] | None = None,
    runner_version: str | None = None,
) -> HostIdentity:
    """Spend an enrollment token for this machine's lasting credential."""
    answer = _post(
        f"{base_url.rstrip('/')}/v1/agent/host/register",
        {
            "enroll_token": enroll_token,
            "name": host,
            "broker_url": broker_url,
            "slots_total": slots_total,
            "capabilities": capabilities or [],
            "runner_version": runner_version,
        },
    )
    credential = str(answer.get("credential") or "")
    if not credential:
        raise EnrollmentError("registration returned no credential")
    identity = HostIdentity(
        host=str(answer.get("host") or host),
        credential=credential,
        # A registration that answers without a status is a control plane
        # older than this runner. `enrolling` is what it would have said —
        # registration issues the credential, the first heartbeat is what
        # promotes the host — so the fallback claims nothing until that
        # heartbeat confirms it, rather than assuming its way into the pool.
        status=str(answer.get("status") or "enrolling"),
    )
    store_credential(credential)
    _warn_broker_error(answer, identity.host)
    logger.info(
        "agent_host_registered",
        extra={"event": "agent_host_registered", "host": identity.host, "status": identity.status},
    )
    return identity


def _warn_broker_error(answer: dict[str, Any], host: str) -> None:
    """Surface the control plane's verdict on the address we just reported.

    It is the only party that can answer "is this address usable", because it
    is the one that will dial it — and it is on another machine, which is
    exactly when the answer stops being obvious. Printed to stderr as well as
    logged because `sandbox_host.sh up` tails these logs after starting
    the stack: a deploy that misconfigured the reverse tunnel or the shared
    broker token says so while somebody is still watching, instead of at the
    first terminal request of the first job.
    """
    error = answer.get("broker_error")
    if not error:
        return
    logger.warning(
        "agent_host_broker_unusable",
        extra={"event": "agent_host_broker_unusable", "host": host, "broker_error": error},
    )
    print(
        f"agent runner: the control plane cannot use this host's broker address: {error}\n"
        "  Files, terminals and git for every job on this host will fail until this is fixed.\n"
        "  On a remote runner host, check AGENT_WORKSPACE_BROKER_ADVERTISE_URL, the reverse\n"
        "  forward on the control-plane tunnel, and that AGENT_WORKSPACE_BROKER_TOKEN matches\n"
        "  the control plane's.",
        file=sys.stderr,
        flush=True,
    )


def heartbeat(
    *,
    base_url: str,
    identity: HostIdentity,
    slots_total: int | None = None,
    broker_url: str | None = None,
    capabilities: list[str] | None = None,
    runner_version: str | None = None,
    sandbox_runtime: str | None = None,
    sandbox_image_digest: str | None = None,
    runtime_versions: dict[str, str] | None = None,
    runtime_versions_available: dict[str, list[str]] | None = None,
    residencies: list[dict[str, Any]] | None = None,
    retained_sandboxes: list[dict[str, Any]] | None = None,
    on_residencies_lost: Callable[[list[str]], None] | None = None,
    on_retained_sandboxes_stop: Callable[[list[dict[str, Any]]], None] | None = None,
) -> HostIdentity:
    """Report liveness and capacity, and read back what the pool decided.

    The answer carries the operator's side of the conversation — ``draining``,
    ``offline``, a changed interval — which is the only way a runner learns
    that a human asked it to stop taking work.

    The version stamp travels with every beat (issue #54): ``sandbox_runtime``
    (``""`` = shared-kernel runc, a positive statement), the sandbox image
    digest, and the CLI versions baked into that image. ``None`` means "not
    reported" and leaves the control plane's last knowledge in place.

    ``runtime_versions`` is the *default* per runtime and
    ``runtime_versions_available`` the whole menu this image holds. The admin
    page offers the menu and the claim validates a stored selection against it,
    so a host that stops reporting a version stops being asked to run it.
    """
    answer = _post(
        f"{base_url.rstrip('/')}/v1/agent/host/heartbeat",
        {
            "slots_total": slots_total,
            "broker_url": broker_url,
            "capabilities": capabilities,
            "runner_version": runner_version,
            "sandbox_runtime": sandbox_runtime,
            "sandbox_image_digest": sandbox_image_digest,
            "runtime_versions": runtime_versions,
            "runtime_versions_available": runtime_versions_available,
            "residencies": residencies,
            "retained_sandboxes": retained_sandboxes,
        },
        bearer=identity.credential,
    )
    _warn_broker_error(answer, identity.host)
    lost = answer.get("residencies_lost")
    if lost and on_residencies_lost is not None:
        # The control plane's side of the fence, delivered with the
        # generation each verdict is about: the pool retires an entry only on
        # an exact (thread, generation) match, so a late answer to an old
        # beat cannot end a successor residency.
        on_residencies_lost([item for item in lost if isinstance(item, dict)])
    stop_retained = answer.get("retained_sandboxes_stop")
    if stop_retained and on_retained_sandboxes_stop is not None:
        on_retained_sandboxes_stop([item for item in stop_retained if isinstance(item, dict)])
    identity.status = str(answer.get("status") or identity.status)
    interval = answer.get("heartbeat_interval_seconds")
    if isinstance(interval, (int, float)) and interval > 0:
        identity.interval_s = float(interval)
    reported = answer.get("slots_total")
    identity.slots_total = int(reported) if isinstance(reported, int) else identity.slots_total
    return identity


def ensure_enrolled(
    *,
    base_url: str,
    host: str,
    broker_url: str | None = None,
    slots_total: int | None = None,
    capabilities: list[str] | None = None,
    runner_version: str | None = None,
) -> HostIdentity | None:
    """Return this machine's identity, enrolling if it has a token and no credential.

    ``None`` means this deployment does not use the pool — neither a stored
    credential nor an enrollment token — which is the single-host shape that
    worked before phase G and must keep working. Enrolment is opt-in; a runner
    that has never heard of it is not broken.

    **Under a lock, because a host runs several runner processes.** The compose
    file starts three replicas from one machine, sharing one credential file,
    one host name and one enrollment token. Unlocked, all three read no
    credential and all three register: the control plane spends the token for
    exactly one of them (the `consumed_at IS NULL` predicate in
    ``consume_enroll_token``), so the other two fail and crash-loop until one
    of the restarts happens to read the file the winner wrote. It settles, but
    "a new host crash-loops on first start" is not something to make an
    operator recognise. The losers wait here and find the credential instead.
    """
    with _enrollment_lock():
        return _enroll_once(
            base_url=base_url,
            host=host,
            broker_url=broker_url,
            slots_total=slots_total,
            capabilities=capabilities,
            runner_version=runner_version,
        )


@contextmanager
def _enrollment_lock() -> Iterator[None]:
    """Serialize load-or-register across every runner process on this machine.

    On the credential's own directory, which is the volume all replicas share
    — a lock anywhere else would be per-container and would serialize nothing.
    A deployment whose lock file cannot be created (a read-only path, an
    unusual mount) proceeds unlocked rather than refusing to start: the
    unlocked path is what this code did before, and the control plane still
    settles the race correctly.
    """
    path = credential_path().parent / ".enroll.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w")
    except OSError:
        yield
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def _enroll_once(
    *,
    base_url: str,
    host: str,
    broker_url: str | None,
    slots_total: int | None,
    capabilities: list[str] | None,
    runner_version: str | None,
) -> HostIdentity | None:
    """The body of :func:`ensure_enrolled`, run holding the lock."""
    credential = load_credential()
    if credential:
        return HostIdentity(host=host, credential=credential)

    token = (os.environ.get(ENV_ENROLL_TOKEN) or "").strip()
    if not token:
        return None

    return register(
        base_url=base_url,
        host=host,
        enroll_token=token,
        broker_url=broker_url,
        slots_total=slots_total,
        capabilities=capabilities,
        runner_version=runner_version,
    )


class HostHeartbeat:
    """Heartbeats on a daemon thread for as long as the runner is up.

    Modelled on the per-job lease heartbeat that already exists, and separate
    from it because they answer different questions: that one says "this
    attempt is still mine", this one says "this machine is still here". A host
    with no jobs must still report, or the reaper marks it offline and stops
    sending it work — which is a pool that empties whenever it is idle.
    """

    def __init__(
        self,
        *,
        base_url: str,
        identity: HostIdentity,
        slots_total: int | None = None,
        broker_url: str | None = None,
        capabilities: list[str] | Callable[[], list[str]] | None = None,
        runner_version: str | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: dict[str, list[str]] | None = None,
        residency_beats: Callable[[], list[dict[str, Any]]] | None = None,
        retained_sandbox_beats: Callable[[], list[dict[str, Any]] | None] | None = None,
        on_residencies_lost: Callable[[list[str]], None] | None = None,
        on_retained_sandboxes_stop: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> None:
        self._base_url = base_url
        self._identity = identity
        self._slots_total = slots_total
        self._broker_url = broker_url
        self._capabilities = capabilities
        self._runner_version = runner_version
        self._sandbox_runtime = sandbox_runtime
        self._sandbox_image_digest = sandbox_image_digest
        self._runtime_versions = runtime_versions
        self._runtime_versions_available = runtime_versions_available
        # Freshly asked on every beat — a static list would report sandboxes
        # the pool has already torn down.
        self._residency_beats = residency_beats
        self._retained_sandbox_beats = retained_sandbox_beats
        self._on_residencies_lost = on_residencies_lost
        self._on_retained_sandboxes_stop = on_retained_sandboxes_stop
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Set when the control plane says this host is gone. The runner reads
        #: it to stop claiming; it does not abandon work already in flight,
        #: because a job in progress has its own lease and its own fence.
        self.removed = threading.Event()

    @property
    def status(self) -> str:
        """What the pool last said about this host."""
        return self._identity.status

    @property
    def credential(self) -> str:
        """This host's own claim credential, naming the machine.

        What the runner presents when claiming work and heartbeating
        once enrolled. The control plane reads the host from this rather than
        from the request body, so a compromised machine can drain the queue
        but cannot claim as its neighbour — which is the whole reason drain,
        pin and revoke are per-host decisions.
        """
        return self._identity.credential

    @property
    def claimable(self) -> bool:
        """Whether this runner should ask for more work.

        `draining` is an operator saying "finish what you have and stop", so
        it is not an error and nothing is interrupted — but a runner that kept
        claiming through it would make draining mean nothing. `enrolling` and
        `offline` have not proved the machine is there, and `disabled` has had
        its credential taken away.
        """
        return not self.removed.is_set() and self._identity.status in CLAIMABLE_STATUSES

    def start(self) -> None:
        """Begin heartbeating."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="agent-host-heartbeat")
        self._thread.start()

    def stop(self) -> None:
        """Stop heartbeating and join."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                capabilities = (
                    self._capabilities() if callable(self._capabilities) else self._capabilities
                )
                heartbeat(
                    base_url=self._base_url,
                    identity=self._identity,
                    slots_total=self._slots_total,
                    broker_url=self._broker_url,
                    capabilities=capabilities,
                    runner_version=self._runner_version,
                    sandbox_runtime=self._sandbox_runtime,
                    sandbox_image_digest=self._sandbox_image_digest,
                    runtime_versions=self._runtime_versions,
                    runtime_versions_available=self._runtime_versions_available,
                    residencies=(
                        self._residency_beats() if self._residency_beats is not None else None
                    ),
                    retained_sandboxes=(
                        self._retained_sandbox_beats()
                        if self._retained_sandbox_beats is not None
                        else None
                    ),
                    on_residencies_lost=self._on_residencies_lost,
                    on_retained_sandboxes_stop=self._on_retained_sandboxes_stop,
                )
            except HostRemoved:
                # Definitive: this credential is not one the control plane
                # accepts. Stop claiming and stop asking — retrying a refusal
                # that names us cannot succeed, and the log line is what an
                # operator will look for after removing a host.
                self.removed.set()
                logger.warning(
                    "agent_host_credential_rejected",
                    extra={
                        "event": "agent_host_credential_rejected",
                        "host": self._identity.host,
                    },
                )
                return
            except EnrollmentError:
                # A blip, an outage, a restart. Keep the host and keep trying:
                # the control plane's own reaper decides when silence has gone
                # on long enough, and that decision is not ours to duplicate.
                logger.warning(
                    "agent_host_heartbeat_failed",
                    exc_info=True,
                    extra={"event": "agent_host_heartbeat_failed", "host": self._identity.host},
                )
            except Exception:
                # Nothing gets to kill this thread. The `EnrollmentError` arm
                # above is the intended path and stays the documented one; this
                # exists because an exception type nobody anticipated reaching
                # here already did, and the cost was silent and lasting: a
                # thread that dies is never restarted, so the host reports
                # offline for the life of the process while claiming work
                # normally the whole time. An operator reading that page sees a
                # dead machine that is not dead, which is worse than an error.
                #
                # Deliberately after `HostRemoved`, which must still stop.
                logger.exception(
                    "agent_host_heartbeat_crashed",
                    extra={"event": "agent_host_heartbeat_crashed", "host": self._identity.host},
                )
            self._stop.wait(self._identity.interval_s)


def sleep_until(deadline: float) -> None:  # pragma: no cover - trivial
    """Sleep to an absolute time, ignoring how long the caller took."""
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
