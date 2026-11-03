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
when the guard closes or its process exits normally, after any step under way.
At interpreter exit that wait lasts at most :data:`SHUTDOWN_WAIT_S`, and a
helper a step still holds then ends as follows. A guard process that dies
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
Before the step runs again the guard kills the helper's ``docker run`` client
and reaps it, and only then removes the container by name (``docker rm -f``,
which ends every process in it) and confirms that it is gone. The client goes
first because ``docker rm -f`` succeeds for a name no container has yet too: a
client still creating its container would start it after such a removal, and
the request it holds would run after all. A dead client creates, attaches and
starts nothing more. A create the daemon still had under way can leave a
container that was created and never started, and holds no request; later
steps remove that name again for :data:`LEFTOVER_S`.

If the client is not reaped within :data:`KILL_REAP_S`, or the removal is not
confirmed within :data:`REMOVE_TIMEOUT_S`, the step fails as a timed-out
per-run helper does, and nothing runs it again. So does a helper whose ``nft``
call is in uninterruptible sleep: ``docker rm -f`` cannot end it, and the
removal stays unconfirmed. Later steps go ahead, and each first tries to
remove that name again, within :data:`SWEEP_S`; until one shows it gone, what
that helper held may still land, as what a timed-out per-run helper held may.
For :data:`COOLDOWN_S` after a failure every step runs on per-run helpers, so
a helper that fails every time costs one failure per period rather than one
per step. A helper that idled out just as a request reached it (its exit
status :data:`IDLE_EXIT`, before it read the request) has not failed: a new
one takes the step.

