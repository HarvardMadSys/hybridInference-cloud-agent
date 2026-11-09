"""Commands run in a retained chat container as its members, and their processes.

:class:`MemberExecMixin` is the part of
:class:`~cloud_agent_host.container.chat_sandbox.ChatSandboxManager` that runs
``docker exec`` inside one exact container: bounded captures, attached
processes and member-to-member pipes, each environment value passed by name
rather than in argv. It also owns what a member's processes leave behind: the
PID/start-time receipts, the reaper that installs into older containers, the
census and reap around each activation, the writer freeze around a capture,
and the owner inference script each member's bash reads.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from typing import TYPE_CHECKING

from cloud_agent_host.bounded_output import OutputPump, TextTail, protocol_lines, text_chunks
from cloud_agent_host.container import chat_sandbox_writers as writers
from cloud_agent_host.container.chat_sandbox_exec import spawn_target
from cloud_agent_host.container.chat_sandbox_models import (
    ACTIVATION_TMPFS,
    CHAT_WORKDIR,
    MEMBER_TMPFS_ROOT,
    MEMBER_UID_BASE,
    ChatSandboxSpec,
    MemberProcessReceiptMissing,
)
from cloud_agent_host.container.guest_root_exec import GuestRootExec
from cloud_agent_host.container.sandbox import (
    KATA_RUNTIME,
    ContainerBackend,
    SandboxError,
    SandboxProcess,
)
from cloud_agent_host.container.stream_input import interactive_input
from cloud_agent_host.member_setup import member_preparation
from cloud_agent_host.member_setup.install_locks import install_lock
from cloud_agent_shared.environment import (
    DOCKER_CLIENT_ENV as _DOCKER_CLIENT_ENV,
    DOCKER_CLIENT_PREFIXES as _DOCKER_CLIENT_ENV_PREFIXES,
    PROXY_ENV,
)
from cloud_agent_shared.operation_profiling import StageObserver, substage

if TYPE_CHECKING:
    import builtins
    from collections.abc import Iterator
    from typing import BinaryIO

    from cloud_agent_host.member_setup.member_preparation import MemberPreparation


MEMBER_REAPER_TARGET = "/usr/local/bin/member-process-reaper"
MEMBER_REAPER_SOURCE = "/usr/local/libexec/member-process-reaper"
_UNPRIVILEGED_EXEC = ("/usr/bin/setpriv", "--no-new-privs", "--")
# A sandbox environment reaches ``docker exec`` by name (``--env KEY``), its
# value through the Docker CLI's own environment, so a member's inference or
# MCP credential never sits in the client's argv (``/proc/<pid>/cmdline``,
# ``ps``, process accounting) for the life of a resident CLI. Only the names
# the Docker CLI itself reads -- its endpoint and config directory, executable,
# plugin and credential-helper lookup, TLS roots, proxies, the Go runtime and
# the dynamic loader -- still travel as ``KEY=VALUE``: a sandbox value under one
# of them must not reconfigure the client, and none of them carries a secret.


_PROXY_ENV = frozenset(name.upper() for name in PROXY_ENV)


def _exec_env_by_name(key: str) -> bool:
    """Whether one sandbox variable crosses ``docker exec`` as a bare name."""
    return not (
        key in _DOCKER_CLIENT_ENV
        or key.upper() in _PROXY_ENV
        or key.startswith(_DOCKER_CLIENT_ENV_PREFIXES)
    )


def member_inference_env_path(slot_id: int) -> str:
    """Where one member's owner inference credentials wait for its bash."""
    return f"{MEMBER_TMPFS_ROOT}/{slot_id}/inference-env.sh"


def _reap_step(user_uid: int, baseline: bytes) -> member_preparation.MemberStep:
    """Reap every process of ``user_uid`` that ``baseline`` does not name."""
    return member_preparation.MemberStep(
        ("python3", "-I", MEMBER_REAPER_TARGET, "reap", str(user_uid)),
        timeout_s=30,
        failure=lambda stderr: SandboxError(
            "member activation process cleanup failed: " + stderr.decode("utf-8", "replace")[:300]
        ),
        input_bytes=baseline,
        reaps=True,
    )


