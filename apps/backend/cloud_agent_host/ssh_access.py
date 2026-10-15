"""Apply a sandbox group's SSH state: member files in the guest, exceptions on the host.

The control plane sends the whole state (:class:`cloud_agent_shared.ssh_access.SshAccess`)
at every turn of a group that has one, through the runner, and pushes it
whenever its owner changes something. This module converges the retained
container and the host toward it, in three steps under one per-group lock:

1. **The newest state, as guest root.** A root-owned file under
   ``/run/agent-members`` (which members cannot write) records the newest
   state this group was sent: its revision and, per chat, the digest of every
   server entry it allows (no keys). A state at least as new replaces it; an
   older one is still applied, but only as far as the newest allows, so a late
   delivery cannot reopen what a newer one closed, and is never skipped
   either. The same step removes the SSH directory of every slot whose chat the
   newest state allows nothing.
2. **Host exceptions.** For a state that is the newest, the group bridge's
   ``ssh_targets`` become exactly its ``IPv4:port`` pairs. An older state
   leaves them alone.
3. **Member files, as each member's own UID.** Keys are written only for the
   member whose turn the state starts, from that turn's own claim, so every key
   in a member's sandbox is one its running turn's output redactor knows. Every
   other member, and every member on a push, can only lose servers: a server
   the newest state no longer allows, or allows with another key, is removed.
   A server enabled during a turn arrives with the member's next turn. Writing
   as the member means a symlink the member planted can only ever redirect a
   write to somewhere the member could already write.

When a newest state fails after it was recorded, the bridge's exceptions are
cleared, so the group fails closed until a state applies whole.

Keys travel on stdin, never in argv, and live only in tmpfs: they are gone when
the container stops and never reach the workspace, a snapshot or an archive.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from typing import TYPE_CHECKING, Any

from cloud_agent_host.chat_sandbox import MEMBER_TMPFS_ROOT
from cloud_agent_host.chat_sandbox_models import MEMBER_UID_BASE
from cloud_agent_shared.errors import SandboxError
from cloud_agent_shared.ssh_access import HOST_KEY_ALGORITHMS

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_models import ChatSandboxSpec
    from cloud_agent_shared.ssh_access import SshAccess, SshServerAccess

#: Root-owned, in a directory members cannot write, outside every member tmpfs.
STATE_FILE = f"{MEMBER_TMPFS_ROOT}/.ssh-access-state"
_TIMEOUT_S = 30
_DIGEST = re.compile(r"[0-9a-f]{64}")

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _group_lock(sandbox_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(sandbox_id, threading.Lock())


def ssh_dir(slot_id: int) -> str:
    """Where one member's SSH files live: its private tmpfs."""
    return f"{MEMBER_TMPFS_ROOT}/{slot_id}/ssh"


def server_dir(slot_id: int, server: SshServerAccess) -> str:
    """One server's files, removable on their own when only that server goes."""
    return f"{ssh_dir(slot_id)}/servers/{server.server_id}"


def member_home(slot_id: int) -> str:
    """The member's passwd home, where OpenSSH reads ``~/.ssh/config``."""
    return f"/home/agent-member-{slot_id}"


def include_line(slot_id: int) -> str:
    """The one line the member's ``~/.ssh/config`` starts with."""
    return f"Include {ssh_dir(slot_id)}/servers/*/config"


def host_key_alias(server: SshServerAccess) -> str:
    """The name a server's pinned key is filed under, whatever its address."""
    return f"cloud-agent-ssh-{server.server_id}"


def render_config(slot_id: int, server: SshServerAccess) -> str:
    """A server's ``Host`` block, pinned to its address, key and host key."""
    base = server_dir(slot_id, server)
    return (
        "\n".join(
            [
                "# Written by the platform for this chat's SSH server; replaced every turn.",
                f"Host {server.name}",
                f"  HostName {server.address}",
                f"  Port {server.port}",
                f"  User {server.username}",
                f"  IdentityFile {base}/key",
                "  IdentitiesOnly yes",
                "  IdentityAgent none",
                f"  UserKnownHostsFile {base}/known_hosts",
                "  GlobalKnownHostsFile /dev/null",
                f"  HostKeyAlias {host_key_alias(server)}",
                f"  HostKeyAlgorithms {HOST_KEY_ALGORITHMS[server.host_key_type]}",
                "  StrictHostKeyChecking yes",
                "  UpdateHostKeys no",
                "  CheckHostIP no",
                "  BatchMode yes",
                "  PreferredAuthentications publickey",
                "  PasswordAuthentication no",
                "  KbdInteractiveAuthentication no",
                "  ForwardAgent no",
                "  ForwardX11 no",
                "  ConnectTimeout 15",
                "  ServerAliveInterval 30",
                "  ServerAliveCountMax 4",
            ]
        )
        + "\n"
    )


