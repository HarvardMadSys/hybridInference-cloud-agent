"""The Kata guest observer as a retained sandbox uses it.

Under Kata, a guest-root observer in each sandbox traces member processes
with eBPF and journals what it sees, and :mod:`cloud_agent_host.guest_observer`
reads that journal. This module binds that contract to one label-verified
container: the probe that proves a host can observe before it advertises
shared-live work, the cursor fenced around each member prompt, and the reads
a member execution window makes.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_members import MEMBER_REAPER_TARGET
from cloud_agent_host.chat_sandbox_models import (
    CHAT_NAMESPACE_LABEL,
    CHAT_PROBE_LABEL,
    MEMBER_UID_BASE,
    ChatSandboxSpec,
)
from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_BINARY,
    GUEST_OBSERVER_EVENTS,
    GUEST_OBSERVER_ROTATE_BYTES,
    GuestObservation,
    GuestObserverController,
    GuestObserverCursor,
    GuestObserverRuntimeError,
    GuestObserverSnapshot,
)
from cloud_agent_host.sandbox import KATA_RUNTIME, SandboxError

if TYPE_CHECKING:
    import subprocess

    from cloud_agent_host.chat_sandbox import ChatSandboxManager

# The manager's own logger, as before this module was split from it.
logger = logging.getLogger("cloud_agent_host.chat_sandbox")

#: A probe older than this was abandoned by a process killed mid-probe. Since
#: discovery no longer stops it, the next probe in the namespace removes it.
PROBE_ABANDONED_AFTER_S = 3600

#: How many times the guest observer probe may be tried before a host is
#: refused. Three, because the probe is a race against a guest that has just
#: booted and the cost of asking again is one throwaway sandbox, while the cost
#: of a false refusal is the whole host advertising no deployment.
GUEST_OBSERVER_PROBE_ATTEMPTS = 3
#: Long enough for the observer's poll loop, its ring drain and its first status
#: publication to get clear of whatever was still starting behind them.
GUEST_OBSERVER_PROBE_RETRY_S = 2.0


class _ObserverProbeRace(SandboxError):
    """A probe failure that a second attempt may not reproduce.

    Raised only by the stages that read back from a running guest observer --
    opening the cursor and closing it again. Everything else the probe checks
    is a property of the image, the kernel or the exec sandbox, and answers
    identically however many times it is asked; those keep raising
    ``SandboxError`` so a genuinely incapable host is refused at once instead of
    three times.

    It is a ``SandboxError`` so that callers which already handle a failed
    preflight are unaffected by the distinction.
    """


def probe(manager: ChatSandboxManager) -> str | None:
    """Prove the guest kernel, retained exec isolation and real eBPF delivery.

    The observation stages are retried because they race a guest that is
    still starting. Everything before them is a property of the image and
    the kernel: it answers the same way every time, so it is raised on the
    first attempt rather than being asked three times on a host that is
    genuinely missing a capability.

    A rollout hangs on the answer. `cmd_up` invalidates this host's
    shared-live marker and drains the runners that were serving before it
    runs this, and completes the marker only once every replica passes, so
    a probe that fails leaves the host advertising no deployment at all --
    and a runner that advertises no deployment does not poll. One lost race
    in a freshly booted microVM took staging off the air for 29.6 hours on
    2026-09-13, because nothing retried and nothing else deployed until the
    next day.
    """
    if manager.backend.runtime != KATA_RUNTIME:
        return None
    attempts = 0
    while True:
        attempts += 1
        try:
            return _probe_once(manager)
        except _ObserverProbeRace as exc:
            if attempts >= GUEST_OBSERVER_PROBE_ATTEMPTS:
                raise SandboxError(
                    f"{exc} (after {attempts} attempts on a fresh sandbox each time)"
                ) from exc
            logger.warning(
                "Kata observer probe lost a race with its own guest; retrying: %s",
                exc,
                extra={"event": "guest_observer_probe_retry", "attempt": attempts},
            )
            time.sleep(GUEST_OBSERVER_PROBE_RETRY_S)


def remove_abandoned_probes(manager: ChatSandboxManager) -> None:
    """Remove this namespace's probes left behind by a process killed mid-probe.

    Best effort: a listing that fails leaves them for the next probe, and
    the probe about to run reports a Docker that is genuinely unusable.
    """
    listed = manager._run(
        [
            manager.backend.docker_binary,
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--filter",
            f"label={CHAT_PROBE_LABEL}",
            "--format",
            '{"id":{{json .ID}},"namespace":{{json (.Label "'
            + CHAT_NAMESPACE_LABEL
            + '")}},"created":{{json (.Label "'
            + CHAT_PROBE_LABEL
            + '")}}}',
        ],
        timeout=30,
    )
    if listed.returncode != 0:
        return
    abandoned_before = time.time() - PROBE_ABANDONED_AFTER_S
    for line in listed.stdout.splitlines():
        try:
            row = json.loads(line)
            container_id = row["id"]
            if (
                not isinstance(container_id, str)
                or not container_id
                or row["namespace"] != manager.backend.sandbox_namespace
                or int(row["created"]) > abandoned_before
            ):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        manager._run([manager.backend.docker_binary, "rm", "--force", container_id], timeout=30)


def _probe_once(manager: ChatSandboxManager) -> str:  # noqa: PLR0912 complexity baseline, #771
    """One probe, on a sandbox of its own that is removed however it ends."""
    remove_abandoned_probes(manager)
    token = secrets.token_hex(12)
    spec = ChatSandboxSpec(
        thread_id=f"athr_preflight_{token}",
        sandbox_id=f"asbx_preflight_{token}",
        generation=1,
        runtime_id="preflight",
        resident_slot_capacity=2,
        labels={CHAT_PROBE_LABEL: str(int(time.time()))},
    )
    command = manager.build_create_command(spec)
    # Use the production creation/exec flags, without attaching any relay
    # or touching retained user state. No repository or credential enters.
    command[command.index("--network") + 1] = "none"
    created = manager._run(command, timeout=120)
    if created.returncode != 0:
        raise SandboxError("Kata observer probe creation failed: " + created.stderr[:300])
    try:
        started = manager._run(
            [manager.backend.docker_binary, "start", spec.container_name], timeout=120
        )
        if started.returncode != 0:
            raise SandboxError("Kata observer probe start failed: " + started.stderr[:300])
        isolation = manager.exec_capture(
            spec,
            [
                "python3",
                "-c",
                "import json, os; "
                "s=dict(line.split(':',1) for line in open('/proc/self/status') "
                "if ':' in line); "
                "print(json.dumps(dict(kernel=os.uname().release, uid=os.getuid(), "
                "nnp=int(s['NoNewPrivs']), seccomp=int(s['Seccomp']), "
                "effective=int(s['CapEff'],16), permitted=int(s['CapPrm'],16))))",
            ],
            workdir="/",
            timeout_s=30,
        )
        try:
            observed = json.loads(isolation.stdout)
        except (ValueError, TypeError) as exc:
            raise SandboxError("Kata observer probe could not inspect member isolation") from exc
        if not isinstance(observed, dict):
            raise SandboxError("Kata observer probe could not inspect member isolation")
        guest_kernel = manager.backend.validate_guest_kernel(observed.pop("kernel", None))
        # validate_guest_kernel refuses a shared kernel for Kata; keep
        # that requirement explicit before any guest-root helper runs.
        if guest_kernel is None:
            raise SandboxError("Kata observer probe returned no guest kernel")
        if (
            isolation.returncode != 0
            or observed
            != {
                "uid": manager.backend.uid,
                "nnp": 1,
                "seccomp": 2,
                "effective": 0,
                "permitted": 0,
            }
            or manager.backend.uid == 0
        ):
            raise SandboxError("Kata member exec lacks non-root, seccomp or no-new-privileges")
        cleanup = manager.exec_capture(
            spec,
            [MEMBER_REAPER_TARGET, "reap", str(MEMBER_UID_BASE)],
            workdir="/",
            input_bytes=b"",
            user_uid=MEMBER_UID_BASE,
            user_gid=manager.backend.gid,
            timeout_s=30,
        )
        if cleanup.returncode != 0:
            raise SandboxError(
                "Kata member process cleanup self-test failed: "
                + cleanup.stderr.decode("utf-8", "replace")[:300]
            )
        observer = manager._kata_privileged_exec(
            spec, [GUEST_OBSERVER_BINARY, "--self-test"], timeout_s=30
        )
        if observer.returncode != 0:
            raise SandboxError("Kata guest observer self-test failed: " + observer.stderr[:500])
        check_self_test(observer.stdout)
        marker = manager.exec_capture(
            spec,
            ["/usr/local/libexec/freeinference-tool-mark", "--self-test"],
            workdir="/",
            timeout_s=30,
        )
        if marker.returncode != 0 or marker.stdout.strip() != b"freeinference-tool-mark-v1":
            raise SandboxError("Kata tool marker self-test failed")
        cursor = manager.begin_guest_observation(spec, user_uid=manager.backend.uid)
        if not cursor.available:
            # `begin` folds every reason into an unavailable cursor, so
            # without its detail this reads the same whether the observer
            # died, answered a version this host rejects, or had simply not
            # published its first status yet.
            raise _ObserverProbeRace(
                "Kata tool marker transport observer is unavailable: "
                + (cursor.detail or "no detail reported")
            )
        for phase in ("pre", "post"):
            sent = manager.exec_capture(
                spec,
                [
                    "/usr/local/libexec/freeinference-tool-mark",
                    phase,
                    "/agent-state/tool-marks.log",
                ],
                input_bytes=b'{"tool_use_id":"preflight"}',
                workdir="/",
                timeout_s=30,
            )
            if sent.returncode != 0:
                raise SandboxError("Kata tool marker transport helper failed")
        observation = manager.finish_guest_observation(spec, cursor)
        delivered = [(mark.phase, mark.tool_use_id) for mark in observation.tool_marks]
        if not observation.complete or delivered != [
            ("pre", "preflight"),
            ("post", "preflight"),
        ]:
            # Two different failures wore one sentence here, which is why
            # the 2026-09-13 rollout could not be diagnosed from its logs:
            # an incomplete slice means the journal could not be read back,
            # while a complete slice with the wrong marks means the
            # boundaries themselves never arrived.
            raise _ObserverProbeRace(
                "Kata observer did not receive trusted tool boundaries: "
                f"complete={observation.complete} "
                f"lost_events={observation.lost_events} "
                f"truncated={observation.truncated} "
                f"marks={delivered!r} "
                + (f"detail={observation.detail}" if observation.detail else "")
            )
        return guest_kernel
    finally:
        manager._run(
            [manager.backend.docker_binary, "rm", "--force", spec.container_name], timeout=30
        )


def check_self_test(stdout: str) -> None:
    """Accept the helper's identity line and report its window matrix.

    The helper answers with ``freeinference-guest-observer-v<N>`` and, from
    version 2, a second line naming the live counters this guest can
    actually read. A capability this kernel lacks is reported here, where
    an operator reads preflight output, rather than being discovered as a
    permanently partial window later. It is deliberately not fatal: a
    guest without a live census still observes process lifecycles, and
    the platform offers what it can prove rather than refusing the host.
    """
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("freeinference-guest-observer-v"):
        raise SandboxError("Kata guest observer self-test failed: unrecognised helper identity")
    if len(lines) < 2:
        logger.warning(
            "Kata guest observer reports no member window capability",
            extra={"event": "guest_observer_window_capability_absent"},
        )
        return
    try:
        capability = json.loads(lines[1]).get("window") or {}
    except (ValueError, AttributeError):
        logger.warning(
            "Kata guest observer capability report is unreadable",
            extra={"event": "guest_observer_window_capability_unreadable"},
        )
        return
    missing = [name for name in ("cpu", "storage_io", "pss") if not capability.get(name)]
    if missing:
        logger.warning(
            "Kata guest cannot measure part of a member window: %s",
            ", ".join(missing),
            extra={
                "event": "guest_observer_window_capability_partial",
                "missing": missing,
            },
        )


def snapshot(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    after_poll_seq: int | None = None,
) -> GuestObserverSnapshot:
    """Read an atomic status/size/guest-clock tuple from the root journal."""
    try:
        return manager._guest_observer_controller(spec).snapshot(after_poll_seq=after_poll_seq)
    except GuestObserverRuntimeError as exc:
        raise SandboxError(str(exc)) from exc


def window_journal_delta(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    cursor: GuestObserverCursor,
    *,
    timeout_s: float = 10.0,
) -> tuple[bytes, int]:
    """Read the journal bytes this window has not consumed yet.

    Returns the payload and the offset it ended at, so a caller can keep
    one reducer per window and fold each cycle in exactly once. The read is
    fenced exactly like the observation read is: the device, inode and size
    must all still describe the same journal, because a restarted observer
    would otherwise hand back bytes from a different file under the same
    path and silently restart the counters.
    """
    if not cursor.available or not cursor.journal_ino:
        return b"", cursor.offset
    reader = (
        "import os, sys\n"
        "filename, offset, expected_dev, expected_ino = sys.argv[1], int(sys.argv[2]), "
        "int(sys.argv[3]), int(sys.argv[4])\n"
        "with open(filename, 'rb', buffering=0) as handle:\n"
        "    journal = os.fstat(handle.fileno())\n"
        "    if (int(journal.st_dev) != expected_dev "
        "            or int(journal.st_ino) != expected_ino):\n"
        "        raise RuntimeError('observer journal changed before read')\n"
        "    if int(journal.st_size) < offset:\n"
        "        raise RuntimeError('observer journal is behind the read fence')\n"
        "    handle.seek(offset)\n"
        "    sys.stdout.buffer.write(handle.read(max(0, int(journal.st_size) - offset)))\n"
    )
    result = manager.exec_capture(
        spec,
        [
            "python3",
            "-c",
            reader,
            GUEST_OBSERVER_EVENTS,
            str(cursor.offset),
            str(cursor.journal_dev),
            str(cursor.journal_ino),
        ],
        workdir="/",
        timeout_s=timeout_s,
        user_uid=0,
        user_gid=0,
    )
    if getattr(result, "returncode", 1) != 0:
        detail = getattr(result, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        raise SandboxError(f"member window journal read failed: {str(detail)[-300:]}")
    payload = getattr(result, "stdout", b"")
    if not isinstance(payload, bytes):
        payload = str(payload).encode("utf-8", "replace")
    return payload, cursor.offset + len(payload)


def guest_boot_id(manager: ChatSandboxManager, spec: ChatSandboxSpec) -> str:
    """This guest's boot identifier, or an empty string when unreadable.

    A task's identity is only unique within one boot, so every instance key
    carries it. The observer controller caches the read, so asking once per
    window costs one guest exec rather than one per census.
    """
    if manager.backend.runtime != KATA_RUNTIME:
        return ""
    try:
        return manager._guest_observer_controller(spec).guest_boot_id()
    except Exception:
        return ""


def window_request(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    argv: list[str],
    *,
    timeout_s: float,
) -> subprocess.CompletedProcess[bytes]:
    """Ask the guest observer for one member execution window operation.

    The window helper is a second reviewed binary beside the tool marker,
    and the observer accepts a window request only from it. Nothing here
    composes a command from job input: ``argv`` comes from the fixed
    ``sample``/``register``/``close`` verbs inside
    :class:`~cloud_agent_host.member_resources.MemberWindowSampler`, and
    the container is the same label-verified sandbox the turn is already
    running in.
    """
    return manager.exec_capture(
        spec,
        argv,
        workdir="/",
        timeout_s=timeout_s,
        user_uid=0,
        user_gid=0,
    )


def controller(manager: ChatSandboxManager, spec: ChatSandboxSpec) -> GuestObserverController:
    """Bind the shared observer contract to this label-verified container."""

    def capture(argv: list[str], timeout_s: float) -> subprocess.CompletedProcess[bytes]:
        return manager.exec_capture(
            spec,
            argv,
            workdir="/",
            timeout_s=timeout_s,
            user_uid=0,
            user_gid=0,
        )

    def privileged(
        argv: list[str], detach: bool, timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        return manager._kata_privileged_exec(
            spec,
            argv,
            detach=detach,
            timeout_s=timeout_s,
        )

    return GuestObserverController(
        capture=capture,
        privileged=privileged,
        lock=manager._guest_observer_lock,
    )


def begin(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    user_uid: int,
    measurement_id: str | None = None,
) -> GuestObserverCursor:
    """Fence the journal immediately before one member prompt is admitted.

    ``measurement_id`` additionally marks this fence as the start of a
    member execution window, so the observation also carries that window's
    live census cycles. The fence has to be taken before the window's
    opening census, which is why the identifier travels with it here.
    """
    if manager.backend.runtime != KATA_RUNTIME:
        return GuestObserverCursor.unavailable(user_uid, "sandbox is not a Kata guest")
    controller = manager._guest_observer_controller(spec)
    key = spec.container_name
    with manager._guest_observer_lock:
        active = manager._guest_observer_active.setdefault(key, {})
        if not active:
            controller.rotate_if_needed(threshold=GUEST_OBSERVER_ROTATE_BYTES)
        cursor = controller.begin(
            user_uid=user_uid,
            # Never sacrifice concurrent member observations to admit one
            # more turn. If the singleton is temporarily unreadable, this
            # cursor is explicitly unavailable; cleanup of existing owners
            # makes a later idle begin eligible to restart it.
            restart_if_missing=not active,
            measurement_id=measurement_id,
        )
        if cursor.available:
            # A restart invalidates every old-pid cursor already; do not
            # let abandoned bookkeeping suppress future safe rotations.
            active = {
                token: observer_pid
                for token, observer_pid in active.items()
                if observer_pid == cursor.observer_pid
            }
            active[id(cursor)] = cursor.observer_pid
            manager._guest_observer_active[key] = active
        elif not active:
            manager._guest_observer_active.pop(key, None)
        return cursor


def finish(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    cursor: GuestObserverCursor | None,
) -> GuestObservation:
    """Drain and read exactly one cursor's bounded member event slice."""
    try:
        return manager._guest_observer_controller(spec).finish(cursor)
    finally:
        if cursor is not None:
            key = spec.container_name
            with manager._guest_observer_lock:
                active = manager._guest_observer_active.get(key)
                if active is not None:
                    active.pop(id(cursor), None)
                    if not active:
                        manager._guest_observer_active.pop(key, None)
