"""Agent runtime adapters (issue #1041) — the BYOA half of BYOA x BYOM.

Mirrors the provider-adapter pattern in ``serving/adapters/``: there the
gateway adapts to a model provider's dialect, here the sandbox adapts to an
agent CLI's dialect. Both keep the dialect-specific parsing at the edge so the
rest of the system sees one normalized shape.

An adapter answers three questions:

- ``prepare`` — what command runs this task headlessly, and with what
  environment? The CLI is pinned in the sandbox image; the adapter only builds
  the invocation.
- ``parse_event`` — how does one line of the CLI's output map onto our
  normalized event kinds? **Unrecognized lines are never dropped**: they come
  back as a ``raw`` event so the UI degrades to a log tail instead of going
  silent when a CLI changes its format.
- ``capabilities`` — what can this runtime actually do (report cost, speak MCP)?

Tiering (per the adjudicated design): Tier 1 runtimes get full normalization
and cost attribution; Tier 2 is a generic headless runner whose output flows
through as raw events, so a new agent costs nothing to support badly and can
be promoted when it earns it.

The Claude Code and pi mappings below were derived from observed output —
``claude -p --output-format stream-json`` and ``pi --mode json`` — not from
documentation. The corresponding test data is a sanitized, synthetic contract
fixture per runtime; see ``tests/fixtures/agent_runtime_streams/``.
"""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent_shared.event_payloads import bound_event_payload

if TYPE_CHECKING:
    from collections.abc import Callable

# Normalized event kinds. Kept in lockstep with agent_job_events.event_type and
# the frontend's types.ts; anything else must be reported as ``raw``.
THINKING = "thinking"
MESSAGE = "message"
TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"
DIFF = "diff"
USAGE = "usage"
ERROR = "error"
LIFECYCLE = "lifecycle"
RAW = "raw"

# Where the job token is handed to the agent CLI for its MCP calls. The
# generated MCP config references this name rather than carrying the value,
# so the credential never becomes a process argument.
MCP_TOKEN_ENV = "AGENT_MCP_TOKEN"

# Where the relay entry is handed to an image-shipped wrapper, as one JSON
# object of ``{name: url}``.
#
# The wrapper merges it into the config file it already writes, under whichever
# key its CLI spells MCP with. That split is deliberate: the address and the
# token reference are decided here, and the dialect is decided next to the
# pinned CLI version in ``deploy/sandbox/`` where it can be checked against the
# binary. Writing each dialect's file from Python would put five vendor schemas
# in one module and no way to review them beside the versions they describe.
MCP_SERVERS_ENV = "AGENT_MCP_SERVERS"

# The name every runtime knows the platform's MCP surface by.
#
# **One entry, not one per connection.** The relay's bare prefix is a merged
# host: it answers ``initialize`` itself and presents every connection of the
# job as a single catalogue whose tool and prompt names carry the connection's
# alias. A per-connection entry would therefore present that same catalogue once
# per connection, and a client that fanned out over them would offer every tool
# as many times as the conversation has servers.
#
# The name is a label the CLI prefixes onto tool names, not an alias the relay
# resolves -- aliases stay in the control plane, where a per-conversation name
# cannot be guessed from the sandbox. It is fixed rather than derived from the
# connection list because two of the five CLIs gate their MCP entries by name
# and exact URL identity, which a run-time-varying name cannot satisfy.
MERGED_MCP_SERVER_NAME = "session"


@dataclass(frozen=True)
class NormalizedEvent:
    """One event in the shape the control plane and UI understand."""

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Bound the report before it crosses the sandbox manager or worker transport."""
        object.__setattr__(self, "payload", bound_event_payload(self.payload))


#: Marks the ``raw`` events that carry an answer's text while the model writes
#: it. They are a preview: each harness still reports the complete text as the
#: ``message`` it always did, with the same ``preview_id``, and that replaces it.
REPLY_PREVIEW = "reply-preview"


@dataclass
class _PreviewStream:
    shown: str = ""
    pending: str = ""
    reported_at: float | None = None


class ReplyPreview:
    """Batch an answer's text into preview pieces while the model writes it.

    The CLIs report text a few tokens at a time, and every piece reported is an
    HTTP write and a stored row. So pieces are gathered: the first piece of an
    answer goes out at once, the rest at most once per :attr:`INTERVAL_S`. A
    piece's payload is ``{protocol, preview_id, delta}``, plus ``reset`` when its
    delta replaces the text shown so far instead of extending it.
    """

    INTERVAL_S = 0.2

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        """``clock`` is for tests; it defaults to ``time.monotonic``."""
        self._clock = clock or time.monotonic
        self._streams: dict[str, _PreviewStream] = {}

    def add(self, preview_id: str, delta: str) -> dict[str, Any] | None:
        """Gather ``delta``; return a piece when one is due."""
        stream = self._streams.setdefault(preview_id, _PreviewStream())
        stream.pending += delta
        if not stream.pending:
            return None
        now = self._clock()
        if stream.reported_at is not None and now - stream.reported_at < self.INTERVAL_S:
            return None
        return self._report(preview_id, stream, now)

    def grow_to(self, preview_id: str, text: str) -> dict[str, Any] | None:
        """The same, for a harness that resends the whole text so far."""
        stream = self._streams.setdefault(preview_id, _PreviewStream())
        known = stream.shown + stream.pending
        if text.startswith(known):
            return self.add(preview_id, text[len(known) :])
        # Rewritten rather than extended: start the preview again from here.
        stream.shown, stream.pending, stream.reported_at = text, "", self._clock()
        return {"protocol": REPLY_PREVIEW, "preview_id": preview_id, "delta": text, "reset": True}

    def flush(self, preview_id: str) -> dict[str, Any] | None:
        """Report what is still gathered now, for text that is complete."""
        stream = self._streams.get(preview_id)
        if stream is None or not stream.pending:
            return None
        return self._report(preview_id, stream, self._clock())

    def finish(self, preview_id: str) -> None:
        """Forget a preview: its complete message has arrived."""
        self._streams.pop(preview_id, None)

    def clear(self) -> None:
        """Forget every preview, at a turn's end."""
        self._streams.clear()

    def _report(self, preview_id: str, stream: _PreviewStream, now: float) -> dict[str, Any]:
        delta, stream.pending = stream.pending, ""
        stream.shown += delta
        stream.reported_at = now
        return {"protocol": REPLY_PREVIEW, "preview_id": preview_id, "delta": delta}


@dataclass(frozen=True)
class RuntimeCapabilities:
    """What a runtime can do, so the platform does not assume.

    Session continuity is deliberately not declared here: it belongs to
    ``drivers.HarnessCapabilities.continuity``, the only value the runner
    consults when picking a turn's continuity path.
    """

    cost_report: bool = False
    normalized_events: bool = False
    tier: int = 2
    #: The reasoning-effort levels this adapter can actually put on the wire,
    #: in the CLI's own vocabulary. Empty means "cannot express effort", which
    #: is what every runtime whose flag nobody has verified declares — the
    #: platform then never offers the control for it. Declared here rather than
    #: assumed from the model, because both halves have to be able to deliver:
    #: a model that takes an effort is no use behind a CLI with no way to say
    #: one, and the intersection is what gets offered.
    #:
    #: **Ordered weakest to strongest.** The composer preselects the deepest
    #: level a pair offers, and it finds it by taking the last one rather than
    #: by consulting a ranking table — there is no platform-wide effort scale
    #: to build such a table from, and inventing one would mean a third
    #: vocabulary to keep in step with the CLIs' and the models'. The order is
    #: therefore part of the declaration, and ``test_agent_runtimes`` pins it.
    reasoning_efforts: tuple[str, ...] = ()
    # Whether this adapter can point the CLI at the platform's MCP relay *and*
    # shut out every other MCP config source. Both halves or neither: a runtime
    # that could be given servers but not stopped from picking up the
    # repository's own would be worse than one with no MCP at all.
    mcp: bool = False


@dataclass(frozen=True)
class RuntimeMCPConfig:
    """The MCP connections available to one agent run, and where they are.

    ``server_ids`` decides *whether* the run has MCP at all, and which
    connections it may be offered; the relay resolves each alias against that
    job's own bindings when the catalogue is built. The endpoints themselves no
    longer depend on those names — see :meth:`AgentRuntime.relay_urls` — so a
    runtime is never told a per-connection address it could reconstruct an
    alias from.

    **A separate address and a separate credential from inference.** MCP is
    served by the cloud agent's own relay, model calls by the FreeInference
    gateway, and the two credentials are not interchangeable: a leaked MCP
    token buys no inference, a leaked inference grant buys no tools. Carrying
    them in one field, as this did while the gateway proxied MCP, would make
    that separation impossible to express.

    Still no endpoint or header for the *upstream*: each alias is a name one
    conversation gave one connection, the relay resolves it against that job's
    own bindings, and an upstream's credential never leaves the control plane.
    What is here is this deployment's own relay address and the token that opens
    it — and the token is referenced by environment variable, never written
    into an argument or into a config file.
    """

    server_ids: tuple[str, ...] = ()
    #: The cloud agent's MCP relay, as the sandbox can reach it. Empty means
    #: MCP is unavailable to this run, which a runtime must treat as "refuse"
    #: rather than "fall back to the inference address" — that address is a
    #: different service that no longer answers for MCP at all.
    base_url: str = ""
    #: The ``mcp``-scoped token. Never the inference credential.
    credential: str = ""


