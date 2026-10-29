"""Stop a sandbox's shared-workspace writers for one capture, then resume exactly them.

A snapshot of the shared workspace is consistent only if nothing writes to it
while it is captured. Agent processes run under per-member UIDs, terminals and
setup commands under the sandbox UID, and environment operations and actions
under their own UIDs, so the freeze stops the complete census of every such
identity, as that identity, and records each process it stopped by PID and
start time. The thaw resumes exactly those, and reports a census that changed
in between.

The race handling here is deliberate: a census repeats until every process it
sees is stopped, gives up at its own deadline, and keeps a receipt in a tmpfs
only its identity can write, so a reply lost on the way back still leaves
something to resume.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_models import (
    ACTIVATION_TMPFS,
    MEMBER_TMPFS_ROOT,
    MEMBER_UID_BASE,
)
from cloud_agent_host.environment_process import ACTION_UIDS, ENVIRONMENT_UIDS
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_models import ChatSandboxSpec

#: Seconds a shared-writer census may hold writers stopped before it gives up
#: and resumes them itself. Its exec allows 20, which leaves that resume time to
#: finish before the host stops waiting for the census's reply.
CENSUS_BUDGET_S = 10

#: Classify a complete thread group, failing closed on census races.
#:
#: A zombie group leader may still have live worker threads. Check every
#: task and match both surrounding thread counts. Exited nonleaders make
#: the census ambiguous: another task may replace them without changing
#: the count. Only a sole dead leader proves the group has exited.
TASK_STATE_SCRIPT = (
    "writer_state() { proc=$1; seen=0; live=0; stopped=1; "
    'threads=$(sed -n "s/^Threads:[[:space:]]*//p" "$proc/status" 2>/dev/null || :); '
    'case "$threads" in ""|0|*[!0-9]*) printf "%s" unknown; return;; esac; '
    'for task in "$proc"/task/[0-9]*; do '
    'raw=$(cat "$task/stat" 2>/dev/null || :); fields=${raw##*) }; '
    'state=$(set -- $fields; printf "%s" "${1:-}"); '
    'case "$state" in Z|X|x) '
    'if [ "${task##*/}" != "${proc##*/}" ]; then '
    'printf "%s" unknown; return; fi;; T|t) live=1;; '
    '"") printf "%s" unknown; return;; *) live=1; stopped=0;; esac; '
    "seen=$((seen + 1)); done; "
    'current_threads=$(sed -n "s/^Threads:[[:space:]]*//p" '
    '"$proc/status" 2>/dev/null || :); '
    'if [ "$threads" != "$seen" ] || [ "$current_threads" != "$seen" ]; then '
    'printf "%s" unknown; elif [ "$live" = 0 ]; then '
    'if [ "$seen" = 1 ]; then printf "%s" exited; else printf "%s" unknown; fi; '
    'elif [ "$stopped" = 1 ]; then printf "%s" stopped; '
    'else printf "%s" running; fi; }; '
)


def operation_writer_uids(manager: ChatSandboxManager, spec: ChatSandboxSpec) -> list[int]:
    """Enumerate active operation identities without reading their environments."""
    script = """import os
# AGENT_OPERATION_UID_CENSUS
uids=set()
for name in os.listdir('/proc'):
 if not name.isdigit(): continue
 try: uids.add(os.stat('/proc/'+name).st_uid)
 except FileNotFoundError: pass
