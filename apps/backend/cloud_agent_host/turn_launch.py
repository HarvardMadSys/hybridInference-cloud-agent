"""How a claimed turn reaches its child: a cold start, or a spare started ahead.

Every turn runs in a child of the host service and is handed over on that
child's standard input (:mod:`cloud_agent_host.host_runtime`), never in its
arguments or environment: the turn carries its attempt's credentials, and
``/proc/<pid>/cmdline`` and ``/proc/<pid>/environ`` are readable by anyone
who can see the process. A cold child pays for its interpreter and the
runner's imports, about 0.3 s, between the claim and its answer, and the host
claims nothing else while it waits for that answer.

A spare is a child started before the claim it will serve, with ``--spare``.
It has already imported everything and is blocked reading its standard
input, so a claim hands it the turn exactly as a cold child is handed one,
and the next spare starts once that handover is over. Its command line holds
only this host's configuration, which is why everything that differs per
turn, the lease owner name included, travels in the turn. Until the turn
arrives a spare holds no turn and no credential, so retiring one is always
safe: at end-of-file a spare exits without running anything, not even the
probe a cold child runs on empty input (that probe is the deployment
canary's), and one that does not exit is killed. A spare that has exited, or
that wrote to its handover descriptor before it was handed anything, is never
given a turn; a cold child is started for it instead.

The handover's contract is unchanged. ``LAUNCH_NOT_STARTED`` still means the
child provably never had the turn: no process was created, or the child
declined it. Once a live child's standard input is being written, any failure
is ambiguous, for a spare exactly as for a cold child.
"""

from __future__ import annotations

import contextlib
import functools
import os
import select
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from cloud_agent_host.host_runtime import (
    HANDOVER_ACCEPTED,
    HANDOVER_DECLINED,
    HANDOVER_TIMEOUT_S,
    LAUNCH_NOT_STARTED,
    LAUNCH_STARTED,
    LAUNCH_UNCERTAIN,
    LaunchResult,
    encode_turn,
    read_handover,
)
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.runner_stop import STOPPING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cloud_agent_host.runtime_qualification import HostQualification

logger = get_logger(__name__)

#: The parent's additions a turn's child does read: the profiling selection,
#: and the runtime settings the turn runs with. Every other underscore key is
#: parent bookkeeping, and stripped; what this host proves itself, and the
#: worker id below, are added after the strip (:func:`spawn_turn`).
CHILD_HANDOFF_KEYS = frozenset({"_profiling", "_host_settings"})

#: The lease owner name the turn runs under, distinct per turn. The host sets
#: it on every handover after the claim's own underscore keys are stripped,
#: so a claim can never supply it.
WORKER_ID_KEY = "_worker_id"

#: How long a retired spare gets to exit at end-of-file before it is killed:
#: enough to finish its imports. It holds nothing, so killing it loses nothing.
SPARE_RETIRE_WAIT_S = 5.0


