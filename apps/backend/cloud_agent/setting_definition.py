"""One administrator setting's definition and the rules its values must meet.

Kept apart from the registry in :mod:`cloud_agent.settings`, which lists every
setting and owns the stored snapshot, and re-exported from it.
"""

from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import SplitResult, urlsplit

from pydantic import AnyHttpUrl, ValidationError

from cloud_agent_shared.s3_bucket_name import is_s3_bucket_name
from cloud_agent_shared.sandbox_manager_compat import STORED_SETTING_KEYS

if TYPE_CHECKING:
    from collections.abc import Callable

SettingValue = str | int | float | bool
SettingKind = Literal["string", "boolean", "integer", "number"]
#: When a saved value reaches the code that uses it. ``unverified`` is the
#: default and makes no promise; every other mode names a checked reader.
ApplyMode = Literal["next_operation", "new_session", "restart", "host_reenroll", "unverified"]
APPLY_MODES: tuple[ApplyMode, ...] = (
    "next_operation",
    "new_session",
    "restart",
    "host_reenroll",
    "unverified",
)


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
    #: A URL the code that uses it requires to be HTTPS, with no query.
    https_only: bool = False
    #: Where the control plane sends its gateway dispatch token, fetches the
    #: keys that identity tokens are checked against, or sends a browser to
    #: sign in. A new value must use HTTPS unless it names this machine or a
    #: private name (see :func:`_private_host`). A value saved or imported
    #: before this rule still loads, so an internal plain-HTTP gateway keeps
    #: working.
    https_unless_private: bool = False
    #: Also takes ``mailto:`` and one address: a support address is as likely
    #: as a support page. One plain address only: header fields would let the
    #: link prefill a message the user did not write.
    mailto: bool = False
    #: This setting's own rule for its text, after its length, choices and URL
    #: shape. It is given every such value, so it says whether empty is valid.
    check: Callable[[str], bool] | None = None
    #: What a valid value looks like, added to the message that refuses one.
    hint: str = ""
    #: When a saved value takes effect, checked against its readers.
    apply_mode: ApplyMode = "unverified"
    #: Which component must restart (``apply_mode="restart"``), or what else
    #: the administrator should know about when the value is used.
    apply_description: str = ""
    #: The reader this setting replaced used its default for a missing,
    #: unparsable or out-of-range environment value. The import keeps doing
    #: that rather than stopping the control plane on an old value.
    invalid_env_uses_default: bool = False
    #: Written only by a rotation workflow (the previous key it keeps for
    #: verifying or decrypting, which key builds lookup digests). The settings
    #: page shows it read-only and an administrator's update cannot set it:
    #: a hand-set value would strand data the workflow has not re-encrypted.
    workflow_only: bool = False

    @property
    def storage_key(self) -> str:
        """Keep existing saved secrets readable across the service rename."""
        return STORED_SETTING_KEYS.get(self.key, self.key)

    def validate(self, value: Any, *, saved: bool = False) -> SettingValue:
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
            valid = (
                isinstance(value, str)
                and "\x00" not in value
                and len(value) <= (131072 if self.secret else 4096)
                and (not self.choices or value in self.choices)
                and (not self.url or not value or self._accepts_link(value, saved=saved))
                and (self.check is None or self.check(value))
            )
        if not valid:
            hint = f". {self.hint}" if self.hint else ""
            raise ValueError(f"Invalid value for {self.label} ({self.key}){hint}")
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

    def _accepts_link(self, value: str, *, saved: bool) -> bool:
        """Whether a value that is set is a link this setting may hold."""
        if self.mailto and value[:7].lower() == "mailto:":
            return bool(_CONTACT_MAILTO.fullmatch(value))
        parsed = _web_url(value)
        return (
            parsed is not None
            and (not self.https_only or (parsed.scheme == "https" and not parsed.query))
            and (
                not self.https_unless_private
                or saved
                or parsed.scheme == "https"
                or _private_host(parsed.hostname or "")
            )
        )

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
        result["apply_mode"] = self.apply_mode
        if self.workflow_only:
            result["workflow_only"] = True
        if self.apply_description:
            result["apply_description"] = self.apply_description
        return result


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


def _web_url(value: str) -> SplitResult | None:
    """``value`` parsed, if it is an absolute HTTP(S) URL a browser can use as written."""
    if any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        return None
    try:
        # The browser uses a WHATWG URL parser for saved appearance links.
        # urlsplit alone accepts malformed hosts (such as percent signs or
        # spaces) that would break the root layout.
        AnyHttpUrl(value)
        parsed = urlsplit(value)
        _ = parsed.port
    except (ValueError, ValidationError):
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return None
    return parsed


def _issuer(value: str) -> bool:
    """Exactly what the Gateway signs: no surrounding space or control characters."""
    return value == value.strip() and not any(ord(char) < 32 for char in value)


def _grant_base_path(value: str) -> bool:
    """A path prefix the grant clients accept.

    They check it during startup as well as on live requests, so an unusable
    prefix is refused before it can be saved.
    """
    return (
        value.rstrip("/").startswith("/")
        and not value.startswith("//")
        and not any(char in value for char in ("?", "#", "\\"))
        and not any(char.isspace() or ord(char) < 32 for char in value)
    )


def _s3_bucket(value: str) -> bool:
    """Empty, or a name S3 accepts for a general purpose bucket."""
    return not value or is_s3_bucket_name(value)


def _s3_region(value: str) -> bool:
    """Empty, or the shape of an AWS region name."""
    return not value or bool(re.fullmatch(r"[a-z0-9-]{1,64}", value))


def _s3_prefix(value: str) -> bool:
    """Empty, or an object key prefix of plain, non-relative path segments."""
    return not value or (
        len(value.encode()) <= 512
        and bool(re.fullmatch(r"[a-zA-Z0-9_./-]+", value))
        and all(part and part not in {".", ".."} for part in value.split("/"))
    )


# The code that uses these two refuses anything else, so the editor asks it
# rather than keeping a second copy of the rule.
def _github_install_url(value: str) -> bool:
    from cloud_agent.source_control import github_install_url_is_usable

    return not value or github_install_url_is_usable(value)


def _gitlab_redirect_uri(value: str) -> bool:
    from cloud_agent.source_control import gitlab_redirect_uri_is_usable

    return not value or gitlab_redirect_uri_is_usable(value)


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
    apply_mode: ApplyMode = "unverified",
    apply_description: str = "",
    invalid_env_uses_default: bool = False,
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
        apply_mode=apply_mode,
        apply_description=apply_description,
        invalid_env_uses_default=invalid_env_uses_default,
    )


#: RFC 7518 §3.2's floor for an HS256 key, which the session key has always
#: been held to. Below it a signing key is brute-forceable offline from any one
#: value it signed, and every sandbox holds a worker token the control signing
#: key signed; a bearer secret that short is guessable outright.
MINIMUM_CREDENTIAL_BYTES = 32
