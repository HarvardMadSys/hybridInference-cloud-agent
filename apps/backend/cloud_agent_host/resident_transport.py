"""Supervisor-owned live Agent CLI transports inside retained containers.

The runner owns attempts; this module owns processes.  A transport is keyed by
one sandbox membership and survives the runner request that delivered a turn.
It deliberately contains no control-plane client, so a sandbox process can
never inherit the worker credential used to mutate jobs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from cloud_agent_host.chat_sandbox import (
    CHAT_STATE_DIR,
    CHAT_WORKDIR,
    CLAUDE_SHARED_PROJECTS_DIR,
    LEGACY_MEMBER_HOME_COLLECTIONS,
    MEMBER_TMPFS_ROOT,
    MEMBER_UID_BASE,
    PRIVATE_MEMBER_HOME_COLLECTIONS,
    ChatSandboxManager,
    ChatSandboxSpec,
    claude_member_config_dir,
)
from cloud_agent_host.codex_plugins import install_codex_plugins
from cloud_agent_host.drivers import (
    DialectRuntime,
    DriverError,
    HarnessEvent,
    HarnessTurn,
    InputOutcome,
    LaunchContext,
    TurnInput,
    TurnResult,
    get_driver,
)
from cloud_agent_host.other_plugins import install_other_runtime_plugins
from cloud_agent_host.plugins import install_claude_plugins
from cloud_agent_host.runtimes import RuntimeMCPConfig
from cloud_agent_host.sandbox import SANDBOX_PATH, SandboxProcess, runtime_bin_dir
from cloud_agent_shared.plugins import validate_sandbox_plugins

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Iterator

    from cloud_agent_shared.environment import BoundEnvironment

#: The dialect a resident transport is speaking; the transport only reads the
#: session it was handed, so the parameter stays covariant and a caller that
#: knows a narrower runtime can still hand it one.
_RuntimeT_co = TypeVar("_RuntimeT_co", bound=DialectRuntime, covariant=True)
#: The session contract itself is stated invariantly: a session hands back the
#: turn type of exactly the runtime it was opened for.
_SessionRuntimeT = TypeVar("_SessionRuntimeT", bound=DialectRuntime)


# Provider homes contain files on which the owning CLI performs owner-only
# chmod, SQLite, or project-lock operations. Shared workspace membership uses
# distinct UIDs and distinct live processes, so fork siblings cannot safely
# point those CLIs at one common provider home. The workspace remains shared;
# the state captured at the native fork boundary is copied into the child's
# private home before the two processes resume independently.
PRIVATE_MEMBER_STATE_ROOTS = dict(
    zip(("codex", "opencode", "kilo"), PRIVATE_MEMBER_HOME_COLLECTIONS, strict=True)
)
_LEGACY_MEMBER_STATE_ROOTS = dict(
    zip(("codex", "opencode", "kilo"), LEGACY_MEMBER_HOME_COLLECTIONS, strict=True)
)

#: Exit status of a member script that found its collection untrusted. The
#: caller secures the collections as the sandbox user and asks once more.
_UNTRUSTED_COLLECTION_EXIT = 75

# Each home is private to its member UID, not only distinct: its config and
# plugins run at every process start as that member, with that member's grant
# and MCP token. Mode 0700 keeps a sibling out of the home; the collection
# around it keeps a sibling from renaming the home away and putting its own
# tree at the same path between this check and the CLI's start. Every path
# component is therefore checked first: /agent-state and the collection must be
# sticky and owned by the sandbox user (``$trusted``), so no member UID can
# rename an entry it does not own (see
# ChatSandboxManager.ensure_private_member_collections). A home that is a
# symlink or belongs to another UID was put there by someone else and is
# refused rather than adopted. A sibling can still squat on an unused name,
# which only refuses that start. Shell prologue; expects ``$target`` and
# ``$trusted``.
_PRIVATE_MEMBER_HOME_CHECK = (
    "collection=${target%/*}; root=${collection%/*}; "
    'if test -z "$(find "$root" -maxdepth 0 -type d -uid "$trusted" -perm -1000)" '
    '|| test -z "$(find "$collection" -maxdepth 0 -type d -uid "$trusted" '
    '-perm -3770 ! -perm /0007)"; then '
    'echo "member provider collection is not trusted" >&2; '
    f"exit {_UNTRUSTED_COLLECTION_EXIT}; fi; "
    "umask 0077; "
)
# An earlier release kept the home in a member-created, group-writable
# collection, where a sibling could rename or replace it at any time. The
# member moves whatever is at that path into the trusted collection once, and
# only then judges it, where no sibling can swap it any more; a moved home is
# also narrowed to the member alone. Expects ``$legacy`` as well.
_PRIVATE_MEMBER_HOME_MIGRATE = (
    "migrated=; "
    'if test ! -e "$target" && test ! -L "$target" '
    '&& { test -e "$legacy" || test -L "$legacy"; }; then '
    'mv -T -- "$legacy" "$target"; migrated=1; fi; '
)
_PRIVATE_MEMBER_HOME_CLAIM = (
    'mkdir -p -- "$target"; '
    'if test -L "$target" || test ! -d "$target" '
    '|| test "$(stat -c %u -- "$target")" != "$(id -u)"; then '
    'echo "member provider home is not owned by this member" >&2; exit 1; fi; '
    'chmod u=rwx,go=,g-s -- "$target"; '
)
# Judged on every start, not only after a move: whether a home is adopted must
# never depend on an earlier start having managed to set a bad one aside. Every
# legitimate writer of a provider home runs as its member -- the CLI, plugin
# installation, lock cleanup, and the fork copy's unpack -- so an entry owned
# by any other UID was put there by someone else (through an earlier release's
# group-writable home). Such a home is refused, and first renamed to an
# unpredictable name so the next start can begin clean: a sibling can create
# entries in the sticky collection, and a guessable name it had taken would
# make the rename fail. If the rename still fails, the start is refused all the
# same. A walk that cannot be completed is refused too. The walk stops at the
# first foreign entry; a clean home costs one lstat per entry.
_PRIVATE_MEMBER_HOME_VERIFY = (
    'foreign=$(find "$target" -xdev ! -uid "$(id -u)" -print -quit); '
    'if test -n "$foreign"; then '
    "attempt=0; "
    'until aside=$(mktemp -u -- "$target.refused-XXXXXXXXXXXXXXXX") '
    '&& mv -T -- "$target" "$aside" 2>/dev/null; do '
    'attempt=$((attempt + 1)); test "$attempt" -lt 3 || break; done; '
    'echo "member provider home holds another UID files" >&2; exit 1; fi; '
)
_PRIVATE_MEMBER_HOME_NARROW_MIGRATED = (
    'if test -n "$migrated"; then '
    'find "$target" -xdev -mindepth 1 ! -type l -exec chmod go=,g-s -- {} +; fi; '
)
_PRIVATE_MEMBER_HOME_PROLOGUE = (
    _PRIVATE_MEMBER_HOME_CHECK
    + _PRIVATE_MEMBER_HOME_MIGRATE
    + _PRIVATE_MEMBER_HOME_CLAIM
    + _PRIVATE_MEMBER_HOME_VERIFY
    + _PRIVATE_MEMBER_HOME_NARROW_MIGRATED
)
#: For a home that has no earlier location: a fork child's.
_NEW_PRIVATE_MEMBER_HOME_PROLOGUE = _PRIVATE_MEMBER_HOME_CHECK + _PRIVATE_MEMBER_HOME_CLAIM

# Host-owned exactly-once receipts must not live inside a provider home. Some
# CLIs enumerate their whole state root, and the deliberately unreadable
# platform directory otherwise turns into a provider-side EACCES. This path is
# in the container writable layer (not tmpfs or a host mount), so docker stop /
# start preserves it just like /workspace and /agent-state.
PLATFORM_STATE_DIR = "/home/agent/.freeinference-platform"


@dataclass(frozen=True)
class ResidentLaunch:
    """Everything needed to open one member process, excluding its prompt."""

    thread_id: str
    runtime_id: str
    runtime_version: str | None
    native_session_id: str | None
    model: str
    reasoning_effort: str | None
    gateway_base_url: str
    credential: str | None
    mcp_server_ids: tuple[str, ...] = ()
    mcp_base_url: str = ""
    mcp_credential: str = ""
    slot_id: int = 0
    residency_generation: int = 0
    plugins: tuple[dict[str, str], ...] = ()
    # Short-lived, single-repository, contents/PR write GitHub App token. Kept in
    # this member's environment, never in shared workspace or image config.
    github_token: str | None = field(default=None, repr=False)
    github_repo: str = ""
    environment: BoundEnvironment | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.thread_id or not self.runtime_id or not self.model:
            raise ValueError("resident launch identity, runtime, and model are required")
        if not self.gateway_base_url:
            raise ValueError("resident launch gateway is required")
        if self.slot_id < 0:
            raise ValueError("resident slot id cannot be negative")
        if self.residency_generation < 0:
            raise ValueError("residency generation cannot be negative")
        object.__setattr__(
            self,
            "plugins",
            tuple(validate_sandbox_plugins(self.runtime_id, self.plugins)),
        )


class ResidentTurn(Generic[_RuntimeT_co]):
    """One activation borrowing a live transport without owning its process."""

    def __init__(self, turn: HarnessTurn[_RuntimeT_co]) -> None:
        self._turn = turn

    def lines(self) -> Iterator[HarnessEvent]:
        """Yield normalized driver events through the exact turn boundary."""
        for line in self._turn.raw_lines():
            yield from self._turn.events_for_line(line)

    def wait(self) -> TurnResult:
        """Return the settled result while leaving a healthy transport alive."""
        return self._turn.wait()

    def interrupt(self) -> None:
        """Interrupt only this activation through the driver's native seam."""
        self._turn.cancel()

    def inject(self, text: str, *, message_id: str) -> InputOutcome:
        """Hand ``text`` to this activation mid-turn through the driver's seam."""
        return self._turn.inject(text, message_id=message_id)

    def pending_questions(self) -> list[dict[str, Any]]:
        """Return native questions still waiting on this exact activation."""
        return self._turn.pending_questions()

    def answer_question(
        self, request_id: str, answers: dict[str, list[str]], *, reject: bool = False
    ) -> InputOutcome:
        """Reply to one native request through the activation that raised it."""
        return self._turn.answer_question(request_id, answers, reject=reject)

    def stderr_text(self) -> str:
        """Return bounded diagnostic stderr from the resident process."""
        return self._turn.stderr_text()


