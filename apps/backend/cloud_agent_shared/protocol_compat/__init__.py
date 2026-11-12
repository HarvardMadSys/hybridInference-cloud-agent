"""Run a Responses or Anthropic Messages client on a Chat Completions upstream.

A private compatibility layer for an owner's own model connection: Codex
(Responses) or Claude Code (Anthropic Messages) on a connection that offers
only Chat Completions. This package is the pure part -- the request gate, the
translators and the stream guard -- shared by the member's loopback service
(``cloud_agent_host.protocol_translator``, beside its CLI in the sandbox) and
the control plane's connection check. Standard library only, Python 3.11 or
later: the sandbox image's interpreter runs it from a zipapp the host
installs.

The translation kernels are vendored from HybridInference (``NOTICE.md``).
"""

from __future__ import annotations

#: The two client protocols translated, each onto Chat Completions.
RESPONSES = "responses"
ANTHROPIC_MESSAGES = "anthropic_messages"
CHAT_COMPLETIONS = "chat_completions"
CLIENTS: tuple[str, ...] = (RESPONSES, ANTHROPIC_MESSAGES)
#: The launcher's other mode: a direct plan's CLI, its requests forwarded
#: unchanged and measured (``cloud_agent_host.model_passthrough``).
PASSTHROUGH = "passthrough"

#: Where the launcher writes the loopback origin into the CLI's argv and env.
ORIGIN_PLACEHOLDER = "@@MODEL_PROTOCOL_COMPAT_ORIGIN@@"
#: Where the launcher writes the member's token into the CLI's environment;
#: never accepted in its argv. The control plane issues the token
#: (``ModelConnectionAccess.local_token``).
TOKEN_PLACEHOLDER = "@@MODEL_PROTOCOL_COMPAT_TOKEN@@"

#: What a host hands the launcher (``cloud_agent_host.protocol_translator``)
#: in the CLI's environment.
ENV_CLIENT = "AGENT_COMPAT_CLIENT"
ENV_UPSTREAM_URL = "AGENT_COMPAT_UPSTREAM_URL"
ENV_UPSTREAM_KEY = "AGENT_COMPAT_UPSTREAM_KEY"
ENV_MODEL = "AGENT_COMPAT_MODEL"
#: The token this member's CLI must present, issued with the claim.
ENV_TOKEN = "AGENT_COMPAT_TOKEN"
#: The reasoning level the turn chose, validated by the control plane against
#: what the upstream declares; absent or empty is Auto. A changed level starts
#: a new member process, and with it a new helper.
ENV_REASONING_EFFORT = "AGENT_COMPAT_REASONING_EFFORT"
#: Set to ``1`` when the upstream is the host's own model relay on the group
#: bridge, for a member on a connection the deployment provides. Only then is
#: a plain ``http`` upstream accepted: the relay is reached on the group's own
#: bridge, and what the member holds is a placeholder, never a provider key.
ENV_UPSTREAM_RELAY = "AGENT_COMPAT_UPSTREAM_RELAY"
#: Every variable the launcher consumes; none reaches the CLI.
LAUNCH_ENV = (
    ENV_CLIENT,
    ENV_UPSTREAM_URL,
    ENV_UPSTREAM_KEY,
    ENV_MODEL,
    ENV_TOKEN,
    ENV_REASONING_EFFORT,
    ENV_UPSTREAM_RELAY,
)


def passthrough_authority(host: str, port: int | None) -> str:
    """How a passthrough's one upstream is named in its CLI's URL: ``host[:port]``.

    The host builds the CLI's URL with it and the launcher checks every
    request's path against it, so the two cannot spell one origin two ways.
    """
    host = host.lower()
    return host if port in (None, 443) else f"{host}:{port}"


def upstream_port(scheme: str, port: int | None) -> int:
    """The port an upstream URL means: its own, or its scheme's default.

    Both sides resolve it here, so an ``http`` relay written without a port
    is named ``host:80`` by the host and by the launcher alike.
    """
    return port or (80 if scheme == "http" else 443)


def upstream_scheme(relay: str, scheme: str) -> str:
    """The scheme a launch's upstream may use, or an empty string for one it may not.

    ``https`` always. ``http`` only for the host's model relay, which the
    launch says with :data:`ENV_UPSTREAM_RELAY`; and a relay is never
    ``https``, so the flag cannot be put on a provider's URL to no effect.
    """
    expected = "http" if relay == "1" else "https"
    return scheme if scheme == expected and relay in ("", "1") else ""


__all__ = [
    "ANTHROPIC_MESSAGES",
    "CHAT_COMPLETIONS",
    "CLIENTS",
    "ENV_CLIENT",
    "ENV_MODEL",
    "ENV_REASONING_EFFORT",
    "ENV_TOKEN",
    "ENV_UPSTREAM_KEY",
    "ENV_UPSTREAM_RELAY",
    "ENV_UPSTREAM_URL",
    "LAUNCH_ENV",
    "ORIGIN_PLACEHOLDER",
    "PASSTHROUGH",
    "RESPONSES",
    "TOKEN_PLACEHOLDER",
    "passthrough_authority",
    "upstream_port",
    "upstream_scheme",
]
