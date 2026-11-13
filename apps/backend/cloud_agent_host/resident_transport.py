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
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from cloud_agent_host.container.chat_sandbox import (
    CHAT_STATE_DIR,
    CHAT_WORKDIR,
    MEMBER_TMPFS_ROOT,
    MEMBER_UID_BASE,
    ChatSandboxManager,
    ChatSandboxSpec,
)
from cloud_agent_host.container.sandbox import SANDBOX_PATH, SandboxProcess, runtime_bin_dir
from cloud_agent_host.drivers import (
    DialectRuntime,
    DriverError,
    HarnessEvent,
    HarnessTurn,
    InputOutcome,
    LaunchContext,
    TurnInput,
    TurnResult,
)
from cloud_agent_host.github_relay_env import relay_configured
from cloud_agent_host.launch.environment import EnvironmentFacet
from cloud_agent_host.launch.facet import PrepareContext
from cloud_agent_host.launch.facets import PREPARE_ORDER, facet_differences, launch_facets
from cloud_agent_host.launch.github_relay import GitHubRelayFacet
from cloud_agent_host.launch.inference import InferenceFacet
from cloud_agent_host.member_setup.member_homes import (
    NEW_PRIVATE_MEMBER_HOME_PROLOGUE,
    PRIVATE_MEMBER_STATE_ROOTS,
    UNTRUSTED_COLLECTION_EXIT,
    member_state_dir,
)
from cloud_agent_host.member_setup.member_preparation import (
    MemberPreparation,
    MemberStep,
    run_member_steps,
)
from cloud_agent_host.request_prefixes import TurnPrefixes
from cloud_agent_host.runtimes import RuntimeMCPConfig, connection_base_url, meter_launch
from cloud_agent_host.runtimes.base import COMPAT_LAUNCHER
from cloud_agent_host.runtimes.claude import member as claude_member
from cloud_agent_host.runtimes.codex import member as codex_member
from cloud_agent_host.runtimes.codex.fork_state import CODEX_REBASE_ROLLOUT_PATHS
from cloud_agent_host.runtimes.http_bridge import member as http_member
from cloud_agent_host.runtimes.live import get_driver, member_launch
from cloud_agent_shared.plugins import validate_sandbox_plugins

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloud_agent_host.launch.facet import LaunchFacet
    from cloud_agent_shared.environment import BoundEnvironment
    from cloud_agent_shared.sandbox_mcp import SandboxMcpServer

#: The dialect a resident transport is speaking; the transport only reads the
#: session it was handed, so the parameter stays covariant and a caller that
#: knows a narrower runtime can still hand it one.
_RuntimeT_co = TypeVar("_RuntimeT_co", bound=DialectRuntime, covariant=True)
#: The session contract itself is stated invariantly: a session hands back the
#: turn type of exactly the runtime it was opened for.
_SessionRuntimeT = TypeVar("_SessionRuntimeT", bound=DialectRuntime)
_FacetT = TypeVar("_FacetT", bound="LaunchFacet")
#: The launch's own process-fixed identity, compared before its facets and
#: after them, in the order a refused reuse reports them.
_IDENTITY_FIELDS = ("thread_id", "runtime_id", "runtime_version", "model", "reasoning_effort")
_SLOT_FIELDS = ("slot_id",)

# Host-owned exactly-once receipts must not live inside a provider home. Some
# CLIs enumerate their whole state root, and the deliberately unreadable
# platform directory otherwise turns into a provider-side EACCES. This path is
# in the container writable layer (not tmpfs or a host mount), so docker stop /
# start preserves it just like /workspace and /agent-state.
PLATFORM_STATE_DIR = "/home/agent/.freeinference-platform"