def child_turn_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strip parent bookkeeping while preserving the profiling and settings handoff."""
    return {
        key: value
        for key, value in payload.items()
        if not key.startswith("_") or key in CHILD_HANDOFF_KEYS
    }


def handed_worker_id(payload: Mapping[str, Any], fallback: str) -> str:
    """The lease owner name the host handed with this turn, else the command line's.

    The host hands one with every turn. The command line's serves a turn
    delivered without one, by hand or by an older caller.
    """
    value = payload.get(WORKER_ID_KEY)
    return value if isinstance(value, str) and value.strip() else fallback


def turn_argv(
    handover_fd: int,
    *,
    base_url: str,
    gateway_url: str,
    worker_id: str,
    lease_ttl: float,
    agent_timeout_s: float,
    host: str,
    spare: bool = False,
) -> list[str]:
    """A turn child's command line: its answer's descriptor and this host's configuration.

    Never anything the turn carries. A cold child's names the turn's own
    worker id, as it always has; a spare's names this host's.
    """
    argv = [
        sys.executable,
        "-m",
        "cloud_agent_host.runner",
        "--turn",
        "--handover-fd",
        str(handover_fd),
        "--base-url",
        base_url,
        "--gateway-url",
        gateway_url,
        "--worker-id",
        worker_id,
        "--host",
        host,
        "--lease-ttl",
        str(lease_ttl),
        "--agent-timeout",
        str(agent_timeout_s),
    ]
    return [*argv, "--spare"] if spare else argv


# ── The child's side ───────────────────────────────────────────────────


def read_turn_input(*, spare: bool) -> str | None:
    """Read the turn from standard input, or ``None`` for a spare retired without one.

    A cold child reads at once: its host is already writing. A spare first
    warms what its turn will use (:func:`_warm_http_stack`), then waits here
    for as long as its host keeps it. End-of-file before a single byte is that
    host retiring it, and it exits without the probe a cold child runs on
    empty input. Any other input is read exactly as a cold child reads it.
    """
    if spare:
        _warm_http_stack()
    raw = sys.stdin.read()
    if spare and not raw:
        return None
    return raw


def _warm_http_stack() -> None:
    """Import, before the turn arrives, what its first HTTP client would.

    A turn builds its control-plane client as soon as it has answered, and
    the first client in a process imports httpx's transport stack (about
    25 ms). A throwaway client, with no credential and no connection, pays
    that here instead. Nothing it builds is kept, so the turn's own clients
    are built exactly as a cold child builds them, and a failure here is
    left for the turn to meet.
    """
    with contextlib.suppress(Exception):
        httpx.Client().close()


# ── The host's side ────────────────────────────────────────────────────


class _NotStarted(Exception):
    """No child process exists, so nothing can hold the turn."""


@dataclass(frozen=True)
class _Child:
    """A started turn child, and the read end of its handover descriptor."""

    process: subprocess.Popen[bytes]
    answers: int


def _start_child(argv: Callable[[int], list[str]]) -> tuple[_Child, int]:
    """Start a turn child on a new handover pipe; return it and the pipe's write end.

    The write end is this process's copy of the child's descriptor, still
    open. Raises :class:`_NotStarted`, holding nothing open, when no process
    was created.
    """
    try:
        read_fd, write_fd = os.pipe()
    except OSError as exc:
        raise _NotStarted(f"could not create the turn handover pipe: {exc}") from exc
    try:
        process = subprocess.Popen(argv(write_fd), stdin=subprocess.PIPE, pass_fds=(write_fd,))
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise _NotStarted(f"could not start a turn process: {exc}") from exc
    return _Child(process, read_fd), write_fd


def _abandon(child: _Child, open_fds: list[int]) -> None:
    """Close what this process still holds of a handover it is giving up on."""
    for fd in open_fds:
        with contextlib.suppress(OSError):
            os.close(fd)
    if child.process.stdin is not None:
        with contextlib.suppress(OSError, ValueError):
            child.process.stdin.close()


def _hand_over(
    child: _Child,
    encoded: bytes,
    *,
    read_answer: Callable[[int], str | None],
    write_fd: int | None = None,
) -> LaunchResult:
    """Write the turn to a started child's standard input and read its one answer.

    ``write_fd`` is this process's copy of a cold child's handover
    descriptor, still open; a spare's was closed when it started.
    """
    open_fds = [child.answers] if write_fd is None else [child.answers, write_fd]
    try:
        if write_fd is not None:
            # Closed so the read below ends at end-of-file when the child
            # exits rather than waiting out the deadline. Dropped from the
            # list first: a close that fails has still released the number.
            open_fds.remove(write_fd)
            os.close(write_fd)
        assert child.process.stdin is not None
        child.process.stdin.write(encoded)
        child.process.stdin.close()
        # read_answer owns and always closes this descriptor, including when
        # it raises. Mark it transferred before entering the call so cleanup
        # never closes a descriptor number that another thread could reuse.
        open_fds.remove(child.answers)
        answer = read_answer(child.answers)
    except KeyboardInterrupt:
        # Ctrl-C means immediate shutdown. This child is not in TurnPool until
        # the launch returns, so reap it here rather than orphaning it in the
        # handover window.
        _abandon(child, open_fds)
        with contextlib.suppress(OSError):
            child.process.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            child.process.wait(timeout=10)
        raise
    except Exception as exc:
        # The process exists and may already have accepted the turn. Losing
        # the handover transport is not proof that it is safe to requeue or
        # omit this child from graceful shutdown accounting.
        _abandon(child, open_fds)
        return LaunchResult(
            outcome=LAUNCH_UNCERTAIN,
            handle=child.process,
            detail=(
                f"handover failed ({type(exc).__name__}); the turn stays claimed "
                "because the spawned child may already own it"
            ),
        )
    return _answered(child.process, answer)


def _answered(process: subprocess.Popen[bytes], answer: str | None) -> LaunchResult:
    """What a child's one answer proves about whether it took its turn."""
    prefix = answer.split(" ", 1)[0] if answer else ""
    if prefix == HANDOVER_ACCEPTED:
        return LaunchResult(outcome=LAUNCH_STARTED, handle=process)
    if prefix == HANDOVER_DECLINED:
        _reap_unstarted(process)
        return LaunchResult(outcome=LAUNCH_NOT_STARTED, detail=answer or HANDOVER_DECLINED)
    if answer is None:
        detail = f"no answer within {HANDOVER_TIMEOUT_S:.0f}s"
    elif not answer:
        detail = "the handover closed without a trustworthy answer"
    else:
        detail = f"the child returned an unexpected handover answer: {answer!r}"
    return LaunchResult(
        outcome=LAUNCH_UNCERTAIN,
        handle=process,
        detail=f"{detail}; the turn stays claimed because silence is not proof it did not start",
    )


