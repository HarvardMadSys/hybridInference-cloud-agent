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
import time
from dataclasses import dataclass
from typing import Any

from cloud_agent_host.bounded_output import OutputPump, TextTail
from cloud_agent_host.sandbox import SandboxSpec


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
) -> SetupResult:
    """Run one bounded setup command in the retained container.

    Output is retained only as a bounded diagnostic tail; the durable result is
    already in the container layer.
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
    output = OutputPump(getattr(process, "chunks", process.lines)(), name="retained-setup-output")
    deadline = time.monotonic() + timeout_s
    tail = TextTail()
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
            try:
                chunk = output.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if chunk is None:
                break
            tail.append(chunk)
    finally:
        output.close()

    exit_code = process.wait()
    if exit_code != 0:
        return SetupResult(ran=True, exit_code=exit_code, detail=tail.text()[-500:])
    return SetupResult(ran=True, detail="setup completed")


__all__ = ["SetupResult", "run_setup"]
