"""A recorded stand-in for Docker, deploy scripts and the Kata helper, for agent tests.

It does what the real commands would leave behind on disk where an agent
checks for evidence (``.deployed-sha`` after an activate), so a test proves the
agent judged the evidence and not merely the exit status. It never runs a
real command.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from deploy.agent.store import parse_env
from deploy.agent.system import CommandResult


def _public_blob(path: Path) -> str:
    """A stable, valid-looking key body for a private key file."""
    return base64.b64encode(hashlib.sha256(path.read_bytes()).digest() * 2).decode()


@dataclass
class FakeMachine:
    """Records every argv; answers like Docker Compose, a recipe and the Kata helper."""

    deploy_root: Path
    calls: list[list[str]] = field(default_factory=list)
    envs: list[dict[str, str]] = field(default_factory=list)
    #: Services that never come up healthy, optionally only for one env value.
    unhealthy: set[str] = field(default_factory=set)
    unhealthy_when: dict[str, str] = field(default_factory=dict)
    #: The commit an ``activate`` phase writes into ``.deployed-sha``; ``None`` writes nothing.
    activate_writes: str | None = None
    phase_exit: int = 0
    missing_images: set[str] = field(default_factory=set)
    kata_install_exit: int = 0
    kata_check_exit: int = 0
    #: What ``ssh`` exits with when a key is tried; 255 is ssh's own failure.
    ssh_exit: int = 0
    #: Programs run for real (the PostgreSQL clients a database test needs).
    passthrough: tuple[str, ...] = ("psql", "pg_dump", "pg_restore")

    def _real(self, argv) -> bool:
        program = argv[argv.index("postgres:15") + 1] if "postgres:15" in argv else argv[0]
        return program in self.passthrough

    def stream(self, argv, *, cwd, env=None, **kwargs) -> CommandResult:
        self.calls.append(list(argv))
        self.envs.append(dict(env or {}))
        from deploy.agent.system import SubprocessRunner

        return SubprocessRunner().stream(argv, cwd=cwd, env=env, **kwargs)

    def _env(self) -> dict[str, str]:
        try:
            return parse_env((self.deploy_root / ".env").read_text())
        except FileNotFoundError:
            return {}

    def run(self, argv, *, cwd, env=None, timeout=600.0) -> CommandResult:
        self.calls.append(list(argv))
        self.envs.append(dict(env or {}))
        if self._real(argv):
            from deploy.agent.system import SubprocessRunner

            return SubprocessRunner().run(argv, cwd=cwd, env=env, timeout=timeout)
        if argv and argv[0] == "ssh-keygen":
            path = Path(argv[argv.index("-f") + 1])
            if "-y" in argv:
                return CommandResult(0, f"ssh-ed25519 {_public_blob(path)} comment\n")
            path.write_text("private key " + path.name)
            path.with_name(path.name + ".pub").write_text("public")
            return CommandResult(0, "")
        if argv and argv[0] == "ssh":
            return CommandResult(self.ssh_exit, "")
        if argv[:2] == ["docker", "compose"] and argv[-3:-1] == ["ps", "-q"]:
            return CommandResult(0, f"{argv[-1]}-container\n")
        if argv[:2] == ["docker", "compose"] and "ps" in argv:
            services = argv[argv.index("json") + 1 :]
            current = self._env()
            bad = set(self.unhealthy)
            for name, value in self.unhealthy_when.items():
                key, _, wanted = value.partition("=")
                if current.get(key) == wanted:
                    bad.add(name)
            lines = [
                json.dumps(
                    {"Service": s, "State": "exited" if s in bad else "running", "Health": ""}
                )
                for s in services
            ]
            return CommandResult(0, "\n".join(lines))
        if argv[:2] == ["docker", "compose"]:
            return CommandResult(0, "")
        if argv[:3] == ["docker", "image", "inspect"]:
            return CommandResult(1 if argv[3] in self.missing_images else 0, "")
        if argv and argv[0] == "bash" and "--phase" in argv:
            phase = argv[argv.index("--phase") + 1]
            if phase == "activate" and self.phase_exit == 0 and self.activate_writes is not None:
                (self.deploy_root / ".deployed-sha").write_text(self.activate_writes + "\n")
            return CommandResult(self.phase_exit, f"phase {phase}")
        if "install" in argv:
            return CommandResult(self.kata_install_exit, "installed")
        if "check" in argv:
            return CommandResult(self.kata_check_exit, "checked")
        return CommandResult(0, "")
