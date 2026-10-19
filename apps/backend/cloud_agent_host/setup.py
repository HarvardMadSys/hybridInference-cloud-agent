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


@dataclass(frozen=True)
class SetupResult:
    """What the retained-container setup phase did."""

    ran: bool
    exit_code: int = 0
    detail: str = ""


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
    """
    if not script.strip():
        return SetupResult(ran=False)

    if phase and hasattr(backend, "spawn_environment_script"):
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

    def drain_service() -> None:
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
            if (
                phase
                and hasattr(backend, "spawn_environment_script")
                and process.poll() is not None
            ):
                code = process.wait()
                if code == 0 and phase == "start":
                    retained_output = True
                    threading.Thread(
                        target=drain_service, daemon=True, name="environment-service-output"
                    ).start()
                else:
                    process.kill()
                    while True:
                        try:
                            chunk = output.get(timeout=0.25)
                        except queue.Empty:
                            break
                        if chunk is None:
                            break
                        consume(chunk)
                break
            try:
                chunk = output.get(timeout=min(0.25, remaining))
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
    if (
        phase
        and hasattr(backend, "spawn_environment_script")
        and (phase != "start" or exit_code != 0)
    ):
        process.kill()
    if cleanup_background and not (phase and hasattr(backend, "spawn_environment_script")):
        process.kill()
    stderr = redactor.text(process.stderr_text())
    tail.append(stderr)
    if on_output and stderr:
        on_output(stderr)
    if exit_code != 0:
        return SetupResult(ran=True, exit_code=exit_code, detail=tail.text()[-500:])
    return SetupResult(ran=True, detail="setup completed")


__all__ = ["SetupResult", "run_setup"]
