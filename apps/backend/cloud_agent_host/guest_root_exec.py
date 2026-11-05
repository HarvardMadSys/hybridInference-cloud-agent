"""The sandbox manager's own Kata guest-root exec, kept off the exact-container surface.

:class:`~cloud_agent_host.exact_container.ExactContainer` is handed to every
module that reaches a chat's container; running a helper as guest root is not
one of its operations. :class:`GuestRootExec` gives it to the sandbox manager
alone, privately, built on the exact container's public identity gate. The
group network's one use of it is :meth:`GuestRootExec.sync_guest_network`,
the method its container protocol names, which takes no argv.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent_host.sandbox import KATA_RUNTIME, SandboxError

if TYPE_CHECKING:
    import subprocess

    from cloud_agent_host.chat_sandbox_models import ChatSandboxSpec
    from cloud_agent_host.exact_container import ExactContainer

#: Writes the guest's relay block in ``/etc/hosts`` and its one resolver in
#: ``/etc/resolv.conf``, each only while root-owned and protected.
_GUEST_NETWORK_SCRIPT = (
    "import fcntl, ipaddress, json, os, stat, sys\n"
    "entries=json.loads(sys.argv[1])\n"
    "resolver=str(ipaddress.IPv4Address(sys.argv[2]))\n"
    "def protected(f, name):\n"
    " s=os.fstat(f.fileno())\n"
    " if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:\n"
    "  raise RuntimeError('guest '+name+' is not root-owned and protected')\n"
    "begin='# BEGIN FREEINFERENCE RELAYS\\n'\n"
    "end='# END FREEINFERENCE RELAYS\\n'\n"
    "fd=os.open('/etc/hosts', os.O_RDWR | os.O_NOFOLLOW)\n"
    "with os.fdopen(fd, 'r+') as f:\n"
    " fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
    " protected(f, 'hosts file')\n"
    " original=f.read(65537)\n"
    " text=original\n"
    " if len(text)>65536: raise RuntimeError('guest hosts file is too large')\n"
    " if begin in text:\n"
    "  before, rest=text.split(begin,1)\n"
    "  if end not in rest: raise RuntimeError('invalid managed relay block')\n"
    "  text=before+rest.split(end,1)[1]\n"
    " text=text.rstrip()+'\\n'+begin\n"
    " text+=''.join(ip+' '+host+'\\n' for host,ip in sorted(entries.items()))\n"
    " if original != text+end:\n"
    "  f.seek(0); f.write(text+end); f.truncate()\n"
    "fd=os.open('/etc/resolv.conf', os.O_RDWR | os.O_NOFOLLOW)\n"
    "with os.fdopen(fd, 'r+') as f:\n"
    " fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
    " protected(f, 'resolv.conf')\n"
    " desired='nameserver '+resolver+'\\n'\n"
    " if f.read(len(desired)+1) != desired:\n"
    "  f.seek(0); f.write(desired); f.truncate()\n"
)


class GuestRootExec:
    """Run source-controlled platform helpers as Kata guest root, for the sandbox manager."""

    #: The identity gate every Docker operation on a chat goes through.
    exact: ExactContainer

    def _kata_privileged_exec(
        self,
        spec: ChatSandboxSpec,
        argv: list[str],
        *,
        detach: bool = False,
        timeout_s: float = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Run one source-controlled platform helper as Kata guest root.

        This is deliberately not a general privileged-exec API.  Callers pass
        only source-controlled observer/cleanup/network argv, and the runtime
        guard prevents them from gaining privilege on the host's shared kernel
        under runc.
        """
        exact = self.exact
        if exact.backend.runtime != KATA_RUNTIME:
            raise SandboxError("guest-root platform helpers require the Kata runtime")
        not_running = "chat sandbox must be started before guest observation"
        current, proven = exact.exec_target(spec, not_running=None)
        if current.sandbox_runtime != KATA_RUNTIME:
            raise SandboxError("guest-root platform helpers require an inspected Kata container")
        if proven is None and not current.running:
            raise SandboxError(not_running)
        command = [exact.backend.docker_binary, "exec"]
        if detach:
            command.append("--detach")
        command += [
            "--privileged",
            "--user",
            "0:0",
            "--workdir",
            "/",
            current.container_id,
            *argv,
        ]
        result = exact.run(command, timeout=timeout_s)
        return exact.checked_exec(spec, proven, result, not_running=not_running)

    def sync_guest_network(
        self, spec: ChatSandboxSpec, addresses: dict[str, str], resolver: str
    ) -> subprocess.CompletedProcess[str]:
        """Write the guest's relay hosts and its one resolver, as Kata guest root.

        The group network's one guest-root operation
        (:class:`~cloud_agent_host.chat_sandbox_network.ChatNetworkContainers`),
        running only this module's own script.
        """
        argv = ["python3", "-I", "-c", _GUEST_NETWORK_SCRIPT, json.dumps(addresses), resolver]
        return self._kata_privileged_exec(spec, argv, timeout_s=30)


__all__ = ["GuestRootExec"]