class MemberExecMixin(GuestRootExec):
    """Run bounded execs as a chat's members and fence their processes; part of ``ChatSandboxManager``."""

    backend: ContainerBackend

    def build_exec_command(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        interactive: bool = False,
        tty: bool = False,
        user_uid: int | None = None,
        user_gid: int | None = None,
        container_id: str | None = None,
    ) -> builtins.list[str]:
        """Compose ``docker exec`` after identity/running checks at spawn time.

        Most of ``env`` is named here and valued only in the client environment
        :meth:`exec_client_env` builds; run the command with that environment.
        """
        if not argv:
            raise ValueError("a retained sandbox exec needs an argv")
        command = [self.backend.docker_binary, "exec"]
        if interactive:
            command.append("--interactive")
        if tty:
            command.append("--tty")
        command += [
            "--user",
            f"{user_uid if user_uid is not None else self.backend.uid}:"
            f"{user_gid if user_gid is not None else self.backend.gid}",
            "--workdir",
            workdir,
        ]
        for key, value in sorted((env or {}).items()):
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError(f"invalid environment entry {key!r}")
            command += ["--env", key if _exec_env_by_name(key) else f"{key}={value}"]
        command.append(container_id or spec.container_name)
        if self.backend.runtime == KATA_RUNTIME:
            command += list(_UNPRIVILEGED_EXEC)
            if not interactive:
                # Kata leaves an unattached stdin pipe open. Give commands
                # that have no input a real EOF, without interpolating argv.
                command += ["/bin/sh", "-c", 'exec "$@" </dev/null', "sh"]
        command += argv
        return command

    @staticmethod
    def exec_client_env(env: dict[str, str] | None = None) -> dict[str, str]:
        """Return the Docker CLI environment that supplies a named ``env``.

        The sandbox manager's own environment still configures the client; only the
        values :meth:`build_exec_command` passed by name are added to it.
        """
        client = {**os.environ}
        client.update({key: value for key, value in (env or {}).items() if _exec_env_by_name(key)})
        return client

    def spawn(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        live_input: bool = False,
        user_uid: int | None = None,
        user_gid: int | None = None,
        process_pidfile: str | None = None,
    ) -> SandboxProcess:
        """Run an attached command inside the retained container."""
        current, env = spawn_target(self, spec, env)
        exec_argv = argv
        if process_pidfile is not None:
            self._validate_member_pidfile(spec, process_pidfile, user_uid=user_uid)
            # The CLI is a session leader, so its complete child tree can be
            # interrupted without stopping sibling sessions in this container.
            # Persist both PID and Linux start time: a stale numeric PID must
            # never be allowed to signal a later, unrelated process.
            wrapper = (
                'pidfile="$1"; shift; temporary="$pidfile.tmp.$$"; umask 0077; '
                'cleanup() { rm -f -- "$pidfile" "$temporary"; }; '
                "trap cleanup EXIT; "
                "setsid --fork --wait /bin/sh -c '"
                'pidfile="$1"; temporary="$2"; shift 2; '
                'fields=$(sed "s/^[^)]*) //" "/proc/$$/stat") || exit 126; '
                # Read field 22 (start time; field 20 after stripping pid and
                # comm) in a subshell.  Doing ``set -- $fields`` in this shell
                # would overwrite the CLI argv that must remain in ``$@``.
                'started=$(set -- $fields; printf "%s" "${20:-}"); '
                'case "$started" in ""|*[!0-9]*) exit 126;; esac; '
                'printf "%s %s\\n" "$$" "$started" > "$temporary" || exit 126; '
                'mv -f -- "$temporary" "$pidfile" || exit 126; '
                'exec "$@"\' sh "$pidfile" "$temporary" "$@"'
            )
            exec_argv = ["/bin/sh", "-c", wrapper, "sh", process_pidfile, *argv]
        command = self.build_exec_command(
            spec,
            exec_argv,
            env=env,
            workdir=workdir,
            interactive=live_input,
            user_uid=user_uid,
            user_gid=user_gid,
            container_id=current.container_id,
        )
        process = subprocess.Popen(
            command,
            env=self.exec_client_env(env),
            stdin=subprocess.PIPE if live_input else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return _RetainedExecProcess(
            process,
            manager=self,
            spec=spec,
            process_pidfile=process_pidfile,
            user_uid=user_uid,
            user_gid=user_gid,
        )

    def _validate_member_pidfile(
        self,
        spec: ChatSandboxSpec,
        pidfile: str,
        *,
        user_uid: int | None,
    ) -> int:
        """Validate one member resident or bounded retained-command PID path."""
        command_match = re.fullmatch(
            rf"{re.escape(ACTIVATION_TMPFS)}/command-[0-9a-f]{{32}}\.pid",
            pidfile,
        )
        if command_match is not None:
            if user_uid != self.backend.uid:
                raise ValueError("command PID file does not match the sandbox uid")
            return -1
        member_match = re.fullmatch(
            rf"{re.escape(MEMBER_TMPFS_ROOT)}/([0-9]+)/resident-[0-9a-f]{{32}}\.pid",
            pidfile,
        )
        if member_match is None:
            raise ValueError("process-scoped kill requires a shared member PID file")
        slot_id = int(member_match.group(1))
        if slot_id >= spec.resident_slot_capacity:
            raise ValueError("resident PID file is outside sandbox capacity")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident PID file does not match its member uid")
        return slot_id

    def write_member_inference_env(
        self,
        spec: ChatSandboxSpec,
        script: str,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
    ) -> str:
        """Write one member's ``BASH_ENV`` script of owner inference credentials.

        The member tmpfs is private to that uid and gone with the container; the
        values travel over stdin, never argv. Returns the path to point
        ``BASH_ENV`` at.
        """
        if not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")
        path = member_inference_env_path(slot_id)
        write_script = (
            'set -eu; target="$1"; temporary="$target.tmp.$$"; umask 077; '
            "trap 'rm -f -- \"$temporary\"' EXIT HUP INT TERM; "
            'cat > "$temporary"; mv "$temporary" "$target"; '
            "trap - EXIT HUP INT TERM"
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", write_script, "sh", path],
            workdir="/",
            timeout_s=20,
            input_bytes=script.encode("utf-8"),
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "member inference environment could not be written: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )
        return path

    def ensure_member_process_reaper(self, spec: ChatSandboxSpec) -> None:
        """Install the immutable helper into retained pre-upgrade containers.

        Found or installed once per proven container (``ExactContainer.note_helper``),
        then not probed again on each turn's census and reap.
        """
        proven = self.exact.proven_container(spec)
        if self.exact.holds_helper(proven, MEMBER_REAPER_TARGET):
            return
        with install_lock(spec.container_name):
            probe = self.exec_capture(
                spec,
                [
                    "/bin/sh",
                    "-c",
                    'test "$(stat -c %u:%g:%a "$1" 2>/dev/null)" = 0:0:555',
                    "sh",
                    MEMBER_REAPER_TARGET,
                ],
                workdir="/",
                timeout_s=20,
                user_uid=0,
                user_gid=0,
            )
            if probe.returncode == 0:
                self.exact.note_helper(proven, MEMBER_REAPER_TARGET)
                return
            current = self.exact.require(spec)
            if not current.running:
                raise SandboxError("chat sandbox must be started before helper installation")
            source = os.environ.get("AGENT_MEMBER_PROCESS_REAPER_SOURCE", MEMBER_REAPER_SOURCE)
            if not os.path.isfile(source):
                raise SandboxError(
                    "retained sandbox lacks member-process-reaper and the trusted "
                    f"runner source {source!r} is unavailable"
                )
            temporary = f"{MEMBER_REAPER_TARGET}.install-{secrets.token_hex(8)}"
            copied = self.exact.run(
                [
                    self.backend.docker_binary,
                    "cp",
                    source,
                    f"{current.container_id}:{temporary}",
                ],
                timeout=30,
            )
            if copied.returncode != 0:
                raise SandboxError(
                    "member-process-reaper compatibility install failed: "
                    + copied.stderr.strip()[:300]
                )
            installed = self.exec_capture(
                spec,
                [
                    "/bin/sh",
                    "-c",
                    'chown 0:0 "$1" && chmod 0555 "$1" && mv -f "$1" "$2"',
                    "sh",
                    temporary,
                    MEMBER_REAPER_TARGET,
                ],
                workdir="/",
                timeout_s=20,
                user_uid=0,
                user_gid=0,
            )
            if installed.returncode != 0:
                with contextlib.suppress(Exception):
                    self.exec_capture(
                        spec,
                        ["/bin/rm", "-f", temporary],
                        workdir="/",
                        timeout_s=10,
                        user_uid=0,
                        user_gid=0,
                    )
                raise SandboxError(
                    "member-process-reaper compatibility activation failed: "
                    + installed.stderr.decode("utf-8", "replace")[:300]
                )
            self.exact.note_helper(proven, MEMBER_REAPER_TARGET)

    def ensure_codex_mcp_policy(self, spec: ChatSandboxSpec, base_url: str) -> None:
        """Allow only this deployment's relay before starting a Codex member."""
        from cloud_agent_host.runtimes.codex.mcp import install_policy_argv, relay_endpoint

        endpoint = relay_endpoint(base_url)
        result = self.exec_capture(
            spec,
            install_policy_argv(endpoint),
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
        )
        if result.returncode != 0:
            raise SandboxError("could not install the isolated Codex MCP relay policy")

    def reap_member_processes(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
        batch: MemberPreparation | None = None,
    ) -> None:
        """Kill unattached live CLI trees in exactly one private member slot.

        Every launch reaps its slot first, which also drops the owner inference
        credentials an earlier launch left for its bash: a launch with them
        writes them again, and one whose owner removed them must not find them.
        The reap and the removal after it run in one exec, or are queued on
        ``batch`` with the rest of the member's preparation.
        """
        if not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")
        stale = member_preparation.MemberStep(
            (
                "/bin/sh",
                "-c",
                f"rm -f -- {MEMBER_TMPFS_ROOT}/{slot_id}/resident-*.pid "
                f"{member_inference_env_path(slot_id)}",
            ),
            timeout_s=20,
            failure=lambda stderr: SandboxError(
                "stale resident process cleanup failed: " + stderr.decode("utf-8", "replace")[:300]
            ),
        )
        member_preparation.run_member_steps(
            self,
            spec,
            _reap_step(user_uid, b""),
            stale,
            user_uid=user_uid,
            user_gid=user_gid,
            batch=batch,
        )

    def capture_member_processes(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
    ) -> tuple[tuple[int, int], ...]:
        """Capture the reviewed resident PID/start-time set before one turn."""
        self._validate_member_identity(spec, slot_id=slot_id, user_uid=user_uid)
        self.ensure_member_process_reaper(spec)
        result = self.exec_capture(
            spec,
            ["python3", "-I", MEMBER_REAPER_TARGET, "capture", str(user_uid)],
            workdir="/",
            timeout_s=20,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "member process census failed: " + result.stderr.decode("utf-8", "replace")[:300]
            )
        return self._parse_member_processes(result.stdout)

    def await_member_launch(
        self,
        spec: ChatSandboxSpec,
        *,
        pidfile: str,
        launcher: str,
        user_uid: int,
        user_gid: int,
    ) -> None:
        """Return once the process ``pidfile`` names has stopped being ``launcher``.

        The member's request meter starts in front of its CLI: the launcher
        forks the meter, waits until it serves, then replaces itself with the
        CLI. Until it has, the meter may not exist yet, and a turn's process
        census taken then leaves it out, so the turn's reap kills it as the
        turn's own leftover. Once the receipt's process no longer runs the
        launcher, the meter is up, and a census includes it.
        """
        self._validate_member_pidfile(spec, pidfile, user_uid=user_uid)
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", _AWAIT_LAUNCH_SCRIPT, "sh", pidfile, launcher],
            workdir="/",
            timeout_s=_AWAIT_LAUNCH_TIMEOUT_S + 5,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            stderr = (
                result.stderr.decode("utf-8", "replace")
                if isinstance(result.stderr, bytes)
                else str(result.stderr or "")
            )
            raise SandboxError(
                f"member CLI launch did not finish (exit {result.returncode}): {stderr[:300]}"
            )

    def reap_member_processes_except(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
        baseline: tuple[tuple[int, int], ...],
    ) -> None:
        """Reap every member process not present in a fenced baseline.

        This is the activation boundary for background tools. It runs as the
        member UID (the container has no CAP_KILL), validates Linux start times
        to defeat PID reuse, and repeats through SIGKILL so a process cannot
        escape merely by changing process groups or forking during cleanup.
        """
        self._validate_member_identity(spec, slot_id=slot_id, user_uid=user_uid)
        step = _reap_step(user_uid, self._encode_member_processes(baseline))
        member_preparation.run_member_steps(self, spec, step, user_uid=user_uid, user_gid=user_gid)

    @staticmethod
    def _validate_member_identity(
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
    ) -> None:
        if not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")

    @staticmethod
    def _parse_member_processes(payload: bytes) -> tuple[tuple[int, int], ...]:
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SandboxError("member process census is not ASCII") from exc
        found: builtins.list[tuple[int, int]] = []
        seen: set[int] = set()
        for line in text.splitlines():
            fields = line.split()
            if len(fields) != 2 or any(
                not field.isascii() or not field.isdigit() for field in fields
            ):
                raise SandboxError("member process census is malformed")
            pid, started = (int(field) for field in fields)
            if pid <= 1 or started <= 0 or pid in seen or len(found) >= 4096:
                raise SandboxError("member process census is outside its identity fence")
            seen.add(pid)
            found.append((pid, started))
        return tuple(sorted(found))

    @staticmethod
    def _encode_member_processes(processes: tuple[tuple[int, int], ...]) -> bytes:
        canonical = MemberExecMixin._parse_member_processes(
            "".join(f"{pid} {started}\n" for pid, started in processes).encode("ascii")
        )
        return "".join(f"{pid} {started}\n" for pid, started in canonical).encode("ascii")

    def operation_writer_uids(self, spec: ChatSandboxSpec) -> builtins.list[int]:
        """Enumerate active operation identities without reading their environments."""
        return writers.operation_writer_uids(self, spec)

    def freeze_shared_writers(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_ids: builtins.list[int],
        token: str,
        expected_container_id: str | None = None,
    ) -> str:
        """Stop every existing shared-workspace writer for one short capture.

        Agent processes use per-member UIDs, while terminals/setup commands
        use the sandbox UID.  Freezing the complete existing UID census closes
        the detached-child gap that a process-group-only barrier leaves.  The
        Git capture exec starts after this census and is the sole new writer;
        admissions remain blocked by the supervisor until thaw completes.
        """
        return writers.freeze(
            self,
            spec,
            slot_ids=slot_ids,
            token=token,
            expected_container_id=expected_container_id,
        )

    def thaw_shared_writers(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_ids: builtins.list[int],
        manifest: str,
        require_exact_census: bool = True,
        expected_container_id: str | None = None,
    ) -> None:
        """Resume exactly the frozen PID/start pairs and detect new writers."""
        writers.thaw(
            self,
            spec,
            slot_ids=slot_ids,
            manifest=manifest,
            require_exact_census=require_exact_census,
            expected_container_id=expected_container_id,
        )

    def kill_member_process(
        self,
        spec: ChatSandboxSpec,
        *,
        pidfile: str,
        user_uid: int,
        user_gid: int,
    ) -> None:
        """Signal one verified resident or command process group, never the container."""
        self._validate_member_pidfile(spec, pidfile, user_uid=user_uid)
        argv = [
            "/bin/sh",
            "-c",
            self._member_reaper_script('"$2"'),
            "sh",
            str(user_uid),
            pidfile,
        ]
        result = (
            self._kata_privileged_exec(spec, argv, timeout_s=20)
            if self.backend.runtime == KATA_RUNTIME
            else self.exec_capture(
                spec,
                argv,
                workdir="/",
                timeout_s=20,
                user_uid=user_uid,
                user_gid=user_gid,
            )
        )
        if result.returncode != 0:
            stderr = (
                result.stderr.decode("utf-8", "replace")
                if isinstance(result.stderr, bytes)
                else str(result.stderr or "")
            )
            raise _member_kill_failure(result.returncode, stderr)

    @staticmethod
    def _member_reaper_script(pidfile_expression: str) -> str:
        """Build an exact PID/start receipt wait, verification, and group kill."""
        return (
            'set -eu; expected_uid="$1"; '
            # kill -0 sees zombies too. Docker's init normally reaps them,
            # but a delayed reap is not a surviving command. Examine state
            # before declaring cleanup failed, without requiring procps.
            'group_alive() { kill -0 "-$pid" 2>/dev/null || return 1; '
            "for task in /proc/[0-9]*/stat; do "
            'read -r raw < "$task" 2>/dev/null || continue; '
            'set -- ${raw##*) }; [ "${3:-}" = "$pid" ] || continue; '
            'case "${1:-}" in Z|X) continue;; esac; return 0; '
            "done; return 1; }; "
            f"for pidfile in {pidfile_expression}; do "
            'attempt=0; ready=0; pid=""; started=""; '
            'while [ "$attempt" -lt 40 ]; do '
            'receipt=$(cat -- "$pidfile" 2>/dev/null || :); set -- $receipt; '
            'if [ "$#" -eq 2 ]; then pid="$1"; started="$2"; '
            'case "$pid" in ""|*[!0-9]*) ;; *) case "$started" in '
            '""|*[!0-9]*) ;; *) ready=1; break;; esac;; esac; fi; '
            "attempt=$((attempt + 1)); sleep 0.05; done; "
            '[ "$ready" = 1 ] || exit 125; '
            '[ "$(stat -c %u "/proc/$pid" 2>/dev/null || :)" = "$expected_uid" ] '
            '|| { rm -f -- "$pidfile"; continue; }; '
            'fields=$(sed "s/^[^)]*) //" "/proc/$pid/stat" 2>/dev/null || :); '
            "set -- $fields; current_pgid=${3:-}; current_started=${20:-}; "
            '[ "$current_pgid" = "$pid" ] && [ "$current_started" = "$started" ] '
            '|| { rm -f -- "$pidfile"; exit 126; }; '
            'kill -TERM "-$pid" 2>/dev/null || :; attempt=0; '
            "while group_alive; do attempt=$((attempt + 1)); "
            '[ "$attempt" -lt 40 ] || { kill -KILL "-$pid" 2>/dev/null || :; '
            "attempt=0; while group_alive; do "
            'attempt=$((attempt + 1)); [ "$attempt" -lt 100 ] || exit 124; '
            "sleep 0.05; done; break; }; "
            'sleep 0.05; done; rm -f -- "$pidfile"; '
            "done"
        )

    def exec_capture(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        timeout_s: float = 60,
        input_bytes: bytes | None = None,
        stdin: BinaryIO | None = None,
        user_uid: int | None = None,
        user_gid: int | None = None,
        observer: StageObserver | None = None,
        expected_container_id: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one bounded command and capture bytes without opening a host path.

        ``observer`` times the exec as one stage covering the attach, the
        process start and the command's own run. That is a combination and is
        reported as one: it is never presented as pure disk-read time, and
        splitting it further would need the exec'd helper to report its own
        sub-durations rather than a second exec to probe them.

        ``expected_container_id`` fences an archive or lifecycle read on that
        exact container, inspected first; otherwise a proven container is
        entered by id without an inspection (:meth:`ExactContainer.exec_target`).
        """
        current, proven = self.exact.exec_target(spec, expected_container_id)
        interactive = interactive_input(input_bytes, stdin, self.backend.runtime == KATA_RUNTIME)
        command = self.build_exec_command(
            spec,
            argv,
            env=env,
            workdir=workdir,
            interactive=interactive,
            user_uid=user_uid,
            user_gid=user_gid,
            container_id=current.container_id,
        )
        try:
            with substage(observer, "exec"):
                result = subprocess.run(
                    command,
                    env=self.exec_client_env(env),
                    input=input_bytes,
                    stdin=stdin,
                    capture_output=True,
                    timeout=timeout_s,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"chat sandbox exec failed: {exc}") from exc
        return self.exact.checked_exec(spec, proven, result)

    def pipe_member_exec(
        self,
        spec: ChatSandboxSpec,
        *,
        source_argv: builtins.list[str],
        source_uid: int,
        target_argv: builtins.list[str],
        target_uid: int,
        user_gid: int,
        timeout_s: float,
    ) -> tuple[subprocess.CompletedProcess[bytes], subprocess.CompletedProcess[bytes]]:
        """Stream one member exec's stdout into another member exec's stdin.

        The two commands meet only in a pipe on this host, so a file private to
        one member UID can reach another member without being widened inside
        the container or staged where a sibling could read it. Both run in the
        one inspected container. Returns ``(source, target)`` results carrying
        each command's bounded stderr tail; the target's stdout is discarded.
        """
        current, proven = self.exact.exec_target(spec)
        commands = (
            self.build_exec_command(
                spec,
                source_argv,
                workdir="/",
                user_uid=source_uid,
                user_gid=user_gid,
                container_id=current.container_id,
            ),
            self.build_exec_command(
                spec,
                target_argv,
                workdir="/",
                interactive=True,
                user_uid=target_uid,
                user_gid=user_gid,
                container_id=current.container_id,
            ),
        )
        deadline = time.monotonic() + timeout_s
        processes: builtins.list[subprocess.Popen[bytes]] = []
        with (
            tempfile.TemporaryFile(prefix="freeinference-member-pipe-") as source_stderr,
            tempfile.TemporaryFile(prefix="freeinference-member-pipe-") as target_stderr,
        ):
            try:
                source = subprocess.Popen(
                    commands[0],
                    env=self.exec_client_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=source_stderr,
                )
                processes.append(source)
                assert source.stdout is not None
                try:
                    processes.append(
                        subprocess.Popen(
                            commands[1],
                            env=self.exec_client_env(),
                            stdin=source.stdout,
                            stdout=subprocess.DEVNULL,
                            stderr=target_stderr,
                        )
                    )
                finally:
                    # The target owns the read end now; a target that exits
                    # early ends the source on a broken pipe.
                    source.stdout.close()
                codes = [
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                    for process in reversed(processes)
                ]
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SandboxError(f"chat sandbox member pipe failed: {exc}") from exc
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            process.wait(timeout=5)
            results = []
            for command, code, stderr in zip(
                commands, reversed(codes), (source_stderr, target_stderr), strict=True
            ):
                stderr.seek(max(0, stderr.seek(0, os.SEEK_END) - 4096))
                results.append(subprocess.CompletedProcess(command, code, b"", stderr.read()))
        # Either exec that failed is explained as a lone exec's failure would be.
        self.exact.checked_exec(spec, proven, results[0] if results[0].returncode else results[1])
        return results[0], results[1]


#: How long a launcher may take to start its meter and become the CLI.
_AWAIT_LAUNCH_TIMEOUT_S = 15
#: ``$1`` the receipt, ``$2`` the launcher's path. Exit 125: no receipt in
#: time; 126: the process ended, or its PID now names another; 124: the
#: launcher never became the CLI. The receipt's start time fences PID reuse.
#: An exiting process has an empty command line before it turns Z, so only a
#: non-empty one that is not the launcher's is the CLI; an empty one is looked
#: at again, and then reads as ended.
_AWAIT_LAUNCH_SCRIPT = (
    'set -u; pidfile="$1"; launcher="$2"; attempt=0; '
    f"limit={_AWAIT_LAUNCH_TIMEOUT_S * 50}; "
    "while :; do "
    'receipt=$(cat -- "$pidfile" 2>/dev/null || :); set -- $receipt; '
    '[ "$#" -eq 2 ] && break; '
    'attempt=$((attempt + 1)); [ "$attempt" -lt "$limit" ] || exit 125; sleep 0.02; done; '
    'pid="$1"; started="$2"; '
    "while :; do "
    'fields=$(sed "s/^[^)]*) //" "/proc/$pid/stat" 2>/dev/null) || exit 126; '
    "set -- $fields; "
    'case "${1:-}" in Z|X) exit 126;; esac; [ "${20:-}" = "$started" ] || exit 126; '
    'argv=$(tr "\\000" "\\n" < "/proc/$pid/cmdline" 2>/dev/null) || exit 126; '
    'if [ -n "$argv" ]; then printf "%s\\n" "$argv" | grep -qxF -- "$launcher" || exit 0; fi; '
    'attempt=$((attempt + 1)); [ "$attempt" -lt "$limit" ] || exit 124; sleep 0.02; done'
)
#: The member reaper's exit when no process receipt appeared in its wait.
_REAPER_NO_RECEIPT = 125

#: How long a killed command whose receipt is gone may take to finish exiting.
_RECEIPT_EXIT_GRACE_S = 5.0


def _member_kill_failure(returncode: int, stderr: str) -> SandboxError:
    """Name a failed member reaper run.

    Only the reaper's own silent exit means the receipt is missing: a Docker
    exec failure that shares the code always explains itself on stderr.
    """
    if returncode == _REAPER_NO_RECEIPT and not stderr.strip():
        return MemberProcessReceiptMissing("resident process receipt is missing")
    return SandboxError("resident process cleanup failed: " + stderr[:300])


class _RetainedExecProcess(SandboxProcess):
    """An attached ``docker exec`` with container- or member-scoped cleanup."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        manager: MemberExecMixin,
        spec: ChatSandboxSpec,
        process_pidfile: str | None,
        user_uid: int | None,
        user_gid: int | None,
    ) -> None:
        self._process = process
        self._manager = manager
        self._spec = spec
        self._process_pidfile = process_pidfile
        self._user_uid = user_uid
        self._user_gid = user_gid
        self._stderr_tail = TextTail()
        self._stderr_thread: threading.Thread | None = None
        if process.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                daemon=True,
                name="retained-sandbox-stderr",
            )
            self._stderr_thread.start()

    @property
    def container(self) -> str:
        return self._spec.container_name

    def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        for chunk in text_chunks(self._process.stderr):
            self._stderr_tail.append(self.sieve_stderr(chunk))

    def lines(self) -> Iterator[str]:
        try:
            yield from protocol_lines(self.chunks())
        except SandboxError:
            # Docker's attached exec channels can backpressure cleanup execs.
            # Discard any remaining stdout while the scoped reaper runs.
            drain = OutputPump(self.chunks(), name="oversized-resident-output")
            drain.close(drain=True)
            self.kill()
            raise

    def chunks(self) -> Iterator[str]:
        assert self._process.stdout is not None
        yield from text_chunks(self._process.stdout)

    def byte_chunks(self) -> Iterator[bytes]:
        yield from self._pipe_bytes(self._process.stdout)

    def write_line(self, text: str) -> None:
        if self._process.stdin is None:
            raise SandboxError("this retained exec was not spawned with live input")
        self._process.stdin.write(text.rstrip("\n") + "\n")
        self._process.stdin.flush()

    def close_stdin(self) -> None:
        if self._process.stdin is not None:
            with contextlib.suppress(OSError):
                self._process.stdin.close()

    def poll(self) -> int | None:
        return self._process.poll()

    def kill(self) -> None:
        """Kill only this verified exec process group, never its container."""
        try:
            if self._process_pidfile is None:
                raise SandboxError("retained exec has no process-scoped kill receipt")
            assert self._user_uid is not None and self._user_gid is not None
            try:
                self._manager.kill_member_process(
                    self._spec,
                    pidfile=self._process_pidfile,
                    user_uid=self._user_uid,
                    user_gid=self._user_gid,
                )
            except MemberProcessReceiptMissing:
                # A resident CLI exits on its own after stdin EOF, and its
                # launcher then deletes the receipt. Once this exec has
                # returned too, nothing is left to signal. A receipt missing
                # while the exec still runs remains a failed kill.
                if not self._exits_within(_RECEIPT_EXIT_GRACE_S):
                    raise
        finally:
            if self._process.poll() is None:
                self._process.kill()

    def _exits_within(self, seconds: float) -> bool:
        try:
            self._process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return False
        return True

    def wait(self) -> int:
        return self._process.wait()

    def stderr_text(self) -> str:
        # A resident CLI holds stderr open for its whole life, so the drain
        # reaches EOF only after the exec exits. Waiting on a live process
        # would hold every turn's result for the full timeout; the tail
        # collected so far is the answer until then.
        if self._stderr_thread is not None and self._process.poll() is not None:
            self._stderr_thread.join(timeout=5)
        return self._stderr_tail.text()


__all__ = [
    "MEMBER_REAPER_SOURCE",
    "MEMBER_REAPER_TARGET",
    "MemberExecMixin",
    "member_inference_env_path",
]
