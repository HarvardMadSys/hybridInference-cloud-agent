"""One administrator setting's definition and the rules its values must meet.

Kept apart from the registry in :mod:`cloud_agent.settings`, which lists every
setting and owns the stored snapshot, and re-exported from it.
"""

from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, ValidationError

from cloud_agent_shared.sandbox_manager_compat import STORED_SETTING_KEYS

SettingValue = str | int | float | bool
SettingKind = Literal["string", "boolean", "integer", "number"]


class SettingTooShort(ValueError):
    """A value shorter than its setting's minimum length; otherwise acceptable."""


class SettingsRevisionConflict(Exception):
    """Settings were saved after the revision an update was reviewed against."""


_CONTACT_MAILTO = re.compile(
    r"mailto:[A-Za-z0-9.!#$%&'*+/=^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Setting:
    """An admin-editable value and its public validation rules."""

    key: str
    label: str
    category: str
    default: SettingValue
    kind: SettingKind = "string"
    env: str | None = None
    description: str = "Applies to subsequent operations after settings synchronize."
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    secret: bool = False
    url: bool = False
    minimum_bytes: int = 0
    #: The floor arrived after deployments had already saved this value. A
    #: shorter saved or imported value still loads, and startup warns about
    #: it: refusing it would stop the control plane before the settings page
    #: that replaces it could open. Every new value is held to the floor.
    minimum_bytes_new_values_only: bool = False
    #: An empty environment value is imported as empty instead of meaning
    #: "unset". Only for a text setting whose empty value is itself a choice;
    #: its compose entry then defaults with ``${NAME-value}``, never ``:-``.
    empty_env_imports: bool = False

    @property
    def storage_key(self) -> str:
        """Keep existing saved secrets readable across the service rename."""
        return STORED_SETTING_KEYS.get(self.key, self.key)

    def validate(self, value: Any, *, saved: bool = False) -> SettingValue:  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
        """Return a checked value, raising a message that contains no supplied data.

        ``saved`` marks a value already stored or imported from the
        environment rather than one an administrator is submitting now.
        """
        valid = True
        if self.kind == "boolean":
            valid = isinstance(value, bool)
        elif self.kind in {"number", "integer"}:
            try:
                valid = (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and (self.kind != "integer" or isinstance(value, int))
                )
            except OverflowError:
                valid = False
            if valid:
                valid = (self.minimum is None or value >= self.minimum) and (
                    self.maximum is None or value <= self.maximum
                )
        else:
            valid = isinstance(value, str) and "\x00" not in value
            if valid:
                valid = len(value) <= (131072 if self.secret else 4096)
            if valid and self.key == "gateway_issuer" and value:
                valid = value == value.strip() and not any(ord(char) < 32 for char in value)
            if valid and self.choices:
                valid = value in self.choices
            if valid and self.key == "gateway_grant_base_path":
                # Grant clients validate this during startup as well as live
                # requests. Reject an unusable prefix before it can be saved.
                valid = (
                    value.rstrip("/").startswith("/")
                    and not value.startswith("//")
                    and not any(char in value for char in ("?", "#", "\\"))
                    and not any(char.isspace() or ord(char) < 32 for char in value)
                )
            if valid and value and self.key == "sandbox_archive.bucket":
                valid = bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value)) and (
                    not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value)
                    and not any(part in value for part in ("..", ".-", "-."))
                    and not value.startswith(("xn--", "sthree-", "amzn-s3-demo-"))
                    and not value.endswith(("-s3alias", "--ol-s3", ".mrap", "--x-s3", "--table-s3"))
                )
            if valid and value and self.key == "sandbox_archive.region":
                valid = bool(re.fullmatch(r"[a-z0-9-]{1,64}", value))
            if valid and value and self.key == "sandbox_archive.prefix":
                valid = (
                    len(value.encode()) <= 512
                    and bool(re.fullmatch(r"[a-zA-Z0-9_./-]+", value))
                    and all(part and part not in {".", ".."} for part in value.split("/"))
                )
            if (
                valid
                and value
                and self.key == "site.contact_url"
                and value[:7].lower() == "mailto:"
            ):
                # A support address is as likely as a support page. One plain
                # address only: header fields would let the link prefill a
                # message the user did not write.
                valid = bool(_CONTACT_MAILTO.fullmatch(value))
            elif valid and self.url and value:
                try:
                    if any(
                        char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
                        for char in value
                    ):
                        raise ValueError("A URL cannot contain whitespace or control characters")
                    # The browser uses a WHATWG URL parser for saved appearance
                    # links. urlsplit alone accepts malformed hosts (such as
                    # percent signs or spaces) that would break the root layout.
                    AnyHttpUrl(value)
                    parsed = urlsplit(value)
                    valid = (
                        parsed.scheme in {"http", "https"}
                        and bool(parsed.hostname)
                        and not parsed.username
                        and not parsed.password
                        and not parsed.fragment
                    )
                    _ = parsed.port
                    if self.key in {
                        "compute.cloudlab_api_url",
                        "compute.registration_url",
                        "mcp_registry_url",
                        "sandbox_archive.endpoint",
                    }:
                        valid = valid and parsed.scheme == "https" and not parsed.query
                    if self.key in _GATEWAY_TRUST_URLS and not saved:
                        valid = valid and (
                            parsed.scheme == "https" or _private_host(parsed.hostname or "")
                        )
                    # The code that uses these refuses anything else, so the
                    # editor asks it rather than keeping a second copy of the rule.
                    if self.key == "github_app_install_url":
                        from cloud_agent.source_control import github_install_url_is_usable

                        valid = valid and github_install_url_is_usable(value)
                    elif self.key == "gitlab_redirect_uri":
                        from cloud_agent.source_control import gitlab_redirect_uri_is_usable

                        valid = valid and gitlab_redirect_uri_is_usable(value)
                except (ValueError, ValidationError):
                    valid = False
        if not valid:
            if self.key == "sandbox_archive.bucket":
                raise ValueError(
                    f"Invalid value for {self.label} ({self.key}). "
                    "Use a bucket name such as freeinference; set cloud-agent/sandbox "
                    "in Sandbox archive S3 prefix."
                )
            raise ValueError(f"Invalid value for {self.label} ({self.key})")
        # Last, and its own error: a stored key saved before its minimum
        # existed is still the key everything was signed or encrypted with.
        if (
            isinstance(value, str)
            and value
            and len(value.encode()) < self.minimum_bytes
            and not (saved and self.minimum_bytes_new_values_only)
        ):
            raise SettingTooShort(
                f"Invalid value for {self.label} ({self.key}): "
                f"use at least {self.minimum_bytes} bytes"
            )
        return value

    def definition(self) -> dict[str, Any]:
        """The public editor definition, without environment or credential values."""
        result: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "kind": self.kind,
            "default": self.default,
            "secret": self.secret,
        }
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        if self.choices:
            result["choices"] = list(self.choices)
        if self.minimum_bytes:
            result["minimum_bytes"] = self.minimum_bytes
        return result


