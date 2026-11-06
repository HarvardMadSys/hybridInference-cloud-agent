"""The shared workspace and private member homes inside a retained chat container.

:class:`WorkspaceFilesMixin` is the part of
:class:`~cloud_agent_host.container.chat_sandbox.ChatSandboxManager` that keeps
the group-writable workspace and state roots, secures the collections each
member's provider home lives in, resets a container's unsettled state, and
moves trees in and out of it: a base bundle, an imported archive, and a bounded
``docker cp`` export.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import selectors
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING

from cloud_agent_host.container import chat_sandbox_export
from cloud_agent_host.container.chat_sandbox_members import MemberExecMixin
from cloud_agent_host.container.chat_sandbox_models import (
    ACTIVATION_TMPFS,
    CHAT_STATE_DIR,
    CHAT_WORKDIR,
    CLAUDE_SHARED_PROJECTS_DIR,
    MEMBER_UID_BASE,
    ChatSandboxSpec,
    RetainedChatLost,
)
from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.container.stream_input import require_nonempty_rewindable

if TYPE_CHECKING:
    from typing import BinaryIO

    from _typeshed import SupportsWrite


#: ``docker cp`` time allowed per GiB of an imported archive, and the floor for
#: a smaller one: the two minutes a 1 GiB checkout always had, kept in step with
#: a host that raised its source import limit.
ARCHIVE_IMPORT_TIMEOUT_PER_GIB_S = 120.0
#: Export gets the same per-GiB allowance as import. The floor also bounds a
#: Docker daemon that accepts the request but never produces the first tar
#: header; callers additionally provide a byte ceiling for the path itself.
ARCHIVE_EXPORT_TIMEOUT_PER_GIB_S = ARCHIVE_IMPORT_TIMEOUT_PER_GIB_S
#: Where Codex, OpenCode and Kilo keep one mode-0700 provider home per member
#: (``member_homes.PRIVATE_MEMBER_STATE_ROOTS``). Only a home's own
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
#: What both permission passes do to an entry they reach: open a directory to
#: the group (rwx and setgid, ``-2070``) or a file (rw, ``-0060``) unless it
#: already is. One walk does both, and an entry that already has its bits is
#: left alone, so a pass over a tree that has not changed since the last one
#: changes nothing: no chmod, and no new ctime for Git's index to re-check.
#: The two branches are one parenthesized group so that a ``-uid`` written
#: before it applies to both: ``find`` binds an implicit AND tighter than
#: ``-o``.
_OPEN_TO_GROUP = (
    "\\( \\( -type d ! -perm -2070 -exec chmod g+rwx,g+s -- {} + \\) "
    "-o \\( -type f ! -perm -0060 -exec chmod g+rw -- {} + \\) \\)"
)
SHARED_WORKSPACE_PERMISSIONS_SCRIPT = (
    "set -eu; me=$(id -u); "
    f"find {CHAT_WORKDIR} {CHAT_STATE_DIR} -xdev {_NOT_OURS_AND_UNSEARCHABLE} "
    f'-o -uid "$me" {_OPEN_TO_GROUP}; '
    f"git -C {CHAT_WORKDIR} config core.sharedRepository group"
)
_FULL_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


class WorkspaceFilesMixin(MemberExecMixin):
    """Keep the shared workspace and member homes, and copy trees in and out; part of ``ChatSandboxManager``."""

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
        # This runs at the end of every turn, so it walks each root once and
        # touches only what the turn left without its group bits.
        trusted_prunes = "".join(f"-o -path {path} " for path in PRIVATE_MEMBER_HOME_COLLECTIONS)
        script = (
            "set -eu; uid=$1; "
            f'normalize() {{ find "$@" -xdev -uid "$uid" {_OPEN_TO_GROUP}; }}; '
            f"normalize {CHAT_WORKDIR}; "
            f"find {CHAT_STATE_DIR} -xdev "
            f"\\( -path {CHAT_STATE_DIR}/.platform "
            f"-o -path {CHAT_STATE_DIR}/claude "
            f"-o -path {CHAT_STATE_DIR}/codex-members "
            f"-o -path {CHAT_STATE_DIR}/opencode-members "
            f"-o -path {CHAT_STATE_DIR}/kilo-members "
            f"{trusted_prunes}\\) -prune -o "
            f'-uid "$uid" {_OPEN_TO_GROUP}; '
            f"if test -d {CLAUDE_SHARED_PROJECTS_DIR}; then "
            f"normalize {CLAUDE_SHARED_PROJECTS_DIR}; fi"
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
        bundle: BinaryIO,
        *,
        commit: str,
        env: dict[str, str] | None = None,
    ) -> None:
        """Import a follow-up's base objects without changing workspace Git state.

        The bundle crosses stdin into activation tmpfs and is removed before
        this command returns. ``git bundle unbundle`` adds objects only: it
        does not move HEAD, refs, the real index, or working-tree files.
        """
        require_nonempty_rewindable(bundle)
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
            stdin=bundle,
        )
        bundle.seek(0)
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
        self.exact.require(spec)
        self._validate_container_path(path)
        if isinstance(archive, bytes):
            size, input_bytes, stdin = len(archive), archive, None
        else:
            size = os.fstat(archive.fileno()).st_size - archive.tell()
            input_bytes, stdin = None, archive
        result = self.exact.run_bytes(
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
        current = self.exact.require(spec)
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
                total = chat_sandbox_export.pump_archive_export(
                    process,
                    process.stdout,
                    selector,
                    destination,
                    export_deadline=export_deadline,
                    max_bytes=max_bytes,
                    path=path,
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
                chat_sandbox_export.raise_archive_export_failure(
                    stderr, path=path, container_id=current.container_id, total=total
                )
        return total

    @staticmethod
    def _validate_container_path(path: str) -> None:
        if not path.startswith("/") or "\x00" in path:
            raise ValueError("container archive path must be absolute")


__all__ = [
    "ARCHIVE_EXPORT_TIMEOUT_PER_GIB_S",
    "ARCHIVE_IMPORT_TIMEOUT_PER_GIB_S",
    "LEGACY_MEMBER_HOME_COLLECTIONS",
    "PRIVATE_MEMBER_HOME_COLLECTIONS",
    "SHARED_WORKSPACE_PERMISSIONS_SCRIPT",
    "WorkspaceFilesMixin",
]