@dataclass(frozen=True)
class ResidentLaunch:
    """Everything needed to open one member process, excluding its prompt.

    The flat record the runner sends the sandbox manager (``asdict``) and
    amends with ``replace``. Each platform capability reads its fields as one
    facet (:mod:`cloud_agent_host.launch`), which states what the capability
    fixes at process start, prepares, and adds to the CLI's environment.
    """

    thread_id: str
    runtime_id: str
    runtime_version: str | None
    native_session_id: str | None
    model: str
    reasoning_effort: str | None
    # Never in a repr: the owner connection's own key.
    credential: str | None = field(repr=False)
    mcp_server_ids: tuple[str, ...] = ()
    mcp_base_url: str = ""
    mcp_credential: str = field(default="", repr=False)
    # MCP servers this member runs inside the sandbox, behind its MCP gateway
    # (cloud_agent_host.sandbox_mcp). Fixed at process start like the relay's.
    sandbox_mcp_servers: tuple[SandboxMcpServer, ...] = ()
    slot_id: int = 0
    residency_generation: int = 0
    plugins: tuple[dict[str, str], ...] = ()
    # The residency's GitHub relay placeholder (``agh.``), which only the
    # GitHub relay accepts. Kept in this member's environment, never in shared
    # workspace or image config. Like ``mcp_credential`` it names the
    # residency, so a probe carries none and borrows a live process's.
    github_credential: str = field(default="", repr=False)
    github_repo: str = ""
    # The owner as commit author, and the co-author line for the agent. Fixed
    # per residency, like the credential they arrive with.
    git_author_name: str = ""
    git_author_email: str = ""
    git_coauthor: str = ""
    environment: BoundEnvironment | None = field(default=None, repr=False)
    # The owner model connection the member runs on: its exact Base URL,
    # protocol, and ``<connection>@<revision>``. ``credential`` is that
    # connection's key. All three are fixed at process start; the source is
    # what tells a rotated key's process apart.
    inference_base_url: str = ""
    inference_protocol: str = ""
    inference_source: str = ""
    # The CLI's own protocol when the member reaches the connection through
    # its compatibility helper; empty is direct. Fixed at process start, like
    # the source: a plan change restarts the member.
    inference_client_protocol: str = ""
    # Its loopback token on a translated plan (``ModelConnectionAccess.local_token``):
    # fixed at process start and part of reuse identity, so a process never
    # holds a token the turn's redactor does not mask.
    inference_local_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.thread_id or not self.runtime_id or not self.model:
            raise ValueError("resident launch identity, runtime, and model are required")
        if not (self.inference_source and self.inference_base_url and self.inference_protocol):
            raise ValueError(
                "a resident runs on its owner's model connection: URL, protocol and source"
            )
        if self.inference_client_protocol and not self.inference_source:
            raise ValueError("a translated launch needs its model connection")
        if bool(self.inference_client_protocol) != bool(self.inference_local_token):
            raise ValueError("a translated launch, and only one, carries its member's token")
        if self.slot_id < 0:
            raise ValueError("resident slot id cannot be negative")
        if self.residency_generation < 0:
            raise ValueError("residency generation cannot be negative")
        object.__setattr__(
            self,
            "plugins",
            tuple(validate_sandbox_plugins(self.runtime_id, self.plugins)),
        )

    @cached_property
    def facets(self) -> tuple[LaunchFacet, ...]:
        """Every capability this launch carries, one facet each, in comparison order."""
        return launch_facets(self)

    def facet(self, kind: type[_FacetT]) -> _FacetT:
        """This launch's facet of ``kind``."""
        return next(facet for facet in self.facets if isinstance(facet, kind))


