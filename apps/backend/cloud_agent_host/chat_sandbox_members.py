"""Member processes in a shared sandbox: receipts, census, reaping and kill.

Each member slot runs its CLI as its own UID, with a tmpfs private to that UID.
A launch records the CLI's PID and Linux start time there as a receipt, so a
PID the kernel later reuses is never signalled. Before a turn the host takes a
census of the member's processes, and it reaps every process outside that
census through a root-owned helper running as the member. A kill signals one
verified process group, never the container.

The module also writes a member's owner inference credentials for its bash
into that private tmpfs, over stdin and never in argv.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_models import (
    ACTIVATION_TMPFS,
    MEMBER_TMPFS_ROOT,
    MEMBER_UID_BASE,
    MemberProcessReceiptMissing,
)
from cloud_agent_host.sandbox import KATA_RUNTIME, SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_models import ChatSandboxSpec

MEMBER_REAPER_TARGET = "/usr/local/bin/member-process-reaper"
MEMBER_REAPER_SOURCE = "/usr/local/libexec/member-process-reaper"

#: The member reaper's exit when no process receipt appeared in its wait.
_REAPER_NO_RECEIPT = 125


def inference_env_path(slot_id: int) -> str:
    """Where one member's owner inference credentials wait for its bash."""
    return f"{MEMBER_TMPFS_ROOT}/{slot_id}/inference-env.sh"


def validate_identity(spec: ChatSandboxSpec, *, slot_id: int, user_uid: int) -> None:
    """Refuse a slot outside the sandbox, or a UID that is not that slot's."""
    if not 0 <= slot_id < spec.resident_slot_capacity:
        raise ValueError("resident slot is outside this shared sandbox")
    if user_uid != MEMBER_UID_BASE + slot_id:
        raise ValueError("resident slot does not match its member uid")


