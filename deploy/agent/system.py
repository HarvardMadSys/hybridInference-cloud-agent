"""The only ways the deployment agent touches the machine: fixed argv, never a shell.

Every command the agent runs is built here from the operation's validated
parameters and the machine's local policy: Docker Compose for one named
service, a release recipe's own ``deploy.sh --phase``, the Kata helper. The
:class:`CommandRunner` is the seam tests replace; production uses
:class:`SubprocessRunner`.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pathlib import Path

#: The last part of a command's output kept in the journal; never the environment.
OUTPUT_TAIL = 4000


@dataclass(frozen=True)
class CommandResult:
    """What a command did: its exit status and the end of its output."""

    code: int
    output: str = ""

    @property
    def ok(self) -> bool:
        """Exit status zero."""
        return self.code == 0


class CommandRunner(Protocol):
    """Runs one argv with an explicit environment."""

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float = 600.0,
    ) -> CommandResult:
        """Run ``argv`` and return its result; never raises for a non-zero exit."""
        ...


class SubprocessRunner:
    """The production runner: ``subprocess.run`` with ``shell=False``."""

    def stream(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        stdin_path: Path | None = None,
        stdin_data: bytes | None = None,
        stdout_path: Path | None = None,
        timeout: float = 3600.0,
    ) -> CommandResult:
        """Run ``argv`` with binary stdin/stdout from files or memory; stderr is kept."""
        merged = {**os.environ, **(env or {})}
        stdin = (
            stdin_path.open("rb")
            if stdin_path
            else (subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL)
        )
        stdout = stdout_path.open("wb") if stdout_path else subprocess.PIPE
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=merged,
                input=stdin_data,
                stdin=None if stdin_data is not None else stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(127, type(exc).__name__)
        finally:
            for handle in (stdin, stdout):
                if hasattr(handle, "close"):
                    handle.close()
        out = (
            completed.stdout.decode(errors="replace") if isinstance(completed.stdout, bytes) else ""
        )
        return CommandResult(
            completed.returncode, (out + completed.stderr.decode(errors="replace"))[-OUTPUT_TAIL:]
        )

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float = 600.0,
    ) -> CommandResult:
        """Run ``argv``; a timeout or a missing program is a failed result."""
        merged = {**os.environ, **(env or {})}
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=merged,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(127, f"{type(exc).__name__}")
        output = (completed.stdout or "") + (completed.stderr or "")
        return CommandResult(completed.returncode, output[-OUTPUT_TAIL:])


#: A turn may run for hours; the deadline only frees the agent from a stuck daemon.
DRAIN_TIMEOUT_S = 7 * 24 * 3600.0


@dataclass
class Compose:
    """``docker compose`` for this machine's project, as its local policy names it."""

    runner: CommandRunner
    deploy_root: Path
    policy: dict[str, Any] = field(default_factory=dict)

    def _base(self) -> list[str]:
        compose = self.policy.get("compose") or {}
        argv = [str(compose.get("docker") or "docker"), "compose"]
        project = compose.get("project")
        if project:
            argv += ["-p", str(project)]
        directory = compose.get("project_directory")
        if directory:
            argv += ["--project-directory", str(directory)]
        for path in compose.get("files") or []:
            argv += ["-f", str(path)]
        argv += ["--env-file", str(self.deploy_root / ".env")]
        return argv

    def recreate(self, services: list[str]) -> CommandResult:
        """Recreate named services with the current ``.env``, nothing else."""
        return self.runner.run(
            [*self._base(), "up", "-d", "--no-deps", "--force-recreate", *services],
            cwd=self.deploy_root,
        )

    def stop(self, services: list[str]) -> CommandResult:
        """Stop named services, honouring their configured stop grace."""
        return self.runner.run([*self._base(), "stop", *services], cwd=self.deploy_root)

    def drain(self, services: list[str]) -> CommandResult:
        """Stop the runner, then the sandbox manager, with no forced-kill deadline.

        The same order and the same unbounded stop as ``sandbox_host.sh``: the
        runner treats SIGTERM as a drain and waits for its turns, which still
        use the sandbox manager. A Compose stop would turn a long turn into a
        SIGKILL at its stop grace.
        """
        docker = str((self.policy.get("compose") or {}).get("docker") or "docker")
        result = CommandResult(0, "")
        for service in ("runner", "sandbox-manager"):
            if service not in services:
                continue
            found = self.runner.run([*self._base(), "ps", "-q", service], cwd=self.deploy_root)
            if not found.ok:
                return found
            containers = found.output.split()
            if containers:
                result = self.runner.run(
                    [docker, "stop", "--time=-1", *containers],
                    cwd=self.deploy_root,
                    timeout=DRAIN_TIMEOUT_S,
                )
                if not result.ok:
                    return result
        return result

    def running(self, services: list[str]) -> dict[str, bool]:
        """Whether each named service has a running (and, if checked, healthy) container."""
        result = self.runner.run(
            [*self._base(), "ps", "--format", "json", *services], cwd=self.deploy_root
        )
        states: dict[str, bool] = dict.fromkeys(services, False)
        if not result.ok:
            return states
        for line in result.output.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            service = row.get("Service")
            if service in states:
                health = (row.get("Health") or "").lower()
                states[service] = row.get("State") == "running" and health in {"", "healthy"}
        return states


def http_ok(url: str, timeout: float = 3.0) -> bool:
    """Whether ``url`` answers 2xx; only ever a loopback health endpoint."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_until(check: Any, *, timeout: float, interval: float = 1.0) -> bool:
    """Poll ``check`` until it is true or ``timeout`` passes."""
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


__all__ = [
    "CommandResult",
    "CommandRunner",
    "Compose",
    "SubprocessRunner",
    "http_ok",
    "wait_until",
]
