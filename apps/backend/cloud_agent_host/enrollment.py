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
* **sandbox manager address** — the row carries the `sandbox_manager_url` from which file,
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
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cloud_agent_host.logging_utils import get_logger
from cloud_agent_shared.host_settings import admin_values
from cloud_agent_shared.sandbox_archive_settings import (
    SandboxArchivePolicy,
    SandboxArchivePolicyError,
)
from cloud_agent_shared.shared_protocol import HOST_LAUNCH_ERROR_MAX_CHARS

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

#: How often the deployment readiness marker is re-read while waiting for the
#: next scheduled beat. It is a small file on a local mount and it changes at
#: most twice per deployment; the poll exists so a rollout does not have to
#: wait out an interval to hear a fact that is already true.
READINESS_MARKER_POLL_S = 1.0

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
#: the same status independently (``PostgresAttemptCommands.claim_job``): the worst a
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
    #: How many turns at once the control plane last asked this host to run —
    #: an operator's number from the admin page — or ``None`` when it has not
    #: said, in which case the host's own default applies. Reset on every
    #: heartbeat: a cleared request must not linger as the old number.
    slots_desired: int | None = None
    capabilities: list[str] = field(default_factory=list)
    #: The operation-profiling policy the control plane returned with the last
    #: heartbeat, or ``None`` when it returned none. Read by the runner's own
    #: policy sync, which is the only thing that writes the collector's store.
    profiling_policy: dict[str, Any] | None = None
    #: The runtime settings an admin set for this host, as the last answered
    #: heartbeat carried them; a setting absent here is the host's own. Replaced
    #: whole on every answer, so a cleared value does not linger, and kept
    #: through a failed beat, so an outage does not revert anything.
    settings: dict[str, int] = field(default_factory=dict)
    #: Complete database-owned archive policy returned by the last answered
    #: heartbeat. ``None`` means the control plane did not provide the contract,
    #: which is distinct from an explicit policy whose ``enabled`` is false.
    sandbox_archive_settings: dict[str, Any] | None = None
    #: The agent CLI versions the control plane wants this host to hold, as the
    #: last answered heartbeat carried them (``runtime_downloads`` targets).
    #: ``None`` means that control plane does not send them, which leaves
    #: downloads as they are rather than treating every target as withdrawn.
    runtime_downloads: list[dict[str, Any]] | None = None


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

    The temporary file is a new one with a name nobody could predict, created
    exclusively and without following a link (``mkstemp``), so nothing left
    at a fixed name, whether a stale file keeping a wider mode or a symlink
    elsewhere, can receive the credential. It is replaced into place whole,
    so a concurrent reader never sees half of it. A directory this creates is
    0700; an existing one is the operator's and is left as it is.
    """
    path = credential_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            os.fchmod(fd, 0o600)
            data = credential.encode()
            while data:
                data = data[os.write(fd, data) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    # The rename is durable only once the directory is: the enrollment token is
    # already spent, and a crash that loses the new entry cannot re-enroll.
    with suppress(OSError):
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


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
    sandbox_manager_url: str | None = None,
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
            "broker_url": sandbox_manager_url,
            "slots_total": slots_total,
            "capabilities": capabilities or [],
            "runner_version": runner_version,
            "labels": {"physical_host": physical_host}
            if (physical_host := os.environ.get("AGENT_PHYSICAL_HOST", "").strip())
            else {},
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
    _warn_sandbox_manager_error(answer, identity.host)
    logger.info(
        "agent_host_registered",
        extra={"event": "agent_host_registered", "host": identity.host, "status": identity.status},
    )
    return identity


def _warn_sandbox_manager_error(answer: dict[str, Any], host: str) -> None:
    """Surface the control plane's verdict on the address we just reported.

    It is the only party that can answer "is this address usable", because it
    is the one that will dial it — and it is on another machine, which is
    exactly when the answer stops being obvious. Printed to stderr as well as
    logged because `sandbox_host.sh up` tails these logs after starting
    the stack: a deploy that misconfigured the reverse tunnel or the shared
    sandbox manager token says so while somebody is still watching, instead of at the
    first terminal request of the first job.
    """
    error = answer.get("sandbox_manager_error", answer.get("broker_error"))
    if not error:
        return
    logger.warning(
        "agent_host_sandbox_manager_unusable",
        extra={
            "event": "agent_host_sandbox_manager_unusable",
            "host": host,
            "sandbox_manager_error": error,
        },
    )
    print(
        f"agent runner: the control plane cannot use this host's sandbox manager address: {error}\n"
        "  Files, terminals and git for every job on this host will fail until this is fixed.\n"
        "  On a remote runner host, check AGENT_SANDBOX_MANAGER_ADVERTISE_URL, the reverse\n"
        "  forward on the control-plane tunnel, and that AGENT_SANDBOX_MANAGER_TOKEN matches\n"
        "  the control plane's.",
        file=sys.stderr,
        flush=True,
    )


def heartbeat(
    *,
    base_url: str,
    identity: HostIdentity,
    slots_total: int | None = None,
    launch_error: str | None = None,
    sandbox_manager_url: str | None = None,
    capabilities: list[str] | None = None,
    runner_version: str | None = None,
    sandbox_runtime: str | None = None,
    sandbox_image_digest: str | None = None,
    runtime_versions: dict[str, str] | None = None,
    runtime_versions_available: dict[str, list[str]] | None = None,
    residencies: list[dict[str, Any]] | None = None,
    retained_sandboxes: list[dict[str, Any]] | None = None,
    capacity: dict[str, Any] | None = None,
    profiling: dict[str, Any] | None = None,
    settings_applied: dict[str, int | float] | None = None,
    settings_local: dict[str, int | float] | None = None,
    sandbox_archive_settings_applied_revision: int | None = None,
    runtime_downloads: list[dict[str, Any]] | None = None,
    on_residencies_lost: Callable[[list[dict[str, Any]]], None] | None = None,
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

    ``capacity`` is this host's own CPU, memory and retained-container
    observation, already stamped with the age it had when the beat was built.
    It is optional in both directions: a host that does not collect it omits
    it, and a control plane that cannot use it drops it without refusing the
    beat. Liveness never depends on a monitoring field.

    ``settings_applied`` and ``settings_local`` report the runtime settings
    this host applies and what its own environment would give it; the answer's
    ``settings`` are the values an admin set for it, kept on the identity.

    ``runtime_downloads`` reports how far this host has got with each agent
    version it was asked to hold; the answer's targets are kept on the identity.
    """
    answer = _post(
        f"{base_url.rstrip('/')}/v1/agent/host/heartbeat",
        {
            "slots_total": slots_total,
            # Trimmed rather than sent whole: the control plane refuses an
            # overlong reason with the beat, and a host that cannot beat goes
            # offline over its own explanation.
            "launch_error": (
                None if launch_error is None else launch_error[:HOST_LAUNCH_ERROR_MAX_CHARS]
            ),
            "broker_url": sandbox_manager_url,
            "capabilities": capabilities,
            "runner_version": runner_version,
            "sandbox_runtime": sandbox_runtime,
            "sandbox_image_digest": sandbox_image_digest,
            "runtime_versions": runtime_versions,
            "runtime_versions_available": runtime_versions_available,
            "residencies": residencies,
            "retained_sandboxes": retained_sandboxes,
            "capacity": capacity,
            "profiling": profiling,
            "settings_applied": settings_applied,
            "settings_local": settings_local,
            "sandbox_archive_settings_applied_revision": (
                sandbox_archive_settings_applied_revision
            ),
            "runtime_downloads": runtime_downloads,
            "physical_host": os.environ.get("AGENT_PHYSICAL_HOST", "").strip() or None,
        },
        bearer=identity.credential,
    )
    _warn_sandbox_manager_error(answer, identity.host)
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
    raw_archive_settings = answer.get("sandbox_archive_settings")
    if raw_archive_settings is None:
        archive_settings: dict[str, Any] | None = None
    elif not isinstance(raw_archive_settings, dict):
        raise EnrollmentError("heartbeat returned invalid sandbox archive settings")
    else:
        try:
            archive_policy = SandboxArchivePolicy.from_mapping(raw_archive_settings)
        except SandboxArchivePolicyError as exc:
            raise EnrollmentError("heartbeat returned invalid sandbox archive settings") from exc
        archive_settings = archive_policy.as_sandbox_manager_dict()
        updated_at = raw_archive_settings.get("updated_at")
        if isinstance(updated_at, str):
            archive_settings["updated_at"] = updated_at

    identity.status = str(answer.get("status") or identity.status)
    interval = answer.get("heartbeat_interval_seconds")
    if isinstance(interval, (int, float)) and interval > 0:
        identity.interval_s = float(interval)
    reported = answer.get("slots_total")
    identity.slots_total = int(reported) if isinstance(reported, int) else identity.slots_total
    desired = answer.get("slots_desired")
    identity.slots_desired = (
        int(desired) if isinstance(desired, int) and not isinstance(desired, bool) else None
    )
    # Checked again here against the bounds this host was built with: a
    # control plane from another release can know a setting, or a bound, that
    # this host does not. A control plane predating settings sends none, which
    # leaves every setting on the host's own .env.
    identity.settings = admin_values(answer.get("settings"))
    identity.sandbox_archive_settings = archive_settings
    # The profiling policy rides this same answer, and the caller applies it to
    # its own process's policy store. Returning it rather than applying it here
    # keeps this module about talking to the control plane: the store belongs to
    # the collector, and the runner is the only thing that should write it.
    policy = answer.get("profiling")
    identity.profiling_policy = policy if isinstance(policy, dict) else None
    targets = answer.get("runtime_downloads")
    identity.runtime_downloads = (
        [item for item in targets if isinstance(item, dict)] if isinstance(targets, list) else None
    )
    return identity


