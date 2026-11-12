"""Models a deployment provides: allowances, the relay's route list and the owner tag.

Pure rules, no I/O. A *provided* (operator) connection has the fields an
owner's connection has, so the same predicate decides which agents run which
model (``domain.model_connections.require_usable``). What is its own lives
here:

**Allowances.** Each owner may send each provided model a number of requests
per UTC day. The model has a default; an owner may have a number of their own,
which replaces it. ``None`` is unlimited and ``0`` withholds the model. A
model with no default set is offered to nobody.

**The relay's routes.** The model relay forwards exactly the inference and
listing paths an agent CLI calls under the connection's Base URL, matched
whole, and nothing else under that prefix.

See ``docs/agents/specs/2026-11-12-platform-model-relay.zh-CN.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from cloud_agent.domain.model_connections import ModelConnectionInvalid
from cloud_agent_shared.model_connections import anthropic_base_url

#: The domain the stored key of a provided connection is encrypted under, and
#: its members' translator tokens are derived under, from the settings root key.
OPERATOR_CONNECTION_KEY_DOMAIN = b"cloud-agent/operator-model-connection-key/v1"
#: What an owner's tag is a MAC of, ahead of the account id.
END_USER_TAG_DOMAIN = b"cloud-agent/operator-model-connection/end-user/v1"
END_USER_TAG_CHARS = 24

DEFAULT_USER_CONCURRENCY = 4

_HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,62}")
#: Headers the relay writes or strips itself; an owner tag may not be one.
_RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "host",
        "content-length",
        "content-type",
        "connection",
        "transfer-encoding",
        "cookie",
        "anthropic-version",
        "anthropic-beta",
    }
)


class OperatorLimitsMissing(Exception):
    """A connection cannot be enabled while a model of its has no default allowance."""

    def __init__(self, models: list[str]) -> None:
        """Keep the models that still need one."""
        super().__init__("models without a default allowance")
        self.models = models


@dataclass(frozen=True)
class Allowance:
    """One owner's allowance on one provided model, and today's use."""

    #: Requests per UTC day; ``None`` is unlimited, ``0`` is not offered.
    limit: int | None
    used: int = 0

    @property
    def offered(self) -> bool:
        """Whether the owner may use the model at all."""
        return self.limit is None or self.limit > 0

    @property
    def exhausted(self) -> bool:
        """Whether today's requests are used up."""
        return self.limit is not None and self.used >= self.limit


#: A model with no default set: offered to nobody.
NOT_OFFERED = Allowance(limit=0)


def effective_allowance(
    default: dict[str, Any] | None, own: dict[str, Any] | None, *, used: int = 0
) -> Allowance:
    """The allowance an owner has on a model.

    ``default`` is the model's limit row and ``own`` the owner's, each ``None``
    when there is none. Without a default the model is not offered, whatever
    the owner's row says: a model is opened by setting its default, to ``0``
    when it is for named owners only.
    """
    if default is None:
        return NOT_OFFERED
    if own is not None:
        return Allowance(limit=own.get("daily_requests"), used=used)
    return Allowance(limit=default.get("user_daily_requests"), used=used)


def utc_day(now: datetime | None = None) -> datetime:
    """Midnight UTC of the day ``now`` is in."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def next_reset(now: datetime | None = None) -> datetime:
    """When today's counts start over: the next midnight, UTC."""
    return utc_day(now) + timedelta(days=1)


def clean_end_user_header(value: str | None) -> str | None:
    """The header a relayed request names its owner in; ``None`` sends none."""
    name = (value or "").strip()
    if not name:
        return None
    if not _HEADER_NAME.fullmatch(name) or name.lower() in _RESERVED_HEADERS:
        raise ModelConnectionInvalid(
            "An end-user header is a plain header name such as X-End-User, "
            "and not one the relay sets itself."
        )
    return name


def clean_default_model(value: str | None, models: list[str]) -> str | None:
    """The model a new owner starts on: one of ``models``, or none."""
    model = (value or "").strip()
    if not model:
        return None
    if model not in models:
        raise ModelConnectionInvalid("The default model must be one of the listed models.")
    return model


def end_user_tag(secret: str, user_id: str) -> str:
    """An opaque, stable tag for one account, as a provider may attribute requests by.

    A keyed digest: not the account id, its email or anything derived from
    them without the deployment's key, and the same for every request of the
    account, so an administrator can compute it and find the account's
    requests at the provider.
    """
    if not secret or not user_id:
        raise ValueError("an end-user tag needs the deployment's key and an account")
    key = hashlib.sha256(END_USER_TAG_DOMAIN + b"\0" + secret.encode()).digest()
    return hmac.new(key, user_id.encode(), hashlib.sha256).hexdigest()[:END_USER_TAG_CHARS]


@dataclass(frozen=True)
class RelayRoute:
    """One path the model relay forwards."""

    #: Whether the request is an inference call: its body names the model, and
    #: it counts against the owner's allowance.
    inference: bool
    #: Whether a refusal is shaped as Anthropic's error rather than OpenAI's.
    anthropic: bool


def relay_route(base_url: str, method: str, raw_path: str) -> RelayRoute | None:
    """The route for ``method`` on ``raw_path`` under a connection, or ``None``.

    ``raw_path`` is the path exactly as the client sent it, undecoded and
    without its query. It must equal one of the listed paths byte for byte, so
    an escaped or dot-segment spelling of another path matches nothing. A
    prefix match would also open whatever else the provider serves under the
    same prefix, such as stored responses.
    """
    openai = urlsplit(base_url).path.rstrip("/")
    anthropic = urlsplit(anthropic_base_url(base_url)).path.rstrip("/")
    routes: dict[tuple[str, str], RelayRoute] = {
        ("POST", f"{openai}/chat/completions"): RelayRoute(inference=True, anthropic=False),
        ("POST", f"{openai}/responses"): RelayRoute(inference=True, anthropic=False),
        ("GET", f"{openai}/models"): RelayRoute(inference=False, anthropic=False),
        ("POST", f"{anthropic}/v1/messages"): RelayRoute(inference=True, anthropic=True),
        ("POST", f"{anthropic}/v1/messages/count_tokens"): RelayRoute(
            inference=False, anthropic=True
        ),
    }
    # Claude Code and an OpenAI client both list ``…/v1/models``; where the two
    # spellings are one path, the OpenAI shape already listed above stands.
    routes.setdefault(("GET", f"{anthropic}/v1/models"), RelayRoute(False, anthropic=True))
    return routes.get((method.upper(), raw_path))


def is_messages_path(raw_path: str) -> bool:
    """Whether a path the relay refuses outright looks like an Anthropic call."""
    return "/messages" in raw_path


__all__ = [
    "DEFAULT_USER_CONCURRENCY",
    "END_USER_TAG_CHARS",
    "END_USER_TAG_DOMAIN",
    "NOT_OFFERED",
    "OPERATOR_CONNECTION_KEY_DOMAIN",
    "Allowance",
    "OperatorLimitsMissing",
    "RelayRoute",
    "clean_default_model",
    "clean_end_user_header",
    "effective_allowance",
    "end_user_tag",
    "is_messages_path",
    "next_reset",
    "relay_route",
    "utc_day",
]
