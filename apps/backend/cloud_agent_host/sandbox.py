"""Sandbox primitives used to operate a retained chat container.

The runner is deliberately substrate-agnostic: it claims over HTTP, executes,
and reports over HTTP. *How* the execution is isolated is this module's job,
selected by configuration, so the same runner code serves dogfooding on CI,
self-hosted machines, and a multi-tenant fleet.

The production topology is runner outside, retained container per chat group
(``container`` / ``kata``). Scoped inference/MCP credentials and a short-lived
repository-scoped GitHub App token cross into the sandbox; control-plane,
account and service credentials remain outside. The
``process`` backend exists only as a deterministic test seam and cannot attest
or claim a retained job.

Backends are intentionally thin: they take an argv the runtime adapter built
and hand back a line stream. They do not know what an agent is.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent_host.bounded_output import TextTail, protocol_lines, text_chunks
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_shared.egress import SandboxNetwork, network_from_env
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloud_agent_host.network_guard import HostNetworkGuard

logger = get_logger(__name__)

# Run inside one ephemeral preflight probe on the relay discovery network,
# with $1 = the URL to probe and $2 = its hostname. Prints exactly one verdict.
#
# curl and python3 are both in the shipped sandbox image; a deployment-owned
# image might have neither, and "dns-only" says so rather than letting a
# weaker check pass for the same thing. Any HTTP status counts as reachable —
# the question is whether packets arrive, not what the gateway makes of them.
_GATEWAY_PROBE_SH = """
url="$1"; host="$2"
if command -v curl >/dev/null 2>&1; then
    # Captured immediately: after an `if`, $? is the compound statement's own
    # status, not curl's, and every failure would read as the same verdict.
    curl -sS -o /dev/null --max-time 15 "$url"; rc=$?
    # 6 is curl's "could not resolve host"; everything else got that far.
    if [ "$rc" -eq 0 ]; then echo ok
    elif [ "$rc" -eq 6 ]; then echo unresolved
    else echo unreachable; fi
    exit 0
fi
if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import socket, sys, urllib.error, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=15)
except urllib.error.HTTPError:
    print("ok")
except urllib.error.URLError as exc:
    print("unresolved" if isinstance(exc.reason, socket.gaierror) else "unreachable")
except Exception:
    print("unreachable")
else:
    print("ok")' "$url"
    exit 0