**One minute for a step.** A step's minute starts once its command line, and
with it the image's id, is known, as a per-run helper's always did, and every
part of the step on the long-lived helper ends with it. Removing earlier
failures' names again takes at most :data:`SWEEP_S`. The helper gets
:data:`SERVE_REQUEST_S` and :data:`ANSWER_MARGIN_S`, and
:data:`START_ALLOWANCE_S` more when the step starts it; a second helper gets
the same only after the first idled out before it read the request, which
takes at most :data:`CLOSE_GRACE_S` to see. Taking down a helper that failed
takes at most :data:`KILL_REAP_S` to kill and reap its client and
:data:`REMOVE_TIMEOUT_S` to remove its container. All of that together is less
than the minute, and the per-run helper that runs the step again gets what is
left of it, so a step takes no longer than it could before.

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
#: How long the guard waits for a failed helper's client to exit once killed;
#: SIGKILL ends a client in milliseconds.
KILL_REAP_S = 2.0
#: How long removing a helper that failed a step, and confirming it is gone,
#: may take before the step fails instead.
REMOVE_TIMEOUT_S = 10.0
#: How long after a failed helper's client is killed later steps remove its
#: name again: a create the daemon still had under way has settled by then. A
#: name not yet shown gone is kept until it is.
LEFTOVER_S = 30.0
#: How much of a step removing those names again may take.
SWEEP_S = 2.0
#: How long the guard waits for a helper to end on its own when it closes.
CLOSE_GRACE_S = 2.0
#: How long stopping the helper at interpreter exit waits for a step under way.
SHUTDOWN_WAIT_S = 5.0
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
    process in its PID namespace, and returns once the container is removed.
    It succeeds for a name no container has, too (an older client says "No
    such container"), so it cannot tell a container that is gone from one not
    created yet: the guard kills the helper's client before it calls this. A
    removal another one has under way is waited for. A process the kill cannot
    end, such as one in uninterruptible sleep, leaves the removal unconfirmed.
    """
    deadline = time.monotonic() + timeout
    while (left := deadline - time.monotonic()) > 0:
        try:
            removed = subprocess.run(
                [docker_binary, "rm", "-f", name],
                capture_output=True,
                text=True,
                # Whatever the client prints, its exit status still decides.
                errors="replace",
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


@dataclass(eq=False)
class _Helper:
    """One helper container: its ``docker run`` client and its name."""

    process: HelperProcess
    name: str
    argv: list[str]
    #: The unlinked file its client's stderr goes to, for the warning if it
    #: fails; ``None`` once closed.
    errors: int | None
    started_at: float
    #: When it last answered, or started.
    used_at: float


@dataclass
class _Running:
    """The guard's helpers, kept apart from :class:`LongLivedHelper` for its finalizer."""

    #: The helper that takes the next step, if one is running.
    helper: _Helper | None = None
    #: Helpers stopped with nothing under way, and failed helpers whose
    #: clients did not exit when killed: reaped at later steps.
    retiring: list[_Helper] = field(default_factory=list)
    #: Names of helpers that failed a step, removed again at later steps: each
    #: until it is shown gone at or after the time it maps to.
    leftovers: dict[str, float] = field(default_factory=dict)


class LongLivedHelper:
    """One guard process's long-lived helper: started when first needed, one step at a time."""

    def __init__(self, spawn: Spawn, remove: Remove, *, name_prefix: str) -> None:
        self._spawn = spawn
        self._remove = remove
        self._name_prefix = name_prefix
        #: Held for one step, and while the helper is started or stopped. A
        #: step never waits for it: one that finds it held runs on a per-run
        #: helper.
        self._slot = threading.Lock()
        self._running = _Running()
        #: Until when every step runs on per-run helpers, after a failure.
        self._resume_at = 0.0
        self._closed = False
        # At close, garbage collection or interpreter exit, whichever comes
        # first: a client left to outlive this process would be inherited by
        # its parent, which in a container may reap nothing.
        self._stop = weakref.finalize(self, _shutdown, self._running, remove, self._slot)

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
            if self._closed:
                return None
            # Cooling down or not: the failure that started the cooldown is
            # what leaves names to remove.
            _sweep(self._running, self._remove, min(time.monotonic() + SWEEP_S, deadline))
            if time.monotonic() < self._resume_at:
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
                helper, started = self._ready(argv)
                answer_by = time.monotonic() + SERVE_REQUEST_S + ANSWER_MARGIN_S
                answer_by += START_ALLOWANCE_S if started else 0.0
                answer = _exchange(helper.process, argv, stdin, min(answer_by, deadline))
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
            helper.used_at = time.monotonic()
            return result
        return None

    def _ready(self, argv: list[str]) -> tuple[_Helper, bool]:
        """A helper waiting for the next step, and whether it was started for this one."""
        helper = self._running.helper
        now = time.monotonic()
        if helper is not None and (
            argv != helper.argv
            or now - helper.used_at >= RETIRE_IDLE_S
            or now - helper.started_at >= RETIRE_AGE_S
            # Output it was not asked for, or the end of its output: it is not
            # waiting for a request, and nothing it wrote is read as an answer.
            or not _waiting(helper.process)
        ):
            self._retire()
            helper = None
        if helper is not None:
            return helper, False
        return self._start(argv), True

    def _start(self, argv: list[str]) -> _Helper:
        errors, path = tempfile.mkstemp(prefix="network-guard-helper-")
        os.unlink(path)
        name = f"{self._name_prefix}-{secrets.token_hex(6)}"
        try:
            process = self._spawn([*argv[:2], "--name", name, *argv[2:]], errors)
        except BaseException:
            _close(errors)
            raise
        now = time.monotonic()
        helper = self._running.helper = _Helper(process, name, list(argv), errors, now, now)
        for stream in (process.stdin, process.stdout):
            if stream is None:
                raise _Failed("started without its pipes")
            os.set_blocking(stream.fileno(), False)
        return helper

    def _retire(self) -> None:
        """Stop a helper with nothing under way: close its pipes, and reap its client later."""
        running = self._running
        if running.helper is not None:
            _close_pipes(running.helper.process)
            running.retiring.append(running.helper)
        running.helper = None

    def _reap(self) -> None:
        """Reap clients that have exited, the running one's included, without waiting for any."""
        running = self._running
        if running.helper is not None and running.helper.process.poll() is not None:
            self._retire()
        still = []
        for helper in running.retiring:
            if helper.process.poll() is None:
                still.append(helper)
            else:
                _close(helper.errors)
        running.retiring[:] = still

    def _abandon(self, failure: str, deadline: float) -> None:
        """Take down a helper that failed a step, and confirm it, before the step runs again."""
        running = self._running
        helper, running.helper = running.helper, None
        self._resume_at = time.monotonic() + COOLDOWN_S
        status: int | None = None
        removed = True
        if helper is not None:
            status, removed = _take_down(running, self._remove, helper, deadline)
        logger.warning(
            "network_guard_helper_discarded",
            extra={
                "event": "network_guard_helper_discarded",
                "detail": failure[:300],
                "helper": helper.name if helper else "",
                "removed": removed,
                "exit_status": status,
                "stderr": _tail(helper.errors) if helper else "",
                "cooldown_s": COOLDOWN_S,
            },
        )
        if helper is None:
            return
        _close(helper.errors)
        helper.errors = None
        if status is None:
            running.retiring.append(helper)
        if not removed:
            what = "its client did not exit" if status is None else "its removal was not confirmed"
            raise HelperStuck(
                f"the long-lived helper {helper.name} {failure}, and could not be shown removed: "
                + what
            )


def _take_down(
    running: _Running, remove: Remove, helper: _Helper, deadline: float
) -> tuple[int | None, bool]:
    """Kill a helper's client and reap it, then remove its container, both by ``deadline``.

    Returns the client's exit status, if it was reaped, and whether both are
    confirmed. The client goes first: once it is dead it creates, attaches and
    starts nothing more, so a removal that found no container stays true.
    """
    with contextlib.suppress(OSError):
        helper.process.kill()
    status = _reaped(helper.process, _within(KILL_REAP_S, deadline))
    _close_pipes(helper.process)
    removed = _removed(remove, helper.name, _within(REMOVE_TIMEOUT_S, deadline))
    # A create the daemon had under way when the client died can still leave a
    # container, created and never started: later steps remove the name again.
    running.leftovers[helper.name] = time.monotonic() + LEFTOVER_S
    return status, removed and status is not None


def _sweep(running: _Running, remove: Remove, until: float) -> None:
    """Remove failed helpers' names again until ``until``, forgetting each once it is done."""
    for name, keep_until in list(running.leftovers.items()):
        left = until - time.monotonic()
        if left <= 0:
            return
        # Tried again at a later step, after the others, unless done now: kept
        # until then, so a step interrupted mid-removal loses no name.
        running.leftovers[name] = running.leftovers.pop(name)
        if _removed(remove, name, left) and time.monotonic() >= keep_until:
            del running.leftovers[name]


def _removed(remove: Remove, name: str, timeout: float) -> bool:
    """Whether ``remove`` shows the container gone; anything it raises counts as not."""
    try:
        return remove(name, timeout)
    except Exception as exc:
        logger.warning(
            "network_guard_helper_removal_failed",
            extra={
                "event": "network_guard_helper_removal_failed",
                "helper": name,
                "detail": f"{type(exc).__name__}: {exc}"[:300],
            },
        )
        return False


def _shutdown(running: _Running, remove: Remove, slot: threading.Lock) -> None:
    """Stop the helper and reap every client: at close, or when this process exits.

    After any step under way, which holds ``slot``. At interpreter exit that
    step's thread may never run again, so the wait is bounded, and a helper a
    step still holds is left to it: it ends once this process's end of its
    stdin closes.
    """
    if not slot.acquire(timeout=SHUTDOWN_WAIT_S):
        return
    try:
        helper, running.helper = running.helper, None
        if helper is not None:
            _close_pipes(helper.process)
            if _reaped(helper.process, CLOSE_GRACE_S) is None:
                # It did not end at EOF on its stdin: down as a failed one goes.
                until = time.monotonic() + KILL_REAP_S + REMOVE_TIMEOUT_S
                _take_down(running, remove, helper, until)
            _close(helper.errors)
        for retired in running.retiring:
            _ended(retired.process)
            _close(retired.errors)
        running.retiring.clear()
        _sweep(running, remove, time.monotonic() + SWEEP_S)
        running.leftovers.clear()
    finally:
        slot.release()


def _close_pipes(process: HelperProcess) -> None:
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


def _reaped(process: HelperProcess, timeout: float) -> int | None:
    """A client's exit status, once it exits within ``timeout``; ``None`` if it does not."""
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


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
    process: HelperProcess, argv: list[str], stdin: str, deadline: float
) -> subprocess.CompletedProcess[str]:
    """Send the helper one request and read its one answer, as a per-run helper's result."""
    if process.stdin is None or process.stdout is None:
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


def _within(limit: float, deadline: float) -> float:
    """``limit`` seconds, cut short at ``deadline``."""
    return max(0.0, min(limit, deadline - time.monotonic()))


def _left(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


__all__ = [
    "ANSWER_MARGIN_S",
    "CLOSE_GRACE_S",
    "COOLDOWN_S",
    "IDLE_EXIT",
    "KILL_REAP_S",
    "LEFTOVER_S",
    "OVERDUE_EXIT",
    "REMOVE_TIMEOUT_S",
    "RETIRE_AGE_S",
    "RETIRE_IDLE_S",
    "SERVE_IDLE_S",
    "SERVE_REQUEST_S",
    "SHUTDOWN_WAIT_S",
    "START_ALLOWANCE_S",
    "SWEEP_S",
    "HelperProcess",
    "HelperStuck",
    "LongLivedHelper",
    "Remove",
    "Spawn",
    "remove_helper",
    "serve_program",
    "spawn_helper",
]
