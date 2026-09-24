"""Retained-container setup execution."""

from __future__ import annotations

import threading

import pytest

from cloud_agent_host.setup import run_setup


class _Process:
    def __init__(self, *, lines: list[str], exit_code: int = 0, silent: bool = False) -> None:
        self._lines = lines
        self._exit_code = exit_code
        self._silent = silent
        self._killed = threading.Event()
        self.killed = False
        self.waited = False

    def lines(self):
        if self._silent:
            self._killed.wait()
            return
        yield from self._lines

    def kill(self) -> None:
        self.killed = True
        self._killed.set()

    def wait(self) -> int:
        self.waited = True
        return self._exit_code


class _Backend:
    def __init__(self, process: _Process) -> None:
        self.process = process
        self.specs = []

    def spawn(self, spec):
        self.specs.append(spec)
        return self.process


def test_setup_runs_in_the_retained_workspace():
    backend = _Backend(_Process(lines=["installing\n"]))

    result = run_setup(script="pip install -e .", workdir="/workspace", backend=backend)

    assert result.ran is True
    assert result.exit_code == 0
    assert backend.specs[0].workdir == "/workspace"
    assert backend.specs[0].argv == ["/bin/sh", "-c", "pip install -e ."]


def test_failed_setup_returns_a_bounded_diagnostic_tail():
    backend = _Backend(_Process(lines=["x" * 600], exit_code=7))

    result = run_setup(script="exit 7", workdir="/workspace", backend=backend)

    assert result.exit_code == 7
    assert len(result.detail) == 500


def test_silent_setup_is_killed_and_reaped_at_its_resource_deadline():
    process = _Process(lines=[], silent=True)
    backend = _Backend(process)

    result = run_setup(
        script="sleep forever",
        workdir="/workspace",
        backend=backend,
        timeout_s=0.01,
    )

    assert result.exit_code == 124
    assert process.waited is True
    assert process.killed is True


@pytest.mark.parametrize("script", ["", "   ", "\n"])
def test_blank_script_does_not_spawn_a_process(script: str):
    backend = _Backend(_Process(lines=[]))

    result = run_setup(script=script, workdir="/workspace", backend=backend)

    assert result.ran is False
    assert backend.specs == []