fi
if getent hosts "$host" >/dev/null 2>&1; then echo dns-only; else echo unresolved; fi
"""

# There is no default image, deliberately. Upstream publishes none, and an
# unqualified name is not an inert placeholder: `docker run hybridinference/
# agent-sandbox` resolves through Docker Hub, so anyone who registered that
# namespace would be supplying the container an untrusted agent runs inside.
# A runner started with a container backend and no AGENT_SANDBOX_IMAGE refuses
# instead.
# containerd's Kata shim. Docker exposes VM-isolated runtimes under the same
# `--runtime` flag as runc, which is why one backend covers both: they are the
# same mechanism with a different isolation boundary underneath.
KATA_RUNTIME = "io.containerd.kata.v2"

#: Docker 28 (API 1.48) gave a network endpoint its gateway priority, which is
#: what keeps a group bridge from becoming a relay's default route. Both ends
#: need it: the client to send it, the daemon to honour it.
DOCKER_MIN_CLIENT_MAJOR = 28
DOCKER_MIN_API = (1, 48)

# Image labels carrying the CLI versions baked into a sandbox image, e.g.
# ``org.freeinference.runtime.claude-code.version=1.2.3``. The image build
# writes them; the runner only reads. Unlabeled images report no versions
# rather than guessing — a stamp that might be wrong is worse than none.
RUNTIME_VERSION_LABEL_PREFIX = "org.freeinference.runtime."
RUNTIME_VERSION_LABEL_SUFFIX = ".version"
#: The whole menu, where ``.version`` is only the default. Distinct labels
#: because they answer different questions and a reader that knows nothing
#: about menus must keep reading a single exact version out of ``.version``.
RUNTIME_VERSION_MENU_LABEL_SUFFIX = ".versions"
#: Image-resident helpers which participate in a protocol capability but are
#: not selectable Agent CLIs.  Keeping this namespace distinct prevents a
#: helper such as the Claude Agent SDK from appearing in the admin runtime
#: version menu.
HELPER_VERSION_LABEL_PREFIX = "org.freeinference.helper."
HELPER_VERSION_LABEL_SUFFIX = ".version"

#: Where `deploy/sandbox/install-runtimes.sh` puts every version it installs.
#: Selecting one is a PATH prepend and nothing more: all five CLIs resolve
#: their binary through PATH, so neither the adapters in ``runtimes.py`` nor
#: the pi/opencode/kilo wrappers have to know a choice was made.
RUNTIME_BIN_ROOT = "/opt/runtimes"

# Must match `useradd --uid` in deploy/sandbox/Dockerfile.agent-sandbox. The
# retained container creates the workspace for this id, so a drift between the
# two means the Agent CLI cannot write its own tree.
SANDBOX_UID = 10001
SANDBOX_GID = 10001
SANDBOX_HOME = "/home/agent"
SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def runtime_bin_dir(runtime_id: str, version: str) -> str:
    """Where this runtime's ``version`` keeps its binaries inside the image.

    Prepending this to the sandbox's ``PATH`` is the whole of version
    selection — see ``RUNTIME_BIN_ROOT``.
    """
    return f"{RUNTIME_BIN_ROOT}/{runtime_id}/{version}/bin"


def _labelled(labels: dict[str, Any], suffix: str) -> dict[str, str]:
    """Image labels under this project's runtime namespace, keyed by runtime id.

    ``.version`` is not a suffix of ``.versions``, so the two never collide.
    """
    found: dict[str, str] = {}
    for key, value in labels.items():
        if (
            isinstance(key, str)
            and isinstance(value, str)
            and key.startswith(RUNTIME_VERSION_LABEL_PREFIX)
            and key.endswith(suffix)
        ):
            runtime_id = key[len(RUNTIME_VERSION_LABEL_PREFIX) : -len(suffix)]
            if runtime_id:
                found[runtime_id] = value
    return found


def _helper_versions(labels: dict[str, Any]) -> dict[str, str]:
    """Return exact helper attestations without treating them as runtimes."""
    found: dict[str, str] = {}
    for key, value in labels.items():
        if (
            isinstance(key, str)
            and isinstance(value, str)
            and key.startswith(HELPER_VERSION_LABEL_PREFIX)
            and key.endswith(HELPER_VERSION_LABEL_SUFFIX)
        ):
            helper_id = key[len(HELPER_VERSION_LABEL_PREFIX) : -len(HELPER_VERSION_LABEL_SUFFIX)]
            if helper_id:
                found[helper_id] = value
    return found


def _split_menu(menu: str | None, default: str) -> list[str]:
    """The versions a runtime offers, default first and always present.

    Order is the label's, which is the order they were installed in, which puts
    the default at the head. Kept rather than sorted: "first" is a meaning here,
    not an accident, and an operator reading the admin page sees the same order
    the image declares.
    """
    versions = [part.strip() for part in (menu or "").split(",")]
    offered = [version for version in versions if version]
    if default and default not in offered:
        # A menu that omits its own default cannot be honoured — the default is
        # what a job with no selection runs, so it is always available.
        offered.insert(0, default)
    return offered


class TerminalNotSupportedError(SandboxError):
    """Raised when a backend cannot safely provide persistent terminals."""


#: Where a chat's durable harness state lives in its Docker writable layer.
#: Constant on purpose: Claude Code derives its session-file path from CWD and
#: config dir, and a path that moved between turns would strand native state.
STATE_MOUNT_TARGET = "/agent-state"


@dataclass
class SandboxSpec:
    """Everything a backend needs to run one job's agent."""

    argv: list[str]
    workdir: str
    env: dict[str, str] = field(default_factory=dict)
    #: Keep stdin open for a resident session (HOT, design §7). Bounded helper
    #: commands such as setup/Git probes leave this false.
    live_input: bool = False


class SandboxProcess(ABC):
    """A running agent, exposing a line stream and a kill switch."""

    @abstractmethod
    def lines(self) -> Iterator[str]:
        """Yield the agent's stdout, line by line, as it is produced."""

    def chunks(self) -> Iterator[str]:
        """Yield bounded stdout chunks; line-only test adapters may reuse lines."""
        yield from self.lines()

    @abstractmethod
    def kill(self) -> None:
        """Terminate the agent immediately."""

    @abstractmethod
    def wait(self) -> int:
        """Wait for exit and return the status code."""

    @abstractmethod
    def stderr_text(self) -> str:
        """Return whatever the agent wrote to stderr."""

    @property
    def container(self) -> str | None:
        """The container this runs in, when it runs in one.

        Only the host-side resource sampler consumes this: measuring a
        sandbox means resolving its cgroup, and that starts from a name the
        daemon can be asked about. Backends without a container answer None
        and are simply not measured.
        """
        return None

    def write_line(self, text: str) -> None:
        """Write one line to the agent's stdin (HOT live input, design §7).

        Only meaningful for a process spawned with ``live_input``; the
        default refuses, because writing into a closed pipe should be a
        driver bug surfaced loudly, not a silent drop.
        """
        raise SandboxError("this sandbox process was not spawned with live input")

    def close_stdin(self) -> None:
        """Signal end-of-input to a live process. No-op without live input."""
        return

    def poll(self) -> int | None:
        """Reap-if-exited: the exit code, or ``None`` while still running."""
        return None


