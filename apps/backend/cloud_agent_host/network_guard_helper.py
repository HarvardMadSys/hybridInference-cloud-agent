"""The long-lived host-rule helper: one ``docker run`` that takes a guard's steps in turn.

Every step of :class:`~cloud_agent_host.network_guard.HostNetworkGuard` used to
start a helper container of its own, and starting one costs far more than the
``nft`` calls it makes: about 264 ms on runc, against milliseconds. The
sandbox manager's sweep starts one every five seconds while groups run, and a
cold start waits for two. So a guard process that outlives its steps, the
runner's host service or the sandbox manager, keeps one helper running while
it uses it and hands it one step at a time on its standard input. A turn's own
process does not: it ends with its turn, and the runner's first process, which
would inherit the helper's client, reaps nothing.

**The same helper, kept running.** Its command line is a per-run helper's,
flag for flag (``HostNetworkGuard._command``): this process's own image by its
``sha256:`` id, never pulled; the host's network namespace under runc;
``NET_ADMIN`` and no other capability; no-new-privileges; a read-only root;
``--rm``; and ``--interactive``, which a per-run helper has too, for its
request. It adds only ``--name``, a fresh one per helper, so that a helper
that fails a step can be removed. Only the program differs
(:func:`serve_program`): it reads one request per line, runs the per-run
helper's own program on it, writes one answer line, and waits for the next.

**One request at a time.** A step that finds the helper busy runs on a per-run
helper instead of waiting, so updates of different bridges still run side by
side, and the helper has no say in who waits for whom: that stays the guard's
lock. A step on the helper holds that lock exactly as a step on a per-run
helper does.

Threat analysis
---------------

**Who can reach it.** Its standard input is a pipe from the guard's process,
created close-on-exec, so no other program that process starts inherits it.
The one fork without an exec in this package, the protocol translator's, runs
inside a sandbox, never in a process that holds a guard. The helper listens on
no socket: being in the host's network namespace gives no one anything to
connect to. Its pipes can also be opened through ``/proc/<pid>/fd`` by root in
the guard's own container, and as containerd's stdio FIFOs by root on the host;
and the Docker daemon reaches it through ``docker attach`` (its stdin is open)
or ``docker exec``. All of these are inside the Docker socket's trust domain:
whoever holds the socket, which the runner and the sandbox manager do, can
start a privileged container outright, or attach to a per-run helper while it
runs. A sandbox member holds no Docker socket, and the host's rules give it no
route to the host. So the helper is reachable by exactly whom a per-run helper
is reachable by, for longer, and anyone who could use the longer window
already has more.

**How a request is bound to the guard.** By the pipe alone, with no token: the
only other writers hold the Docker socket, and a secret would not stop them.
Each request carries a fresh random id that its answer must echo, so the guard
never takes an answer for another request's, whether a stale one or one for a
request someone attached and wrote. Output that arrives between steps retires
the helper unread at the next step's check; output after that check is read as
the answer and refused unless it is one line with this request's id.

**What decides a rule.** Only the guard's requests, built exactly as for a
per-run helper: from this deployment's configuration, the relays Docker
reports, and the SSH targets and bridge names the guard checks before it asks.
The helper adds no input of its own. Neither a sandbox member nor anything on
the network can write it a request or read its answer.

**What it can do.** What a per-run helper can, from the same flags:
``NET_ADMIN`` in the host's network namespace and nothing else. It runs only
the per-run helper's program, one request at a time, and gives each request at
most :data:`SERVE_REQUEST_S` before it exits. As the container's first process
its exit ends every process in the container, so the ``nft`` call under way
ends with it.

**What persists between requests.** The process and the modules it imported,
nothing else. Each request runs the program with a fresh Python globals dict
and streams of its own, and every listing in an answer comes from an ``nft``
call made for that request. Nothing is read from the kernel ahead of a request
or kept after it. The ``nft`` calls a step starts find ``/dev/null`` as their
stdin, so nothing they do can read the next step's request.

**Read-backs on the helper that wrote.** A read-back may run on the helper
that applied the update it checks, as an install and its read-back already
share one per-run helper. A read-back is independent of the request it
verifies because it lists the kernel's tables afresh and the guard compares
them with what it rendered, not because another process asks. The program is
the same trusted code from the same image either way.

**How long it lives.** At most :data:`SERVE_IDLE_S` without a request; the
guard replaces it at its first step after :data:`RETIRE_AGE_S`, and stops it
when the guard closes or its process exits normally. A guard process that dies
first leaves it running only until the end of its stdin reaches it, and for at
most :data:`SERVE_REQUEST_S` more if a request is under way. When that process
ends, even by SIGKILL, the kernel closes its end of the pipe. The ``docker run
-i`` client then sends the daemon EOF (docker/cli
``cli/command/container/hijack.go``, ``beginInputStream``), and because an
attached ``docker run -i`` creates the container with ``StdinOnce``
(``cli/command/container/opts.go``), the daemon closes the container's stdin
when that stream ends or the client's connection drops (moby
``container/stream/attach.go``, ``CopyStreams``). The helper reads EOF and
exits, and ``--rm`` (``AutoRemove``, which the daemon applies, not the client)
removes the container.

**Failure, and no late write.** A step the helper does not finish with an
answer the guard accepts is run again on a per-run helper, whose answer is
final, so a failed check raises exactly as it did. That covers a helper that
does not start, does not take its request, exits, answers late, answers with
anything but one line for that request, or reports that the step failed.
Before the step runs again the guard removes the helper by name (``docker rm
-f``, which ends every process in its container, a stuck ``nft`` included) and
confirms that it is gone, so nothing it still held can land after the re-run
or after a later step. If that cannot be confirmed within
:data:`REMOVE_TIMEOUT_S`, the step fails as a timed-out per-run helper does, and
nothing runs it again. For :data:`COOLDOWN_S` after a failure every step runs
on per-run helpers, so a helper that fails every time costs one failure per
period rather than one per step. A helper that idled out just as a request
reached it (its exit status :data:`IDLE_EXIT`, before it read the request) has
not failed: a new one takes the step.

**One minute for a step.** The long-lived helper gives a step
:data:`SERVE_REQUEST_S` and :data:`ANSWER_MARGIN_S` (and
:data:`START_ALLOWANCE_S` more when the step starts it), removing a failed one
takes at most :data:`REMOVE_TIMEOUT_S`, and the per-run helper that runs the
step again gets what is left of the minute a per-run helper always had. So a
step takes no longer than it could before.

**A step applied in part.** A step is a few ``nft`` calls: an optional
install, reads, then one or two set updates. Each ``nft -f`` transaction is
atomic, but a helper that fails between two of them has applied the first, as
a per-run helper that fails there has. Running the step again is safe: the
install is idempotent (:func:`cloud_agent_shared.egress.render_ruleset`), and
an update reads its set again and applies only what differs from what it
wants, so an update already applied changes nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import selectors
import subprocess
import tempfile
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Protocol, TypeVar

from cloud_agent_host.logging_utils import get_logger

logger = get_logger(__name__)

#: How long the helper waits for a request before it exits by itself, so a
#: process that stops using it does not keep it standing. The sweep reads the
#: tables every five seconds while groups run, well within this.
SERVE_IDLE_S = 15
#: How long the helper gives one request before it exits. A step is a few
#: ``nft`` calls; one this slow is run again on a per-run helper.
SERVE_REQUEST_S = 10
#: How long the guard keeps using an idle helper: five seconds inside
#: :data:`SERVE_IDLE_S`, so a request does not meet a helper on its way out.
RETIRE_IDLE_S = 10.0
#: How long the guard keeps using one helper at all. The daemon keeps a
#: container's output in its log until the container is removed, so a fresh
#: helper now and then bounds what that log holds.
RETIRE_AGE_S = 300.0
#: How long the guard waits for an answer beyond :data:`SERVE_REQUEST_S`.
ANSWER_MARGIN_S = 2.0
#: How much longer a step that starts the helper may take, for the container
#: to start.
START_ALLOWANCE_S = 15.0
#: How long removing a helper that failed a step, and confirming it is gone,
#: may take before the step fails instead.
REMOVE_TIMEOUT_S = 10.0
#: How long the guard waits for a helper to end on its own when it closes.
CLOSE_GRACE_S = 2.0
#: How long every step runs on per-run helpers after the long-lived one failed.
COOLDOWN_S = 300.0
#: The most an answer may hold; a listing of this deployment's tables is far
#: smaller.
ANSWER_MAX = 16 << 20
#: The helper's exit status when it ends after :data:`SERVE_IDLE_S` without a
#: request, and when a request runs past :data:`SERVE_REQUEST_S`.
IDLE_EXIT = 4
OVERDUE_EXIT = 3
#: How much of a failed helper's client's stderr a warning repeats.
_STDERR_TAIL = 600

_Accepted = TypeVar("_Accepted")

# The long-lived helper's program. ``PROGRAM`` is the per-run helper's own,
# run for each request with that request as its stdin, with a fresh globals
# dict and streams of its own, exactly as a per-run helper would run it.
# Requests come on a descriptor of their own, and fd 0 becomes /dev/null, which
# the nft calls inherit: nothing a step starts can read the next step's
# request. Each alarm is set before its handler, so an idle alarm that fires
# while a request is being read ends the helper with IDLE_EXIT and the request
# unrun; either deadline ends the whole helper.
_SERVE = """
import io
import json
import os
import signal
import sys

