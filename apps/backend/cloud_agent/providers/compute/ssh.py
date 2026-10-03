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


class OpenSshTransport:
    """Use isolated strict SSH trust files and suppress secret-bearing diagnostics."""

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
        """Run a trusted remote command without exposing credentials or error output."""
        key = await self._resolver.resolve(connection.credential_ref)
        known_hosts = await self._resolver.resolve(connection.known_hosts_ref)
        if not key.strip() or not known_hosts.strip():
            raise ProviderError("ssh_configuration", "SSH key and verified host keys are required")
        with tempfile.TemporaryDirectory(prefix="agent-compute-ssh-") as directory:
            key_path = Path(directory) / "identity"
            known_path = Path(directory) / "known_hosts"
            for path, content in ((key_path, key), (known_path, known_hosts)):
                path.write_text(content.rstrip() + "\n", encoding="utf-8")
                path.chmod(0o600)
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
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known_path}",
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
                    stderr=asyncio.subprocess.PIPE,
                    env={k: v for k, v in os.environ.items() if k != "SSH_AUTH_SOCK"},
                )
            except OSError:
                raise ProviderError("ssh_unavailable", "SSH client could not start") from None
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
            except (TimeoutError, asyncio.CancelledError) as exc:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.communicate()
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
            if len(stdout) > 4 * 1024 * 1024:
                raise ProviderError("invalid_response", "SSH response exceeded the size limit")
            return stdout.decode("utf-8", errors="replace")
