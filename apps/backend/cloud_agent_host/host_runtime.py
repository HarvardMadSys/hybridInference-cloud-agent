"""One long-lived host service, and one child process per turn it is running.

A turn is claimed, executed synchronously, and reported; the number of turns a
host runs at once is the only concurrency this service has. That number used to
be a Compose replica count — a fixed fleet of identical processes, each polling
for work — so changing it meant editing a host's environment and redeploying,
and the fleet existed whether or not there was anything to run.

Here the number is a *limit*, not a population. This host runs one service that
enrolls, heartbeats and samples capacity, and it starts a child process only
when it has actually claimed a turn. An operator's number on the admin Servers
page arrives on the heartbeat answer and becomes the limit; nothing on this
machine has to be resized to honour it. Raising it lets the next poll claim
more; lowering it stops claiming and the turns already running finish. There is
no idle process to reap, no population to reconcile, and no "which one do I
stop" decision to get wrong.

**The handover is the whole safety argument.** A claimed turn holds a database
slot and a lease; handing it to a process that never ran it would strand the
user's message until the lease expired. So the child says, once, on a pipe of
its own, whether it accepted the turn — before it does anything else at all:

* no process was created, or the child explicitly answered ``declined`` — it
  provably never began, so the claim is handed back to the queue and no
  attempt is spent;
* the child answered ``accepted`` — it owns the turn from that moment. Whatever
  happens next is the lease's business and the retained-session recovery rules'
  business, never this module's. It does not get requeued from here;
* the descriptor reached EOF, stayed silent through the deadline, returned an
  unexpected answer, or failed — all are ambiguous, and ambiguity is not
  proof. The turn stays claimed, the child stays counted, and the lease
  remains the backstop.

The pipe rather than stdout because the child's stdout is the container log and
a reader that stops draining it would block the child; a dedicated descriptor
carries exactly one line and is closed.

**A host that cannot start turns stops claiming them.** Consecutive launch
failures back off and are reported on the heartbeat, so the admin page can say
why a machine with capacity is taking no work. That is the one piece of
"pool health" this design keeps, and it is about this host's ability to start a
process, not about maintaining a population.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import select
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent_host.logging_utils import get_logger
from cloud_agent_shared.shared_protocol import (
    HOST_CONCURRENCY_CAPABILITY,
    HOST_CONCURRENCY_MAX_SLOTS,
    HOST_CONCURRENCY_MIN_SLOTS,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = get_logger(__name__)

ENV_DEFAULT_SLOTS = "AGENT_HOST_SLOTS"
DEFAULT_SLOTS = 3
MIN_SLOTS = HOST_CONCURRENCY_MIN_SLOTS
MAX_SLOTS = HOST_CONCURRENCY_MAX_SLOTS

#: The child's one-line answer, on its handover descriptor, before it does
#: anything else. ``accepted`` is the point of no return: after it, this host
#: never hands the turn back.
HANDOVER_ACCEPTED = "accepted"
#: The child received no turn it could run — an empty or unparsable payload.
#: Provably nothing happened, so the claim goes back to the queue.
HANDOVER_DECLINED = "declined"

#: How long a child gets to answer. Generous next to a Python start-up and
#: irrelevant next to a turn, and a deadline only a wedged child reaches.
HANDOVER_TIMEOUT_S = 60.0

#: What the launch produced, and therefore what may be done about it.
LAUNCH_STARTED = "started"
#: The child provably never began: the claim may be handed back.
LAUNCH_NOT_STARTED = "not_started"
#: The child may or may not have begun. Never handed back; the lease decides.
LAUNCH_UNCERTAIN = "uncertain"

#: Consecutive launch failures before this host stops claiming, and the delays
#: it waits. A single failure is a blip and costs one requeued claim; a broken
#: image would otherwise take the whole queue apart one claim at a time.
LAUNCH_FAILURES_BEFORE_PAUSE = 3
LAUNCH_BACKOFF_S = 5.0
LAUNCH_BACKOFF_CAP_S = 300.0

__all__ = [
    "DEFAULT_SLOTS",
    "ENV_DEFAULT_SLOTS",
    "HANDOVER_ACCEPTED",
    "HANDOVER_DECLINED",
    "HANDOVER_TIMEOUT_S",
    "HOST_CONCURRENCY_CAPABILITY",
    "LAUNCH_NOT_STARTED",
    "LAUNCH_STARTED",
    "LAUNCH_UNCERTAIN",
    "MAX_SLOTS",
    "MIN_SLOTS",
    "LaunchResult",
    "RunningTurn",
    "TurnHandle",
    "TurnPool",
    "default_slots",
    "read_handover",
    "requested_slots",
    "write_handover",
]


def default_slots(environ: Mapping[str, str] | None = None) -> int:
    """How many turns this host runs at once until an operator says otherwise.

    Raises ``ValueError`` outside the protocol's bounds, so a misconfigured
    host fails at startup with the variable named rather than running on a
    number that was silently corrected.
    """
    raw = ((environ if environ is not None else os.environ).get(ENV_DEFAULT_SLOTS) or "").strip()
    if not raw:
        return DEFAULT_SLOTS
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if not MIN_SLOTS <= value <= MAX_SLOTS:
        raise ValueError(
            f"{ENV_DEFAULT_SLOTS} must be an integer from {MIN_SLOTS} through {MAX_SLOTS} "
            f"(got {raw!r})"
        )
    return value


def requested_slots(value: object) -> int | None:
    """The operator's number, if it is one this host may honour.

    Anything else — absent, null, a bool, out of bounds — reads as "no
    request", and the host's own default applies. The admin request schema
    enforces the same bounds; they are checked again here because this is the
    side that would act on the number.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if MIN_SLOTS <= value <= MAX_SLOTS else None


