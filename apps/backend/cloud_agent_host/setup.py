"""Run a repository setup script inside its retained chat container.

Setup runs on the group's own network, the same guarded bridge the Agent CLI
uses afterwards, so installs reach public registries exactly as the agent
would. Dependencies and other filesystem changes land directly in the same
Docker writable layer that the live Agent CLI will use. There is no host
worktree, portable setup archive, or second throwaway container to restore
into.
"""

from __future__ import annotations

import codecs
import queue
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cloud_agent_host.bounded_output import OutputPump, TextTail
from cloud_agent_host.sandbox import SandboxSpec
from cloud_agent_shared.secret_redaction import SecretRedactor

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: How often the loop looks for a stop, its deadline and an environment
#: script's receipt while no output arrives. A guest wait reports a receipt
#: within 50 ms, and looking as often keeps the host's share of the delay as
#: short.
CHECK_INTERVAL_S = 0.05
#: An environment script's receipt counts the bytes it wrote, and its helper
#: forwards all of them before writing it, so when a start succeeds they are
#: at most still crossing the exec, which takes tens of milliseconds. Their
#: arrival is waited for this long, time spent consuming them aside, so a slow
#: progress report cannot cut it short; then the service gets the stream.
SCRIPT_OUTPUT_WAIT_S = 1.0
#: How long the rest of a killed phase's stream may take to reach its end,
#: time spent consuming it aside. All the script wrote was already in the
#: stream, and nothing writes once the phase is reaped, so the end comes at
#: once.
REST_OUTPUT_S = 5.0
#: How reading a successful start's output up to its byte count ended.
_ENDED, _COMPLETE, _CUT = "ended", "complete", "cut"


@dataclass(frozen=True)
class SetupResult:
    """What the retained-container setup phase did."""

    ran: bool
    exit_code: int = 0
    detail: str = ""


def _spawn(
    backend: Any, script: str, workdir: str, env: dict[str, str], phase: str | None
) -> tuple[Any, bool]:
    """Start ``script``, an environment phase as its own identity; say which it is."""
    if phase and hasattr(backend, "spawn_environment_script"):
        return backend.spawn_environment_script(phase=phase, script=script, env=env), True
    spec = SandboxSpec(argv=["/bin/sh", "-c", script], workdir=workdir, env=env)
    return backend.spawn(spec), False


def _byte_chunks(process: Any) -> Iterator[bytes]:
    """Yield a process's output as read, before decoding, so its bytes can be counted."""
    reader = getattr(process, "byte_chunks", None)
    if reader is not None:
        yield from reader()
        return
    # A text-only adapter's chunks are encoded again, byte for byte when valid.
    for chunk in getattr(process, "chunks", process.lines)():
        yield chunk.encode()


def _drain_service(output: OutputPump[bytes]) -> None:
    """Keep a started service's pipe flowing, discarding what it writes."""
    try:
        while True:
            try:
                chunk = output.get(timeout=1)
            except queue.Empty:
                continue
            if chunk is None:
                break
            # Service output belongs to the running service, not the
            # completed start step. Keep draining its pipe without
            # mutating or persisting the foreground receipt.
    finally:
        output.close()


class _Output:
    """One script's output: counted as read, then decoded, masked, kept and reported."""

    def __init__(
        self,
        pump: OutputPump[bytes],
        redactor: SecretRedactor,
        tail: TextTail,
        on_output: Callable[[str], None] | None,
    ) -> None:
        self.pump = pump
        self.redactor = redactor
        self.tail = tail
        self.on_output = on_output
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        #: Raw bytes consumed, before decoding and masking change their size.
        self.read = 0
        self.finished = False
        self.handed_over = False
        self.reaped = False

    def consume(self, data: bytes) -> None:
        """Count, decode, mask, keep and report one piece of raw output."""
        self.read += len(data)
        self.show(self.redactor.feed(self.decoder.decode(data)))

    def finish(self) -> None:
        """End the stream: what the decoder and the redactor held back is shown now.

        A stream a service took over goes on, so nothing held back is shown.
        """
        if not (self.finished or self.handed_over):
            self.finished = True
            self.show(self.redactor.feed(self.decoder.decode(b"", final=True), final=True))

    def show(self, safe: str) -> None:
        """Keep and report text that is already masked."""
        self.tail.append(safe)
        if self.on_output and safe:
            self.on_output(safe)

    def read_rest(self) -> None:
        """Consume what a reaped phase's stream still holds, to its end."""
        waited = 0.0
        while waited < REST_OUTPUT_S:
            asked = time.monotonic()
            try:
                data = self.pump.get(timeout=REST_OUTPUT_S - waited)
            except queue.Empty:
                return
            waited += time.monotonic() - asked
            if data is None:
                return
            self.consume(data)

    def read_to(self, count: int, stopped: Callable[[], bool], deadline: float) -> str:
        """Consume the stream up to its ``count``-th byte, the end of the script's output.

        Returns ``_COMPLETE`` once exactly that much was read, ``_ENDED`` if the
        stream ended first, and ``_CUT`` if the wait, the deadline or a stop came
        first, or more had already been read.
        """
        waited = 0.0
        while self.read < count:
            if waited >= SCRIPT_OUTPUT_WAIT_S or stopped() or time.monotonic() >= deadline:
                return _CUT
            asked = time.monotonic()
            try:
                data = self.pump.get(timeout=CHECK_INTERVAL_S)
            except queue.Empty:
                data = b""
            waited += time.monotonic() - asked
            if data is None:
                return _ENDED
            # What follows the count, in this chunk and after it, is the service's.
            self.consume(data[: count - self.read])
        return _COMPLETE if self.read == count else _CUT

    def hand_over(self) -> None:
        """Give the stream to a started service, whose output is drained, not kept."""
        self.handed_over = True
        threading.Thread(
            target=_drain_service, args=(self.pump,), daemon=True, name="environment-service-output"
        ).start()


