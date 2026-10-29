"""Docker commands for retained chats, with plain inspects read from the daemon's socket.

A sandbox's start reads far more than it changes: a follow-up turn's activation
is ten ``docker container inspect`` and ``docker network inspect`` calls and
nothing else, and a cold start is around thirty among its few creates, starts
and connects. Each read through the CLI pays the ``docker`` binary's own
startup, around 25 ms, for an answer the daemon's Engine API gives in under a
millisecond on the unix socket the CLI would use itself.

So :func:`run_docker` answers exactly those reads, in their plain form (one
or more references, no flags), from the API, shaped as the CLI shapes them:
the JSON array of what was found on stdout, one ``Error response from daemon:
...`` line per reference that was not, and exit status 1 when any was not.
Every caller's parsing, identity, mount and membership checks run on that
unchanged, and a fresh read is still a fresh read. Anything that changes
something, and any read with flags, runs the CLI, as does a read the API does
not answer plainly: an unreachable socket, a status other than found or not
found, or a body that is not JSON.

The API is used only where it is certainly the CLI's own daemon
(:func:`engine_from_env`): the default ``docker`` executable, reaching a unix
socket by ``DOCKER_HOST`` or by the default context. Pointed anywhere else, a
process keeps the CLI for every call, since an answer from a daemon the CLI
does not talk to would be worse than a slow one. The requests are GETs, over
the socket the processes that build a backend from the environment already
mount.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The Engine API version every read names: the oldest a supported daemon
#: serves (``sandbox.DOCKER_MIN_API``), so a newer daemon still answers in the
#: shape the readers were written against.
API_VERSION = "1.48"

#: Where the CLI finds its daemon when nothing points it elsewhere.
DEFAULT_SOCKET = "/var/run/docker.sock"

#: The objects whose plain inspect is read here, and each one's API path.
_INSPECT_PATHS = {"container": "/containers/{}/json", "network": "/networks/{}"}


class _UnixHTTPConnection(http.client.HTTPConnection):
    """One HTTP/1.1 connection to the daemon over its unix socket."""

    def __init__(self, socket_path: str, *, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        """Connect to the socket instead of a TCP host."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self._socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class DockerEngine:
    """Read containers and networks from the daemon's Engine API."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET) -> None:
        self.socket_path = socket_path

    def inspect(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str] | None:
        """The CLI's answer to a plain inspect ``argv``, or ``None`` to run the CLI.

        One connection per reference: a connection costs well under a
        millisecond here, and none outlives the call that opened it.

        Raises:
            SandboxError: The daemon did not answer within ``timeout``, as the
                CLI's own timeout would; a hung daemon would hang it too.
        """
        path = _INSPECT_PATHS.get(argv[1]) if len(argv) > 3 and argv[2] == "inspect" else None
        refs = argv[3:]
        if path is None or any(ref.startswith("-") for ref in refs):
            return None
        deadline = time.monotonic() + timeout
        found: list[object] = []
        missing: list[str] = []
        for ref in refs:
            answer = self._get(path.format(quote(ref, safe="")), deadline=deadline)
            if answer is None:
                return None
            status, body = answer
            message = body.get("message") if isinstance(body, dict) else None
            if status == 200 and isinstance(body, dict):
                found.append(body)
            elif status == 404 and isinstance(message, str):
                missing.append(f"Error response from daemon: {message}\n")
            else:
                return None
        return subprocess.CompletedProcess(
            list(argv), 1 if missing else 0, json.dumps(found), "".join(missing)
        )

    def _get(self, path: str, *, deadline: float) -> tuple[int, object] | None:
        """One GET's status and decoded body, or ``None`` when it cannot be read."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxError("Docker lifecycle command failed: the Engine API timed out")
        connection = _UnixHTTPConnection(self.socket_path, timeout=remaining)
        try:
            connection.request("GET", f"/v{API_VERSION}{path}")
            response = connection.getresponse()
            status, data = response.status, response.read()
        except TimeoutError as exc:
            raise SandboxError(f"Docker lifecycle command failed: {exc}") from exc
        except (OSError, http.client.HTTPException):
            return None
        finally:
            connection.close()
        try:
            return status, json.loads(data)
        except ValueError:
            return None


def run_docker(
    argv: list[str], *, timeout: float, engine: DockerEngine | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one Docker command, reading a plain inspect from ``engine`` when it answers."""
    answered = engine.inspect(argv, timeout=timeout) if engine is not None else None
    if answered is not None:
        return answered
    try:
        return subprocess.run(
            argv,
            env={**os.environ},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"Docker lifecycle command failed: {exc}") from exc


def engine_from_env(env: Mapping[str, str], docker_binary: str) -> DockerEngine | None:
    """The Engine API ``docker_binary`` reaches under ``env``, when that is certain.

    The CLI's own order: ``DOCKER_HOST`` overrides any context; otherwise
    ``DOCKER_CONTEXT``, then the configuration's ``currentContext``, then the
    default socket. ``None`` keeps every read on the CLI: another executable,
    a daemon reached other than by a unix socket, or a named context, whose
    endpoint only the CLI's context store knows.
    """
    if docker_binary != "docker":
        return None
    host = env.get("DOCKER_HOST") or ""
    if host:
        path = host.removeprefix("unix://") if host.startswith("unix://") else ""
        return DockerEngine(path) if path else None
    context = env.get("DOCKER_CONTEXT") or _configured_context(env)
    return DockerEngine() if context == "default" else None


def _configured_context(env: Mapping[str, str]) -> str:
    """The context the CLI's configuration file selects; ``""`` when it cannot be read."""
    home = env.get("HOME") or os.path.expanduser("~")
    directory = env.get("DOCKER_CONFIG") or os.path.join(home, ".docker")
    try:
        config = json.loads(Path(directory, "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "default"
    except (OSError, ValueError):
        return ""
    context = config.get("currentContext") if isinstance(config, dict) else None
    return context if isinstance(context, str) and context else "default"


__all__ = ["API_VERSION", "DEFAULT_SOCKET", "DockerEngine", "engine_from_env", "run_docker"]