def render_known_hosts(server: SshServerAccess) -> str:
    """The server's pinned host key, under its alias."""
    return f"{host_key_alias(server)} {server.host_key_type} {server.host_key}\n"


def write_payload(slot_id: int, servers: list[SshServerAccess]) -> dict[str, Any]:
    """What the member-side script writes for a starting turn, keys included. Never log it."""
    return {
        "mode": "write",
        "home": member_home(slot_id),
        "ssh_dir": ssh_dir(slot_id),
        "include": include_line(slot_id),
        "servers": [
            {
                "id": server.server_id,
                "digest": server.digest(),
                "config": render_config(slot_id, server),
                "known_hosts": render_known_hosts(server),
                "key": server.private_key,
            }
            for server in sorted(servers, key=lambda item: item.name)
        ],
    }


def narrow_payload(slot_id: int, keep: set[str]) -> dict[str, Any]:
    """What the member-side script keeps of what it holds: the entries with these digests."""
    return {"mode": "narrow", "ssh_dir": ssh_dir(slot_id), "keep": sorted(keep)}


# Guest root, trusted host input only. /run/agent-members is root-owned and
# only searchable by members, so nothing a member does can put a file at the
# state path or replace a slot's mount point; both are still opened without
# following links. A slot directory is the member's own tmpfs: it is opened
# with O_NOFOLLOW and its ``ssh`` entry is removed relative to that handle,
# never through a path, whatever the member put there.
_GATE_SCRIPT = r"""
import errno
import json
import os
import shutil
import stat
import sys

request = json.load(sys.stdin)
path, root = request["state_file"], request["root"]
info = os.lstat(root)
if not stat.S_ISDIR(info.st_mode) or info.st_uid != request["owner_uid"] or info.st_mode & 0o022:
    raise SystemExit("member root is not protected")
newest = {"revision": 0, "members": {}}
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
except FileNotFoundError:
    pass
else:
    with os.fdopen(fd, encoding="utf-8") as handle:
        newest = json.load(handle)
fresh = request["revision"] >= newest["revision"]
if fresh:
    newest = {"revision": request["revision"], "members": request["members"]}
    temporary = path + ".tmp"
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(newest, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
keep = sorted(
    {slot for thread, slot in request["slots"].items() if newest["members"].get(thread)}
)
for slot in range(request["capacity"]):
    if slot in keep:
        continue
    try:
        slot_fd = os.open(
            os.path.join(root, str(slot)), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except (FileNotFoundError, NotADirectoryError):
        continue
    except OSError as error:
        if error.errno == errno.ELOOP:
            continue
        raise
    try:
        try:
            found = os.lstat("ssh", dir_fd=slot_fd)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(found.st_mode):
            shutil.rmtree("ssh", dir_fd=slot_fd)
        else:
            os.unlink("ssh", dir_fd=slot_fd)
    finally:
        os.close(slot_fd)
json.dump(
    {
        "fresh": fresh,
        "revision": newest["revision"],
        "members": newest["members"],
        "keep_slots": keep,
    },
    sys.stdout,
)
"""

# The member's own UID. ``write`` builds the new directory beside the old one
# and swaps it in, so a reader never sees half a configuration, then makes sure
# the first line of its ``~/.ssh/config`` includes it. OpenSSH restores the
# including file's ``Host`` context after an ``Include``, so the owner's own
# lines below it keep applying to every host. ``narrow`` only removes: each
# server's directory goes out of the include's glob in one rename.
_MEMBER_SCRIPT = r"""
import json
import os
import shutil
import sys

request = json.load(sys.stdin)
target = request["ssh_dir"]


def discard(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.unlink(path)


if request["mode"] == "narrow":
    keep = set(request["keep"])
    servers = os.path.join(target, "servers")
    try:
        entries = os.listdir(servers)
    except (FileNotFoundError, NotADirectoryError):
        entries = []
    for entry in entries:
        try:
            fd = os.open(os.path.join(servers, entry, "digest"), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, encoding="ascii") as handle:
                digest = handle.read().strip()
        except (OSError, ValueError):
            digest = None
        if digest not in keep:
            gone = os.path.join(target, ".gone-" + entry)
            discard(gone)
            os.rename(os.path.join(servers, entry), gone)
            discard(gone)
    raise SystemExit(0)


def write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o400)


staging = target + ".new"
discard(staging)
os.mkdir(staging, 0o700)
os.mkdir(os.path.join(staging, "servers"), 0o700)
for server in request["servers"]:
    directory = os.path.join(staging, "servers", server["id"])
    os.mkdir(directory, 0o700)
    write(os.path.join(directory, "config"), server["config"])
    write(os.path.join(directory, "known_hosts"), server["known_hosts"])
    write(os.path.join(directory, "key"), server["key"])
    write(os.path.join(directory, "digest"), server["digest"] + "\n")
old = target + ".old"
discard(old)
if os.path.lexists(target):
    os.rename(target, old)
os.rename(staging, target)
discard(old)

include = request["include"]
directory = os.path.join(request["home"], ".ssh")
os.makedirs(directory, 0o700, exist_ok=True)
os.chmod(directory, 0o700)
config = os.path.join(directory, "config")
try:
    with open(config, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
except FileNotFoundError:
    lines = []
if not lines or lines[0] != include:
    body = [line for line in lines if line.strip() != include]
    temporary = config + ".platform-tmp"
    if os.path.lexists(temporary):
        os.unlink(temporary)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join([include, *body]) + "\n")
    os.replace(temporary, config)
os.chmod(config, 0o600)
"""


