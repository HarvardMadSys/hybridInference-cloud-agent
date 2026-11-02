"""The long-lived host-rule helper: one ``docker run`` that takes a guard's steps in turn.

Every step of :class:`~cloud_agent_host.network_guard.HostNetworkGuard` used to
start a helper container of its own, and starting one costs far more than the
``nft`` calls it makes: about 264 ms on runc, against milliseconds. The
sandbox manager's sweep starts one every five seconds while groups run, and a
cold start waits for two. So each guard process keeps one helper running while
it uses it and hands it one step at a time on its standard input.

**The same helper, kept running.** Its command line is a per-run helper's,
flag for flag (``HostNetworkGuard._command``): this process's own image by its
``sha256:`` id, never pulled; the host's network namespace under runc;
``NET_ADMIN`` and no other capability; no-new-privileges; a read-only root;
``--rm``; and ``--interactive``, which a per-run helper has too, for its
request. Only the program differs (:func:`serve_program`): it reads one request
per line, runs the per-run helper's own program on it, writes one answer line,
and waits for the next.

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
connect to. Besides that pipe only the Docker daemon reaches it, through
``docker attach`` (its stdin is open) or ``docker exec``. Both need the Docker
socket, which is already the host's authority: whoever holds it can start a
privileged container outright, or attach to a per-run helper while it runs. A
sandbox member holds no Docker socket, and the host's rules give it no route to
the host. So the helper is reachable by exactly whom a per-run helper is
reachable by, for longer, and anyone who could use the longer window already
has more.

**How a request is bound to the guard.** By the pipe alone, with no token: the
only other writers hold the Docker socket, and a secret would not stop them.
Each request carries a fresh random id that its answer must echo, so the guard
never takes an answer for another request's, whether a stale one or one for a
request someone attached and wrote; and a helper that writes anything unasked
between requests is replaced without being read.

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
nothing else. Each request runs the program in a namespace of its own with
streams of its own, and every listing in an answer comes from an ``nft`` call
made for that request. Nothing is read from the kernel ahead of a request or
kept after it.

**Read-backs on the helper that wrote.** A read-back may run on the helper
that applied the update it checks, as an install and its read-back already
share one per-run helper. A read-back is independent of the request it
verifies because it lists the kernel's tables afresh and the guard compares
them with what it rendered, not because another process asks. The program is
the same trusted code from the same image either way.

**How long it lives.** At most :data:`SERVE_IDLE_S` without a request; the
guard replaces it at its first step after :data:`RETIRE_AGE_S`; and never past
the guard's process. When that process ends, even by SIGKILL, the kernel
closes its end of the pipe. The ``docker run -i`` client then sends the daemon
EOF (docker/cli
``cli/command/container/hijack.go``, ``beginInputStream``), and because an
attached ``docker run -i`` creates the container with ``StdinOnce``
(``cli/command/container/opts.go``), the daemon closes the container's stdin
when that stream ends or the client's connection drops (moby
``container/stream/attach.go``, ``CopyStreams``). The helper reads EOF and
exits, and ``--rm`` (``AutoRemove``, which the daemon applies, not the client)
removes the container.

**Failure.** Any step it does not finish with an answer the guard accepts is
run again on a per-run helper, whose answer is final, so a failed check raises
exactly as it did. That covers a helper that does not start, does not take its
request, exits, answers late, answers with anything but one line for that
request, or reports that the step failed. The helper is discarded first, and
for :data:`COOLDOWN_S` every step runs on per-run helpers, so a helper that
fails every time costs one failure per period rather than one per step; after
that the next step starts a new one. One that ends cleanly, idle or retired,
is replaced by the next step at once.

**A step applied in part.** A step is a few ``nft`` calls: an optional
install, reads, then one or two set updates. Each ``nft -f`` transaction is
atomic, but a helper that fails between two of them has applied the first, as
a per-run helper that fails there has. Running the step again is safe: the
install is idempotent (:func:`cloud_agent_shared.egress.render_ruleset`), and
an update reads its set again and applies only what differs from what it
wants, so an update already applied changes nothing.

**A helper that outlives its failure.** Discarding closes its stdin and, after
:data:`DISCARD_GRACE_S`, kills its client, but the container may still be in
its last request, as a per-run helper may be after its client is killed at its
timeout. It can run no other request, since its stdin is closed and requests
go one at a time, and its own deadline ends that one unless an ``nft`` call is
stuck in the kernel. The guard waits for an answer as long as it waits for a
per-run helper, so it moves on from a helper that may still write no sooner
than it does today. Only when the helper's client dies first does the guard
move on at once, as it does when a per-run helper's client dies; the helper
then reads EOF, and its own deadline bounds that request, where a per-run
helper's has no bound. A late write of the re-run step converges the bridge to
the same target; like a per-run helper's late write, it could overtake a later
update of that bridge, and no more often.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import selectors
import subprocess
import threading
import time
from collections.abc import Callable
from typing import IO, Protocol, TypeVar

from cloud_agent_host.logging_utils import get_logger

logger = get_logger(__name__)

#: How long the helper waits for a request before it exits by itself, so a
#: process that stops using it does not keep it standing. The sweep reads the
#: tables every five seconds while groups run, well within this.
SERVE_IDLE_S = 15
#: How long the helper gives one request before it exits. A step is a few
#: ``nft`` calls; one this slow is run again on a per-run helper, which has
#: the whole minute.
SERVE_REQUEST_S = 20
#: How long the guard keeps using an idle helper: well inside
#: :data:`SERVE_IDLE_S`, so a request never meets a helper on its way out.
RETIRE_IDLE_S = 10.0
#: How long the guard keeps using one helper at all. The daemon keeps a
#: container's output in its log until the container is removed, so a fresh
#: helper now and then bounds what that log holds.
RETIRE_AGE_S = 300.0
#: How long a discarded helper's client has to exit once its stdin is closed,
#: before it is killed.
DISCARD_GRACE_S = 2.0
#: How long every step runs on per-run helpers after the long-lived one failed.
COOLDOWN_S = 300.0

_Accepted = TypeVar("_Accepted")

# The long-lived helper's program. ``PROGRAM`` is the per-run helper's own,
# run for each request with that request as its stdin, in a namespace and
# streams of its own, exactly as a per-run helper would run it. Requests come
# on a descriptor of their own, and fd 0 becomes /dev/null, which the nft calls
# inherit: nothing a step starts can read the next step's request. A deadline,
# idle or per request, ends the whole helper.
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


def stop(signum, frame):
    os._exit(3)


signal.signal(signal.SIGALRM, stop)
while True:
    signal.alarm({idle_s})
    line = requests.readline()
    signal.alarm({request_s})
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
        program=repr(program), idle_s=max(1, int(idle_s)), request_s=max(1, int(request_s))
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


Spawn = Callable[[list[str]], HelperProcess]


def spawn_helper(argv: list[str]) -> HelperProcess:
    """Start a helper's ``docker run`` client, its stdin and stdout pipes to this process.

    Its stderr is dropped: a step it fails runs again on a per-run helper,
    whose errors are reported.
    """
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )


class _Failed(Exception):
    """The long-lived helper did not finish a step with one readable answer."""


class LongLivedHelper:
    """One guard process's long-lived helper: started when first needed, one step at a time."""

    def __init__(self, spawn: Spawn = spawn_helper) -> None:
        self._spawn = spawn
        #: Held for one step, and while the helper is started or stopped. Never
        #: waited for: a step that finds it held runs on a per-run helper.
        self._slot = threading.Lock()
        self._process: HelperProcess | None = None
        #: The command line it runs; a step that needs another gets a new one.
        self._argv: list[str] = []
        self._started_at = 0.0
        #: When it last answered, or started.
        self._used_at = 0.0
        #: Until when every step runs on per-run helpers, after a failure.
        self._resume_at = 0.0
        self._closed = False

    def run(
        self,
        argv: list[str],
        stdin: str,
        *,
        timeout: float,
        accept: Callable[[subprocess.CompletedProcess[str]], _Accepted],
    ) -> _Accepted | None:
        """Run one step on the long-lived helper, or return ``None`` for a per-run helper to run it.

        ``argv`` is the helper's command line, ``stdin`` the step's request as
        a per-run helper reads it, and ``timeout`` how long the step may take,
        starting the helper included. ``accept`` reads a successful answer and
        raises if it cannot; what it returns is returned. ``None`` means no
        accepted answer: the helper was busy, closed or cooling down, or it
        failed the step and was discarded.
        """
        if not self._slot.acquire(blocking=False):
            return None
        try:
            if self._closed or time.monotonic() < self._resume_at:
                return None
            return self._serve(argv, stdin, time.monotonic() + timeout, accept)
        finally:
            self._slot.release()

    def close(self) -> None:
        """Stop the helper, after any step it is running, and start no other."""
        with self._slot:
            self._closed = True
            self._discard(None)

    def _serve(
        self,
        argv: list[str],
        stdin: str,
        deadline: float,
        accept: Callable[[subprocess.CompletedProcess[str]], _Accepted],
    ) -> _Accepted | None:
        try:
            process = self._ready(argv)
            result = accept(_exchange(process, argv, stdin, deadline))
        except Exception as exc:
            self._discard(str(exc) or type(exc).__name__)
            return None
        except BaseException:
            # Interrupted mid-step: an answer may still be on its way.
            self._discard("interrupted")
            raise
        self._used_at = time.monotonic()
        return result

    def _ready(self, argv: list[str]) -> HelperProcess:
        """The helper for the next step: the running one while it may be used, or a new one."""
        process = self._process
        now = time.monotonic()
        if process is not None and (
            argv != self._argv
            or now - self._used_at >= RETIRE_IDLE_S
            or now - self._started_at >= RETIRE_AGE_S
            or process.poll() is not None
            # Output it was not asked for, or the end of its output: it is not
            # waiting for a request, and nothing it wrote is read as an answer.
            or process.stdout is None
            or select.select([process.stdout], [], [], 0)[0]
        ):
            self._discard(None)
            process = None
        if process is None:
            process = self._spawn(list(argv))
            self._process, self._argv = process, list(argv)
            self._started_at = self._used_at = now
            for stream in (process.stdin, process.stdout):
                if stream is None:
                    raise _Failed("started without its pipes")
                os.set_blocking(stream.fileno(), False)
        return process

    def _discard(self, failure: str | None) -> None:
        """Stop the helper, if one runs; after a ``failure``, run per-run helpers for a while."""
        process, self._process = self._process, None
        if failure is not None:
            self._resume_at = time.monotonic() + COOLDOWN_S
            logger.warning(
                "network_guard_helper_discarded",
                extra={
                    "event": "network_guard_helper_discarded",
                    "detail": failure[:300],
                    "cooldown_s": COOLDOWN_S,
                },
            )
        if process is not None:
            _stop(process)


