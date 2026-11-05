"""Bounded commands an owner's request runs inside its workspace: Git inspection."""

from __future__ import annotations

import os
import queue
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from cloud_agent_host.bounded_output import OutputPump
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.sandbox import SandboxError, SandboxSpec
from cloud_agent_host.workspace_git import (
    run_git as _workspace_git,
    run_git_metadata as _workspace_git_metadata,
)
from cloud_agent_shared.host_settings import SANDBOX_MANAGER_SETTING_KEYS, default_values

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloud_agent_host.sandbox import SandboxBackend, SandboxProcess

# The sandbox manager's own logger: its command events keep their name.
logger = get_logger("cloud_agent_host.sandbox_manager")

_DEFAULT_SETTINGS = default_values(SANDBOX_MANAGER_SETTING_KEYS)
MAX_COMMAND_OUTPUT_BYTES = _DEFAULT_SETTINGS["workspace_command_output_max_mib"] * 1024 * 1024
DEFAULT_COMMAND_TIMEOUT_S = float(_DEFAULT_SETTINGS["workspace_command_timeout_s"])


def _stop_overrun_command(process: SandboxProcess) -> None:
    """Stop a bounded command that overran, keeping why it was stopped.

    The kill signals only the command's own process group, and the Docker
    client goes with it either way. A command can finish just before its
    kill, leaving no receipt to honour: the caller's answer (timed out, too
    large, unreadable) is still what happened, so a failed kill is logged
    rather than raised in its place.
    """
    try:
        process.kill()
    except (SandboxError, OSError):
        logger.warning("workspace_command_kill_failed", exc_info=True)


def _run_sandbox(
    backend: SandboxBackend,
    *,
    workdir: Path | str,
    script: str,
    env: dict[str, str],
    timeout_s: float,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> tuple[int, str, str]:
    """Run a bounded command and return status, stdout and stderr.

    The deadline covers the command's exit as well as its output, so one that
    closes stdout and keeps running is stopped like one that never closes it.
    """
    process = backend.spawn(
        SandboxSpec(
            argv=["/bin/sh", "-lc", script],
            workdir=str(workdir),
            env={**backend.base_env(), "GIT_CONFIG_NOSYSTEM": "1", **env},
        )
    )
    # Real sandbox adapters read fixed-size chunks; injected line-only adapters
    # remain supported without turning protocol frames into command records.
    chunks_source = getattr(process, "chunks", process.lines)

    def output_then_status() -> Iterator[str | int]:
        yield from chunks_source()
        # Queued behind the output, so the deadline also covers a command
        # that closed its stdout and kept running.
        yield process.wait()

    output = OutputPump(output_then_status(), name="workspace-command")
    deadline = time.monotonic() + timeout_s
    chunks: list[str] = []
    size = 0
    exit_code: int | None = None
    try:
        while exit_code is None:
            if time.monotonic() > deadline:
                output.close(drain=True)
                _stop_overrun_command(process)
                process.wait()
                raise HTTPException(status_code=408, detail="workspace command timed out")
            try:
                chunk = output.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(chunk, int):
                exit_code = chunk
            elif chunk is not None:
                size += len(chunk.encode("utf-8", errors="replace"))
                if size > max_output_bytes:
                    output.close(drain=True)
                    _stop_overrun_command(process)
                    process.wait()
                    raise HTTPException(
                        status_code=413, detail="workspace command output is too large"
                    )
                chunks.append(chunk)
        stderr = process.stderr_text()
        if len(stderr.encode("utf-8", errors="replace")) > max_output_bytes:
            stderr = stderr.encode("utf-8", errors="replace")[-max_output_bytes:].decode(
                "utf-8", errors="replace"
            )
    finally:
        output.close()
    if isinstance(workdir, Path):
        os.utime(workdir, None)
    return exit_code, "".join(chunks), stderr


def _run_git(
    backend: SandboxBackend,
    workdir: Path | str,
    base_sha: str | None = None,
    *,
    timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
    exact_patch: bool = False,
) -> dict[str, Any]:
    return _workspace_git(
        _run_sandbox,
        backend,
        workdir,
        base_sha,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
        exact_patch=exact_patch,
    )


def _run_git_metadata(
    backend: SandboxBackend,
    workdir: Path | str,
    base_sha: str | None = None,
    *,
    timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> dict[str, Any]:
    return _workspace_git_metadata(
        _run_sandbox,
        backend,
        workdir,
        base_sha,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