def _newest(stdout: bytes) -> tuple[bool, int, dict[str, set[str]], set[int]]:
    """The gate's answer, checked: guest output decides nothing on the host but member files."""
    try:
        verdict = json.loads(stdout)
        fresh = verdict["fresh"]
        revision = verdict["revision"]
        members = {
            str(thread): {str(digest) for digest in digests}
            for thread, digests in verdict["members"].items()
        }
        keep = {int(slot) for slot in verdict["keep_slots"]}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SandboxError("SSH access state check returned unreadable output") from exc
    if not isinstance(fresh, bool) or not isinstance(revision, int):
        raise SandboxError("SSH access state check returned unreadable output")
    if any(not _DIGEST.fullmatch(digest) for digests in members.values() for digest in digests):
        raise SandboxError("SSH access state check returned unreadable output")
    return fresh, revision, members, keep


def apply_ssh_access(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    access: SshAccess,
    *,
    starting_thread_id: str | None = None,
) -> dict[str, Any]:
    """Converge one running group toward ``access`` and the newest state it was sent.

    ``starting_thread_id`` is the member whose turn this state starts, the only
    one that receives keys; a push names none. ``newest`` is False for a state
    older than one already recorded, which was applied only as far as the
    newest allows. The caller proves the group network first, so the bridge
    exists and the host rules are verified.
    """
    residents = access.resident_members()
    slots = {member.thread_id: int(member.slot_id or 0) for member in residents}
    if any(slot >= spec.resident_slot_capacity for slot in slots.values()):
        raise SandboxError("SSH access names a slot outside this sandbox")
    with _group_lock(spec.sandbox_id):
        gate = manager.exec_capture(
            spec,
            ["/usr/bin/python3", "-I", "-c", _GATE_SCRIPT],
            workdir="/",
            timeout_s=_TIMEOUT_S,
            input_bytes=json.dumps(
                {
                    "revision": access.revision,
                    "members": {
                        member.thread_id: sorted(server.digest() for server in member.servers)
                        for member in access.members
                        if member.servers
                    },
                    "slots": slots,
                    "state_file": STATE_FILE,
                    "root": MEMBER_TMPFS_ROOT,
                    "owner_uid": 0,
                    "capacity": spec.resident_slot_capacity,
                }
            ).encode(),
            user_uid=0,
            user_gid=0,
        )
        if gate.returncode != 0:
            raise SandboxError(
                "SSH access state could not be checked: "
                + gate.stderr.decode("utf-8", "replace")[-300:]
            )
        fresh, revision, allowed, keep = _newest(gate.stdout)
        try:
            if fresh:
                manager.sync_ssh_targets(spec, access.targets())
            for member in residents:
                slot = slots[member.thread_id]
                if slot not in keep:
                    continue
                permitted = allowed.get(member.thread_id, set())
                if member.thread_id == starting_thread_id:
                    payload = write_payload(
                        slot, [server for server in member.servers if server.digest() in permitted]
                    )
                else:
                    payload = narrow_payload(slot, permitted)
                result = manager.exec_capture(
                    spec,
                    ["/usr/bin/python3", "-I", "-c", _MEMBER_SCRIPT],
                    workdir="/",
                    timeout_s=_TIMEOUT_S,
                    input_bytes=json.dumps(payload).encode(),
                    user_uid=MEMBER_UID_BASE + slot,
                    user_gid=manager.backend.gid,
                )
                if result.returncode != 0:
                    raise SandboxError(
                        f"SSH files for member slot {slot} could not be written: "
                        + result.stderr.decode("utf-8", "replace")[-300:]
                    )
        except Exception:
            if fresh:
                # The newest state is recorded but not in place: close the
                # bridge rather than leave an older selection open behind it.
                with contextlib.suppress(Exception):
                    manager.sync_ssh_targets(spec, set())
            raise
    return {"newest": fresh, "revision": revision}


__all__ = [
    "STATE_FILE",
    "apply_ssh_access",
    "host_key_alias",
    "include_line",
    "member_home",
    "narrow_payload",
    "render_config",
    "render_known_hosts",
    "server_dir",
    "ssh_dir",
    "write_payload",
]
