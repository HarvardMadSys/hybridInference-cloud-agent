"""The guest observer of a retained Kata chat container, and the tool hooks it reads.

:class:`GuestObserverMixin` is the part of
:class:`~cloud_agent_host.container.chat_sandbox.ChatSandboxManager` that
qualifies a host's guest observer with a disposable probe, starts and stops
the singleton observer of each container under its lock, reads its window
journal and boot id, and installs and reads the tool marks a member's hooks
write.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from cloud_agent_host.container.chat_sandbox_create import ContainerCreationMixin
from cloud_agent_host.container.chat_sandbox_members import MEMBER_REAPER_TARGET, MemberExecMixin
from cloud_agent_host.container.chat_sandbox_models import (
    CHAT_NAMESPACE_LABEL,
    CHAT_PROBE_LABEL,
    CHAT_STATE_DIR,
    MEMBER_UID_BASE,
    ChatSandboxSpec,
    claude_member_config_dir,
)
from cloud_agent_host.container.sandbox import KATA_RUNTIME, SandboxError
from cloud_agent_host.member_setup import member_preparation
from cloud_agent_host.observe import guest_observer, tool_windows
from cloud_agent_host.observe.guest_observer_scripts import WINDOW_DELTA_SCRIPT

if TYPE_CHECKING:
    import builtins
    import subprocess
    import threading

    from cloud_agent_host.member_setup.member_preparation import MemberPreparation
    from cloud_agent_host.observe.guest_observer_locks import GuestObserverLocks


#: This module is a library boundary: it reports what it observed and
#: leaves the decision to the caller, so a warning here is the only
#: record an operator gets of a partially capable guest.
logger = logging.getLogger("cloud_agent_host.chat_sandbox")

#: A probe older than this was abandoned; the next namespace probe removes it.
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


class GuestObserverMixin(ContainerCreationMixin, MemberExecMixin):
    """Probe, run and read a chat's guest observer and tool marks; part of ``ChatSandboxManager``."""

    _guest_observers: GuestObserverLocks

    def probe_guest_observer(self) -> str | None:
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
        if self.backend.runtime != KATA_RUNTIME:
            return None
        attempts = 0
        while True:
            attempts += 1
            try:
                return self._probe_guest_observer_once()
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

    def _remove_abandoned_probes(self) -> None:
        """Remove this namespace's probes left behind by a process killed mid-probe.

        Best effort: a listing that fails leaves them for the next probe, and
        the probe about to run reports a Docker that is genuinely unusable.
        """
        listed = self.exact.run(
            [
                self.backend.docker_binary,
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
                    or row["namespace"] != self.backend.sandbox_namespace
                    or int(row["created"]) > abandoned_before
                ):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            self.exact.run([self.backend.docker_binary, "rm", "--force", container_id], timeout=30)

    def _probe_member_isolation(self, isolation: subprocess.CompletedProcess[bytes]) -> str:
        """Return the probe's guest kernel once member exec proves its isolation."""
        try:
            observed = json.loads(isolation.stdout)
        except (ValueError, TypeError) as exc:
            raise SandboxError("Kata observer probe could not inspect member isolation") from exc
        if not isinstance(observed, dict):
            raise SandboxError("Kata observer probe could not inspect member isolation")
        guest_kernel = self.backend.validate_guest_kernel(observed.pop("kernel", None))
        # validate_guest_kernel refuses a shared kernel for Kata; keep that
        # requirement explicit before any guest-root helper runs.
        if guest_kernel is None:
            raise SandboxError("Kata observer probe returned no guest kernel")
        if (
            isolation.returncode != 0
            or observed
            != {
                "uid": self.backend.uid,
                "nnp": 1,
                "seccomp": 2,
                "effective": 0,
                "permitted": 0,
            }
            or self.backend.uid == 0
        ):
            raise SandboxError("Kata member exec lacks non-root, seccomp or no-new-privileges")
        return guest_kernel

    def _probe_guest_observer_once(self) -> str:
        """One probe, on a sandbox of its own that is removed however it ends."""
        self._remove_abandoned_probes()
        token = secrets.token_hex(12)
        spec = ChatSandboxSpec(
            thread_id=f"athr_preflight_{token}",
            sandbox_id=f"asbx_preflight_{token}",
            generation=1,
            runtime_id="preflight",
            resident_slot_capacity=2,
            labels={CHAT_PROBE_LABEL: str(int(time.time()))},
        )
        command = self.build_create_command(spec)
        # Use the production creation/exec flags, without attaching any relay
        # or touching retained user state. No repository or credential enters.
        command[command.index("--network") + 1] = "none"
        created = self.exact.run(command, timeout=120)
        if created.returncode != 0:
            raise SandboxError("Kata observer probe creation failed: " + created.stderr[:300])
        try:
            started = self.exact.run(
                [self.backend.docker_binary, "start", spec.container_name], timeout=120
            )
            if started.returncode != 0:
                raise SandboxError("Kata observer probe start failed: " + started.stderr[:300])
            isolation = self.exec_capture(
                spec,
                [
                    "python3",
                    "-I",
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
            guest_kernel = self._probe_member_isolation(isolation)
            cleanup = self.exec_capture(
                spec,
                ["python3", "-I", MEMBER_REAPER_TARGET, "reap", str(MEMBER_UID_BASE)],
                workdir="/",
                input_bytes=b"",
                user_uid=MEMBER_UID_BASE,
                user_gid=self.backend.gid,
                timeout_s=30,
            )
            if cleanup.returncode != 0:
                raise SandboxError(
                    "Kata member process cleanup self-test failed: "
                    + cleanup.stderr.decode("utf-8", "replace")[:300]
                )
            observer = self._kata_privileged_exec(
                spec, [guest_observer.GUEST_OBSERVER_BINARY, "--self-test"], timeout_s=30
            )
            if observer.returncode != 0:
                raise SandboxError("Kata guest observer self-test failed: " + observer.stderr[:500])
            self._check_observer_self_test(observer.stdout)
            marker = self.exec_capture(
                spec,
                ["/usr/local/libexec/freeinference-tool-mark", "--self-test"],
                workdir="/",
                timeout_s=30,
            )
            if marker.returncode != 0 or marker.stdout.strip() != b"freeinference-tool-mark-v1":
                raise SandboxError("Kata tool marker self-test failed")
            cursor = self.begin_guest_observation(spec, user_uid=self.backend.uid)
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
                sent = self.exec_capture(
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
            observation = self.finish_guest_observation(spec, cursor)
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
            self.exact.run(
                [self.backend.docker_binary, "rm", "--force", spec.container_name], timeout=30
            )

    def _check_observer_self_test(self, stdout: str) -> None:
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

    def _observer_privileged_exec(
        self, spec: ChatSandboxSpec, argv: builtins.list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        """Keep the observer-only seam explicit for tests and call sites."""
        return self._kata_privileged_exec(spec, argv, **options)

    def _guest_observer_snapshot(
        self,
        spec: ChatSandboxSpec,
        *,
        after_poll_seq: int | None = None,
    ) -> guest_observer.GuestObserverSnapshot:
        """Read an atomic status/size/guest-clock tuple from the root journal."""
        try:
            return self._guest_observer_controller(spec).snapshot(after_poll_seq=after_poll_seq)
        except guest_observer.GuestObserverRuntimeError as exc:
            raise SandboxError(str(exc)) from exc

    def member_window_journal_delta(
        self,
        spec: ChatSandboxSpec,
        cursor: guest_observer.GuestObserverCursor,
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
        result = self.exec_capture(
            spec,
            [
                "python3",
                "-I",
                "-c",
                WINDOW_DELTA_SCRIPT,
                guest_observer.GUEST_OBSERVER_EVENTS,
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

    def guest_boot_id(self, spec: ChatSandboxSpec) -> str:
        """This guest's boot identifier, or an empty string when unreadable.

        A task's identity is only unique within one boot, so every instance key
        carries it. A window normally takes it from its cursor, whose opening
        read already carried it; this costs one guest exec, once per window,
        only for a cursor without one.
        """
        if self.backend.runtime != KATA_RUNTIME:
            return ""
        try:
            return self._guest_observer_controller(spec).guest_boot_id()
        except Exception:
            return ""

    def member_window_request(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        timeout_s: float,
    ) -> subprocess.CompletedProcess[bytes]:
        """Ask the guest observer for one member execution window operation.

        The window helper is a second reviewed binary beside the tool marker,
        and the observer accepts a window request only from it. Nothing here
        composes a command from job input: ``argv`` comes from the fixed
        ``sample``/``register``/``close`` verbs inside
        :class:`~cloud_agent_host.observe.member_resources.MemberWindowSampler`, and
        the container is the same label-verified sandbox the turn is already
        running in.
        """
        return self.exec_capture(
            spec,
            argv,
            workdir="/",
            timeout_s=timeout_s,
            user_uid=0,
            user_gid=0,
        )

    def _guest_observer_controller(
        self,
        spec: ChatSandboxSpec,
        *,
        lock: threading.RLock | None = None,
    ) -> guest_observer.GuestObserverController:
        """Bind the shared observer contract to this label-verified container.

        ``lock`` is that container's observer lock, for a controller that may
        start or rotate the helper.
        """

        def capture(
            argv: builtins.list[str], timeout_s: float
        ) -> subprocess.CompletedProcess[bytes]:
            return self.exec_capture(
                spec,
                argv,
                workdir="/",
                timeout_s=timeout_s,
                user_uid=0,
                user_gid=0,
            )

        def privileged(
            argv: builtins.list[str], detach: bool, timeout_s: float
        ) -> subprocess.CompletedProcess[str]:
            return self._observer_privileged_exec(
                spec,
                argv,
                detach=detach,
                timeout_s=timeout_s,
            )

        return guest_observer.GuestObserverController(
            capture=capture,
            privileged=privileged,
            lock=lock,
        )

    def warm_guest_observer(self, spec: ChatSandboxSpec, container_id: str) -> None:
        """Start a just-proven Kata guest's observer before its first prompt needs it."""
        from cloud_agent_host.observe.guest_observer_warmup import warm_guest_observer

        warm_guest_observer(self, spec, container_id)

    def begin_guest_observation(
        self,
        spec: ChatSandboxSpec,
        *,
        user_uid: int,
        measurement_id: str | None = None,
    ) -> guest_observer.GuestObserverCursor:
        """Fence the journal immediately before one member prompt is admitted.

        ``measurement_id`` additionally marks this fence as the start of a
        member execution window, so the observation also carries that window's
        live census cycles. The fence has to be taken before the window's
        opening census, which is why the identifier travels with it here.
        """
        if self.backend.runtime != KATA_RUNTIME:
            return guest_observer.GuestObserverCursor.unavailable(
                user_uid, "sandbox is not a Kata guest"
            )
        with self._guest_observers.container(spec.container_name) as observers:
            controller = self._guest_observer_controller(spec, lock=observers.lock)
            idle = not observers.active
            cursor = controller.begin(
                user_uid=user_uid,
                # Never sacrifice concurrent member observations to admit one
                # more turn. If the singleton is temporarily unreadable, this
                # cursor is explicitly unavailable; cleanup of existing owners
                # makes a later idle begin eligible to restart it.
                restart_if_missing=idle,
                # Rotation restarts the helper too: never under an open cursor.
                rotate_threshold=guest_observer.GUEST_OBSERVER_ROTATE_BYTES if idle else None,
                measurement_id=measurement_id,
            )
            observers.admit(cursor)
            return cursor

    def finish_guest_observation(
        self,
        spec: ChatSandboxSpec,
        cursor: guest_observer.GuestObserverCursor | None,
    ) -> guest_observer.GuestObservation:
        """Drain and read exactly one cursor's bounded member event slice."""
        try:
            return self._guest_observer_controller(spec).finish(cursor)
        finally:
            if cursor is not None:
                with self._guest_observers.container(spec.container_name) as observers:
                    observers.release(cursor)

    def tool_mark_reader(self, spec: ChatSandboxSpec) -> tool_windows.ToolMarkReader:
        """Return a bounded reader for hook marks inside one retained container."""
        if self.backend.runtime == KATA_RUNTIME:
            # Kata accepts marks only through the guest-root observer socket.
            # The agent-owned compatibility file is deliberately not an
            # attribution authority in an untrusted sandbox.
            return tool_windows.ToolMarkReader(None)
        path = f"{CHAT_STATE_DIR}/{tool_windows.TOOL_MARK_FILE}"

        def remote_size() -> int:
            script = (
                "import os, sys\n"
                "try:\n"
                "    size = os.path.getsize(sys.argv[1])\n"
                "except OSError:\n"
                "    size = 0\n"
                "print(size)\n"
            )
            result = self.exec_capture(
                spec,
                ["python3", "-I", "-c", script, path],
                workdir="/",
                timeout_s=5,
                user_uid=self.backend.uid,
                user_gid=self.backend.gid,
            )
            if result.returncode != 0:
                raise SandboxError("tool-mark fence could not be read")
            return int(result.stdout.decode("ascii", "strict").strip() or "0")

        def remote_read(offset: int, count: int) -> bytes:
            bounded = min(max(0, count), tool_windows.MAX_TOOL_MARK_BYTES)
            script = (
                "import os, sys\n"
                "path, offset, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])\n"
                "try:\n"
                "    with open(path, 'rb', buffering=0) as handle:\n"
                "        handle.seek(offset)\n"
                "        sys.stdout.buffer.write(handle.read(count))\n"
                "except FileNotFoundError:\n"
                "    pass\n"
            )
            result = self.exec_capture(
                spec,
                ["python3", "-I", "-c", script, path, str(max(0, offset)), str(bounded)],
                workdir="/",
                timeout_s=5,
                user_uid=self.backend.uid,
                user_gid=self.backend.gid,
            )
            if result.returncode != 0 or len(result.stdout) > bounded:
                raise SandboxError("tool-mark slice could not be read")
            return result.stdout

        return tool_windows.ToolMarkReader(None, remote_size=remote_size, remote_read=remote_read)

    def install_tool_hooks(
        self,
        spec: ChatSandboxSpec,
        update: dict[str, Any],
        *,
        slot_id: int,
        thread_id: str,
        batch: MemberPreparation | None = None,
    ) -> None:
        """Atomically merge Claude hooks into this member's private settings.

        Docker-owned retained state has no corresponding host path. Writing it
        with :mod:`pathlib` on the sandbox manager would configure the sandbox manager itself,
        not the Agent CLI. Settings are never imported from the old shared
        Claude root. The group-writable marks file supports trusted non-Kata
        development; Kata accepts only its guest-root observer journal.

        Behind steps already queued on ``batch``, the merge joins their exec,
        and its failure is reported there instead of raised: the hooks are an
        observability upgrade, never worth a user's launch.
        """
        uid = MEMBER_UID_BASE + slot_id
        self._validate_member_identity(spec, slot_id=slot_id, user_uid=uid)
        payload = json.dumps(update, separators=(",", ":")).encode()
        if len(payload) > 64 * 1024:
            raise SandboxError("tool hook settings exceed their trusted bound")
        queued = batch is not None and batch.pending
        step = member_preparation.MemberStep(
            (
                "python3",
                "-I",
                "-c",
                member_preparation.TOOL_HOOKS_SCRIPT,
                claude_member_config_dir(slot_id, thread_id),
                f"{CHAT_STATE_DIR}/{tool_windows.TOOL_MARK_FILE}",
            ),
            timeout_s=10,
            failure=lambda stderr: SandboxError(
                "tool hooks could not be installed in retained state: "
                + stderr.decode("utf-8", "replace")[-300:]
            ),
            input_bytes=payload,
            soft="tool hooks not installed" if queued else "",
        )
        member_preparation.run_member_steps(
            self,
            spec,
            step,
            user_uid=uid,
            user_gid=self.backend.gid,
            batch=batch if queued else None,
        )


__all__ = [
    "GUEST_OBSERVER_PROBE_ATTEMPTS",
    "GUEST_OBSERVER_PROBE_RETRY_S",
    "PROBE_ABANDONED_AFTER_S",
    "GuestObserverMixin",
]