class TerminalProcess(ABC):
    """A byte-oriented interactive terminal living inside one sandbox.

    This is deliberately separate from :class:`SandboxProcess`. Agent event
    parsing needs line-delimited stdout and a separate stderr stream, while a
    terminal needs one PTY byte stream plus stdin and window resizing. Trying
    to make one interface serve both silently breaks control sequences and
    interactive programs.
    """

    #: The shell this terminal runs, as its owner sees it named.
    shell: str = "sh"

    @abstractmethod
    def chunks(self) -> Iterator[bytes]:
        """Yield terminal output bytes until the terminal reaches EOF."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Write bytes to the terminal's stdin."""

    @abstractmethod
    def resize(self, rows: int, cols: int) -> None:
        """Resize the terminal window."""

    def suspend(self) -> None:
        """Pause the terminal's complete process hierarchy."""
        raise TerminalNotSupportedError("this terminal backend cannot suspend process trees")

    def resume(self) -> None:
        """Resume a terminal process hierarchy paused by :meth:`suspend`."""
        raise TerminalNotSupportedError("this terminal backend cannot resume process trees")

    @abstractmethod
    def kill(self) -> None:
        """Terminate the terminal and its complete process hierarchy."""

    @abstractmethod
    def wait(self) -> int:
        """Wait for exit and return the status code."""


class SandboxBackend(ABC):
    """Creates isolated execution environments for agent jobs."""

    name: str = "abstract"

    @property
    def provides_isolation(self) -> bool:
        """Whether this backend supplies the agent's execution boundary.

        The process backend runs the CLI directly, so a runtime's own sandbox
        remains the only command boundary. Container backends isolate the
        complete CLI already; asking a nested Linux sandbox to create another
        user namespace is both redundant and incompatible with the outer
        container's dropped capabilities.
        """
        return False

    def sandbox_metadata(self) -> dict[str, str]:
        """Return trusted, owner-visible metadata for this execution backend."""
        return {"sandbox_backend": self.name}

    def version_stamp(self) -> dict[str, Any]:
        """What actually runs here, for the host heartbeat to report.

        Empty for backends with nothing to attest. The container backend
        reports its isolation runtime, image digest and the CLI versions baked
        into the image, so the control plane can stamp attempts with what
        executed them (issue #54) instead of trusting documentation.
        """
        return {}

    @abstractmethod
    def spawn(self, spec: SandboxSpec) -> SandboxProcess:
        """Start the agent and return a handle to it."""

    def spawn_terminal(self, spec: SandboxSpec, *, rows: int, cols: int) -> TerminalProcess:
        """Start an interactive terminal and return its byte-stream handle.

        Kept as an optional capability so an existing custom backend that only
        runs agents does not become un-instantiable when terminals are added.
        """
        raise TerminalNotSupportedError(
            "interactive terminals require the container or kata sandbox backend; "
            f"the {self.name!r} backend cannot guarantee whole-session cleanup"
        )

    def preflight(self) -> None:
        """Raise :class:`SandboxError` if this backend cannot run here.

        Called before a job is claimed so a misconfigured host fails loudly
        instead of taking a job off the queue and burning one of its attempts.
        Backends with nothing to check inherit this no-op.
        """
        return

    def has_binary(self, name: str) -> bool:
        """Whether ``name`` will be runnable inside the sandbox.

        Only the backend can answer this. Checking the *runner's* PATH is the
        wrong question for every isolated backend: the agent CLIs live in the
        sandbox image, so a runner that looked for them locally would refuse
        every job on a correctly configured host.
        """
        return True

    def base_env(self) -> dict[str, str]:
        """The environment a process starts with inside this sandbox.

        The runner must not forward its own ``PATH``/``HOME`` into an isolated
        sandbox: those name paths in the *runner's* filesystem. ``HOME`` is the
        one that bites — an agent CLI writes its config there, and pointing it
        at a directory that does not exist (or is not writable by the sandbox
        user) fails the job for a reason nothing in the logs explains.
        """
        return {}


# ── process: no isolation ──────────────────────────────────────────────


