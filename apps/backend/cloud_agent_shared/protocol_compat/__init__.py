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
#: Every variable the launcher consumes; none reaches the CLI.
LAUNCH_ENV = (ENV_CLIENT, ENV_UPSTREAM_URL, ENV_UPSTREAM_KEY, ENV_MODEL, ENV_TOKEN)

__all__ = [
    "ANTHROPIC_MESSAGES",
    "CHAT_COMPLETIONS",
    "CLIENTS",
    "ENV_CLIENT",
    "ENV_MODEL",
    "ENV_TOKEN",
    "ENV_UPSTREAM_KEY",
    "ENV_UPSTREAM_URL",
    "LAUNCH_ENV",
    "ORIGIN_PLACEHOLDER",
    "RESPONSES",
    "TOKEN_PLACEHOLDER",
]
