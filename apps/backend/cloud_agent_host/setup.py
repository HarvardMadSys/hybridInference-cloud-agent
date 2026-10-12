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

from cloud_agent_host.sandbox import SandboxSpec

if TYPE_CHECKING:
    from collections.abc import Callable

#: How much of the setup output's end a failure reports. Output is read in
#: bounded pieces and only this much of it is held, however much the script
#: writes and whether or not it ever ends a line.
_TAIL_CHARS = 500


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
) -> SetupResult:
    """Run one bounded setup command in the retained container.

    Output is retained only as a bounded diagnostic tail; the durable result is
    already in the container layer.

    ``stop`` is polled while the script runs. Once it answers true -- the
    owner pressed Stop, or the attempt's lease is gone -- the script is killed
    rather than left to run out its timeout for a turn that will not use it.
    """
    if not script.strip():
        return SetupResult(ran=False)

    process = backend.spawn(
        SandboxSpec(
            argv=["/bin/sh", "-c", script],
            workdir=workdir,
            env={},
        )
    )
    output: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        try:
            for chunk in process.chunks():
                output.put(chunk)
        finally:
            output.put(None)

    threading.Thread(target=pump, daemon=True, name="retained-setup-output").start()
    deadline = time.monotonic() + timeout_s
    tail = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            process.wait()
            return SetupResult(
                ran=True,
                exit_code=124,
                detail=f"setup exceeded {timeout_s:.0f}s",
            )
        if stop is not None and stop():
            process.kill()
            process.wait()
            return SetupResult(ran=True, exit_code=130, detail="setup was stopped")
        try:
            chunk = output.get(timeout=min(0.25, remaining))
        except queue.Empty:
            continue
        if chunk is None:
            break
        tail = (tail + chunk)[-_TAIL_CHARS:]

    exit_code = process.wait()
    if exit_code != 0:
        return SetupResult(ran=True, exit_code=exit_code, detail=tail)
    return SetupResult(ran=True, detail="setup completed")


__all__ = ["SetupResult", "run_setup"]