class _PopenProcess(SandboxProcess):
    """Wraps a plain ``subprocess.Popen``.

    stderr is drained on a background thread from the moment the process
    starts. Left unread, a chatty agent fills the pipe buffer and blocks
    forever on its next write — while the runner sits reading a stdout stream
    that will never produce another line.
    """

    def __init__(self, process: subprocess.Popen[str], *, container: str | None = None) -> None:
        self._process = process
        self._container = container
        self._stderr_tail = TextTail()
        self._stderr_thread: threading.Thread | None = None
        if process.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, daemon=True, name="sandbox-stderr"
            )
            self._stderr_thread.start()

    @property
    def container(self) -> str | None:
        """The container name, when this process is a ``docker run`` client."""
        return self._container

    def _drain_stderr(self) -> None:
        """Read stderr to EOF, keeping a bounded tail."""
        assert self._process.stderr is not None
        for chunk in text_chunks(self._process.stderr):
            self._stderr_tail.append(chunk)

    def write_line(self, text: str) -> None:
        """Write one line to the child's stdin, flushed immediately."""
        stdin = self._process.stdin
        if stdin is None:
            raise SandboxError("this sandbox process was not spawned with live input")
        stdin.write(text.rstrip("\n") + "\n")
        stdin.flush()

    def close_stdin(self) -> None:
        """EOF the child's stdin; harmless when there is none."""
        stdin = self._process.stdin
        if stdin is not None:
            with contextlib.suppress(OSError):
                stdin.close()

    def poll(self) -> int | None:
        """Reap-if-exited without blocking."""
        return self._process.poll()

    def lines(self) -> Iterator[str]:
        """Yield stdout lines from the child process."""
        try:
            yield from protocol_lines(self.chunks())
        except SandboxError:
            self.kill()
            raise

    def chunks(self) -> Iterator[str]:
        assert self._process.stdout is not None
        yield from text_chunks(self._process.stdout)

    def kill(self) -> None:
        """Kill the child process."""
        self._process.kill()

    def wait(self) -> int:
        """Wait for the child and return its exit status."""
        self._process.wait()
        return self._process.returncode

    def stderr_text(self) -> str:
        """Return the child's stderr tail, collected by the drain thread."""
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
        return self._stderr_tail.text()