# ── The handover ───────────────────────────────────────────────────────


def write_handover(fd: int, line: str) -> None:
    """Send the child's one-line answer and close the descriptor.

    Best effort in one direction only: a host that has already given up on the
    handover has closed its end, and a child that cannot report is not a reason
    to abandon a turn it is about to run. It never raises.

    Standard input, output and error are written but never closed. The
    deployment check points the handover at standard output to read the answer
    from ``docker exec``, and closing it there would take the process's own
    output away from everything after.
    """
    with contextlib.suppress(OSError):
        os.write(fd, f"{line}\n".encode())
    if fd > 2:
        with contextlib.suppress(OSError):
            os.close(fd)


def read_handover(
    fd: int,
    *,
    timeout_s: float = HANDOVER_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """Read the child's answer, or ``None`` when the deadline passes first.

    An empty string means the child closed the descriptor without answering;
    it is only a transport observation, not proof that the child never accepted
    the turn. ``None`` is silence through the deadline. A transport error is
    allowed to propagate because it cannot prove whether the child accepted.

    The descriptor is always closed. Leaking it would keep this host's own
    write end open and make the next read wait for a deadline instead of an
    end-of-file.
    """
    deadline = clock() + timeout_s
    chunks: list[bytes] = []
    try:
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                return None
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise
            if not ready:
                return None
            try:
                chunk = os.read(fd, 256)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise
            if not chunk:
                # End of file: the child closed this descriptor, which exiting
                # does for it. Whatever arrived before is the whole answer.
                return b"".join(chunks).decode(errors="replace").strip()
            chunks.append(chunk)
            if b"\n" in chunk:
                return b"".join(chunks).decode(errors="replace").split("\n", 1)[0].strip()
            if sum(len(part) for part in chunks) > 4096:
                # Not an answer. Bounded because this descriptor is written by
                # a process that has not proved anything about itself yet.
                return ""
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


# ── Running turns ──────────────────────────────────────────────────────


class TurnHandle(Protocol):
    """What this module needs of a child process: ``subprocess.Popen``'s shape."""

    def poll(self) -> int | None:
        """Return the exit status, or ``None`` while the child runs."""
        ...

    def terminate(self) -> None:
        """Ask the child to finish its turn and exit (SIGTERM)."""
        ...

    def kill(self) -> None:
        """Stop the child now (SIGKILL)."""
        ...


@dataclass(frozen=True)
class LaunchResult:
    """A launch's outcome, and the handle to track when there is one."""

    outcome: str
    handle: TurnHandle | None = None
    detail: str = ""


@dataclass
class RunningTurn:
    """One turn this host handed to a child, and what is known about it."""

    job_id: str
    attempt_id: int
    handle: TurnHandle
    started_at: float
    #: False for a turn whose child never answered the handover. It is counted
    #: and waited for like any other; the flag exists so an operator reading a
    #: log can tell the ambiguous ones apart.
    accepted: bool = True


@dataclass
class FinishedTurn:
    """A child that has exited, and the status it exited with."""

    job_id: str
    attempt_id: int
    exit_code: int
    accepted: bool


class TurnPool:
    """The turns this host is running, and the rule for taking another.

    Accounting only. Who is claimed and what a child does with a turn belong to
    the runner; what belongs here is how many turns are in flight, whether
    another may be started, and the handover that decides whether a launch that
    went wrong may be handed back to the queue.
    """

    def __init__(
        self,
        *,
        launch: Callable[[dict[str, Any]], LaunchResult],
        clock: Callable[[], float] = time.monotonic,
        failures_before_pause: int = LAUNCH_FAILURES_BEFORE_PAUSE,
        backoff_s: float = LAUNCH_BACKOFF_S,
        backoff_cap_s: float = LAUNCH_BACKOFF_CAP_S,
    ) -> None:
        self._launch = launch
        self._clock = clock
        self._failures_before_pause = failures_before_pause
        self._backoff_s = backoff_s
        self._backoff_cap_s = backoff_cap_s
        self._turns: list[RunningTurn] = []
        self._launch_failures = 0
        self._claim_not_before = 0.0
        self._launch_error = ""
        self._stopping = False

    @property
    def launch_error(self) -> str:
        """Why this host is not claiming, or ``""`` when it is healthy.

        Reported on the heartbeat, which is what lets the admin page explain a
        machine with free capacity that is taking no work. Empty is a positive
        statement, not an absent one.
        """
        return self._launch_error

    def running(self) -> int:
        """Turns in flight, counting one whose child is still starting up.

        A claimed turn occupies its slot from the moment it is claimed. Waiting
        for the handover before counting it would let a second claim land while
        the first was still being handed over, and the host would run one more
        turn than it was allowed to.
        """
        return len(self._turns)

    def turns(self) -> list[RunningTurn]:
        """A snapshot of what is running."""
        return list(self._turns)

    def may_claim(self, limit: int) -> bool:
        """Whether this host may take one more turn right now."""
        if self._stopping:
            return False
        if len(self._turns) >= limit:
            return False
        return self._clock() >= self._claim_not_before

    def reap(self) -> list[FinishedTurn]:
        """Forget children that have exited, and say which they were."""
        finished: list[FinishedTurn] = []
        for turn in list(self._turns):
            code = turn.handle.poll()
            if code is None:
                continue
            self._turns.remove(turn)
            finished.append(
                FinishedTurn(
                    job_id=turn.job_id,
                    attempt_id=turn.attempt_id,
                    exit_code=code,
                    accepted=turn.accepted,
                )
            )
        return finished

    def launch(self, job: dict[str, Any]) -> LaunchResult:
        """Hand one claimed turn to a child, and say whether it began.

        ``LAUNCH_NOT_STARTED`` is the caller's licence to hand the claim back
        to the queue: the child provably never had the turn. Every other
        outcome leaves the turn where it is.
        """
        job_id = str(job.get("job_id") or "")
        attempt_id = int(job.get("attempt_id") or 0)
        result = self._launch(job)
        if result.handle is not None:
            self._turns.append(
                RunningTurn(
                    job_id=job_id,
                    attempt_id=attempt_id,
                    handle=result.handle,
                    started_at=self._clock(),
                    accepted=result.outcome == LAUNCH_STARTED,
                )
            )
        if result.outcome == LAUNCH_STARTED:
            self._launch_succeeded()
        else:
            self._launch_failed(job_id=job_id, result=result)
        return result

    def shutdown(
        self,
        *,
        immediate: bool = False,
        wait: Callable[[float], None] = time.sleep,
    ) -> list[FinishedTurn]:
        """Stop claiming and wait for every child, or stop them now.

        Graceful by default: a child is one turn and already exits when that
        turn finishes, so do not signal it at all. This protects an ambiguous
        child even during interpreter startup, before its signal handler can be
        installed. Supported deployment asks Docker Engine to wait
        indefinitely. Once installed, the early child handler is defense
        against later direct or process-group signals; ``immediate`` is Ctrl-C
        and still kills at once.
        """
        self._stopping = True
        finished: list[FinishedTurn] = []
        if immediate:
            for turn in self._turns:
                turn.handle.kill()
        while True:
            finished.extend(self.reap())
            if not self._turns:
                return finished
            wait(0.5)

    def _launch_succeeded(self) -> None:
        self._launch_failures = 0
        self._claim_not_before = 0.0
        self._launch_error = ""

    def _launch_failed(self, *, job_id: str, result: LaunchResult) -> None:
        self._launch_failures += 1
        logger.warning(
            "agent_turn_launch_failed",
            extra={
                "event": "agent_turn_launch_failed",
                "job_id": job_id,
                "outcome": result.outcome,
                "detail": result.detail,
                "consecutive": self._launch_failures,
            },
        )
        if self._launch_failures < self._failures_before_pause:
            return
        delay = min(
            self._backoff_s * 2 ** (self._launch_failures - self._failures_before_pause),
            self._backoff_cap_s,
        )
        self._claim_not_before = self._clock() + delay
        self._launch_error = (
            f"{self._launch_failures} turns in a row could not be started "
            f"({result.detail or result.outcome}); this host is not claiming for {delay:.0f}s"
        )


# ── Payload ────────────────────────────────────────────────────────────


def encode_turn(job: Mapping[str, Any]) -> bytes:
    """Serialize a claimed turn for delivery on a child's standard input.

    Standard input rather than arguments or the environment because this
    payload carries the turn's credentials, and ``/proc/<pid>/cmdline`` and
    ``/proc/<pid>/environ`` are readable by anyone who can see the process.
    """
    return (json.dumps(dict(job), separators=(",", ":")) + "\n").encode()


def decode_turn(raw: str) -> dict[str, Any] | None:
    """Read a delivered turn, or ``None`` when nothing usable arrived.

    ``None`` is what a child declines on, and the host reads that as "this turn
    was never delivered". It must therefore stay strictly a parse: a payload
    that decodes to anything but an object with a job id is not a turn, and
    guessing at one would requeue a turn that had in fact been handed over.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict) or not payload.get("job_id"):
        return None
    return payload