class _ResidentSession(Protocol[_SessionRuntimeT]):
    """The session surface one resident transport actually drives.

    ``HarnessSession`` is the shared base every driver can hand back, but only
    a live transport can be sent a turn, so the transport states that narrower
    contract instead of the base one.
    """

    retire_after_turn: bool

    @property
    def runtime(self) -> _SessionRuntimeT:
        """The dialect adapter this session speaks through."""
        ...

    def start_turn(self, turn_input: TurnInput) -> HarnessTurn[_SessionRuntimeT]:
        """Send one prompt to this already-open native session."""
        ...

    def close(self) -> None:
        """Release the process this session owns."""
        ...


class ResidentTransport(Generic[_RuntimeT_co]):
    """A single live native session owned by the host supervisor."""

    def __init__(
        self,
        *,
        launch: ResidentLaunch,
        session: _ResidentSession[_RuntimeT_co],
    ) -> None:
        self.launch = launch
        self._session = session

    @property
    def session(self) -> _ResidentSession[_RuntimeT_co]:
        """The live session this transport owns."""
        return self._session

    @property
    def native_session_id(self) -> str | None:
        """The runtime's observed locator, falling back to the resumed id."""
        observed = getattr(self.session.runtime, "session_id", None)
        return str(observed or self.launch.native_session_id or "").strip() or None

    @property
    def alive(self) -> bool:
        """Whether the underlying resident process still exists."""
        return bool(getattr(self.session, "process_alive", lambda: False)())

    @property
    def retire_after_turn(self) -> bool:
        """Whether native history requires EOF before accepting another user."""
        return self.session.retire_after_turn

    def stderr_text(self) -> str:
        """Return a bounded diagnostic tail without exposing launch secrets."""
        diagnostic = getattr(self.session, "stderr_text", lambda: "")()
        return " ".join(str(diagnostic).strip().split())[-500:]

    @property
    def exit_code(self) -> int | None:
        """Return the attached process exit code when it has terminated."""
        return getattr(self.session, "process_exit_code", lambda: None)()

    def compatible_with(self, launch: ResidentLaunch) -> bool:
        """Whether a new activation may borrow this exact process.

        The caller normally carries a fresh attempt credential, while the
        resident process intentionally keeps its renewable residency grant.
        The inference grant therefore does not participate in equality.
        Everything the CLI fixes at process start still does, including model,
        effort, MCP configuration and the non-renewable GitHub token. A fresh
        GitHub token requires native resume in a replacement process.
        """
        same_mcp = self.launch.mcp_server_ids == launch.mcp_server_ids and (
            not launch.mcp_server_ids
            or (
                self.launch.mcp_base_url == launch.mcp_base_url
                and self.launch.mcp_credential == launch.mcp_credential
            )
        )
        return (
            self.launch.thread_id == launch.thread_id
            and self.launch.runtime_id == launch.runtime_id
            and self.launch.runtime_version == launch.runtime_version
            and self.launch.model == launch.model
            and self.launch.reasoning_effort == launch.reasoning_effort
            and self.launch.plugins == launch.plugins
            and self.launch.gateway_base_url == launch.gateway_base_url
            and same_mcp
            and self.launch.github_token == launch.github_token
            and self.launch.github_repo == launch.github_repo
            and self.launch.environment == launch.environment
            and self.launch.slot_id == launch.slot_id
        )

    def start_turn(
        self,
        *,
        prompt: str,
        model: str,
        reasoning_effort: str | None,
    ) -> ResidentTurn[_RuntimeT_co]:
        """Send one prompt to this already-open native session."""
        return ResidentTurn(
            self.session.start_turn(
                TurnInput(
                    prompt=prompt,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
            )
        )

    def close(self) -> None:
        """Flush/terminate this process generation without deleting state."""
        self.session.close()


class ResidentTransportFactory:
    """Open exact-version transports in credential-isolated member slots."""

    def __init__(self, manager: ChatSandboxManager) -> None:
        self._manager = manager

    def open(
        self, spec: ChatSandboxSpec, launch: ResidentLaunch
    ) -> ResidentTransport[DialectRuntime]:
        """Start and probe a runtime's persistent transport."""
        if not launch.credential:
            raise DriverError("opening a resident transport requires a residency credential")
        if launch.slot_id >= spec.resident_slot_capacity:
            raise DriverError("resident slot is outside this sandbox's immutable capacity")
        runtime_id = launch.runtime_id
        driver = get_driver(runtime_id)
        state_dir = self._member_state_dir(launch)
        member_processes_reaped = runtime_id == "claude-code" or bool(launch.plugins)
        if member_processes_reaped:
            # A failed Fork can release a reserved slot. Reap that slot before
            # preparing its next member's launch state, so an old same-UID
            # process cannot race creation of the new private settings.
            self._manager.reap_member_processes(
                spec,
                slot_id=launch.slot_id,
                user_uid=MEMBER_UID_BASE + launch.slot_id,
                user_gid=self._manager.backend.gid,
            )
        if runtime_id == "claude-code":
            self._prepare_claude_member_state(spec, launch)
        elif runtime_id in PRIVATE_MEMBER_STATE_ROOTS:
            self._prepare_member_state(spec, launch, state_dir)
        context = self._launch_context(
            spec,
            launch,
            credential=launch.credential,
            state_dir=state_dir,
            member_processes_reaped=member_processes_reaped,
        )
        if runtime_id == "claude-code" and launch.plugins:
            install_claude_plugins(
                self._manager,
                spec,
                launch.plugins,
                config_dir=claude_member_config_dir(launch.slot_id, launch.thread_id),
                env=context.base_env,
                slot_id=launch.slot_id,
            )
        elif runtime_id == "codex" and launch.plugins:
            install_codex_plugins(
                self._manager,
                spec,
                launch.plugins,
                state_dir=state_dir,
                env=context.base_env,
                slot_id=launch.slot_id,
            )
        elif runtime_id in {"pi", "opencode", "kilo"} and launch.plugins:
            context.base_env.update(
                install_other_runtime_plugins(
                    self._manager,
                    spec,
                    runtime_id=runtime_id,
                    plugins=launch.plugins,
                    env=context.base_env,
                    slot_id=launch.slot_id,
                )
            )
        if launch.native_session_id:
            opener = getattr(driver, "launch_resident_live", None)
            if opener is None:
                raise DriverError(f"runtime {runtime_id!r} has no live resume transport")
            session = opener(context, session_id=launch.native_session_id)
        else:
            opener = getattr(driver, "launch_live", None)
            if opener is None:
                raise DriverError(f"runtime {runtime_id!r} has no live session transport")
            session = opener(context)
        try:
            ensure_started = getattr(session, "ensure_started", None)
            if ensure_started is None:
                raise DriverError(f"runtime {runtime_id!r} cannot prove resident readiness")
            ensure_started(
                model=launch.model,
                reasoning_effort=launch.reasoning_effort,
            )
            if not bool(getattr(session, "process_alive", lambda: False)()):
                raise DriverError(f"runtime {runtime_id!r} resident process is not alive")
            return ResidentTransport(launch=launch, session=session)
        except Exception:
            with contextlib.suppress(Exception):
                session.close()
            raise

    @staticmethod
    def _member_state_dir(launch: ResidentLaunch) -> str:
        root = PRIVATE_MEMBER_STATE_ROOTS.get(launch.runtime_id)
        if root is None:
            return CHAT_STATE_DIR
        member_key = hashlib.sha256(launch.thread_id.encode("utf-8")).hexdigest()
        return f"{root}/{member_key}"

    @staticmethod
    def _legacy_member_state_dir(launch: ResidentLaunch) -> str:
        """Where an earlier release kept this member's provider home."""
        member_key = hashlib.sha256(launch.thread_id.encode("utf-8")).hexdigest()
        return f"{_LEGACY_MEMBER_STATE_ROOTS[launch.runtime_id]}/{member_key}"

    def _prepare_member_state(
        self,
        spec: ChatSandboxSpec,
        launch: ResidentLaunch,
        state_dir: str,
    ) -> None:
        """Create the member-owned provider home used by this shared group."""
        prepare = (
            self._prepare_codex_member_state
            if launch.runtime_id == "codex"
            else self._prepare_http_member_state
        )
        result = prepare(spec, launch, state_dir)
        if result.returncode == _UNTRUSTED_COLLECTION_EXIT:
            # A group created by an earlier release, or a collection name a
            # member squatted on before the sandbox user secured it.
            self._manager.ensure_private_member_collections(spec)
            result = prepare(spec, launch, state_dir)
        if result.returncode != 0:
            raise DriverError(
                f"{launch.runtime_id} member state could not be prepared: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )

    def _prepare_claude_member_state(self, spec: ChatSandboxSpec, launch: ResidentLaunch) -> None:
        """Share Claude native history while keeping launch input slot-private.

        Old retained containers already have projects under the shared Claude
        root. Link only that credential-free native history; never import its
        settings, environment overrides, credentials, or process registry.
        Member homes are image-created beneath root-owned /home, outside every
        shared-state permission-normalization traversal.
        """
        script = (
            "import os, pathlib, stat, sys\n"
            "config = pathlib.Path(sys.argv[1])\n"
            "shared = pathlib.Path(sys.argv[2])\n"
            "uid = os.getuid()\n"
            "for path in (config.parent.parent,):\n"
            "    info = path.lstat()\n"
            "    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:\n"
            "        raise RuntimeError('Claude member home is not private')\n"
            "    path.chmod(0o700)\n"
            "os.umask(0o077)\n"
            "for path in (config.parent, config):\n"
            "    path.mkdir(exist_ok=True)\n"
            "    info = path.lstat()\n"
            "    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:\n"
            "        raise RuntimeError('Claude config is not member-owned')\n"
            "    path.chmod(0o700)\n"
            "for path in (shared.parent, shared):\n"
            "    path.mkdir(mode=0o2770, exist_ok=True)\n"
            "    if not stat.S_ISDIR(path.lstat().st_mode):\n"
            "        raise RuntimeError('Claude history path is not a directory')\n"
            "    if path.stat().st_uid == uid:\n"
            "        path.chmod(0o2770)\n"
            "projects = config / 'projects'\n"
            "try:\n"
            "    projects.symlink_to(shared, target_is_directory=True)\n"
            "except FileExistsError:\n"
            "    if not projects.is_symlink() or os.readlink(projects) != str(shared):\n"
            "        raise RuntimeError('Claude member history link is invalid')\n"
        )
        result = self._manager.exec_capture(
            spec,
            [
                "python3",
                "-c",
                script,
                claude_member_config_dir(launch.slot_id, launch.thread_id),
                CLAUDE_SHARED_PROJECTS_DIR,
            ],
            workdir="/",
            timeout_s=30,
            user_uid=MEMBER_UID_BASE + launch.slot_id,
            user_gid=self._manager.backend.gid,
        )
        if result.returncode != 0:
            raise DriverError(
                "Claude member state could not be prepared: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )

    def _prepare_http_member_state(
        self,
        spec: ChatSandboxSpec,
        launch: ResidentLaunch,
        state_dir: str,
    ) -> subprocess.CompletedProcess[bytes]:
        """Create an isolated OpenCode/Kilo XDG root for one membership."""
        runtime_id = launch.runtime_id
        if runtime_id not in {"opencode", "kilo"}:
            raise DriverError(f"runtime {runtime_id!r} has no private HTTP state home")
        uid = MEMBER_UID_BASE + launch.slot_id
        script = (
            'set -eu; target="$1"; runtime="$2"; legacy="$3"; trusted="$4"; '
            'marker="$target/.freeinference-member-state-v1"; '
            + _PRIVATE_MEMBER_HOME_PROLOGUE
            + 'if test ! -e "$marker"; then '
            '  rm -rf -- "$target/$runtime/state/$runtime/locks"; '
            '  : > "$marker"; '
            "fi"
        )
        return self._manager.exec_capture(
            spec,
            [
                "/bin/sh",
                "-c",
                script,
                "sh",
                state_dir,
                runtime_id,
                self._legacy_member_state_dir(launch),
                str(self._manager.backend.uid),
            ],
            workdir="/",
            timeout_s=120,
            user_uid=uid,
            user_gid=self._manager.backend.gid,
        )

    def _prepare_codex_member_state(
        self,
        spec: ChatSandboxSpec,
        launch: ResidentLaunch,
        state_dir: str,
    ) -> subprocess.CompletedProcess[bytes]:
        """Create one UID-owned Codex home from the image bootstrap config."""
        uid = MEMBER_UID_BASE + launch.slot_id
        script = (
            'set -eu; source="$1"; target="$2"; legacy="$3"; trusted="$4"; '
            'marker="$target/.freeinference-member-state-v1"; '
            + _PRIVATE_MEMBER_HOME_PROLOGUE
            + 'if test ! -e "$marker"; then '
            '  if test -f "$source/config.toml"; then '
            '    cp -- "$source/config.toml" "$target/config.toml"; '
            "  fi; "
            '  mkdir -p -- "$target/tmp" "$target/.tmp"; : > "$marker"; '
            "fi"
        )
        return self._manager.exec_capture(
            spec,
            [
                "/bin/sh",
                "-c",
                script,
                "sh",
                CHAT_STATE_DIR,
                state_dir,
                self._legacy_member_state_dir(launch),
                str(self._manager.backend.uid),
            ],
            workdir="/",
            timeout_s=120,
            user_uid=uid,
            user_gid=self._manager.backend.gid,
        )

    def _clone_private_member_state(
        self,
        spec: ChatSandboxSpec,
        source_launch: ResidentLaunch,
        target_launch: ResidentLaunch,
    ) -> None:
        """Copy one flushed provider home into the child UID's private home."""
        source_dir = self._member_state_dir(source_launch)
        target_dir = self._member_state_dir(target_launch)
        if source_dir == target_dir:
            raise DriverError("native fork target reused the source provider home")
        runtime_id = source_launch.runtime_id
        if runtime_id != target_launch.runtime_id or runtime_id not in PRIVATE_MEMBER_STATE_ROOTS:
            raise DriverError("native fork cannot clone incompatible provider homes")
        # Neither UID can open the other's home. The source archives its own
        # flushed home, scratch excluded, and the child unpacks that stream
        # into a home only it can open; the copy crosses this host's pipe and
        # is never staged inside the container where a sibling could read it.
        archive = (
            'set -eu; cd -- "$1"; set --; '
            "for entry in * .[!.]* ..?*; do "
            '  test -e "$entry" || continue; '
            '  case "$entry" in tmp|.tmp) continue;; esac; '
            '  set -- "$@" "./$entry"; '
            "done; "
            'exec tar --create --file=- --files-from=/dev/null "$@"'
        )
        unpack = (
            'set -eu; target="$1"; runtime="$2"; trusted="$3"; '
            + _NEW_PRIVATE_MEMBER_HOME_PROLOGUE
            + 'find "$target" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; '
            'tar --extract --file=- --directory="$target" --no-same-owner '
            "--no-same-permissions; "
            'mkdir -p -- "$target/tmp" "$target/.tmp"; '
            'case "$runtime" in '
            '  opencode|kilo) rm -rf -- "$target/$runtime/state/$runtime/locks";; '
            "esac; "
            ': > "$target/.freeinference-member-state-v1"'
        )
        # The source's open already secured the collections; this also covers
        # a fork recovered by a broker that has not opened the source yet.
        self._manager.ensure_private_member_collections(spec)
        source, target = self._manager.pipe_member_exec(
            spec,
            source_argv=["/bin/sh", "-c", archive, "sh", source_dir],
            source_uid=MEMBER_UID_BASE + source_launch.slot_id,
            target_argv=[
                "/bin/sh",
                "-c",
                unpack,
                "sh",
                target_dir,
                runtime_id,
                str(self._manager.backend.uid),
            ],
            target_uid=MEMBER_UID_BASE + target_launch.slot_id,
            user_gid=self._manager.backend.gid,
            timeout_s=120,
        )
        for result in (target, source):
            if result.returncode != 0:
                raise DriverError(
                    f"{runtime_id} fork state could not be materialized for the child: "
                    + result.stderr.decode("utf-8", "replace")[-300:]
                )

    def _clear_http_member_locks(
        self,
        spec: ChatSandboxSpec,
        launch: ResidentLaunch,
    ) -> None:
        """Remove dead OpenCode/Kilo server locks after closing that process.

        Both HTTP runtimes leave a per-project lock in their XDG state tree.
        The source process is intentionally restarted after native fork, so a
        lock that still names the closed PID must not survive into that new
        process generation.  Session history and databases are left intact.
        """
        runtime_id = launch.runtime_id
        if runtime_id not in {"opencode", "kilo"}:
            raise DriverError(f"runtime {runtime_id!r} has no HTTP member locks")
        state_dir = self._member_state_dir(launch)
        uid = MEMBER_UID_BASE + launch.slot_id
        result = self._manager.exec_capture(
            spec,
            [
                "/bin/sh",
                "-c",
                'set -eu; state="$1"; runtime="$2"; '
                'rm -rf -- "$state/$runtime/state/$runtime/locks"',
                "sh",
                state_dir,
                runtime_id,
            ],
            workdir="/",
            timeout_s=120,
            user_uid=uid,
            user_gid=self._manager.backend.gid,
        )
        if result.returncode != 0:
            raise DriverError(
                f"{runtime_id} stale member locks could not be cleared: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )

    def _launch_context(
        self,
        spec: ChatSandboxSpec,
        launch: ResidentLaunch,
        *,
        credential: str,
        state_dir: str | None = None,
        member_processes_reaped: bool = False,
    ) -> LaunchContext:
        """Build one member-isolated launch seam used by live and fork probes."""
        uid = MEMBER_UID_BASE + launch.slot_id
        runtime_id = launch.runtime_id
        activation_dir = f"{MEMBER_TMPFS_ROOT}/{launch.slot_id}/{runtime_id}"
        home = f"/home/agent-member-{launch.slot_id}"
        base_env = dict(self._manager.backend.base_env())
        if launch.environment is not None:
            from cloud_agent_host.environment_process import tool_environment

            base_env.update(tool_environment())
            base_env.update(launch.environment.values("runtime"))
        base_env.update(
            {
                "HOME": home,
                "PATH": SANDBOX_PATH,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": CHAT_WORKDIR,
            }
        )
        # Only the claim's repository-scoped token may authenticate gh. Do not
        # fall back to a credential inherited from a custom host backend.
        base_env.pop("GH_TOKEN", None)
        base_env.pop("GITHUB_TOKEN", None)
        if launch.github_token:
            base_env.update(
                {
                    "GH_TOKEN": launch.github_token,
                    "GH_HOST": "github.com",
                    "GH_PROMPT_DISABLED": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                    # Reset inherited helpers before selecting gh. These
                    # invocation-only settings never write credentials or
                    # helper configuration into the shared .git directory.
                    "GIT_CONFIG_COUNT": "3",
                    "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
                    "GIT_CONFIG_VALUE_1": "",
                    "GIT_CONFIG_KEY_2": "credential.https://github.com.helper",
                    "GIT_CONFIG_VALUE_2": "!gh auth git-credential",
                }
            )
            if launch.github_repo:
                base_env["GH_REPO"] = launch.github_repo
        if launch.runtime_version:
            base_env["PATH"] = (
                f"{runtime_bin_dir(runtime_id, launch.runtime_version)}:{base_env['PATH']}"
            )

        reaped = False

        def spawn_live(argv: list[str], env: dict[str, str]) -> SandboxProcess:
            nonlocal reaped
            if not reaped:
                # A sandbox manager restart cannot reattach Docker-exec pipes. Remove
                # any prior tree in this member's distinct uid before native
                # resume opens the replacement transport.
                if not member_processes_reaped:
                    self._manager.reap_member_processes(
                        spec,
                        slot_id=launch.slot_id,
                        user_uid=uid,
                        user_gid=self._manager.backend.gid,
                    )
                # HTTP dialects always use the trusted live bridge, including
                # turns without MCP. Refresh it from the host when resuming a
                # retained container so new native-question commands are
                # available without replacing its persisted session state.
                if launch.mcp_server_ids or runtime_id in {"opencode", "kilo"}:
                    self._manager.ensure_mcp_helpers(spec, runtime_id)
                if runtime_id == "kilo":
                    self._manager.ensure_kilo_mcp_runtime(spec, launch.runtime_version or "7.4.17")
                if runtime_id == "codex" and launch.mcp_server_ids:
                    self._manager.ensure_codex_mcp_policy(spec, launch.mcp_base_url)
                reaped = True
            pidfile = f"{MEMBER_TMPFS_ROOT}/{launch.slot_id}/resident-{secrets.token_hex(16)}.pid"
            return self._manager.spawn(
                spec,
                argv,
                env=env,
                workdir=CHAT_WORKDIR,
                live_input=True,
                user_uid=uid,
                user_gid=self._manager.backend.gid,
                process_pidfile=pidfile,
            )

        mcp_enabled = bool(launch.mcp_server_ids)
        return LaunchContext(
            workdir=CHAT_WORKDIR,
            gateway_base_url=launch.gateway_base_url,
            credential=credential,
            provides_isolation=True,
            spawn_live=spawn_live,
            base_env=base_env,
            mcp_config=RuntimeMCPConfig(
                server_ids=launch.mcp_server_ids,
                base_url=launch.mcp_base_url if mcp_enabled else "",
                credential=launch.mcp_credential if mcp_enabled else "",
            ),
            state_dir=CHAT_STATE_DIR,
            session_state_dir=state_dir or CHAT_STATE_DIR,
            activation_dir=activation_dir,
            install_tool_hooks=lambda update: self._manager.install_tool_hooks(
                spec, update, slot_id=launch.slot_id, thread_id=launch.thread_id
            ),
            claude_config_dir=(
                claude_member_config_dir(launch.slot_id, launch.thread_id)
                if runtime_id == "claude-code"
                else None
            ),
        )

    def fork_native(
        self,
        spec: ChatSandboxSpec,
        source: ResidentTransport[DialectRuntime],
        operation_id: str,
        target_launch: ResidentLaunch,
        source_native_turn_no: int,
    ) -> str:
        """Materialize or recover one prompt-free, journaled native fork."""
        source_id = source.native_session_id
        if not source_id:
            raise DriverError("native fork source has no settled session id")
        if not operation_id:
            raise DriverError("native fork operation id is required")
        if source_native_turn_no < 1:
            raise DriverError("native fork turn must be positive")
        if source.launch.runtime_id != target_launch.runtime_id:
            raise DriverError("native fork cannot change runtime")
        if source.launch.runtime_version != target_launch.runtime_version:
            raise DriverError("native fork cannot change runtime version")
        if source.launch.plugins != target_launch.plugins:
            raise DriverError("native fork cannot change plugins")
        if not target_launch.credential:
            raise DriverError("native fork target needs a residency credential")

        expected = {
            "operation_id": operation_id,
            "sandbox_id": spec.sandbox_id,
            "sandbox_generation": spec.generation,
            "runtime_id": source.launch.runtime_id,
            "runtime_version": source.launch.runtime_version,
            "source_session_id": source_id,
            "source_native_turn_no": source_native_turn_no,
            "target_thread_id": target_launch.thread_id,
            "target_slot_id": target_launch.slot_id,
        }
        runtime_id = source.launch.runtime_id
        existing = self._read_fork_journal(spec, operation_id)
        codex_baseline: set[str] | None = None
        if existing is not None:
            self._validate_journal(existing, expected)
            if existing.get("state") == "ready":
                child_id = str(existing.get("target_session_id") or "")
                if not child_id or child_id == source_id:
                    raise DriverError("native fork journal has an invalid child id")
                return child_id
            if runtime_id != "codex":
                # The process may have persisted a child immediately before a
                # crash. Retrying that ambiguous mutation would manufacture a
                # second hidden child, so fail closed for the saga reconciler.
                raise DriverError("native fork has an ambiguous preparing journal")
            codex_baseline = self._codex_journal_baseline(existing)
            child_id = self._recover_codex_fork(
                spec,
                source.launch,
                source_id=source_id,
                baseline=codex_baseline,
            )
        else:
            preparing = {**expected, "state": "preparing"}
            if runtime_id == "codex":
                codex_baseline = self._codex_fork_children(
                    spec,
                    source.launch,
                    source_id=source_id,
                )
                preparing["source_child_session_ids"] = sorted(codex_baseline)
            self._write_fork_journal(spec, operation_id, preparing)
            child_id = ""

        if not child_id and runtime_id in {"codex", "pi", "opencode", "kilo"}:
            fork = getattr(source.session, "fork_native", None)
            if fork is None:
                raise DriverError(f"runtime {runtime_id!r} has no live native fork")
            try:
                child_id = str(
                    fork(
                        source_session_id=source_id,
                        operation_id=operation_id,
                        source_native_turn_no=source_native_turn_no,
                    )
                    or ""
                )
            except Exception:
                if runtime_id != "codex" or codex_baseline is None:
                    raise
                # Codex writes the child rollout before returning the RPC.
                # A severed response is recoverable without issuing a second
                # mutation when exactly one new child points at this source.
                # Zero may simply mean the write has not become visible yet;
                # preserve PREPARING and let the idempotent HTTP retry scan it.
                recovered = self._new_codex_fork_children(
                    spec,
                    source.launch,
                    source_id=source_id,
                    baseline=codex_baseline,
                )
                if len(recovered) != 1:
                    raise
                child_id = next(iter(recovered))
        elif not child_id and runtime_id == "claude-code":
            # Claude's result can precede the asynchronous transcript write.
            # EOF and reap the settled source before the helper reads its
            # historical boundary. The supervisor resumes this exact source
            # after the fork, under the source member's existing turn lock.
            source.close()
            child_id = self._fork_claude(
                spec,
                source_id,
                source.launch,
                source_native_turn_no,
            )
        elif not child_id:
            raise DriverError(f"runtime {runtime_id!r} has no native fork path")
        if not child_id or child_id == source_id:
            raise DriverError(f"runtime {runtime_id!r} returned an invalid fork child id")
        if codex_baseline is not None and child_id in codex_baseline:
            raise DriverError("Codex native fork reused a pre-existing child session")

        if runtime_id in PRIVATE_MEMBER_STATE_ROOTS:
            # These providers use owner-only files, SQLite, or project locks.
            # Stop the quiescent source so its state is flushed, publish its
            # shared workspace writes, and copy that stable post-fork state
            # from its private home into the child's. The supervisor
            # resumes the exact source immediately after this method returns;
            # PID continuity is not required. For Codex this materialization
            # is also the recovery path when the original RPC response is lost.
            source.close()
            if runtime_id in {"opencode", "kilo"}:
                self._clear_http_member_locks(spec, source.launch)
            self._manager.normalize_member_files(
                spec,
                slot_id=source.launch.slot_id,
                user_uid=MEMBER_UID_BASE + source.launch.slot_id,
                user_gid=self._manager.backend.gid,
            )
            self._clone_private_member_state(
                spec,
                source.launch,
                target_launch,
            )
            normalization_slot = target_launch.slot_id
        elif runtime_id == "pi":
            # Pi's RPC fork/clone switches the process itself onto the child.
            # Close that probe so the supervisor resumes the source locator;
            # the child is opened independently in its reserved slot.  The
            # child session file was written by the source UID, so expose that
            # UID's new files to the shared member group before target resume.
            source.close()
            normalization_slot = source.launch.slot_id
        else:
            normalization_slot = source.launch.slot_id

        self._manager.normalize_member_files(
            spec,
            slot_id=normalization_slot,
            user_uid=MEMBER_UID_BASE + normalization_slot,
            user_gid=self._manager.backend.gid,
        )
        self._write_fork_journal(
            spec,
            operation_id,
            {**expected, "state": "ready", "target_session_id": child_id},
        )
        return child_id

    def _recover_codex_fork(
        self,
        spec: ChatSandboxSpec,
        source_launch: ResidentLaunch,
        *,
        source_id: str,
        baseline: set[str],
    ) -> str:
        recovered = self._new_codex_fork_children(
            spec,
            source_launch,
            source_id=source_id,
            baseline=baseline,
        )
        if len(recovered) != 1:
            detail = "no child is visible" if not recovered else "multiple children are visible"
            raise DriverError(f"Codex native fork preparing journal is ambiguous: {detail}")
        return next(iter(recovered))

    def _new_codex_fork_children(
        self,
        spec: ChatSandboxSpec,
        source_launch: ResidentLaunch,
        *,
        source_id: str,
        baseline: set[str],
    ) -> set[str]:
        return (
            self._codex_fork_children(
                spec,
                source_launch,
                source_id=source_id,
            )
            - baseline
        )

    def _codex_fork_children(
        self,
        spec: ChatSandboxSpec,
        source_launch: ResidentLaunch,
        *,
        source_id: str,
    ) -> set[str]:
        """Read bounded Codex rollout metadata for crash-safe fork recovery."""
        state_dir = self._member_state_dir(source_launch)
        script = """
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]) / "sessions"
source_id = sys.argv[2]
children = set()
seen = 0
if root.is_dir():
    for path in root.rglob("*.jsonl"):
        seen += 1
        if seen > 100000:
            raise SystemExit(42)
        try:
            with path.open("r", encoding="utf-8") as handle:
                first = handle.readline(1_000_001)
            if len(first) > 1_000_000:
                raise SystemExit(43)
            item = json.loads(first)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(payload, dict) or payload.get("forked_from_id") != source_id:
            continue
        child_id = payload.get("session_id") or payload.get("id")
        if isinstance(child_id, str) and child_id and len(child_id) <= 256:
            children.add(child_id)
        if len(children) > 4096:
            raise SystemExit(44)
print(json.dumps(sorted(children), separators=(",", ":")))
"""
        result = self._manager.exec_capture(
            spec,
            ["python3", "-c", script, state_dir, source_id],
            workdir="/",
            timeout_s=30,
            user_uid=MEMBER_UID_BASE + source_launch.slot_id,
            user_gid=self._manager.backend.gid,
        )
        if result.returncode != 0:
            raise DriverError(
                "Codex fork recovery inventory failed: "
                + result.stderr.decode("utf-8", "replace")[-300:]
            )
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DriverError("Codex fork recovery inventory returned invalid JSON") from exc
        if (
            not isinstance(value, list)
            or len(value) > 4096
            or any(not isinstance(item, str) or not item or len(item) > 256 for item in value)
        ):
            raise DriverError("Codex fork recovery inventory has an invalid shape")
        return set(value)

    @staticmethod
    def _codex_journal_baseline(journal: dict[str, object]) -> set[str]:
        value = journal.get("source_child_session_ids")
        if (
            not isinstance(value, list)
            or len(value) > 4096
            or any(not isinstance(item, str) or not item or len(item) > 256 for item in value)
        ):
            raise DriverError("Codex native fork journal has no valid recovery baseline")
        if len(set(value)) != len(value):
            raise DriverError("Codex native fork journal recovery baseline is not canonical")
        return set(value)

    def _fork_claude(
        self,
        spec: ChatSandboxSpec,
        source_id: str,
        source_launch: ResidentLaunch,
        source_native_turn_no: int,
    ) -> str:
        uid = MEMBER_UID_BASE + source_launch.slot_id
        result = self._manager.exec_capture(
            spec,
            ["claude-session-fork", source_id, str(source_native_turn_no)],
            env={
                "HOME": f"/home/agent-member-{source_launch.slot_id}",
                "CLAUDE_CONFIG_DIR": claude_member_config_dir(
                    source_launch.slot_id, source_launch.thread_id
                ),
                "PATH": SANDBOX_PATH,
            },
            workdir=CHAT_WORKDIR,
            timeout_s=60,
            user_uid=uid,
            user_gid=self._manager.backend.gid,
        )
        if result.returncode != 0:
            raise DriverError(
                "Claude native fork failed: " + result.stderr.decode("utf-8", "replace")[:500]
            )
        try:
            receipt = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DriverError("Claude native fork returned invalid JSON") from exc
        if not isinstance(receipt, dict) or receipt.get("source_session_id") != source_id:
            raise DriverError("Claude native fork returned the wrong source session")
        return str(receipt.get("session_id") or "")

    @staticmethod
    def _journal_key(operation_id: str) -> str:
        return hashlib.sha256(operation_id.encode("utf-8")).hexdigest()

    def _read_fork_journal(
        self,
        spec: ChatSandboxSpec,
        operation_id: str,
    ) -> dict[str, object] | None:
        path = f"{PLATFORM_STATE_DIR}/session-forks/{self._journal_key(operation_id)}.json"
        result = self._manager.exec_capture(
            spec,
            ["/bin/sh", "-c", 'test ! -e "$1" || cat -- "$1"', "sh", path],
            workdir="/",
            timeout_s=15,
            # The sandbox drops every Linux capability. UID 0 therefore does
            # not have CAP_DAC_OVERRIDE and cannot traverse the Docker-layer
            # state root owned by the sandbox account. Use that exact owner
            # for the platform journal instead of assuming root bypasses DAC.
            user_uid=self._manager.backend.uid,
            user_gid=self._manager.backend.gid,
        )
        if result.returncode != 0:
            raise DriverError("native fork journal could not be read")
        if not result.stdout:
            return None
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DriverError("native fork journal is corrupt") from exc
        if not isinstance(value, dict):
            raise DriverError("native fork journal has an invalid shape")
        return value

    def _write_fork_journal(
        self,
        spec: ChatSandboxSpec,
        operation_id: str,
        value: dict[str, object],
    ) -> None:
        directory = f"{PLATFORM_STATE_DIR}/session-forks"
        path = f"{directory}/{self._journal_key(operation_id)}.json"
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        script = (
            'set -eu; directory="$1"; target="$2"; '
            'umask 077; mkdir -p -- "$directory"; chmod 0700 -- "$directory"; '
            'tmp="$target.tmp.$$"; trap \'rm -f -- "$tmp"\' EXIT; '
            'cat > "$tmp"; chmod 0600 -- "$tmp"; mv -f -- "$tmp" "$target"; '
            "trap - EXIT"
        )
        result = self._manager.exec_capture(
            spec,
            ["/bin/sh", "-c", script, "sh", directory, path],
            workdir="/",
            timeout_s=15,
            input_bytes=payload,
            user_uid=self._manager.backend.uid,
            user_gid=self._manager.backend.gid,
        )
        if result.returncode != 0:
            raise DriverError(
                "native fork journal could not be written: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )

    @staticmethod
    def _validate_journal(
        journal: dict[str, object],
        expected: dict[str, object],
    ) -> None:
        if any(journal.get(key) != value for key, value in expected.items()):
            raise DriverError("native fork journal identity does not match the request")
        if journal.get("state") not in {"preparing", "ready"}:
            raise DriverError("native fork journal has an invalid state")


__all__ = [
    "ResidentLaunch",
    "ResidentTransport",
    "ResidentTransportFactory",
    "ResidentTurn",
]
