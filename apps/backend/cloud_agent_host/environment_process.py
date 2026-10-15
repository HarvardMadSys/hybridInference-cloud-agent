"""Scoped identities and completion receipts for environment script processes."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from cloud_agent_host.sandbox import SandboxError, SandboxProcess

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloud_agent_host.chat_sandbox import RetainedChatBackend

ENVIRONMENT_UIDS = (30000, 30001, 30002)
ACTION_UIDS = tuple(range(31000, 31064))

# The attached helper outlives the foreground script while any service still
# owns its pipe. A completion receipt lets the caller hand off without waiting
# for service EOF. No output or variable values are written into the workspace.
_HELPER = r"""
import os, subprocess, threading, sys
path=os.environ.pop("AGENT_SCRIPT_STATUS")
script=os.environ.pop("AGENT_ENVIRONMENT_SCRIPT")
os.umask(0o002)
p=subprocess.Popen(["/bin/sh","-c",script],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
def drain():
 while True:
  chunk=os.read(p.stdout.fileno(),4096)
  if not chunk: break
  try: os.write(1,chunk)
  except BrokenPipeError: pass
reader=threading.Thread(target=drain)
reader.start()
code=p.wait()
with open(path+".tmp","w") as f: f.write(str(code))
os.replace(path+".tmp",path)
reader.join()
sys.exit(code)
"""


def kill_identity(backend: RetainedChatBackend, uid: int) -> None:
    """Reap all descendants of a reserved operation, including detached ones."""
    if uid not in (*ENVIRONMENT_UIDS, *ACTION_UIDS):
        raise SandboxError("Invalid environment operation identity")
    result = backend.manager.exec_capture(
        backend.spec,
        [
            "python3",
            "-c",
            """import os,signal,sys,time
uid=int(sys.argv[1])
def live():
 found=[]
 for name in os.listdir('/proc'):
  if not name.isdigit() or int(name)==os.getpid(): continue
  try:
   if os.stat('/proc/'+name).st_uid==uid and open('/proc/'+name+'/stat').read().rsplit(')',1)[1].split()[0] not in ('Z','X'): found.append(int(name))
  except (OSError,IndexError): pass
 return found
for sig in (signal.SIGTERM,signal.SIGKILL):
 for pid in live():
  try: os.kill(pid,sig)
  except ProcessLookupError: pass
 for _ in range(20):
  if not live(): sys.exit(0)
  time.sleep(.05)
sys.exit(1 if live() else 0)
""",
            str(uid),
        ],
        user_uid=uid,
        user_gid=backend.manager.backend.gid,
        workdir="/",
        timeout_s=10,
    )
    if result.returncode:
        raise SandboxError("Environment operation cleanup could not be confirmed")


def prepare_home(manager: Any, spec: Any, uid: int) -> str:
    """Open root-owned parents and private leaves without following symlinks."""
    if uid not in (*ENVIRONMENT_UIDS, *ACTION_UIDS):
        raise SandboxError("Invalid environment identity")
    script = """import os,sys
uid=int(sys.argv[1])
path='/run/agent-environments/'+str(uid)
fd=os.open(path,os.O_DIRECTORY|os.O_NOFOLLOW)
if os.fstat(fd).st_uid!=uid: sys.exit(2)
os.fchmod(fd,0o700)
try: os.mkdir('home',0o700,dir_fd=fd)
except FileExistsError: pass
home=os.open('home',os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
if os.fstat(home).st_uid!=uid: sys.exit(3)
os.fchmod(home,0o700)
for name in ('status','status.tmp'):
 try: os.unlink(name,dir_fd=fd)
 except FileNotFoundError: pass
"""
    prepared = manager.exec_capture(
        spec,
        ["python3", "-c", script, str(uid)],
        user_uid=uid,
        user_gid=manager.backend.gid,
        workdir="/",
        timeout_s=10,
    )
    if prepared.returncode:
        raise SandboxError("Environment operation home could not be prepared")
    return f"/run/agent-environments/{uid}"


TOOLS_ROOT = "/workspace/.cloud-agent-tools"


def tool_environment() -> dict[str, str]:
    """Share persistent package data without sharing member credential homes."""
    return {
        "UV_PYTHON_INSTALL_DIR": TOOLS_ROOT + "/uv/python",
        "UV_CACHE_DIR": TOOLS_ROOT + "/uv/cache",
        "UV_TOOL_DIR": TOOLS_ROOT + "/uv/tools",
        "UV_TOOL_BIN_DIR": TOOLS_ROOT + "/bin",
        "CARGO_HOME": TOOLS_ROOT + "/cargo",
        "GOPATH": TOOLS_ROOT + "/go",
        "POETRY_VIRTUALENVS_IN_PROJECT": "true",
        "POETRY_CACHE_DIR": TOOLS_ROOT + "/poetry",
        "PYTHONUSERBASE": TOOLS_ROOT + "/python-user",
        "PIP_CACHE_DIR": TOOLS_ROOT + "/pip",
        "NPM_CONFIG_CACHE": TOOLS_ROOT + "/npm",
    }


def prepare_tool_home(manager: Any, spec: Any, uid: int) -> str:
    """Create an executable shared home only with the operation's own identity."""
    result = manager.exec_capture(
        spec,
        [
            "/bin/sh",
            "-c",
            "set -eu; umask 002; mkdir -p .cloud-agent-tools/home; "
            "for directory in .cloud-agent-tools .cloud-agent-tools/home; do "
            'if [ "$(stat -c %u -- "$directory")" = "$(id -u)" ]; then '
            'chmod g+rwx,g+s "$directory"; fi; done; '
            "if [ -d .git ]; then mkdir -p .git/info; touch .git/info/exclude; "
            "grep -qxF '/.cloud-agent-tools/' .git/info/exclude || "
            "printf '\\n/.cloud-agent-tools/\\n' >> .git/info/exclude; fi",
        ],
        user_uid=uid,
        user_gid=manager.backend.gid,
        workdir="/workspace",
        timeout_s=10,
    )
    if result.returncode:
        raise SandboxError("Environment tool directories could not be prepared")
    return TOOLS_ROOT + "/home"


class EnvironmentProcess(SandboxProcess):
    """An exec with a foreground result and a separately owned service stream."""

    def __init__(
        self, backend: RetainedChatBackend, *, phase: str, script: str, env: dict[str, str]
    ) -> None:
        self.backend = backend
        # An update script amends the installation, so it runs as the installer.
        self.uid = {"setup": 30000, "update": 30000, "start": 30001, "verify": 30002}[phase]
        kill_identity(backend, self.uid)
        directory = prepare_home(backend.manager, backend.spec, self.uid)
        self.status_path = directory + "/status"
        values = {
            **backend.base_env(),
            **tool_environment(),
            **env,
            "HOME": prepare_tool_home(backend.manager, backend.spec, self.uid),
            "AGENT_ENVIRONMENT_SCRIPT": script,
            "AGENT_SCRIPT_STATUS": self.status_path,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.process = backend.manager.spawn(
            backend.spec,
            ["python3", "-u", "-c", _HELPER],
            env=values,
            workdir="/workspace",
            user_uid=self.uid,
            user_gid=backend.manager.backend.gid,
        )
        self._code: int | None = None
        self._next_poll = 0.0
        self._lock = threading.Lock()

    def lines(self) -> Iterator[str]:
        """Yield line-oriented attached script output."""
        yield from self.process.lines()

    def chunks(self) -> Iterator[str]:
        """Yield bounded chunks while background services own their pipe."""
        yield from self.process.chunks()

    def poll(self) -> int | None:
        """Read the foreground completion receipt without waiting for service EOF."""
        return self._poll(force=False)

    def _poll(self, *, force: bool) -> int | None:
        with self._lock:
            if self._code is not None:
                return self._code
            if not force and time.monotonic() < self._next_poll:
                return None
            self._next_poll = time.monotonic() + 1
            result = self.backend.manager.exec_capture(
                self.backend.spec,
                ["cat", self.status_path],
                user_uid=self.uid,
                user_gid=self.backend.manager.backend.gid,
                workdir="/",
                timeout_s=10,
            )
            if result.returncode == 0:
                try:
                    self._code = int(result.stdout)
                except ValueError:
                    raise SandboxError("Invalid environment completion receipt") from None
            else:
                helper_code = self.process.poll()
                if helper_code == 0:
                    raise SandboxError("Environment completion receipt is missing")
                self._code = helper_code
            return self._code

    def wait(self) -> int:
        """Return the foreground result independently of background services."""
        code = self._poll(force=True)
        if code is not None:
            return code
        self.process.wait()
        code = self._poll(force=True)
        if code is None:
            raise SandboxError("Environment completion receipt is missing")
        return code

    def kill(self) -> None:
        """Reap only this phase identity, including detached children."""
        kill_identity(self.backend, self.uid)

    def stderr_text(self) -> str:
        """Return helper errors after foreground failure."""
        return "" if self._code == 0 else self.process.stderr_text()
