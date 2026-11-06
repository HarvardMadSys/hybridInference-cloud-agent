"""The host service: enrollment, host heartbeats, the claim loop, and draining.

One process per machine, started as ``python -m cloud_agent_host.runner``. It
claims a turn only while it is under its limit and hands each claimed turn to
a child process of its own (:mod:`cloud_agent_host.turn.turn_child`); it never runs
a turn itself. On SIGTERM it stops claiming and waits for its children.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

from cloud_agent_host.claimed_job import ClaimedJob, ClaimUnreachable, WorkWaitUnsupported
from cloud_agent_host.container.sandbox import (
    ContainerBackend,
    SandboxBackend,
    SandboxError,
    build_backend_from_env,
)
from cloud_agent_host.host_capacity import (
    HostCapacityCollector,
    build_storage_probe,
    capacity_enabled,
)
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.measurement_delivery import DELIVERY_HANDOFF_DRAIN_S
from cloud_agent_host.runtime_downloads import (
    ENV_RUNTIME_DOWNLOAD_NETWORK,
    ENV_RUNTIME_REGISTRY_URL,
    RuntimeDownloads,
)
from cloud_agent_host.runtime_qualification import HostQualification
from cloud_agent_host.sandbox_manager_client import (
    SandboxManagerClient,
    SandboxManagerError,
    sandbox_manager_from_env,
)
from cloud_agent_host.service import incident_relay
from cloud_agent_host.service.enrollment import HostHeartbeat, ensure_enrolled
from cloud_agent_host.service.host_capabilities import (
    _advertised_host_capabilities,
    _shared_live_deployment_ready,
    _shared_live_host_capabilities,
    _shared_live_ready_file,
    claim_attestation,
)
from cloud_agent_host.service.host_control import (
    claim_payload,
    keepalive_client,
    release_turn,
    wait_for_work,
)
from cloud_agent_host.service.host_runtime import (
    LAUNCH_STARTED,
    LAUNCH_UNCERTAIN,
    LaunchResult,
    TurnPool,
    default_slots,
    read_handover,
    requested_slots,
)
from cloud_agent_host.service.local_settings import (
    DEFAULT_AGENT_TIMEOUT_S,
    DEFAULT_LEASE_TTL_S,
    _runner_local_settings,
)
from cloud_agent_host.service.measurement_sender import _enqueue_delivery
from cloud_agent_host.service.profiling_policy import ProfilingPolicySync
from cloud_agent_host.service.runner_stop import (
    STOPPING as _STOPPING,
    drain_on_sigterm as _drain_on_sigterm,
    stay_down as _stay_down,
)
from cloud_agent_host.service.runtime_settings import RuntimeSettings, SandboxArchiveRuntimeSettings
from cloud_agent_host.service.turn_launch import TurnSpare, spawn_turn
from cloud_agent_shared import operation_profiling
from cloud_agent_shared.sandbox_manager_compat import manager_env
from cloud_agent_shared.sandbox_resources import parse_sandbox_resource_sample
from cloud_agent_shared.shared_protocol import SHARED_SANDBOX_CAPABILITY

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import httpx

# Log records keep the name they had while all of this was runner.py.
logger = get_logger("cloud_agent_host.runner")


#: How long a freshly started host waits for its first heartbeat answer — which
#: carries the runtime settings an admin set — before claiming with its own.
#: The first beat is sent at once, so this is normally well under a second; the
#: bound is for a control plane whose heartbeat is failing while claims are not.
FIRST_SETTINGS_WAIT_S = 30.0


#: How long a host polls before asking again for the wait endpoint a control
#: plane did not have. Hosts deploy first, so this is the rest of a rollout.
WORK_WAIT_RETRY_S = 300.0


#: The root profiling operation covering one attempt's startup chain, from the
#: claim request to the moment the prompt is handed to the resident CLI. It is
#: the name an experiment lists in its run descriptor's `operations`.
STARTUP_OPERATION = "attempt.startup"


class _WorkWaiter:
    """Parks an idle host until the control plane says the queue changed.

    Each wait passes the token the previous one returned, which predates the
    claim that just found nothing; anything queued since has advanced it, so
    nothing that claim missed waits out an interval. Waits no longer than the
    poll it replaces, so what the control plane cannot signal is found as soon
    as before. Sleeps that same interval instead when the control plane has no
    wait endpoint, asking again after :data:`WORK_WAIT_RETRY_S`, or when a
    wait fails.
    """

    def __init__(
        self, *, base_url: str, interval_s: float, client: httpx.Client | None = None
    ) -> None:
        self._base_url = base_url
        self._interval_s = interval_s
        self._client = client
        self._token: str | None = None
        self._poll_until = 0.0

    def wait(self, host_credential: str) -> None:
        """Return when the host should claim again."""
        if self._interval_s <= 0 or time.monotonic() < self._poll_until:
            time.sleep(self._interval_s)
            return
        try:
            self._token = wait_for_work(
                base_url=self._base_url,
                host_credential=host_credential,
                since=self._token,
                timeout_s=self._interval_s,
                client=self._client,
            )
        except WorkWaitUnsupported:
            self._poll_until = time.monotonic() + WORK_WAIT_RETRY_S
            time.sleep(self._interval_s)
        except Exception as exc:
            self._token = None
            logger.warning(
                "agent_work_wait_failed",
                extra={"event": "agent_work_wait_failed", "error": str(exc)[:300]},
            )
            time.sleep(self._interval_s)


#: How long the host-heartbeat thread waits on the sandbox manager: for the
#: Docker observation a beat carries, and for the residency cleanup its answer
#: asks for. The beat is the host's liveness signal, and the control plane
#: counts a host offline after three missed intervals: a sandbox manager that
#: is restarting, or whose request workers are all busy, costs one beat its
#: observation or its cleanup, never the beat.
_HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S = 10.0


def _retained_sandbox_heartbeat_rows() -> list[dict[str, Any]] | None:
    """Read Docker truth from the host singleton without breaking host beats.

    The host heartbeat must remain a liveness signal even while the workspace
    sandbox manager is restarting. Returning ``None`` means "not observed this beat";
    it must never be confused with an authoritative empty list or deletion.
    """
    client: SandboxManagerClient | None = None
    try:
        client = sandbox_manager_from_env(timeout=_HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S)
        incident_relay.confirm(client)
        rows = client.list()
        observations: list[dict[str, Any]] = []
        now = time.monotonic()
        for row in rows[:4096]:
            if row.get("shared_live") is not True:
                raise ValueError("sandbox manager returned a non-shared sandbox")
            members = [dict(item) for item in row.get("members") or [] if isinstance(item, dict)]
            activation_generation = int(row["activation_generation"])
            running = bool(row["running"])
            active = bool(row.get("group_busy")) or int(row.get("terminal_count") or 0) > 0
            active = active or int(row.get("operation_count") or 0) > 0
            active = active or bool(row.get("workspace_suspended"))
            deadline = row.get("idle_deadline_monotonic")
            # A generation-zero RUNNING entry with no local deadline was just
            # reconstructed after a sandbox manager restart. Its empty in-memory token
            # set is not proof that the group has been idle: the sandbox manager may
            # have restarted while a terminal had cleared the durable
            # deadline. Report activity as unknown. The control plane installs
            # one recovery deadline only when its durable deadline is also
            # NULL; unlike reporting a fresh full timeout on every heartbeat,
            # that window cannot be extended forever by healthy heartbeats.
            activity_known = activation_generation > 0 or active or deadline is not None
            idle_remaining = None
            if activity_known and deadline is not None:
                idle_remaining = max(0.0, float(deadline) - now)
            resource_usage = parse_sandbox_resource_sample(row.get("resource_usage"))
            terminal_count = row.get("terminal_count")
            if (
                isinstance(terminal_count, bool)
                or not isinstance(terminal_count, int)
                or terminal_count < 0
                or terminal_count > 4096
            ):
                terminal_count = None
            observations.append(
                {
                    "thread_id": str(row["thread_id"]),
                    "sandbox_id": str(row["sandbox_id"]),
                    "container_id": str(row["container_id"]),
                    "generation": int(row["generation"]),
                    "activation_generation": activation_generation,
                    "running": running,
                    "activity_active": active if activity_known else None,
                    "idle_remaining_seconds": idle_remaining,
                    "terminal_count": terminal_count,
                    "resource_usage": None
                    if resource_usage is None
                    else resource_usage.model_dump(),
                    "shared_live": True,
                    "members": members,
                    "checkpoint": (
                        row.get("checkpoint") if isinstance(row.get("checkpoint"), dict) else None
                    ),
                    "incidents": incident_relay.carry(row),
                }
            )
        return observations
    except (KeyError, TypeError, ValueError, SandboxError, SandboxManagerError) as exc:
        print(
            f"agent runner: retained sandbox observation unavailable: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None
    finally:
        if client is not None:
            client.close()


_RETAINED_STOP_COMMAND_MAX = 128


_RetainedStopKey = tuple[str, int, int]


class _RetainedSandboxStopWorker:
    """Keep synchronous sandbox manager stops off the host-heartbeat thread.

    The control plane repeats a durable stop fence until Docker reports the
    group stopped. Exact duplicate fences are ignored and overflow is left for
    a later heartbeat. Closing discards pending work but joins the one request
    already in flight, including a bounded synchronous final archive.
    """

    def __init__(
        self,
        apply: Callable[[list[dict[str, Any]]], None],
        *,
        max_commands: int = _RETAINED_STOP_COMMAND_MAX,
    ) -> None:
        if max_commands < 1:
            raise ValueError("retained stop worker capacity must be positive")
        self._apply = apply
        self._max_commands = max_commands
        self._condition = threading.Condition()
        self._pending: dict[_RetainedStopKey, dict[str, Any]] = {}
        self._active: set[_RetainedStopKey] = set()
        self._closed = False
        self._thread: threading.Thread | None = None

    @staticmethod
    def _key(command: dict[str, Any]) -> _RetainedStopKey | None:
        thread_id = command.get("thread_id")
        generation = command.get("generation")
        activation = command.get("activation_generation")
        if (
            not isinstance(thread_id, str)
            or not thread_id
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or isinstance(activation, bool)
            or not isinstance(activation, int)
            or activation < 0
        ):
            return None
        return thread_id, generation, activation

    def submit(self, commands: list[dict[str, Any]]) -> None:
        """Admit unique exact fences without ever waiting for sandbox manager I/O."""
        invalid = 0
        overflow = 0
        with self._condition:
            if self._closed:
                return
            for command in commands:
                key = self._key(command)
                if key is None:
                    invalid += 1
                    continue
                if key in self._pending or key in self._active:
                    continue
                if len(self._pending) + len(self._active) >= self._max_commands:
                    overflow += 1
                    continue
                self._pending[key] = dict(command)
            if self._pending and self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name="agent-retained-sandbox-stop",
                )
                self._thread.start()
            self._condition.notify_all()
        if invalid:
            logger.warning(
                "agent_retained_stop_command_invalid",
                extra={"event": "agent_retained_stop_command_invalid", "count": invalid},
            )
        if overflow:
            logger.warning(
                "agent_retained_stop_queue_full",
                extra={
                    "event": "agent_retained_stop_queue_full",
                    "count": overflow,
                    "capacity": self._max_commands,
                },
            )

    def close(self) -> None:
        """Seal intake, abandon pending durable commands, and join active I/O."""
        with self._condition:
            self._closed = True
            dropped = len(self._pending)
            self._pending.clear()
            self._condition.notify_all()
            thread = self._thread
        if dropped:
            logger.info(
                "agent_retained_stop_shutdown_drop",
                extra={"event": "agent_retained_stop_shutdown_drop", "count": dropped},
            )
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending:
                    if self._closed:
                        return
                    self._condition.wait()
                key = next(iter(self._pending))
                command = self._pending.pop(key)
                self._active.add(key)
            try:
                self._apply([command])
            except Exception:
                logger.exception(
                    "agent_retained_stop_worker_failed",
                    extra={"event": "agent_retained_stop_worker_failed"},
                )
            finally:
                with self._condition:
                    self._active.discard(key)
                    self._condition.notify_all()


def _stop_retained_sandboxes(commands: list[dict[str, Any]]) -> None:
    """Apply one worker batch of durable settled-idle stop commands."""
    client: SandboxManagerClient | None = None
    try:
        client = sandbox_manager_from_env()
        for command in commands:
            try:
                client.stop_shared_idle(
                    str(command["thread_id"]),
                    generation=int(command["generation"]),
                    activation_generation=int(command["activation_generation"]),
                    reason=str(command.get("reason") or "idle"),
                    member_activation_generations=command.get("member_activation_generations"),
                )
            except (KeyError, TypeError, ValueError, SandboxManagerError) as exc:
                # A stale command is expected during a racing acquire. The
                # next heartbeat reports fresh Docker state and the durable
                # predicate decides again; never fail host liveness for it.
                print(
                    f"agent runner: retained idle stop was not applied: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
    except (SandboxError, SandboxManagerError) as exc:
        print(
            f"agent runner: retained idle stop unavailable: {exc}",
            file=sys.stderr,
            flush=True,
        )
    finally:
        if client is not None:
            client.close()


def _handle_host_residencies_lost(lost: list[dict[str, Any]]) -> None:
    """Fence supervisor-owned shared members whose renewable grant was lost.

    This runs on the host-heartbeat thread, between one beat and the next, so
    it waits on the sandbox manager no longer than the beat's own observation
    does and starts no further call once that much time is spent. A member left
    for now is named again by a later beat, for as long as the sandbox manager
    still reports it live under the generation the control plane refused.
    """
    client: SandboxManagerClient | None = None
    deadline = time.monotonic() + _HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S
    try:
        client = sandbox_manager_from_env(timeout=_HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S)
        shared_members = {
            str(member.get("thread_id"))
            for row in client.list()
            for member in row.get("members") or []
            if isinstance(member, dict) and member.get("thread_id")
        }
        for item in lost:
            thread_id = str(item.get("thread_id") or "")
            generation = item.get("generation")
            if thread_id not in shared_members or not isinstance(generation, int):
                continue
            if time.monotonic() >= deadline:
                break
            with contextlib.suppress(SandboxManagerError):
                client.shared_residency_lost(thread_id, generation=generation)
    except (SandboxError, SandboxManagerError) as exc:
        print(
            f"agent runner: shared residency cleanup unavailable: {exc}",
            file=sys.stderr,
            flush=True,
        )
    finally:
        if client is not None:
            client.close()


def _sandbox_manager_url_from_env() -> str | None:
    """This host's sandbox manager address, **as the control plane will reach it**.

    Defaults to the same variable `sandbox_manager_client` uses, so the
    address a host *reports* cannot drift from the one it *serves* — a row
    pointing at a sandbox manager this machine is not running answers 409 for every file
    and terminal request on its jobs. That is right only when both readers
    share a network, as in the integrated local Compose stack. Merely running
    separate Compose projects on one physical machine does not make their DNS
    or networks shared.

    It stops being right the moment the control plane is on another machine.
    `AGENT_SANDBOX_MANAGER_URL` is a compose service name; it resolves here and
    nowhere else. Reported verbatim, the control plane stores an address it
    cannot resolve, and the first call that needs the sandbox manager — a terminal
    suspend, a file listing — fails with a DNS error surfaced as 503. Every
    attempt then dies at the same point until the job runs out of them, which
    is what happened to the first real job on this deployment.

    So the two questions are separated, and only when a deployment says so:

    * `AGENT_SANDBOX_MANAGER_URL` — how *this runner* reaches its own sandbox manager.
    * `AGENT_SANDBOX_MANAGER_ADVERTISE_URL` — how *the control plane* reaches
      it, when that is a different address (a reverse tunnel's far end).

    Unset, the second is the first, and the drift the original comment warns
    about remains impossible without somebody explicitly asking for it.
    """
    advertised = (manager_env("AGENT_SANDBOX_MANAGER_ADVERTISE_URL") or "").strip()
    if advertised:
        return advertised
    return (manager_env("AGENT_SANDBOX_MANAGER_URL") or "").strip() or None


def _runner_version() -> str:
    """This runner's version, for the pool listing. Empty if unknown.

    Best effort: it is an operator-facing label, not a compatibility gate, and
    a missing version must never stop a host enrolling.
    """
    return (os.environ.get("AGENT_RUNNER_VERSION") or "").strip()


def _start_host_capacity_collector(backend: SandboxBackend) -> HostCapacityCollector | None:
    """Start sampling this host's load, or return ``None`` when disabled.

    Sampling is best effort by construction: it observes a machine, it never
    gates a claim, and a deployment that has not mounted the host's proc files
    gets "not measured" rather than a fabricated reading. That is why a failure
    here is a log line and not a startup error — a runner that cannot report
    CPU must still be able to run jobs.

    One collector per process, on its own daemon thread. Three replicas on one
    machine therefore sample independently and each keeps its own CPU baseline;
    the control plane keeps one latest report per host and never adds replicas
    together, because they are three views of one machine, not three machines.
    """
    if not capacity_enabled():
        logger.info(
            "agent_host_capacity_disabled",
            extra={"event": "agent_host_capacity_disabled"},
        )
        return None
    collector = HostCapacityCollector(
        # The same namespace the sandbox manager stamps on every container it
        # creates, read off the same backend object, so a host can never count
        # a sibling deployment's containers as its own.
        namespace=getattr(backend, "sandbox_namespace", "") or "",
        docker_binary=getattr(backend, "docker_binary", "docker"),
        # Which storage probe to run is the deployment's declaration, not this
        # process's guess: the script that knows the driver and the data root
        # writes the name into the environment, and the same script attests
        # the topology before any reading is published.
        storage_probe=build_storage_probe(),
    )
    collector.start()
    return collector


def _spawn_turn(
    payload: dict[str, Any],
    *,
    base_url: str,
    worker_id: str,
    lease_ttl: float,
    agent_timeout_s: float,
    host: str,
    qualification: HostQualification | None = None,
    spare: TurnSpare | None = None,
) -> LaunchResult:
    """Hand one claimed turn to a child and find out whether it took it.

    The handover is :func:`cloud_agent_host.service.turn_launch.spawn_turn`'s. This
    is the name the service calls and its tests replace, and it reads the
    answer with this module's ``read_handover``, which they replace too.
    """
    return spawn_turn(
        payload,
        base_url=base_url,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
        agent_timeout_s=agent_timeout_s,
        host=host,
        qualification=qualification,
        spare=spare,
        read_answer=read_handover,
    )


def run_forever(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
    *,
    base_url: str,
    worker_id: str,
    lease_ttl: float = DEFAULT_LEASE_TTL_S,
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    backend: SandboxBackend | None = None,
    host: str | None = None,
    spawn_turn: Callable[[dict[str, Any]], LaunchResult] | None = None,
    tick_s: float = 5.0,
) -> int:
    """Serve this host: claim turns up to its limit, and run each in its own child.

    One process per machine. It enrolls, heartbeats and samples capacity — the
    things a host does once — and it claims a turn only when it is under the
    limit an operator set on the admin Servers page. There is no pool of idle
    processes to keep at a size: the limit is a number this loop compares
    against what it is already running, so raising it lets the next poll take
    more and lowering it simply stops taking any until the count has come down.
    Turns already running are never interrupted by a change.

    The limit arrives on the heartbeat answer and is this host's own default
    until one has been set. It is reported back as ``slots_total`` on every
    beat, and the control plane admits claims against the lower of that and the
    operator's number, so a number this host has not yet confirmed can only
    ever admit less work, never more.

    The host runner owns scheduling and attestation only. The sandbox manager
    owns every retained container and resident Agent CLI process, so this loop
    never creates or bind-mounts a host worktree.
    """
    backend = backend or build_backend_from_env(keep_network_helper=True)
    # The host proves its runtime here, once, and its children inherit that
    # verdict rather than each booting a probe guest of its own.
    qualification = (
        HostQualification(backend, renew_network=True)
        if isinstance(backend, ContainerBackend) and spawn_turn is None
        else None
    )
    if qualification is not None and qualification.refresh():
        qualification.backend.check_network()
    else:
        backend.preflight()

    stamper = getattr(backend, "version_stamp", None)
    stamp: dict[str, Any] = stamper() if stamper is not None else {}
    capabilities = _shared_live_host_capabilities(backend, stamp)
    if SHARED_SANDBOX_CAPABILITY not in capabilities:
        raise RuntimeError(
            "this runner cannot attest shared-sandbox-v1; verify the container "
            "backend, sandbox manager URL/token, AGENT_HOST_SLOTS, image labels, "
            "runtime pins, and live-session driver capabilities"
        )
    if not (os.environ.get("AGENT_DEPLOYMENT_SHA") or "").strip():
        raise RuntimeError(
            "shared-live sandbox execution requires AGENT_DEPLOYMENT_SHA; "
            "sandbox_host.sh manages it together with the ready marker"
        )
    if not host:
        raise RuntimeError(
            "shared-live sandbox execution requires AGENT_RUNNER_HOST; every turn "
            "this machine runs reports the same enrolled host name"
        )
    try:
        host_default = default_slots()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    identity = ensure_enrolled(
        base_url=base_url,
        host=host,
        sandbox_manager_url=_sandbox_manager_url_from_env(),
        slots_total=host_default,
        capabilities=_advertised_host_capabilities(backend, stamp),
        runner_version=_runner_version(),
    )
    if identity is None:
        raise RuntimeError(
            "shared-live sandbox execution requires an enrolled host identity; "
            "configure AGENT_ENROLL_TOKEN or a persisted host credential"
        )

    qualification_error = ""
    # The child the next turn goes to, started after each handover so its
    # imports are done before that claim (turn_launch). Only _spawn_turn
    # starts one, so a host given its own launcher never has one.
    spare = TurnSpare(
        base_url=base_url,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
        agent_timeout_s=agent_timeout_s,
        host=host,
    )
    launch = spawn_turn or (
        lambda payload: _spawn_turn(
            payload,
            base_url=base_url,
            worker_id=str(payload.get("_worker_id") or worker_id),
            lease_ttl=lease_ttl,
            agent_timeout_s=agent_timeout_s,
            host=host,
            qualification=qualification,
            spare=spare,
        )
    )
    turns = TurnPool(launch=launch)
    from cloud_agent_host.environment_tests import EnvironmentTestWorker

    environment_tests = (
        EnvironmentTestWorker(base_url, backend, on_idle=turns.slot_freed)
        if isinstance(backend, ContainerBackend)
        else None
    )
    # The limit this host is willing to serve right now. Held across beats so
    # a control plane that is briefly unreachable leaves the host running what
    # it last confirmed rather than falling back to a default nobody chose.
    limit = host_default

    # The runtime settings an admin sets on the Servers page, over this host's
    # own. Adopted from each heartbeat answer on the heartbeat thread, which
    # also hands the sandbox manager its share; read here as each turn is handed over.
    # Until the first answer, every setting is this host's own, which is also
    # what an unreachable control plane leaves in place.
    try:
        settings_sandbox_manager: SandboxManagerClient | None = sandbox_manager_from_env()
    except Exception:
        settings_sandbox_manager = None
    runtime_settings = RuntimeSettings(
        _runner_local_settings(agent_timeout_s, lease_ttl),
        sandbox_manager=None
        if settings_sandbox_manager is None
        else settings_sandbox_manager.apply_host_settings,
    )
    archive_settings = SandboxArchiveRuntimeSettings(
        None
        if settings_sandbox_manager is None
        else settings_sandbox_manager.apply_sandbox_archive_settings
    )

    capacity = _start_host_capacity_collector(backend)
    # The policy sync is installed *before* the host starts taking work, so
    # there is no window in which this process could select an operation
    # without a policy to check it against. Until the first answer arrives the
    # store holds nothing, which reads as "not collecting" -- a restart is a
    # Stop, never an accidental continuation.
    try:
        # The sandbox manager client is the real one, built from this host's own
        # environment. Without it the runner would know what to collect and the
        # sandbox manager -- a separate container that shares no memory with it -- would
        # never hear, so every file and terminal sample would arrive with its
        # container sub-stages missing. A host that has no sandbox manager configured
        # passes ``None`` and still syncs its own configuration.
        try:
            sandbox_manager_client: SandboxManagerClient | None = sandbox_manager_from_env()
        except Exception:
            sandbox_manager_client = None
            logger.info(
                "agent_profiling_sandbox_manager_unconfigured",
                extra={"event": "agent_profiling_sandbox_manager_unconfigured"},
            )
        policy_sync = ProfilingPolicySync(
            base_url=base_url,
            credential=identity.credential,
            host=identity.host,
            sandbox_manager=sandbox_manager_client,
        )
        policy_sync.install()
    except Exception:
        # A host that cannot sync a *monitoring* configuration still serves
        # turns. Reporting the failure is enough; refusing to run would make an
        # experiment's plumbing a prerequisite for the product.
        logger.warning("agent_profiling_policy_unavailable", exc_info=True)
        policy_sync = None

    retained_stop_worker = _RetainedSandboxStopWorker(_stop_retained_sandboxes)

    def apply_archive_settings(settings: dict[str, Any] | None) -> None:
        """Relay policy and immediately report loss or gain of confirmation."""
        before = archive_settings.report()
        try:
            if settings is None:
                archive_settings.invalidate()
            else:
                archive_settings.apply(settings)
        finally:
            if archive_settings.report() != before:
                pool.wake()

    # The agent versions an admin updated to from the Settings page, kept as
    # local images beside the sandbox image. Only a container backend has an
    # image to build them on; any other host reports none and is never asked.
    downloads: RuntimeDownloads | None = None
    if isinstance(backend, ContainerBackend):
        offered = backend.downloaded_versions

        def downloaded() -> None:
            # Offered on the next beat, not an interval later, and not from a
            # menu read before the download finished; a version removed is
            # withdrawn the same way.
            offered.invalidate()
            pool.wake()

        downloads = RuntimeDownloads(
            docker=backend.docker_binary,
            image=backend.image,
            namespace=backend.sandbox_namespace,
            image_menu=backend.image_menu,
            network=os.environ.get(ENV_RUNTIME_DOWNLOAD_NETWORK, "").strip() or "bridge",
            registry=os.environ.get(ENV_RUNTIME_REGISTRY_URL, "").strip() or None,
            on_change=downloaded,
        )
        # A menu too long to report keeps these first (``version_stamp``).
        offered.targets = downloads.targets

    pool = HostHeartbeat(
        base_url=base_url,
        identity=identity,
        sandbox_manager_url=_sandbox_manager_url_from_env(),
        # Read per beat: the limit is what this host will actually allow, and
        # the control plane admits claims against the lower of it and the
        # operator's number.
        slots_total=lambda: limit,
        launch_error=lambda: qualification_error or turns.launch_error,
        # Read per beat: the claim loop replaces ``stamp`` when a qualification
        # refresh changes what this host can run.
        capabilities=lambda: _advertised_host_capabilities(backend, stamp),
        runner_version=_runner_version(),
        sandbox_runtime=stamp.get("sandbox_runtime"),
        sandbox_image_digest=stamp.get("sandbox_image_digest"),
        runtime_versions=stamp.get("runtime_versions"),
        # Read per beat: the image's own menu, plus whatever has been
        # downloaded since this runner started, as last listed; each beat
        # past a few seconds lists it again in the background.
        runtime_versions_available=(
            (lambda: backend.version_stamp().get("runtime_versions_available"))
            if stamper is not None
            else stamp.get("runtime_versions_available")
        ),
        runtime_downloads_report=None if downloads is None else downloads.report,
        on_runtime_downloads=None if downloads is None else downloads.apply,
        config_reports=None if downloads is None else downloads.registry_report,
        on_runtime_registry=None if downloads is None else downloads.set_registry,
        readiness_marker=_shared_live_ready_file(),
        retained_sandbox_beats=_retained_sandbox_heartbeat_rows,
        # Read per beat, so the age it carries is the age it had when the beat
        # was built. ``None`` before the first sample, and once the newest one
        # is older than the protocol allows: an unsent observation reads as
        # stale on the page, which is the truth.
        capacity_beats=None if capacity is None else lambda: capacity.latest()[0],
        settings_report=runtime_settings.report,
        sandbox_archive_settings_report=archive_settings.report,
        # A changed report goes out now rather than an interval later, so the
        # admin page stops saying "applying" as soon as it is true.
        on_settings=lambda admin: pool.wake() if runtime_settings.apply(admin) else None,
        on_sandbox_archive_settings=apply_archive_settings,
        on_residencies_lost=_handle_host_residencies_lost,
        on_retained_sandboxes_stop=retained_stop_worker.submit,
    )
    if policy_sync is not None:
        # Its own loop as well as the beat's answer: the answer only arrives
        # when a beat does, and a Stop must not wait out the heartbeat interval
        # to be noticed.
        policy_sync.start()
    if downloads is not None:
        downloads.start()
    pool.start()
    settings_deadline = time.monotonic() + FIRST_SETTINGS_WAIT_S
    _drain_on_sigterm()

    def stop_all() -> None:
        """Stop heartbeating, lifecycle work, and sampling, in that order.

        The sampler never joins a heartbeat in flight, so the pool is stopped
        first and the thread that fed it second; neither call can block on the
        other, which is what keeps a shut-down runner from waiting on a Docker
        daemon it no longer needs.
        """
        if environment_tests is not None:
            environment_tests.close()
        pool.stop()
        if downloads is not None:
            downloads.stop()
        retained_stop_worker.close()
        if policy_sync is not None:
            policy_sync.stop()
        if capacity is not None:
            capacity.stop()
        if settings_sandbox_manager is not None:
            settings_sandbox_manager.close()

    print(
        f"agent runner {worker_id} serving host {identity.host} (enrolled; "
        f"sandbox backend: {backend.name}; up to {limit} turn(s) at once until the "
        "control plane says otherwise)",
        flush=True,
    )

    # One keep-alive client for the claims, the waits between them and a turn
    # handed back. Each used to open a connection of its own: with the
    # environment-test claim, three in every idle cycle. Credentials still go
    # per request, so a rotated host credential is used at once and a release
    # sends its own turn's token.
    host_client = keepalive_client()
    work = _WorkWaiter(base_url=base_url, interval_s=tick_s, client=host_client)
    immediate = False
    rejected = False
    claims = 0
    try:
        while True:
            for finished in turns.reap():
                logger.info(
                    "agent_turn_finished",
                    extra={
                        "event": "agent_turn_finished",
                        "job_id": finished.job_id,
                        "attempt_id": finished.attempt_id,
                        "exit_code": finished.exit_code,
                    },
                )
            if _STOPPING.is_set():
                print(
                    "agent runner: stopping; turns already running finish first",
                    file=sys.stderr,
                    flush=True,
                )
                break
            if pool.removed.is_set():
                print(
                    "agent runner: this host's credential was rejected; not claiming further work",
                    file=sys.stderr,
                    flush=True,
                )
                rejected = True
                break

            # Whatever the last successful beat carried, including nothing:
            # clearing the number on the admin page is an instruction like any
            # other, and a host that only followed numbers it could parse went
            # on serving the old one until its container was replaced.
            #
            # No special case is needed to survive an outage. A failed beat
            # raises before it reads the answer, so ``slots_desired`` keeps the
            # value it last carried and this follows it unchanged.
            confirmed = requested_slots(identity.slots_desired)
            target = host_default if confirmed is None else confirmed
            if target != limit:
                logger.info(
                    "agent_host_concurrency_changed",
                    extra={
                        "event": "agent_host_concurrency_changed",
                        "host": identity.host,
                        "was": limit,
                        "now": target,
                        "source": "host_default" if confirmed is None else "control_plane",
                        "running": turns.running(),
                    },
                )
                limit = target
                # The scheduler admits against the number on the host row, so
                # a change this host has applied should be there now rather
                # than up to an interval from now.
                pool.wake()

            if not pool.claimable:
                time.sleep(tick_s)
                continue
            test_slots = int(environment_tests is not None and environment_tests.busy)
            if not turns.may_claim(max(0, limit - test_slots)):
                # A turn's child exiting or an environment test ending ends this wait at once,
                # and nothing else does; the tick is the fallback. No API poll or retry is added.
                turns.wait_for_slot(min(tick_s, 1.0) if turns.running() >= limit else tick_s)
                continue
            if qualification is not None:
                try:
                    qualification.refresh()
                    stamp = qualification.backend.version_stamp()
                except SandboxError as exc:
                    # Reported once per reason. The retry already backs off
                    # inside refresh(); a traceback and an extra beat on every
                    # poll would only bury the first report.
                    error = f"runtime qualification failed: {exc}"
                    if error != qualification_error:
                        qualification_error = error
                        logger.warning("agent_runtime_qualification_failed", exc_info=True)
                        pool.wake()
                    time.sleep(tick_s)
                    continue
                if qualification_error:
                    qualification_error = ""
                    pool.wake()
            # Always gate claims after refreshing the image attestation. A
            # cached invalid image must not prevent qualification of a repair.
            # Keep the old control plane safe during a parallel rollout. It
            # does not enforce this generation's exact-SHA attestation, so this
            # host stays idle until its own checks complete.
            if not _shared_live_deployment_ready(backend, stamp):
                time.sleep(tick_s)
                continue
            # A host that restarted can claim before its first heartbeat answer
            # arrives, and a turn claimed then would run with this host's own
            # settings rather than an admin's. Wait for that answer, but only so
            # long: claiming must not come to depend on the heartbeat.
            if not runtime_settings.adopted and time.monotonic() < settings_deadline:
                time.sleep(tick_s)
                continue
            # Archive policy is a stop-safety boundary. The sandbox manager must
            # explicitly confirm the complete DB revision, including an
            # explicit disabled policy, before this process creates work.
            if not archive_settings.ready:
                time.sleep(tick_s)
                continue

            # A distinct lease owner per turn. Fencing never reads this
            # string, but "which turn is stuck" stops being answerable the
            # moment two concurrent turns report the same name — which is
            # exactly when it is worth asking.
            claims += 1
            turn_worker_id = f"{worker_id}-{claims}"
            # Selected before the request: the wait for a claim is part of
            # the startup a user feels.
            # The slot is claimed here and not in the child, so one attempt
            # costs one sample however the turn is actually executed.
            measurement = operation_profiling.start_operation(
                operation=STARTUP_OPERATION,
                host=host,
                worker_id=turn_worker_id,
            )
            try:
                with operation_profiling.span(measurement, "claim"):
                    # Built once a pass: after an empty claim, the
                    # environment-test claim presents this same proof.
                    attestation = claim_attestation(
                        backend,
                        sandbox_archive_settings_applied_revision=archive_settings.report(),
                        host_settings=runtime_settings.turn_settings(),
                    )
                    payload = claim_payload(
                        base_url=base_url,
                        host_credential=pool.credential,
                        worker_id=turn_worker_id,
                        # The lease an admin set, adopted from the latest
                        # heartbeat answer; this host's own until then.
                        lease_ttl=runtime_settings.turn_settings().get("lease_ttl_s", lease_ttl),
                        attestation=attestation,
                        client=host_client,
                    )
            except ClaimUnreachable as exc:
                operation_profiling.discard(measurement)
                print(
                    f"agent runner: control plane unreachable, retrying: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(tick_s)
                continue
            except Exception as exc:
                operation_profiling.discard(measurement)
                print(
                    f"agent runner: claim failed unexpectedly: {exc}", file=sys.stderr, flush=True
                )
                time.sleep(tick_s)
                continue

            if payload is None:
                # Nothing was claimed, so there is no startup to measure and
                # the poll must not spend a slot.
                operation_profiling.discard(measurement)
                if environment_tests is not None:
                    environment_tests.poll(pool.credential, attestation)
                work.wait(pool.credential)
                continue

            _hand_over_turn(
                payload,
                turns=turns,
                base_url=base_url,
                worker_id=turn_worker_id,
                measurement=_bound_measurement(measurement, payload),
                host_settings=runtime_settings.turn_settings(),
                client=host_client,
            )
    except KeyboardInterrupt:
        immediate = True
    finally:
        try:
            # It holds no turn, so a drain never waits for it.
            spare.close(immediate=immediate)
            turns.shutdown(immediate=immediate)
        finally:
            try:
                stop_all()
            finally:
                host_client.close()
    if rejected:
        _stay_down()
    return 0


def _bound_measurement(
    measurement: operation_profiling.Measurement | None, payload: dict[str, Any]
) -> operation_profiling.Measurement | None:
    """Apply the target filter to a selection, now that the turn is known.

    Job, thread, sandbox and runtime only exist once a claim has answered, so
    the authoritative scope check happens here. A measurement that falls out of
    scope returns ``None`` and emits nothing -- and its slot goes back, because
    a target that never matched must not spend the experiment.
    """
    if measurement is None:
        return None
    try:
        job = ClaimedJob.from_response(payload)
    except (KeyError, TypeError, ValueError):
        # A payload this process cannot read is the child's problem to decline;
        # it is not a reason to report a measurement against facts nobody
        # established.
        operation_profiling.discard(measurement)
        return None
    if not operation_profiling.bind(
        measurement,
        attempt_id=job.attempt_id,
        job_id=job.job_id,
        thread_id=job.thread_id,
        sandbox_id=str((job.thread_sandbox or {}).get("sandbox_id") or "") or None,
        runtime_id=job.runtime,
        runtime_version=job.runtime_version,
        execution_profile=job.execution_profile,
    ):
        operation_profiling.discard(measurement)
        return None
    return measurement


def _hand_over_turn(
    payload: dict[str, Any],
    *,
    turns: TurnPool,
    base_url: str,
    worker_id: str,
    measurement: operation_profiling.Measurement | None = None,
    host_settings: Mapping[str, float] | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Give one claimed turn to a child, and requeue it only if it never began.

    The requeue is the narrow case and is kept narrow deliberately. A turn the
    child took is the child's — if it then dies, the lease expires and the
    control plane decides under the retained-session rules, which can end a
    conversation rather than replay an admitted prompt. Handing such a turn
    back from here would be this host overruling that judgement with a guess.
    """
    job_id = str(payload.get("job_id") or "")
    try:
        delivery_job = ClaimedJob.from_response(payload)
    except (KeyError, TypeError, ValueError):
        delivery_job = None
    # Carried beside the turn under an underscore key: the child logs under
    # the same name the claim recorded as this turn's lease owner. The
    # handover strips the claim's own underscore keys, then hands the child
    # this one (turn_launch.spawn_turn), so a claim can never supply it.
    # The selection rides beside the turn for the same reason the worker id
    # does: the child runs the part worth measuring, and the parent holds the
    # budget slot. It is discarded once the child is gone, because the child's
    # record is the measurement -- this process only opened it.
    context = operation_profiling.handoff_context(measurement)
    if measurement is not None and context is None:
        measurement.expired = measurement.expired or measurement.past_deadline()
        operation_profiling.finish(
            measurement,
            outcome="incomplete",
            failure_stage=(
                "expired_before_handoff"
                if measurement.expired
                else "selection_unavailable_before_handoff"
            ),
        )
        if delivery_job is not None:
            _enqueue_delivery(
                measurement,
                job=delivery_job,
                base_url=base_url,
                drain_s=DELIVERY_HANDOFF_DRAIN_S,
            )
        measurement = None
    payload = {
        **payload,
        "_worker_id": worker_id,
        **({"_profiling": context} if context else {}),
        # The runtime settings as of this handover. The child runs the whole
        # turn with them, so a change on the admin page never lands mid-turn.
        **({"_host_settings": dict(host_settings)} if host_settings else {}),
    }
    try:
        result = turns.launch(payload)
    except Exception as exc:
        # Whatever went wrong here, this host cannot say the turn did not
        # start — and an exception escaping the loop would end the service,
        # leaving every turn it was running to its lease. Treat it as the
        # ambiguous case it is.
        logger.warning(
            "agent_turn_handover_failed",
            exc_info=True,
            extra={"event": "agent_turn_handover_failed", "job_id": job_id},
        )
        result = LaunchResult(LAUNCH_UNCERTAIN, None, f"{type(exc).__name__}: {exc}")
    if result.outcome == LAUNCH_STARTED:
        # The child owns the record now, and with it the budget slot: the
        # selection was handed over, so refunding it here would let one
        # experiment spend its whole allowance on launches that were measured.
        # Marking it finished without emitting is what releases the parent's
        # hold on the record while keeping the slot spent.
        if measurement is not None:
            measurement.finished = True
            measurement.reserved = False
        return
    if result.outcome == LAUNCH_UNCERTAIN:
        # Nothing observed the turn's fate, and the child may or may not have
        # started. The selection is emitted as it stands -- incomplete, naming
        # the boundary it reached -- rather than dropped, because "we do not
        # know how far it got" is itself the observation.
        operation_profiling.finish(
            measurement, outcome="incomplete", failure_stage="handover_uncertain"
        )
        if delivery_job is not None:
            _enqueue_delivery(
                measurement,
                job=delivery_job,
                base_url=base_url,
                drain_s=DELIVERY_HANDOFF_DRAIN_S,
            )
        logger.warning(
            "agent_turn_handover_uncertain",
            extra={
                "event": "agent_turn_handover_uncertain",
                "job_id": job_id,
                "detail": result.detail,
            },
        )
        return
    # Provably never started: back to the queue, with no attempt spent and no
    # wait for a lease that nothing is holding.
    operation_profiling.finish(measurement, outcome="incomplete", failure_stage="not_started")
    if delivery_job is not None:
        _enqueue_delivery(
            measurement,
            job=delivery_job,
            base_url=base_url,
            drain_s=DELIVERY_HANDOFF_DRAIN_S,
        )
    worker_token = str(payload.get("worker_token") or "")
    released = bool(worker_token) and release_turn(
        base_url=base_url,
        worker_token=worker_token,
        job_id=job_id,
        reason="launch_failed",
        client=client,
    )
    logger.warning(
        "agent_turn_not_started",
        extra={
            "event": "agent_turn_not_started",
            "job_id": job_id,
            "detail": result.detail,
            "requeued": released,
        },
    )
