"""Sandbox primitives used to operate a retained chat container.

The runner is deliberately substrate-agnostic: it claims over HTTP, executes,
and reports over HTTP. *How* the execution is isolated is this module's job,
selected by configuration, so the same runner code serves dogfooding on CI,
self-hosted machines, and a multi-tenant fleet.

The production topology is runner outside, retained container per chat group
(``container`` / ``kata``). Only scoped inference/MCP credentials cross into
the sandbox; control-plane and repository credentials remain outside. The
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

from cloud_agent_host.egress import (
    PHASES,
    EgressPolicy,
    EgressPolicyError,
    EgressTier,
    build_policy_from_env,
)
from cloud_agent_host.egress_proxy import CANARY_HOST
from cloud_agent_host.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = get_logger(__name__)

# Run inside one ephemeral preflight probe on the agent phase's own network,
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


class SandboxError(Exception):
    """Raised when a sandbox cannot be created or configured."""


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
    # Which lifecycle phase this spawn belongs to. The two get different egress
    # tiers: setup installs dependencies and needs a registry, the agent turn
    # afterwards does not — and the agent turn is the one running untrusted
    # model output, so it is the one that gets the least reach.
    phase: str = "agent"


class SandboxProcess(ABC):
    """A running agent, exposing a line stream and a kill switch."""

    @abstractmethod
    def lines(self) -> Iterator[str]:
        """Yield the agent's stdout, line by line, as it is produced."""

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
        self._stderr_chunks: list[str] = []
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
        for line in self._process.stderr:
            self._stderr_chunks.append(line)
            # Bounded: a runaway agent must not turn its own noise into an
            # out-of-memory on the runner.
            del self._stderr_chunks[:-200]

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
        assert self._process.stdout is not None
        yield from self._process.stdout

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
        return "".join(self._stderr_chunks)


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

    Networking is a policy input rather than a default: the sandbox must reach
    the fixed inference and MCP relays and, during setup, package registries —
    but nothing else. ``network`` names the docker network that encodes that
    policy for this deployment; event reporting stays in the trusted runner.
    """

    name = "container"

    def __init__(
        self,
        *,
        image: str,
        runtime: str | None = None,
        network: str = "bridge",
        docker_binary: str = "docker",
        extra_args: list[str] | None = None,
        allow_open_network: bool = False,
        egress: EgressPolicy | None = None,
        chat_network_pool: str = "10.240.0.0/16",
        sandbox_namespace: str = "default",
        uid: int = SANDBOX_UID,
        gid: int = SANDBOX_GID,
        home: str = SANDBOX_HOME,
    ) -> None:
        self.image = image
        self.runtime = runtime
        self.network = network
        self.docker_binary = docker_binary
        self.extra_args = extra_args or []
        self.allow_open_network = allow_open_network
        self.egress = egress
        # Per-chat internal bridges must not consume Docker's tiny default
        # address pools (commonly only 31 networks). ChatSandboxManager splits
        # this operator-overridable RFC1918 pool into /29 phase networks.
        self.chat_network_pool = chat_network_pool
        if not sandbox_namespace or "\x00" in sandbox_namespace or len(sandbox_namespace) > 128:
            raise ValueError("sandbox_namespace must be a non-empty value of at most 128 bytes")
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
            stamp["shared_sandbox_protocol"] = str(
                labels.get("org.freeinference.shared-sandbox.protocol") or ""
            )
        return stamp

    @property
    def is_vm_isolated(self) -> bool:
        """Whether each job gets its own kernel."""
        return self.runtime is not None

    def network_for_phase(self, phase: str) -> str:
        """Resolve the docker network this phase runs on.

        With no egress policy configured this is the single network the backend
        was built with, so an existing deployment behaves exactly as before.
        """
        if self.egress is None:
            return self.network
        try:
            return self.egress.network_for(phase)
        except EgressPolicyError:
            # A phase with no network configured for its tier must not silently
            # fall back to a more open one.
            raise

    def base_env(self) -> dict[str, str]:
        """The image's own environment, not the runner's.

        ``HOME`` must name a directory that exists in the *image* and is
        writable by the sandbox user: every agent CLI writes state there, and
        inheriting the runner's ``HOME=/root`` makes the CLI fail on startup
        with an error about a path the operator has never heard of.
        """
        return {"HOME": self.home, "PATH": SANDBOX_PATH, "LANG": "C.UTF-8"}

    def preflight(self) -> None:
        """Verify the container runtime is usable before accepting jobs.

        Retained chat workspaces live in Docker's writable layer, so this
        preflight deliberately has no host-path or bind-mount probe.
        """
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
        self._probe_kernel_boundary()
        if self.runtime == KATA_RUNTIME:
            # Import here: retained operations depend on these base primitives.
            from cloud_agent_host.chat_sandbox import ChatSandboxManager

            ChatSandboxManager(self).probe_guest_observer()
        self._check_networks_are_closed()
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
        guest_kernel = lines[-1]
        host_kernel = str(self._kernel_probe["host_kernel"])
        if guest_kernel == host_kernel:
            if self.runtime == KATA_RUNTIME:
                raise SandboxError(
                    f"sandbox runtime {self.runtime!r} reported the host kernel "
                    f"{host_kernel!r}; refusing to claim VM isolation"
                )
            return
        self._kernel_probe["guest_kernel"] = guest_kernel

    def _kata_service_host_args(self, phase: str, network: str) -> list[str]:
        """Give disposable Kata probes the same fixed relay resolution as chats."""
        if self.runtime != KATA_RUNTIME:
            return []
        from cloud_agent_host.chat_sandbox import ChatSandboxManager

        addresses = ChatSandboxManager(self).fixed_service_addresses(phase, network)
        return [
            part
            for alias, address in sorted(addresses.items())
            for part in ("--add-host", f"{alias}={address}")
        ]

    def _check_networks_are_closed(self) -> None:
        """Check every network a phase can run on, not just the default one.

        A per-phase policy means there is no single network to validate: a
        deployment can be closed for the agent turn and open for setup, and
        only the tier names say which is which.
        """
        if self.egress is None:
            self._check_one_network(self.network)
            return
        probed: set[EgressTier] = set()
        for phase in PHASES:
            tier = self.egress.tier_for(phase)
            try:
                network = self.egress.network_for(phase)
            except EgressPolicyError:
                # A phase whose tier names no network cannot run. That is only
                # fatal for a phase this runner actually spawns: a job with no
                # setup script never enters the setup phase, and refusing to
                # start over a capability this deployment does not use would be
                # a startup failure with no cause an operator could act on. It
                # still fails loudly at spawn, with this same error.
                if phase == "agent":
                    raise
                continue
            # Trusted runs on a network with no route of its own, exactly like
            # the closed tier: the *proxy* has the second leg, not the network.
            # This used to exempt it as "allowlist-fronted by construction",
            # which was an assumption about a network nobody checked — pointing
            # AGENT_EGRESS_NETWORK_TRUSTED at the default bridge passed
            # preflight in silence and gave the setup phase the whole internet.
            #
            # Custom stays exempt, and deliberately: it exists for a deployment
            # whose own gateway filters transparently, which needs a routable
            # network. Demanding `internal` there would force the operator to
            # set AGENT_SANDBOX_ALLOW_OPEN_NETWORK, which silences this check
            # for *every* tier including the closed one. Full is exempt because
            # it is already gated behind that same acknowledgement.
            if tier in (EgressTier.PLATFORM_ONLY, EgressTier.TRUSTED):
                self._check_one_network(network)
            # Probed only for the tier whose proxy we render the config for. A
            # custom proxy refuses in whatever way its operator chose — a drop
            # rather than a 403, say — and reading that as "not enforcing"
            # would refuse to start a deployment that is working correctly.
            if tier is EgressTier.TRUSTED and tier not in probed:
                probed.add(tier)
                self._check_proxy_denies_unlisted(phase=phase, network=network)

    def _check_proxy_denies_unlisted(self, *, phase: str, network: str) -> None:
        """Refuse to start if the tier's proxy is not actually enforcing.

        A proxy that answers but allows everything is the worst outcome
        available here: the tier reads as "allowlisted" in every log and
        config, the network is internal exactly as designed, and the sandbox
        still reaches the whole internet. Nothing downstream would notice,
        which is the same shape as the Kata runtime silently degrading to a
        shared kernel — so it is checked the same way, by observing the
        behaviour rather than trusting the configuration.

        The probe asks for a host that cannot be allowlisted and cannot exist
        (RFC 2606 ``.invalid``), so it never leaves the host even when it
        passes, and it distinguishes the three outcomes that matter: refused by
        the proxy, unable to reach the proxy at all, or served.

        **Both paths are probed, and the CONNECT one is why.** A cleartext
        request alone proves very little here: this deployment's own config
        denies plain HTTP outright, so a proxy that refused cleartext while
        tunnelling CONNECT to anywhere would answer 403 and pass — with every
        HTTPS destination in the world still reachable, which is the whole of
        what a package manager (or an exfiltration attempt) actually uses.
        ``%{http_connect}`` reports the proxy's answer to the CONNECT itself,
        as distinct from whatever happens inside the tunnel.
        """
        env = self.egress.sandbox_env(phase) if self.egress else {}
        argv = [self.docker_binary, "run", "--rm", "--network", network]
        # Under the runtime jobs will actually use. The bind probe learned this
        # the hard way: probing under the daemon's default while the shipped
        # backend is kata validates a configuration nobody runs.
        if self.runtime:
            argv += ["--runtime", self.runtime]
        argv += self.extra_args
        argv += self._kata_service_host_args(phase, network)
        for key, value in sorted(env.items()):
            argv += ["--env", f"{key}={value}"]
        argv += [
            "--entrypoint",
            "/bin/sh",
            self.image,
            "-c",
            # curl reads the proxy out of the environment above, which is the
            # exact mechanism a setup script will use — a `--proxy` flag here
            # would test a path no real job takes. Both stderrs are left alone
            # on purpose: "Failed to connect to ... port 3128" is the
            # diagnostic the unreachable branch below reports.
            f"plain=$(curl -sS -o /dev/null -w '%{{http_code}}' --max-time 10 "
            f"http://{CANARY_HOST}/ || true); "
            f"tunnel=$(curl -sS -o /dev/null -w '%{{http_connect}}' --max-time 10 "
            f"https://{CANARY_HOST}/ || true); "
            'echo "$plain $tunnel"',
        ]
        try:
            # Matches the bind probe's budget: on a host that has not pulled
            # the sandbox image yet, this is the call that pulls it.
            probe = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(
                f"the egress proxy for the {phase} phase could not be probed: {exc}"
            ) from exc
        answered = probe.stdout.strip().splitlines()[-1].split() if probe.stdout.strip() else []
        plain, tunnel = [*answered, "", ""][:2]
        # `0` is what %{http_connect} reports when no CONNECT was ever answered.
        no_answer = {"", "0", "000"}
        if plain == "403" and tunnel == "403":
            return
        if plain in no_answer and tunnel in no_answer:
            # Fatal, even though an unreachable proxy leaves the sandbox *more*
            # closed rather than less: it means this deployment's setup tier is
            # broken, and one startup failure is how an operator learns that
            # instead of one confusing job failure per user. The way out is
            # named, because otherwise it holds up jobs that never install
            # anything.
            raise SandboxError(
                f"the {phase} phase's egress proxy is unreachable from network {network!r}. "
                "Every dependency install would fail with a connection error. Check that "
                "the proxy service is running and on that network "
                f"(docker: {probe.stderr.strip()[:200]}). If this deployment does not need "
                "dependency installation, set AGENT_EGRESS_SETUP_TIER=platform_only."
            )
        refused = "CONNECT" if tunnel == "403" else "cleartext"
        raise SandboxError(
            f"the {phase} phase's egress proxy answered {plain!r} (cleartext) and "
            f"{tunnel!r} (CONNECT) for {CANARY_HOST}, which is on no allowlist and does "
            f"not exist — so it refuses {refused} and lets the other through. The sandbox "
            "can reach the internet while the configuration says otherwise. Check the "
            "proxy's generated configuration."
        )

    def _check_one_network(self, network: str) -> None:
        """Refuse to start unless the sandbox network denies egress by default.

        The design puts egress control at the network layer and says
        degradation must fail closed. An unset or open network is therefore a
        startup failure, not a warning: the agent runs untrusted repository
        code, and "nobody configured this" must not be the same thing as "full
        internet".
        """
        # Docker's built-in null network has Internal=false because it has no
        # bridge at all. It is stricter than an internal bridge: no interfaces
        # except loopback. Candidate preflight deliberately uses this network.
        if network == "none":
            return
        if self.allow_open_network:
            logger.warning(
                "agent_sandbox_open_network",
                extra={
                    "event": "agent_sandbox_open_network",
                    "detail": (
                        f"sandbox network {network!r} is not internal and the "
                        "operator has accepted that; the agent can reach the internet"
                    ),
                },
            )
            return
        if not network:
            raise SandboxError(
                "AGENT_SANDBOX_NETWORK is not set. There is deliberately no default: "
                "an unset value used to mean the Docker bridge, i.e. unrestricted "
                "outbound internet for untrusted repository code. Point it at an "
                "internal network (the compose overlay declares `agent-egress`), or "
                "set AGENT_SANDBOX_ALLOW_OPEN_NETWORK=1 to accept open egress."
            )
        probe = subprocess.run(
            [self.docker_binary, "network", "inspect", network, "--format", "{{.Internal}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode != 0:
            raise SandboxError(
                f"the sandbox network {network!r} does not exist: "
                f"{probe.stderr.strip()[:200]}. Every job would fail at spawn."
            )
        if probe.stdout.strip().lower() != "true":
            raise SandboxError(
                f"the sandbox network {network!r} is not `internal`, so the agent "
                "would have unrestricted egress. Declare it `internal: true` (as the "
                "compose overlay does), front it with an allowlist proxy, or set "
                "AGENT_SANDBOX_ALLOW_OPEN_NETWORK=1 to accept that deliberately."
            )

    def check_gateway_reachable(self, base_url: str) -> None:
        """Fail startup if the sandbox cannot reach the gateway it will be given.

        `platform_only` means only the platform's fixed inference and MCP
        relays. This method proves the inference half and therefore assumes the
        gateway relay is *on that network*. If an operator points
        AGENT_GATEWAY_URL at an unreachable remote address, every job dies at
        its first model call and the error reads like a broken model rather
        than a network that was never going to work.

        Probed rather than inferred: one container on the phase's real network,
        making a real request. A heuristic on the URL would be wrong for every
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
        network = self.network_for_phase("agent")
        probe_url = f"{base_url.rstrip('/').removesuffix('/v1')}/health"
        argv = [self.docker_binary, "run", "--rm", "--network", network]
        # Probe under the runtime jobs will actually use. Otherwise a Kata
        # deployment could pass a runc-based probe while every real sandbox
        # still fails at spawn.
        if self.runtime:
            argv += ["--runtime", self.runtime]
        argv += self.extra_args
        argv += self._kata_service_host_args("agent", network)
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
                f"cannot resolve {host!r}. A closed `platform_only` network only works "
                "when the fixed inference relay is on it; for a gateway on another "
                "machine, put its fixed relay on this network or give "
                "the agent phase a network that routes there (AGENT_EGRESS_AGENT_TIER="
                "custom with AGENT_EGRESS_NETWORK_CUSTOM)."
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
            f"the sandbox network {network!r} {detail} Every job would fail at its "
            "first model call."
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


