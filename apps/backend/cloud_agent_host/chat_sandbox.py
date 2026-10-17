"""Docker-owned, restartable sandboxes for agent chats.

This module is the storage/lifecycle boundary for retained execution. A chat
sandbox has the following invariants:

* its name and ownership labels are stable for the lifetime of a chat;
* it has no bind or volume carrying workspace/session state;
* ``docker stop`` deactivates it without deleting its writable layer; and
* commands run with ``docker exec`` after an explicit, inspected start.

The control-plane and runner integration is intentionally layered on top of
this class.  Keeping Docker identity checks here means every caller gets the
same fencing and no-mount guarantees instead of rebuilding security-sensitive
argv in several services. Container identity, labels and lifecycle value objects
live in :mod:`cloud_agent_host.chat_sandbox_models`.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import selectors
import shlex
import signal
import struct
import subprocess
import tarfile
import tempfile
import termios
import threading
import time
import tty
from typing import TYPE_CHECKING, Any

from cloud_agent_host.bounded_output import OutputPump, TextTail, protocol_lines, text_chunks
from cloud_agent_host.chat_sandbox_models import (
    CHAT_CHECKPOINT_LABEL as CHAT_CHECKPOINT_LABEL,
    CHAT_CONTAINER_PREFIX as CHAT_CONTAINER_PREFIX,
    CHAT_CPU_LIMIT_LABEL as CHAT_CPU_LIMIT_LABEL,
    CHAT_DISK_LIMIT_LABEL as CHAT_DISK_LIMIT_LABEL,
    CHAT_ENVIRONMENT_SNAPSHOT_LABEL as CHAT_ENVIRONMENT_SNAPSHOT_LABEL,
    CHAT_GENERATION_LABEL as CHAT_GENERATION_LABEL,
    CHAT_IMAGE_DIGEST_LABEL as CHAT_IMAGE_DIGEST_LABEL,
    CHAT_MEMORY_LIMIT_LABEL as CHAT_MEMORY_LIMIT_LABEL,
    CHAT_NAMESPACE_LABEL as CHAT_NAMESPACE_LABEL,
    CHAT_NETWORK_GENERATION_LABEL as CHAT_NETWORK_GENERATION_LABEL,
    CHAT_NETWORK_LABEL as CHAT_NETWORK_LABEL,
    CHAT_NETWORK_POOL_LABEL as CHAT_NETWORK_POOL_LABEL,
    CHAT_NETWORK_SANDBOX_ID_LABEL as CHAT_NETWORK_SANDBOX_ID_LABEL,
    CHAT_NETWORK_SERVICES_LABEL as CHAT_NETWORK_SERVICES_LABEL,
    CHAT_PROBE_LABEL as CHAT_PROBE_LABEL,
    CHAT_RESIDENT_CAPACITY_LABEL as CHAT_RESIDENT_CAPACITY_LABEL,
    CHAT_RUNTIME_LABEL as CHAT_RUNTIME_LABEL,
    CHAT_RUNTIME_VERSION_LABEL as CHAT_RUNTIME_VERSION_LABEL,
    CHAT_SANDBOX_ID_LABEL as CHAT_SANDBOX_ID_LABEL,
    CHAT_SANDBOX_LABEL as CHAT_SANDBOX_LABEL,
    CHAT_SHARED_LABEL as CHAT_SHARED_LABEL,
    CHAT_STOP_TIMEOUT_LABEL as CHAT_STOP_TIMEOUT_LABEL,
    CHAT_THREAD_LABEL as CHAT_THREAD_LABEL,
    ENVIRONMENT_TEST_LABEL as ENVIRONMENT_TEST_LABEL,
    MEMBER_UID_BASE as MEMBER_UID_BASE,
    MEMBER_UID_LIMIT as MEMBER_UID_LIMIT,
    ArchiveExportTooLarge as ArchiveExportTooLarge,
    ChatSandboxInspection as ChatSandboxInspection,
    ChatSandboxSpec as ChatSandboxSpec,
    ChatSandboxStatus as ChatSandboxStatus,
    MemberProcessReceiptMissing as MemberProcessReceiptMissing,
    RetainedChatFailStopped as RetainedChatFailStopped,
    RetainedChatLost as RetainedChatLost,
    SandboxActionFacts as SandboxActionFacts,
    SandboxRestoreFailed as SandboxRestoreFailed,
)
from cloud_agent_host.chat_sandbox_network import (
    _GUEST_NETWORK_CHECK as _GUEST_NETWORK_CHECK,
    CHAT_BOOTSTRAP_NETWORK as CHAT_BOOTSTRAP_NETWORK,
    CHAT_NETWORK_BRIDGE_OPTION as CHAT_NETWORK_BRIDGE_OPTION,
    CHAT_NETWORK_PREFIX as CHAT_NETWORK_PREFIX,
    CHAT_NETWORK_PREFIX_LENGTH as CHAT_NETWORK_PREFIX_LENGTH,
    CHAT_NETWORK_PRUNE_GRACE_S as CHAT_NETWORK_PRUNE_GRACE_S,
    CHAT_NULL_NETWORK as CHAT_NULL_NETWORK,
    CHAT_OFFLINE_DNS as CHAT_OFFLINE_DNS,
    RELAY_GATEWAY_PRIORITY as RELAY_GATEWAY_PRIORITY,
    ChatSandboxNetwork,
    _GroupNetworkSnapshot,
)
from cloud_agent_host.environment_process import ACTION_UIDS, ENVIRONMENT_UIDS
from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_BINARY,
    GUEST_OBSERVER_EVENTS,
    GUEST_OBSERVER_ROOT,
    GUEST_OBSERVER_ROTATE_BYTES,
    GuestObservation,
    GuestObserverController,
    GuestObserverCursor,
    GuestObserverRuntimeError,
    GuestObserverSnapshot,
)
from cloud_agent_host.network_guard import GUARD_RECHECK_S
from cloud_agent_host.sandbox import (
    KATA_RUNTIME,
    ContainerBackend,
    SandboxBackend,
    SandboxError,
    SandboxProcess,
    SandboxSpec,
    TerminalProcess,
)
from cloud_agent_host.sandbox_checkpoints import checkpoint_image_reference
from cloud_agent_host.tool_windows import MAX_TOOL_MARK_BYTES, TOOL_MARK_FILE, ToolMarkReader
from cloud_agent_shared.environment import (
    DOCKER_CLIENT_ENV as _DOCKER_CLIENT_ENV,
    DOCKER_CLIENT_PREFIXES as _DOCKER_CLIENT_ENV_PREFIXES,
    PROXY_ENV,
)
from cloud_agent_shared.environment_snapshots import SETUP_MARKER_NAME, snapshot_image_reference
from cloud_agent_shared.operation_profiling import StageObserver, substage

if TYPE_CHECKING:
    import builtins
    import ipaddress
    from collections.abc import Callable, Iterator, Sequence
    from typing import BinaryIO

    from _typeshed import SupportsWrite

    from cloud_agent_shared.egress import SandboxNetwork
#: A probe older than this was abandoned by a process killed mid-probe. Since
#: discovery no longer stops it, the next probe in the namespace removes it.
PROBE_ABANDONED_AFTER_S = 3600
CHAT_WORKDIR = "/workspace"
#: Where a task's uploaded files land. Outside the worktree on purpose: the
#: publish path stages the workspace with ``git add -A -N``, so anything
#: written under CHAT_WORKDIR is committed and shows up in the draft pull
#: request. Root-owned and read-only to the sandbox — see
#: ``build_upload_archive``, and the Dockerfile that creates the directory.
CHAT_UPLOADS_DIR = "/uploads"
#: ``docker cp`` time allowed per GiB of an imported archive, and the floor for
#: a smaller one: the two minutes a 1 GiB checkout always had, kept in step with
#: a host that raised its source import limit.
ARCHIVE_IMPORT_TIMEOUT_PER_GIB_S = 120.0
#: Export gets the same per-GiB allowance as import. The floor also bounds a
#: Docker daemon that accepts the request but never produces the first tar
#: header; callers additionally provide a byte ceiling for the path itself.
ARCHIVE_EXPORT_TIMEOUT_PER_GIB_S = ARCHIVE_IMPORT_TIMEOUT_PER_GIB_S
CHAT_STATE_DIR = "/agent-state"
CLAUDE_SHARED_PROJECTS_DIR = f"{CHAT_STATE_DIR}/claude/projects"
#: Where Codex, OpenCode and Kilo keep one mode-0700 provider home per member
#: (``resident_transport.PRIVATE_MEMBER_STATE_ROOTS``). Only a home's own
#: member UID can open it, and no member can rename or replace one: each
#: collection is owned by the sandbox user and sticky, inside a sticky
#: /agent-state (see :meth:`ChatSandboxManager.ensure_private_member_collections`).
PRIVATE_MEMBER_HOME_COLLECTIONS = tuple(
    f"{CHAT_STATE_DIR}/{runtime}-homes" for runtime in ("codex", "opencode", "kilo")
)
#: The member-created, group-writable collections earlier releases used. A
#: member moves its own home out of one once; nothing is ever started from it.
LEGACY_MEMBER_HOME_COLLECTIONS = tuple(
    f"{CHAT_STATE_DIR}/{runtime}-members" for runtime in ("codex", "opencode", "kilo")
)
#: Opens a new chat's imported tree to the shared group, run as the sandbox
#: user before its first turn. Only the sandbox user's own files change. A
#: workspace started from an installation snapshot already holds the
#: installer's files (another UID, which the sandbox user may not chmod); they
#: keep the modes the installer gave them, as in a workspace that installs in
#: full, where installation runs after this step. A directory of theirs the
#: sandbox user cannot search is skipped rather than failing the turn.
_NOT_OURS_AND_UNSEARCHABLE = '\\( -type d ! -uid "$me" ! \\( -readable -executable \\) -prune \\)'
SHARED_WORKSPACE_PERMISSIONS_SCRIPT = (
    "set -eu; me=$(id -u); "
    f"find {CHAT_WORKDIR} {CHAT_STATE_DIR} -xdev {_NOT_OURS_AND_UNSEARCHABLE} "
    '-o \\( -type d -uid "$me" -exec chmod g+rwx,g+s -- {} + \\); '
    f"find {CHAT_WORKDIR} {CHAT_STATE_DIR} -xdev {_NOT_OURS_AND_UNSEARCHABLE} "
    '-o \\( -type f -uid "$me" -exec chmod g+rw -- {} + \\); '
    f"git -C {CHAT_WORKDIR} config core.sharedRepository group"
)
ACTIVATION_TMPFS = "/run/agent-activation"
MEMBER_TMPFS_ROOT = "/run/agent-members"
#: Sandbox Manager-owned durable receipt for the immutable pre-prompt archive. It is
#: deliberately outside every agent-writable or archived root.  Guest root
#: creates this directory as 0700 and the one marker as 0600; all operations
#: validate those exact properties without following symlinks.
# Keep the on-disk path so existing retained containers keep their archive fences.
INITIAL_ARCHIVE_MARKER_ROOT = "/var/lib/freeinference-broker"
INITIAL_ARCHIVE_MARKER_NAME = "initial-archive-v1.json"
INITIAL_ARCHIVE_MARKER_MAX_BYTES = 4096
#: Root-owned policy fence written before a final Docker stop. Unlike the
#: initial receipt, a newer running lifecycle deliberately replaces this file:
#: the retained bridge, not marker presence alone, distinguishes pending from
#: completed final archival after a sandbox manager restart.
FINAL_ARCHIVE_MARKER_NAME = "final-archive-v1.json"
# The canonical uploaded receipt can exceed 4 KiB at the accepted policy and
# S3 VersionId field bounds. Keep the sandbox-manager-private record tightly bounded,
# but leave enough room for every schema-valid marker.
FINAL_ARCHIVE_MARKER_MAX_BYTES = 8192
SETUP_MARKER_MAX_BYTES = 1024

# Runs only through an exact-container, guest-root ``docker exec``.  Keeping
# the filesystem checks in one source-controlled program makes read, publish,
# and delete agree on the same trust boundary.  The immediate parent is also
# checked because ``O_NOFOLLOW`` protects only the final path component.
_INITIAL_ARCHIVE_MARKER_PREAMBLE = r"""
import fcntl
import os
import stat
import sys

root, name, limit_text, uid_text, gid_text = sys.argv[1:6]
limit = int(limit_text)
expected_uid = int(uid_text)
expected_gid = int(gid_text)

def same_identity(left, right):
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_uid,
        left.st_gid,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_uid,
        right.st_gid,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )

def checked_parent():
    parent = os.path.dirname(root)
    leaf = os.path.basename(root)
    if not parent or not leaf or leaf in ('.', '..'):
        raise RuntimeError('invalid marker root')
    before = os.lstat(parent)
    if (
        not stat.S_ISDIR(before.st_mode)
        or before.st_uid != expected_uid
        or before.st_gid != expected_gid
        or stat.S_IMODE(before.st_mode) & 0o022
    ):
        raise RuntimeError('marker parent cannot be trusted')
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    after = os.fstat(descriptor)
    if not same_identity(before, after):
        os.close(descriptor)
        raise RuntimeError('marker parent changed while opening')
    return descriptor, leaf

def checked_root():
    parent_fd, leaf = checked_parent()
    try:
        try:
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        else:
            os.fsync(parent_fd)
        before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(before.st_mode)
            or before.st_uid != expected_uid
            or before.st_gid != expected_gid
            or stat.S_IMODE(before.st_mode) != 0o700
        ):
            raise RuntimeError('marker directory cannot be trusted')
        descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        after = os.fstat(descriptor)
        if not same_identity(before, after):
            os.close(descriptor)
            raise RuntimeError('marker directory changed while opening')
    finally:
        os.close(parent_fd)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor

def read_checked(descriptor):
    try:
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != expected_uid
        or before.st_gid != expected_gid
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or not 0 < before.st_size <= limit
    ):
        raise RuntimeError('marker file cannot be trusted')
    marker_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    try:
        opened = os.fstat(marker_fd)
        if not same_identity(before, opened):
            raise RuntimeError('marker file changed while opening')
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(marker_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b''.join(chunks)
        after = os.fstat(marker_fd)
        if not same_identity(opened, after) or len(payload) != before.st_size:
            raise RuntimeError('marker file changed while reading')
        return payload
    finally:
        os.close(marker_fd)
"""
#: This module is a library boundary: it reports what it observed and
#: leaves the decision to the caller, so a warning here is the only
#: record an operator gets of a partially capable guest.
logger = logging.getLogger(__name__)


def claude_member_config_dir(slot_id: int, thread_id: str) -> str:
    """Return durable Claude launch state under this member's private home."""
    if slot_id < 0 or not thread_id:
        raise ValueError("resident slot and member identity are required")
    member_key = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return f"/home/agent-member-{slot_id}/.claude-members/{member_key}"


#: How many times the guest observer probe may be tried before a host is
#: refused. Three, because the probe is a race against a guest that has just
#: booted and the cost of asking again is one throwaway sandbox, while the cost
#: of a false refusal is the whole host advertising no deployment.
GUEST_OBSERVER_PROBE_ATTEMPTS = 3
#: Long enough for the observer's poll loop, its ring drain and its first status
#: publication to get clear of whatever was still starting behind them.
GUEST_OBSERVER_PROBE_RETRY_S = 2.0


class _ObserverProbeRace(SandboxError):
    """A probe failure that a second attempt may not reproduce.

    Raised only by the stages that read back from a running guest observer --
    opening the cursor and closing it again. Everything else the probe checks
    is a property of the image, the kernel or the exec sandbox, and answers
    identically however many times it is asked; those keep raising
    ``SandboxError`` so a genuinely incapable host is refused at once instead of
    three times.

    It is a ``SandboxError`` so that callers which already handle a failed
    preflight are unaffected by the distinction.
    """


MEMBER_REAPER_TARGET = "/usr/local/bin/member-process-reaper"
MEMBER_REAPER_SOURCE = "/usr/local/libexec/member-process-reaper"
# Kata exec cannot acquire capabilities outside the container's bounding set.
# Only the fixed guest-root helpers use these: tracing, self-test UID drops,
# socket ownership, and reading/signalling member process receipts.
KATA_HELPER_CAPABILITIES = (
    "BPF",
    "PERFMON",
    "SETUID",
    "SETGID",
    "CHOWN",
    "DAC_OVERRIDE",
    "KILL",
    "SYS_PTRACE",
)
_UNPRIVILEGED_EXEC = ("/usr/bin/setpriv", "--no-new-privs", "--")
# A sandbox environment reaches ``docker exec`` by name (``--env KEY``), its
# value through the Docker CLI's own environment, so a member's inference or
# MCP credential never sits in the client's argv (``/proc/<pid>/cmdline``,
# ``ps``, process accounting) for the life of a resident CLI. Only the names
# the Docker CLI itself reads -- its endpoint and config directory, executable,
# plugin and credential-helper lookup, TLS roots, proxies, the Go runtime and
# the dynamic loader -- still travel as ``KEY=VALUE``: a sandbox value under one
# of them must not reconfigure the client, and none of them carries a secret.


_PROXY_ENV = frozenset(name.upper() for name in PROXY_ENV)
_FULL_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
#: Where retained chat state lives: only ever the container's writable layer.
_CHAT_STATE_ROOTS = (CHAT_WORKDIR, CHAT_STATE_DIR)
# ``AGENT_SANDBOX_EXTRA_ARGS`` follow the platform's own ``docker create``
# flags, so a later spelling would win. These would undo what those flags
# establish: the chat's identity and restart behaviour, a secret-free
# environment, state in the writable layer, the unprivileged user and its
# security profile, the one guarded network and resolver, or the limits.
_REFUSED_EXTRA_ARGS = frozenset(
    {
        "--rm",
        "--name",
        "--label",
        "--label-file",
        "--restart",
        "--entrypoint",
        "--init",
        "--env",
        "--env-file",
        "--mount",
        "--volume",
        "--volumes-from",
        "--tmpfs",
        "--storage-opt",
        "--read-only",
        "--user",
        "--group-add",
        "--privileged",
        "--cap-add",
        "--security-opt",
        "--runtime",
        "--userns",
        "--cgroupns",
        "--cgroup-parent",
        "--sysctl",
        "--device",
        "--device-cgroup-rule",
        "--gpus",
        "--pid",
        "--ipc",
        "--uts",
        "--network",
        "--network-alias",
        "--link",
        "--ip",
        "--ip6",
        "--mac-address",
        "--dns",
        "--add-host",
        "--publish",
        "--publish-all",
        "--memory",
        "--memory-swap",
        "--memory-reservation",
        "--kernel-memory",
        "--oom-kill-disable",
        "--cpus",
        "--cpu-period",
        "--cpu-quota",
        "--cpu-shares",
        "--cpu-rt-period",
        "--cpu-rt-runtime",
        "--pids-limit",
    }
)
# Docker's short flags, as the long options they spell, and the hidden aliases
# it still accepts. A short flag that takes a value ends a combined group such
# as ``-itu0``: the rest of the token is that value.
_EXTRA_ARG_SHORT_FLAGS = {
    "a": "--attach",
    "c": "--cpu-shares",
    "e": "--env",
    "h": "--hostname",
    "i": "--interactive",
    "l": "--label",
    "m": "--memory",
    "p": "--publish",
    "P": "--publish-all",
    "t": "--tty",
    "u": "--user",
    "v": "--volume",
    "w": "--workdir",
}
_EXTRA_ARG_SHORT_SWITCHES = frozenset("iPt")
_EXTRA_ARG_ALIASES = {"--net": "--network", "--net-alias": "--network-alias"}


def _extra_arg_options(token: str) -> list[str]:
    """Name the options one extra-argument token sets, as Docker reads it.

    A token that is not an option -- the value of the one before it -- sets
    none.
    """
    if token.startswith("--"):
        option = token.split("=", 1)[0]
        return [_EXTRA_ARG_ALIASES.get(option, option)]
    if not token.startswith("-") or token == "-":
        return []
    options: list[str] = []
    for letter in token[1:]:
        options.append(_EXTRA_ARG_SHORT_FLAGS.get(letter, f"-{letter}"))
        if letter not in _EXTRA_ARG_SHORT_SWITCHES:
            break
    return options


def _exec_env_by_name(key: str) -> bool:
    """Whether one sandbox variable crosses ``docker exec`` as a bare name."""
    return not (
        key in _DOCKER_CLIENT_ENV
        or key.upper() in _PROXY_ENV
        or key.startswith(_DOCKER_CLIENT_ENV_PREFIXES)
    )


# PID 1 contains no chat credential and owns no chat state outside the
# container writable layer. A reconnectable protocol bridge may later replace
# this inert process as a latency optimization; continuity does not depend on
# it. Docker's ``--init`` owns signal forwarding/reaping, while one never-
# respawned child avoids manufacturing a same-UID process event every hour.

_INIT_COMMAND = (
    'trap \'kill "$child" 2>/dev/null; wait "$child"; exit 0\' TERM INT; '
    'sleep 2147483647 & child=$!; wait "$child"'
)


class ChatSandboxManager:
    """Create and operate retained chat containers through Docker only."""

    def __init__(self, backend: ContainerBackend) -> None:
        self.backend = backend
        self._network_lifecycle = ChatSandboxNetwork(self)
        self._member_reaper_lock = threading.Lock()
        # Multiple fork siblings may admit turns concurrently. Singleton
        # observer startup/rotation and its live cursors therefore need one
        # sandbox-manager-wide critical section per manager.
        self._guest_observer_lock = threading.RLock()
        self._guest_observer_active: dict[str, dict[int, int]] = {}
        # Containers this manager ran ``docker start`` on that have not since
        # passed the in-guest network check. A start that fails in between
        # stops its guest; if even that fails, the next activation finds the
        # guest running and must prove it before reusing it.
        self._unproven_guests: set[str] = set()
        self._unproven_guests_lock = threading.Lock()

    # ── the group network ──────────────────────────────────────────────
    #
    # Delegates preserve the manager surface. The network component owns the
    # routable bridge per sandbox group, used by setup, the agent and every
    # fork member alike, named from the group's identity and guarded on the
    # host by the deployment's rule set (cloud_agent_shared.egress). Docker
    # routes and masquerades it; the host's rules decide what it reaches. The
    # only other endpoints on it are the two relays, copied from the relay
    # network, and the host holds an exact exception for each of them.

    def _network(self) -> SandboxNetwork:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._network()

    def network_for(self, spec: ChatSandboxSpec) -> str:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle.network_for(spec)

    def _relay_aliases(self) -> tuple[str, ...]:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._relay_aliases()

    def _network_labels(self, spec: ChatSandboxSpec) -> dict[str, str]:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._network_labels(spec)

    def _inspect_network(self, network: str) -> dict[str, Any] | None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._inspect_network(network)

    def _verify_group_network(self, network: dict[str, Any], spec: ChatSandboxSpec) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._verify_group_network(network, spec)

    def _network_uses_chat_pool(self, network: dict[str, Any]) -> bool:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._network_uses_chat_pool(network)

    def _inspect_containers(self, container_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._inspect_containers(container_ids)

    @staticmethod
    def _endpoint_aliases(raw: dict[str, Any], network: str) -> set[str] | None:
        """Delegate to the group-network lifecycle owner."""
        return ChatSandboxNetwork._endpoint_aliases(raw, network)

    def _network_aliases_for(
        self, container_ids: Sequence[str], network: str
    ) -> dict[str, set[str]]:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._network_aliases_for(container_ids, network)

    def _discover_relays(self) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._discover_relays()

    def _relay_owners(self) -> dict[str, list[str]]:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._relay_owners()

    def _container_network_aliases(self, container_id: str, network: str) -> set[str] | None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._container_network_aliases(container_id, network)

    def relay_addresses(self, network: str) -> dict[str, str]:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle.relay_addresses(network)

    @staticmethod
    def _relay_addresses_from(
        owners: dict[str, list[str]], raw: dict[str, Any] | None
    ) -> dict[str, str]:
        """Delegate to the group-network lifecycle owner."""
        return ChatSandboxNetwork._relay_addresses_from(owners, raw)

    def _sync_kata_guest_network(
        self,
        spec: ChatSandboxSpec,
        current: ChatSandboxInspection,
        addresses: dict[str, str] | None = None,
    ) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._sync_kata_guest_network(spec, current, addresses)

    def _relay_endpoint(self, container_id: str, network: str) -> tuple[set[str], int] | None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._relay_endpoint(container_id, network)

    @classmethod
    def _relay_endpoint_from(
        cls, raw: dict[str, Any] | None, network: str
    ) -> tuple[set[str], int] | None:
        """Delegate to the group-network lifecycle owner."""
        return ChatSandboxNetwork._relay_endpoint_from(raw, network)

    def _ensure_service_connection(
        self,
        *,
        network: str,
        container_id: str,
        aliases: builtins.list[str],
        inspected: dict[str, Any] | None = None,
    ) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._ensure_service_connection(
            network=network, container_id=container_id, aliases=aliases, inspected=inspected
        )

    def _reconcile_group_members(
        self,
        *,
        network: str,
        spec: ChatSandboxSpec,
        service_ids: set[str],
        service_aliases: tuple[str, ...],
        sandbox: ChatSandboxInspection | None = None,
    ) -> dict[str, Any]:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._reconcile_group_members(
            network=network,
            spec=spec,
            service_ids=service_ids,
            service_aliases=service_aliases,
            sandbox=sandbox,
        )

    def _candidate_chat_subnets(self, network_name: str) -> Iterator[ipaddress.IPv4Network]:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._candidate_chat_subnets(network_name)

    @staticmethod
    def _is_subnet_allocation_conflict(stderr: str) -> bool:
        """Delegate to the group-network lifecycle owner."""
        return ChatSandboxNetwork._is_subnet_allocation_conflict(stderr)

    def _create_group_network(self, expected: str, spec: ChatSandboxSpec) -> dict[str, Any]:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._create_group_network(expected, spec)

    def _attach_relays(
        self,
        network: str,
        discovery: tuple[dict[str, list[str]], dict[str, dict[str, Any]]] | None = None,
    ) -> set[str]:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._attach_relays(network, discovery)

    def _sync_relay_exceptions(self, network: str, addresses: dict[str, str] | None = None) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._sync_relay_exceptions(network, addresses)

    def _drop_relay_exceptions(self, network_name: str) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._drop_relay_exceptions(network_name)

    def _ensure_group_services(
        self,
        network: str,
        spec: ChatSandboxSpec,
        *,
        sandbox: ChatSandboxInspection | None = None,
    ) -> _GroupNetworkSnapshot:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._ensure_group_services(network, spec, sandbox=sandbox)

    def sync_ssh_targets(self, spec: ChatSandboxSpec, endpoints: set[tuple[str, int]]) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle.sync_ssh_targets(spec, endpoints)

    def require_host_rules(self) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle.require_host_rules()

    def renew_host_rules(self) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle.renew_host_rules()

    def _ensure_group_network(
        self,
        spec: ChatSandboxSpec,
        *,
        current: ChatSandboxInspection | None = None,
        snapshot: _GroupNetworkSnapshot | None = None,
    ) -> str:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._ensure_group_network(
            spec, current=current, snapshot=snapshot
        )

    def _owned_network_matches(self, network_name: str, spec: ChatSandboxSpec) -> bool:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._owned_network_matches(network_name, spec)

    def _disconnect_endpoint(
        self,
        network: str,
        container_id: str,
        *,
        network_name: str | None = None,
    ) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._disconnect_endpoint(
            network, container_id, network_name=network_name
        )

    def _release_owned_network(
        self,
        network_name: str,
        spec: ChatSandboxSpec,
        *,
        only_if_sandbox_unattached: bool = False,
        expected_network_id: str | None = None,
        expected_container_id: str | None = None,
    ) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._release_owned_network(
            network_name,
            spec,
            only_if_sandbox_unattached=only_if_sandbox_unattached,
            expected_network_id=expected_network_id,
            expected_container_id=expected_container_id,
        )

    def _release_orphan_network(self, network: dict[str, Any]) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._release_orphan_network(network)

    def _release_sandbox_networks(
        self,
        spec: ChatSandboxSpec,
        current: ChatSandboxInspection,
        *,
        expected_container_id: str | None = None,
    ) -> ChatSandboxInspection:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._release_sandbox_networks(
            spec, current, expected_container_id=expected_container_id
        )

    @staticmethod
    def _network_is_stale_for_prune(network: dict[str, Any]) -> bool:
        """Delegate to the group-network lifecycle owner."""
        return ChatSandboxNetwork._network_is_stale_for_prune(network)

    def prune_orphan_networks(self, specs: builtins.list[ChatSandboxSpec]) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle.prune_orphan_networks(specs)

    def _endpoint_is_current(
        self,
        current: ChatSandboxInspection,
        network_name: str,
        network: dict[str, Any] | None = None,
    ) -> bool:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._endpoint_is_current(current, network_name, network)

    def _attach_to_group_network(
        self,
        spec: ChatSandboxSpec,
        current: ChatSandboxInspection,
        snapshot: _GroupNetworkSnapshot | None = None,
    ) -> ChatSandboxInspection:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._attach_to_group_network(spec, current, snapshot)

    def _verify_group_snapshot(
        self,
        spec: ChatSandboxSpec,
        current: ChatSandboxInspection,
        observed: _GroupNetworkSnapshot,
    ) -> ChatSandboxInspection | None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._verify_group_snapshot(spec, current, observed)

    def _require_group_network(
        self,
        spec: ChatSandboxSpec,
        current: ChatSandboxInspection,
        snapshot: _GroupNetworkSnapshot | None = None,
    ) -> ChatSandboxInspection:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._require_group_network(spec, current, snapshot)

    def _verify_guest_network(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str | None = None,
        defer_network_release: bool = False,
    ) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle._verify_guest_network(
            spec,
            expected_container_id=expected_container_id,
            defer_network_release=defer_network_release,
        )

    def verify_host_rules(self, running: Sequence[ChatSandboxSpec] = ()) -> None:
        """Delegate to the group-network lifecycle owner."""
        return self._network_lifecycle.verify_host_rules(running)

    def require_docker_data_root(self, expected: str) -> str:
        """Verify retained state is owned by the intended Docker data root.

        The sandbox manager intentionally cannot read or mount this host path. Docker
        reports its daemon-side root over the socket; comparing that value is
        the configuration fence that catches a host accidentally using its
        ephemeral system disk instead of the operator's durable Docker disk.
        Whether that path is a mounted durable device remains a host bootstrap
        check (``findmnt``), because a container with only the Docker socket
        must not be given the host filesystem merely to prove it.
        """
        if not expected or "\x00" in expected or not os.path.isabs(expected):
            raise ValueError("expected Docker data root must be an absolute host path")
        result = self._run(
            [self.backend.docker_binary, "info", "--format", "{{.DockerRootDir}}"],
            timeout=30,
        )
        if result.returncode != 0:
            raise SandboxError("Docker data-root inspection failed: " + result.stderr.strip()[:300])
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1 or not os.path.isabs(lines[0]):
            raise SandboxError("Docker returned an invalid data-root path")
        actual = os.path.normpath(lines[0])
        configured = os.path.normpath(expected)
        if actual != configured:
            raise SandboxError(
                f"Docker data root is {actual!r}, expected retained storage at {configured!r}"
            )
        return actual

    def build_create_command(
        self,
        spec: ChatSandboxSpec,
        *,
        image: str | None = None,
        checkpoint_id: str | None = None,
        environment_snapshot: tuple[str, str] | None = None,
    ) -> builtins.list[str]:
        """Compose a no-secret, no-chat-volume ``docker create`` command.

        ``checkpoint_id`` recreates a moved group: the image is that
        checkpoint's local image and nothing else, and the container records
        which checkpoint it came from. ``environment_snapshot`` (owner hash,
        snapshot id) creates a new group from an installation snapshot, the
        same way.
        """
        self._validate_extra_args()
        if checkpoint_id is not None and environment_snapshot is not None:
            raise SandboxError("a chat sandbox comes from a checkpoint or a snapshot, not both")
        if checkpoint_id is not None:
            if image != checkpoint_image_reference(spec.sandbox_id, checkpoint_id):
                raise SandboxError("chat sandbox restore image does not match its checkpoint")
        elif environment_snapshot is not None:
            if image != snapshot_image_reference(*environment_snapshot):
                raise SandboxError("chat sandbox image does not match its environment snapshot")
        elif (
            image is not None
            and spec.sandbox_image_digest is not None
            and image != spec.sandbox_image_digest
        ):
            raise SandboxError("chat sandbox image override does not match its immutable digest")
        argv = [
            self.backend.docker_binary,
            "create",
            "--name",
            spec.container_name,
            "--init",
        ]
        labels = {
            **spec.docker_labels(),
            CHAT_NAMESPACE_LABEL: self.backend.sandbox_namespace,
        }
        if checkpoint_id is not None:
            labels[CHAT_CHECKPOINT_LABEL] = checkpoint_id
        if environment_snapshot is not None:
            labels[CHAT_ENVIRONMENT_SNAPSHOT_LABEL] = ":".join(environment_snapshot)
            labels[ENVIRONMENT_TEST_LABEL] = ""
        for key, value in sorted(labels.items()):
            argv += ["--label", f"{key}={value}"]
        argv += [
            "--workdir",
            CHAT_WORKDIR,
            "--user",
            f"{self.backend.uid}:{self.backend.gid}",
            # Docker retains the create-time network as immutable primary
            # metadata even after disconnect. Use its daemon-stable bridge as
            # a dormant anchor, then attach the group bridge and disconnect
            # this one while the container is still stopped. No untrusted code
            # runs until start() verifies the group bridge is the sole network.
            "--network",
            CHAT_BOOTSTRAP_NETWORK,
            # Docker resolves relay aliases through 127.0.0.11 and forwards
            # every other name to this server from the container's namespace:
            # the deployment's one resolver, the only DNS the host's rules let
            # a group reach. A Kata guest is given the same server directly.
            "--dns",
            (
                str(self.backend.sandbox_network.dns_server)
                if self.backend.sandbox_network is not None
                else CHAT_OFFLINE_DNS
            ),
            "--memory",
            spec.memory_limit,
            "--cpus",
            spec.cpu_limit,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
            "--stop-timeout",
            str(spec.stop_timeout_s),
            "--tmpfs",
            (
                f"{ACTIVATION_TMPFS}:rw,nosuid,nodev,noexec,mode=0700,"
                f"uid={self.backend.uid},gid={self.backend.gid}"
            ),
        ]
        if spec.disk_limit_mib:
            # Docker enforces this against the writable layer. Unsupported
            # drivers/backing filesystems reject creation; never retry without
            # the requested quota or pretend it was applied.
            argv += ["--storage-opt", f"size={spec.disk_limit_mib}M"]
        for uid in (*ENVIRONMENT_UIDS, *ACTION_UIDS):
            argv += [
                "--tmpfs",
                f"/run/agent-environments/{uid}:rw,nosuid,nodev,noexec,mode=0700,uid={uid},gid={self.backend.gid}",
            ]
        for slot_id in range(spec.resident_slot_capacity):
            uid = MEMBER_UID_BASE + slot_id
            argv += [
                "--tmpfs",
                (
                    f"{MEMBER_TMPFS_ROOT}/{slot_id}:rw,nosuid,nodev,noexec,"
                    f"mode=0700,uid={uid},gid={self.backend.gid}"
                ),
            ]
        if self.backend.runtime == KATA_RUNTIME:
            # The eBPF program is loaded by a trusted root helper *inside* the
            # Kata guest.  Its journal is root-only and ephemeral; agent UIDs
            # cannot read, forge, or retain it across a guest restart.
            #
            # Kata limits even privileged exec to this bounding set. The
            # ordinary member exec path explicitly sets no-new-privileges;
            # Kata does not inherit PID 1's setting for docker exec.
            for capability in KATA_HELPER_CAPABILITIES:
                argv += ["--cap-add", capability]
            argv += [
                "--tmpfs",
                f"{GUEST_OBSERVER_ROOT}:rw,nosuid,nodev,noexec,mode=0700,uid=0,gid=0",
            ]
        if self.backend.runtime:
            argv += ["--runtime", self.backend.runtime]
        argv += self.backend.extra_args
        argv += [
            "--entrypoint",
            "/bin/sh",
            image or spec.sandbox_image_digest or self.backend.image,
            "-c",
            _INIT_COMMAND,
        ]
        return argv

    def create(
        self,
        spec: ChatSandboxSpec,
        *,
        image: str | None = None,
        checkpoint_id: str | None = None,
        environment_snapshot: tuple[str, str] | None = None,
        facts: SandboxActionFacts | None = None,
        observer: StageObserver | None = None,
    ) -> ChatSandboxInspection:
        """Create one chat container idempotently, never replacing an existing one.

        The container is created stopped on the dormant bootstrap bridge, and
        its group bridge is left to :meth:`start`, which attaches it while it
        is still stopped and proves the network before anything runs in it.
        Reconciling the bridge here as well only repeated that work: each
        reconciliation is a dozen Docker calls and a host-rule helper
        container. The host's rules are still checked first, so a host that
        cannot show them refuses before any container exists.
        """
        with substage(observer, "host_rules"):
            self.require_host_rules()
        existing = self.inspect(spec.container_name)
        if existing is not None:
            # Idempotent adoption, not a creation. No action is noted here:
            # the start() that follows knows whether this attempt had to run
            # `docker start`, and that is what classifies the path.
            self._verify_identity(existing, spec)
            return existing
        command = self.build_create_command(
            spec,
            image=image,
            checkpoint_id=checkpoint_id,
            environment_snapshot=environment_snapshot,
        )
        with substage(observer, "container_create"):
            created = self._run(command, timeout=120)
        if created.returncode != 0:
            # A racing replica may have won after our inspect.  Only accept the
            # resulting container after checking every ownership label.
            raced = self.inspect(spec.container_name)
            if raced is None:
                raise SandboxError(
                    "chat sandbox could not be created: " + created.stderr.strip()[:300]
                )
            # The other replica paid for the creation, so this attempt did
            # not create the container. `start` reports what it did with it.
            self._verify_identity(raced, spec)
            return raced
        if facts is not None:
            # The literal the startup-path classifier reads. Emitting anything
            # else -- "created", say -- hid every brand-new group: the guard in
            # ``SandboxActionFacts.note`` would not recognise it either, so the
            # ``start`` that always follows overwrote it, and a container this
            # attempt really paid to create was reported as a wake from
            # stopped. Two of the four measured paths became indistinguishable.
            facts.note("create")
        try:
            inspected = self.inspect(spec.container_name)
            if inspected is None:
                raise SandboxError("Docker created the chat sandbox but it cannot be inspected")
            self._verify_identity(inspected, spec)
        except BaseException:
            # This call made it and nothing has run in it, but nothing will
            # adopt it either: left, it would hold the group's name, and the
            # anonymous volumes its image declares, on a host the group may
            # never come back to. It goes by the id Docker printed, so only
            # what this call made can go.
            self._remove_created(spec, created.stdout.strip())
            raise
        return inspected

    def _remove_created(self, spec: ChatSandboxSpec, container_id: str) -> None:
        """Remove a container :meth:`create` made that failed its checks; best effort."""
        removed = None
        if container_id:
            with contextlib.suppress(SandboxError):
                removed = self._run(
                    [self.backend.docker_binary, "rm", "--force", "--volumes", container_id],
                    timeout=120,
                )
        if removed is not None and "no such container" in removed.stderr.lower():
            return
        if removed is None or removed.returncode != 0:
            logger.warning(
                "chat sandbox that failed its checks could not be removed",
                extra={
                    "event": "sandbox_create_cleanup_failed",
                    "sandbox_id": spec.sandbox_id,
                },
            )

    def remove_superseded(
        self, spec: ChatSandboxSpec, *, keep_checkpoint: str | None = None
    ) -> bool:
        """Remove this group's container from an older generation, if one holds its name.

        A group moved here was rehomed under a new generation; whatever an
        earlier attempt left on this host under the old one is fenced out by
        the control plane and can never run again. Anything else holding the
        name is left alone. Returns whether a container was removed.

        A container recreated from a checkpoint takes that checkpoint's image
        with it, unless it is ``keep_checkpoint``, the one about to be loaded.
        """
        current = self.inspect(spec.container_name)
        if current is None:
            return False
        labels = current.labels
        try:
            generation = int(labels.get(CHAT_GENERATION_LABEL) or "0")
        except ValueError:
            return False
        if (
            labels.get(CHAT_SANDBOX_ID_LABEL) != spec.sandbox_id
            or labels.get(CHAT_NAMESPACE_LABEL) != self.backend.sandbox_namespace
            or not 0 < generation < spec.generation
        ):
            return False
        removed = self._run(
            [self.backend.docker_binary, "rm", "--force", current.container_id], timeout=120
        )
        if removed.returncode != 0 and self.inspect(spec.container_name) is not None:
            raise SandboxError(
                "superseded chat sandbox could not be removed: " + removed.stderr.strip()[:300]
            )
        checkpoint_id = labels.get(CHAT_CHECKPOINT_LABEL)
        if checkpoint_id and checkpoint_id != keep_checkpoint:
            self.remove_checkpoint_image(spec, checkpoint_id)
        return True

    def require_existing(
        self,
        spec: ChatSandboxSpec,
        *,
        container_id: str,
    ) -> ChatSandboxInspection:
        """Adopt only the exact durable container; never recreate missing chat state."""
        if not container_id:
            raise ValueError("expected retained container id must be non-empty")
        try:
            current = self._require(spec)
        except SandboxError as exc:
            raise RetainedChatLost("retained chat container is missing or invalid") from exc
        if current.container_id != container_id:
            raise RetainedChatLost(
                "retained chat container id does not match its durable sandbox record"
            )
        return current

    def require_stopped_archive(
        self, spec: ChatSandboxSpec, *, container_id: str
    ) -> ChatSandboxInspection:
        """Fence archival and removal to an exact owned container that is stopped or dead.

        Docker's dead state is terminal and not running: the container cannot be
        resumed, but its writable layer can still be exported. Removal stays
        ``docker rm`` without ``--force``, so a start between these fences is
        left in place.
        """
        current = self.require_existing(spec, container_id=container_id)
        if not self._belongs_to_namespace(current):
            raise SandboxError("sandbox archive requires this deployment's ownership label")
        if current.running or current.status not in {
            ChatSandboxStatus.STOPPED,
            ChatSandboxStatus.DEAD,
        }:
            raise SandboxError("sandbox archive requires a stopped or dead container")
        return current

    def export_stopped_archive(
        self,
        spec: ChatSandboxSpec,
        *,
        container_id: str,
        output: BinaryIO,
        max_bytes: int,
        timeout_s: float = 600,
    ) -> dict[str, Any]:
        """Export the Docker-owned filesystem without starting user processes.

        The normal mount fence rejects bind/volume state. Docker export
        includes workspace and all providers' native state in the writable
        layer, while stopped credential tmpfs mounts contain no retained data.
        Inspect configuration accompanies the export for offline recovery.
        """
        self.require_stopped_archive(spec, container_id=container_id)
        metadata_result = self._run(
            [self.backend.docker_binary, "container", "inspect", container_id], timeout=30
        )
        if metadata_result.returncode != 0:
            raise SandboxError("sandbox archive metadata inspection failed")
        try:
            metadata = json.loads(metadata_result.stdout)[0]
            if not isinstance(metadata, dict) or metadata.get("Id") != container_id:
                raise ValueError("container identity changed")
        except (IndexError, TypeError, ValueError) as exc:
            raise SandboxError("sandbox archive metadata is invalid") from exc
        process: subprocess.Popen[bytes] | None = None
        try:
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(
                    [self.backend.docker_binary, "container", "export", container_id],
                    stdout=output,
                    stderr=errors,
                )
                deadline = time.monotonic() + timeout_s
                while process.poll() is None:
                    if os.fstat(output.fileno()).st_size > max_bytes:
                        raise SandboxError("sandbox archive exceeds the S3 upload size limit")
                    if time.monotonic() >= deadline:
                        raise SandboxError("sandbox archive export timed out")
                    time.sleep(0.1)
                if process.returncode != 0:
                    raise SandboxError("sandbox archive filesystem export failed")
                output.flush()
                if os.fstat(output.fileno()).st_size > max_bytes:
                    raise SandboxError("sandbox archive exceeds the S3 upload size limit")
        except OSError as exc:
            raise SandboxError("sandbox archive filesystem export failed") from exc
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        self.require_stopped_archive(spec, container_id=container_id)
        config = metadata.get("Config") or {}
        host_config = metadata.get("HostConfig") or {}
        owned_label_keys = {
            CHAT_SANDBOX_LABEL,
            CHAT_NAMESPACE_LABEL,
            CHAT_THREAD_LABEL,
            CHAT_SANDBOX_ID_LABEL,
            CHAT_GENERATION_LABEL,
            CHAT_RUNTIME_LABEL,
            CHAT_RUNTIME_VERSION_LABEL,
            CHAT_IMAGE_DIGEST_LABEL,
            CHAT_MEMORY_LIMIT_LABEL,
            CHAT_CPU_LIMIT_LABEL,
            CHAT_STOP_TIMEOUT_LABEL,
            CHAT_SHARED_LABEL,
            CHAT_RESIDENT_CAPACITY_LABEL,
        }
        # Docker inspect also includes environment values, host paths, mounts,
        # and arbitrary labels. Offline recovery needs image identity and
        # container policy, not those potential secret-bearing values.
        return {
            **{key: metadata[key] for key in ("Id", "Name", "Image", "Created") if key in metadata},
            "Config": {
                **{
                    key: config[key]
                    for key in ("Image", "User", "WorkingDir", "Entrypoint", "Cmd")
                    if key in config
                },
                "Labels": {
                    key: value
                    for key, value in (config.get("Labels") or {}).items()
                    if key in owned_label_keys
                },
            },
            "HostConfig": {
                key: host_config[key]
                for key in (
                    "Runtime",
                    "Memory",
                    "MemorySwap",
                    "NanoCpus",
                    "CpuQuota",
                    "CpuPeriod",
                    "CpuShares",
                    "PidsLimit",
                    "ReadonlyRootfs",
                    "CapDrop",
                    "CapAdd",
                    "SecurityOpt",
                )
                if key in host_config
            },
        }

    def remove_environment_test(self, spec: ChatSandboxSpec, *, container_id: str) -> None:
        """Remove only the exact independently labelled ephemeral test container."""
        inspection = self._require_exact(spec, container_id)
        if inspection.labels.get(ENVIRONMENT_TEST_LABEL) != spec.labels.get(
            ENVIRONMENT_TEST_LABEL
        ) or not spec.labels.get(ENVIRONMENT_TEST_LABEL):
            raise SandboxError("Container is not an environment test")
        if inspection.running:
            self.stop(spec, expected_container_id=container_id)
        result = self._run([self.backend.docker_binary, "rm", container_id], timeout=120)
        if result.returncode:
            raise SandboxError("Environment test cleanup failed")

    def remove_stopped_archived(self, spec: ChatSandboxSpec, *, container_id: str) -> None:
        """Remove only a verified archive's exact stopped container, without force."""
        current = self.require_stopped_archive(spec, container_id=container_id)
        self.require_stopped_archive(spec, container_id=container_id)
        result = self._run(
            [self.backend.docker_binary, "container", "rm", container_id], timeout=60
        )
        if result.returncode != 0:
            raise SandboxError("archived sandbox removal failed")
        if self.inspect(container_id) is not None:
            raise SandboxError("archived sandbox still exists after removal")
        # Remove first, without force: a start after the stopped fence makes
        # Docker refuse removal. Generic stop/network release before rm could
        # stop a newly running original and discard post-backup writes.
        network_ids = dict(current.network_ids)
        for network_name in current.networks:
            if network_name in {CHAT_NULL_NETWORK, CHAT_BOOTSTRAP_NETWORK}:
                continue
            try:
                self._release_owned_network(
                    network_name,
                    spec,
                    only_if_sandbox_unattached=True,
                    expected_network_id=network_ids.get(network_name),
                )
            except SandboxError:
                # The container is already absent. Normal orphan-network
                # pruning can retry cleanup, fenced to that network's labels.
                logger.warning("archived sandbox network cleanup needs reconciliation")
        checkpoint_id = current.labels.get(CHAT_CHECKPOINT_LABEL)
        if checkpoint_id:
            self.remove_checkpoint_image(spec, checkpoint_id)

    def remove_checkpoint_image(self, spec: ChatSandboxSpec, checkpoint_id: str) -> None:
        """Drop this group's image of ``checkpoint_id``, unless a container still uses it.

        The image is named for this group's checkpoint, and only the group's
        container on this host ever runs it, so nothing else needs its layers.
        Best effort: Docker keeps an image a container still uses, an image
        already gone is what was asked for, and one left behind costs disk,
        not correctness.
        """
        reference = checkpoint_image_reference(spec.sandbox_id, checkpoint_id)
        try:
            removed = self._run([self.backend.docker_binary, "image", "rm", reference], timeout=120)
        except SandboxError:
            removed = None
        if removed is not None and "no such image" in removed.stderr.lower():
            return
        if removed is None or removed.returncode != 0:
            logger.warning(
                "restored checkpoint image could not be removed",
                extra={
                    "event": "sandbox_checkpoint_image_remove_failed",
                    "sandbox_id": spec.sandbox_id,
                    "checkpoint_id": checkpoint_id,
                },
            )

    def inspect(
        self, container: str, *, verify_mounts: bool = True
    ) -> ChatSandboxInspection | None:
        """Inspect a container; missing is ``None``, daemon/protocol failures raise."""
        result = self._run(
            [self.backend.docker_binary, "container", "inspect", container], timeout=30
        )
        if result.returncode != 0:
            lowered = result.stderr.lower()
            if "no such container" in lowered or "no such object" in lowered:
                return None
            raise SandboxError("chat sandbox inspect failed: " + result.stderr.strip()[:300])
        try:
            payload = json.loads(result.stdout)
            raw = payload[0]
            config = raw.get("Config") or {}
            labels = config.get("Labels") or {}
            mounts = raw.get("Mounts") or []
            state = raw.get("State") or {}
            network_settings = (raw.get("NetworkSettings") or {}).get("Networks") or {}
            networks = network_settings.keys()
            status = self._status(state)
            inspection = ChatSandboxInspection(
                container_id=str(raw["Id"]),
                name=str(raw.get("Name") or "").removeprefix("/"),
                status=status,
                running=bool(state.get("Running")),
                # Config.Image is the exact reference passed to ``docker
                # create``. ``Image`` is Docker's resolved local layer id and
                # cannot be compared directly with a registry repo digest.
                image=str(config.get("Image") or raw.get("Image") or ""),
                labels={str(key): str(value) for key, value in labels.items()},
                mounts=tuple(dict(mount) for mount in mounts),
                networks=tuple(sorted(str(network) for network in networks)),
                network_ids=tuple(
                    sorted(
                        (
                            str(network),
                            str((endpoint or {}).get("NetworkID") or ""),
                        )
                        for network, endpoint in network_settings.items()
                    )
                ),
                sandbox_runtime=str((raw.get("HostConfig") or {}).get("Runtime") or ""),
                tmpfs=tuple(
                    sorted(str(path) for path in (raw.get("HostConfig") or {}).get("Tmpfs") or {})
                ),
                started_at=str(state.get("StartedAt") or ""),
            )
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxError("Docker returned an invalid chat sandbox inspection") from exc
        if verify_mounts:
            self._verify_mounts(inspection)
        return inspection

    def _belongs_to_namespace(self, inspection: ChatSandboxInspection) -> bool:
        """Fence daemon-wide discovery to this independently deployed stack.

        Ownership is the immutable namespace label and nothing else. Containers
        without one predate that label and the single-network change; they are
        left for the one-time cleanup rather than adopted by guesswork.
        """
        return inspection.labels.get(CHAT_NAMESPACE_LABEL) == self.backend.sandbox_namespace

    def list(self) -> builtins.list[ChatSandboxInspection]:
        """List retained containers owned by this deployment namespace."""
        result = self._run(
            [
                self.backend.docker_binary,
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                '{"id":{{json .ID}},"namespace":{{json (.Label "'
                + CHAT_NAMESPACE_LABEL
                + '")}},"probe":{{json (.Label "'
                + CHAT_PROBE_LABEL
                + '")}}}',
                "--filter",
                f"label={CHAT_SANDBOX_LABEL}=true",
            ],
            timeout=30,
        )
        if result.returncode != 0:
            raise SandboxError("chat sandbox listing failed: " + result.stderr.strip()[:300])
        found: builtins.list[ChatSandboxInspection] = []
        for line in result.stdout.splitlines():
            try:
                row = json.loads(line)
                container_id, namespace = row["id"], row["namespace"]
                probe = row.get("probe") or ""
                if not isinstance(container_id, str) or not container_id:
                    raise ValueError("missing container id")
                if not isinstance(namespace, str) or not isinstance(probe, str):
                    raise ValueError("invalid labels")
            except (KeyError, TypeError, ValueError) as exc:
                raise SandboxError("Docker returned an invalid chat sandbox listing") from exc
            # Read ownership from ls before making a per-container RPC: a
            # wedged foreign Kata shim can block even docker inspect. A probe
            # belongs to the process running it and is never a chat to adopt.
            if namespace != self.backend.sandbox_namespace or probe:
                continue
            # A foreign namespace cannot make this sandbox manager fail startup by
            # carrying policy-invalid mounts. Apply this stack's mount fence
            # only after ownership is established.
            item = self.inspect(container_id, verify_mounts=False)
            if item is not None and self._belongs_to_namespace(item):
                self._verify_mounts(item)
                if not item.labels.get(ENVIRONMENT_TEST_LABEL):
                    found.append(item)
        return found

    def start(
        self,
        spec: ChatSandboxSpec,
        *,
        facts: SandboxActionFacts | None = None,
        observer: StageObserver | None = None,
        expected_container_id: str | None = None,
        defer_network_release_on_fail_stop: bool = False,
        before_stopped_start: Callable[[ChatSandboxInspection], None] | None = None,
    ) -> ChatSandboxInspection:
        """Start the exact stopped container and verify it reached RUNNING.

        A cold start is also where the network is proved from inside: nothing
        from the repository runs in a guest that has not passed
        :meth:`_verify_guest_network`. A start that fails after ``docker
        start`` stops its guest again; a running guest whose proof never
        finished is proved before it is reused.
        """
        current = self._require(spec)
        exact_container_id = expected_container_id or current.container_id
        if current.container_id != exact_container_id:
            raise RetainedChatLost("chat sandbox container identity changed before start")
        network_snapshot = _GroupNetworkSnapshot()
        with substage(observer, "group_network"):
            current = self._require_group_network(spec, current, network_snapshot)
        if current.container_id != exact_container_id:
            raise RetainedChatLost("chat sandbox container identity changed during start")
        if current.running:
            if facts is not None:
                facts.note("reuse")
            with self._unproven_guests_lock:
                unproven = exact_container_id in self._unproven_guests
            if unproven:
                # A start of this exact container failed before its in-guest
                # check and could not stop it either. Nothing has run in it
                # since; prove it now, or stop it.
                return self._prove_started_guest(
                    spec,
                    exact_container_id,
                    observer=observer,
                    defer_network_release=defer_network_release_on_fail_stop,
                    addresses=network_snapshot.addresses,
                )
            with substage(observer, "guest_network_sync"):
                self._sync_kata_guest_network(spec, current, network_snapshot.addresses)
            return current
        if current.status is ChatSandboxStatus.PAUSED:
            raise SandboxError("a paused chat sandbox is not a valid stopped activation")
        if current.status is ChatSandboxStatus.DEAD:
            raise SandboxError("a dead chat sandbox cannot be resumed")
        if before_stopped_start is not None:
            # The callback owns the durable pre-start fence. Invoke it only
            # after the exact object has been re-inspected and proven stopped,
            # immediately before this method performs the Docker transition.
            before_stopped_start(current)
        # Recorded before the Docker transition, and cleared only by a passed
        # check: a start Docker completes after its client gave up is
        # unproven too.
        with self._unproven_guests_lock:
            self._unproven_guests.add(exact_container_id)
        with substage(observer, "container_start"):
            result = self._run(
                [self.backend.docker_binary, "start", current.container_id], timeout=120
            )
        if facts is not None:
            facts.note("start")
        if result.returncode != 0:
            raise SandboxError("chat sandbox could not be started: " + result.stderr.strip()[:300])
        return self._prove_started_guest(
            spec,
            exact_container_id,
            observer=observer,
            defer_network_release=defer_network_release_on_fail_stop,
        )

    def _prove_started_guest(
        self,
        spec: ChatSandboxSpec,
        container_id: str,
        *,
        observer: StageObserver | None,
        defer_network_release: bool,
        addresses: dict[str, str] | None = None,
    ) -> ChatSandboxInspection:
        """Sync and prove a guest Docker has started, or stop it trying.

        Any failure on the way -- a relay rollout, a guest helper timing out,
        the check itself not answering -- stops the exact container, keeping
        its bridge when ``defer_network_release``, and raises
        :class:`RetainedChatFailStopped`, as a failed check does. If the stop
        fails too, the original error is raised and the guest stays recorded
        as unproven, so :meth:`start` proves it before any reuse.
        """
        try:
            started = self._require(spec)
            if started.container_id != container_id:
                raise RetainedChatLost("chat sandbox container identity changed during start")
            if not started.running or started.status is not ChatSandboxStatus.RUNNING:
                raise SandboxError(
                    f"chat sandbox started into unexpected state {started.status.value!r}"
                )
            with substage(observer, "guest_network_sync"):
                self._sync_kata_guest_network(spec, started, addresses)
            with substage(observer, "guest_network_check"):
                self._verify_guest_network(
                    spec,
                    expected_container_id=container_id,
                    defer_network_release=defer_network_release,
                )
        except RetainedChatFailStopped:
            raise
        except Exception as exc:
            stopped: ChatSandboxInspection | None = None
            try:
                stopped = self.stop(
                    spec,
                    expected_container_id=container_id,
                    release_networks=not defer_network_release,
                )
            except Exception as stop_exc:
                logger.warning(
                    "chat_unproven_guest_left_running",
                    extra={
                        "sandbox_id": spec.sandbox_id,
                        "detail": str(exc)[:300],
                        "stop_detail": str(stop_exc)[:300],
                    },
                )
            if stopped is None:
                raise
            raise RetainedChatFailStopped(
                f"chat sandbox stopped before its network check passed: {exc}",
                stopped,
            ) from exc
        with self._unproven_guests_lock:
            self._unproven_guests.discard(container_id)
        verified = self._require(spec)
        if verified.container_id != container_id:
            raise RetainedChatLost("chat sandbox container identity changed during start")
        return verified

    def stop(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str | None = None,
        release_networks: bool = True,
    ) -> ChatSandboxInspection:
        """Deactivate, release private networking, and retain the writable layer."""
        current = self._require(spec)
        exact_container_id = expected_container_id or current.container_id
        if current.container_id != exact_container_id:
            raise RetainedChatLost("chat sandbox container identity changed before stop")
        if current.status is ChatSandboxStatus.PAUSED:
            raise SandboxError("refusing to treat a paused chat sandbox as cleanly stopped")
        if not current.running:
            if not release_networks:
                return current
            return self.release_stopped_networks(
                spec,
                expected_container_id=exact_container_id,
            )
        result = self._run(
            [
                self.backend.docker_binary,
                "stop",
                "--time",
                str(spec.stop_timeout_s),
                current.container_id,
            ],
            timeout=spec.stop_timeout_s + 30,
        )
        if result.returncode != 0:
            raise SandboxError("chat sandbox could not be stopped: " + result.stderr.strip()[:300])
        stopped = self._require(spec)
        if stopped.container_id != exact_container_id:
            raise RetainedChatLost("chat sandbox container identity changed during stop")
        if stopped.running or stopped.status not in {
            ChatSandboxStatus.STOPPED,
            ChatSandboxStatus.CREATED,
            ChatSandboxStatus.DEAD,
        }:
            raise SandboxError(
                f"chat sandbox stopped into unexpected state {stopped.status.value!r}"
            )
        if not release_networks:
            return stopped
        return self.release_stopped_networks(
            spec,
            expected_container_id=exact_container_id,
        )

    def release_stopped_networks(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> ChatSandboxInspection:
        """Release networking only for one exact, already-stopped container."""
        current = self._require(spec)
        if current.container_id != expected_container_id:
            raise RetainedChatLost("chat sandbox container identity changed during stop")
        if current.running:
            raise SandboxError("refusing to release networking from a running chat sandbox")
        released = self._release_sandbox_networks(
            spec,
            current,
            expected_container_id=expected_container_id,
        )
        if released.container_id != expected_container_id:
            raise RetainedChatLost("chat sandbox container identity changed during stop")
        return released

    def require_network(
        self, spec: ChatSandboxSpec, *, observer: StageObserver | None = None
    ) -> None:
        """Prove this container is on exactly its guarded group bridge.

        Called before every terminal and sandbox manager workspace operation, and by a
        turn's commands once its last proof is a minute old, so it is cheap: the
        guard's last verification is reused for a minute, and the relays and
        their host exceptions are reconciled against what Docker reports now.
        A guest whose in-guest check never passed is refused until
        :meth:`start` proves it.
        """
        snapshot = _GroupNetworkSnapshot()
        with substage(observer, "network_ensure"):
            current = self._require_group_network(spec, self._require(spec), snapshot)
        expected = self.network_for(spec)
        if set(current.networks) != {expected}:
            raise SandboxError(
                f"retained chat network is {list(current.networks)!r}, not its group "
                f"bridge {expected!r}"
            )
        with self._unproven_guests_lock:
            unproven = current.container_id in self._unproven_guests
        if unproven:
            raise SandboxError(
                "retained chat started without passing its network check; it must be "
                "activated again before anything runs in it"
            )
        with substage(observer, "guest_network_sync"):
            self._sync_kata_guest_network(spec, current, snapshot.addresses)

    def build_exec_command(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        interactive: bool = False,
        tty: bool = False,
        user_uid: int | None = None,
        user_gid: int | None = None,
        container_id: str | None = None,
    ) -> builtins.list[str]:
        """Compose ``docker exec`` after identity/running checks at spawn time.

        Most of ``env`` is named here and valued only in the client environment
        :meth:`exec_client_env` builds; run the command with that environment.
        """
        if not argv:
            raise ValueError("a retained sandbox exec needs an argv")
        command = [self.backend.docker_binary, "exec"]
        if interactive:
            command.append("--interactive")
        if tty:
            command.append("--tty")
        command += [
            "--user",
            f"{user_uid if user_uid is not None else self.backend.uid}:"
            f"{user_gid if user_gid is not None else self.backend.gid}",
            "--workdir",
            workdir,
        ]
        for key, value in sorted((env or {}).items()):
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError(f"invalid environment entry {key!r}")
            command += ["--env", key if _exec_env_by_name(key) else f"{key}={value}"]
        command.append(container_id or spec.container_name)
        if self.backend.runtime == KATA_RUNTIME:
            command += list(_UNPRIVILEGED_EXEC)
            if not interactive:
                # Kata leaves an unattached stdin pipe open. Give commands
                # that have no input a real EOF, without interpolating argv.
                command += ["/bin/sh", "-c", 'exec "$@" </dev/null', "sh"]
        command += argv
        return command

    @staticmethod
    def exec_client_env(env: dict[str, str] | None = None) -> dict[str, str]:
        """Return the Docker CLI environment that supplies a named ``env``.

        The sandbox manager's own environment still configures the client; only the
        values :meth:`build_exec_command` passed by name are added to it.
        """
        client = {**os.environ}
        client.update({key: value for key, value in (env or {}).items() if _exec_env_by_name(key)})
        return client

    def spawn(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        live_input: bool = False,
        user_uid: int | None = None,
        user_gid: int | None = None,
        process_pidfile: str | None = None,
    ) -> SandboxProcess:
        """Run an attached command inside the retained container."""
        current = self._require(spec)
        if not current.running:
            raise SandboxError("chat sandbox must be started before exec")
        exec_argv = argv
        if process_pidfile is not None:
            self._validate_member_pidfile(spec, process_pidfile, user_uid=user_uid)
            # The CLI is a session leader, so its complete child tree can be
            # interrupted without stopping sibling sessions in this container.
            # Persist both PID and Linux start time: a stale numeric PID must
            # never be allowed to signal a later, unrelated process.
            wrapper = (
                'pidfile="$1"; shift; temporary="$pidfile.tmp.$$"; umask 0077; '
                'cleanup() { rm -f -- "$pidfile" "$temporary"; }; '
                "trap cleanup EXIT; "
                "setsid --fork --wait /bin/sh -c '"
                'pidfile="$1"; temporary="$2"; shift 2; '
                'fields=$(sed "s/^[^)]*) //" "/proc/$$/stat") || exit 126; '
                # Read field 22 (start time; field 20 after stripping pid and
                # comm) in a subshell.  Doing ``set -- $fields`` in this shell
                # would overwrite the CLI argv that must remain in ``$@``.
                'started=$(set -- $fields; printf "%s" "${20:-}"); '
                'case "$started" in ""|*[!0-9]*) exit 126;; esac; '
                'printf "%s %s\\n" "$$" "$started" > "$temporary" || exit 126; '
                'mv -f -- "$temporary" "$pidfile" || exit 126; '
                'exec "$@"\' sh "$pidfile" "$temporary" "$@"'
            )
            exec_argv = ["/bin/sh", "-c", wrapper, "sh", process_pidfile, *argv]
        command = self.build_exec_command(
            spec,
            exec_argv,
            env=env,
            workdir=workdir,
            interactive=live_input,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        process = subprocess.Popen(
            command,
            env=self.exec_client_env(env),
            stdin=subprocess.PIPE if live_input else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return _RetainedExecProcess(
            process,
            manager=self,
            spec=spec,
            process_pidfile=process_pidfile,
            user_uid=user_uid,
            user_gid=user_gid,
        )

    def _validate_member_pidfile(
        self,
        spec: ChatSandboxSpec,
        pidfile: str,
        *,
        user_uid: int | None,
    ) -> int:
        """Validate one member resident or bounded retained-command PID path."""
        command_match = re.fullmatch(
            rf"{re.escape(ACTIVATION_TMPFS)}/command-[0-9a-f]{{32}}\.pid",
            pidfile,
        )
        if command_match is not None:
            if user_uid != self.backend.uid:
                raise ValueError("command PID file does not match the sandbox uid")
            return -1
        member_match = re.fullmatch(
            rf"{re.escape(MEMBER_TMPFS_ROOT)}/([0-9]+)/resident-[0-9a-f]{{32}}\.pid",
            pidfile,
        )
        if member_match is None:
            raise ValueError("process-scoped kill requires a shared member PID file")
        slot_id = int(member_match.group(1))
        if slot_id >= spec.resident_slot_capacity:
            raise ValueError("resident PID file is outside sandbox capacity")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident PID file does not match its member uid")
        return slot_id

    def write_member_inference_env(
        self,
        spec: ChatSandboxSpec,
        script: str,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
    ) -> str:
        """Write one member's ``BASH_ENV`` script of owner inference credentials.

        The member tmpfs is private to that uid and gone with the container; the
        values travel over stdin, never argv. Returns the path to point
        ``BASH_ENV`` at.
        """
        if not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")
        path = member_inference_env_path(slot_id)
        write_script = (
            'set -eu; target="$1"; temporary="$target.tmp.$$"; umask 077; '
            "trap 'rm -f -- \"$temporary\"' EXIT HUP INT TERM; "
            'cat > "$temporary"; mv "$temporary" "$target"; '
            "trap - EXIT HUP INT TERM"
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", write_script, "sh", path],
            workdir="/",
            timeout_s=20,
            input_bytes=script.encode("utf-8"),
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "member inference environment could not be written: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )
        return path

    def ensure_member_process_reaper(self, spec: ChatSandboxSpec) -> None:
        """Install the immutable helper into retained pre-upgrade containers."""
        with self._member_reaper_lock:
            probe = self.exec_capture(
                spec,
                [
                    "/bin/sh",
                    "-c",
                    'test "$(stat -c %u:%g:%a "$1" 2>/dev/null)" = 0:0:555',
                    "sh",
                    MEMBER_REAPER_TARGET,
                ],
                workdir="/",
                timeout_s=20,
                user_uid=0,
                user_gid=0,
            )
            if probe.returncode == 0:
                return
            current = self._require(spec)
            if not current.running:
                raise SandboxError("chat sandbox must be started before helper installation")
            source = os.environ.get("AGENT_MEMBER_PROCESS_REAPER_SOURCE", MEMBER_REAPER_SOURCE)
            if not os.path.isfile(source):
                raise SandboxError(
                    "retained sandbox lacks member-process-reaper and the trusted "
                    f"runner source {source!r} is unavailable"
                )
            temporary = f"{MEMBER_REAPER_TARGET}.install-{secrets.token_hex(8)}"
            copied = self._run(
                [
                    self.backend.docker_binary,
                    "cp",
                    source,
                    f"{current.container_id}:{temporary}",
                ],
                timeout=30,
            )
            if copied.returncode != 0:
                raise SandboxError(
                    "member-process-reaper compatibility install failed: "
                    + copied.stderr.strip()[:300]
                )
            installed = self.exec_capture(
                spec,
                [
                    "/bin/sh",
                    "-c",
                    'chown 0:0 "$1" && chmod 0555 "$1" && mv -f "$1" "$2"',
                    "sh",
                    temporary,
                    MEMBER_REAPER_TARGET,
                ],
                workdir="/",
                timeout_s=20,
                user_uid=0,
                user_gid=0,
            )
            if installed.returncode != 0:
                with contextlib.suppress(Exception):
                    self.exec_capture(
                        spec,
                        ["/bin/rm", "-f", temporary],
                        workdir="/",
                        timeout_s=10,
                        user_uid=0,
                        user_gid=0,
                    )
                raise SandboxError(
                    "member-process-reaper compatibility activation failed: "
                    + installed.stderr.decode("utf-8", "replace")[:300]
                )

    def ensure_codex_mcp_policy(self, spec: ChatSandboxSpec, base_url: str) -> None:
        """Allow only this deployment's relay before starting a Codex member."""
        from cloud_agent_host.codex_mcp import INSTALL_POLICY, relay_endpoint

        endpoint = relay_endpoint(base_url)
        result = self.exec_capture(
            spec,
            ["/usr/bin/python3", "-I", "-c", INSTALL_POLICY, endpoint],
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
        )
        if result.returncode != 0:
            raise SandboxError("could not install the isolated Codex MCP relay policy")

    def ensure_kilo_mcp_runtime(self, spec: ChatSandboxSpec, version: str) -> None:
        """Bring retained Kilo members onto the deployment's isolated runtime."""
        from cloud_agent_host.kilo_mcp import ensure_kilo_runtime

        with self._member_reaper_lock:
            ensure_kilo_runtime(self, spec, version)

    def ensure_downloaded_runtime(self, spec: ChatSandboxSpec, runtime: str, version: str) -> None:
        """Copy a version downloaded after the image was built into this chat."""
        from cloud_agent_host.runtime_install import ensure_downloaded_runtime

        with self._member_reaper_lock:
            ensure_downloaded_runtime(self, spec, runtime, version)

    def ensure_mcp_helpers(self, spec: ChatSandboxSpec, runtime: str) -> None:
        """Keep existing conversations on the same adapters as new conversations."""
        from cloud_agent_host.mcp_helpers import install_mcp_helpers

        with self._member_reaper_lock:
            install_mcp_helpers(self, spec, runtime)

    def reap_member_processes(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
    ) -> None:
        """Kill unattached live CLI trees in exactly one private member slot.

        Every launch reaps its slot first, which also drops the owner inference
        credentials an earlier launch left for its bash: a launch with them
        writes them again, and one whose owner removed them must not find them.
        """
        if not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")
        directory = f"{MEMBER_TMPFS_ROOT}/{slot_id}"
        self.reap_member_processes_except(
            spec,
            slot_id=slot_id,
            user_uid=user_uid,
            user_gid=user_gid,
            baseline=(),
        )
        result = self.exec_capture(
            spec,
            [
                "/bin/sh",
                "-c",
                f"rm -f -- {directory}/resident-*.pid {member_inference_env_path(slot_id)}",
            ],
            workdir="/",
            timeout_s=20,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "stale resident process cleanup failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def capture_member_processes(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
    ) -> tuple[tuple[int, int], ...]:
        """Capture the reviewed resident PID/start-time set before one turn."""
        self._validate_member_identity(spec, slot_id=slot_id, user_uid=user_uid)
        self.ensure_member_process_reaper(spec)
        result = self.exec_capture(
            spec,
            [MEMBER_REAPER_TARGET, "capture", str(user_uid)],
            workdir="/",
            timeout_s=20,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "member process census failed: " + result.stderr.decode("utf-8", "replace")[:300]
            )
        return self._parse_member_processes(result.stdout)

    def reap_member_processes_except(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
        baseline: tuple[tuple[int, int], ...],
    ) -> None:
        """Reap every member process not present in a fenced baseline.

        This is the activation boundary for background tools. It runs as the
        member UID (the container has no CAP_KILL), validates Linux start times
        to defeat PID reuse, and repeats through SIGKILL so a process cannot
        escape merely by changing process groups or forking during cleanup.
        """
        self._validate_member_identity(spec, slot_id=slot_id, user_uid=user_uid)
        self.ensure_member_process_reaper(spec)
        payload = self._encode_member_processes(baseline)
        result = self.exec_capture(
            spec,
            [MEMBER_REAPER_TARGET, "reap", str(user_uid)],
            workdir="/",
            timeout_s=30,
            input_bytes=payload,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "member activation process cleanup failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    @staticmethod
    def _validate_member_identity(
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
    ) -> None:
        if not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")

    @staticmethod
    def _parse_member_processes(payload: bytes) -> tuple[tuple[int, int], ...]:
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SandboxError("member process census is not ASCII") from exc
        found: builtins.list[tuple[int, int]] = []
        seen: set[int] = set()
        for line in text.splitlines():
            fields = line.split()
            if len(fields) != 2 or any(
                not field.isascii() or not field.isdigit() for field in fields
            ):
                raise SandboxError("member process census is malformed")
            pid, started = (int(field) for field in fields)
            if pid <= 1 or started <= 0 or pid in seen or len(found) >= 4096:
                raise SandboxError("member process census is outside its identity fence")
            seen.add(pid)
            found.append((pid, started))
        return tuple(sorted(found))

    @staticmethod
    def _encode_member_processes(processes: tuple[tuple[int, int], ...]) -> bytes:
        canonical = ChatSandboxManager._parse_member_processes(
            "".join(f"{pid} {started}\n" for pid, started in processes).encode("ascii")
        )
        return "".join(f"{pid} {started}\n" for pid, started in canonical).encode("ascii")

    def prepare_shared_workspace(self, spec: ChatSandboxSpec) -> None:
        """Make the imported owner tree writable through the shared group."""
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", SHARED_WORKSPACE_PERMISSIONS_SCRIPT],
            workdir="/",
            timeout_s=120,
        )
        if result.returncode != 0:
            raise SandboxError(
                "shared workspace permission preparation failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def ensure_private_member_collections(self, spec: ChatSandboxSpec) -> None:
        """Make every provider-home collection one no member can rename or swap.

        A member's home is mode 0700, but that alone does not stop a sibling
        from renaming it out of a group-writable directory and putting its own
        tree at the known path before the member's CLI starts. Run as the
        sandbox user, which owns /agent-state and is no member's UID, this sets
        the sticky bit on /agent-state and creates each collection owned by the
        sandbox user with mode 3770 (setgid, sticky). A member can then add its
        own home but cannot rename or remove another's, nor the collection, and
        no member UID can undo either bit. An entry at a collection's name that
        the sandbox user does not own -- a sibling's squat, or a symlink -- is
        moved aside, which the owner of a sticky directory may always do.
        """
        script = (
            'set -eu; me=$(id -u); group=$(id -g); root="$1"; shift; '
            'test -n "$(find "$root" -maxdepth 0 -type d -uid "$me")" '
            '|| { echo "shared state root is not owned by the sandbox user" >&2; exit 1; }; '
            'chmod +t -- "$root"; '
            'for collection in "$@"; do '
            "attempt=0; "
            'until mkdir -m 3770 -- "$collection" 2>/dev/null '
            '|| test -n "$(find "$collection" -maxdepth 0 -type d -uid "$me" -gid "$group")"; '
            "do "
            'attempt=$((attempt + 1)); test "$attempt" -le 3; '
            'mv -T -- "$collection" "$collection.refused-$$-$attempt"; '
            "done; "
            'chmod 3770 -- "$collection"; '
            "done"
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script, "sh", CHAT_STATE_DIR, *PRIVATE_MEMBER_HOME_COLLECTIONS],
            workdir="/",
            timeout_s=60,
        )
        if result.returncode != 0:
            raise SandboxError(
                "private member home collections could not be secured: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def normalize_member_files(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_id: int,
        user_uid: int,
        user_gid: int,
    ) -> None:
        """Expose one member's credential-free workspace/state writes to peers."""
        if not 0 <= slot_id < spec.resident_slot_capacity:
            raise ValueError("resident slot is outside this shared sandbox")
        if user_uid != MEMBER_UID_BASE + slot_id:
            raise ValueError("resident slot does not match its member uid")
        # Codex, OpenCode, and Kilo each need a member-private provider home:
        # Codex can make temp files owner-only, while the HTTP runtimes keep
        # SQLite databases and per-project process locks. Each home is mode
        # 0700 to its member UID and is never widened, not even by its owner:
        # its config and plugins run as that member, with that member's
        # credentials, at every process start. Fork copies a child's home
        # through a pipe between the two UIDs, so no sibling needs to read it.
        # Claude now loads settings and its sessions/ process registry from
        # /home/agent-member-N/.claude-members, outside this traversal. Only projects/
        # remains shared; never widen other legacy shared Claude files.
        # Normalize shared roots while pruning every private-home collection.
        trusted_prunes = "".join(f"-o -path {path} " for path in PRIVATE_MEMBER_HOME_COLLECTIONS)
        script = (
            "set -eu; uid=$1; "
            'normalize_dirs() { find "$@" -xdev -uid "$uid" -type d '
            "-exec chmod g+rwx,g+s -- {} +; }; "
            'normalize_files() { find "$@" -xdev -uid "$uid" -type f '
            "-exec chmod g+rw -- {} +; }; "
            f"normalize_dirs {CHAT_WORKDIR}; normalize_files {CHAT_WORKDIR}; "
            f"find {CHAT_STATE_DIR} -xdev "
            f"\\( -path {CHAT_STATE_DIR}/.platform "
            f"-o -path {CHAT_STATE_DIR}/claude "
            f"-o -path {CHAT_STATE_DIR}/codex-members "
            f"-o -path {CHAT_STATE_DIR}/opencode-members "
            f"-o -path {CHAT_STATE_DIR}/kilo-members "
            f"{trusted_prunes}\\) -prune -o "
            '-uid "$uid" -type d -exec chmod g+rwx,g+s -- {} +; '
            f"find {CHAT_STATE_DIR} -xdev "
            f"\\( -path {CHAT_STATE_DIR}/.platform "
            f"-o -path {CHAT_STATE_DIR}/claude "
            f"-o -path {CHAT_STATE_DIR}/codex-members "
            f"-o -path {CHAT_STATE_DIR}/opencode-members "
            f"-o -path {CHAT_STATE_DIR}/kilo-members "
            f"{trusted_prunes}\\) -prune -o "
            '-uid "$uid" -type f -exec chmod g+rw -- {} +; '
            f"if test -d {CLAUDE_SHARED_PROJECTS_DIR}; then "
            f"normalize_dirs {CLAUDE_SHARED_PROJECTS_DIR}; "
            f"normalize_files {CLAUDE_SHARED_PROJECTS_DIR}; fi"
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script, "sh", str(user_uid)],
            workdir="/",
            timeout_s=120,
            user_uid=user_uid,
            user_gid=user_gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "shared member permission normalization failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    @staticmethod
    def _shared_writer_task_state_script() -> str:
        """Classify a complete thread group, failing closed on census races.

        A zombie group leader may still have live worker threads. Check every
        task and match both surrounding thread counts. Exited nonleaders make
        the census ambiguous: another task may replace them without changing
        the count. Only a sole dead leader proves the group has exited.
        """
        return (
            "writer_state() { proc=$1; seen=0; live=0; stopped=1; "
            'threads=$(sed -n "s/^Threads:[[:space:]]*//p" "$proc/status" 2>/dev/null || :); '
            'case "$threads" in ""|0|*[!0-9]*) printf "%s" unknown; return;; esac; '
            'for task in "$proc"/task/[0-9]*; do '
            'raw=$(cat "$task/stat" 2>/dev/null || :); fields=${raw##*) }; '
            'state=$(set -- $fields; printf "%s" "${1:-}"); '
            'case "$state" in Z|X|x) '
            'if [ "${task##*/}" != "${proc##*/}" ]; then '
            'printf "%s" unknown; return; fi;; T|t) live=1;; '
            '"") printf "%s" unknown; return;; *) live=1; stopped=0;; esac; '
            "seen=$((seen + 1)); done; "
            'current_threads=$(sed -n "s/^Threads:[[:space:]]*//p" '
            '"$proc/status" 2>/dev/null || :); '
            'if [ "$threads" != "$seen" ] || [ "$current_threads" != "$seen" ]; then '
            'printf "%s" unknown; elif [ "$live" = 0 ]; then '
            'if [ "$seen" = 1 ]; then printf "%s" exited; else printf "%s" unknown; fi; '
            'elif [ "$stopped" = 1 ]; then printf "%s" stopped; '
            'else printf "%s" running; fi; }; '
        )

    def _operation_writer_uids(self, spec: ChatSandboxSpec) -> builtins.list[int]:
        """Enumerate active operation identities without reading their environments."""
        script = """import os
# AGENT_OPERATION_UID_CENSUS
uids=set()
for name in os.listdir('/proc'):
 if not name.isdigit(): continue
 try: uids.add(os.stat('/proc/'+name).st_uid)
 except FileNotFoundError: pass
print(' '.join(str(uid) for uid in sorted(uids) if uid in (30000,30001,30002) or 31000<=uid<31064))
"""
        result = self.exec_capture(spec, ["python3", "-c", script], workdir="/", timeout_s=10)
        if result.returncode:
            raise SandboxError("Environment writer census could not be verified")
        try:
            uids = [int(value) for value in result.stdout.decode().split()]
        except ValueError:
            raise SandboxError("Invalid environment writer census") from None
        if any(uid not in (*ENVIRONMENT_UIDS, *ACTION_UIDS) for uid in uids):
            raise SandboxError("Unexpected environment writer identity")
        return uids

    def freeze_shared_writers(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_ids: builtins.list[int],
        token: str,
        expected_container_id: str | None = None,
    ) -> str:
        """Stop every existing shared-workspace writer for one short capture.

        Agent processes use per-member UIDs, while terminals/setup commands
        use the sandbox UID.  Freezing the complete existing UID census closes
        the detached-child gap that a process-group-only barrier leaves.  The
        Git capture exec starts after this census and is the sole new writer;
        admissions remain blocked by the supervisor until thaw completes.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ValueError("snapshot freeze token is invalid")
        unique_slots = sorted(set(slot_ids))
        if any(slot < 0 or slot >= spec.resident_slot_capacity for slot in unique_slots):
            raise ValueError("snapshot member slot is outside sandbox capacity")
        if expected_container_id is not None:
            self._require_exact(spec, expected_container_id)
        manifest = f"{ACTIVATION_TMPFS}/snapshot-{token}.pids"
        target_uids = [
            self.backend.uid,
            *self._operation_writer_uids(spec),
            *(MEMBER_UID_BASE + slot for slot in unique_slots),
        ]
        # The container drops every capability.  Even UID 0 therefore cannot
        # signal a process owned by a member UID; CAP_KILL is intentionally not
        # added back.  Run one census as each identity instead.  Repeating the
        # census until every observed process is stopped closes the race where
        # an idle CLI background process forks while /proc is being scanned.
        # Exclude only fully exited thread groups: a zombie leader can retain
        # live workers, and the leader stays Z even when SIGSTOP stops them.
        script = self._shared_writer_task_state_script() + (
            'set -eu; expected_uid="$1"; committed=0; receipt=""; '
            "resume_receipt() { "
            '[ "$committed" = 0 ] || return 0; '
            "for item in $receipt; do pid=${item%%:*}; started=${item#*:}; "
            'uid=$(stat -c %u "/proc/$pid" 2>/dev/null || :); '
            'raw=$(cat "/proc/$pid/stat" 2>/dev/null || :); fields=${raw##*) }; '
            'current=$(set -- $fields; printf "%s" "${20:-}"); '
            '[ "$uid" = "$expected_uid" ] && [ "$current" = "$started" ] '
            '&& kill -CONT "$pid" 2>/dev/null || :; done; }; '
            "trap resume_receipt EXIT; trap 'exit 46' HUP INT TERM; round=0; "
            "while :; do round=$((round + 1)); stable=1; "
            "for proc in /proc/[0-9]*; do pid=${proc##*/}; "
            '[ "$pid" != 1 ] && [ "$pid" != "$$" ] || continue; '
            'uid=$(stat -c %u "$proc" 2>/dev/null || :); '
            '[ "$uid" = "$expected_uid" ] || continue; '
            'raw=$(cat "$proc/stat" 2>/dev/null || :); fields=${raw##*) }; '
            'state=$(writer_state "$proc"); [ "$state" != exited ] || continue; '
            'started=$(set -- $fields; printf "%s" "${20:-}"); '
            'case "$started" in ""|*[!0-9]*) continue;; esac; item="$pid:$started"; '
            'case " $receipt " in *" $item "*) '
            'case "$state" in stopped) ;; *) kill -STOP "$pid" 2>/dev/null || continue; '
            'stable=0;; esac;; *) kill -STOP "$pid" 2>/dev/null || continue; '
            'receipt="$receipt $item"; stable=0;; esac; done; '
            '[ "$stable" = 1 ] && break; [ "$round" -lt 64 ] || exit 45; done; '
            "for item in $receipt; do pid=${item%%:*}; started=${item#*:}; "
            'printf "%s %s %s\\n" "$pid" "$expected_uid" "$started"; done; '
            "committed=1"
        )
        frozen: builtins.list[tuple[int, int, int]] = []
        try:
            for uid in target_uids:
                result = self.exec_capture(
                    spec,
                    ["/bin/sh", "-c", script, "sh", str(uid)],
                    workdir="/",
                    timeout_s=20,
                    user_uid=uid,
                    user_gid=self.backend.gid,
                    expected_container_id=expected_container_id,
                )
                if result.returncode != 0:
                    raise SandboxError(
                        "shared snapshot writer freeze failed: "
                        + result.stderr.decode("utf-8", "replace")[:300]
                    )
                frozen.extend(self._parse_frozen_processes(result.stdout, allowed_uids={uid}))
            if len({pid for pid, _uid, _started in frozen}) != len(frozen):
                raise SandboxError("shared snapshot writer census returned a duplicate pid")
            payload = self._encode_frozen_processes(frozen)
            write_script = (
                'set -eu; manifest="$1"; temporary="$manifest.tmp.$$"; umask 077; '
                "trap 'rm -f -- \"$temporary\"' EXIT HUP INT TERM; "
                'cat > "$temporary"; chmod 600 "$temporary"; mv "$temporary" "$manifest"; '
                "trap - EXIT HUP INT TERM"
            )
            written = self.exec_capture(
                spec,
                ["/bin/sh", "-c", write_script, "sh", manifest],
                workdir="/",
                timeout_s=20,
                input_bytes=payload,
                user_uid=self.backend.uid,
                user_gid=self.backend.gid,
                expected_container_id=expected_container_id,
            )
            if written.returncode != 0:
                raise SandboxError(
                    "shared snapshot writer manifest failed: "
                    + written.stderr.decode("utf-8", "replace")[:300]
                )
            if expected_container_id is not None:
                self._require_exact(spec, expected_container_id)
        except BaseException:
            with contextlib.suppress(Exception):
                self._resume_frozen_processes(
                    spec,
                    target_uids=target_uids,
                    frozen=frozen,
                    require_exact_census=False,
                    expected_container_id=expected_container_id,
                )
            raise
        return manifest

    def thaw_shared_writers(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_ids: builtins.list[int],
        manifest: str,
        require_exact_census: bool = True,
        expected_container_id: str | None = None,
    ) -> None:
        """Resume exactly the frozen PID/start pairs and detect new writers."""
        if not re.fullmatch(
            rf"{re.escape(ACTIVATION_TMPFS)}/snapshot-[0-9a-f]{{32}}\.pids", manifest
        ):
            raise ValueError("snapshot writer manifest path is invalid")
        unique_slots = sorted(set(slot_ids))
        if any(slot < 0 or slot >= spec.resident_slot_capacity for slot in unique_slots):
            raise ValueError("snapshot member slot is outside sandbox capacity")
        if expected_container_id is not None:
            self._require_exact(spec, expected_container_id)
        target_uids = [
            self.backend.uid,
            *self._operation_writer_uids(spec),
            *(MEMBER_UID_BASE + slot for slot in unique_slots),
        ]
        read = self.exec_capture(
            spec,
            ["/bin/sh", "-c", 'cat -- "$1"', "sh", manifest],
            workdir="/",
            timeout_s=20,
            user_uid=self.backend.uid,
            user_gid=self.backend.gid,
            expected_container_id=expected_container_id,
        )
        if read.returncode != 0:
            raise SandboxError(
                "shared snapshot writer manifest is unavailable: "
                + read.stderr.decode("utf-8", "replace")[:300]
            )
        frozen = self._parse_frozen_processes(
            read.stdout, allowed_uids={*target_uids, *ENVIRONMENT_UIDS, *ACTION_UIDS}
        )
        target_uids = sorted({*target_uids, *(uid for _, uid, _ in frozen)})
        resume_error: BaseException | None = None
        try:
            self._resume_frozen_processes(
                spec,
                target_uids=target_uids,
                frozen=frozen,
                require_exact_census=require_exact_census,
                expected_container_id=expected_container_id,
            )
        except BaseException as exc:
            resume_error = exc
        removed = self.exec_capture(
            spec,
            ["/bin/sh", "-c", 'rm -f -- "$1"', "sh", manifest],
            workdir="/",
            timeout_s=20,
            user_uid=self.backend.uid,
            user_gid=self.backend.gid,
            expected_container_id=expected_container_id,
        )
        if resume_error is not None:
            raise resume_error
        if removed.returncode != 0:
            raise SandboxError(
                "shared snapshot writer manifest cleanup failed: "
                + removed.stderr.decode("utf-8", "replace")[:300]
            )
        if expected_container_id is not None:
            self._require_exact(spec, expected_container_id)

    @staticmethod
    def _parse_frozen_processes(
        payload: bytes,
        *,
        allowed_uids: set[int],
    ) -> builtins.list[tuple[int, int, int]]:
        """Validate the private PID/start-time receipt before any signal."""
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SandboxError("shared snapshot writer census is not ASCII") from exc
        frozen: builtins.list[tuple[int, int, int]] = []
        seen: set[int] = set()
        for line in text.splitlines():
            fields = line.split()
            if len(fields) != 3 or any(
                not field.isascii() or not field.isdigit() for field in fields
            ):
                raise SandboxError("shared snapshot writer census is malformed")
            pid, uid, started = (int(field) for field in fields)
            if pid <= 1 or started <= 0 or uid not in allowed_uids or pid in seen:
                raise SandboxError("shared snapshot writer census is outside its identity fence")
            seen.add(pid)
            frozen.append((pid, uid, started))
        return frozen

    @staticmethod
    def _encode_frozen_processes(frozen: builtins.list[tuple[int, int, int]]) -> bytes:
        """Encode a deterministic, private manifest for the later thaw."""
        return "".join(f"{pid} {uid} {started}\n" for pid, uid, started in sorted(frozen)).encode(
            "ascii"
        )

    def _resume_frozen_processes(
        self,
        spec: ChatSandboxSpec,
        *,
        target_uids: builtins.list[int],
        frozen: builtins.list[tuple[int, int, int]],
        require_exact_census: bool,
        expected_container_id: str | None = None,
    ) -> None:
        """Resume each receipt as its owner and report any changed census."""
        by_uid = {
            uid: [(pid, started) for pid, owner, started in frozen if owner == uid]
            for uid in target_uids
        }
        script = self._shared_writer_task_state_script() + (
            'set -u; exact="$1"; expected_uid="$2"; shift 2; unknown=0; '
            'if [ "$exact" = 1 ]; then for proc in /proc/[0-9]*; do '
            'pid=${proc##*/}; [ "$pid" != 1 ] && [ "$pid" != "$$" ] || continue; '
            'uid=$(stat -c %u "$proc" 2>/dev/null || :); '
            '[ "$uid" = "$expected_uid" ] || continue; '
            'raw=$(cat "$proc/stat" 2>/dev/null || :); fields=${raw##*) }; '
            'state=$(writer_state "$proc"); [ "$state" != exited ] || continue; '
            'started=$(set -- $fields; printf "%s" "${20:-}"); found=0; '
            'for item in "$@"; do [ "$item" = "$pid:$started" ] && found=1; done; '
            '[ "$found" = 1 ] || unknown=1; done; fi; '
            'for item in "$@"; do pid=${item%%:*}; started=${item#*:}; '
            'uid=$(stat -c %u "/proc/$pid" 2>/dev/null || :); '
            'raw=$(cat "/proc/$pid/stat" 2>/dev/null || :); fields=${raw##*) }; '
            'current=$(set -- $fields; printf "%s" "${20:-}"); '
            '[ "$uid" = "$expected_uid" ] && [ "$current" = "$started" ] '
            '&& kill -CONT "$pid" 2>/dev/null || :; done; [ "$unknown" = 0 ]'
        )
        errors: builtins.list[str] = []
        for uid in target_uids:
            expected = [f"{pid}:{started}" for pid, started in by_uid[uid]]
            try:
                result = self.exec_capture(
                    spec,
                    [
                        "/bin/sh",
                        "-c",
                        script,
                        "sh",
                        "1" if require_exact_census else "0",
                        str(uid),
                        *expected,
                    ],
                    workdir="/",
                    timeout_s=20,
                    user_uid=uid,
                    user_gid=self.backend.gid,
                    expected_container_id=expected_container_id,
                )
            except BaseException as exc:
                errors.append(str(exc))
                continue
            if result.returncode != 0:
                errors.append(result.stderr.decode("utf-8", "replace")[:300])
        if errors:
            detail = next((error for error in errors if error), "writer census changed")
            raise SandboxError("shared snapshot writer census changed during capture: " + detail)

    def kill_member_process(
        self,
        spec: ChatSandboxSpec,
        *,
        pidfile: str,
        user_uid: int,
        user_gid: int,
    ) -> None:
        """Signal one verified resident or command process group, never the container."""
        self._validate_member_pidfile(spec, pidfile, user_uid=user_uid)
        argv = [
            "/bin/sh",
            "-c",
            self._member_reaper_script('"$2"'),
            "sh",
            str(user_uid),
            pidfile,
        ]
        result = (
            self._kata_privileged_exec(spec, argv, timeout_s=20)
            if self.backend.runtime == KATA_RUNTIME
            else self.exec_capture(
                spec,
                argv,
                workdir="/",
                timeout_s=20,
                user_uid=user_uid,
                user_gid=user_gid,
            )
        )
        if result.returncode != 0:
            stderr = (
                result.stderr.decode("utf-8", "replace")
                if isinstance(result.stderr, bytes)
                else str(result.stderr or "")
            )
            raise _member_kill_failure(result.returncode, stderr)

    @staticmethod
    def _member_reaper_script(pidfile_expression: str) -> str:
        """Build an exact PID/start receipt wait, verification, and group kill."""
        return (
            'set -eu; expected_uid="$1"; '
            # kill -0 sees zombies too. Docker's init normally reaps them,
            # but a delayed reap is not a surviving command. Examine state
            # before declaring cleanup failed, without requiring procps.
            'group_alive() { kill -0 "-$pid" 2>/dev/null || return 1; '
            "for task in /proc/[0-9]*/stat; do "
            'read -r raw < "$task" 2>/dev/null || continue; '
            'set -- ${raw##*) }; [ "${3:-}" = "$pid" ] || continue; '
            'case "${1:-}" in Z|X) continue;; esac; return 0; '
            "done; return 1; }; "
            f"for pidfile in {pidfile_expression}; do "
            'attempt=0; ready=0; pid=""; started=""; '
            'while [ "$attempt" -lt 40 ]; do '
            'receipt=$(cat -- "$pidfile" 2>/dev/null || :); set -- $receipt; '
            'if [ "$#" -eq 2 ]; then pid="$1"; started="$2"; '
            'case "$pid" in ""|*[!0-9]*) ;; *) case "$started" in '
            '""|*[!0-9]*) ;; *) ready=1; break;; esac;; esac; fi; '
            "attempt=$((attempt + 1)); sleep 0.05; done; "
            '[ "$ready" = 1 ] || exit 125; '
            '[ "$(stat -c %u "/proc/$pid" 2>/dev/null || :)" = "$expected_uid" ] '
            '|| { rm -f -- "$pidfile"; continue; }; '
            'fields=$(sed "s/^[^)]*) //" "/proc/$pid/stat" 2>/dev/null || :); '
            "set -- $fields; current_pgid=${3:-}; current_started=${20:-}; "
            '[ "$current_pgid" = "$pid" ] && [ "$current_started" = "$started" ] '
            '|| { rm -f -- "$pidfile"; exit 126; }; '
            'kill -TERM "-$pid" 2>/dev/null || :; attempt=0; '
            "while group_alive; do attempt=$((attempt + 1)); "
            '[ "$attempt" -lt 40 ] || { kill -KILL "-$pid" 2>/dev/null || :; '
            "attempt=0; while group_alive; do "
            'attempt=$((attempt + 1)); [ "$attempt" -lt 100 ] || exit 124; '
            "sleep 0.05; done; break; }; "
            'sleep 0.05; done; rm -f -- "$pidfile"; '
            "done"
        )

    def spawn_terminal(
        self,
        spec: ChatSandboxSpec,
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        rows: int,
        cols: int,
        argv: builtins.list[str] | None = None,
        user_uid: int | None = None,
    ) -> TerminalProcess:
        """Attach a PTY to one shell without creating or killing a container."""
        current = self._require(spec)
        if not current.running:
            raise SandboxError("chat sandbox must be started before terminal exec")
        token = secrets.token_hex(16)
        if user_uid is not None:
            from cloud_agent_host.environment_process import (
                prepare_home,
                prepare_tool_home,
                tool_environment,
            )

            directory = prepare_home(self, spec, user_uid)
            pidfile = f"{directory}/terminal-{token}.pid"
            env = {
                **tool_environment(),
                **(env or {}),
                "HOME": prepare_tool_home(self, spec, user_uid),
            }
        else:
            pidfile = f"{ACTIVATION_TMPFS}/terminal-{token}.pid"
        rcfile = _terminal_rcfile(pidfile)
        # The session shell gets its own process group.  Sandbox Manager suspend/kill
        # then signals this terminal tree only; pausing or killing the retained
        # container would also stop the Agent CLI and violate one-chat
        # lifecycle ownership.
        #
        # Docker's TTY exec already starts this wrapper as the foreground
        # session/process-group leader, and the pidfile names that group.
        # Every shell command has to stay in it: a background shell observes
        # /dev/null, while another ``setsid`` loses this exec's controlling
        # terminal.
        #
        # bash (when the image has it) gives the owner line editing, history
        # and completion. An interactive bash always turns job control on and
        # makes itself a process-group leader, ignoring ``+m``, so it replaces
        # the wrapper rather than running under it: it then leads the group the
        # pidfile already names. Its rcfile turns job control back off before
        # every prompt -- bash restores it after reading startup files -- so the
        # commands it runs join that group too.
        #
        # Nothing in this group can resume a stopped job, so Ctrl-Z must not
        # stop one: an ignored SIGTSTP is inherited by every command. The ``sh``
        # fallback keeps its wrapper for the pidfile cleanup, which therefore
        # has to outlive the Ctrl-C and Ctrl-\ that reach the whole group.
        script = (
            'pidfile="$1"; rcfile="$2"; '
            'printf "%s\\n" "$$" > "$pidfile" || exit 126; '
            "umask 0002; "
            "trap '' TSTP TTIN TTOU; "
            'if [ "$#" -gt 3 ]; then shift 3; exec "$@"; fi; '
            "if command -v bash >/dev/null 2>&1; then "
            'if printf "%s" "$3" > "$rcfile"; then '
            "SHELL=$(command -v bash); export SHELL; "
            'exec bash --rcfile "$rcfile" -i; fi; fi; '
            'rm -f "$rcfile"; '
            'cleanup() { rm -f "$pidfile"; }; '
            "trap 'cleanup' EXIT; "
            "trap : INT QUIT; "
            '/bin/sh +m -i; status=$?; exit "$status"'
        )
        command = self.build_exec_command(
            spec,
            [
                "/bin/sh",
                "-c",
                script,
                "sh",
                pidfile,
                rcfile,
                _terminal_bashrc(pidfile, rcfile),
                *(argv or []),
            ],
            env=env,
            workdir=workdir,
            interactive=True,
            tty=True,
            user_uid=user_uid,
            user_gid=self.backend.gid if user_uid is not None else None,
        )
        master_fd, slave_fd = os.openpty()
        try:
            _set_pty_size(slave_fd, rows, cols)
            tty.setraw(slave_fd)
            # All three streams share the PTY. The Docker CLI reads the window
            # size from its *stdout*: with a pipe there it sends the exec no
            # console size and drops every SIGWINCH, so the shell inside never
            # learns the browser's dimensions.
            process = subprocess.Popen(
                command,
                env=self.exec_client_env(env),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                bufsize=0,
                start_new_session=True,
            )
        except Exception:
            with contextlib.suppress(OSError):
                os.close(master_fd)
            raise
        finally:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        return _RetainedTerminalProcess(
            process,
            manager=self,
            spec=spec,
            pidfile=pidfile,
            input_fd=master_fd,
            operation_uid=user_uid,
        )

    def reap_stale_terminal_processes(self, spec: ChatSandboxSpec) -> None:
        """Remove PTYs whose sandbox manager client disappeared before graceful close.

        Interactive terminal streams are intentionally ephemeral and cannot
        be reattached after a sandbox manager process restart.  The retained container
        is durable, however, and Docker may leave an exec process alive after
        its attaching client dies.  Match both the unguessable pidfile and the
        exact argv before signalling, so a stale/reused numeric PID can never
        target the Agent CLI or PID 1.  The ``sh`` wrapper's argv carries the
        pidfile; a bash terminal's carries its rcfile, named after the pidfile.
        """
        current = self._require(spec)
        if not current.running:
            return
        script = (
            "set -eu; command -v tr >/dev/null; command -v grep >/dev/null; "
            f"for pidfile in {ACTIVATION_TMPFS}/terminal-*.pid; do "
            '[ -e "$pidfile" ] || continue; '
            'rcfile="${pidfile%.pid}.bashrc"; '
            'pid=$(cat "$pidfile" 2>/dev/null || :); '
            'case "$pid" in ""|*[!0-9]*) rm -f "$pidfile" "$rcfile"; continue;; esac; '
            'if [ -r "/proc/$pid/cmdline" ] '
            '&& tr "\\000" "\\n" < "/proc/$pid/cmdline" '
            '| grep -Fqx -e "$pidfile" -e "$rcfile"; then '
            'kill -CONT -"$pid" 2>/dev/null || :; '
            'kill -KILL -"$pid" 2>/dev/null || :; '
            'attempt=0; while kill -0 -"$pid" 2>/dev/null; do '
            'attempt=$((attempt + 1)); test "$attempt" -lt 100 || exit 124; '
            "sleep 0.05; done; "
            'fi; rm -f "$pidfile" "$rcfile"; done'
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script],
            workdir="/",
            timeout_s=15,
        )
        if result.returncode != 0:
            raise SandboxError(
                "stale retained terminal cleanup failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def exec_capture(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        timeout_s: float = 60,
        input_bytes: bytes | None = None,
        user_uid: int | None = None,
        user_gid: int | None = None,
        observer: StageObserver | None = None,
        expected_container_id: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one bounded command and capture bytes without opening a host path.

        ``observer`` times the exec as one stage covering the attach, the
        process start and the command's own run. That is a combination and is
        reported as one: it is never presented as pure disk-read time, and
        splitting it further would need the exec'd helper to report its own
        sub-durations rather than a second exec to probe them.
        """
        current = (
            self._require_exact(spec, expected_container_id)
            if expected_container_id is not None
            else self._require(spec)
        )
        if not current.running:
            raise SandboxError("chat sandbox must be started before exec")
        interactive = input_bytes is not None
        if self.backend.runtime == KATA_RUNTIME and not input_bytes:
            # Attaching and immediately closing an empty stdin can wedge the
            # Kata shim. build_exec_command supplies /dev/null in this case.
            interactive = False
        command = self.build_exec_command(
            spec,
            argv,
            env=env,
            workdir=workdir,
            interactive=interactive,
            user_uid=user_uid,
            user_gid=user_gid,
            container_id=current.container_id,
        )
        try:
            with substage(observer, "exec"):
                return subprocess.run(
                    command,
                    env=self.exec_client_env(env),
                    input=input_bytes,
                    capture_output=True,
                    timeout=timeout_s,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"chat sandbox exec failed: {exc}") from exc

    def pipe_member_exec(
        self,
        spec: ChatSandboxSpec,
        *,
        source_argv: builtins.list[str],
        source_uid: int,
        target_argv: builtins.list[str],
        target_uid: int,
        user_gid: int,
        timeout_s: float,
    ) -> tuple[subprocess.CompletedProcess[bytes], subprocess.CompletedProcess[bytes]]:
        """Stream one member exec's stdout into another member exec's stdin.

        The two commands meet only in a pipe on this host, so a file private to
        one member UID can reach another member without being widened inside
        the container or staged where a sibling could read it. Both run in the
        one inspected container. Returns ``(source, target)`` results carrying
        each command's bounded stderr tail; the target's stdout is discarded.
        """
        current = self._require(spec)
        if not current.running:
            raise SandboxError("chat sandbox must be started before exec")
        commands = (
            self.build_exec_command(
                spec,
                source_argv,
                workdir="/",
                user_uid=source_uid,
                user_gid=user_gid,
                container_id=current.container_id,
            ),
            self.build_exec_command(
                spec,
                target_argv,
                workdir="/",
                interactive=True,
                user_uid=target_uid,
                user_gid=user_gid,
                container_id=current.container_id,
            ),
        )
        deadline = time.monotonic() + timeout_s
        processes: builtins.list[subprocess.Popen[bytes]] = []
        with (
            tempfile.TemporaryFile(prefix="freeinference-member-pipe-") as source_stderr,
            tempfile.TemporaryFile(prefix="freeinference-member-pipe-") as target_stderr,
        ):
            try:
                source = subprocess.Popen(
                    commands[0],
                    env=self.exec_client_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=source_stderr,
                )
                processes.append(source)
                assert source.stdout is not None
                try:
                    processes.append(
                        subprocess.Popen(
                            commands[1],
                            env=self.exec_client_env(),
                            stdin=source.stdout,
                            stdout=subprocess.DEVNULL,
                            stderr=target_stderr,
                        )
                    )
                finally:
                    # The target owns the read end now; a target that exits
                    # early ends the source on a broken pipe.
                    source.stdout.close()
                codes = [
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                    for process in reversed(processes)
                ]
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SandboxError(f"chat sandbox member pipe failed: {exc}") from exc
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            process.wait(timeout=5)
            results = []
            for command, code, stderr in zip(
                commands, reversed(codes), (source_stderr, target_stderr), strict=True
            ):
                stderr.seek(max(0, stderr.seek(0, os.SEEK_END) - 4096))
                results.append(subprocess.CompletedProcess(command, code, b"", stderr.read()))
        return results[0], results[1]

    def _kata_privileged_exec(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        detach: bool = False,
        timeout_s: float = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Run one source-controlled platform helper as Kata guest root.

        This is deliberately not a general privileged-exec API.  Callers pass
        only source-controlled observer/cleanup argv from private methods, and
        the runtime guard prevents them from gaining privilege on the host's
        shared kernel under runc.
        """
        if self.backend.runtime != KATA_RUNTIME:
            raise SandboxError("guest-root platform helpers require the Kata runtime")
        current = self._require(spec)
        if current.sandbox_runtime != KATA_RUNTIME:
            raise SandboxError("guest-root platform helpers require an inspected Kata container")
        if not current.running:
            raise SandboxError("chat sandbox must be started before guest observation")
        command = [self.backend.docker_binary, "exec"]
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
        return self._run(command, timeout=timeout_s)

    def probe_guest_observer(self) -> str | None:
        """Prove the guest kernel, retained exec isolation and real eBPF delivery.

        The observation stages are retried because they race a guest that is
        still starting. Everything before them is a property of the image and
        the kernel: it answers the same way every time, so it is raised on the
        first attempt rather than being asked three times on a host that is
        genuinely missing a capability.

        A rollout hangs on the answer. `cmd_up` invalidates this host's
        shared-live marker and drains the runners that were serving before it
        runs this, and completes the marker only once every replica passes, so
        a probe that fails leaves the host advertising no deployment at all --
        and a runner that advertises no deployment does not poll. One lost race
        in a freshly booted microVM took staging off the air for 29.6 hours on
        2026-09-13, because nothing retried and nothing else deployed until the
        next day.
        """
        if self.backend.runtime != KATA_RUNTIME:
            return None
        attempts = 0
        while True:
            attempts += 1
            try:
                return self._probe_guest_observer_once()
            except _ObserverProbeRace as exc:
                if attempts >= GUEST_OBSERVER_PROBE_ATTEMPTS:
                    raise SandboxError(
                        f"{exc} (after {attempts} attempts on a fresh sandbox each time)"
                    ) from exc
                logger.warning(
                    "Kata observer probe lost a race with its own guest; retrying: %s",
                    exc,
                    extra={"event": "guest_observer_probe_retry", "attempt": attempts},
                )
                time.sleep(GUEST_OBSERVER_PROBE_RETRY_S)

    def _remove_abandoned_probes(self) -> None:
        """Remove this namespace's probes left behind by a process killed mid-probe.

        Best effort: a listing that fails leaves them for the next probe, and
        the probe about to run reports a Docker that is genuinely unusable.
        """
        listed = self._run(
            [
                self.backend.docker_binary,
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                f"label={CHAT_PROBE_LABEL}",
                "--format",
                '{"id":{{json .ID}},"namespace":{{json (.Label "'
                + CHAT_NAMESPACE_LABEL
                + '")}},"created":{{json (.Label "'
                + CHAT_PROBE_LABEL
                + '")}}}',
            ],
            timeout=30,
        )
        if listed.returncode != 0:
            return
        abandoned_before = time.time() - PROBE_ABANDONED_AFTER_S
        for line in listed.stdout.splitlines():
            try:
                row = json.loads(line)
                container_id = row["id"]
                if (
                    not isinstance(container_id, str)
                    or not container_id
                    or row["namespace"] != self.backend.sandbox_namespace
                    or int(row["created"]) > abandoned_before
                ):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            self._run([self.backend.docker_binary, "rm", "--force", container_id], timeout=30)

    def _probe_guest_observer_once(self) -> str:
        """One probe, on a sandbox of its own that is removed however it ends."""
        self._remove_abandoned_probes()
        token = secrets.token_hex(12)
        spec = ChatSandboxSpec(
            thread_id=f"athr_preflight_{token}",
            sandbox_id=f"asbx_preflight_{token}",
            generation=1,
            runtime_id="preflight",
            resident_slot_capacity=2,
            labels={CHAT_PROBE_LABEL: str(int(time.time()))},
        )
        command = self.build_create_command(spec)
        # Use the production creation/exec flags, without attaching any relay
        # or touching retained user state. No repository or credential enters.
        command[command.index("--network") + 1] = "none"
        created = self._run(command, timeout=120)
        if created.returncode != 0:
            raise SandboxError("Kata observer probe creation failed: " + created.stderr[:300])
        try:
            started = self._run(
                [self.backend.docker_binary, "start", spec.container_name], timeout=120
            )
            if started.returncode != 0:
                raise SandboxError("Kata observer probe start failed: " + started.stderr[:300])
            isolation = self.exec_capture(
                spec,
                [
                    "python3",
                    "-c",
                    "import json, os; "
                    "s=dict(line.split(':',1) for line in open('/proc/self/status') "
                    "if ':' in line); "
                    "print(json.dumps(dict(kernel=os.uname().release, uid=os.getuid(), "
                    "nnp=int(s['NoNewPrivs']), seccomp=int(s['Seccomp']), "
                    "effective=int(s['CapEff'],16), permitted=int(s['CapPrm'],16))))",
                ],
                workdir="/",
                timeout_s=30,
            )
            try:
                observed = json.loads(isolation.stdout)
            except (ValueError, TypeError) as exc:
                raise SandboxError(
                    "Kata observer probe could not inspect member isolation"
                ) from exc
            if not isinstance(observed, dict):
                raise SandboxError("Kata observer probe could not inspect member isolation")
            guest_kernel = self.backend.validate_guest_kernel(observed.pop("kernel", None))
            # validate_guest_kernel refuses a shared kernel for Kata; keep
            # that requirement explicit before any guest-root helper runs.
            if guest_kernel is None:
                raise SandboxError("Kata observer probe returned no guest kernel")
            if (
                isolation.returncode != 0
                or observed
                != {
                    "uid": self.backend.uid,
                    "nnp": 1,
                    "seccomp": 2,
                    "effective": 0,
                    "permitted": 0,
                }
                or self.backend.uid == 0
            ):
                raise SandboxError("Kata member exec lacks non-root, seccomp or no-new-privileges")
            cleanup = self.exec_capture(
                spec,
                [MEMBER_REAPER_TARGET, "reap", str(MEMBER_UID_BASE)],
                workdir="/",
                input_bytes=b"",
                user_uid=MEMBER_UID_BASE,
                user_gid=self.backend.gid,
                timeout_s=30,
            )
            if cleanup.returncode != 0:
                raise SandboxError(
                    "Kata member process cleanup self-test failed: "
                    + cleanup.stderr.decode("utf-8", "replace")[:300]
                )
            observer = self._kata_privileged_exec(
                spec, [GUEST_OBSERVER_BINARY, "--self-test"], timeout_s=30
            )
            if observer.returncode != 0:
                raise SandboxError("Kata guest observer self-test failed: " + observer.stderr[:500])
            self._check_observer_self_test(observer.stdout)
            marker = self.exec_capture(
                spec,
                ["/usr/local/libexec/freeinference-tool-mark", "--self-test"],
                workdir="/",
                timeout_s=30,
            )
            if marker.returncode != 0 or marker.stdout.strip() != b"freeinference-tool-mark-v1":
                raise SandboxError("Kata tool marker self-test failed")
            cursor = self.begin_guest_observation(spec, user_uid=self.backend.uid)
            if not cursor.available:
                # `begin` folds every reason into an unavailable cursor, so
                # without its detail this reads the same whether the observer
                # died, answered a version this host rejects, or had simply not
                # published its first status yet.
                raise _ObserverProbeRace(
                    "Kata tool marker transport observer is unavailable: "
                    + (cursor.detail or "no detail reported")
                )
            for phase in ("pre", "post"):
                sent = self.exec_capture(
                    spec,
                    [
                        "/usr/local/libexec/freeinference-tool-mark",
                        phase,
                        "/agent-state/tool-marks.log",
                    ],
                    input_bytes=b'{"tool_use_id":"preflight"}',
                    workdir="/",
                    timeout_s=30,
                )
                if sent.returncode != 0:
                    raise SandboxError("Kata tool marker transport helper failed")
            observation = self.finish_guest_observation(spec, cursor)
            delivered = [(mark.phase, mark.tool_use_id) for mark in observation.tool_marks]
            if not observation.complete or delivered != [
                ("pre", "preflight"),
                ("post", "preflight"),
            ]:
                # Two different failures wore one sentence here, which is why
                # the 2026-09-13 rollout could not be diagnosed from its logs:
                # an incomplete slice means the journal could not be read back,
                # while a complete slice with the wrong marks means the
                # boundaries themselves never arrived.
                raise _ObserverProbeRace(
                    "Kata observer did not receive trusted tool boundaries: "
                    f"complete={observation.complete} "
                    f"lost_events={observation.lost_events} "
                    f"truncated={observation.truncated} "
                    f"marks={delivered!r} "
                    + (f"detail={observation.detail}" if observation.detail else "")
                )
            return guest_kernel
        finally:
            self._run(
                [self.backend.docker_binary, "rm", "--force", spec.container_name], timeout=30
            )

    def _check_observer_self_test(self, stdout: str) -> None:
        """Accept the helper's identity line and report its window matrix.

        The helper answers with ``freeinference-guest-observer-v<N>`` and, from
        version 2, a second line naming the live counters this guest can
        actually read. A capability this kernel lacks is reported here, where
        an operator reads preflight output, rather than being discovered as a
        permanently partial window later. It is deliberately not fatal: a
        guest without a live census still observes process lifecycles, and
        the platform offers what it can prove rather than refusing the host.
        """
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines or not lines[0].startswith("freeinference-guest-observer-v"):
            raise SandboxError("Kata guest observer self-test failed: unrecognised helper identity")
        if len(lines) < 2:
            logger.warning(
                "Kata guest observer reports no member window capability",
                extra={"event": "guest_observer_window_capability_absent"},
            )
            return
        try:
            capability = json.loads(lines[1]).get("window") or {}
        except (ValueError, AttributeError):
            logger.warning(
                "Kata guest observer capability report is unreadable",
                extra={"event": "guest_observer_window_capability_unreadable"},
            )
            return
        missing = [name for name in ("cpu", "storage_io", "pss") if not capability.get(name)]
        if missing:
            logger.warning(
                "Kata guest cannot measure part of a member window: %s",
                ", ".join(missing),
                extra={
                    "event": "guest_observer_window_capability_partial",
                    "missing": missing,
                },
            )

    def _observer_privileged_exec(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        detach: bool = False,
        timeout_s: float = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Keep the observer-only seam explicit for tests and call sites."""
        return self._kata_privileged_exec(
            spec,
            argv,
            detach=detach,
            timeout_s=timeout_s,
        )

    def _guest_observer_snapshot(
        self,
        spec: ChatSandboxSpec,
        *,
        after_poll_seq: int | None = None,
    ) -> GuestObserverSnapshot:
        """Read an atomic status/size/guest-clock tuple from the root journal."""
        try:
            return self._guest_observer_controller(spec).snapshot(after_poll_seq=after_poll_seq)
        except GuestObserverRuntimeError as exc:
            raise SandboxError(str(exc)) from exc

    def member_window_journal_delta(
        self,
        spec: ChatSandboxSpec,
        cursor: GuestObserverCursor,
        *,
        timeout_s: float = 10.0,
    ) -> tuple[bytes, int]:
        """Read the journal bytes this window has not consumed yet.

        Returns the payload and the offset it ended at, so a caller can keep
        one reducer per window and fold each cycle in exactly once. The read is
        fenced exactly like the observation read is: the device, inode and size
        must all still describe the same journal, because a restarted observer
        would otherwise hand back bytes from a different file under the same
        path and silently restart the counters.
        """
        if not cursor.available or not cursor.journal_ino:
            return b"", cursor.offset
        reader = (
            "import os, sys\n"
            "filename, offset, expected_dev, expected_ino = sys.argv[1], int(sys.argv[2]), "
            "int(sys.argv[3]), int(sys.argv[4])\n"
            "with open(filename, 'rb', buffering=0) as handle:\n"
            "    journal = os.fstat(handle.fileno())\n"
            "    if (int(journal.st_dev) != expected_dev "
            "            or int(journal.st_ino) != expected_ino):\n"
            "        raise RuntimeError('observer journal changed before read')\n"
            "    if int(journal.st_size) < offset:\n"
            "        raise RuntimeError('observer journal is behind the read fence')\n"
            "    handle.seek(offset)\n"
            "    sys.stdout.buffer.write(handle.read(max(0, int(journal.st_size) - offset)))\n"
        )
        result = self.exec_capture(
            spec,
            [
                "python3",
                "-c",
                reader,
                GUEST_OBSERVER_EVENTS,
                str(cursor.offset),
                str(cursor.journal_dev),
                str(cursor.journal_ino),
            ],
            workdir="/",
            timeout_s=timeout_s,
            user_uid=0,
            user_gid=0,
        )
        if getattr(result, "returncode", 1) != 0:
            detail = getattr(result, "stderr", b"")
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")
            raise SandboxError(f"member window journal read failed: {str(detail)[-300:]}")
        payload = getattr(result, "stdout", b"")
        if not isinstance(payload, bytes):
            payload = str(payload).encode("utf-8", "replace")
        return payload, cursor.offset + len(payload)

    def guest_boot_id(self, spec: ChatSandboxSpec) -> str:
        """This guest's boot identifier, or an empty string when unreadable.

        A task's identity is only unique within one boot, so every instance key
        carries it. The observer controller caches the read, so asking once per
        window costs one guest exec rather than one per census.
        """
        if self.backend.runtime != KATA_RUNTIME:
            return ""
        try:
            return self._guest_observer_controller(spec).guest_boot_id()
        except Exception:
            return ""

    def member_window_request(
        self,
        spec: ChatSandboxSpec,
        argv: builtins.list[str],
        *,
        timeout_s: float,
    ) -> subprocess.CompletedProcess[bytes]:
        """Ask the guest observer for one member execution window operation.

        The window helper is a second reviewed binary beside the tool marker,
        and the observer accepts a window request only from it. Nothing here
        composes a command from job input: ``argv`` comes from the fixed
        ``sample``/``register``/``close`` verbs inside
        :class:`~cloud_agent_host.member_resources.MemberWindowSampler`, and
        the container is the same label-verified sandbox the turn is already
        running in.
        """
        return self.exec_capture(
            spec,
            argv,
            workdir="/",
            timeout_s=timeout_s,
            user_uid=0,
            user_gid=0,
        )

    def _guest_observer_controller(self, spec: ChatSandboxSpec) -> GuestObserverController:
        """Bind the shared observer contract to this label-verified container."""

        def capture(
            argv: builtins.list[str], timeout_s: float
        ) -> subprocess.CompletedProcess[bytes]:
            return self.exec_capture(
                spec,
                argv,
                workdir="/",
                timeout_s=timeout_s,
                user_uid=0,
                user_gid=0,
            )

        def privileged(
            argv: builtins.list[str], detach: bool, timeout_s: float
        ) -> subprocess.CompletedProcess[str]:
            return self._observer_privileged_exec(
                spec,
                argv,
                detach=detach,
                timeout_s=timeout_s,
            )

        return GuestObserverController(
            capture=capture,
            privileged=privileged,
            lock=self._guest_observer_lock,
        )

    def begin_guest_observation(
        self,
        spec: ChatSandboxSpec,
        *,
        user_uid: int,
        measurement_id: str | None = None,
    ) -> GuestObserverCursor:
        """Fence the journal immediately before one member prompt is admitted.

        ``measurement_id`` additionally marks this fence as the start of a
        member execution window, so the observation also carries that window's
        live census cycles. The fence has to be taken before the window's
        opening census, which is why the identifier travels with it here.
        """
        if self.backend.runtime != KATA_RUNTIME:
            return GuestObserverCursor.unavailable(user_uid, "sandbox is not a Kata guest")
        controller = self._guest_observer_controller(spec)
        key = spec.container_name
        with self._guest_observer_lock:
            active = self._guest_observer_active.setdefault(key, {})
            if not active:
                controller.rotate_if_needed(threshold=GUEST_OBSERVER_ROTATE_BYTES)
            cursor = controller.begin(
                user_uid=user_uid,
                # Never sacrifice concurrent member observations to admit one
                # more turn. If the singleton is temporarily unreadable, this
                # cursor is explicitly unavailable; cleanup of existing owners
                # makes a later idle begin eligible to restart it.
                restart_if_missing=not active,
                measurement_id=measurement_id,
            )
            if cursor.available:
                # A restart invalidates every old-pid cursor already; do not
                # let abandoned bookkeeping suppress future safe rotations.
                active = {
                    token: observer_pid
                    for token, observer_pid in active.items()
                    if observer_pid == cursor.observer_pid
                }
                active[id(cursor)] = cursor.observer_pid
                self._guest_observer_active[key] = active
            elif not active:
                self._guest_observer_active.pop(key, None)
            return cursor

    def finish_guest_observation(
        self,
        spec: ChatSandboxSpec,
        cursor: GuestObserverCursor | None,
    ) -> GuestObservation:
        """Drain and read exactly one cursor's bounded member event slice."""
        try:
            return self._guest_observer_controller(spec).finish(cursor)
        finally:
            if cursor is not None:
                key = spec.container_name
                with self._guest_observer_lock:
                    active = self._guest_observer_active.get(key)
                    if active is not None:
                        active.pop(id(cursor), None)
                        if not active:
                            self._guest_observer_active.pop(key, None)

    def tool_mark_reader(self, spec: ChatSandboxSpec) -> ToolMarkReader:
        """Return a bounded reader for hook marks inside one retained container."""
        if self.backend.runtime == KATA_RUNTIME:
            # Kata accepts marks only through the guest-root observer socket.
            # The agent-owned compatibility file is deliberately not an
            # attribution authority in an untrusted sandbox.
            return ToolMarkReader(None)
        path = f"{CHAT_STATE_DIR}/{TOOL_MARK_FILE}"

        def remote_size() -> int:
            script = (
                "import os, sys\n"
                "try:\n"
                "    size = os.path.getsize(sys.argv[1])\n"
                "except OSError:\n"
                "    size = 0\n"
                "print(size)\n"
            )
            result = self.exec_capture(
                spec,
                ["python3", "-c", script, path],
                workdir="/",
                timeout_s=5,
                user_uid=self.backend.uid,
                user_gid=self.backend.gid,
            )
            if result.returncode != 0:
                raise SandboxError("tool-mark fence could not be read")
            return int(result.stdout.decode("ascii", "strict").strip() or "0")

        def remote_read(offset: int, count: int) -> bytes:
            bounded = min(max(0, count), MAX_TOOL_MARK_BYTES)
            script = (
                "import os, sys\n"
                "path, offset, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])\n"
                "try:\n"
                "    with open(path, 'rb', buffering=0) as handle:\n"
                "        handle.seek(offset)\n"
                "        sys.stdout.buffer.write(handle.read(count))\n"
                "except FileNotFoundError:\n"
                "    pass\n"
            )
            result = self.exec_capture(
                spec,
                ["python3", "-c", script, path, str(max(0, offset)), str(bounded)],
                workdir="/",
                timeout_s=5,
                user_uid=self.backend.uid,
                user_gid=self.backend.gid,
            )
            if result.returncode != 0 or len(result.stdout) > bounded:
                raise SandboxError("tool-mark slice could not be read")
            return result.stdout

        return ToolMarkReader(
            None,
            remote_size=remote_size,
            remote_read=remote_read,
        )

    def install_tool_hooks(
        self, spec: ChatSandboxSpec, update: dict[str, Any], *, slot_id: int, thread_id: str
    ) -> None:
        """Atomically merge Claude hooks into this member's private settings.

        Docker-owned retained state has no corresponding host path. Writing it
        with :mod:`pathlib` on the sandbox manager would configure the sandbox manager itself,
        not the Agent CLI. Settings are never imported from the old shared
        Claude root. The group-writable marks file supports trusted non-Kata
        development; Kata accepts only its guest-root observer journal.
        """
        uid = MEMBER_UID_BASE + slot_id
        self._validate_member_identity(spec, slot_id=slot_id, user_uid=uid)
        payload = json.dumps(update, separators=(",", ":")).encode()
        if len(payload) > 64 * 1024:
            raise SandboxError("tool hook settings exceed their trusted bound")
        script = (
            "import contextlib, fcntl, json, os, pathlib, stat, sys, tempfile\n"
            "config = pathlib.Path(sys.argv[1])\n"
            "marks = pathlib.Path(sys.argv[2])\n"
            "raw = sys.stdin.buffer.read(65537)\n"
            "if len(raw) > 65536:\n"
            "    raise RuntimeError('tool hook settings are too large')\n"
            "update = json.loads(raw)\n"
            "if not isinstance(update, dict):\n"
            "    raise RuntimeError('tool hook settings are not an object')\n"
            "os.umask(0o077)\n"
            "info = config.lstat()\n"
            "if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():\n"
            "    raise RuntimeError('Claude config is not member-owned')\n"
            "config.chmod(0o700)\n"
            "mark_fd = os.open(marks, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o660)\n"
            "with contextlib.suppress(PermissionError):\n"
            "    os.fchmod(mark_fd, 0o660)\n"
            "os.close(mark_fd)\n"
            "lock_path = config / '.freeinference-hooks.lock'\n"
            "lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)\n"
            "os.fchmod(lock_fd, 0o600)\n"
            "with os.fdopen(lock_fd, 'r+') as lock:\n"
            "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "    settings_path = config / 'settings.json'\n"
            "    settings = {}\n"
            "    try:\n"
            "        fd = os.open(settings_path, os.O_RDONLY | os.O_NOFOLLOW)\n"
            "        with os.fdopen(fd, 'r') as handle:\n"
            "            candidate = json.load(handle)\n"
            "        if isinstance(candidate, dict):\n"
            "            settings = candidate\n"
            "    except (OSError, ValueError, TypeError):\n"
            "        pass\n"
            "    settings.update(update)\n"
            "    fd, filename = tempfile.mkstemp(prefix='.settings.', suffix='.tmp', dir=config)\n"
            "    temporary = pathlib.Path(filename)\n"
            "    try:\n"
            "        with os.fdopen(fd, 'w') as handle:\n"
            "            json.dump(settings, handle, indent=2)\n"
            "        os.replace(temporary, settings_path)\n"
            "    finally:\n"
            "        with contextlib.suppress(FileNotFoundError):\n"
            "            temporary.unlink()\n"
        )
        result = self.exec_capture(
            spec,
            [
                "python3",
                "-c",
                script,
                claude_member_config_dir(slot_id, thread_id),
                f"{CHAT_STATE_DIR}/{TOOL_MARK_FILE}",
            ],
            workdir="/",
            timeout_s=10,
            input_bytes=payload,
            user_uid=uid,
            user_gid=self.backend.gid,
        )
        if result.returncode != 0:
            raise SandboxError(
                "tool hooks could not be installed in retained state: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )

    def read_initial_archive_marker(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> bytes | None:
        """Read the bounded sandbox-manager-private marker from one exact container.

        Absence is the only soft outcome.  A symlink, non-regular file,
        unexpected owner/mode/link count, oversized payload, or identity race
        fails closed before any S3 decision can cause workspace preparation to
        be skipped.
        """
        script = (
            _INITIAL_ARCHIVE_MARKER_PREAMBLE
            + r"""
descriptor = checked_root()
try:
    payload = read_checked(descriptor)
    if payload is None:
        raise SystemExit(44)
    sys.stdout.buffer.write(payload)
finally:
    os.close(descriptor)
"""
        )
        result = self.exec_capture(
            spec,
            [
                "python3",
                "-c",
                script,
                INITIAL_ARCHIVE_MARKER_ROOT,
                INITIAL_ARCHIVE_MARKER_NAME,
                str(INITIAL_ARCHIVE_MARKER_MAX_BYTES),
                "0",
                "0",
            ],
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
            expected_container_id=expected_container_id,
        )
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise SandboxError(
                "initial archive marker cannot be trusted: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )
        payload = result.stdout
        if not 0 < len(payload) <= INITIAL_ARCHIVE_MARKER_MAX_BYTES:
            raise SandboxError("initial archive marker has an invalid size")
        return payload

    def write_initial_archive_marker(
        self,
        spec: ChatSandboxSpec,
        payload: bytes,
        *,
        expected_container_id: str,
    ) -> None:
        """Atomically publish and fsync one exact immutable marker.

        An identical existing marker is idempotent.  A different marker is
        never overwritten: it may describe an initial object whose commit
        response was lost, so replacing it would destroy the only safe retry
        proof.
        """
        if not isinstance(payload, bytes) or not 0 < len(payload) <= (
            INITIAL_ARCHIVE_MARKER_MAX_BYTES
        ):
            raise ValueError("initial archive marker payload has an invalid size")
        self._write_archive_marker(
            spec,
            payload,
            marker_name=INITIAL_ARCHIVE_MARKER_NAME,
            marker_max_bytes=INITIAL_ARCHIVE_MARKER_MAX_BYTES,
            expected_container_id=expected_container_id,
            replace_existing=False,
        )

    def read_environment_setup_marker(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> bytes | None:
        """Read the root-only "installation done" marker, which travels with the layer.

        It sits beside the archive markers, so the sandbox user can neither
        write nor forge it, and a checkpoint or snapshot carries it along.
        """
        script = (
            _INITIAL_ARCHIVE_MARKER_PREAMBLE
            + r"""
descriptor = checked_root()
try:
    payload = read_checked(descriptor)
    if payload is None:
        raise SystemExit(44)
    sys.stdout.buffer.write(payload)
finally:
    os.close(descriptor)
"""
        )
        result = self.exec_capture(
            spec,
            [
                "python3",
                "-c",
                script,
                INITIAL_ARCHIVE_MARKER_ROOT,
                SETUP_MARKER_NAME,
                str(SETUP_MARKER_MAX_BYTES),
                "0",
                "0",
            ],
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
            expected_container_id=expected_container_id,
        )
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise SandboxError(
                "environment setup marker cannot be trusted: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )
        payload = result.stdout
        if not 0 < len(payload) <= SETUP_MARKER_MAX_BYTES:
            raise SandboxError("environment setup marker has an invalid size")
        return payload

    def write_environment_setup_marker(
        self,
        spec: ChatSandboxSpec,
        payload: bytes,
        *,
        expected_container_id: str,
    ) -> None:
        """Record, root-only, which installation this container's layer holds."""
        if not isinstance(payload, bytes) or not 0 < len(payload) <= SETUP_MARKER_MAX_BYTES:
            raise ValueError("environment setup marker payload has an invalid size")
        self._write_archive_marker(
            spec,
            payload,
            marker_name=SETUP_MARKER_NAME,
            marker_max_bytes=SETUP_MARKER_MAX_BYTES,
            expected_container_id=expected_container_id,
            replace_existing=True,
        )

    def write_final_archive_marker(
        self,
        spec: ChatSandboxSpec,
        payload: bytes,
        *,
        expected_container_id: str,
    ) -> None:
        """Atomically fence a pending final archive to one immutable policy.

        A marker from a completed prior stop may remain in the writable layer.
        The caller invokes this only while the exact container is running and
        before its next stop, so replacing that stale marker is both necessary
        and safe. The owned bridge remains the durable pending/completed bit.
        """
        if not isinstance(payload, bytes) or not 0 < len(payload) <= (
            FINAL_ARCHIVE_MARKER_MAX_BYTES
        ):
            raise ValueError("final archive marker payload has an invalid size")
        current = self._require(spec)
        if current.container_id != expected_container_id:
            raise RetainedChatLost("chat sandbox container identity changed before archive marker")
        if not current.running:
            raise SandboxError("final archive marker requires the exact running container")
        self._write_archive_marker(
            spec,
            payload,
            marker_name=FINAL_ARCHIVE_MARKER_NAME,
            marker_max_bytes=FINAL_ARCHIVE_MARKER_MAX_BYTES,
            expected_container_id=expected_container_id,
            replace_existing=True,
        )

    def read_running_final_archive_marker(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> bytes | None:
        """Read a trusted final marker before replacing it on a running stop."""
        current = self._require(spec)
        if current.container_id != expected_container_id:
            raise RetainedChatLost("chat sandbox container identity changed before marker read")
        if not current.running:
            raise SandboxError("running final archive marker requires a running container")
        script = (
            _INITIAL_ARCHIVE_MARKER_PREAMBLE
            + r"""
descriptor = checked_root()
try:
    payload = read_checked(descriptor)
    if payload is None:
        raise SystemExit(44)
    sys.stdout.buffer.write(payload)
finally:
    os.close(descriptor)
"""
        )
        result = self.exec_capture(
            spec,
            [
                "python3",
                "-c",
                script,
                INITIAL_ARCHIVE_MARKER_ROOT,
                FINAL_ARCHIVE_MARKER_NAME,
                str(FINAL_ARCHIVE_MARKER_MAX_BYTES),
                "0",
                "0",
            ],
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
            expected_container_id=expected_container_id,
        )
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise SandboxError(
                "running final archive marker cannot be trusted: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )
        payload = result.stdout
        if not 0 < len(payload) <= FINAL_ARCHIVE_MARKER_MAX_BYTES:
            raise SandboxError("running final archive marker has an invalid size")
        return payload

    def _write_archive_marker(
        self,
        spec: ChatSandboxSpec,
        payload: bytes,
        *,
        marker_name: str,
        marker_max_bytes: int,
        expected_container_id: str,
        replace_existing: bool,
    ) -> None:
        """Publish one fsynced sandbox-manager-private marker through guest root."""
        script = (
            _INITIAL_ARCHIVE_MARKER_PREAMBLE
            + r"""
payload = sys.stdin.buffer.read(limit + 1)
if not 0 < len(payload) <= limit:
    raise RuntimeError('marker payload has an invalid size')
replace_existing = sys.argv[6] == '1'
descriptor = checked_root()
temporary = '.%s.%s.tmp' % (name, os.getpid())
try:
    existing = read_checked(descriptor)
    if existing is not None:
        if existing == payload:
            raise SystemExit(0)
        if not replace_existing:
            raise RuntimeError('a different marker already exists')
    marker_fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=descriptor,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(marker_fd, payload[offset:])
        os.fchmod(marker_fd, 0o600)
        written = os.fstat(marker_fd)
        if (
            written.st_uid != expected_uid
            or written.st_gid != expected_gid
            or stat.S_IMODE(written.st_mode) != 0o600
            or written.st_nlink != 1
            or written.st_size != len(payload)
        ):
            raise RuntimeError('temporary marker cannot be trusted')
        os.fsync(marker_fd)
    finally:
        os.close(marker_fd)
    os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
    os.fsync(descriptor)
    if read_checked(descriptor) != payload:
        raise RuntimeError('published marker failed verification')
finally:
    try:
        os.unlink(temporary, dir_fd=descriptor)
    except FileNotFoundError:
        pass
    os.close(descriptor)
"""
        )
        result = self.exec_capture(
            spec,
            [
                "python3",
                "-c",
                script,
                INITIAL_ARCHIVE_MARKER_ROOT,
                marker_name,
                str(marker_max_bytes),
                "0",
                "0",
                "1" if replace_existing else "0",
            ],
            workdir="/",
            timeout_s=20,
            input_bytes=payload,
            user_uid=0,
            user_gid=0,
            expected_container_id=expected_container_id,
        )
        if result.returncode != 0:
            raise SandboxError(
                "sandbox archive marker could not be persisted: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )

    def read_final_archive_marker(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> bytes:
        """Read and validate the root-owned marker from a stopped container.

        ``docker cp`` works without starting the retained container. Its tar
        metadata is checked before reading bytes; nothing is extracted onto the
        host filesystem and the exact immutable container id fences the copy.
        Absence or any malformed ownership/mode/type fails closed.
        """
        current = self._require(spec)
        if current.container_id != expected_container_id:
            raise RetainedChatLost("chat sandbox container identity changed before marker read")
        if current.running:
            raise SandboxError("final archive recovery requires the exact stopped container")
        marker_path = f"{INITIAL_ARCHIVE_MARKER_ROOT}/{FINAL_ARCHIVE_MARKER_NAME}"
        with tempfile.TemporaryFile(prefix="freeinference-final-archive-marker-") as archive:
            self.export_archive(
                spec,
                marker_path,
                archive,
                max_bytes=FINAL_ARCHIVE_MARKER_MAX_BYTES + 32 * 1024,
                expected_container_id=expected_container_id,
            )
            archive.seek(0)
            try:
                with tarfile.open(fileobj=archive, mode="r:*") as copied:
                    entries = copied.getmembers()
                    if len(entries) != 1:
                        raise SandboxError("final archive marker copy has unexpected entries")
                    entry = entries[0]
                    if (
                        not entry.isreg()
                        or entry.name.rsplit("/", 1)[-1] != FINAL_ARCHIVE_MARKER_NAME
                        or entry.uid != 0
                        or entry.gid != 0
                        or entry.mode & 0o7777 != 0o600
                        or not 0 < entry.size <= FINAL_ARCHIVE_MARKER_MAX_BYTES
                    ):
                        raise SandboxError("final archive marker cannot be trusted")
                    source = copied.extractfile(entry)
                    if source is None:
                        raise SandboxError("final archive marker has no readable payload")
                    payload = source.read(FINAL_ARCHIVE_MARKER_MAX_BYTES + 1)
            except (tarfile.TarError, OSError) as exc:
                raise SandboxError("final archive marker copy is invalid") from exc
        if len(payload) != entry.size:
            raise SandboxError("final archive marker changed while being read")
        return payload

    def read_stopped_final_archive_marker_optional(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> bytes | None:
        """Read a stopped marker, softening only an explicit fixed-path absence."""
        try:
            return self.read_final_archive_marker(
                spec,
                expected_container_id=expected_container_id,
            )
        except FileNotFoundError as exc:
            marker_path = f"{INITIAL_ARCHIVE_MARKER_ROOT}/{FINAL_ARCHIVE_MARKER_NAME}"
            # export_archive normalizes only Docker's exact missing source
            # for the inspected container. Host-side missing files and other
            # Docker failures must still prevent activation.
            if exc.args != (marker_path,):
                raise
            return None

    def write_stopped_final_archive_marker(
        self,
        spec: ChatSandboxSpec,
        payload: bytes,
        *,
        expected_container_id: str,
    ) -> None:
        """Replace the trusted marker in one exact stopped writable layer.

        This is used only to publish the post-upload receipt or to arm a
        possible start fail-stop. A partial Docker copy leaves an invalid
        marker and therefore fails closed; the read-back must exactly match
        before the caller can release the retained bridge.
        """
        if not isinstance(payload, bytes) or not 0 < len(payload) <= (
            FINAL_ARCHIVE_MARKER_MAX_BYTES
        ):
            raise ValueError("final archive marker payload has an invalid size")
        current = self._require(spec)
        if current.container_id != expected_container_id:
            raise RetainedChatLost("chat sandbox container identity changed before marker write")
        if current.running:
            raise SandboxError("stopped final archive marker requires a stopped container")
        with tempfile.TemporaryFile(prefix="freeinference-final-archive-marker-write-") as archive:
            with tarfile.open(fileobj=archive, mode="w") as payload_archive:
                marker_root = INITIAL_ARCHIVE_MARKER_ROOT.rsplit("/", 1)[-1]
                directory = tarfile.TarInfo(marker_root)
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o700
                directory.uid = 0
                directory.gid = 0
                directory.mtime = 0
                payload_archive.addfile(directory)
                entry = tarfile.TarInfo(f"{marker_root}/{FINAL_ARCHIVE_MARKER_NAME}")
                entry.size = len(payload)
                entry.mode = 0o600
                entry.uid = 0
                entry.gid = 0
                entry.mtime = 0
                payload_archive.addfile(entry, io.BytesIO(payload))
            archive.seek(0)
            result = self._run_bytes(
                [
                    self.backend.docker_binary,
                    "cp",
                    "-",
                    f"{current.container_id}:{INITIAL_ARCHIVE_MARKER_ROOT.rsplit('/', 1)[0]}",
                ],
                timeout=30,
                stdin=archive,
            )
        if result.returncode != 0:
            raise SandboxError(
                "stopped final archive marker could not be persisted: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )
        if (
            self.read_final_archive_marker(
                spec,
                expected_container_id=expected_container_id,
            )
            != payload
        ):
            raise SandboxError("stopped final archive marker failed verification")

    def clear_final_archive_marker(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> None:
        """Remove any trusted stale completion marker while the container runs."""
        current = self._require(spec)
        if current.container_id != expected_container_id:
            raise RetainedChatLost("chat sandbox container identity changed before marker clear")
        if not current.running:
            raise SandboxError("final archive marker can be cleared only while running")
        script = (
            _INITIAL_ARCHIVE_MARKER_PREAMBLE
            + r"""
descriptor = checked_root()
try:
    if read_checked(descriptor) is None:
        raise SystemExit(0)
    os.unlink(name, dir_fd=descriptor)
    os.fsync(descriptor)
    if read_checked(descriptor) is not None:
        raise RuntimeError('marker deletion was not durable')
finally:
    os.close(descriptor)
"""
        )
        result = self.exec_capture(
            spec,
            [
                "python3",
                "-c",
                script,
                INITIAL_ARCHIVE_MARKER_ROOT,
                FINAL_ARCHIVE_MARKER_NAME,
                str(FINAL_ARCHIVE_MARKER_MAX_BYTES),
                "0",
                "0",
            ],
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
            expected_container_id=expected_container_id,
        )
        if result.returncode != 0:
            raise SandboxError(
                "final archive marker could not be cleared safely: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )

    def delete_initial_archive_marker(
        self,
        spec: ChatSandboxSpec,
        expected_payload: bytes,
        *,
        expected_container_id: str,
    ) -> None:
        """Atomically unlink and fsync only the marker the caller read."""
        if not isinstance(expected_payload, bytes) or not 0 < len(expected_payload) <= (
            INITIAL_ARCHIVE_MARKER_MAX_BYTES
        ):
            raise ValueError("initial archive marker payload has an invalid size")
        script = (
            _INITIAL_ARCHIVE_MARKER_PREAMBLE
            + r"""
expected = sys.stdin.buffer.read(limit + 1)
if not 0 < len(expected) <= limit:
    raise RuntimeError('expected marker has an invalid size')
descriptor = checked_root()
try:
    if read_checked(descriptor) != expected:
        raise RuntimeError('marker changed before deletion')
    os.unlink(name, dir_fd=descriptor)
    os.fsync(descriptor)
    try:
        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError('marker deletion was not durable')
finally:
    os.close(descriptor)
"""
        )
        result = self.exec_capture(
            spec,
            [
                "python3",
                "-c",
                script,
                INITIAL_ARCHIVE_MARKER_ROOT,
                INITIAL_ARCHIVE_MARKER_NAME,
                str(INITIAL_ARCHIVE_MARKER_MAX_BYTES),
                "0",
                "0",
            ],
            workdir="/",
            timeout_s=20,
            input_bytes=expected_payload,
            user_uid=0,
            user_gid=0,
            expected_container_id=expected_container_id,
        )
        if result.returncode != 0:
            raise SandboxError(
                "initial archive marker could not be removed safely: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )

    def reset_unsettled_state(self, spec: ChatSandboxSpec, *, keep_workspace: bool = False) -> None:
        """Clear a never-settled first-turn import before an idempotent retry.

        ``keep_workspace`` leaves ``/workspace`` alone for a group created
        from an installation snapshot: it holds that installation, without
        the repository, which the import that follows adds or replaces.

        This is deliberately unavailable as a generic path-taking primitive:
        only the two Docker-layer roots owned by this exact, label-verified
        sandbox are cleared. A failed prompt admission is marked LOST and can
        never reach this method; it is only for checkout/setup failures before
        the Agent CLI saw the user's message.

        A resident that started before the failure left a member-private
        provider home that only its member UID can open. The reset names
        those owners before it deletes anything, has each member remove its
        own homes, and only then clears both roots.
        """
        collections = " ".join(PRIVATE_MEMBER_HOME_COLLECTIONS + LEGACY_MEMBER_HOME_COLLECTIONS)
        script = (
            "set -eu; "
            f"owners=$(for collection in {collections}; do "
            'test -d "$collection" || continue; '
            'find "$collection" -mindepth 1 -maxdepth 1 ! -uid "$(id -u)" -printf "%U\\n"; '
            "done | sort -u); "
            'if test -n "$owners"; then printf "%s\\n" "$owners"; exit 75; fi; '
            f"for root in {CHAT_STATE_DIR if keep_workspace else f'{CHAT_WORKDIR} {CHAT_STATE_DIR}'}; do "
            'find "$root" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; '
            "done"
        )
        for attempt in range(2):
            result = self.exec_capture(
                spec,
                ["/bin/sh", "-c", script],
                workdir="/",
                timeout_s=120,
            )
            if result.returncode != 75 or attempt:
                break
            self._clear_private_member_homes(spec, result.stdout)
        if result.returncode != 0:
            raise SandboxError(
                "chat sandbox unsettled-state reset failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def _clear_private_member_homes(self, spec: ChatSandboxSpec, owners: bytes) -> None:
        """Have each named member UID remove the provider homes it owns."""
        uids: set[int] = set()
        for line in owners.decode("ascii", "replace").split():
            if not line.isdigit():
                raise SandboxError("private member home owners are malformed")
            uid = int(line)
            if not MEMBER_UID_BASE <= uid < MEMBER_UID_BASE + spec.resident_slot_capacity:
                raise SandboxError(f"private member home is owned by unexpected uid {uid}")
            uids.add(uid)
        collections = " ".join(PRIVATE_MEMBER_HOME_COLLECTIONS + LEGACY_MEMBER_HOME_COLLECTIONS)
        script = (
            "set -eu; uid=$(id -u); "
            f"for collection in {collections}; do "
            'test -d "$collection" || continue; '
            'find "$collection" -mindepth 1 -maxdepth 1 -uid "$uid" -exec rm -rf -- {} +; '
            "done"
        )
        for uid in sorted(uids):
            result = self.exec_capture(
                spec,
                ["/bin/sh", "-c", script],
                workdir="/",
                timeout_s=120,
                user_uid=uid,
                user_gid=self.backend.gid,
            )
            if result.returncode != 0:
                raise SandboxError(
                    "private member home could not be cleared: "
                    + result.stderr.decode("utf-8", "replace")[:300]
                )

    def import_base_bundle(
        self,
        spec: ChatSandboxSpec,
        bundle: bytes,
        *,
        commit: str,
        env: dict[str, str] | None = None,
    ) -> None:
        """Import publisher-base objects without changing workspace Git state.

        The bundle crosses stdin into activation tmpfs and is removed before
        this command returns. ``git bundle unbundle`` adds objects only: it
        does not move HEAD, refs, the real index, or working-tree files.
        """
        if not bundle:
            raise ValueError("base bundle must be non-empty")
        if not _FULL_OBJECT_ID.fullmatch(commit):
            raise ValueError("base bundle commit must be a full Git object id")
        script = (
            'set -eu; expected="$1"; object="$2"; '
            f"bundle={ACTIVATION_TMPFS}/base.bundle; "
            "umask 077; trap 'rm -f \"$bundle\"' EXIT HUP INT TERM; "
            'cat > "$bundle"; '
            'set -- $(git bundle list-heads "$bundle" HEAD); '
            'test "$#" -eq 2; test "$1" = "$expected"; test "$2" = HEAD; '
            'git bundle verify "$bundle" >/dev/null; '
            'git bundle unbundle "$bundle" >/dev/null; '
            'git cat-file -e "$object"'
        )
        # A partial clone fetches a missing prerequisite on demand, through a
        # transport the workspace's own config names (``remote.*.uploadpack``,
        # ``core.sshCommand``). These objects come only from the bundle, so an
        # empty ``GIT_ALLOW_PROTOCOL`` allows no transport, whatever
        # ``protocol.*.allow`` says.
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script, "sh", commit, f"{commit}^{{commit}}"],
            env={**(env or {}), "GIT_ALLOW_PROTOCOL": ""},
            workdir=CHAT_WORKDIR,
            timeout_s=300,
            input_bytes=bundle,
        )
        if result.returncode != 0:
            raise SandboxError(
                "retained base bundle import failed: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )

    def import_archive(self, spec: ChatSandboxSpec, path: str, archive: bytes | BinaryIO) -> None:
        """Write a validated caller-provided tar through Docker's archive boundary.

        ``archive`` is the tar itself or a real file positioned at its start. A
        file becomes ``docker cp``'s standard input, so a large checkout is never
        read into this process.
        """
        self._require(spec)
        self._validate_container_path(path)
        if isinstance(archive, bytes):
            size, input_bytes, stdin = len(archive), archive, None
        else:
            size = os.fstat(archive.fileno()).st_size - archive.tell()
            input_bytes, stdin = None, archive
        result = self._run_bytes(
            [
                self.backend.docker_binary,
                "cp",
                "-",
                f"{spec.container_name}:{path}",
            ],
            timeout=ARCHIVE_IMPORT_TIMEOUT_PER_GIB_S * max(1.0, size / (1 << 30)),
            input_bytes=input_bytes,
            stdin=stdin,
        )
        if result.returncode != 0:
            raise SandboxError(
                "chat sandbox archive import failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    def export_archive(
        self,
        spec: ChatSandboxSpec,
        path: str,
        destination: SupportsWrite[bytes],
        *,
        max_bytes: int,
        expected_container_id: str | None = None,
        deadline: float | None = None,
    ) -> int:
        """Stream one container-owned path as Docker's tar archive.

        The exact inspected container id is used rather than its reusable
        name, and the writable layer is read through Docker's archive
        boundary rather than by walking the daemon's overlay directory. The
        method works for both running and stopped containers. Callers own the
        lifecycle barrier that makes the selected point in time consistent.
        """
        if max_bytes <= 0:
            raise ValueError("chat sandbox archive export limit must be positive")
        if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise ValueError("chat sandbox archive deadline must be a finite monotonic time")
        current = self._require(spec)
        if expected_container_id and current.container_id != expected_container_id:
            raise RetainedChatLost("chat sandbox container identity changed during archive export")
        self._validate_container_path(path)
        command = [
            self.backend.docker_binary,
            "cp",
            f"{current.container_id}:{path}",
            "-",
        ]
        timeout_s = ARCHIVE_EXPORT_TIMEOUT_PER_GIB_S * max(1.0, max_bytes / (1 << 30))
        local_deadline = time.monotonic() + timeout_s
        export_deadline = (
            local_deadline if deadline is None else min(local_deadline, float(deadline))
        )
        if export_deadline <= time.monotonic():
            raise SandboxError("chat sandbox archive export timed out")
        total = 0
        with tempfile.TemporaryFile(prefix="freeinference-docker-cp-error-") as stderr:
            try:
                process = subprocess.Popen(
                    command,
                    env={**os.environ},
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                )
            except OSError as exc:
                raise SandboxError(f"chat sandbox archive export failed: {exc}") from exc
            assert process.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            try:
                while True:
                    remaining = export_deadline - time.monotonic()
                    if remaining <= 0:
                        raise SandboxError("chat sandbox archive export timed out")
                    ready = selector.select(timeout=min(remaining, 1.0))
                    if not ready:
                        if process.poll() is None:
                            continue
                        chunk = os.read(process.stdout.fileno(), 1024 * 1024)
                    else:
                        chunk = os.read(process.stdout.fileno(), 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ArchiveExportTooLarge(
                            f"chat sandbox archive for {path!r} exceeds its byte limit"
                        )
                    if destination.write(chunk) != len(chunk):
                        raise SandboxError(
                            "chat sandbox archive destination accepted a short write"
                        )
                remaining = export_deadline - time.monotonic()
                if remaining <= 0:
                    raise SandboxError("chat sandbox archive export timed out")
                try:
                    returncode = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired as exc:
                    raise SandboxError("chat sandbox archive export timed out") from exc
            except BaseException:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                raise
            finally:
                selector.close()
                process.stdout.close()
            if returncode != 0:
                stderr.seek(0)
                detail = stderr.read(4096).decode("utf-8", "replace").strip()
                missing = f"Could not find the file {path} in container {current.container_id}"
                if total == 0 and detail in {
                    missing,
                    f"Error response from daemon: {missing}",
                }:
                    raise FileNotFoundError(path)
                raise SandboxError(
                    "chat sandbox archive export failed: " + (detail[:300] or "Docker cp failed")
                )
        return total

    def _require(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
        current = self.inspect(spec.container_name)
        if current is None:
            raise SandboxError("chat sandbox is missing")
        self._verify_identity(current, spec)
        return current

    def _require_exact(
        self,
        spec: ChatSandboxSpec,
        container_id: str,
    ) -> ChatSandboxInspection:
        """Require the original Docker object even after its name is reused."""
        current = self.inspect(container_id)
        if current is None:
            raise RetainedChatLost("chat sandbox exact container is missing")
        if current.container_id != container_id:
            raise RetainedChatLost("chat sandbox container identity changed")
        self._verify_identity(current, spec, require_name=False)
        return current

    @staticmethod
    def _status(state: dict[str, Any]) -> ChatSandboxStatus:
        if bool(state.get("Paused")):
            return ChatSandboxStatus.PAUSED
        raw = str(state.get("Status") or "").lower()
        if raw == "exited":
            return ChatSandboxStatus.STOPPED
        try:
            return ChatSandboxStatus(raw)
        except ValueError:
            return ChatSandboxStatus.UNKNOWN

    @staticmethod
    def _verify_mounts(inspection: ChatSandboxInspection) -> None:
        """Chat state must be in the writable layer, never a host/volume mount.

        Nor a tmpfs: one over the workspace or the agent state would empty it
        at every stop without an error anywhere.
        """
        mounted = [
            (str(mount.get("Type") or ""), str(mount.get("Destination") or ""))
            for mount in inspection.mounts
        ]
        mounted += [("tmpfs", destination) for destination in inspection.tmpfs]
        for mount_type, destination in mounted:
            if mount_type in {"bind", "volume"}:
                raise SandboxError(
                    f"retained chat sandbox has forbidden {mount_type} mount at "
                    f"{destination or '<unknown>'}"
                )
            path = destination.rstrip("/")
            if any(path == root or path.startswith(f"{root}/") for root in _CHAT_STATE_ROOTS):
                raise SandboxError(
                    f"retained chat state path {destination!r} is mounted, not Docker-layer owned"
                )

    def _verify_identity(
        self,
        inspection: ChatSandboxInspection,
        spec: ChatSandboxSpec,
        *,
        require_name: bool = True,
    ) -> None:
        expected = spec.docker_labels()
        mismatched = {
            key: (inspection.labels.get(key), value)
            for key, value in expected.items()
            if inspection.labels.get(key) != value
        }
        # Groups created before disk limits have no label and no quota. An
        # unexpected positive quota is still an immutable policy mismatch.
        disk_limit = inspection.labels.get(CHAT_DISK_LIMIT_LABEL, "0")
        if disk_limit != str(spec.disk_limit_mib):
            mismatched[CHAT_DISK_LIMIT_LABEL] = (disk_limit, str(spec.disk_limit_mib))
        namespace = inspection.labels.get(CHAT_NAMESPACE_LABEL)
        if namespace is not None and namespace != self.backend.sandbox_namespace:
            mismatched[CHAT_NAMESPACE_LABEL] = (
                namespace,
                self.backend.sandbox_namespace,
            )
        # A recreated group runs its checkpoint's image, which extends the
        # immutable one; the checkpoint label says which, and only that image
        # is accepted for it.
        checkpoint_id = inspection.labels.get(CHAT_CHECKPOINT_LABEL)
        snapshot = inspection.labels.get(CHAT_ENVIRONMENT_SNAPSHOT_LABEL)
        snapshot_image = None
        if snapshot and not checkpoint_id:
            owner, _, snapshot_id = snapshot.partition(":")
            try:
                snapshot_image = snapshot_image_reference(owner, snapshot_id)
            except ValueError:
                mismatched[CHAT_ENVIRONMENT_SNAPSHOT_LABEL] = (snapshot, "")
        expected_image = (
            checkpoint_image_reference(spec.sandbox_id, checkpoint_id)
            if checkpoint_id
            else snapshot_image or spec.sandbox_image_digest
        )
        if (checkpoint_id or snapshot_image or spec.sandbox_image_digest is not None) and (
            inspection.image != expected_image
        ):
            mismatched["container-image"] = (inspection.image, expected_image or "")
        if (require_name and inspection.name != spec.container_name) or mismatched:
            raise SandboxError(
                "Docker name is occupied by a container outside this chat/generation"
            )

    def _validate_extra_args(self) -> None:
        """Reject options that would defeat retained ownership, secret or isolation rules.

        Options are read the way Docker reads them: a long option with or
        without ``=value`` or through a hidden alias, a short flag alone or
        inside a combined group. Only the option is named in the refusal,
        never its value, which may be a secret.
        """
        for token in self.backend.extra_args:
            for option in _extra_arg_options(token):
                if option in _REFUSED_EXTRA_ARGS:
                    raise SandboxError(
                        f"AGENT_SANDBOX_EXTRA_ARGS option {option!r} is unsafe for retained chats"
                    )

    @staticmethod
    def _validate_container_path(path: str) -> None:
        if not path.startswith("/") or "\x00" in path:
            raise ValueError("container archive path must be absolute")

    @staticmethod
    def _run(argv: builtins.list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
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

    @staticmethod
    def _run_bytes(
        argv: builtins.list[str],
        *,
        timeout: float,
        input_bytes: bytes | None = None,
        stdin: BinaryIO | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                argv,
                env={**os.environ},
                input=input_bytes,
                stdin=stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"Docker archive command failed: {exc}") from exc


#: The member reaper's exit when no process receipt appeared in its wait.
_REAPER_NO_RECEIPT = 125

#: How long a killed command whose receipt is gone may take to finish exiting.
_RECEIPT_EXIT_GRACE_S = 5.0


def _member_kill_failure(returncode: int, stderr: str) -> SandboxError:
    """Name a failed member reaper run.

    Only the reaper's own silent exit means the receipt is missing: a Docker
    exec failure that shares the code always explains itself on stderr.
    """
    if returncode == _REAPER_NO_RECEIPT and not stderr.strip():
        return MemberProcessReceiptMissing("resident process receipt is missing")
    return SandboxError("resident process cleanup failed: " + stderr[:300])


class _RetainedExecProcess(SandboxProcess):
    """An attached ``docker exec`` with container- or member-scoped cleanup."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        process_pidfile: str | None,
        user_uid: int | None,
        user_gid: int | None,
    ) -> None:
        self._process = process
        self._manager = manager
        self._spec = spec
        self._process_pidfile = process_pidfile
        self._user_uid = user_uid
        self._user_gid = user_gid
        self._stderr_tail = TextTail()
        self._stderr_thread: threading.Thread | None = None
        if process.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                daemon=True,
                name="retained-sandbox-stderr",
            )
            self._stderr_thread.start()

    @property
    def container(self) -> str:
        return self._spec.container_name

    def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        for chunk in text_chunks(self._process.stderr):
            self._stderr_tail.append(chunk)

    def lines(self) -> Iterator[str]:
        try:
            yield from protocol_lines(self.chunks())
        except SandboxError:
            # Docker's attached exec channels can backpressure cleanup execs.
            # Discard any remaining stdout while the scoped reaper runs.
            drain = OutputPump(self.chunks(), name="oversized-resident-output")
            drain.close(drain=True)
            self.kill()
            raise

    def chunks(self) -> Iterator[str]:
        assert self._process.stdout is not None
        yield from text_chunks(self._process.stdout)

    def write_line(self, text: str) -> None:
        if self._process.stdin is None:
            raise SandboxError("this retained exec was not spawned with live input")
        self._process.stdin.write(text.rstrip("\n") + "\n")
        self._process.stdin.flush()

    def close_stdin(self) -> None:
        if self._process.stdin is not None:
            with contextlib.suppress(OSError):
                self._process.stdin.close()

    def poll(self) -> int | None:
        return self._process.poll()

    def kill(self) -> None:
        """Kill only this verified exec process group, never its container."""
        try:
            if self._process_pidfile is None:
                raise SandboxError("retained exec has no process-scoped kill receipt")
            assert self._user_uid is not None and self._user_gid is not None
            try:
                self._manager.kill_member_process(
                    self._spec,
                    pidfile=self._process_pidfile,
                    user_uid=self._user_uid,
                    user_gid=self._user_gid,
                )
            except MemberProcessReceiptMissing:
                # A resident CLI exits on its own after stdin EOF, and its
                # launcher then deletes the receipt. Once this exec has
                # returned too, nothing is left to signal. A receipt missing
                # while the exec still runs remains a failed kill.
                if not self._exits_within(_RECEIPT_EXIT_GRACE_S):
                    raise
        finally:
            if self._process.poll() is None:
                self._process.kill()

    def _exits_within(self, seconds: float) -> bool:
        try:
            self._process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return False
        return True

    def wait(self) -> int:
        return self._process.wait()

    def stderr_text(self) -> str:
        # A resident CLI holds stderr open for its whole life, so the drain
        # reaches EOF only after the exec exits. Waiting on a live process
        # would hold every turn's result for the full timeout; the tail
        # collected so far is the answer until then.
        if self._stderr_thread is not None and self._process.poll() is not None:
            self._stderr_thread.join(timeout=5)
        return self._stderr_tail.text()


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _terminal_rcfile(pidfile: str) -> str:
    """Name the bash startup file that belongs to one terminal's pidfile."""
    return pidfile.removesuffix(".pid") + ".bashrc"


def _terminal_bashrc(pidfile: str, rcfile: str) -> str:
    """Return the startup file a terminal's interactive bash reads.

    It deletes itself first: bash has read it whole by then, and its path
    stays in bash's argv, which is what marks the process as this terminal's.
    """
    lines = (
        f"rm -f -- {shlex.quote(rcfile)}",
        "[ -r ~/.bashrc ] && . ~/.bashrc",
        # bash re-enables job control after reading its startup files, which
        # would put each command in a process group of its own. Switching it
        # off before every prompt keeps commands in the group the sandbox manager
        # suspends and signals.
        'PROMPT_COMMAND="set +m${PROMPT_COMMAND:+;$PROMPT_COMMAND}"',
        f"trap {shlex.quote('rm -f -- ' + shlex.quote(pidfile))} EXIT",
    )
    return "\n".join(lines) + "\n"


class _RetainedTerminalProcess(TerminalProcess):
    """One Docker-exec PTY whose lifecycle never removes the chat container."""

    # The wrapper falls back to ``sh`` only for an image without bash; the
    # sandbox image this platform ships always has it.
    shell = "bash"

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        pidfile: str,
        input_fd: int,
        operation_uid: int | None = None,
    ) -> None:
        self._process = process
        self._manager = manager
        self._spec = spec
        self._pidfile = pidfile
        self._operation_uid = operation_uid
        self._input_fd: int | None = input_fd
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._killed = False
        self._suspended = False

    def chunks(self) -> Iterator[bytes]:
        # The output side reads its own descriptor for the PTY master, so
        # closing the input side (kill, wait) can neither cut off output still
        # buffered nor hand this read a reused descriptor number.
        with self._write_lock:
            if self._input_fd is None:
                return
            output_fd = os.dup(self._input_fd)
        try:
            while True:
                try:
                    chunk = os.read(output_fd, 64 * 1024)
                except OSError as exc:
                    # EIO: the Docker client exited and closed the last
                    # descriptor for the PTY's other end.
                    if exc.errno == errno.EIO:
                        return
                    raise
                if not chunk:
                    return
                yield chunk
        finally:
            os.close(output_fd)

    def write(self, data: bytes) -> None:
        with self._write_lock:
            if self._process.poll() is not None or self._input_fd is None:
                raise SandboxError("terminal is closed")
            try:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(self._input_fd, remaining)
                    if written == 0:
                        raise OSError("terminal input closed")
                    remaining = remaining[written:]
            except OSError as exc:
                raise SandboxError("terminal is closed") from exc

    def resize(self, rows: int, cols: int) -> None:
        with self._write_lock:
            if self._input_fd is None or self._process.poll() is not None:
                raise SandboxError("terminal is closed")
            try:
                _set_pty_size(self._input_fd, rows, cols)
                # Docker CLI watches its controlling TTY on SIGWINCH and
                # forwards the new dimensions to this exec instance.
                os.kill(self._process.pid, signal.SIGWINCH)
            except OSError as exc:
                raise SandboxError("terminal cannot be resized") from exc

    def _signal_tree(self, signal_name: str) -> None:
        script = (
            'attempt=0; while [ ! -r "$1" ]; do '
            'attempt=$((attempt + 1)); test "$attempt" -lt 100 || exit 3; '
            'sleep 0.05; done; pid=$(cat "$1") || exit 4; '
            'case "$pid" in ""|*[!0-9]*) exit 2;; esac; '
            # The minimal sandbox image intentionally has no /bin/kill. The
            # POSIX shell builtin supports negative process-group operands.
            f'kill -{signal_name} -"$pid" 2>/dev/null'
        )
        result = self._manager.exec_capture(
            self._spec,
            ["/bin/sh", "-c", script, "sh", self._pidfile],
            timeout_s=15,
            user_uid=self._operation_uid,
            user_gid=self._manager.backend.gid if self._operation_uid is not None else None,
        )
        # Natural exit can win the small poll-to-signal race. That is already
        # the requested end state; a live client without a confirmed signal
        # must fail closed so protected workspace phases cannot overlap it.
        if result.returncode != 0 and self._process.poll() is None:
            raise SandboxError(f"terminal cannot receive {signal_name}")

    def _set_suspended(self, suspended: bool) -> None:
        with self._state_lock, self._write_lock:
            if self._killed or self._process.poll() is not None:
                return
            if self._suspended is suspended:
                return
            self._signal_tree("STOP" if suspended else "CONT")
            self._suspended = suspended

    def suspend(self) -> None:
        self._set_suspended(True)

    def resume(self) -> None:
        self._set_suspended(False)

    def _close_input(self) -> None:
        with self._write_lock:
            input_fd = self._input_fd
            self._input_fd = None
            if input_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(input_fd)

    def kill(self) -> None:
        with self._state_lock:
            if self._killed:
                return
            if self._operation_uid is not None:
                from cloud_agent_host.environment_process import kill_identity

                kill_identity(RetainedChatBackend(self._manager, self._spec), self._operation_uid)
                self._close_input()
                if self._process.poll() is None:
                    self._process.kill()
                self._process.wait(timeout=5)
                self._killed = True
                return
            if self._process.poll() is None:
                # A hangup, as when a terminal window closes. An interactive
                # shell ignores SIGTERM, so a TERM would always wait out the
                # deadline below before the KILL.
                self._signal_tree("HUP")
                deadline = time.monotonic() + 3
                while self._process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
            if self._process.poll() is None:
                self._signal_tree("KILL")
            if self._process.poll() is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(self._process.pid, signal.SIGKILL)
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise SandboxError("terminal Docker exec could not be stopped") from exc
            self._close_input()
            self._killed = True

    def wait(self) -> int:
        try:
            return self._process.wait()
        finally:
            self._close_input()


#: How long a turn's commands rely on a proof that the group bridge is in
#: place before checking again: the minute the host guard already trusts its
#: own last verification for.
TURN_NETWORK_PROOF_S = GUARD_RECHECK_S


class RetainedChatBackend(SandboxBackend):
    """Present one retained container through the runner's sandbox contract.

    This adapter is what lets runtime parsing, setup, patch building and Agent
    CLI drivers execute with ``docker exec``. It never interprets a host
    ``workdir`` or ``state_dir``: both canonical paths already live in the
    container writable layer.
    """

    name = "retained-container"

    def __init__(
        self,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        *,
        network_proved_at: float | None = None,
    ) -> None:
        self.manager = manager
        self.spec = spec
        self.image = spec.sandbox_image_digest or manager.backend.image
        self.runtime = manager.backend.runtime
        # Monotonic time the group bridge was last proved, for a turn's backend
        # only; ``None`` checks before every command.
        self._network_proved_at = network_proved_at

    @property
    def provides_isolation(self) -> bool:
        """Report the retained container as the outer execution boundary."""
        return True

    @property
    def is_vm_isolated(self) -> bool:
        """Mirror whether the underlying container runtime uses a guest VM."""
        return self.manager.backend.is_vm_isolated

    def sandbox_metadata(self) -> dict[str, str]:
        """Describe the retained lifecycle alongside backend isolation metadata."""
        return {
            **self.manager.backend.sandbox_metadata(),
            "sandbox_lifecycle": "restartable",
            "sandbox_id": self.spec.sandbox_id,
        }

    def version_stamp(self) -> dict[str, Any]:
        """Return a stamp that describes the retained image, not today's image."""
        stamp = self.manager.backend.version_stamp()
        if self.spec.sandbox_image_digest:
            stamp = {
                **stamp,
                "sandbox_image_digest": self.spec.sandbox_image_digest,
            }
        if self.spec.runtime_version:
            versions = dict(stamp.get("runtime_versions") or {})
            versions[self.spec.runtime_id] = self.spec.runtime_version
            available = dict(stamp.get("runtime_versions_available") or {})
            available[self.spec.runtime_id] = [self.spec.runtime_version]
            stamp = {
                **stamp,
                "runtime_versions": versions,
                "runtime_versions_available": available,
            }
        return stamp

    def base_env(self) -> dict[str, str]:
        """Return the underlying backend's hermetic sandbox environment."""
        return self.manager.backend.base_env()

    def has_binary(self, name: str) -> bool:
        """Check for one binary inside the exact retained container."""
        if not name or "\x00" in name:
            return False
        result = self.manager.exec_capture(
            self.spec,
            ["/bin/sh", "-c", 'command -v -- "$1" >/dev/null 2>&1', "sh", name],
            timeout_s=30,
        )
        return result.returncode == 0

    def require_network(self, *, observer: StageObserver | None = None) -> None:
        """Prove the container is on its guarded group bridge before running anything.

        A turn's backend starts from the proof its activation made: the sandbox manager
        checked exactly this when it acquired the turn, moments before. That
        proof, and each one this backend makes itself, stands for
        :data:`TURN_NETWORK_PROOF_S`; a command after that, such as one after a
        long setup script or at the end of a long turn, checks again. A backend
        built without a proof checks before every command.
        """
        proved_at = self._network_proved_at
        if proved_at is not None and time.monotonic() - proved_at < TURN_NETWORK_PROOF_S:
            return
        started = time.monotonic()
        self.manager.require_network(self.spec, observer=observer)
        if proved_at is not None:
            self._network_proved_at = started

    def spawn(self, spec: SandboxSpec) -> SandboxProcess:
        """Execute one Agent CLI or bounded command in the retained container."""
        self.require_network()
        pidfile = f"{ACTIVATION_TMPFS}/command-{secrets.token_hex(16)}.pid"
        return self.manager.spawn(
            self.spec,
            spec.argv,
            env=dict(spec.env),
            workdir=CHAT_WORKDIR,
            live_input=spec.live_input,
            user_uid=self.manager.backend.uid,
            user_gid=self.manager.backend.gid,
            process_pidfile=pidfile,
        )

    def spawn_environment_script(
        self, *, phase: str, script: str, env: dict[str, str]
    ) -> SandboxProcess:
        """Give install, services and verification independent non-root identities."""
        from cloud_agent_host.environment_process import EnvironmentProcess

        return EnvironmentProcess(self, phase=phase, script=script, env=env)

    def spawn_terminal(self, spec: SandboxSpec, *, rows: int, cols: int) -> TerminalProcess:
        """Open an interactive PTY in this chat's retained workspace."""
        if not (spec.workdir == CHAT_WORKDIR or spec.workdir.startswith(CHAT_WORKDIR + "/")):
            raise SandboxError("retained terminal must run in the chat workspace")
        return self.manager.spawn_terminal(
            self.spec,
            env=spec.env,
            workdir=spec.workdir,
            rows=rows,
            cols=cols,
            argv=spec.argv if len(spec.argv) > 1 else None,
            user_uid=int(spec.env["AGENT_ACTION_UID"]) if "AGENT_ACTION_UID" in spec.env else None,
        )

    def preflight(self) -> None:
        """Use the container backend checks without probing a host bind mount."""
        self.manager.backend.preflight()

    def check_gateway_reachable(self, base_url: str) -> None:
        """Delegate the backend's isolated gateway reachability probe."""
        self.manager.backend.check_gateway_reachable(base_url)


def member_inference_env_path(slot_id: int) -> str:
    """Where one member's owner inference credentials wait for its bash."""
    return f"{MEMBER_TMPFS_ROOT}/{slot_id}/inference-env.sh"


__all__ = [
    "ACTIVATION_TMPFS",
    "CHAT_CONTAINER_PREFIX",
    "CHAT_GENERATION_LABEL",
    "CHAT_IMAGE_DIGEST_LABEL",
    "CHAT_NAMESPACE_LABEL",
    "CHAT_RESIDENT_CAPACITY_LABEL",
    "CHAT_RUNTIME_LABEL",
    "CHAT_RUNTIME_VERSION_LABEL",
    "CHAT_SANDBOX_ID_LABEL",
    "CHAT_SANDBOX_LABEL",
    "CHAT_SHARED_LABEL",
    "CHAT_STATE_DIR",
    "CHAT_THREAD_LABEL",
    "CHAT_UPLOADS_DIR",
    "CHAT_WORKDIR",
    "MEMBER_TMPFS_ROOT",
    "MEMBER_UID_BASE",
    "MEMBER_UID_LIMIT",
    "TURN_NETWORK_PROOF_S",
    "ChatSandboxInspection",
    "ChatSandboxManager",
    "ChatSandboxSpec",
    "ChatSandboxStatus",
    "RetainedChatBackend",
    "RetainedChatFailStopped",
    "RetainedChatLost",
    "member_inference_env_path",
]
