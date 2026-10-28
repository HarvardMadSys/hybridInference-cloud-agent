"""Owner model connections: the vocabulary both images agree on.

An owner may add their own inference endpoint -- a Base URL, an API key, the
protocols it speaks (OpenAI Chat Completions or Responses, Anthropic Messages)
and the model ids it serves -- and run their own tasks on it instead of the
Gateway. The key is theirs and so is the bill. The control plane stores
the connection; a host only ever sees one inside the claim of a turn its owner
started on it, and hands it to that turn's member process.

Which wire protocol each runtime speaks is fixed by its adapter, not by the
connection: OpenCode, Kilo and pi are configured with Chat Completions, Codex
with Responses, and Claude Code with Anthropic Messages. A runtime uses its
own protocol directly when the connection offers it. Otherwise Codex and
Claude Code can use Chat Completions through the member's private translator.

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
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: A host advertises this when its runner and sandbox manager can launch a
#: member on an owner connection: the exact Base URL instead of the host-local
#: Gateway relay, and the connection's identity in the process-fixed launch.
#: A host without it must never claim such a turn, or it would run the turn's
#: model id against the Gateway.
MODEL_CONNECTION_CAPABILITY = "model-connections-v1"
#: A host advertises this when it can also run a *translated* plan: install
#: the protocol compatibility helper and launch the member's CLI behind it. A
#: host with only ``model-connections-v1`` gets such a claim back.
MODEL_PROTOCOL_COMPAT_CAPABILITY = "model-protocol-translation-v1"
#: What a host running this release advertises for owner connections.
HOST_MODEL_CONNECTION_CAPABILITIES = (MODEL_CONNECTION_CAPABILITY, MODEL_PROTOCOL_COMPAT_CAPABILITY)

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

#: The two translated plans: a client protocol, run on a connection that
#: offers only the upstream protocol named here. No other pair is translated.
COMPAT_UPSTREAMS: dict[str, str] = {
    RESPONSES: CHAT_COMPLETIONS,
    ANTHROPIC_MESSAGES: CHAT_COMPLETIONS,
}
DIRECT = "direct"
TRANSLATED = "translated"

CONNECTION_ID = re.compile(r"mconn_[0-9a-f]{32}")
#: A translated member's loopback token: an HMAC-SHA256 digest, in hex.
LOCAL_TOKEN = re.compile(r"[0-9a-f]{64}")
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


@dataclass(frozen=True)
class ProtocolPlan:
    """How one runtime reaches one connection.

    ``client_protocol`` is what the runtime's CLI speaks; ``upstream_protocol``
    is what the connection offers and the request finally goes out as. They
    differ only on a translated plan. The connection's declared protocols are
    the upstream's own and are never rewritten to match the client.
    """

    client_protocol: str
    upstream_protocol: str

    @property
    def translated(self) -> bool:
        """Whether the member's compatibility helper sits between the two."""
        return self.client_protocol != self.upstream_protocol

    @property
    def mode(self) -> str:
        """``direct`` or ``translated``."""
        return TRANSLATED if self.translated else DIRECT


def protocol_plan(runtime: str, protocols: list[str] | tuple[str, ...]) -> ProtocolPlan | None:
    """The one plan for ``runtime`` on a connection offering ``protocols``, or ``None``.

    Native first: a runtime whose own protocol the connection offers always
    runs on it directly. Only then, and only for Responses (Codex) and
    Anthropic Messages (Claude Code), a connection offering Chat Completions
    is used through the compatibility helper. Every path that asks whether a
    turn can run -- admission, follow-ups, waiting messages, goal rounds,
    claims, residencies, Fork, the composer and the connection check -- asks
    this, so they cannot disagree.
    """
    client = runtime_protocol(runtime)
    if client is None:
        return None
    if client in protocols:
        return ProtocolPlan(client, client)
    upstream = COMPAT_UPSTREAMS.get(client)
    if upstream is not None and upstream in protocols:
        return ProtocolPlan(client, upstream)
    return None