class ProcessBackend(SandboxBackend):
    """Unisolated subprocess backend retained solely as a unit-test seam.

    It cannot advertise the shared-live capability and therefore cannot claim
    a runnable job. Keeping it makes deterministic protocol tests possible
    without leaving an executable legacy deployment topology behind.
    """

    name = "process"

    def __init__(self, *, acknowledged_unsafe: bool = False) -> None:
        self._acknowledged = acknowledged_unsafe

    def preflight(self) -> None:
        """Refuse to run unisolated unless explicitly acknowledged."""
        if not self._acknowledged:
            raise SandboxError(
                "the 'process' sandbox backend provides no isolation: the agent "
                "runs as the runner's own user, which means it can read the "
                "host and clone credentials out of /proc and act as the trusted "
                "runner. Set AGENT_SANDBOX_ALLOW_UNISOLATED=1 to accept that "
                "(only valid single-tenant, where an ephemeral VM is the real "
                "boundary), or use AGENT_SANDBOX_BACKEND=container."
            )

    def has_binary(self, name: str) -> bool:
        """The agent runs on this host, so this host must have the binary."""
        return shutil.which(name) is not None

    def base_env(self) -> dict[str, str]:
        """Inherit the host's essentials — the agent runs on this host."""
        return {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
            if key in os.environ
        }

    def spawn(self, spec: SandboxSpec) -> SandboxProcess:
        """Start the agent directly on this host."""
        return _PopenProcess(
            subprocess.Popen(
                spec.argv,
                cwd=spec.workdir,
                env=spec.env,
                # Same contract as the container backend: a live spawn keeps
                # stdin open for follow-up turns; everything else sees EOF.
                stdin=subprocess.PIPE if spec.live_input else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        )


# ── container: docker / kata ───────────────────────────────────────────


class ContainerBackend(SandboxBackend):
    """Configure and attest retained chat containers, optionally VM-isolated.

    ``runtime=None`` gives an ordinary (shared-kernel) container, which is a
    reasonable boundary for repositories you already trust. ``runtime`` set to
    a VM-backed runtime such as :data:`KATA_RUNTIME` puts a kernel boundary
    around each job, which is what untrusted multi-tenant code needs — the
    agent installs dependencies and runs repository scripts, so a shared kernel
    means one kernel bug reaches every tenant.

    Networking is fixed rather than chosen: every chat group gets its own
    routable bridge, guarded on the host by one rule set (public IPv4 TCP
    80/443, the named resolver, the group's two relays, and nothing else; see
    :mod:`cloud_agent_shared.egress`). ``sandbox_network`` carries this
    deployment's pool, resolver and relays; event reporting stays in the
    trusted runner.
    """

    name = "container"

    def __init__(
        self,
        *,
        image: str,
        runtime: str | None = None,
        docker_binary: str = "docker",
        extra_args: list[str] | None = None,
        sandbox_network: SandboxNetwork | None = None,
        sandbox_namespace: str = "default",
        network_helper_image: str | None = None,
        uid: int = SANDBOX_UID,
        gid: int = SANDBOX_GID,
        home: str = SANDBOX_HOME,
    ) -> None:
        self.image = image
        self.runtime = runtime
        self.docker_binary = docker_binary
        self.extra_args = extra_args or []
        # None only for a backend that never builds a group network: the
        # candidate check, and unit tests of unrelated behaviour.
        self.sandbox_network = sandbox_network
        # The image the host-rule helper runs from. None, which every
        # deployment uses, means this process's own container image; only a
        # process that runs outside a container (a benchmark, the external
        # network test) names one. Deliberately not an environment setting.
        self.network_helper_image = network_helper_image
        self._network_guard: HostNetworkGuard | None = None
        if not sandbox_namespace or "\x00" in sandbox_namespace or len(sandbox_namespace) > 128:
            raise ValueError("sandbox_namespace must be a non-empty value of at most 128 bytes")
        if sandbox_network is not None and sandbox_network.namespace != sandbox_namespace:
            raise ValueError("sandbox_network belongs to a different sandbox namespace")
        # A Docker daemon may host sibling staging/production or local checkout
        # stacks. Their control-plane databases are independent, so lifecycle
        # discovery must never adopt another stack's retained containers.
        self.sandbox_namespace = sandbox_namespace
        self.uid = uid
        self.gid = gid
        self.home = home
        # Filled by the first version_stamp() call. The image is pinned for
        # this backend's lifetime, so one inspect answers for every heartbeat.
        self._version_stamp: dict[str, Any] | None = None
        # Filled by preflight from a real container under the exact runtime
        # passed to jobs. Kept separate so an early image stamp cannot freeze
        # out the later kernel observation.
        self._kernel_probe: dict[str, str | None] = {
            "host_kernel": os.uname().release,
            "guest_kernel": None,
        }

    @property
    def provides_isolation(self) -> bool:
        """The retained container, not the Agent CLI process, is the boundary."""
        return True

    def sandbox_metadata(self) -> dict[str, str]:
        """Describe the concrete container image and optional VM runtime."""
        metadata = {
            "sandbox_backend": self.name,
            "sandbox_image": self.image,
        }
        if self.runtime:
            metadata["sandbox_runtime"] = self.runtime
        return metadata

    def version_stamp(self) -> dict[str, Any]:
        """Report runtime, image, CLI versions and the preflight kernel probe.

        ``sandbox_runtime`` is always present and ``""`` is a positive
        statement — a container backend with no VM runtime is runc on the
        shared kernel. ``None``/absent would read as "not reported", and the
        difference between "attested runc" and "silent" is exactly what the
        control plane needs to stop trusting documentation about isolation.

        Digest and versions degrade to empty on inspect failure rather than
        failing the heartbeat: liveness reporting must not come to depend on
        the local image store answering.
        """
        if self._version_stamp is None:
            self._version_stamp = self._collect_version_stamp()
        return {**self._version_stamp, **self._kernel_probe}

    def _collect_version_stamp(self) -> dict[str, Any]:
        stamp: dict[str, Any] = {
            "sandbox_runtime": self.runtime or "",
            "sandbox_image_digest": "",
            "runtime_versions": {},
            "runtime_versions_available": {},
            "helper_versions": {},
        }
        try:
            proc = subprocess.run(
                [self.docker_binary, "image", "inspect", self.image, "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return stamp
        if proc.returncode != 0:
            return stamp
        try:
            info = json.loads(proc.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return stamp
        if not isinstance(info, dict):
            return stamp
        # RepoDigests names the registry content address — stable across hosts
        # pulling the same image. A locally built, never-pushed image has none;
        # its local Id still pins *what* ran, just not where it came from.
        digests = info.get("RepoDigests")
        if isinstance(digests, list) and digests and isinstance(digests[0], str):
            stamp["sandbox_image_digest"] = digests[0]
        elif isinstance(info.get("Id"), str):
            stamp["sandbox_image_digest"] = info["Id"]
        labels = (info.get("Config") or {}).get("Labels") or {}
        if isinstance(labels, dict):
            versions = _labelled(labels, RUNTIME_VERSION_LABEL_SUFFIX)
            menus = _labelled(labels, RUNTIME_VERSION_MENU_LABEL_SUFFIX)
            stamp["runtime_versions"] = versions
            # An image built before menus existed labels only its default, and
            # a menu of one is exactly what that image offers — degrading to
            # an *empty* menu would instead read as "this host can run nothing"
            # and make every stored selection unservable.
            stamp["runtime_versions_available"] = {
                runtime_id: _split_menu(menus.get(runtime_id), default)
                for runtime_id, default in versions.items()
            }
            stamp["helper_versions"] = _helper_versions(labels)
        return stamp

    @property
    def is_vm_isolated(self) -> bool:
        """Whether each job gets its own kernel."""
        return self.runtime is not None

    def require_sandbox_network(self) -> SandboxNetwork:
        """This deployment's network, or a refusal naming what is missing."""
        if self.sandbox_network is None:
            raise SandboxError(
                "no sandbox network is configured for this backend; set "
                "AGENT_EGRESS_CHAT_NETWORK_POOL, AGENT_EGRESS_DNS_SERVER and "
                "AGENT_RELAY_NETWORK"
            )
        return self.sandbox_network

    def network_guard(self) -> HostNetworkGuard:
        """The one guard of this process, shared by every manager it builds."""
        if self._network_guard is None:
            from cloud_agent_host.network_guard import HostNetworkGuard

            self._network_guard = HostNetworkGuard(
                self.require_sandbox_network(),
                docker_binary=self.docker_binary,
                helper_image=self.network_helper_image,
            )
        return self._network_guard

    def base_env(self) -> dict[str, str]:
        """The image's own environment, not the runner's.

        ``HOME`` must name a directory that exists in the *image* and is
        writable by the sandbox user: every agent CLI writes state there, and
        inheriting the runner's ``HOME=/root`` makes the CLI fail on startup
        with an error about a path the operator has never heard of.
        """
        return {"HOME": self.home, "PATH": SANDBOX_PATH, "LANG": "C.UTF-8"}

    def preflight(self, *, check_network: bool = True) -> None:
        """Verify the container runtime is usable before accepting jobs.

        Retained chat workspaces live in Docker's writable layer, so this
        preflight deliberately has no host-path or bind-mount probe.
        ``check_network=False`` validates only the runtime: candidate checks
        have no deployment network, and host qualification leaves mutable
        host rules for each child to verify before accepting its turn.
        """
        if self.runtime == KATA_RUNTIME:
            from cloud_agent_host.runtime_qualification import check_runtime_state

            check_runtime_state()
        try:
            result = subprocess.run(
                [self.docker_binary, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"{self.docker_binary} is not usable: {exc}") from exc
        if result.returncode != 0:
            raise SandboxError(f"{self.docker_binary} is not usable: {result.stderr.strip()[:200]}")
        if self.runtime == KATA_RUNTIME:
            # Import here: retained operations depend on these base primitives.
            from cloud_agent_host.chat_sandbox import ChatSandboxManager

            # The observer probe already boots a fresh container with the
            # production runtime and image. Observe its kernel too, instead
            # of booting another microVM just to run uname.
            self._kernel_probe = {"host_kernel": os.uname().release, "guest_kernel": None}
            guest_kernel = ChatSandboxManager(self).probe_guest_observer()
            self._kernel_probe["guest_kernel"] = self.validate_guest_kernel(guest_kernel)
        else:
            self._probe_kernel_boundary()
        if check_network:
            self._check_sandbox_network()
        if not self.is_vm_isolated:
            logger.warning(
                "agent_sandbox_shared_kernel",
                extra={
                    "event": "agent_sandbox_shared_kernel",
                    "detail": (
                        "container backend without a VM-isolated runtime: fine for "
                        "trusted repositories, not for untrusted multi-tenant code"
                    ),
                },
            )

    def _probe_kernel_boundary(self) -> None:
        """Observe the kernel supplied by the configured container runtime."""
        self._kernel_probe = {"host_kernel": os.uname().release, "guest_kernel": None}
        # Docker's implicit default and explicit runc are the same trusted-only
        # shared-kernel boundary, not a failed VM probe.
        if not self.runtime or self.runtime == "runc":
            return
        argv = [
            self.docker_binary,
            "run",
            "--rm",
            "--network",
            "none",
            "--runtime",
            self.runtime,
            *self.extra_args,
            "--entrypoint",
            "/bin/sh",
            self.image,
            "-c",
            "uname -r",
        ]
        try:
            probe = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"could not probe sandbox runtime {self.runtime!r}: {exc}") from exc
        if probe.returncode != 0:
            raise SandboxError(
                f"could not start sandbox image {self.image!r} under runtime "
                f"{self.runtime!r}: {probe.stderr.strip()[:200]}"
            )
        lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        if not lines:
            raise SandboxError(
                f"sandbox runtime {self.runtime!r} returned no guest kernel from uname -r"
            )
        self._kernel_probe["guest_kernel"] = self.validate_guest_kernel(lines[-1])

    def validate_guest_kernel(self, guest_kernel: object) -> str | None:
        """Accept observed kernel evidence, never a missing or shared Kata kernel."""
        if not isinstance(guest_kernel, str) or not guest_kernel.strip():
            raise SandboxError(
                f"sandbox runtime {self.runtime!r} returned no guest kernel from uname -r"
            )
        guest_kernel = guest_kernel.strip()
        host_kernel = os.uname().release
        if guest_kernel == host_kernel:
            if self.runtime == KATA_RUNTIME:
                raise SandboxError(
                    f"sandbox runtime {self.runtime!r} reported the host kernel "
                    f"{host_kernel!r}; refusing to claim VM isolation"
                )
            return None
        return guest_kernel

    def _kata_relay_host_args(self, network: str) -> list[str]:
        """Give disposable Kata probes the same fixed relay resolution as chats."""
        if self.runtime != KATA_RUNTIME:
            return []
        from cloud_agent_host.chat_sandbox import ChatSandboxManager

        addresses = ChatSandboxManager(self).relay_addresses(network)
        return [
            part
            for alias, address in sorted(addresses.items())
            for part in ("--add-host", f"{alias}={address}")
        ]

    def check_network(self) -> None:
        """Revalidate mutable network state even when runtime evidence is inherited."""
        self._check_sandbox_network()

    def _check_sandbox_network(self) -> None:
        """Refuse to accept work until this host enforces the sandbox network.

        Two things are checked. The relay discovery network must be
        ``internal``: it is where the two relays are found, never a route a
        sandbox could use, and nothing but the relays belong on it. Then the
        guard installs this deployment's rules and reads them back from the
        kernel, so a host that cannot enforce the boundary never claims.
        """
        network = self.require_sandbox_network()
        self._require_gateway_priority()
        probe = subprocess.run(
            [
                self.docker_binary,
                "network",
                "inspect",
                network.relay_network,
                "--format",
                "{{.Internal}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode != 0:
            raise SandboxError(
                f"the relay network {network.relay_network!r} does not exist: "
                f"{probe.stderr.strip()[:200]}. No group could reach its relays."
            )
        if probe.stdout.strip().lower() != "true":
            raise SandboxError(
                f"the relay network {network.relay_network!r} is not `internal`. It is "
                "where the two relays are discovered, and must not route anywhere."
            )
        self.network_guard().ensure(max_age_s=0)

    def _require_gateway_priority(self) -> None:
        """Refuse a Docker too old to keep group bridges off the relays' routes."""
        probe = subprocess.run(
            [
                self.docker_binary,
                "version",
                "--format",
                "{{.Client.Version}} {{.Server.APIVersion}}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        client, _, api = probe.stdout.strip().partition(" ")
        try:
            client_major = int(client.split(".")[0])
            server_api = tuple(int(part) for part in api.split(".")[:2])
        except ValueError:
            client_major, server_api = 0, (0, 0)
        if (
            probe.returncode != 0
            or client_major < DOCKER_MIN_CLIENT_MAJOR
            or server_api < DOCKER_MIN_API
        ):
            raise SandboxError(
                "the sandbox network needs Docker 28 or newer, client and daemon: relays "
                "join each group bridge with a gateway priority, so that bridge never "
                f"becomes their default route (client {client or 'unknown'}, daemon API "
                f"{api or 'unknown'})"
            )

    def check_gateway_reachable(self, base_url: str) -> None:
        """Fail startup if the sandbox cannot reach the gateway it will be given.

        A sandbox reaches inference only through the fixed relay, which is
        discovered on the relay network. This method proves that relay answers
        there. If an operator points
        AGENT_GATEWAY_URL at an unreachable remote address, every job dies at
        its first model call and the error reads like a broken model rather
        than a network that was never going to work.

        Probed rather than inferred: one container on the relay network, making
        a real request. A heuristic on the URL would be wrong for every
        deployment that does route out.

        The request matters, not just the name. This used to resolve DNS and
        stop there, which passes for anything with a DNS record — including a
        relay container that answers to the name and cannot reach the gateway
        behind it. That is precisely the shape a cross-machine deployment has,
        so the one topology needing the check hardest was the one it could not
        see. Any HTTP response counts, 404 and 401 included: the question is
        whether packets get there, not what the gateway thinks of them.
        """
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        host = (parsed.hostname or "").strip()
        if not host:
            return
        network = self.require_sandbox_network().relay_network
        probe_url = f"{base_url.rstrip('/').removesuffix('/v1')}/health"
        argv = [self.docker_binary, "run", "--rm", "--network", network]
        # Probe under the runtime jobs will actually use. Otherwise a Kata
        # deployment could pass a runc-based probe while every real sandbox
        # still fails at spawn.
        if self.runtime:
            argv += ["--runtime", self.runtime]
        argv += self.extra_args
        argv += self._kata_relay_host_args(network)
        argv += [
            "--entrypoint",
            "/bin/sh",
            self.image,
            "-c",
            _GATEWAY_PROBE_SH,
            "sh",
            probe_url,
            host,
        ]
        probe = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        verdict = (probe.stdout or "").strip().splitlines()
        outcome = verdict[-1] if verdict else ""
        if outcome == "ok":
            return
        if outcome == "dns-only":
            # A custom sandbox image with neither curl nor python3. Resolving
            # is all that can be asked of it; say so rather than let the log
            # imply the stronger check ran.
            logger.warning(
                "agent_sandbox_gateway_probe_degraded",
                extra={
                    "event": "agent_sandbox_gateway_probe_degraded",
                    "detail": (
                        f"{self.image!r} has neither curl nor python3, so the gateway "
                        f"check for {probe_url!r} confirmed DNS only; a relay that "
                        "resolves but cannot forward would still pass"
                    ),
                },
            )
            return
        detail = {
            "unresolved": (
                f"cannot resolve {host!r}. The inference relay has to be on the relay "
                "network under that name; a gateway on another machine is reached "
                "through its tunnel container, not directly."
            ),
        }.get(
            outcome,
            (
                f"resolves {host!r} but no HTTP response came back from {probe_url!r}. "
                "Something answers to the name and cannot reach the gateway behind it — "
                "a relay whose upstream is down, or a port nothing is listening on."
            ),
        )
        raise SandboxError(
            f"the relay network {network!r} {detail} Every job would fail at its first model call."
        )

    def spawn(self, spec: SandboxSpec) -> SandboxProcess:
        """Reject the removed one-container-per-turn execution topology."""
        raise SandboxError(
            "direct container execution was removed; commands must run through "
            "a RetainedChatBackend inside the chat group's existing container"
        )

    def spawn_terminal(self, spec: SandboxSpec, *, rows: int, cols: int) -> TerminalProcess:
        """Reject the removed one-container-per-terminal topology."""
        raise SandboxError(
            "direct container terminals were removed; terminals must run through "
            "a RetainedChatBackend inside the chat group's existing container"
        )


# ── selection ──────────────────────────────────────────────────────────


def build_backend_from_env(
    env: dict[str, str] | None = None, *, with_network: bool = True
) -> SandboxBackend:
    """Construct the configured backend.

    ``AGENT_SANDBOX_BACKEND`` is ``kata`` by default, ``container`` is the
    explicit trusted-repository shared-kernel downgrade, and ``process`` is a
    non-claimable test seam. The remaining
    variables tune the container backends: ``AGENT_SANDBOX_IMAGE``,
    ``AGENT_SANDBOX_RUNTIME`` (overrides the Kata shim name),
    ``AGENT_SANDBOX_DOCKER``, ``AGENT_SANDBOX_EXTRA_ARGS`` and
    ``AGENT_SANDBOX_UID``/``AGENT_SANDBOX_GID``/``AGENT_SANDBOX_HOME`` for a
    custom sandbox image whose user differs from the one we ship. The network
    comes from :func:`cloud_agent_shared.egress.network_from_env`; only the
    candidate check, which runs no group, passes ``with_network=False``.
    """
    source = env if env is not None else dict(os.environ)
    choice = (source.get("AGENT_SANDBOX_BACKEND") or "kata").strip().lower()

    if choice == "process":
        return ProcessBackend(
            acknowledged_unsafe=source.get("AGENT_SANDBOX_ALLOW_UNISOLATED", "") not in ("", "0")
        )
    if choice in ("container", "docker", "kata"):
        runtime = source.get("AGENT_SANDBOX_RUNTIME") or (
            KATA_RUNTIME if choice == "kata" else None
        )
        image = (source.get("AGENT_SANDBOX_IMAGE") or "").strip()
        if not image:
            raise ValueError(
                "AGENT_SANDBOX_BACKEND=" + choice + " needs AGENT_SANDBOX_IMAGE. "
                "There is no default: an unqualified name would be resolved "
                "through Docker Hub, and this image is what an untrusted agent "
                "runs inside."
            )
        namespace = source.get("AGENT_SANDBOX_NAMESPACE") or "default"
        return ContainerBackend(
            image=image,
            runtime=runtime,
            docker_binary=source.get("AGENT_SANDBOX_DOCKER") or "docker",
            extra_args=shlex.split(source.get("AGENT_SANDBOX_EXTRA_ARGS") or ""),
            # No fallback: a missing namespace, pool, resolver or relay
            # network refuses here rather than building a group network, or
            # naming a host table, nobody specified.
            sandbox_network=network_from_env(source) if with_network else None,
            sandbox_namespace=namespace,
            uid=int(source.get("AGENT_SANDBOX_UID") or SANDBOX_UID),
            gid=int(source.get("AGENT_SANDBOX_GID") or SANDBOX_GID),
            home=source.get("AGENT_SANDBOX_HOME") or SANDBOX_HOME,
        )
    raise SandboxError(
        f"unknown AGENT_SANDBOX_BACKEND {choice!r}; expected process, container or kata"
    )