EMPTY_RUNTIME_MCP_CONFIG = RuntimeMCPConfig()


class RuntimeMCPUnavailableError(RuntimeError):
    """Raised when MCP is requested before a runtime has a mediated path."""


class RuntimeEffortUnsupportedError(RuntimeError):
    """Raised when an effort is requested of a runtime that cannot express it."""


class AgentRuntime:
    """Base adapter. Subclasses override the three questions."""

    name = "generic"
    binary = ""

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Return ``(argv, extra_env)`` to run this task headlessly.

        ``reasoning_effort`` is ``None`` for "let the CLI and the model decide",
        which is every turn that predates the control and every pair that
        cannot offer it. A non-``None`` value must be one this runtime
        declared; see :meth:`effort_args`.
        """
        raise NotImplementedError

    def _check_effort(self, effort: str | None) -> str | None:
        """Refuse a level this runtime never declared; return it otherwise.

        Fails closed the way :meth:`_require_empty_mcp_config` does. The
        control plane validates the same set at create time, so reaching this
        is a platform bug rather than a user's — which is exactly why it raises
        instead of dropping: a silently ignored effort is the CLI-flag lie this
        whole feature exists to end, and it would be indistinguishable from a
        turn that ran at the requested depth.
        """
        if effort is None:
            return None
        declared = self.capabilities().reasoning_efforts
        if effort not in declared:
            raise RuntimeEffortUnsupportedError(
                f"runtime {self.name!r} cannot express reasoning effort {effort!r} "
                f"(declares {list(declared)})"
            )
        return effort

    def effort_args(self, effort: str | None) -> list[str]:
        """The argv fragment expressing ``effort``, or nothing at all.

        Public because the resident session assembles its own argv and must ask
        the dialect layer how to spell this, exactly as it asks
        :meth:`ClaudeCodeRuntime.user_message_line` how to spell a user turn.

        Empty is not "unsupported": three of the five runtimes express this in
        a config file their wrapper writes rather than on the command line, and
        they carry it in the environment instead. :meth:`_check_effort` is what
        refuses; this only renders.
        """
        self._check_effort(effort)
        return []

    @staticmethod
    def relay_urls(config: RuntimeMCPConfig, *, gateway_base_url: str) -> dict[str, str]:
        """Resolve one run's MCP configuration into the sandbox-local relay URL.

        The agent is told this service's fixed MCP-only relay URL, and nothing
        else: not an upstream address, not the user's credential that reaches
        them. All of that stays in the control plane, which is what lets a
        sandbox use these servers without ever holding their credentials.

        **The merged host, which is what makes a connection generic.** The
        relay's per-connection route forwards one connection's protocol
        untouched, so it can only serve a server that already speaks the shape
        the CLI speaks: a legacy SSE server, a stateless-era server, or one that
        answers a call with ``InputRequiredResult`` is a 405 or a protocol error
        there. The bare prefix is the platform speaking MCP itself -- it opens
        the session in whichever era the server turned out to speak, merges
        every connection of the job into one catalogue, and routes a call back
        to its own server. Pointing a runtime at a per-connection alias would
        put the protocol back in the CLI's hands, which is the whole design
        decision this replaced.

        Empty when the run has asked for no servers, which is not a relay with
        nothing in it: a run told about an endpoint it must not use would call
        it.

        **No header and no credential here.** How a CLI is told to put a bearer
        token on a request is a dialect question, and every one of them spells
        it differently: Claude Code expands ``${VAR}``, OpenCode and its fork
        expand ``{env:VAR}``, and a config written with the wrong one sends the
        placeholder verbatim and 401s on every call. So the address is decided
        here and the reference is written next to the CLI that has to expand
        it, which is also where it can be checked against the pinned binary.
        """
        if not config.server_ids:
            return {}
        if not config.base_url:
            # No relay address is not a reason to fall back to the inference
            # one: that is a different service which does not answer for MCP.
            # Silently pointing the sandbox at it would produce 404s from an
            # unrelated host, which reads as "the tool is broken".
            raise RuntimeMCPUnavailableError(
                "MCP was requested but no relay address was configured for this run"
            )
        if not config.credential:
            # Every config below references the token rather than embedding it,
            # so a missing one expands to the empty string and the relay refuses
            # each call with 401 -- which reads to the agent as a broken server
            # rather than as a run that was never given a credential.
            raise RuntimeMCPUnavailableError(
                "MCP was requested but no relay credential was minted for this run"
            )
        base = config.base_url.rstrip("/").removesuffix("/v1")
        return {MERGED_MCP_SERVER_NAME: f"{base}/v1/agent/mcp"}

    @classmethod
    def _mcp_endpoints(
        cls, config: RuntimeMCPConfig, *, gateway_base_url: str
    ) -> dict[str, dict[str, Any]]:
        """The relay endpoint in Claude Code's own dialect and header syntax.

        **The token is referenced, not embedded.** ``${AGENT_MCP_TOKEN}`` is
        expanded by the CLI from the environment, so the credential never
        becomes a process argument — where it would be readable from the
        process table and, worse, liable to be copied into an error tail or an
        event payload the owner can read. Verified against the real CLI: with
        the header written this way and the variable exported, the proxy
        authenticates the request and the argv carries only the placeholder.
        """
        return {
            server_id: {
                "type": "http",
                "url": url,
                # Use the same bounded window for per-server request/tool
                # timers and the environment's overall/idle timers.
                "timeout": 1560000,
                "headers": {"Authorization": f"Bearer ${{{MCP_TOKEN_ENV}}}"},
            }
            for server_id, url in cls.relay_urls(config, gateway_base_url=gateway_base_url).items()
        }

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Map one output line onto a normalized event, or ``None`` to skip."""
        raise NotImplementedError

    def capabilities(self) -> RuntimeCapabilities:
        """Describe what this runtime supports."""
        return RuntimeCapabilities()

    def _require_empty_mcp_config(self, config: RuntimeMCPConfig) -> None:
        """Fail closed until this adapter has a platform-mediated MCP path."""
        if config.server_ids:
            raise RuntimeMCPUnavailableError(
                f"runtime {self.name!r} cannot use MCP servers until its isolated relay path is enabled"
            )

    def _accept_mcp(
        self,
        config: RuntimeMCPConfig,
        *,
        gateway_base_url: str,
        env: dict[str, str],
    ) -> None:
        """Carry the relay endpoints into the run, or refuse to.

        The default refuses, and an adapter overrides this only when it can do
        *both* halves: point the CLI at the platform's relay and stop it reading
        any other MCP source. A runtime that could be given servers but not
        stopped from picking up the repository's own would be worse than one
        with no MCP at all, so there is no override that supplies endpoints
        without a verified suppression.
        """
        del gateway_base_url, env
        self._require_empty_mcp_config(config)

    def _wrapper_mcp_env(
        self,
        config: RuntimeMCPConfig,
        *,
        gateway_base_url: str,
        env: dict[str, str],
    ) -> None:
        """Publish the relay endpoint for an image-shipped wrapper to merge.

        Written even when the run has no servers. A key that appears only
        sometimes would leave the run with whatever MCP sources the CLI finds
        for itself on the turns where it is absent, which is the shape the
        suppression exists to prevent.
        """
        endpoints = self.relay_urls(config, gateway_base_url=gateway_base_url)
        env[MCP_SERVERS_ENV] = json.dumps(endpoints, sort_keys=True)
        # The wrapper's config references this name; it has to resolve, to the
        # token or to the empty string when the run has no servers. Setting it
        # only sometimes would leave the reference dangling on the turns where
        # it was absent, which is the shape a stale token hides in.
        env[MCP_TOKEN_ENV] = config.credential

    @staticmethod
    def _raw(line: str, reason: str) -> NormalizedEvent:
        """Wrap an unrecognized line so nothing is silently lost."""
        return NormalizedEvent(RAW, {"text": line[:4000], "reason": reason})