def ensure_enrolled(
    *,
    base_url: str,
    host: str,
    sandbox_manager_url: str | None = None,
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
            sandbox_manager_url=sandbox_manager_url,
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
    sandbox_manager_url: str | None,
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
        sandbox_manager_url=sandbox_manager_url,
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
        slots_total: int | Callable[[], int | None] | None = None,
        launch_error: str | Callable[[], str] | None = None,
        sandbox_manager_url: str | None = None,
        capabilities: list[str] | Callable[[], list[str]] | None = None,
        runner_version: str | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: (
            dict[str, list[str]] | Callable[[], dict[str, list[str]] | None] | None
        ) = None,
        readiness_marker: str | None = None,
        residency_beats: Callable[[], list[dict[str, Any]]] | None = None,
        retained_sandbox_beats: Callable[[], list[dict[str, Any]] | None] | None = None,
        capacity_beats: Callable[[], dict[str, Any] | None] | None = None,
        profiling: Callable[[], dict[str, Any] | None] | None = None,
        settings_report: (
            Callable[[], tuple[dict[str, int | float], dict[str, int | float]]] | None
        ) = None,
        on_settings: Callable[[dict[str, int]], None] | None = None,
        sandbox_archive_settings_report: Callable[[], int | None] | None = None,
        on_sandbox_archive_settings: Callable[[dict[str, Any] | None], None] | None = None,
        runtime_downloads_report: Callable[[], list[dict[str, Any]]] | None = None,
        on_runtime_downloads: Callable[[list[dict[str, Any]] | None], None] | None = None,
        on_residencies_lost: Callable[[list[dict[str, Any]]], None] | None = None,
        on_retained_sandboxes_stop: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> None:
        self._base_url = base_url
        self._identity = identity
        # A callable when the number can move: the host's limit is read fresh
        # on every beat, so one that has just been lowered is never reported
        # as the capacity it no longer offers.
        self._slots_total = slots_total
        # Why this host is not claiming, when it is not. Read per beat for the
        # same reason, and ``""`` is a positive "nothing wrong" rather than an
        # absent field that would leave a stale reason on the page.
        self._launch_error = launch_error
        self._sandbox_manager_url = sandbox_manager_url
        self._capabilities = capabilities
        self._runner_version = runner_version
        self._sandbox_runtime = sandbox_runtime
        self._sandbox_image_digest = sandbox_image_digest
        self._runtime_versions = runtime_versions
        # A callable once versions can be downloaded while the host runs: a
        # version that finished downloading is offered on the next beat.
        self._runtime_versions_available = runtime_versions_available
        #: The deployment publishes the exact target SHA by rewriting this
        #: file, and the control plane's rollout fence admits nothing until a
        #: heartbeat carries that SHA back. Read here as well as per beat so
        #: the baseline is the state this runner started in: without it the
        #: first poll would read an unchanged marker as a change.
        #:
        #: Waking early only carries that SHA when `capabilities` is a
        #: callable: the marker reaches the control plane through the
        #: capability list, so a static list would beat sooner with an answer
        #: computed once at startup and the fence would still wait out the
        #: interval, silently. The runner passes a lambda for that reason.
        self._readiness_marker = (readiness_marker or "").strip() or None
        self._readiness_marker_value = self._read_readiness_marker()
        # Freshly asked on every beat — a static list would report sandboxes
        # the pool has already torn down.
        self._residency_beats = residency_beats
        self._retained_sandbox_beats = retained_sandbox_beats
        # Also asked per beat: the age is recomputed from the sampler's
        # monotonic clock at send time, so a cached block cannot be made to
        # look fresh by the act of sending it. ``None`` — before the first
        # sample, or once the newest one is too old to be worth sending —
        # simply omits the field.
        self._capacity_beats = capacity_beats
        # Asked freshly on every beat. It reports the revision this process has
        # *applied*, which is what lets the Admin page tell "this host has not
        # heard about the Stop" from "this host is not there" -- two different
        # problems with two different fixes.
        self._profiling = profiling
        # Asked per beat for what this host applies now, and told after each
        # answered beat what an admin set. The callback runs on this thread and
        # must not raise; one that does is logged and the beats go on.
        self._settings_report = settings_report
        self._on_settings = on_settings
        self._sandbox_archive_settings_report = sandbox_archive_settings_report
        self._on_sandbox_archive_settings = on_sandbox_archive_settings
        # Asked per beat for each download's progress, and told after each
        # answered beat which versions the control plane wants, or ``None``
        # when the answer does not say.
        self._runtime_downloads_report = runtime_downloads_report
        self._on_runtime_downloads = on_runtime_downloads
        self._on_residencies_lost = on_residencies_lost
        self._on_retained_sandboxes_stop = on_retained_sandboxes_stop
        self._stop = threading.Event()
        #: Set to cut the wait between beats short. The supervisor pulls it
        #: when its executor count changes, so the control plane learns the
        #: new capacity now rather than up to an interval later.
        self._wake = threading.Event()
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
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def wake(self) -> None:
        """Send the next beat as soon as the thread is free, not on the timer."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                capabilities = (
                    self._capabilities() if callable(self._capabilities) else self._capabilities
                )
                settings_applied, settings_local = (
                    self._settings_report() if self._settings_report is not None else (None, None)
                )
                archive_settings_applied_revision = (
                    self._sandbox_archive_settings_report()
                    if self._sandbox_archive_settings_report is not None
                    else None
                )
                heartbeat(
                    base_url=self._base_url,
                    identity=self._identity,
                    slots_total=(
                        self._slots_total() if callable(self._slots_total) else self._slots_total
                    ),
                    launch_error=(
                        self._launch_error() if callable(self._launch_error) else self._launch_error
                    ),
                    sandbox_manager_url=self._sandbox_manager_url,
                    capabilities=capabilities,
                    runner_version=self._runner_version,
                    sandbox_runtime=self._sandbox_runtime,
                    sandbox_image_digest=self._sandbox_image_digest,
                    runtime_versions=self._runtime_versions,
                    runtime_versions_available=(
                        self._runtime_versions_available()
                        if callable(self._runtime_versions_available)
                        else self._runtime_versions_available
                    ),
                    residencies=(
                        self._residency_beats() if self._residency_beats is not None else None
                    ),
                    retained_sandboxes=(
                        self._retained_sandbox_beats()
                        if self._retained_sandbox_beats is not None
                        else None
                    ),
                    capacity=(self._capacity_beats() if self._capacity_beats is not None else None),
                    profiling=(self._profiling() if self._profiling is not None else None),
                    settings_applied=settings_applied,
                    settings_local=settings_local,
                    sandbox_archive_settings_applied_revision=(archive_settings_applied_revision),
                    runtime_downloads=(
                        self._runtime_downloads_report()
                        if self._runtime_downloads_report is not None
                        else None
                    ),
                    on_residencies_lost=self._on_residencies_lost,
                    on_retained_sandboxes_stop=self._on_retained_sandboxes_stop,
                )
                self._apply_settings()
                self._apply_sandbox_archive_settings()
                self._apply_runtime_downloads()
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
            self._wait_for_next_beat()

    def _apply_settings(self) -> None:
        """Hand the answered settings on, without letting a failure stop the beats."""
        if self._on_settings is None:
            return
        try:
            self._on_settings(dict(self._identity.settings))
        except Exception:
            logger.exception(
                "agent_host_settings_apply_failed",
                extra={"event": "agent_host_settings_apply_failed", "host": self._identity.host},
            )

    def _apply_sandbox_archive_settings(self) -> None:
        """Relay the answered DB policy without letting a failure stop heartbeats."""
        if self._on_sandbox_archive_settings is None:
            return
        settings = self._identity.sandbox_archive_settings
        try:
            self._on_sandbox_archive_settings(None if settings is None else dict(settings))
        except Exception:
            logger.exception(
                "agent_sandbox_archive_settings_apply_failed",
                extra={
                    "event": "agent_sandbox_archive_settings_apply_failed",
                    "host": self._identity.host,
                },
            )

    def _apply_runtime_downloads(self) -> None:
        """Hand the answered download targets on, without stopping the beats.

        ``None`` is an answer without them: the downloader keeps what it holds
        and removes nothing on that answer's word.
        """
        if self._on_runtime_downloads is None:
            return
        targets = self._identity.runtime_downloads
        try:
            self._on_runtime_downloads(None if targets is None else list(targets))
        except Exception:
            logger.exception(
                "agent_runtime_downloads_apply_failed",
                extra={
                    "event": "agent_runtime_downloads_apply_failed",
                    "host": self._identity.host,
                },
            )

    def _wait_for_next_beat(self) -> None:
        """Sleep one interval, waking early for a marker move or a nudge.

        Two things cut the wait short. :meth:`wake` is the supervisor saying
        its executor count changed, and the control plane admits claims
        against that count, so it should hear the new one now rather than up
        to an interval later. The readiness marker is the other:

        A deployment publishes the exact target SHA by rewriting the marker
        file every runner already mounts, and the control plane's rollout fence
        admits nothing until a heartbeat carries that SHA back. Sleeping the
        whole interval first made that fence wait on a timer instead of on the
        fact it was waiting for: measured on staging, 15.2s of a 46s host
        deployment, drawn from a range of zero to one full interval.

        Nothing else about the cadence changes. The beat is the same request,
        a deployment rewrites the marker at most twice, and a missed wake-up
        costs exactly what the runner pays today.
        """
        # ``_wake`` is the one event both a nudge and ``stop`` set, so a single
        # wait covers both; ``_stop`` itself is re-read by the loop above.
        if self._readiness_marker is None:
            self._wake.wait(self._identity.interval_s)
            self._wake.clear()
            return
        deadline = time.monotonic() + self._identity.interval_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._wake.wait(min(READINESS_MARKER_POLL_S, remaining)):
                self._wake.clear()
                return
            if self._readiness_marker_moved():
                return

    def _readiness_marker_moved(self) -> bool:
        """Whether the readiness marker changed since the last beat.

        An empty read is not a change. The deployment rewrites this file by
        truncating it in place — deliberately, so the inode every replica has
        bind-mounted survives — so a poll can land inside that write and see
        nothing. Counting that as news would beat on a torn read and, worse,
        reset the baseline to nothing, which would make the value that landed
        look like a second change. It is also what "empty" means everywhere
        else here: nothing to report, and the next scheduled beat reads
        whatever arrived.
        """
        current = self._read_readiness_marker()
        if not current or current == self._readiness_marker_value:
            return False
        self._readiness_marker_value = current
        return True

    def _read_readiness_marker(self) -> str:
        """The marker's current value, or an empty string when it is unreadable."""
        if self._readiness_marker is None:
            return ""
        try:
            return Path(self._readiness_marker).read_text().strip()
        except OSError:
            return ""