#: Where the control plane sends its gateway dispatch token, fetches the keys
#: that identity tokens are checked against, and sends a browser to sign in.
#: A new value must use HTTPS unless it names this machine or a private name
#: (see :func:`_private_host`). A value saved or imported before this rule
#: still loads, so an internal plain-HTTP gateway keeps working.
_GATEWAY_TRUST_URLS = frozenset({"gateway_base_url", "gateway_jwks_url", "gateway_authorize_url"})


def _private_host(host: str) -> bool:
    """Whether plain HTTP to ``host`` stays on this machine or a private network.

    Loopback addresses and ``localhost`` names, and a single-label name such
    as a Compose service (``local-gateway``), which public DNS cannot answer.
    """
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return bool(host) and "." not in host


def _numeric(
    key: str,
    label: str,
    category: str,
    default: int | float,
    minimum: float,
    maximum: float,
    *,
    env: str | None = None,
    description: str = "Applies to subsequent operations after settings synchronize.",
) -> Setting:
    return Setting(
        key,
        label,
        category,
        default,
        kind="integer" if isinstance(default, int) else "number",
        env=env,
        minimum=minimum,
        maximum=maximum,
        description=description,
    )


#: RFC 7518 §3.2's floor for an HS256 key, which the session key has always
#: been held to. Below it a signing key is brute-forceable offline from any one
#: value it signed, and every sandbox holds a worker token the control signing
#: key signed; a bearer secret that short is guessable outright.
MINIMUM_CREDENTIAL_BYTES = 32