def _hand_over(
    process: Any,
    output: _Output,
    *,
    code: int,
    start: bool,
    stopped: Callable[[], bool],
    deadline: float,
) -> None:
    """Split a finished environment script's output from what follows it.

    Any phase but a successful start is reaped at once: all the script wrote
    is already in the stream, which the reap only ends. A started service
    keeps the stream once the host has read the bytes the receipt counts, or
    when their wait, the deadline or a stop comes first. A process that
    counts nothing hands its stream over at once.
    """
    if code != 0 or not start:
        process.kill()
        output.reaped = True
        output.read_rest()
        return
    count = getattr(process, "script_bytes", None)
    try:
        ended = _CUT if count is None else output.read_to(count, stopped, deadline)
        if ended == _ENDED:
            return
        if ended == _COMPLETE:
            # The script's output is whole, so nothing it holds back can grow.
            output.finish()
    except BaseException:
        # The service's pipe must keep flowing whatever reporting did.
        output.hand_over()
        raise
    output.hand_over()


def run_setup(
    *,
    script: str,
    workdir: str,
    backend: Any,
    timeout_s: float = 900.0,
    stop: Callable[[], bool] | None = None,
    env: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
    on_output: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    cleanup_background: bool = False,
    phase: str | None = None,
) -> SetupResult:
    """Run one bounded setup command in the retained container.

    Output is retained only as a bounded diagnostic tail; the durable result is
    already in the container layer.

    ``stop`` is polled while the script runs. Once it answers true -- the
    owner pressed Stop, or the attempt's lease is gone -- the script is killed
    rather than left to run out its timeout for a turn that will not use it.
    Setting ``cancel``, which revokes an environment's preparation, does the
    same, and either reads as stopped rather than as out of time.

    An environment script's end is read from its receipt, which counts the
    bytes the script wrote: those are its output, and what follows them is a
    service's.
    """
    if not script.strip():
        return SetupResult(ran=False)

    process, environment = _spawn(backend, script, workdir, env or {}, phase)
    deadline = time.monotonic() + timeout_s
    tail = TextTail()
    redactor = SecretRedactor(secrets or {})
    output = _Output(
        OutputPump(_byte_chunks(process), name="retained-setup-output"), redactor, tail, on_output
    )

    def stopped() -> bool:
        return (cancel is not None and cancel.is_set()) or (stop is not None and stop())

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                output.pump.close(drain=True)
                process.kill()
                process.wait()
                return SetupResult(
                    ran=True,
                    exit_code=124,
                    detail=f"setup exceeded {timeout_s:.0f}s",
                )
            if stopped():
                output.pump.close(drain=True)
                process.kill()
                process.wait()
                return SetupResult(ran=True, exit_code=130, detail="setup was stopped")
            if environment and (code := process.poll()) is not None:
                _hand_over(
                    process,
                    output,
                    code=code,
                    start=phase == "start",
                    stopped=stopped,
                    deadline=deadline,
                )
                break
            try:
                chunk = output.pump.get(timeout=min(CHECK_INTERVAL_S, remaining))
            except queue.Empty:
                continue
            if chunk is None:
                break
            output.consume(chunk)
    finally:
        if not output.handed_over:
            output.pump.close()

    output.finish()
    exit_code = process.wait()
    # A successful start's services keep running; any other environment phase
    # is reaped, unless that was done when its receipt came, and a plain
    # setup's leftovers only when asked.
    reap = (phase != "start" or exit_code != 0) if environment else cleanup_background
    if reap and not output.reaped:
        process.kill()
    output.show(redactor.text(process.stderr_text()))
    if exit_code != 0:
        return SetupResult(ran=True, exit_code=exit_code, detail=tail.text()[-500:])
    return SetupResult(ran=True, detail="setup completed")


__all__ = ["SetupResult", "run_setup"]
