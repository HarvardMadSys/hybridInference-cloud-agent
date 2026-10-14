"""Runtime contracts, MCP configuration, and safe invocation helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .events import RAW, NormalizedEvent

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