def runtime_plans(protocols: list[str] | tuple[str, ...]) -> dict[str, ProtocolPlan]:
    """Every runtime that can run on a connection offering ``protocols``, with its plan."""
    plans = {runtime: protocol_plan(runtime, protocols) for runtime in sorted(RUNTIME_PROTOCOLS)}
    return {runtime: plan for runtime, plan in plans.items() if plan is not None}


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
    #: What the connection's URL speaks: the upstream protocol of the plan.
    protocol: str
    api_key: str = Field(..., repr=False)
    #: What the member's CLI speaks, when the plan translates; empty is direct,
    #: and then left out, so a direct claim is what it was before translation.
    client_protocol: str = Field("", exclude_if=lambda value: not value)
    #: On a translated plan, the bearer this member's CLI presents to its own
    #: loopback translator instead of the key. The control plane derives it
    #: with its own platform key for this member, connection revision, plan
    #: and model, so a sibling holding the same provider key cannot compute
    #: it. A secret like the key: masked in output, never in a repr.
    local_token: str = Field("", repr=False, exclude_if=lambda value: not value)

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

    @model_validator(mode="after")
    def _translated_pair(self) -> ModelConnectionAccess:
        # Only the two pairs the helper implements; anything else is direct.
        if self.client_protocol and COMPAT_UPSTREAMS.get(self.client_protocol) != self.protocol:
            raise ValueError("unsupported translated model connection plan")
        if bool(self.client_protocol) != bool(self.local_token):
            raise ValueError("a translated plan, and only one, carries its member's token")
        if self.local_token and not LOCAL_TOKEN.fullmatch(self.local_token):
            raise ValueError("a translated plan needs a valid member token")
        return self

    @property
    def plan(self) -> ProtocolPlan:
        """The plan this claim was resolved with."""
        return ProtocolPlan(self.client_protocol or self.protocol, self.protocol)

    @property
    def source(self) -> str:
        """The process-fixed identity: which connection, at which credential revision."""
        return f"{self.connection_id}@{self.revision}"

    def launch_fields(self) -> dict[str, str]:
        """The process-fixed launch fields a sandbox manager starts a member with.

        Everything but the key, which travels as the launch's own credential.
        A translated plan adds the CLI's protocol and the member's token; a
        direct one is unchanged.
        """
        fields = {
            "inference_base_url": self.base_url,
            "inference_protocol": self.protocol,
            "inference_source": self.source,
        }
        if self.plan.translated:
            fields["inference_client_protocol"] = self.client_protocol
            fields["inference_local_token"] = self.local_token
        return fields

    def secrets(self) -> dict[str, str]:
        """The key, and on a translated plan the member's token, for the output redactor."""
        values = {f"model-connection:{self.connection_id}": self.api_key}
        if self.local_token:
            values[f"model-connection-translator:{self.connection_id}"] = self.local_token
        return values


__all__ = [
    "ANTHROPIC_MESSAGES",
    "ANTHROPIC_VERSION",
    "CHAT_COMPLETIONS",
    "COMPAT_UPSTREAMS",
    "CONNECTION_ID",
    "DIRECT",
    "HOST_MODEL_CONNECTION_CAPABILITIES",
    "LOCAL_TOKEN",
    "MAX_API_KEY_CHARS",
    "MAX_BASE_URL_CHARS",
    "MAX_MODELS",
    "MODEL_CONNECTION_CAPABILITY",
    "MODEL_ID",
    "MODEL_PROTOCOL_COMPAT_CAPABILITY",
    "PROTOCOLS",
    "RESPONSES",
    "RUNTIME_PROTOCOLS",
    "TRANSLATED",
    "ModelConnectionAccess",
    "ProtocolPlan",
    "anthropic_base_url",
    "anthropic_url",
    "protocol_plan",
    "runtime_plans",
    "runtime_protocol",
    "valid_api_key",
]