print(' '.join(str(uid) for uid in sorted(uids) if uid in (30000,30001,30002) or 31000<=uid<31064))
"""
    result = manager.exec_capture(spec, ["python3", "-I", "-c", script], workdir="/", timeout_s=10)
    if result.returncode:
        raise SandboxError("Environment writer census could not be verified")
    try:
        uids = [int(value) for value in result.stdout.decode().split()]
    except ValueError:
        raise SandboxError("Invalid environment writer census") from None
    if any(uid not in (*ENVIRONMENT_UIDS, *ACTION_UIDS) for uid in uids):
        raise SandboxError("Unexpected environment writer identity")
    return uids


def freeze(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    slot_ids: list[int],
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
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("snapshot freeze token is invalid")
    unique_slots = sorted(set(slot_ids))
    if any(slot < 0 or slot >= spec.resident_slot_capacity for slot in unique_slots):
        raise ValueError("snapshot member slot is outside sandbox capacity")
    if expected_container_id is not None:
        manager._require_exact(spec, expected_container_id)
    manifest = f"{ACTIVATION_TMPFS}/snapshot-{token}.pids"
    target_uids = [
        manager.backend.uid,
        *manager._operation_writer_uids(spec),
        *(MEMBER_UID_BASE + slot for slot in unique_slots),
    ]
    # The container drops every capability.  Even UID 0 therefore cannot
    # signal a process owned by a member UID; CAP_KILL is intentionally not
    # added back.  Run one census as each identity instead.  Repeating the
    # census until every observed process is stopped closes the race where
    # an idle CLI background process forks while /proc is being scanned.
    # Exclude only fully exited thread groups: a zombie leader can retain
    # live workers, and the leader stays Z even when SIGSTOP stops them.
    #
    # A process already stopped when the census first sees it was stopped
    # on purpose, such as an owner terminal the Sandbox Manager suspended.
    # It counts as frozen but enters neither receipt, so neither the thaw
    # nor recovery resumes it; the manifest marks it only so that the
    # exact thaw census knows it.  Like any newly seen process it costs
    # another round, since it may have forked while the round scanned,
    # and seen running in a later round it is frozen as a writer.
    #
    # The host can lose a reply after its census stopped writers: the exec
    # times out, or its transport fails while the guest carries on.  Once
    # it has stopped anything, the census therefore gives up at its own
    # deadline, well inside the exec timeout, and resumes through its trap.
    # It also keeps its receipt in the identity's private tmpfs, naming
    # itself first and recording each writer before stopping it, so that
    # recovery resumes the processes its trap or the thaw would.  Resuming
    # every stopped process of the identity instead would also wake ones
    # stopped on purpose meanwhile, such as a suspended terminal.
    script = TASK_STATE_SCRIPT + (
        'set -eu; expected_uid="$1"; committed=0; receipt=""; already=""; receipt_file="$2"; '
        "resume_receipt() { "
        '[ "$committed" = 0 ] || return 0; '
        "for item in $receipt; do pid=${item%%:*}; started=${item#*:}; "
        'uid=$(stat -c %u "/proc/$pid" 2>/dev/null || :); '
        'raw=$(cat "/proc/$pid/stat" 2>/dev/null || :); fields=${raw##*) }; '
        'current=$(set -- $fields; printf "%s" "${20:-}"); '
        '[ "$uid" = "$expected_uid" ] && [ "$current" = "$started" ] '
        '&& kill -CONT "$pid" 2>/dev/null || :; done; }; '
        "trap resume_receipt EXIT; trap 'exit 46' HUP INT TERM; round=0; "
        "read -r now _ < /proc/uptime; deadline=$((${now%%.*} + $3)); "
        'in_time() { read -r now _ < /proc/uptime; [ "${now%%.*}" -lt "$deadline" ]; }; '
        'raw=$(cat "/proc/$$/stat"); fields=${raw##*) }; '
        'started=$(set -- $fields; printf "%s" "${20:-}"); '
        'printf "%s:%s\\n" "$$" "$started" > "$receipt_file"; '
        "while :; do round=$((round + 1)); stable=1; "
        'for proc in /proc/[0-9]*; do [ -z "$receipt" ] || in_time || exit 47; '
        "pid=${proc##*/}; "
        '[ "$pid" != 1 ] && [ "$pid" != "$$" ] || continue; '
        'uid=$(stat -c %u "$proc" 2>/dev/null || :); '
        '[ "$uid" = "$expected_uid" ] || continue; '
        'raw=$(cat "$proc/stat" 2>/dev/null || :); fields=${raw##*) }; '
        'state=$(writer_state "$proc"); [ "$state" != exited ] || continue; '
        'started=$(set -- $fields; printf "%s" "${20:-}"); '
        'case "$started" in ""|*[!0-9]*) continue;; esac; item="$pid:$started"; '
        'case " $receipt " in *" $item "*) '
        'case "$state" in stopped) ;; *) kill -STOP "$pid" 2>/dev/null || continue; '
        'stable=0;; esac;; *) case "$state" in stopped) case " $already " in '
        '*" $item "*) ;; *) already="$already $item"; stable=0;; esac;; '
        '*) printf "%s\\n" "$item" >> "$receipt_file"; '
        'kill -STOP "$pid" 2>/dev/null || continue; '
        'receipt="$receipt $item"; stable=0;; esac;; esac; done; '
        '[ "$stable" = 1 ] && break; [ "$round" -lt 64 ] || exit 45; done; '
        '[ -z "$receipt" ] || in_time || exit 47; '
        "for item in $receipt; do pid=${item%%:*}; started=${item#*:}; "
        'printf "%s %s %s\\n" "$pid" "$expected_uid" "$started"; done; '
        'for item in $already; do case " $receipt " in *" $item "*) continue;; esac; '
        "pid=${item%%:*}; started=${item#*:}; "
        'printf "%s %s %s stopped\\n" "$pid" "$expected_uid" "$started"; done; '
        "committed=1"
    )
    frozen: list[tuple[int, int, int]] = []
    already_stopped: list[tuple[int, int, int]] = []
    try:
        for uid in target_uids:
            result = manager.exec_capture(
                spec,
                [
                    "/bin/sh",
                    "-c",
                    script,
                    "sh",
                    str(uid),
                    receipt_path(manager, spec, uid, token),
                    str(CENSUS_BUDGET_S),
                ],
                workdir="/",
                timeout_s=20,
                user_uid=uid,
                user_gid=manager.backend.gid,
                expected_container_id=expected_container_id,
            )
            if result.returncode != 0:
                raise SandboxError(
                    "shared snapshot writer freeze failed: "
                    + result.stderr.decode("utf-8", "replace")[:300]
                )
            census_frozen, census_stopped = parse_frozen_processes(
                result.stdout, allowed_uids={uid}
            )
            frozen.extend(census_frozen)
            already_stopped.extend(census_stopped)
        census = [*frozen, *already_stopped]
        if len({pid for pid, _uid, _started in census}) != len(census):
            raise SandboxError("shared snapshot writer census returned a duplicate pid")
        payload = encode_frozen_processes(frozen, already_stopped)
        write_script = (
            'set -eu; manifest="$1"; temporary="$manifest.tmp.$$"; umask 077; '
            "trap 'rm -f -- \"$temporary\"' EXIT HUP INT TERM; "
            'cat > "$temporary"; chmod 600 "$temporary"; mv "$temporary" "$manifest"; '
            "trap - EXIT HUP INT TERM"
        )
        written = manager.exec_capture(
            spec,
            ["/bin/sh", "-c", write_script, "sh", manifest],
            workdir="/",
            timeout_s=20,
            input_bytes=payload,
            user_uid=manager.backend.uid,
            user_gid=manager.backend.gid,
            expected_container_id=expected_container_id,
        )
        if written.returncode != 0:
            raise SandboxError(
                "shared snapshot writer manifest failed: "
                + written.stderr.decode("utf-8", "replace")[:300]
            )
        if expected_container_id is not None:
            manager._require_exact(spec, expected_container_id)
    except BaseException:
        with contextlib.suppress(Exception):
            _resume(
                manager,
                spec,
                target_uids=target_uids,
                frozen=frozen,
                already_stopped=already_stopped,
                token=token,
                require_exact_census=False,
                expected_container_id=expected_container_id,
            )
        raise
    return manifest


def thaw(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    slot_ids: list[int],
    manifest: str,
    require_exact_census: bool = True,
    expected_container_id: str | None = None,
) -> None:
    """Resume exactly the frozen PID/start pairs and detect new writers."""
    named = re.fullmatch(
        rf"{re.escape(ACTIVATION_TMPFS)}/snapshot-([0-9a-f]{{32}})\.pids", manifest
    )
    if named is None:
        raise ValueError("snapshot writer manifest path is invalid")
    unique_slots = sorted(set(slot_ids))
    if any(slot < 0 or slot >= spec.resident_slot_capacity for slot in unique_slots):
        raise ValueError("snapshot member slot is outside sandbox capacity")
    if expected_container_id is not None:
        manager._require_exact(spec, expected_container_id)
    target_uids = [
        manager.backend.uid,
        *manager._operation_writer_uids(spec),
        *(MEMBER_UID_BASE + slot for slot in unique_slots),
    ]
    read = manager.exec_capture(
        spec,
        ["/bin/sh", "-c", 'cat -- "$1"', "sh", manifest],
        workdir="/",
        timeout_s=20,
        user_uid=manager.backend.uid,
        user_gid=manager.backend.gid,
        expected_container_id=expected_container_id,
    )
    if read.returncode != 0:
        raise SandboxError(
            "shared snapshot writer manifest is unavailable: "
            + read.stderr.decode("utf-8", "replace")[:300]
        )
    frozen, already_stopped = parse_frozen_processes(
        read.stdout, allowed_uids={*target_uids, *ENVIRONMENT_UIDS, *ACTION_UIDS}
    )
    target_uids = sorted({*target_uids, *(uid for _, uid, _ in [*frozen, *already_stopped])})
    resume_error: BaseException | None = None
    try:
        _resume(
            manager,
            spec,
            target_uids=target_uids,
            frozen=frozen,
            already_stopped=already_stopped,
            token=named.group(1),
            require_exact_census=require_exact_census,
            expected_container_id=expected_container_id,
        )
    except BaseException as exc:
        resume_error = exc
    removed = manager.exec_capture(
        spec,
        ["/bin/sh", "-c", 'rm -f -- "$1"', "sh", manifest],
        workdir="/",
        timeout_s=20,
        user_uid=manager.backend.uid,
        user_gid=manager.backend.gid,
        expected_container_id=expected_container_id,
    )
    if resume_error is not None:
        raise resume_error
    if removed.returncode != 0:
        raise SandboxError(
            "shared snapshot writer manifest cleanup failed: "
            + removed.stderr.decode("utf-8", "replace")[:300]
        )
    if expected_container_id is not None:
        manager._require_exact(spec, expected_container_id)


def parse_frozen_processes(
    payload: bytes,
    *,
    allowed_uids: set[int],
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """Validate the private PID/start-time receipt before any signal.

    Returns the processes the census stopped, then those it found already
    stopped, which a trailing ``stopped`` marks and nothing may resume.
    """
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SandboxError("shared snapshot writer census is not ASCII") from exc
    frozen: list[tuple[int, int, int]] = []
    already_stopped: list[tuple[int, int, int]] = []
    seen: set[int] = set()
    for line in text.splitlines():
        fields = line.split()
        found_stopped = fields[3:] == ["stopped"]
        if found_stopped:
            fields.pop()
        if len(fields) != 3 or any(not field.isascii() or not field.isdigit() for field in fields):
            raise SandboxError("shared snapshot writer census is malformed")
        pid, uid, started = (int(field) for field in fields)
        if pid <= 1 or started <= 0 or uid not in allowed_uids or pid in seen:
            raise SandboxError("shared snapshot writer census is outside its identity fence")
        seen.add(pid)
        (already_stopped if found_stopped else frozen).append((pid, uid, started))
    return frozen, already_stopped


def encode_frozen_processes(
    frozen: list[tuple[int, int, int]],
    already_stopped: list[tuple[int, int, int]],
) -> bytes:
    """Encode a deterministic, private manifest for the later thaw."""
    lines = [f"{pid} {uid} {started}\n" for pid, uid, started in sorted(frozen)]
    lines += [f"{pid} {uid} {started} stopped\n" for pid, uid, started in sorted(already_stopped)]
    return "".join(lines).encode("ascii")


def receipt_path(manager: ChatSandboxManager, spec: ChatSandboxSpec, uid: int, token: str) -> str:
    """Name one identity's census receipt in the tmpfs only it can write."""
    if uid == manager.backend.uid:
        directory = ACTIVATION_TMPFS
    elif uid in (*ENVIRONMENT_UIDS, *ACTION_UIDS):
        directory = f"/run/agent-environments/{uid}"
    elif 0 <= uid - MEMBER_UID_BASE < spec.resident_slot_capacity:
        directory = f"{MEMBER_TMPFS_ROOT}/{uid - MEMBER_UID_BASE}"
    else:
        raise ValueError("snapshot writer identity has no private tmpfs")
    return f"{directory}/snapshot-{token}.receipt"


