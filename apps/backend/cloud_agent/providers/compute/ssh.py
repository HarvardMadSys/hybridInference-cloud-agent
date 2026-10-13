"""One SSH transport for resource identity checks and host deployment."""

from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from cloud_agent.ports.external.compute_provider import ProviderError

if TYPE_CHECKING:
    from cloud_agent.ports.external.compute_provider import SecretResolver, SshConnection

#: The most a remote step may print. Every script sent here answers with one
#: JSON document or one line, far below this; a node that keeps writing is
#: refused while the bytes arrive rather than after they are all in memory.
MAX_SSH_OUTPUT_BYTES = 4 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class _OutputTooLarge(Exception):
    """The remote printed more than :data:`MAX_SSH_OUTPUT_BYTES`."""


def known_hosts_line(address: str, port: int, host_key: str) -> str:
    """Render one pinned key in the known_hosts form OpenSSH looks up."""
    pattern = address if port == 22 else f"[{address}]:{port}"
    return f"{pattern} {host_key}\n"


async def _exchange(process: asyncio.subprocess.Process, stdin: bytes | None) -> bytes:
    """Feed ``stdin`` and read stdout to EOF, refusing more than the output cap.

    ``communicate()`` reads to EOF before anything can look at the size, so a
    remote that never stops writing fills this process first. The node is the
    party being inspected here, not a trusted peer: a replaced
    ``/etc/machine-id`` or a ``ForceCommand`` can print without end. Reading in
    chunks lets the cap act as the bytes arrive; the caller's deadline still
    covers a remote that writes slowly forever.

    Stdin is written concurrently, as ``communicate()`` does, so a remote that
    answers while it is still reading a large archive cannot deadlock on a
    full pipe.
    """
    assert process.stdin is not None and process.stdout is not None
    writer = process.stdin

    async def feed() -> None:
        try:
            if stdin:
                writer.write(stdin)
                await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # The remote stopped reading; its exit status says why.
        finally:
            writer.close()

    feeding = asyncio.create_task(feed())
    try:
        output = bytearray()
        while chunk := await process.stdout.read(_READ_CHUNK_BYTES):
            if len(output) + len(chunk) > MAX_SSH_OUTPUT_BYTES:
                raise _OutputTooLarge
            output.extend(chunk)
        await feeding
        await process.wait()
    finally:
        if not feeding.done():
            feeding.cancel()
            with suppress(asyncio.CancelledError):
                await feeding
    return bytes(output)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill the client and reap it without keeping what is left in the pipe.

    Once the client is dead nothing writes to the pipe any more, so draining it
    reads at most what the kernel and the stream had already buffered. It must
    be drained: a paused pipe never reports EOF, and asyncio does not report
    the exit of a process whose pipes are still open.
    """
    with suppress(ProcessLookupError):
        process.kill()
    if process.stdout is not None:
        while await process.stdout.read(_READ_CHUNK_BYTES):
            pass
    await process.wait()


class OpenSshTransport:
    """Use isolated SSH credentials and suppress secret-bearing diagnostics."""

    def __init__(self, resolver: SecretResolver) -> None:
        self._resolver = resolver

    async def run(
        self,
        connection: SshConnection,
        script: str,
        *,
        stdin: bytes | None = None,
        timeout: float = 30,
    ) -> str:
        # Do not inherit the daemon user's SSH config, agents, forwarding or
        # trust-on-first-use policy. Each request has isolated temporary files.
        """Run a trusted script without exposing credentials or error output."""
        key = await self._resolver.resolve(connection.credential_ref)
        if not key.strip():
            raise ProviderError("ssh_configuration", "SSH key is required")
        known_hosts = ""
        verify_host_key = connection.verify_host_key
        if verify_host_key:
            if connection.host_key:
                # A key the node registered with its one-time token: trust exactly it.
                known_hosts = known_hosts_line(
                    connection.address, connection.port, connection.host_key
                )
            elif connection.known_hosts_ref:
                known_hosts = await self._resolver.resolve(connection.known_hosts_ref)
            if not known_hosts.strip():
                raise ProviderError("ssh_configuration", "Verified SSH host keys are required")
        with tempfile.TemporaryDirectory(prefix="agent-compute-ssh-") as directory:
            key_path = Path(directory) / "identity"
            key_path.write_text(key.rstrip() + "\n", encoding="utf-8")
            key_path.chmod(0o600)
            host_key_args: list[str]
            if verify_host_key:
                known_path = Path(directory) / "known_hosts"
                known_path.write_text(known_hosts.rstrip() + "\n", encoding="utf-8")
                known_path.chmod(0o600)
                host_key_args = [
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={known_path}",
                ]
            else:
                host_key_args = [
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                ]
            args = [
                "ssh",
                "-F",
                "/dev/null",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                *host_key_args,
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                "UpdateHostKeys=no",
                "-o",
                "ForwardAgent=no",
                "-o",
                "ClearAllForwardings=yes",
                "-o",
                "ControlMaster=no",
                "-o",
                "ConnectTimeout=20",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=3",
                "-i",
                str(key_path),
                "-p",
                str(connection.port),
                "-l",
                connection.username,
                connection.address,
                "sh -c " + shlex.quote(script),
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    # Never read: it can carry remote diagnostics, and an
                    # unread pipe is one more stream the remote could fill.
                    stderr=asyncio.subprocess.DEVNULL,
                    env={k: v for k, v in os.environ.items() if k != "SSH_AUTH_SOCK"},
                )
            except OSError:
                raise ProviderError("ssh_unavailable", "SSH client could not start") from None
            try:
                stdout = await asyncio.wait_for(_exchange(process, stdin), timeout=timeout)
            except _OutputTooLarge:
                await _terminate(process)
                raise ProviderError(
                    "invalid_response", "SSH response exceeded the size limit"
                ) from None
            except (TimeoutError, asyncio.CancelledError) as exc:
                await _terminate(process)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                # Killing the client cannot undo a remote side effect.
                raise ProviderError(
                    "result_unknown", "SSH timed out; verify remote state before retrying"
                ) from None
            if process.returncode != 0:
                code = (
                    "ssh_connection_failed" if process.returncode == 255 else "remote_step_failed"
                )
                raise ProviderError(code, "SSH verification or remote deployment step failed")
            return stdout.decode("utf-8", errors="replace")