PROGRAM = {program}
requests, answers = os.fdopen(os.dup(0), "rb"), sys.stdout.buffer
null = os.open(os.devnull, os.O_RDONLY)
os.dup2(null, 0)
os.close(null)


def idle(signum, frame):
    os._exit({idle_exit})


def overdue(signum, frame):
    os._exit({overdue_exit})


while True:
    signal.alarm({idle_s})
    signal.signal(signal.SIGALRM, idle)
    line = requests.readline()
    signal.alarm({request_s})
    signal.signal(signal.SIGALRM, overdue)
    if not line:
        break
    frame = json.loads(line)
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(frame["stdin"]), io.StringIO(), io.StringIO()
    try:
        exec(PROGRAM, dict(__name__="__main__"))
        code = 0
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else int(exit_.code is not None)
    finally:
        output, errors = sys.stdout.getvalue(), sys.stderr.getvalue()
        sys.stdin, sys.stdout, sys.stderr = sys.__stdin__, sys.__stdout__, sys.__stderr__
    answer = dict(id=frame["id"], returncode=code, stdout=output, stderr=errors)
    answers.write(json.dumps(answer).encode() + b"\\n")
    answers.flush()
"""


def serve_program(
    program: str, *, idle_s: int = SERVE_IDLE_S, request_s: int = SERVE_REQUEST_S
) -> str:
    """The long-lived helper's program: ``program`` run afresh for each request on its stdin."""
    # alarm(0) would cancel a deadline rather than set one.
    return _SERVE.format(
        program=repr(program),
        idle_s=max(1, int(idle_s)),
        request_s=max(1, int(request_s)),
        idle_exit=IDLE_EXIT,
        overdue_exit=OVERDUE_EXIT,
    )