def build_backend_from_env(env: dict[str, str] | None = None) -> SandboxBackend:
    """Construct the configured backend.

    ``AGENT_SANDBOX_BACKEND`` is ``kata`` by default, ``container`` is the
    explicit trusted-repository shared-kernel downgrade, and ``process`` is a
    non-claimable test seam. The remaining
    variables tune the container backends: ``AGENT_SANDBOX_IMAGE``,
    ``AGENT_SANDBOX_NETWORK``, ``AGENT_SANDBOX_RUNTIME`` (overrides the Kata
    shim name), ``AGENT_SANDBOX_DOCKER``, ``AGENT_SANDBOX_EXTRA_ARGS`` and
    ``AGENT_SANDBOX_UID``/``AGENT_SANDBOX_GID``/``AGENT_SANDBOX_HOME`` for a
    custom sandbox image whose user differs from the one we ship.
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
        return ContainerBackend(
            image=image,
            runtime=runtime,
            # No fallback. `bridge` meant an unset variable gave the agent full
            # outbound internet — a missing setting opening the boundary rather
            # than closing it, which is the opposite of the fail-closed rule the
            # process backend already follows. Preflight refuses if it is unset.
            network=source.get("AGENT_SANDBOX_NETWORK") or "",
            docker_binary=source.get("AGENT_SANDBOX_DOCKER") or "docker",
            extra_args=shlex.split(source.get("AGENT_SANDBOX_EXTRA_ARGS") or ""),
            allow_open_network=source.get("AGENT_SANDBOX_ALLOW_OPEN_NETWORK", "") not in ("", "0"),
            egress=build_policy_from_env(source),
            chat_network_pool=source.get("AGENT_EGRESS_CHAT_NETWORK_POOL") or "10.240.0.0/16",
            sandbox_namespace=source.get("AGENT_SANDBOX_NAMESPACE") or "default",
            uid=int(source.get("AGENT_SANDBOX_UID") or SANDBOX_UID),
            gid=int(source.get("AGENT_SANDBOX_GID") or SANDBOX_GID),
            home=source.get("AGENT_SANDBOX_HOME") or SANDBOX_HOME,
        )
    raise SandboxError(
        f"unknown AGENT_SANDBOX_BACKEND {choice!r}; expected process, container or kata"
    )