def _resume(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    target_uids: list[int],
    frozen: list[tuple[int, int, int]],
    already_stopped: list[tuple[int, int, int]],
    token: str,
    require_exact_census: bool,
    expected_container_id: str | None = None,
) -> None:
    """Resume each receipt as its owner and report any changed census.

    Each identity's guest-side census receipt is resumed too, then removed:
    it is the only record of a census whose reply never reached the host.
    The census it names first may still be running and would stop again
    what this resumes, so it is killed and its exit awaited before anything
    is resumed.  A process the census found already stopped is known to the
    exact census, but never resumed.
    """
    by_uid = {
        uid: [(pid, started) for pid, owner, started in frozen if owner == uid]
        for uid in target_uids
    }
    script = TASK_STATE_SCRIPT + (
        'set -u; exact="$1"; expected_uid="$2"; receipt_file="$3"; already="$4"; shift 4; '
        'unknown=0; if [ "$exact" = 1 ]; then for proc in /proc/[0-9]*; do '
        'pid=${proc##*/}; [ "$pid" != 1 ] && [ "$pid" != "$$" ] || continue; '
        'uid=$(stat -c %u "$proc" 2>/dev/null || :); '
        '[ "$uid" = "$expected_uid" ] || continue; '
        'raw=$(cat "$proc/stat" 2>/dev/null || :); fields=${raw##*) }; '
        'state=$(writer_state "$proc"); [ "$state" != exited ] || continue; '
        'started=$(set -- $fields; printf "%s" "${20:-}"); found=0; '
        'for item in "$@" $already; do [ "$item" = "$pid:$started" ] && found=1; done; '
        '[ "$found" = 1 ] || unknown=1; done; fi; '
        'census=""; [ ! -f "$receipt_file" ] || read -r census < "$receipt_file" || :; '
        "pid=${census%%:*}; started=${census#*:}; attempt=0; "
        'while [ -n "$census" ]; do '
        'uid=$(stat -c %u "/proc/$pid" 2>/dev/null || :); '
        'raw=$(cat "/proc/$pid/stat" 2>/dev/null || :); fields=${raw##*) }; '
        'current=$(set -- $fields; printf "%s" "${20:-}"); '
        '[ "$uid" = "$expected_uid" ] && [ "$current" = "$started" ] || break; '
        '[ "$(writer_state "/proc/$pid")" != exited ] || break; '
        'kill -KILL "$pid" 2>/dev/null || :; attempt=$((attempt + 1)); '
        '[ "$attempt" -lt 100 ] || break; sleep 0.05; done; '
        'if [ -f "$receipt_file" ]; then { read -r _ || :; while read -r item; do '
        'case " $* " in *" $item "*) ;; *) set -- "$@" "$item";; esac; '
        'done; } < "$receipt_file"; fi; '
        'for item in "$@"; do pid=${item%%:*}; started=${item#*:}; '
        'uid=$(stat -c %u "/proc/$pid" 2>/dev/null || :); '
        'raw=$(cat "/proc/$pid/stat" 2>/dev/null || :); fields=${raw##*) }; '
        'current=$(set -- $fields; printf "%s" "${20:-}"); '
        '[ "$uid" = "$expected_uid" ] && [ "$current" = "$started" ] '
        '&& kill -CONT "$pid" 2>/dev/null || :; done; '
        'rm -f -- "$receipt_file"; [ "$unknown" = 0 ]'
    )
    errors: list[str] = []
    for uid in target_uids:
        expected = [f"{pid}:{started}" for pid, started in by_uid[uid]]
        already = " ".join(
            f"{pid}:{started}" for pid, owner, started in already_stopped if owner == uid
        )
        try:
            result = manager.exec_capture(
                spec,
                [
                    "/bin/sh",
                    "-c",
                    script,
                    "sh",
                    "1" if require_exact_census else "0",
                    str(uid),
                    receipt_path(manager, spec, uid, token),
                    already,
                    *expected,
                ],
                workdir="/",
                timeout_s=20,
                user_uid=uid,
                user_gid=manager.backend.gid,
                expected_container_id=expected_container_id,
            )
        except BaseException as exc:
            errors.append(str(exc))
            continue
        if result.returncode != 0:
            errors.append(result.stderr.decode("utf-8", "replace")[:300])
    if errors:
        detail = next((error for error in errors if error), "writer census changed")
        raise SandboxError("shared snapshot writer census changed during capture: " + detail)