class HelperProcess(Protocol):
    """The long-lived helper's ``docker run`` client, as :class:`subprocess.Popen` presents it."""

    stdin: IO[bytes] | None
    stdout: IO[bytes] | None

    def poll(self) -> int | None:
        """Its exit status, or ``None`` while it runs."""
        ...

    def wait(self, timeout: float | None = None) -> int:
        """Wait for it to exit."""
        ...

    def kill(self) -> None:
        """Kill it."""
        ...


#: Starts a helper's client, given its command line and the descriptor its
#: stderr goes to.
Spawn = Callable[[list[str], int], HelperProcess]
#: Removes the helper container of that name within that many seconds, and
#: says whether it is gone.
Remove = Callable[[str, float], bool]


def spawn_helper(argv: list[str], errors: int) -> HelperProcess:
    """Start a helper's ``docker run`` client, its stdin and stdout pipes to this process."""
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=errors,
        bufsize=0,
    )


def remove_helper(docker_binary: str, name: str, timeout: float) -> bool:
    """Remove a helper container by name, and say whether it is gone.

    ``docker rm -f`` kills the container's first process, which ends every
    process in its PID namespace, and returns once the container is removed;
    for a container that is gone already it succeeds too (an older client says
    "No such container"). A removal another one has under way is waited for.
    """
    deadline = time.monotonic() + timeout
    while (left := deadline - time.monotonic()) > 0:
        try:
            removed = subprocess.run(
                [docker_binary, "rm", "-f", name],
                capture_output=True,
                text=True,
                timeout=left,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if removed.returncode == 0 or "no such container" in removed.stderr.lower():
            return True
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    return False


class HelperStuck(Exception):
    """A long-lived helper failed a step and could not be shown removed: nothing may rerun it."""


class _Failed(Exception):
    """The long-lived helper did not finish a step with one readable answer."""


class _Ended(_Failed):
    """The helper's output ended, or its stdin closed, before it answered."""


class _Retired(Exception):
    """The helper idled out before it read the request: no failure, and the request never ran."""


@dataclass
class _Running:
    """The helper now running, kept apart from :class:`LongLivedHelper` for its finalizer."""

    process: HelperProcess | None = None
    #: The unlinked file its client's stderr goes to, for the warning if it fails.
    errors: int | None = None
    name: str = ""
    argv: list[str] = field(default_factory=list)
    started_at: float = 0.0
    #: When it last answered, or started.
    used_at: float = 0.0
    #: Clients of helpers stopped with nothing under way, not yet reaped.
    retiring: list[tuple[HelperProcess, int | None]] = field(default_factory=list)


class LongLivedHelper:
    """One guard process's long-lived helper: started when first needed, one step at a time."""

    def __init__(self, spawn: Spawn, remove: Remove, *, name_prefix: str) -> None:
        self._spawn = spawn
        self._remove = remove
        self._name_prefix = name_prefix
        #: Held for one step, and while the helper is started or stopped. Never
        #: waited for: a step that finds it held runs on a per-run helper.
        self._slot = threading.Lock()
        self._running = _Running()
        #: Until when every step runs on per-run helpers, after a failure.
        self._resume_at = 0.0
        self._closed = False
        # At close, garbage collection or interpreter exit, whichever comes
        # first: a client left to outlive this process would be inherited by
        # its parent, which in a container may reap nothing.
        self._stop = weakref.finalize(self, _shutdown, self._running, remove)

    def run(
        self,
        argv: list[str],
        stdin: str,
        *,
        deadline: float,
        accept: Callable[[subprocess.CompletedProcess[str]], _Accepted],
    ) -> _Accepted | None:
        """Run one step on the long-lived helper, or return ``None`` for a per-run helper to run it.

        ``argv`` is the helper's command line, without its name, ``stdin`` the
        step's request as a per-run helper reads it, and ``deadline`` the
        monotonic time by which the step must be done, on whichever helper.
        ``accept`` reads a successful answer and raises if it cannot; what it
        returns is returned. ``None`` means no accepted answer: the helper was
        busy, closed or cooling down, or it failed the step and was removed.
        Raises :class:`HelperStuck` when a helper that failed the step could not
        be shown removed.
        """
        if not self._slot.acquire(blocking=False):
            return None
        try:
            self._reap()
            if self._closed or time.monotonic() < self._resume_at:
                return None
            return self._serve(argv, stdin, deadline, accept)
        finally:
            self._slot.release()

    def close(self) -> None:
        """Stop the helper, after any step it is running, and start no other."""
        with self._slot:
            self._closed = True
            self._stop()

    def _serve(
        self,
        argv: list[str],
        stdin: str,
        deadline: float,
        accept: Callable[[subprocess.CompletedProcess[str]], _Accepted],
    ) -> _Accepted | None:
        # A second attempt only after a helper that idled out before it read
        # the request: that request never ran.
        for _attempt in range(2):
            try:
                started = self._ready(argv)
                answer_by = time.monotonic() + SERVE_REQUEST_S + ANSWER_MARGIN_S
                answer_by += START_ALLOWANCE_S if started else 0.0
                answer = _exchange(self._running, argv, stdin, min(answer_by, deadline))
                result = accept(answer)
            except _Retired:
                self._retire()
                continue
            except Exception as exc:
                self._abandon(str(exc) or type(exc).__name__, deadline)
                return None
            except BaseException:
                # Interrupted mid-step: the helper may still be running it.
                with contextlib.suppress(HelperStuck):
                    self._abandon("was interrupted", deadline)
                raise
            self._running.used_at = time.monotonic()
            return result
        return None

    def _ready(self, argv: list[str]) -> bool:
        """Have a helper waiting for the next step; whether it was started for this one."""
        running = self._running
        process = running.process
        now = time.monotonic()
        if process is not None and (
            argv != running.argv
            or now - running.used_at >= RETIRE_IDLE_S
            or now - running.started_at >= RETIRE_AGE_S
            # Output it was not asked for, or the end of its output: it is not
            # waiting for a request, and nothing it wrote is read as an answer.
            or not _waiting(process)
        ):
            self._retire()
            process = None
        if process is not None:
            return False
        errors, path = tempfile.mkstemp(prefix="network-guard-helper-")
        os.unlink(path)
        name = f"{self._name_prefix}-{secrets.token_hex(6)}"
        try:
            process = self._spawn([*argv[:2], "--name", name, *argv[2:]], errors)
        except BaseException:
            _close(errors)
            raise
        running.process, running.errors, running.name = process, errors, name
        running.argv = list(argv)
        running.started_at = running.used_at = time.monotonic()
        for stream in (process.stdin, process.stdout):
            if stream is None:
                raise _Failed("started without its pipes")
            os.set_blocking(stream.fileno(), False)
        return True

    def _retire(self) -> None:
        """Stop a helper with nothing under way: close its pipes, and reap its client later."""
        running = self._running
        if running.process is not None:
            _close_pipes(running.process)
            running.retiring.append((running.process, running.errors))
        running.process, running.errors = None, None

    def _reap(self) -> None:
        """Reap clients that have exited, the running one's included, without waiting for any."""
        running = self._running
        if running.process is not None and running.process.poll() is not None:
            self._retire()
        still = []
        for client, errors in running.retiring:
            if client.poll() is None:
                still.append((client, errors))
            else:
                _close(errors)
        running.retiring[:] = still

    def _abandon(self, failure: str, deadline: float) -> None:
        """Remove a helper that failed a step, and confirm it is gone, before the step reruns."""
        running = self._running
        process, errors = running.process, running.errors
        name = running.name if process is not None else ""
        running.process, running.errors = None, None
        self._resume_at = time.monotonic() + COOLDOWN_S
        removed = True
        status: int | None = None
        if process is not None:
            _close_pipes(process)
            left = deadline - time.monotonic()
            removed = self._remove(name, min(REMOVE_TIMEOUT_S, max(1.0, left)))
            status = _ended(process)
        logger.warning(
            "network_guard_helper_discarded",
            extra={
                "event": "network_guard_helper_discarded",
                "detail": failure[:300],
                "helper": name,
                "removed": removed,
                "exit_status": status,
                "stderr": _tail(errors),
                "cooldown_s": COOLDOWN_S,
            },
        )
        _close(errors)
        if not removed:
            raise HelperStuck(
                f"the long-lived helper {name} {failure}, and could not be shown removed "
                f"within {REMOVE_TIMEOUT_S:.0f}s"
            )


def _shutdown(running: _Running, remove: Remove) -> None:
    """Stop the running helper and reap every client: at close, or when this process exits."""
    process, errors, name = running.process, running.errors, running.name
    running.process, running.errors = None, None
    if process is not None:
        _close_pipes(process)
        try:
            process.wait(timeout=CLOSE_GRACE_S)
        except subprocess.TimeoutExpired:
            remove(name, REMOVE_TIMEOUT_S)
            _ended(process)
        _close(errors)
    for client, retired in running.retiring:
        _ended(client)
        _close(retired)
    running.retiring.clear()


def _close_pipes(process: HelperProcess) -> None:
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


def _ended(process: HelperProcess) -> int | None:
    """Reap a client whose container is stopped or gone, killing it if it lingers."""
    try:
        return process.wait(timeout=CLOSE_GRACE_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            return process.wait(timeout=CLOSE_GRACE_S)
    return None


def _waiting(process: HelperProcess) -> bool:
    """Whether the helper is alive and has written nothing since its last answer."""
    if process.poll() is not None or process.stdout is None:
        return False
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        return not selector.select(0)


def _tail(errors: int | None) -> str:
    """The last of what a failed helper's client wrote to stderr."""
    if errors is None:
        return ""
    with contextlib.suppress(OSError):
        size = os.lseek(errors, 0, os.SEEK_END)
        os.lseek(errors, max(0, size - _STDERR_TAIL), os.SEEK_SET)
        return os.read(errors, _STDERR_TAIL).decode("utf-8", "replace").strip()
    return ""


def _close(fd: int | None) -> None:
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)


def _exchange(
    running: _Running, argv: list[str], stdin: str, deadline: float
) -> subprocess.CompletedProcess[str]:
    """Send the helper one request and read its one answer, as a per-run helper's result."""
    process = running.process
    if process is None or process.stdin is None or process.stdout is None:
        raise _Failed("has no pipes")
    request_id = secrets.token_hex(8)
    frame = json.dumps({"id": request_id, "stdin": stdin}).encode() + b"\n"
    try:
        _send(process.stdin, frame, deadline)
        line = _receive(process.stdout, deadline)
    except _Ended as exc:
        # Its exit status says whether it left before reading the request.
        with contextlib.suppress(subprocess.TimeoutExpired):
            if process.wait(timeout=min(CLOSE_GRACE_S, _left(deadline))) == IDLE_EXIT:
                raise _Retired from exc
        raise
    output, errors = _answer(line, request_id)
    return subprocess.CompletedProcess(argv, 0, output, errors)


def _answer(line: bytes, request_id: str) -> tuple[str, str]:
    """A successful step's output and errors from its answer line; raises on anything else."""
    try:
        answer = json.loads(line)
    except ValueError as exc:
        raise _Failed("answered with unreadable JSON") from exc
    if not isinstance(answer, dict) or answer.get("id") != request_id:
        raise _Failed("answered something other than this request")
    code, output, errors = answer.get("returncode"), answer.get("stdout"), answer.get("stderr")
    if type(code) is not int or not isinstance(output, str) or not isinstance(errors, str):
        raise _Failed("answered without a status, output and errors")
    if code:
        # The per-run helper runs it again and reports the failure as before.
        raise _Failed(f"failed the step: {errors.strip()[:200]}")
    return output, errors


def _send(stream: IO[bytes], data: bytes, deadline: float) -> None:
    """Write all of ``data`` before ``deadline``, however slowly the client reads it."""
    fd = stream.fileno()
    view = memoryview(data)
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while view:
            if not selector.select(_left(deadline)):
                raise _Failed("did not take its request in time")
            try:
                view = view[os.write(fd, view) :]
            except BlockingIOError:
                continue
            except BrokenPipeError as exc:
                raise _Ended("closed its stdin before it took its request") from exc
            except OSError as exc:
                raise _Failed(f"could not take its request: {exc}") from exc


def _receive(stream: IO[bytes], deadline: float) -> bytes:
    """One answer line, read before ``deadline``; raises on an end, a delay or a second line."""
    fd = stream.fileno()
    received = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            if not selector.select(_left(deadline)):
                raise _Failed("did not answer in time")
            try:
                chunk = os.read(fd, 1 << 16)
            except BlockingIOError:
                continue
            if not chunk:
                raise _Ended("exited before it answered")
            received += chunk
            # Only the new chunk can hold the end of the line.
            end = chunk.find(b"\n")
            if end != -1:
                if end != len(chunk) - 1:
                    raise _Failed("wrote more than one answer")
                return bytes(received)
            if len(received) > ANSWER_MAX:
                raise _Failed(f"answered more than {ANSWER_MAX} bytes")


def _left(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


__all__ = [
    "ANSWER_MARGIN_S",
    "COOLDOWN_S",
    "IDLE_EXIT",
    "OVERDUE_EXIT",
    "REMOVE_TIMEOUT_S",
    "RETIRE_AGE_S",
    "RETIRE_IDLE_S",
    "SERVE_IDLE_S",
    "SERVE_REQUEST_S",
    "START_ALLOWANCE_S",
    "HelperProcess",
    "HelperStuck",
    "LongLivedHelper",
    "Remove",
    "Spawn",
    "remove_helper",
    "serve_program",
    "spawn_helper",
]