def _reap_unstarted(process: subprocess.Popen[bytes]) -> None:
    """Collect a child that never took its turn, without waiting on it forever.

    It explicitly declined, so it is on its way out. A child that somehow is
    not gets killed: it holds no turn, so there is nothing for it to finish,
    and leaving it would leak a process per failure.
    """
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)


def _has_written(fd: int) -> bool:
    """Whether a spare's handover descriptor reads: it wrote, or it exited."""
    readable, _, _ = select.select([fd], [], [], 0)
    return bool(readable)


def _retire(spare: _Child, *, immediate: bool) -> None:
    """Stop a spare that was never handed a turn, and collect it.

    End-of-file makes a spare exit without probing or answering
    (:func:`read_turn_input`). One still running after that holds nothing
    either, so it is killed; ``immediate`` kills it at once. Never raises an
    ``Exception``: retiring a spare must not stop what its host does next.
    """
    process = spare.process
    try:
        with contextlib.suppress(OSError, ValueError):
            if process.stdin is not None:
                process.stdin.close()
        if immediate:
            process.kill()
        try:
            process.wait(timeout=SPARE_RETIRE_WAIT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=SPARE_RETIRE_WAIT_S)
    except Exception:
        logger.warning(
            "agent_turn_spare_not_collected",
            exc_info=True,
            extra={"event": "agent_turn_spare_not_collected", "pid": process.pid},
        )
    finally:
        with contextlib.suppress(OSError):
            os.close(spare.answers)


class TurnSpare:
    """The one turn child this host starts ahead of the claim it will serve.

    Used from the claim loop's thread only. :meth:`take` gives the waiting
    spare to a handover, :meth:`replenish` starts the next, and :meth:`close`
    retires the waiting one for good. None of them raises an ``Exception``: a
    spare only saves time, and a host that cannot keep one starts a cold child
    for every turn, as it did before spares existed.
    """

    def __init__(
        self,
        *,
        base_url: str,
        gateway_url: str,
        worker_id: str,
        lease_ttl: float,
        agent_timeout_s: float,
        host: str,
    ) -> None:
        # The host's own worker id, not a turn's: a spare's command line is
        # fixed before its turn is claimed, and the turn carries its own.
        self._argv = functools.partial(
            turn_argv,
            base_url=base_url,
            gateway_url=gateway_url,
            worker_id=worker_id,
            lease_ttl=lease_ttl,
            agent_timeout_s=agent_timeout_s,
            host=host,
            spare=True,
        )
        self._waiting: _Child | None = None
        self._closed = False

    def take(self) -> _Child | None:
        """The waiting spare, now the caller's, if it can still be handed a turn.

        ``None`` when there is none, or when the one there has exited or
        written to its handover descriptor before it was handed anything.
        Such a spare never had a turn, so it is only collected, and the
        caller starts a cold child instead.
        """
        spare, self._waiting = self._waiting, None
        if spare is None:
            return None
        try:
            usable = spare.process.poll() is None and not _has_written(spare.answers)
        except Exception:
            usable = False
        if usable:
            return spare
        logger.warning(
            "agent_turn_spare_lost",
            extra={
                "event": "agent_turn_spare_lost",
                "pid": spare.process.pid,
                "exit_code": spare.process.returncode,
            },
        )
        _retire(spare, immediate=True)
        return None

    def replenish(self) -> None:
        """Start the next spare, unless one is waiting or this host is stopping."""
        if self._closed or self._waiting is not None or STOPPING.is_set():
            return
        try:
            spare, write_fd = _start_child(self._argv)
        except Exception as exc:
            logger.warning(
                "agent_turn_spare_unstarted",
                extra={"event": "agent_turn_spare_unstarted", "detail": str(exc)},
            )
            return
        try:
            # Closed so the spare's exit reads as end-of-file on its descriptor.
            os.close(write_fd)
        except OSError:
            _retire(spare, immediate=True)
            return
        self._waiting = spare

    def close(self, *, immediate: bool = False) -> None:
        """Retire the waiting spare and start no other: this host is shutting down.

        ``immediate`` is Ctrl-C, and kills the spare rather than waiting for
        it to exit at end-of-file.
        """
        self._closed = True
        spare, self._waiting = self._waiting, None
        if spare is not None:
            _retire(spare, immediate=immediate)


