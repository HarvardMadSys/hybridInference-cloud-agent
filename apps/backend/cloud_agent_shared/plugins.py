"""Validated plugin selections shared by admission and sandbox initialization."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Sequence

MAX_SANDBOX_PLUGINS = 8
PLUGIN_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*"
CODEX_PLUGIN_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9_-]*"
GITHUB_MARKETPLACE_PATTERN = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
NPM_PLUGIN_PATTERN = (
    r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*"
    r"(?:@(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)?"
)


def supported_plugin_runtimes() -> list[str]:
    """Return runtimes with an explicit sandbox plugin installation path."""
    return ["claude-code", "codex", "pi", "opencode", "kilo"]


class SandboxPlugin(BaseModel):
    """One native marketplace plugin or npm plugin package for a chat."""

    model_config = ConfigDict(extra="forbid", strict=True)

    plugin: str = Field(min_length=1, max_length=128)
    marketplace: str | None = Field(None, min_length=3, max_length=512)

    @field_validator("plugin")
    @classmethod
    def validate_plugin(cls, value: str) -> str:
        """Reject CLI options and control characters before runtime validation."""
        value = value.strip()
        if (
            not value
            or value.startswith("-")
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in value
            )
        ):
            raise ValueError("Plugin cannot be empty, a CLI option, or contain whitespace.")
        return value

    @field_validator("marketplace")
    @classmethod
    def validate_marketplace(cls, value: str | None) -> str | None:
        """Allow GitHub repositories and credential-free public HTTPS sources."""
        if value is None:
            return None
        value = value.strip()
        if re.fullmatch(GITHUB_MARKETPLACE_PATTERN, value) is not None:
            return value
        if any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        ):
            raise ValueError("Marketplace source cannot contain whitespace or control characters.")
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.port in (None, 443)
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "Marketplace must be a GitHub owner/repo or an HTTPS URL without credentials, query, or fragment."
            )
        return value


def validate_sandbox_plugins(
    runtime: str, selections: Sequence[SandboxPlugin | dict[str, Any]]
) -> list[dict[str, str]]:
    """Validate and normalize selections for the runtime that will load them."""
    if not selections:
        return []
    if runtime not in supported_plugin_runtimes():
        raise ValueError(f"Plugins are not supported for runtime {runtime!r}.")
    if len(selections) > MAX_SANDBOX_PLUGINS:
        raise ValueError(f"Select at most {MAX_SANDBOX_PLUGINS} plugins.")
    normalized: list[dict[str, str]] = []
    names: set[str] = set()
    sources: dict[str, str] = {}
    for selection in selections:
        selected = SandboxPlugin.model_validate(selection)
        name = selected.plugin
        if runtime in {"claude-code", "codex"}:
            pattern = CODEX_PLUGIN_PATTERN if runtime == "codex" else PLUGIN_PATTERN
            if re.fullmatch(pattern, name) is None or not selected.marketplace:
                raise ValueError(
                    "Marketplace plugins require name@marketplace and a marketplace source."
                )
            entry = {"plugin": name, "marketplace": selected.marketplace}
            marketplace_name = name.rsplit("@", 1)[1]
            if marketplace_name in sources and sources[marketplace_name] != selected.marketplace:
                raise ValueError("Plugins from the same marketplace must use the same source.")
            sources[marketplace_name] = selected.marketplace
        else:
            name = name.removeprefix("npm:")
            if selected.marketplace is not None or re.fullmatch(NPM_PLUGIN_PATTERN, name) is None:
                raise ValueError(
                    "This runtime requires an npm package, optionally with an exact version, and no marketplace."
                )
            entry = {"plugin": name}
        identity = (
            name
            if runtime in {"claude-code", "codex"}
            else name.rsplit("@", 1)[0]
            if "@" in name[1:]
            else name
        )
        if identity in names:
            raise ValueError(f"Plugin {name!r} is selected more than once.")
        names.add(identity)
        normalized.append(entry)
    return normalized