def validate_pidfile(
    manager: ChatSandboxManager,
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
        if user_uid != manager.backend.uid:
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


def write_inference_env(
    manager: ChatSandboxManager,
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
    validate_identity(spec, slot_id=slot_id, user_uid=user_uid)
    path = inference_env_path(slot_id)
    write_script = (
        'set -eu; target="$1"; temporary="$target.tmp.$$"; umask 077; '
        "trap 'rm -f -- \"$temporary\"' EXIT HUP INT TERM; "
        'cat > "$temporary"; mv "$temporary" "$target"; '
        "trap - EXIT HUP INT TERM"
    )
    result = manager.exec_capture(
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


def ensure_reaper(manager: ChatSandboxManager, spec: ChatSandboxSpec) -> None:
    """Install the immutable helper into retained pre-upgrade containers."""
    with manager._member_reaper_lock:
        probe = manager.exec_capture(
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
            return
        current = manager._require(spec)
        if not current.running:
            raise SandboxError("chat sandbox must be started before helper installation")
        source = os.environ.get("AGENT_MEMBER_PROCESS_REAPER_SOURCE", MEMBER_REAPER_SOURCE)
        if not os.path.isfile(source):
            raise SandboxError(
                "retained sandbox lacks member-process-reaper and the trusted "
                f"runner source {source!r} is unavailable"
            )
        temporary = f"{MEMBER_REAPER_TARGET}.install-{secrets.token_hex(8)}"
        copied = manager._run(
            [
                manager.backend.docker_binary,
                "cp",
                source,
                f"{current.container_id}:{temporary}",
            ],
            timeout=30,
        )
        if copied.returncode != 0:
            raise SandboxError(
                "member-process-reaper compatibility install failed: " + copied.stderr.strip()[:300]
            )
        installed = manager.exec_capture(
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
                manager.exec_capture(
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


def reap(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    slot_id: int,
    user_uid: int,
    user_gid: int,
) -> None:
    """Kill unattached live CLI trees in exactly one private member slot.

    Every launch reaps its slot first, which also drops the owner inference
    credentials an earlier launch left for its bash: a launch with them
    writes them again, and one whose owner removed them must not find them.
    """
    validate_identity(spec, slot_id=slot_id, user_uid=user_uid)
    directory = f"{MEMBER_TMPFS_ROOT}/{slot_id}"
    manager.reap_member_processes_except(
        spec,
        slot_id=slot_id,
        user_uid=user_uid,
        user_gid=user_gid,
        baseline=(),
    )
    result = manager.exec_capture(
        spec,
        [
            "/bin/sh",
            "-c",
            f"rm -f -- {directory}/resident-*.pid {inference_env_path(slot_id)}",
        ],
        workdir="/",
        timeout_s=20,
        user_uid=user_uid,
        user_gid=user_gid,
    )
    if result.returncode != 0:
        raise SandboxError(
            "stale resident process cleanup failed: "
            + result.stderr.decode("utf-8", "replace")[:300]
        )


def capture(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    slot_id: int,
    user_uid: int,
    user_gid: int,
) -> tuple[tuple[int, int], ...]:
    """Capture the reviewed resident PID/start-time set before one turn."""
    validate_identity(spec, slot_id=slot_id, user_uid=user_uid)
    manager.ensure_member_process_reaper(spec)
    result = manager.exec_capture(
        spec,
        [MEMBER_REAPER_TARGET, "capture", str(user_uid)],
        workdir="/",
        timeout_s=20,
        user_uid=user_uid,
        user_gid=user_gid,
    )
    if result.returncode != 0:
        raise SandboxError(
            "member process census failed: " + result.stderr.decode("utf-8", "replace")[:300]
        )
    return parse_processes(result.stdout)


def reap_except(
    manager: ChatSandboxManager,
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
    validate_identity(spec, slot_id=slot_id, user_uid=user_uid)
    manager.ensure_member_process_reaper(spec)
    payload = encode_processes(baseline)
    result = manager.exec_capture(
        spec,
        [MEMBER_REAPER_TARGET, "reap", str(user_uid)],
        workdir="/",
        timeout_s=30,
        input_bytes=payload,
        user_uid=user_uid,
        user_gid=user_gid,
    )
    if result.returncode != 0:
        raise SandboxError(
            "member activation process cleanup failed: "
            + result.stderr.decode("utf-8", "replace")[:300]
        )


def parse_processes(payload: bytes) -> tuple[tuple[int, int], ...]:
    """Validate a member census of ``pid start-time`` lines, sorted and fenced."""
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SandboxError("member process census is not ASCII") from exc
    found: list[tuple[int, int]] = []
    seen: set[int] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2 or any(not field.isascii() or not field.isdigit() for field in fields):
            raise SandboxError("member process census is malformed")
        pid, started = (int(field) for field in fields)
        if pid <= 1 or started <= 0 or pid in seen or len(found) >= 4096:
            raise SandboxError("member process census is outside its identity fence")
        seen.add(pid)
        found.append((pid, started))
    return tuple(sorted(found))


def encode_processes(processes: tuple[tuple[int, int], ...]) -> bytes:
    """Encode a baseline for the reaper, refusing one its census could not return."""
    canonical = parse_processes(
        "".join(f"{pid} {started}\n" for pid, started in processes).encode("ascii")
    )
    return "".join(f"{pid} {started}\n" for pid, started in canonical).encode("ascii")


def kill(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    pidfile: str,
    user_uid: int,
    user_gid: int,
) -> None:
    """Signal one verified resident or command process group, never the container."""
    validate_pidfile(manager, spec, pidfile, user_uid=user_uid)
    argv = [
        "/bin/sh",
        "-c",
        reaper_script('"$2"'),
        "sh",
        str(user_uid),
        pidfile,
    ]
    result = (
        manager._kata_privileged_exec(spec, argv, timeout_s=20)
        if manager.backend.runtime == KATA_RUNTIME
        else manager.exec_capture(
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
        raise kill_failure(result.returncode, stderr)


def reaper_script(pidfile_expression: str) -> str:
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


def kill_failure(returncode: int, stderr: str) -> SandboxError:
    """Name a failed member reaper run.

    Only the reaper's own silent exit means the receipt is missing: a Docker
    exec failure that shares the code always explains itself on stderr.
    """
    if returncode == _REAPER_NO_RECEIPT and not stderr.strip():
        return MemberProcessReceiptMissing("resident process receipt is missing")
    return SandboxError("resident process cleanup failed: " + stderr[:300])
