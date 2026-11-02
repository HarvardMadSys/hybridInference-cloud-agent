"""Run a repository setup script inside its retained chat container.

Setup runs on the group's own network, the same guarded bridge the Agent CLI
uses afterwards, so installs reach public registries exactly as the agent
would. Dependencies and other filesystem changes land directly in the same
Docker writable layer that the live Agent CLI will use. There is no host
worktree, portable setup archive, or second throwaway container to restore
into.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cloud_agent_host.bounded_output import OutputPump, TextTail
from cloud_agent_host.sandbox import SandboxSpec
from cloud_agent_shared.secret_redaction import SecretRedactor

if TYPE_CHECKING:
    from collections.abc import Callable

#: How often the loop looks for a stop, its deadline and an environment
#: script's receipt while no output arrives. A guest wait reports a receipt
#: within 50 ms, and looking as often keeps the host's share of the delay as
#: short.
CHECK_INTERVAL_S = 0.05
#: An environment script's receipt is read through an exec of its own, so it
#: can arrive before the last output the script wrote before it ended. That
#: output crosses docker or kata exec in tens of milliseconds, so once the
#: receipt is in, the stream is read until it ends or stays quiet this long,
#: several times that, before a start's service gets it or the phase is killed.
FINAL_OUTPUT_QUIET_S = 0.2
#: A service that never goes quiet still gets its stream once this long has
#: been spent waiting for output: the script's last bytes precede whatever the
#: service writes after it in the same pipe, so they arrive well within it.
#: Time spent consuming does not count, so a slow consumer, such as a progress
#: report, cannot cut the reading short.
FINAL_OUTPUT_WAIT_S = 1.0
#: A service that floods its stream, so that output never has to be waited
#: for, gets it once this much has been read. When the receipt was read, the
#: script's last bytes were already in the pipes and buffers between it and
#: this process, and those hold well under this.
FINAL_OUTPUT_CHARS = 4 * 1024 * 1024


@dataclass(frozen=True)
class SetupResult:
    """What the retained-container setup phase did."""

    ran: bool
    exit_code: int = 0
    detail: str = ""


def _read_final_output(output: OutputPump[str], consume: Callable[[str], None]) -> bool:
    """Consume what a finished script wrote last; return whether the stream ended."""
    waited = 0.0
    read = 0
    while waited < FINAL_OUTPUT_WAIT_S and read < FINAL_OUTPUT_CHARS:
        asked = time.monotonic()
        try:
            chunk = output.get(timeout=min(FINAL_OUTPUT_QUIET_S, FINAL_OUTPUT_WAIT_S - waited))
        except queue.Empty:
            return False
        waited += time.monotonic() - asked
        if chunk is None:
            return True
        read += len(chunk)
        consume(chunk)
    return False


def _read_rest(output: OutputPump[str], consume: Callable[[str], None]) -> None:
    """Consume what a killed phase's stream still holds, until it ends or goes quiet."""
    while True:
        try:
            chunk = output.get(timeout=0.25)
        except queue.Empty:
            return
        if chunk is None:
            return
        consume(chunk)


def _drain_service(output: OutputPump[str]) -> None:
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


def _hand_over(
    process: Any,
    output: OutputPump[str],
    consume: Callable[[str], None],
    *,
    code: int,
    start: bool,
) -> bool:
    """Finish the output of a script whose receipt came first; True if its service keeps it.

    What the script wrote last is read first, as its own, until the stream
    ends or stays quiet. Then a started service gets the stream, which it may
    keep writing to, or any other phase is killed and what is left is read.
    """
    if _read_final_output(output, consume):
        return False
    if code == 0 and start:
        threading.Thread(
            target=_drain_service, args=(output,), daemon=True, name="environment-service-output"
        ).start()
        return True
    process.kill()
    _read_rest(output, consume)
    return False


def run_setup(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
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

    An environment script's end is read from its receipt, which can come
    before the last of its output. What is still arriving then is read as the
    script's before a start's service is given the stream or any other phase
    is killed.
    """
    if not script.strip():
        return SetupResult(ran=False)

    environment = bool(phase) and hasattr(backend, "spawn_environment_script")
    if environment:
        process = backend.spawn_environment_script(phase=phase, script=script, env=env or {})
    else:
        process = backend.spawn(
            SandboxSpec(
                argv=["/bin/sh", "-c", script],
                workdir=workdir,
                env=env or {},
            )
        )
    output = OutputPump(getattr(process, "chunks", process.lines)(), name="retained-setup-output")
    deadline = time.monotonic() + timeout_s
    tail = TextTail()
    redactor = SecretRedactor(secrets or {})
    retained_output = False

    def consume(chunk: str) -> None:
        safe = redactor.feed(chunk)
        tail.append(safe)
        if on_output and safe:
            on_output(safe)

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                output.close(drain=True)
                process.kill()
                process.wait()
                return SetupResult(
                    ran=True,
                    exit_code=124,
                    detail=f"setup exceeded {timeout_s:.0f}s",
                )
            if (cancel is not None and cancel.is_set()) or (stop is not None and stop()):
                output.close(drain=True)
                process.kill()
                process.wait()
                return SetupResult(ran=True, exit_code=130, detail="setup was stopped")
            if environment and (code := process.poll()) is not None:
                retained_output = _hand_over(
                    process, output, consume, code=code, start=phase == "start"
                )
                break
            try:
                chunk = output.get(timeout=min(CHECK_INTERVAL_S, remaining))
            except queue.Empty:
                continue
            if chunk is None:
                break
            consume(chunk)
    finally:
        if not retained_output:
            output.close()

    final = "" if retained_output else redactor.flush()
    tail.append(final)
    if on_output and final:
        on_output(final)
    exit_code = process.wait()
    if environment and (phase != "start" or exit_code != 0):
        process.kill()
    if cleanup_background and not environment:
        process.kill()
    stderr = redactor.text(process.stderr_text())
    tail.append(stderr)
    if on_output and stderr:
        on_output(stderr)
    if exit_code != 0:
        return SetupResult(ran=True, exit_code=exit_code, detail=tail.text()[-500:])
    return SetupResult(ran=True, detail="setup completed")


__all__ = ["SetupResult", "run_setup"]
