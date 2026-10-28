"""Owner model connections: the vocabulary both images agree on.

An owner may add their own inference endpoint -- a Base URL, an API key, the
protocols it speaks (OpenAI Chat Completions or Responses, Anthropic Messages)
and the model ids it serves -- and run their own tasks on it instead of the
Gateway. The key is theirs and so is the bill. The control plane stores
the connection; a host only ever sees one inside the claim of a turn its owner
started on it, and hands it to that turn's member process.

Which wire protocol each runtime speaks is fixed by its adapter, not by the
connection: OpenCode, Kilo and pi are configured with Chat Completions, Codex
with Responses, and Claude Code with Anthropic Messages. A runtime runs on a
connection only when the connection declares the protocol that runtime speaks;
nothing translates between them.

**Where each protocol's requests go.** An OpenAI-style CLI appends its own
path (``/chat/completions``, ``/responses``) to the Base URL exactly as
written, so a provider's ``/v1`` or ``/api/v1`` prefix is part of it. Claude
Code's SDK instead appends ``/v1/messages`` to ``ANTHROPIC_BASE_URL``: given
``https://host/v1`` it would call ``/v1/v1/messages``. So for Anthropic
Messages one trailing ``/v1`` -- and nothing else -- is dropped before the CLI
is told the URL, and the check posts to the same ``/v1/messages`` the CLI
will (``anthropic_base_url``). ``https://host``, ``https://host/v1``,
``https://host/anthropic`` and ``https://host/anthropic/v1`` therefore all
work, and one Base URL ending in ``/v1`` serves all three protocols.

See ``docs/agents/specs/2026-10-27-user-model-connections.zh-CN.md``.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: A host advertises this when its runner and sandbox manager can launch a
#: member on an owner connection: the exact Base URL instead of the host-local
#: Gateway relay, and the connection's identity in the process-fixed launch.
#: A host without it must never claim such a turn, or it would run the turn's
#: model id against the Gateway.
MODEL_CONNECTION_CAPABILITY = "model-connections-v1"

CHAT_COMPLETIONS = "chat_completions"
RESPONSES = "responses"
ANTHROPIC_MESSAGES = "anthropic_messages"
PROTOCOLS: tuple[str, ...] = (CHAT_COMPLETIONS, RESPONSES, ANTHROPIC_MESSAGES)
#: The Messages API version every Anthropic request names, the CLI's included.
ANTHROPIC_VERSION = "2023-06-01"

#: The protocol each runtime's adapter puts on the wire. A runtime missing
#: here speaks none a connection offers, so it cannot run on one.
RUNTIME_PROTOCOLS: dict[str, str] = {
    "opencode": CHAT_COMPLETIONS,
    "kilo": CHAT_COMPLETIONS,
    "pi": CHAT_COMPLETIONS,
    "codex": RESPONSES,
    "claude-code": ANTHROPIC_MESSAGES,
}

CONNECTION_ID = re.compile(r"mconn_[0-9a-f]{32}")
#: A model id as the upstream spells it. Provider ids carry slashes, colons,
#: dots and at-signs (``openai/gpt-4.1``, ``models/gemini-2.5-pro``,
#: ``claude-sonnet@2025``); nothing that could leave a config value.
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
MAX_MODELS = 64
#: Longer than any provider key seen in practice, short enough to bound every
#: config file and environment it is written into.
MAX_API_KEY_CHARS = 4096
MAX_BASE_URL_CHARS = 512


def runtime_protocol(runtime: str) -> str | None:
    """The protocol ``runtime`` speaks to a connection, or ``None`` for none."""
    return RUNTIME_PROTOCOLS.get(runtime)


def anthropic_base_url(base_url: str) -> str:
    """What ``ANTHROPIC_BASE_URL`` is set to for a connection's Base URL.

    The SDK appends ``/v1/messages`` (and ``/v1/models``), so exactly one
    trailing ``/v1`` is dropped; any other path, such as a gateway's
    ``/anthropic`` prefix, is kept as written. Expects a cleaned Base URL,
    which never ends in ``/``.
    """
    return base_url.removesuffix("/v1")


def anthropic_url(base_url: str, path: str) -> str:
    """The URL the CLI calls for ``path`` (``/v1/messages``, ``/v1/models``)."""
    return anthropic_base_url(base_url) + path


def valid_api_key(value: str) -> bool:
    """Whether ``value`` is a key a header, a config file and an env var all carry intact."""
    return 0 < len(value) <= MAX_API_KEY_CHARS and all(33 <= ord(ch) <= 126 for ch in value)


class ModelConnectionAccess(BaseModel):
    """One connection, as the claim of a turn running on it hands it to a host.

    ``revision`` is the connection's credential revision. It is part of the
    member process's fixed launch, so a turn after the key, Base URL or
    protocols changed starts a new process rather than borrowing one that holds
    the old key. ``api_key`` is the owner's own secret; it is masked in every
    durable output of the turn and written only into the member's private
    environment and activation tmpfs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: str = Field(..., max_length=64)
    revision: int = Field(..., ge=1)
    base_url: str = Field(..., min_length=8, max_length=MAX_BASE_URL_CHARS)
    protocol: str
    api_key: str = Field(..., repr=False)

    @field_validator("connection_id")
    @classmethod
    def _connection_id(cls, value: str) -> str:
        if not CONNECTION_ID.fullmatch(value):
            raise ValueError("invalid model connection id")
        return value

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        # Validated in full by the control plane; here only what a config file
        # and an argument-free environment need to carry it safely.
        if not value.startswith("https://") or any(ord(ch) <= 32 or ord(ch) > 126 for ch in value):
            raise ValueError("a model connection's Base URL must be a plain https URL")
        return value

    @field_validator("protocol")
    @classmethod
    def _protocol(cls, value: str) -> str:
        if value not in PROTOCOLS:
            raise ValueError("unknown model connection protocol")
        return value

    @field_validator("api_key")
    @classmethod
    def _api_key(cls, value: str) -> str:
        if not valid_api_key(value):
            raise ValueError("a model connection's API key must be printable ASCII")
        return value

    @property
    def source(self) -> str:
        """The process-fixed identity: which connection, at which credential revision."""
        return f"{self.connection_id}@{self.revision}"

    def launch_fields(self) -> dict[str, str]:
        """The process-fixed launch fields a sandbox manager starts a member with.

        Everything but the key, which travels as the launch's own credential.
        """
        return {
            "inference_base_url": self.base_url,
            "inference_protocol": self.protocol,
            "inference_source": self.source,
        }

    def secrets(self) -> dict[str, str]:
        """The key by name, for the output redactor."""
        return {f"model-connection:{self.connection_id}": self.api_key}


__all__ = [
    "ANTHROPIC_MESSAGES",
    "ANTHROPIC_VERSION",
    "CHAT_COMPLETIONS",
    "CONNECTION_ID",
    "MAX_API_KEY_CHARS",
    "MAX_BASE_URL_CHARS",
    "MAX_MODELS",
    "MODEL_CONNECTION_CAPABILITY",
    "MODEL_ID",
    "PROTOCOLS",
    "RESPONSES",
    "RUNTIME_PROTOCOLS",
    "ModelConnectionAccess",
    "anthropic_base_url",
    "anthropic_url",
    "runtime_protocol",
    "valid_api_key",
]