class ClaudeCodeRuntime(AgentRuntime):
    """Claude Code headless (``claude -p --output-format stream-json``).

    Speaks the Anthropic Messages surface to the gateway, so ``credential``
    (the job's capability token) rides in ``ANTHROPIC_API_KEY``.
    """

    name = "claude-code"
    binary = "claude"

    # `type: "system"` covers two unrelated things: a few genuine milestones,
    # and progress telemetry. Only these are milestones. The distinction is not
    # cosmetic — the first real job on staging stored 412 lifecycle events, 408
    # of them `subtype: "thinking_tokens"`, so a counter became 408 rows in the
    # append-only log, 408 SSE frames, and 408 ticked-off steps in the UI.
    # An allowlist rather than a denylist: a CLI upgrade that invents another
    # counter must not be able to flood the stream just because nobody had
    # heard of it yet.
    MILESTONE_SUBTYPES = frozenset({"init", "compact_boundary"})

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        """Track which unclassified subtypes this run has already reported."""
        # One adapter instance per job (see get_runtime), so this is per-run
        # state — the suppression below cannot leak across jobs.
        self._reported_subtypes: set[str] = set()
        #: The CLI's own session id, captured from the ``init`` event. The
        #: retained container resumes this native locator directly; ``None``
        #: until the CLI has announced itself.
        self.session_id: str | None = None
        self._preview = ReplyPreview(clock)
        # The text block being written: ``<message id>:<block index>``.
        self._message_id: str | None = None
        self._text_block: str | None = None

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        config_dir: str | None = None,
        resume_session_id: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Build the headless invocation and its environment.

        ``config_dir`` relocates the CLI's config/session storage inside the
        retained container's writable layer; ``resume_session_id`` continues
        an earlier native session with only the new prompt after the resident
        process was replaced. Both are Claude-specific and passed only by the
        Claude driver; the generic session never sets them.
        """
        argv = [
            self.binary,
            "-p",
            task_prompt,
        ]
        if resume_session_id:
            argv += ["--resume", resume_session_id]
        argv += [
            "--model",
            model,
            # Kept adjacent to --model deliberately: the resident session finds
            # the model slot by index and rebuilds the effort beside it, so the
            # two settings a turn can carry stay in one place in the argv.
            *self.effort_args(reasoning_effort),
            "--output-format",
            "stream-json",
            "--verbose",
            # Without it the CLI writes an answer only once the model has
            # finished it, so the owner watched nothing happen and then a whole
            # answer. With it the text also arrives as it is written, as
            # ``stream_event`` lines; ``_from_stream`` turns those into a
            # preview that the complete block then replaces.
            "--include-partial-messages",
            # A repository can commit .mcp.json and Claude otherwise starts
            # its stdio commands before the model's first turn. Reproduced
            # against the real CLI in this exact configuration: without this
            # flag the CLI reports the repository's own server alongside the
            # platform's, with no approval step, because the permission mode
            # below is precisely what removes the gate that would have caught
            # it. Unconditional, including for a job with no MCP servers —
            # which is where an injected one would be least expected.
            "--strict-mcp-config",
            # Fork siblings share the repository, so its project/local
            # settings are untrusted launch input. Only the member's private
            # user settings (including platform-installed hooks) are loaded.
            "--setting-sources",
            "user",
            # The sandbox IS the boundary, so an interactive permission prompt
            # inside it has nothing left to protect — it only guarantees the
            # agent cannot do the work. Without this the CLI denies every write
            # with "you haven't granted it yet", the job produces an empty
            # patch, and (worse) a model that ignores the error still reports
            # success. Found by the first real run, not by the fake.
            "--permission-mode",
            "bypassPermissions",
            # Paired with --strict-mcp-config above: that flag makes this the
            # *only* source of MCP servers, and this supplies the platform's.
            # Passed even when empty, so the pairing has no gap.
            "--mcp-config",
            json.dumps(
                {"mcpServers": self._mcp_endpoints(mcp_config, gateway_base_url=gateway_base_url)}
            ),
        ]
        env = {
            "ANTHROPIC_BASE_URL": gateway_base_url.rstrip("/").removesuffix("/v1"),
            "ANTHROPIC_API_KEY": credential,
            "MCP_TOOL_TIMEOUT": "1560000",
            # Remote MCP has a separate five-minute idle timer. The relay can
            # wait ten minutes for owner approval without sending progress;
            # keep that wait within the overall tool timeout so the CLI receives
            # the approval outcome instead of abandoning a still-pending call.
            "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT": "1560000",
            # Referenced by the MCP config above rather than written into it,
            # so the job token never appears in a process argument.
            # The MCP relay's token, which is *not* the inference credential
            # above. Two services, two scopes, two blast radii.
            MCP_TOKEN_ENV: mcp_config.credential,
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            # A concrete env override outranks Agent's model argument and the
            # subagent definition. `inherit` skips that override, allowing
            # Explore to pick an ungranted default (e.g. opus for DeepSeek).
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
            # The pinned CLI retries transient inference failures inside the
            # current native turn with exponential backoff and jitter. Extend
            # its default request budget without replaying the user's prompt;
            # disabling the watchdog keeps exhaustion bounded.
            "CLAUDE_CODE_MAX_RETRIES": "15",
            "CLAUDE_CODE_RETRY_WATCHDOG": "0",
            # Telemetry and auto-update are switched off explicitly. The
            # sandbox reaches the public web, so nothing else stops the CLI
            # phoning home or replacing itself in the middle of a job.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        if config_dir:
            # Session files land in the retained container layer instead of a
            # process-private HOME, so docker stop/start can resume them.
            env["CLAUDE_CONFIG_DIR"] = config_dir
        return argv, env

    def prepare_live(
        self,
        *,
        workdir: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        config_dir: str | None = None,
        resume_session_id: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """The resident invocation (HOT): prompts arrive on stdin, not argv.

        ``--input-format stream-json`` keeps the process alive between turns,
        reading user messages from stdin — the terminal-never-closed shape the
        design's HOT tier is. ``--replay-user-messages`` echoes each one back
        on stdout at the moment the CLI reads it into the conversation, which
        is how a message sent into a running turn is known to have landed in
        it. The common argv/env builder stays shared with runtime conformance
        tests so auth, model, and MCP flags cannot drift.
        """
        argv, env = self.prepare(
            workdir=workdir,
            task_prompt="",
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
            config_dir=config_dir,
            resume_session_id=resume_session_id,
        )
        # Drop the empty positional prompt (`-p ""`), add the input format.
        prompt_at = argv.index("-p")
        del argv[prompt_at : prompt_at + 2]
        argv[1:1] = ["-p", "--input-format", "stream-json", "--replay-user-messages"]
        # AskUserQuestion and ExitPlanMode require owner interaction even in bypassPermissions.
        # The pinned CLI sends its can_use_tool request over this control
        # channel, and the live turn answers it with the owner's choices.
        argv += ["--permission-prompt-tool", "stdio"]
        return argv, env

    @staticmethod
    def user_message_line(text: str) -> str:
        """One stdin line carrying one user turn, in the CLI's wire shape."""
        return json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
        )

    @staticmethod
    def turn_settled(line: str) -> bool:
        """Whether this stdout line is the CLI's end-of-turn result event."""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False
        return isinstance(event, dict) and event.get("type") == "result"

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Normalize one stream-json line."""
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return self._raw(line, "not json")
        if not isinstance(event, dict):
            return self._raw(line, "not an object")

        kind = event.get("type")
        if kind == "system":
            subtype = str(event.get("subtype") or "system").strip() or "system"
            if subtype == "api_retry":
                # Each notice describes a new request attempt and its wait;
                # suppressing repeated subtypes would hide the retry progress.
                return NormalizedEvent(
                    LIFECYCLE,
                    {
                        "phase": "api_retry",
                        "attempt": event.get("attempt"),
                        "max_retries": event.get("max_retries"),
                        "retry_delay_ms": event.get("retry_delay_ms"),
                        "error_status": event.get("error_status"),
                    },
                )
            if subtype not in self.MILESTONE_SUBTYPES:
                # Not discarded outright: the first occurrence is kept as a raw
                # diagnostic so a subtype nobody has classified yet is still
                # discoverable in the event log. Repeats are dropped, which is
                # what turns 408 rows into 1.
                if subtype in self._reported_subtypes:
                    return None
                self._reported_subtypes.add(subtype)
                return self._raw(line, f"unclassified system subtype {subtype!r}")
            # Only the session's own init is republished as its inventory. A
            # subagent emits the same subtype for a narrower menu, and the
            # normalized event carries no marker saying which one it was, so
            # attaching the inventory there would read as the session's.
            harness: dict[str, Any] | None = None
            if subtype == "init" and not event.get("parent_tool_use_id"):
                captured = str(event.get("session_id") or "").strip()
                if captured:
                    self.session_id = captured
                harness = _harness_inventory(event)
            payload: dict[str, Any] = {
                "phase": subtype,
                "model": event.get("model"),
                "runtime_version": event.get("claude_code_version"),
                "session_id": self.session_id,
            }
            if harness is not None:
                payload["harness"] = harness
            return NormalizedEvent(LIFECYCLE, payload)
        if kind == "stream_event":
            return self._from_stream(event)
        if kind == "assistant":
            return self._from_assistant(event)
        if kind == "user":
            return self._from_user(event)
        if kind == "rate_limit_event":
            return NormalizedEvent(
                LIFECYCLE, {"phase": "rate_limit", "info": event.get("rate_limit_info")}
            )
        if kind == "result":
            self._preview.clear()
            self._message_id = self._text_block = None
            return self._from_result(event)
        return self._raw(line, f"unknown type {kind!r}")

    def _from_stream(self, event: dict[str, Any]) -> NormalizedEvent | None:
        """Preview the answer text the model is writing.

        Only answer text is previewed. Thinking stays a single status in the
        chat, and a tool call's input is shown once it is complete; a file the
        model writes would otherwise arrive as thousands of fragments. A
        subagent's text is not the conversation's answer, so it is left out
        too. Every other line is consumed here and stored nowhere, which is
        safe because the CLI repeats the complete content in ``assistant`` lines.
        """
        if event.get("parent_tool_use_id"):
            return None
        stream = event.get("event")
        if not isinstance(stream, dict):
            return None
        kind = stream.get("type")
        index = stream.get("index")
        if kind == "message_start":
            message = stream.get("message")
            message_id = message.get("id") if isinstance(message, dict) else None
            self._message_id = str(message_id) if message_id else None
            self._text_block = None
            return None
        if kind == "content_block_start":
            block = stream.get("content_block")
            is_text = isinstance(block, dict) and block.get("type") == "text"
            self._text_block = (
                f"{self._message_id}:{index}"
                if is_text and self._message_id and isinstance(index, int)
                else None
            )
            return None
        delta = stream.get("delta")
        if (
            kind != "content_block_delta"
            or self._text_block is None
            or self._text_block != f"{self._message_id}:{index}"
            or not isinstance(delta, dict)
            or delta.get("type") != "text_delta"
        ):
            return None
        piece = self._preview.add(self._text_block, str(delta.get("text") or ""))
        return NormalizedEvent(RAW, piece) if piece else None

    def _from_assistant(self, event: dict[str, Any]) -> NormalizedEvent:
        """Map an assistant turn to its most significant block."""
        message = event.get("message") or {}
        blocks = message.get("content") or []
        usage = message.get("usage") or {}
        # The CLI emits text and parallel tools as separate blocks of one
        # response, each repeating its usage. Keep the identity for counting.
        usage_fields = {"usage": usage or None, "usage_id": message.get("id")}
        parent_tool_use_id = event.get("parent_tool_use_id")
        if isinstance(parent_tool_use_id, str) and parent_tool_use_id:
            # The enclosing Task/Agent call owns these blocks. Keep that
            # recorded relationship so the UI can distinguish a child agent's
            # activity and reply from the conversation's own answer.
            usage_fields["parent_tool_use_id"] = parent_tool_use_id
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                return NormalizedEvent(
                    TOOL_USE,
                    {
                        "name": block.get("name"),
                        "id": block.get("id"),
                        "input": block.get("input"),
                        **usage_fields,
                    },
                )
            if block.get("type") == "thinking":
                return NormalizedEvent(
                    THINKING, {"text": block.get("thinking") or "", **usage_fields}
                )
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        payload: dict[str, Any] = {"text": text, **usage_fields}
        preview_id = self._text_block
        if (
            preview_id is not None
            and not event.get("parent_tool_use_id")
            and preview_id.startswith(f"{message.get('id')}:")
        ):
            # The pinned 2.1.220 writes a block's complete line before that
            # block's stop event, so this is the block that was previewed.
            payload["preview_id"] = preview_id
            self._preview.finish(preview_id)
            self._text_block = None
        return NormalizedEvent(MESSAGE, payload)

    def _from_user(self, event: dict[str, Any]) -> NormalizedEvent | None:
        """Map a tool result echoed back into the conversation."""
        if event.get("isReplay"):
            # A user message the CLI read from stdin, echoed back: the driver
            # accounts for it, and it is nothing new to report.
            return None
        blocks = (event.get("message") or {}).get("content") or []
        parent_tool_use_id = event.get("parent_tool_use_id")
        parent_fields = (
            {"parent_tool_use_id": parent_tool_use_id}
            if isinstance(parent_tool_use_id, str) and parent_tool_use_id
            else {}
        )
        # Native tools carry their structured result outside the content block.
        # TaskCreate's assigned task ID is here rather than in its input.
        structured_output = event.get("tool_use_result")
        result_fields = (
            {"structured_output": structured_output}
            if isinstance(structured_output, (dict, list))
            else {}
        )
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                return NormalizedEvent(
                    TOOL_RESULT,
                    {
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": bool(block.get("is_error")),
                        "content": _payload_text(content),
                        **parent_fields,
                        **result_fields,
                    },
                )
        return NormalizedEvent(RAW, {"text": "user turn without a tool result"})

    def _from_result(self, event: dict[str, Any]) -> NormalizedEvent:
        """Map the terminal result line."""
        # Result usage is the total for this query (one platform attempt),
        # including failed queries. It supersedes provisional block usage.
        usage_fields = {"usage": event.get("usage") or None, "usage_scope": "attempt"}
        if event.get("is_error") or event.get("terminal_reason") == "api_error":
            # An API failure arrives as subtype "success" with the reason in
            # ``result``; the error subtypes carry it in ``errors`` instead
            # (both verified against the pinned 2.1.220).
            errors = event.get("errors")
            reasons = [str(item) for item in errors if item] if isinstance(errors, list) else []
            text = (
                str(event.get("result") or "")
                or "; ".join(reasons)
                or str(event.get("subtype") or "")
            )
            return NormalizedEvent(
                ERROR,
                {
                    "subtype": event.get("subtype"),
                    "terminal_reason": event.get("terminal_reason"),
                    "api_error_status": event.get("api_error_status"),
                    "text": text[:2000],
                    **usage_fields,
                },
            )
        return NormalizedEvent(
            LIFECYCLE,
            {
                "phase": "result",
                "text": str(event.get("result") or ""),
                **usage_fields,
                "num_turns": event.get("num_turns"),
                # Reported for observability only. The billing authority is
                # api_logs.agent_job_id — the platform never trusts a number
                # the agent computed about itself.
                "reported_cost_usd": event.get("total_cost_usd"),
            },
        )

    #: Captured from the pinned 2.1.220, **not** from ``--help``: the 2.1.90
    #: help string reads "(low, medium, high, max)" and this list was first
    #: written from it, missing ``xhigh`` — which the CLI does put on the wire.
    #: Reading the documentation instead of the request is how a level the
    #: product supports went unoffered.
    #:
    #: Exactness matters more here than for the config-file runtimes, because
    #: **an unrecognised level is not refused, it silently becomes ``high``**
    #: (``--effort ultra``, ``none`` and ``minimal`` all arrive as ``high``).
    #: A level declared here that the CLI does not know would therefore run at
    #: a depth nobody chose while reporting the one they did.
    EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

    def effort_args(self, effort: str | None) -> list[str]:
        """Express effort as ``--effort <level>``."""
        if self._check_effort(effort) is None:
            return []
        return ["--effort", str(effort)]

    def capabilities(self) -> RuntimeCapabilities:
        """Claude Code is Tier 1: normalized events, a cost report, and MCP."""
        return RuntimeCapabilities(
            cost_report=True,
            normalized_events=True,
            tier=1,
            mcp=True,
            reasoning_efforts=self.EFFORT_LEVELS,
        )


class CodexRuntime(AgentRuntime):
    """OpenAI Codex headless (``codex exec --json``).

    Speaks the OpenAI surface, configured through command-line provider flags
    so nothing writes to or depends on the operator's own Codex config.
    """

    name = "codex"
    binary = "codex"

    def __init__(self) -> None:
        """Remember the native thread locator announced by ``--json``."""
        self.session_id: str | None = None

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        session_state_dir: str | None = None,
        resume_session_id: str | None = None,
        activation_dir: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Build the headless invocation and its environment.

        ``reasoning_effort`` becomes ``-c model_reasoning_effort="<level>"``
        among the provider flags below. Verified against the pinned
        ``CODEX_VERSION`` (0.145.0) rather than assumed: the CLI puts it on the
        wire as ``reasoning.effort`` in the ``/v1/responses`` body, which is
        exactly where the gateway's Responses translation reads it. See the
        recorded capture in ``tests/fixtures/agent_runtime_streams``.

        MCP uses the merged session relay, authenticated through the member's
        environment. The trusted host installs the exact relay URL into the
        root-owned system requirements before the CLI starts, for both exec
        and app-server. Repository configuration cannot admit another server.

        Mirrors the invocation the gateway's Codex on-call workflow ran in
        production, because that one was known to work against this gateway.
        Three things it gets right that the first pass here did not:

        - the model rides ``--model``. The previous version set ``CODEX_MODEL``,
          which is not a variable the CLI reads, so the owner's model choice was
          silently dropped and Codex used whatever its own default was.
        - ``wire_api = "responses"``, not ``"chat"``. Codex removed chat wire
          support upstream (openai/codex#7782); our gateway serves
          ``/v1/responses`` and translates southbound.
        - the provider is configured with ``-c`` flags rather than a config file.
          ``--ignore-user-config`` keeps the operator's own config out, while a
          supplied ``session_state_dir`` becomes ``CODEX_HOME`` so Codex's
          rollout files survive a process exit and ``exec resume`` can find
          them on the next turn.
        """
        _ = activation_dir
        base = gateway_base_url.rstrip("/").removesuffix("/v1")
        provider = "hybridinference"
        argv = [
            self.binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            # The operator's own Codex config must not reach a sandbox run.
            "--ignore-user-config",
            # A container backend is already the boundary. Running Codex's
            # bubblewrap sandbox inside that non-root, capability-dropped
            # container cannot create its user namespace, so every shell
            # command fails before it starts. The process backend has no such
            # outer boundary and must retain Codex's workspace sandbox.
            "--sandbox",
            "danger-full-access" if provides_isolation else "workspace-write",
            "--model",
            model,
            # Beside the other -c overrides, and built by the dialect helper so
            # every resident launch uses exactly the same spelling.
            *self.effort_args(reasoning_effort),
            *self._mcp_args(mcp_config, gateway_base_url=gateway_base_url),
            "-c",
            f'model_provider="{provider}"',
            "-c",
            f'model_providers.{provider}.name="HybridInference"',
            "-c",
            f'model_providers.{provider}.base_url="{base}/v1"',
            "-c",
            f'model_providers.{provider}.env_key="CODEX_API_KEY"',
            "-c",
            f'model_providers.{provider}.wire_api="responses"',
        ]
        # 0.145.0 accepts the exec-wide flags above before its ``resume``
        # subcommand. Keeping one option list prevents fresh and resumed turns
        # from drifting in provider, model, sandbox, or reasoning behaviour.
        if resume_session_id:
            argv += ["resume", resume_session_id, task_prompt]
        else:
            argv.append(task_prompt)
        env = {
            "CODEX_API_KEY": credential,
            MCP_TOKEN_ENV: mcp_config.credential,
            # Some code paths still read the OpenAI names; keep them consistent
            # rather than leaving a second, stale credential source.
            "OPENAI_BASE_URL": f"{base}/v1",
            "OPENAI_API_KEY": credential,
        }
        if session_state_dir:
            # Codex requires CODEX_HOME itself to exist. /agent-state is made
            # by the image (or is the state mount), so use that exact root
            # rather than naming a child the CLI would refuse to create.
            env["CODEX_HOME"] = session_state_dir
        return argv, env

    def prepare_live(
        self,
        *,
        workdir: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        session_state_dir: str | None = None,
        resume_session_id: str | None = None,
        activation_dir: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Build the pinned 0.145.0 stdio app-server transport.

        Thread creation/resume is an RPC after the initialize handshake, so
        ``model``, ``workdir`` and ``resume_session_id`` intentionally do not
        become positional CLI arguments here.  Provider configuration remains
        command-line overrides, exactly as in :meth:`prepare`, and the root-owned
        managed MCP allowlist installed before launch remains the final policy
        layer for app-server (which has no ``--ignore-user-config`` flag).
        """
        _ = (workdir, model, provides_isolation, resume_session_id, activation_dir)
        base = gateway_base_url.rstrip("/").removesuffix("/v1")
        provider = "hybridinference"
        argv = [
            self.binary,
            *self.effort_args(reasoning_effort),
            *self._mcp_args(mcp_config, gateway_base_url=gateway_base_url),
            "-c",
            f'model_provider="{provider}"',
            "-c",
            f'model_providers.{provider}.name="HybridInference"',
            "-c",
            f'model_providers.{provider}.base_url="{base}/v1"',
            "-c",
            f'model_providers.{provider}.env_key="CODEX_API_KEY"',
            "-c",
            f'model_providers.{provider}.wire_api="responses"',
            "app-server",
        ]
        env = {
            "CODEX_API_KEY": credential,
            MCP_TOKEN_ENV: mcp_config.credential,
            "OPENAI_BASE_URL": f"{base}/v1",
            "OPENAI_API_KEY": credential,
        }
        if session_state_dir:
            env["CODEX_HOME"] = session_state_dir
        return argv, env

    def _mcp_args(self, config: RuntimeMCPConfig, *, gateway_base_url: str) -> list[str]:
        """Use the same authenticated relay configuration in exec and app-server."""
        endpoints = self.relay_urls(config, gateway_base_url=gateway_base_url)
        entries = []
        for name, url in endpoints.items():
            entries.append(
                f"{name} = {{ url = {json.dumps(url)}, "
                f'bearer_token_env_var = "{MCP_TOKEN_ENV}", '
                "required = true, startup_timeout_sec = 30, tool_timeout_sec = 1560 }"
            )
        return ["-c", "mcp_servers = { " + ", ".join(entries) + " }"]

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Normalize one ``codex exec --json`` line."""
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return self._raw(line, "not json")
        if not isinstance(event, dict):
            return self._raw(line, "not an object")

        kind = event.get("type") or ""
        item = event.get("item") or {}
        item_type = item.get("type") or item.get("item_type")

        if kind == "thread.started":
            captured = str(event.get("thread_id") or "").strip()
            if captured:
                self.session_id = captured
            return NormalizedEvent(
                LIFECYCLE,
                {"phase": "init", "session_id": self.session_id},
            )

        if kind.startswith("item.") and item_type:
            if item_type == "command_execution" and kind == "item.completed":
                output = (
                    item.get("aggregated_output") or item.get("output") or item.get("stdout") or ""
                )
                exit_code = item.get("exit_code")
                return NormalizedEvent(
                    TOOL_RESULT,
                    {
                        "tool_use_id": item.get("id"),
                        "is_error": isinstance(exit_code, int) and exit_code != 0,
                        "content": _truncate(output),
                        "exit_code": exit_code,
                    },
                )
            if item_type in ("command_execution", "function_call", "tool_call"):
                return NormalizedEvent(
                    TOOL_USE,
                    {
                        "name": item.get("name") or item_type,
                        "id": item.get("id"),
                        "input": item.get("command") or item.get("arguments"),
                    },
                )
            if item_type in ("agent_message", "assistant_message"):
                return NormalizedEvent(
                    MESSAGE,
                    {
                        "text": _truncate(item.get("text")),
                        "truncated": _clipped(item.get("text")),
                    },
                )
            if item_type in ("reasoning", "thinking"):
                return NormalizedEvent(THINKING, {"text": _truncate(item.get("text"))})
            if item_type in ("patch", "file_change"):
                return NormalizedEvent(DIFF, {"text": _truncate(item.get("changes"))})
        if kind in ("turn.completed", "session.completed", "thread.completed"):
            return NormalizedEvent(LIFECYCLE, {"phase": "result", "usage": event.get("usage")})
        if kind in ("error", "turn.failed"):
            return NormalizedEvent(ERROR, {"text": _truncate(event.get("error") or event)})
        return self._raw(line, f"unknown type {kind!r}")

    #: What ``model_reasoning_effort`` may be set to. Codex 0.145.0 does **not**
    #: validate this locally -- ``--strict-config`` checks the key's name and
    #: nothing else, and a nonsense value reaches the wire unchallenged -- so
    #: this list is not the CLI enforcing anything. It is the vocabulary the
    #: CLI is documented to use, and the real constraint is the intersection
    #: with the model's own declared domain, which is where a level the model
    #: never heard of gets refused.
    EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

    def effort_args(self, effort: str | None) -> list[str]:
        """Express effort as a ``-c model_reasoning_effort`` override."""
        if self._check_effort(effort) is None:
            return []
        return ["-c", f'model_reasoning_effort="{effort}"']

    def capabilities(self) -> RuntimeCapabilities:
        """Codex is Tier 1 for events; cost comes from the gateway ledger."""
        return RuntimeCapabilities(
            cost_report=False,
            normalized_events=True,
            tier=1,
            reasoning_efforts=self.EFFORT_LEVELS,
            mcp=True,
        )


class GenericRuntime(AgentRuntime):
    """Tier 2: run any headless CLI and stream its output as raw events.

    The point of Tier 2 is that a new agent costs nothing to support: it gets
    a working log tail immediately, and only earns a normalizing adapter once
    it proves worth the maintenance.
    """

    name = "generic"

    def __init__(self, command_template: str, *, binary: str = "") -> None:
        """Build from a shell-style template using ``{prompt}`` / ``{model}``."""
        self.command_template = command_template
        self.binary = binary or shlex.split(command_template)[0]

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Expand the template into argv without ever invoking a shell.

        A Tier 2 runtime is any headless CLI, so there is no flag this adapter
        could know to pass and no way to shut out a repository's own config
        *unless* an image-shipped wrapper supplies one. That is what
        :meth:`_accept_mcp` decides, and why the base answer is a refusal: a
        non-empty set is dropped tools or someone else's servers, and neither
        is a thing to discover mid-run.
        """
        self.effort_args(reasoning_effort)
        argv = [
            part.replace("{prompt}", task_prompt).replace("{model}", model)
            for part in shlex.split(self.command_template)
        ]
        base = gateway_base_url.rstrip("/").removesuffix("/v1")
        env = {
            "OPENAI_BASE_URL": f"{base}/v1",
            "OPENAI_API_KEY": credential,
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_API_KEY": credential,
            "MCP_TOOL_TIMEOUT": "1560000",
        }
        self._accept_mcp(mcp_config, gateway_base_url=gateway_base_url, env=env)
        return argv, env

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Emit every non-empty line as a raw event."""
        line = line.rstrip()
        if not line:
            return None
        return NormalizedEvent(RAW, {"text": line[:4000]})


#: The reviewed MCP bridge, shipped in the sandbox image and loaded explicitly.
#: It lives beside the other image helpers rather than in the workspace, so the
#: code that talks to the relay is the code the image was built and reviewed
#: with. Verified against pi 0.73.1: with `--no-extensions -e <this>` the relay
#: is discovered at its merged endpoint, the tools it lists are registered under
#: that one entry's name followed by the relay's own `<alias>__<tool>`, a call
#: reaches the upstream with the bare name, and a repository's own
#: `.pi/extensions/*.ts` does not load.
PI_MCP_BRIDGE = "/usr/local/libexec/pi-mcp-bridge.ts"


class PiRuntime(GenericRuntime):
    """Tier 1: the pi coding agent, ``--mode json`` normalized.

    pi ignores ``OPENAI_BASE_URL`` — its built-in ``openai`` provider goes
    straight to api.openai.com (verified against a local fake: zero hits, a
    real OpenAI 401). The supported route is a custom provider in
    ``~/.pi/agent/models.json``, so the sandbox image ships a reviewed
    ``pi-freeinference`` wrapper that writes that file from this environment
    and then ``exec``s the real CLI. The prompt stays in argv end to end;
    nothing user-controlled passes through a shell.

    Promoted from Tier 2 on the condition the tier design set: real jobs
    proved the format. Passing the stream through as ``raw`` was not a quiet
    downgrade but a silent one — ``raw`` renders nowhere in the thread and
    only ``message`` events reach ``agent_thread_messages``, so five finished
    staging runs (thinking, a bash call, a full answer each) showed the owner
    an empty conversation and "completed (no changes)". The mapping below is
    derived from those recorded runs, not from documentation; the fixture is a
    sanitized replay of one.
    """

    name = "pi"

    # pi streams a completed block twice: once as deltas, then whole. The
    # ``*_end`` sub-events are what is mapped — the recorded run's 300
    # ``message_update`` lines carry 4 completed blocks between them, and
    # emitting per delta would be the thinking_tokens flood again with a
    # different name. Answer text deltas only feed the batched preview.
    _BLOCK_END_KINDS = frozenset({"thinking_end", "text_end", "toolcall_end"})

    # Recognized and deliberately not emitted. ``turn_end`` and ``agent_end``
    # repeat the whole assistant message that ``*_end`` already reported, and
    # the ``tool_execution_*`` trio repeats the tool call and its output; the
    # authoritative copies carry ``isError``, which those do not.
    _SILENT_KINDS = frozenset(
        {
            "agent_start",
            "turn_start",
            "turn_end",
            "message_start",
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        }
    )

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        """Fix the wrapper invocation and remember pi's native session id."""
        super().__init__(
            "pi-freeinference --provider freeinference --model {model} "
            # pi has no native MCP client at the pinned version; MCP arrives
            # through executable extensions, and a project-local one is as easy
            # to commit as a file in the checkout. `--no-extensions` disables
            # discovery entirely -- the project's `.pi/extensions/`, the global
            # directory, and anything a settings file names -- leaving the
            # explicit `-e` below as the only way in.
            "--mode json --no-extensions "
            f"-e {PI_MCP_BRIDGE} "
            "-p {prompt}",
            binary="pi-freeinference",
        )
        # One adapter instance per job (see get_runtime), so the suppression
        # in _from_message_update is per-run and cannot leak across jobs.
        self._reported_subtypes: set[str] = set()
        self.session_id: str | None = None
        self._preview = ReplyPreview(clock)
        # pi gives its messages no id of its own, and the provider's
        # ``responseId`` is absent from ``message_start``; counting messages
        # gives each text block a preview id that its ``text_end`` can name again.
        self._message_no = 0

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        session_state_dir: str | None = None,
        resume_session_id: str | None = None,
        activation_dir: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Add the model id the wrapper writes into pi's provider config."""
        argv, env = super().prepare(
            workdir=workdir,
            task_prompt=task_prompt,
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            # The relay endpoints are published below rather than by the base
            # class, because pi is the one runtime whose MCP client is not the
            # CLI: the bridge extension reads them, and handing a server set to
            # a CLI that cannot read it would be a silent no-op.
            mcp_config=EMPTY_RUNTIME_MCP_CONFIG,
            reasoning_effort=reasoning_effort,
        )
        self._accept_mcp(mcp_config, gateway_base_url=gateway_base_url, env=env)
        # models.json wants the model listed under the provider; the wrapper
        # cannot parse it back out of pi's argv without reimplementing pi's
        # option handling, so hand it over explicitly.
        env["PI_GATEWAY_MODEL"] = model
        # Provider config contains the short-lived turn credential. Keep it in
        # the activation tmpfs; only the session JSONL belongs in durable chat
        # state. The wrapper creates this directory before exec.
        env["PI_CODING_AGENT_DIR"] = activation_dir or "/run/agent-activation/pi"
        if session_state_dir:
            prompt_at = argv.index("-p")
            native_args = ["--session-dir", session_state_dir]
            if resume_session_id:
                native_args += ["--session", resume_session_id]
            argv[prompt_at:prompt_at] = native_args
        if self._check_effort(reasoning_effort) is not None:
            argv += ["--thinking", str(reasoning_effort)]
            # `--thinking` alone is a no-op: verified against 0.73.1, pi emits
            # no `reasoning_effort` at all unless the provider entry declares
            # the model as a reasoning model. The wrapper writes that entry, so
            # it has to be told — and only when an effort was asked for, since
            # declaring it unconditionally makes pi send `medium` on every turn
            # and Auto would stop meaning "as before".
            env["PI_GATEWAY_REASONING"] = "1"
        return argv, env

    def prepare_live(
        self,
        *,
        workdir: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        session_state_dir: str | None = None,
        resume_session_id: str | None = None,
        activation_dir: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Build pi 0.73.1's bidirectional JSONL RPC process."""
        argv, env = self.prepare(
            workdir=workdir,
            task_prompt="",
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
            session_state_dir=session_state_dir,
            resume_session_id=resume_session_id,
            activation_dir=activation_dir,
        )
        mode_at = argv.index("--mode") + 1
        argv[mode_at] = "rpc"
        prompt_at = argv.index("-p")
        del argv[prompt_at : prompt_at + 2]
        return argv, env

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Normalize one ``--mode json`` line (session format v3)."""
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return self._raw(line, "not json")
        if not isinstance(event, dict):
            return self._raw(line, "not an object")

        kind = event.get("type")
        if kind == "session":
            captured = str(event.get("id") or "").strip()
            if captured:
                self.session_id = captured
            return NormalizedEvent(
                LIFECYCLE,
                {
                    "phase": "init",
                    "session_id": self.session_id,
                    # pi's own version is not on the wire; this is the session
                    # format's, and naming it as such keeps it from being read
                    # as the CLI version the way Claude Code's line supplies.
                    "session_format": event.get("version"),
                },
            )
        if kind == "message_update":
            return self._from_message_update(event, line)
        if kind == "message_end":
            return self._from_message_end(event, line)
        if kind == "message_start":
            self._message_no += 1
            return None
        if kind == "agent_end":
            self._preview.clear()
            failure = self.agent_error(event)
            if failure is not None:
                return NormalizedEvent(ERROR, {"text": failure})
            return NormalizedEvent(LIFECYCLE, {"phase": "result"})
        if kind in self._SILENT_KINDS:
            return None
        return self._raw(line, f"unknown type {kind!r}")

    @staticmethod
    def agent_error(event: dict[str, Any]) -> str | None:
        """Read the final assistant outcome from Pi's native agent-end event."""
        messages = event.get("messages")
        if not isinstance(messages, list):
            return None
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "assistant":
                if message.get("stopReason") in {"error", "aborted"}:
                    return _truncate(message.get("errorMessage") or "Pi agent turn failed")
                return None
        return None

    def _from_message_update(self, event: dict[str, Any], line: str) -> NormalizedEvent | None:
        """Map a completed content block; drop the deltas that built it."""
        update = event.get("assistantMessageEvent")
        if not isinstance(update, dict):
            return self._raw(line, "message_update without an assistantMessageEvent")
        subtype = str(update.get("type") or "")
        if subtype == "text_delta":
            piece = self._preview.add(self._preview_id(update), str(update.get("delta") or ""))
            return NormalizedEvent(RAW, piece) if piece else None
        if subtype.endswith(("_start", "_delta")):
            return None
        if subtype not in self._BLOCK_END_KINDS:
            # Same treatment as an unclassified Claude Code subtype: keep the
            # first as a diagnostic so a new block kind is discoverable, drop
            # the repeats so it cannot flood the log at one row per token.
            if subtype in self._reported_subtypes:
                return None
            self._reported_subtypes.add(subtype)
            return self._raw(line, f"unclassified message_update subtype {subtype!r}")

        response = self._response_identity(event)
        if subtype == "thinking_end":
            return NormalizedEvent(THINKING, {"text": _truncate(update.get("content")), **response})
        if subtype == "text_end":
            # The one event the owner is actually waiting for: `message` is
            # what the control plane copies into agent_thread_messages, which
            # is both the visible reply and the next turn's context.
            preview_id = self._preview_id(update)
            self._preview.finish(preview_id)
            return NormalizedEvent(
                MESSAGE,
                {
                    "text": _truncate(update.get("content")),
                    "truncated": _clipped(update.get("content")),
                    "preview_id": preview_id,
                    **response,
                },
            )
        tool_call = update.get("toolCall")
        if not isinstance(tool_call, dict):
            return self._raw(line, "toolcall_end without a toolCall")
        return NormalizedEvent(
            TOOL_USE,
            {
                "name": tool_call.get("name"),
                "id": tool_call.get("id"),
                # Keyed `input` like every other adapter, because that is what
                # the UI reads to render the command.
                "input": tool_call.get("arguments"),
                **response,
            },
        )

    @staticmethod
    def _response_identity(event: dict[str, Any]) -> dict[str, str]:
        """Name the provider response a message belongs to, when pi recorded it.

        pi copies the first stream chunk's id into ``responseId`` and repeats
        the message on every ``message_update`` and ``message_end``. Against
        the gateway that is the chat-completion id it logs for the request, so
        ``usage_id`` joins a message to its measured request, as Claude Code's
        native ``message.id`` does.
        """
        message = event.get("message")
        response_id = message.get("responseId") if isinstance(message, dict) else None
        return {"usage_id": response_id} if isinstance(response_id, str) and response_id else {}

    def _preview_id(self, update: dict[str, Any]) -> str:
        return f"message-{self._message_no}:{update.get('contentIndex')}"

    def _from_message_end(self, event: dict[str, Any], line: str) -> NormalizedEvent | None:
        """Map the end of a message: a tool result, or a turn's token usage."""
        message = event.get("message")
        if not isinstance(message, dict):
            return self._raw(line, "message_end without a message")
        role = message.get("role")
        if role == "toolResult":
            return NormalizedEvent(
                TOOL_RESULT,
                {
                    "tool_use_id": message.get("toolCallId"),
                    "name": message.get("toolName"),
                    # The runner reads this to count failed calls, so a Tier 2
                    # pi job had none. `isError` appears here and nowhere else in the
                    # stream, which is why this is the authoritative copy.
                    "is_error": bool(message.get("isError")),
                    "content": _truncate(message.get("content")),
                },
            )
        if role == "assistant":
            usage = message.get("usage")
            if not isinstance(usage, dict):
                return None
            return NormalizedEvent(
                USAGE,
                {
                    "input_tokens": usage.get("input"),
                    "output_tokens": usage.get("output"),
                    "cache_read_tokens": usage.get("cacheRead"),
                    "cache_write_tokens": usage.get("cacheWrite"),
                    "total_tokens": usage.get("totalTokens"),
                    "model": message.get("model"),
                    "text": (f"{usage.get('input', 0)} in / {usage.get('output', 0)} out tokens"),
                    **self._response_identity(event),
                },
            )
        # role="user" is the prompt echoed back; the owner wrote it, and the
        # thread already has it.
        if role == "user":
            return None
        return self._raw(line, f"message_end with unknown role {role!r}")

    #: pi's own flag vocabulary, minus the two that do not survive the trip.
    #: Verified against 0.73.1 against a recording endpoint: `off` sends no
    #: parameter at all (which is Auto by another name, and Auto is already
    #: spelled by not choosing), and **`xhigh` arrives as `high`** — offering
    #: it would let someone pick a depth this runtime cannot actually ask for,
    #: which is the whole failure this control exists to end.
    EFFORT_LEVELS = ("minimal", "low", "medium", "high")

    def effort_args(self, effort: str | None) -> list[str]:
        """Rendered in :meth:`prepare`, because the flag is only half of it.

        pi needs the provider entry to declare the model as a reasoning model
        as well, and that is the wrapper's file rather than this argv — so both
        halves are set together where the environment is also being built.
        """
        self._check_effort(effort)
        return []

    def _accept_mcp(
        self, config: RuntimeMCPConfig, *, gateway_base_url: str, env: dict[str, str]
    ) -> None:
        """Publish the relay endpoints for the image's own bridge to read.

        Both halves hold, and both were checked against the pinned binary
        rather than assumed: `--no-extensions` leaves the explicit `-e` as the
        only loadable extension, so the bridge is the only MCP source, and the
        flag is passed whether or not the run has servers -- the bridge returns
        immediately on an empty map, which keeps the argv identical between a
        run with tools and one without, so an injected extension has nowhere to
        hide behind a turn that did not need one.
        """
        self._wrapper_mcp_env(config, gateway_base_url=gateway_base_url, env=env)

    def capabilities(self) -> RuntimeCapabilities:
        """Tier 1 events and no trusted cost attribution.

        pi reports a per-turn ``cost`` block, but it is computed from a price
        table the gateway does not populate — every field came back zero in
        the recorded runs. ``api_logs`` is the billing authority regardless,
        so this claims no cost report rather than surfacing a confident zero.
        Native resume is declared by :class:`drivers.PiDriver`, not by this
        event/cost capability row.
        """
        return RuntimeCapabilities(
            cost_report=False,
            normalized_events=True,
            tier=1,
            mcp=True,
            reasoning_efforts=self.EFFORT_LEVELS,
        )


class OpencodeRuntime(GenericRuntime):
    """Tier 2: OpenCode headless, streamed as raw JSON lines.

    Two verified facts shape the invocation. OpenCode ignores
    ``OPENAI_BASE_URL``, and its built-in ``openai`` provider speaks the
    Responses API; the ``opencode-freeinference`` wrapper instead declares a
    provider over the **bundled** ``@ai-sdk/openai-compatible`` package
    (chat-completions dialect, nothing downloaded at run time). And its
    startup fetch of the models.dev catalog hard-fails offline, so the
    wrapper disables it and declares the model in the config — without which
    every sandboxed run dies before the first request.

    ``--auto`` is the same lesson as Claude Code's bypassPermissions: the
    sandbox is the boundary, and an interactive permission gate inside it
    only guarantees the agent cannot do the work.
    """

    name = "opencode"

    def __init__(self) -> None:
        """Fix the wrapper invocation and remember OpenCode's session id."""
        super().__init__(
            "opencode-freeinference run --format json --auto -m freeinference/{model} {prompt}",
            binary="opencode-freeinference",
        )
        self.session_id: str | None = None

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        session_state_dir: str | None = None,
        resume_session_id: str | None = None,
        activation_dir: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Add the model id the wrapper declares in OpenCode's config."""
        argv, env = super().prepare(
            workdir=workdir,
            task_prompt=task_prompt,
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
        )
        env["OPENCODE_GATEWAY_MODEL"] = model
        env["OPENCODE_FREEINFERENCE_CONFIG_DIR"] = (
            activation_dir or "/run/agent-activation/opencode"
        )
        if session_state_dir:
            # OpenCode 1.18.9 stores sessions in its XDG data root. Relocating
            # every XDG root keeps all native continuation data inside the
            # Docker-owned /agent-state tree and out of the repository.
            env.update(
                {
                    "XDG_DATA_HOME": f"{session_state_dir}/opencode/data",
                    "XDG_CONFIG_HOME": f"{session_state_dir}/opencode/config",
                    "XDG_CACHE_HOME": f"{session_state_dir}/opencode/cache",
                    "XDG_STATE_HOME": f"{session_state_dir}/opencode/state",
                }
            )
            if resume_session_id:
                argv[-1:-1] = ["--session", resume_session_id]
        if self._check_effort(reasoning_effort) is not None:
            # No command-line flag exists for this: the level belongs in the
            # provider config the wrapper writes, so it travels as environment
            # rather than argv. Absent means the wrapper writes no option at
            # all, which is the request shape this runtime already sent.
            env["OPENCODE_GATEWAY_REASONING_EFFORT"] = str(reasoning_effort)
        return argv, env

    def prepare_live(
        self,
        *,
        workdir: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        session_state_dir: str | None = None,
        resume_session_id: str | None = None,
        activation_dir: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Launch the reviewed stdlib bridge around OpenCode 1.18.9 serve."""
        _argv, env = self.prepare(
            workdir=workdir,
            task_prompt="",
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
            session_state_dir=session_state_dir,
            resume_session_id=None,
            activation_dir=activation_dir,
        )
        if resume_session_id:
            env["AGENT_LIVE_SESSION_ID"] = resume_session_id
        env["AGENT_LIVE_WORKDIR"] = workdir
        return ["http-agent-live-bridge", "opencode"], env

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Keep Tier-2 output raw while capturing its native session locator."""
        stripped = line.strip()
        if not stripped:
            return None
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return self._raw(stripped, "not json")
        if isinstance(event, dict):
            captured = str(event.get("sessionID") or "").strip()
            if captured:
                self.session_id = captured
        return NormalizedEvent(RAW, {"text": stripped[:4000]})

    #: Passed through to the AI SDK provider verbatim, which does not validate
    #: it — same posture as codex. The real constraint is the intersection with
    #: the model's own declared domain. Verified against the pinned CLI: the
    #: config's ``options.reasoningEffort`` arrives as ``reasoning_effort`` in
    #: the chat-completions body, and nothing else in the config does.
    EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

    def _accept_mcp(
        self, config: RuntimeMCPConfig, *, gateway_base_url: str, env: dict[str, str]
    ) -> None:
        """Hand the relay endpoints to the reviewed wrapper.

        Both halves hold, and both are checked against the pinned binary rather
        than assumed: the wrapper writes the platform's own ``OPENCODE_CONFIG``,
        and it exports ``OPENCODE_DISABLE_PROJECT_CONFIG``, which is the flag
        that stops the repository's ``opencode.json`` and ``.opencode/`` from
        being merged afterwards. Without the second, the first would be one MCP
        source among several and a committed file could add its own servers.
        """
        self._wrapper_mcp_env(config, gateway_base_url=gateway_base_url, env=env)

    def capabilities(self) -> RuntimeCapabilities:
        """Tier 2 mechanics, but this one control is verified and wired."""
        return RuntimeCapabilities(tier=2, reasoning_efforts=self.EFFORT_LEVELS, mcp=True)


class KiloRuntime(GenericRuntime):
    """Tier 2: Kilo Code headless, streamed as raw JSON lines.

    Kilo's CLI is an OpenCode fork and keeps its invocation surface
    (``run --format json --auto -m provider/model``) and both of its gaps:
    ``OPENAI_BASE_URL`` is ignored, and startup fetches the models.dev
    catalog. The ``kilo-freeinference`` wrapper closes them the same way the
    OpenCode wrapper does — the fork renamed every kill switch to ``KILO_*``
    but kept the semantics (verified against the pinned binary).

    What the fork adds is more phone-home: PostHog telemetry, session ingest
    and share links to app.kilo.ai, presence, LSP downloads. Each has a
    supported kill switch, and the wrapper sets them all: the sandbox reaches
    the public web, so a path left enabled would simply phone home. Smoke-verified end to end against the pinned CLI: config
    injection, tool calls through the gateway, and JSON events on stdout.
    """

    name = "kilo"

    def __init__(self) -> None:
        """Fix the wrapper invocation and remember Kilo's session id."""
        super().__init__(
            "kilo-freeinference run --format json --auto -m freeinference/{model} {prompt}",
            binary="kilo-freeinference",
        )
        self.session_id: str | None = None

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        session_state_dir: str | None = None,
        resume_session_id: str | None = None,
        activation_dir: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Add the model id the wrapper declares in Kilo's config."""
        argv, env = super().prepare(
            workdir=workdir,
            task_prompt=task_prompt,
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
        )
        env["KILO_GATEWAY_MODEL"] = model
        env["KILO_FREEINFERENCE_CONFIG_DIR"] = activation_dir or "/run/agent-activation/kilo"
        if session_state_dir:
            env.update(
                {
                    "XDG_DATA_HOME": f"{session_state_dir}/kilo/data",
                    "XDG_CONFIG_HOME": f"{session_state_dir}/kilo/config",
                    "XDG_CACHE_HOME": f"{session_state_dir}/kilo/cache",
                    "XDG_STATE_HOME": f"{session_state_dir}/kilo/state",
                }
            )
            if resume_session_id:
                argv[-1:-1] = ["--session", resume_session_id]
        if self._check_effort(reasoning_effort) is not None:
            # No command-line flag exists for this: the level belongs in the
            # provider config the wrapper writes, so it travels as environment
            # rather than argv. Absent means the wrapper writes no option at
            # all, which is the request shape this runtime already sent.
            env["KILO_GATEWAY_REASONING_EFFORT"] = str(reasoning_effort)
        return argv, env

    def prepare_live(
        self,
        *,
        workdir: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        session_state_dir: str | None = None,
        resume_session_id: str | None = None,
        activation_dir: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Launch the reviewed stdlib bridge around Kilo 7.4.17 serve."""
        _argv, env = self.prepare(
            workdir=workdir,
            task_prompt="",
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
            session_state_dir=session_state_dir,
            resume_session_id=None,
            activation_dir=activation_dir,
        )
        if resume_session_id:
            env["AGENT_LIVE_SESSION_ID"] = resume_session_id
        env["AGENT_LIVE_WORKDIR"] = workdir
        return ["http-agent-live-bridge", "kilo"], env

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Keep Tier-2 output raw while capturing its native session locator."""
        stripped = line.strip()
        if not stripped:
            return None
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return self._raw(stripped, "not json")
        if isinstance(event, dict):
            captured = str(event.get("sessionID") or "").strip()
            if captured:
                self.session_id = captured
        return NormalizedEvent(RAW, {"text": stripped[:4000]})

    #: Passed through to the AI SDK provider verbatim, which does not validate
    #: it — same posture as codex. The real constraint is the intersection with
    #: the model's own declared domain. Verified against the pinned CLI: the
    #: config's ``options.reasoningEffort`` arrives as ``reasoning_effort`` in
    #: the chat-completions body, and nothing else in the config does.
    EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

    def _accept_mcp(
        self, config: RuntimeMCPConfig, *, gateway_base_url: str, env: dict[str, str]
    ) -> None:
        """Pass the relay to the image's Kilo build with project sources disabled."""
        self._wrapper_mcp_env(config, gateway_base_url=gateway_base_url, env=env)

    def capabilities(self) -> RuntimeCapabilities:
        """Kilo supports the same platform MCP surface as every other runtime."""
        return RuntimeCapabilities(tier=2, reasoning_efforts=self.EFFORT_LEVELS, mcp=True)


_REGISTRY: dict[str, type[AgentRuntime]] = {
    ClaudeCodeRuntime.name: ClaudeCodeRuntime,
    CodexRuntime.name: CodexRuntime,
    PiRuntime.name: PiRuntime,
    OpencodeRuntime.name: OpencodeRuntime,
    KiloRuntime.name: KiloRuntime,
}


def registered_runtimes() -> list[str]:
    """Runtime ids this deployment can actually run.

    The composer offers these rather than a hardcoded list, so the picker
    cannot advertise a runtime the backend would refuse at job creation.
    """
    return sorted(_REGISTRY)


def runtimes_supporting_mcp() -> list[str]:
    """Runtime ids that can be given MCP servers safely.

    Used by the composer and by job creation, so a job never reaches a sandbox
    having been promised tools its harness will not load.
    """
    return sorted(
        name for name, runtime_cls in _REGISTRY.items() if runtime_cls().capabilities().mcp
    )


def runtime_reasoning_efforts() -> dict[str, list[str]]:
    """Effort levels each registered runtime can express, in its own words.

    A runtime that cannot express one is absent rather than empty-listed, so
    the composer reaches "no control here" the same way whether the runtime
    cannot say it or the platform is talking to something too old to answer.

    Used by the composer and by job creation, so a turn is never created
    carrying a depth its harness has no flag for — the MCP argument applied to
    the other thing a runtime either can or cannot do.
    """
    efforts: dict[str, list[str]] = {}
    for name, runtime_cls in _REGISTRY.items():
        declared = runtime_cls().capabilities().reasoning_efforts
        if declared:
            efforts[name] = list(declared)
    return efforts


def get_runtime(name: str) -> AgentRuntime:
    """Return one of the five live-session runtime adapters.

    Unknown ids fail closed. A command template cannot opt a one-shot process
    into the retained topology because it cannot attest native resume, live
    transport, immediate fork, or concurrent shared-workspace behavior.
    """
    runtime_cls = _REGISTRY.get(name)
    if runtime_cls is not None:
        return runtime_cls()
    known = ", ".join(sorted(_REGISTRY))
    raise KeyError(f"Unknown agent runtime {name!r}. Known: {known}")


#: How many tool names one ``init`` event may republish. Claude Code ships a
#: menu of a few dozen and an MCP server adds more, so the bound sits far above
#: any real session. It exists because the alternative is unbounded: a CLI that
#: reported thousands would otherwise put the largest object in the stream on a
#: single lifecycle row. Overflow is reported rather than hidden — see
#: ``tools_truncated`` below.
MAX_INIT_TOOLS = 200


def _harness_inventory(event: dict[str, Any]) -> dict[str, Any] | None:
    """What an ``init`` event says the CLI started with, quoted not inferred.

    Claude Code composes its system prompt inside the CLI and never puts it on
    the wire, so there is no prompt here to republish. What the CLI does report
    is the configuration it actually started under: the tool menu it loaded, the
    MCP servers it reached, and the permission mode it ran in. The mode is the
    one a reader has a reason to check — the platform asks for
    ``bypassPermissions``, and the CLI's own copy of what it used is evidence
    the ask was honoured, which a flag in an argv is not.

    Returns ``None`` when the CLI reported none of it, so a lifecycle event
    carries an inventory only when there is one to carry.
    """
    tools, tools_truncated = _reported_tool_names(event.get("tools"))
    servers = _reported_mcp_servers(event.get("mcp_servers"))
    mode = event.get("permissionMode")
    permission_mode = mode.strip() if isinstance(mode, str) else ""
    if not tools and not servers and not permission_mode:
        return None
    inventory: dict[str, Any] = {}
    if tools:
        inventory["tools"] = tools
        if tools_truncated:
            inventory["tools_truncated"] = True
    if servers:
        inventory["mcp_servers"] = servers
    if permission_mode:
        inventory["permission_mode"] = permission_mode
    return inventory


def _reported_tool_names(value: Any) -> tuple[list[str], bool]:
    """The tool names an ``init`` event listed, in the order it listed them.

    Names are copied, never derived: a name the CLI did not send as a non-empty
    string names no tool this platform can vouch for. Repeats collapse because a
    menu is a set. The second value says whether the bound cut the list, so a
    short list is never mistaken for a short menu.
    """
    if not isinstance(value, list):
        return [], False
    seen: set[str] = set()
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names[:MAX_INIT_TOOLS], len(names) > MAX_INIT_TOOLS


def _reported_mcp_servers(value: Any) -> list[dict[str, Any]]:
    """The MCP servers an ``init`` event listed, with the status it gave each.

    A server the CLI named without a status keeps its name and reports no
    status. Which servers the platform meant to load is a different claim from
    which ones the CLI says it loaded, and only the second is evidence.
    """
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    servers: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        status = item.get("status")
        servers.append({"name": name, "status": status if isinstance(status, str) else None})
    return servers


def _truncate(value: Any, limit: int = 4000) -> str:
    """Render any payload fragment as bounded text."""
    return _payload_text(value)[:limit]


def _clipped(value: Any, limit: int = 4000) -> bool:
    """Whether ``_truncate`` would drop any of ``value``.

    A message that was clipped is not the model's whole answer, and the part
    that was dropped may be the part that matters — the goal report is at the
    *end* of a final message. The reader has to be able to tell that apart from
    a message that simply stopped, so the fact travels with the text.
    """
    return len(_payload_text(value)) > limit


def _payload_text(value: Any) -> str:
    """Render transcript content without silently losing its tail."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return value