class ResidentTurn(Generic[_RuntimeT_co]):
    """One activation borrowing a live transport without owning its process."""

    def __init__(self, turn: HarnessTurn[_RuntimeT_co]) -> None:
        self._turn = turn

    def _request_prefixes(self) -> list[dict[str, Any]]:
        """The member's request-meter records its CLI process read since the last call."""
        # Read from the turn's own process: every driver's turn holds the one
        # it reads, and a stand-in process without a meter has none to give.
        take = getattr(getattr(self._turn, "_process", None), "request_prefixes", None)
        return take() if callable(take) else []

    def lines(self) -> Iterator[HarnessEvent]:
        """Yield normalized driver events through the exact turn boundary.

        Ahead of each line's events, the prefixes the member's request meter
        measured since the line before (``request_prefixes``): a request's
        record is written as it is sent, before any of its answer.
        """
        prefixes = TurnPrefixes()
        try:
            for line in self._turn.raw_lines():
                yield from prefixes.events(self._request_prefixes())
                yield from self._turn.events_for_line(line)
        except Exception:
            # A stream that breaks before its boundary still reports what the
            # driver had gathered from it, ahead of the failure.
            yield from self._turn.flush_events()
            raise
        yield from prefixes.events(self._request_prefixes())

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
        The inference grant therefore does not participate in equality. Nor
        does a residency MCP credential, for the same reason: it follows the
        residency, which the supervisor checks beside this, so a probe that
        carries none may borrow the process holding one. Everything else the
        CLI fixes at process start still participates, including model,
        effort, the MCP servers and relay, a per-attempt MCP token, the
        non-renewable GitHub token and the model connection's source. A fresh
        GitHub token requires native resume in a replacement process, and so
        does a connection whose key, endpoint or protocols changed: its source
        names the credential revision, so the old key never serves a new turn.
        """
        return not self.incompatible_fields(launch)

    def incompatible_fields(self, launch: ResidentLaunch) -> tuple[str, ...]:
        """Name the process-fixed launch fields that keep ``launch`` from borrowing this process.

        The rule :meth:`compatible_with` applies, field by field. Only names
        are returned, never values: several of these fields are credentials,
        and the names are what a startup measurement records to explain why a
        live process could not take another turn.
        """
        held = self.launch

        def differing(names: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(name for name in names if getattr(held, name) != getattr(launch, name))

        # Each capability names what it fixes at process start
        # (cloud_agent_host.launch); the slot is compared last.
        return (
            *differing(_IDENTITY_FIELDS),
            *facet_differences(held.facets, launch.facets),
            *differing(_SLOT_FIELDS),
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
        state_dir = member_state_dir(launch)
        preparation, member_processes_reaped = self._member_preparation(spec, launch, state_dir)
        context = self._launch_context(
            spec,
            launch,
            credential=launch.credential,
            state_dir=state_dir,
            member_processes_reaped=member_processes_reaped,
            preparation=preparation,
        )
        if launch.plugins:
            # Every installer finds its slot reaped and its home prepared.
            preparation.run()
            context.base_env.update(
                member_launch(runtime_id).install_plugins(
                    self._manager, spec, launch, state_dir=state_dir, env=context.base_env
                )
            )
        if launch.native_session_id:
            session = driver.launch_resident_live(context, session_id=launch.native_session_id)
        else:
            session = driver.launch_live(context)
        try:
            session.ensure_started(model=launch.model, reasoning_effort=launch.reasoning_effort)
            if not session.process_alive():
                raise DriverError(f"runtime {runtime_id!r} resident process is not alive")
            return ResidentTransport(launch=launch, session=session)
        except Exception:
            with contextlib.suppress(Exception):
                session.close()
            raise

    def _member_preparation(
        self, spec: ChatSandboxSpec, launch: ResidentLaunch, state_dir: str
    ) -> tuple[MemberPreparation, bool]:
        """Queue what the member's own UID does before its CLI starts, in that order.

        The queue runs in one exec (:mod:`cloud_agent_host.member_setup.member_preparation`):
        before the first plugin installer, or otherwise at the spawn, with
        Claude's hook merge. Returns it, and whether it holds the slot's reap.
        """
        uid = MEMBER_UID_BASE + launch.slot_id
        preparation = MemberPreparation(
            self._manager, spec, user_uid=uid, user_gid=self._manager.backend.gid
        )
        steps = member_launch(launch.runtime_id)
        reaped = steps.reaps_before_preparation or bool(launch.plugins)
        if reaped:
            # A failed Fork can release a reserved slot. Reap that slot before
            # preparing its next member's launch state, so an old same-UID
            # process cannot race creation of the new private settings.
            self._manager.reap_member_processes(
                spec,
                slot_id=launch.slot_id,
                user_uid=uid,
                user_gid=self._manager.backend.gid,
                batch=preparation,
            )
        steps.prepare_member(self._manager, spec, launch, preparation)
        if launch.runtime_id in PRIVATE_MEMBER_STATE_ROOTS:
            self._prepare_member_state(spec, launch, state_dir, preparation)
        return preparation, reaped

    def _prepare_member_state(
        self,
        spec: ChatSandboxSpec,
        launch: ResidentLaunch,
        state_dir: str,
        batch: MemberPreparation | None = None,
    ) -> None:
        """Create the member-owned provider home used by this shared group.

        Queued on ``batch`` when one is given, and run now otherwise.
        """
        runtime_id = launch.runtime_id
        step = MemberStep(
            member_launch(runtime_id).member_state_argv(self._manager, launch, state_dir),
            timeout_s=120,
            failure=lambda stderr: DriverError(
                f"{runtime_id} member state could not be prepared: "
                + stderr.decode("utf-8", "replace")[-300:]
            ),
            # A group created by an earlier release, or a collection name a
            # member squatted on before the sandbox user secured it.
            retry_status=UNTRUSTED_COLLECTION_EXIT,
            before_retry=lambda: self._manager.ensure_private_member_collections(spec),
        )
        run_member_steps(
            self._manager,
            spec,
            step,
            user_uid=MEMBER_UID_BASE + launch.slot_id,
            user_gid=self._manager.backend.gid,
            batch=batch,
        )

    def _clone_private_member_state(
        self,
        spec: ChatSandboxSpec,
        source_launch: ResidentLaunch,
        target_launch: ResidentLaunch,
    ) -> None:
        """Copy one flushed provider home into the child UID's private home."""
        source_dir = member_state_dir(source_launch)
        target_dir = member_state_dir(target_launch)
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
            'set -eu; target="$1"; source="$2"; runtime="$3"; trusted="$4"; '
            + NEW_PRIVATE_MEMBER_HOME_PROLOGUE
            + 'find "$target" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; '
            'tar --extract --file=- --directory="$target" --no-same-owner '
            "--no-same-permissions; "
            'mkdir -p -- "$target/tmp" "$target/.tmp"; '
            'if [ "$runtime" = codex ]; then '
            'python3 -I - "$target" "$source" <<\'PY\'\n' + CODEX_REBASE_ROLLOUT_PATHS + "\nPY\n"
            "fi; "
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
                source_dir,
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

    def _prepare_runtime_helpers(self, spec: ChatSandboxSpec, launch: ResidentLaunch) -> None:
        """Run each facet's trusted step before the first spawn.

        In :data:`~cloud_agent_host.launch.facets.PREPARE_ORDER`: the browser
        service, sandbox MCP admission, the runtime's helpers (its CLI
        package's), then the compatibility helper.
        """
        context = PrepareContext(self._manager, spec, launch.slot_id)
        for kind in PREPARE_ORDER:
            launch.facet(kind).prepare(context)

    def _member_env(self, launch: ResidentLaunch) -> tuple[dict[str, str], str]:
        """The environment one member's CLI starts with, and its inference script.

        Exactly this member's own credentials: its private home and state, the
        platform inference credential, an owner connection's token or an owner
        environment's hand-off script, and the claim's repository-scoped GitHub
        token. Nothing another member of the group could read.
        """
        runtime_id = launch.runtime_id
        home = f"/home/agent-member-{launch.slot_id}"
        base_env = dict(self._manager.backend.base_env())
        environment = launch.facet(EnvironmentFacet)
        base_env.update(environment.env())
        base_env.update(
            {
                "HOME": home,
                "PATH": SANDBOX_PATH,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": CHAT_WORKDIR,
            }
        )
        base_env.update(launch.facet(InferenceFacet).env())
        # Only the residency's GitHub relay placeholder may authenticate gh and
        # git, and only when this host sends github.com to the relay. Without
        # that, GitHub itself rejects the placeholder, including a public
        # repository that would have been reachable with no credential. Do not
        # fall back to a token inherited from a custom host backend. The
        # owner's author identity still applies.
        base_env.pop("GH_TOKEN", None)
        base_env.pop("GITHUB_TOKEN", None)
        github = launch.facet(GitHubRelayFacet)
        if github.credential and not relay_configured():
            github = replace(github, credential="")
        base_env.update(github.env())
        if launch.runtime_version:
            base_env["PATH"] = (
                f"{runtime_bin_dir(runtime_id, launch.runtime_version)}:{base_env['PATH']}"
            )
        return base_env, environment.inference_script

    def _launch_context(
        self,
        spec: ChatSandboxSpec,
        launch: ResidentLaunch,
        *,
        credential: str,
        state_dir: str | None = None,
        member_processes_reaped: bool = False,
        preparation: MemberPreparation | None = None,
    ) -> LaunchContext:
        """Build one member-isolated launch seam used by live and fork probes.

        ``preparation`` holds what the member's UID still has to do before its
        CLI starts; the first spawn adds its own steps and runs them all.
        """
        uid = MEMBER_UID_BASE + launch.slot_id
        runtime_id = launch.runtime_id
        if preparation is None:
            preparation = MemberPreparation(
                self._manager, spec, user_uid=uid, user_gid=self._manager.backend.gid
            )
        activation_dir = f"{MEMBER_TMPFS_ROOT}/{launch.slot_id}/{runtime_id}"
        base_env, inference_script = self._member_env(launch)
        steps = member_launch(runtime_id)
        instructions = steps.platform_instructions(launch)
        # The connection's URL is used exactly as written (``ExactBaseUrl``),
        # typed by its protocol, so only the adapter that speaks it accepts it.
        model_base_url = connection_base_url(
            launch.inference_base_url,
            launch.inference_protocol,
            launch.inference_client_protocol,
            metered=launch.facet(InferenceFacet).metered,
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
                        batch=preparation,
                    )
                preparation.run()
                if inference_script:
                    # An exec of its own, after the reap has run: a process an
                    # earlier launch left as this UID could read it from the
                    # exec that reaps (cloud_agent_host.member_setup.member_preparation).
                    self._manager.write_member_inference_env(
                        spec,
                        inference_script,
                        slot_id=launch.slot_id,
                        user_uid=uid,
                        user_gid=self._manager.backend.gid,
                    )
                self._prepare_runtime_helpers(spec, launch)
                reaped = True
            pidfile = f"{MEMBER_TMPFS_ROOT}/{launch.slot_id}/resident-{secrets.token_hex(16)}.pid"
            argv, env = steps.with_platform_instructions(argv, env, instructions)
            if model_base_url.metered:
                argv, env = meter_launch(argv, env, launch.inference_base_url)
            process = self._manager.spawn(
                spec,
                argv,
                env=env,
                workdir=CHAT_WORKDIR,
                live_input=True,
                user_uid=uid,
                user_gid=self._manager.backend.gid,
                process_pidfile=pidfile,
            )
            if tuple(argv[: len(COMPAT_LAUNCHER)]) == COMPAT_LAUNCHER:
                # The launcher forks the member's meter, then becomes the CLI.
                # A turn's census taken before that misses the meter, and the
                # turn's reap then kills it as a leftover of the turn.
                try:
                    self._manager.await_member_launch(
                        spec,
                        pidfile=pidfile,
                        launcher=COMPAT_LAUNCHER[-1],
                        user_uid=uid,
                        user_gid=self._manager.backend.gid,
                    )
                except Exception:
                    with contextlib.suppress(Exception):
                        process.kill()
                    raise
            return process

        mcp_enabled = bool(launch.mcp_server_ids)
        return LaunchContext(
            workdir=CHAT_WORKDIR,
            model_base_url=model_base_url,
            credential=credential,
            provides_isolation=True,
            spawn_live=spawn_live,
            base_env=base_env,
            mcp_config=RuntimeMCPConfig(
                server_ids=launch.mcp_server_ids,
                base_url=launch.mcp_base_url if mcp_enabled else "",
                credential=launch.mcp_credential if mcp_enabled else "",
                # The gateway authenticates the relay's credential, so a launch
                # with no relay has none to reach it with.
                sandbox=mcp_enabled and bool(launch.sandbox_mcp_servers),
            ),
            state_dir=CHAT_STATE_DIR,
            session_state_dir=state_dir or CHAT_STATE_DIR,
            activation_dir=activation_dir,
            install_tool_hooks=lambda update: self._manager.install_tool_hooks(
                spec,
                {**update, **steps.tool_hook_settings(launch)},
                slot_id=launch.slot_id,
                thread_id=launch.thread_id,
                batch=preparation,
            ),
            claude_config_dir=steps.config_dir(launch),
        )

    def fork_native(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
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
            codex_baseline = codex_member.journal_baseline(existing)
            child_id = codex_member.recover_fork(
                self._manager,
                spec,
                source.launch,
                source_id=source_id,
                baseline=codex_baseline,
            )
        else:
            preparing = {**expected, "state": "preparing"}
            if runtime_id == "codex":
                codex_baseline = codex_member.fork_children(
                    self._manager,
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
                recovered = codex_member.new_fork_children(
                    self._manager,
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
            child_id = claude_member.fork_session(
                self._manager,
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
                http_member.clear_member_locks(self._manager, spec, source.launch)
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