def _stop(process: HelperProcess) -> None:
    """Close a helper's pipes, which ends it, and kill its client if it does not exit."""
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()
    try:
        process.wait(timeout=DISCARD_GRACE_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=DISCARD_GRACE_S)


def _exchange(
    process: HelperProcess, argv: list[str], stdin: str, deadline: float
) -> subprocess.CompletedProcess[str]:
    """Send the helper one request and read its one answer, as a per-run helper's result."""
    if process.stdin is None or process.stdout is None:
        raise _Failed("has no pipes")
    request_id = os.urandom(8).hex()
    _send(process.stdin, json.dumps({"id": request_id, "stdin": stdin}).encode() + b"\n", deadline)
    output, errors = _answer(_receive(process.stdout, deadline), request_id)
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
            except OSError as exc:
                raise _Failed(f"could not take its request: {exc}") from exc


def _receive(stream: IO[bytes], deadline: float) -> bytes:
    """One answer line, read before ``deadline``; raises on an end, a delay or a second line."""
    fd = stream.fileno()
    received = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while b"\n" not in received:
            if not selector.select(_left(deadline)):
                raise _Failed("did not answer in time")
            try:
                chunk = os.read(fd, 1 << 16)
            except BlockingIOError:
                continue
            if not chunk:
                raise _Failed("exited before it answered")
            received += chunk
    if received.index(b"\n") != len(received) - 1:
        raise _Failed("wrote more than one answer")
    return bytes(received)


def _left(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


__all__ = [
    "COOLDOWN_S",
    "DISCARD_GRACE_S",
    "RETIRE_AGE_S",
    "RETIRE_IDLE_S",
    "SERVE_IDLE_S",
    "SERVE_REQUEST_S",
    "HelperProcess",
    "LongLivedHelper",
    "Spawn",
    "serve_program",
    "spawn_helper",
]