def spawn_turn(
    payload: Mapping[str, Any],
    *,
    base_url: str,
    gateway_url: str,
    worker_id: str,
    lease_ttl: float,
    agent_timeout_s: float,
    host: str,
    qualification: HostQualification | None = None,
    spare: TurnSpare | None = None,
    read_answer: Callable[[int], str | None] = read_handover,
) -> LaunchResult:
    """Hand one claimed turn to a child and find out whether it took it.

    The waiting spare takes it when ``spare`` has one that can still be
    handed a turn; otherwise a cold child is started for it, exactly as
    before spares existed. Either way the turn travels on the child's
    standard input. The child's answer comes back on a descriptor of its own,
    because its standard output is the container log and a reader that
    stopped draining that would block the turn it was waiting for.

    Every path that returns ``LAUNCH_NOT_STARTED`` has shown the child never
    had the turn: no process was created or the child explicitly declined it.
    Silence, EOF, an unexpected answer, and any failure once the handover to
    a live child has begun are ambiguous: the turn stays claimed, the child
    stays tracked, and its lease remains the backstop.
    """
    child = child_turn_payload(payload)
    if qualification is not None:
        # Added after the claim's own underscore keys were stripped: only this
        # host, which proved the runtime and the network rules, can spare the
        # child its own proof of them (HostQualification.handover).
        child.update(qualification.handover())
    # This host's own as well, and in the turn rather than on the command
    # line, which a spare had fixed before this turn was claimed.
    child[WORKER_ID_KEY] = worker_id
    try:
        encoded = encode_turn(child)
    except (TypeError, ValueError) as exc:
        return LaunchResult(
            outcome=LAUNCH_NOT_STARTED,
            detail=f"the turn payload could not be encoded: {type(exc).__name__}: {exc}",
        )
    waiting = spare.take() if spare is not None else None
    if waiting is not None:
        result = _hand_over(waiting, encoded, read_answer=read_answer)
    else:
        argv = functools.partial(
            turn_argv,
            base_url=base_url,
            gateway_url=gateway_url,
            worker_id=worker_id,
            lease_ttl=lease_ttl,
            agent_timeout_s=agent_timeout_s,
            host=host,
        )
        result = _spawn_cold(argv, encoded, read_answer=read_answer)
    if spare is not None:
        # Started now, while this host goes back to claiming, so that it has
        # imported everything by the time the next turn arrives.
        try:
            spare.replenish()
        except KeyboardInterrupt:
            # Ctrl-C means immediate shutdown, as in the handover itself: the
            # child that has this turn is not in TurnPool until this returns.
            if result.handle is not None:
                with contextlib.suppress(OSError):
                    result.handle.kill()
            raise
    return result


def _spawn_cold(
    argv: Callable[[int], list[str]],
    encoded: bytes,
    *,
    read_answer: Callable[[int], str | None],
) -> LaunchResult:
    """Start a child for this turn alone and hand the turn over to it."""
    try:
        child, write_fd = _start_child(argv)
    except _NotStarted as exc:
        return LaunchResult(outcome=LAUNCH_NOT_STARTED, detail=str(exc))
    return _hand_over(child, encoded, read_answer=read_answer, write_fd=write_fd)


__all__ = [
    "CHILD_HANDOFF_KEYS",
    "SPARE_RETIRE_WAIT_S",
    "WORKER_ID_KEY",
    "TurnSpare",
    "child_turn_payload",
    "handed_worker_id",
    "read_turn_input",
    "spawn_turn",
    "turn_argv",
]
