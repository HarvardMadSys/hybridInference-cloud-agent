"""Turning a pasted MCP configuration into connection drafts, without running it.

People already have MCP servers configured in half a dozen clients, and asking
them to retype a URL and a token is how a generic feature ends up used by
nobody. So the platform accepts the configuration they already have. What it
must not do is *execute* any of it: an imported ``command`` is a proposal that
becomes a launch plan only after the owner has seen what it would run, where,
and with which secrets.

Three dialects are recognised, because they are genuinely different and a
parser that assumes one produces confident nonsense on the others:

``mcpServers`` (Claude Code, Claude Desktop, Cursor)
    ``command`` / ``args`` / ``env`` for stdio, or ``url`` with an optional
    ``type`` of ``http`` / ``sse`` for remote.
``servers`` (VS Code ``.vscode/mcp.json``)
    The same two shapes under a different key, with ``type`` required and
    ``inputs`` declaring values the editor prompts for.
``mcp_servers`` (Codex ``config.toml``)
    TOML, where a remote server names its transport by carrying a ``url``.

The output is a :class:`ImportDraft` per server, carrying what the user must
still decide: the run location, whether a detected secret is kept, and which
local paths the server would be given. Nothing here talks to a network, spawns
a process, or reads a file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import tomllib

from cloud_agent.mcp.models import (
    AuthKind,
    HeaderAuth,
    LaunchSpec,
    Mount,
    Transport,
)

#: Where an imported entry says it should run. Imported configuration never
#: chooses a location on its own: a ``command`` from somebody's laptop means
#: "their machine", and reading that as the control plane's own filesystem is
#: the mistake the design calls out by name.
DraftLocation = Literal["remote", "managed-worker", "user-connector"]

_SECRET_NAME = re.compile(
    r"authorization|authentication|\bauth\b|token|secret|key|password|passwd|credential"
    r"|(?:^|[^A-Za-z0-9])pat(?:$|[^A-Za-z0-9])|cookie",
    re.I,
)
_ENV_REFERENCE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")
_PATH_LIKE = re.compile(r"^(/|~[/\\]|[A-Za-z]:[\\/])")
_PATH_FLAGS = frozenset({"--directory", "--cwd", "--root", "--workspace", "--path"})

#: Header schemes a pasted ``Authorization`` value may carry. Anything else is
#: kept as one opaque value, because guessing the split would corrupt it.
_AUTH_SCHEMES = frozenset({"bearer", "token", "basic", "apikey", "key"})

#: Command runners whose value is an installable package. Recognising them is
#: what lets the platform pin a version instead of downloading ``latest`` on
#: every start.
_PACKAGE_RUNNERS: Mapping[str, str] = {
    "npx": "npm",
    "npm": "npm",
    "pnpm": "npm",
    "yarn": "npm",
    "bunx": "npm",
    "uvx": "pypi",
    "pipx": "pypi",
    "docker": "oci",
}


class ImportError_(ValueError):
    """Raised when pasted configuration cannot be read as an MCP config."""


@dataclass(frozen=True)
class ImportSecret:
    """One value the imported configuration carries, or expects the user to give.

    ``value`` is present when the paste contained a literal, and ``None`` when
    the configuration referenced something the client would have supplied --
    ``${input:token}`` from VS Code, ``${env:TOKEN}`` from Codex. Both cases are
    reported to the user before anything is stored, because "we imported six
    secrets from your clipboard" is not something to discover afterwards.
    """

    name: str
    target: Literal["env", "header"]
    value: str | None = None
    #: True when the value came from the pasted text rather than a prompt.
    literal: bool = False

    def masked(self) -> str:
        """A display form that never contains the whole secret."""
        if self.value is None:
            return "(not provided)"
        if len(self.value) <= 4:
            return "*" * len(self.value)
        return f"{self.value[:2]}{'*' * 6}{self.value[-2:]}"


@dataclass(frozen=True)
class ImportDraft:
    """One server the user pasted, as a proposal they must still confirm."""

    label: str
    transport: Transport
    location: DraftLocation
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    launch: LaunchSpec | None = None
    auth_kind: AuthKind = AuthKind.NONE
    header_auth: HeaderAuth = field(default_factory=HeaderAuth)
    secrets: tuple[ImportSecret, ...] = ()
    #: Local paths the server's arguments name. On a managed worker these are
    #: mounts to approve; on a connector they are paths on the user's machine.
    paths: tuple[str, ...] = ()
    #: The command runner, when the entry starts a package. Kept so the launch
    #: plan can pin a version rather than resolving ``latest`` every start.
    package_runner: str | None = None
    package_ecosystem: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def needs_a_secret(self) -> bool:
        """Whether the user still has to supply a value before this can work."""
        return any(secret.value is None for secret in self.secrets)

    def summary(self) -> dict[str, Any]:
        """Render the draft for the confirmation step.

        Secrets are masked and the launch is shown as an argument vector rather
        than a shell string, so what the user approves is what will run.
        """
        return {
            "label": self.label,
            "transport": self.transport.value,
            "location": self.location,
            "url": self.url,
            "argv": list(self.launch.argv()) if self.launch else [],
            "env_names": sorted(self.launch.env) if self.launch else [],
            "headers": sorted(self.headers),
            "secrets": [
                {"name": secret.name, "target": secret.target, "value": secret.masked()}
                for secret in self.secrets
            ],
            "paths": list(self.paths),
            "package": (
                {"runner": self.package_runner, "ecosystem": self.package_ecosystem}
                if self.package_runner
                else None
            ),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ImportResult:
    """Every draft a paste produced, plus what could not be read."""

    drafts: tuple[ImportDraft, ...] = ()
    #: Entries that were present but could not be turned into a draft, each
    #: with the reason. Reported rather than raised: one bad server in a
    #: five-server paste should not lose the other four.
    rejected: tuple[tuple[str, str], ...] = ()
    dialect: str = "unknown"


def parse_import(text: str, *, format_hint: str | None = None) -> ImportResult:
    """Parse pasted configuration in any recognised dialect.

    ``format_hint`` may be ``"json"`` or ``"toml"``; without it the text is
    tried as JSON first, then TOML, which is enough to tell the dialects apart
    because a Codex config is never valid JSON and a JSON config is never valid
    TOML.
    """
    payload: Any
    kind = (format_hint or "").lower()
    errors: list[str] = []
    for candidate in _candidate_formats(kind):
        try:
            payload = json.loads(text) if candidate == "json" else tomllib.loads(text)
        except (json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        return _from_mapping(payload, dialect=candidate)
    raise ImportError_(
        "that is neither a JSON nor a TOML MCP configuration (" + "; ".join(errors) + ")"
    )


def _candidate_formats(kind: str) -> tuple[str, ...]:
    """Which parsers to try, in order, for a format hint."""
    if kind in ("json", "jsonc"):
        return ("json",)
    if kind in ("toml",):
        return ("toml",)
    return ("json", "toml")


def _from_mapping(payload: Any, *, dialect: str) -> ImportResult:
    """Build drafts from a decoded configuration document."""
    if not isinstance(payload, Mapping):
        raise ImportError_("an MCP configuration must be an object at the top level")
    servers = _find_servers(payload)
    if servers is None:
        raise ImportError_(
            "no MCP servers found: expected an 'mcpServers', 'servers' or 'mcp_servers' key"
        )
    entries, declared_inputs = servers
    drafts: list[ImportDraft] = []
    rejected: list[tuple[str, str]] = []
    for name, raw in entries.items():
        if not isinstance(raw, Mapping):
            rejected.append((str(name), "the entry is not an object"))
            continue
        try:
            drafts.append(_draft(str(name), raw, declared_inputs=declared_inputs))
        except ImportError_ as exc:
            rejected.append((str(name), str(exc)))
    return ImportResult(drafts=tuple(drafts), rejected=tuple(rejected), dialect=dialect)


def _find_servers(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """Locate the server map and any declared inputs, whatever the dialect.

    All three keys are checked rather than the first match, because a Codex
    config that also contains ``mcpServers`` under a profile section should not
    be read as the top-level server list.
    """
    inputs: Mapping[str, Any] = {}
    if isinstance(payload.get("inputs"), list):
        inputs = {
            str(item.get("id")): item
            for item in payload["inputs"]
            if isinstance(item, Mapping) and item.get("id")
        }
    for key in ("mcpServers", "mcp_servers", "servers"):
        value = payload.get(key)
        if isinstance(value, Mapping) and value:
            # A server entry is an object; a profile table is a table of
            # objects. One level of nesting is unwrapped so that
            # ``mcp_servers.<profile>.<name>`` is still readable.
            if all(isinstance(item, Mapping) for item in value.values()) and not any(
                _looks_like_server(item) for item in value.values()
            ):
                nested: dict[str, Any] = {}
                for group in value.values():
                    for name, entry in group.items():
                        nested[str(name)] = entry
                return nested, inputs
            return value, inputs
    return None


def _looks_like_server(candidate: Mapping[str, Any]) -> bool:
    """Whether an object has any of the keys a server entry uses."""
    return any(key in candidate for key in ("command", "url", "type", "transport", "serverUrl"))


def _draft(
    name: str,
    entry: Mapping[str, Any],
    *,
    declared_inputs: Mapping[str, Any],
) -> ImportDraft:
    """Build one draft, refusing an entry that names no transport at all."""
    warnings: list[str] = []
    url = _first_string(entry, "url", "serverUrl", "endpoint", "httpUrl")
    command = _first_string(entry, "command")
    declared_type = (_first_string(entry, "type", "transport") or "").lower()

    if url:
        transport = Transport.HTTP_SSE if "sse" in declared_type else Transport.STREAMABLE_HTTP
        if declared_type in ("stdio", "command"):
            raise ImportError_("the entry declares stdio but gives a URL")
        headers, secrets, auth_kind, header_auth, header_warnings = _headers(
            name, entry, declared_inputs
        )
        warnings.extend(header_warnings)
        if not headers and any(key in entry for key in ("headers",)):
            warnings.append("the entry declares headers but none could be read")
        return ImportDraft(
            label=name,
            transport=transport,
            location="remote",
            url=url,
            headers=headers,
            auth_kind=auth_kind,
            header_auth=header_auth,
            secrets=secrets,
            warnings=tuple(warnings),
        )

    if command:
        if declared_type in ("http", "sse", "streamable-http") and not url:
            # The entry says HTTP and gives a command. Guessing which half is
            # right would run the wrong thing, so it is reported instead.
            raise ImportError_(f"the entry declares {declared_type!r} but gives no url to reach")
        args = tuple(_string_list(entry.get("args"), "args"))
        env, env_secrets, env_warnings = _env(entry, declared_inputs)
        warnings.extend(env_warnings)
        paths = _paths(args)
        if paths:
            warnings.append(
                "this server's arguments name local paths; choose where it runs before saving"
            )
        runner = command.rsplit("/", 1)[-1].lower()
        ecosystem = _PACKAGE_RUNNERS.get(runner)
        if ecosystem:
            warnings.append(
                f"{runner} resolves a package at start; the platform pins the version it installs"
            )
        return ImportDraft(
            label=name,
            transport=Transport.STDIO,
            # Never inferred as the control plane's own host: an imported
            # command is the user's, so it starts on their connector until
            # they say otherwise.
            location="user-connector",
            launch=LaunchSpec(command=command, args=args, env=env),
            auth_kind=AuthKind.STATIC if env_secrets else AuthKind.NONE,
            secrets=env_secrets,
            paths=paths,
            package_runner=runner if ecosystem else None,
            package_ecosystem=ecosystem,
            warnings=tuple(warnings),
        )

    raise ImportError_("the entry names neither a URL nor a command")


def _first_string(entry: Mapping[str, Any], *keys: str) -> str | None:
    """The first non-empty string among ``keys``."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _string_list(raw: Any, field_name: str) -> list[str]:
    """Coerce a JSON array of scalars into strings, refusing other shapes."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, Sequence):
        raise ImportError_(f"{field_name} must be a list")
    values: list[str] = []
    for item in raw:
        if isinstance(item, (str, int, float)):
            values.append(str(item))
        else:
            raise ImportError_(f"{field_name} must contain only strings")
    return values


def _headers(
    name: str,
    entry: Mapping[str, Any],
    declared_inputs: Mapping[str, Any],
) -> tuple[dict[str, str], tuple[ImportSecret, ...], AuthKind, HeaderAuth, list[str]]:
    """Read an HTTP entry's headers, separating secrets from ordinary values."""
    raw = entry.get("headers")
    warnings: list[str] = []
    headers: dict[str, str] = {}
    secrets: list[ImportSecret] = []
    if isinstance(raw, Mapping):
        for header_name, value in raw.items():
            text = _resolve_value(str(value), declared_inputs, warnings)
            if text is None:
                secrets.append(ImportSecret(name=str(header_name), target="header", value=None))
                continue
            if _is_secret_name(str(header_name)):
                secrets.append(
                    ImportSecret(name=str(header_name), target="header", value=text, literal=True)
                )
            headers[str(header_name)] = text
    elif raw is not None:
        raise ImportError_("headers must be an object")
    if not secrets:
        return headers, (), AuthKind.NONE, HeaderAuth(), warnings

    header_name = secrets[0].name
    scheme = ""
    if header_name.lower() == "authorization" and secrets[0].value:
        # ``Bearer ghp_x`` is split so the scheme is configuration and the
        # value is the secret. Storing the pair as one blob would make the
        # header impossible to rebuild after a refresh, and would leave the
        # scheme looking like part of the credential.
        prefix, _, remainder = secrets[0].value.partition(" ")
        if remainder and prefix.lower() in _AUTH_SCHEMES:
            scheme = prefix
            headers[header_name] = remainder
            secrets = [
                ImportSecret(name=header_name, target="header", value=remainder, literal=True)
            ]
    return (
        headers,
        tuple(secrets),
        AuthKind.STATIC,
        HeaderAuth(header_name=header_name, scheme=scheme),
        warnings,
    )


def _env(
    entry: Mapping[str, Any],
    declared_inputs: Mapping[str, Any],
) -> tuple[dict[str, str], tuple[ImportSecret, ...], list[str]]:
    """Read a stdio entry's environment, turning values into secret references."""
    raw = entry.get("env")
    warnings: list[str] = []
    env: dict[str, str] = {}
    secrets: list[ImportSecret] = []
    if raw is None:
        return env, (), warnings
    if not isinstance(raw, Mapping):
        raise ImportError_("env must be an object")
    for key, value in raw.items():
        text = _resolve_value(str(value), declared_inputs, warnings)
        if text is None:
            secrets.append(ImportSecret(name=str(key), target="env", value=None))
            env[str(key)] = f"secret://{key}"
            continue
        if _is_secret_name(str(key)):
            secrets.append(ImportSecret(name=str(key), target="env", value=text, literal=True))
            # The value never lands in the launch spec: the spec carries a
            # reference the worker resolves, so an imported literal is stored
            # encrypted rather than copied into a config document.
            env[str(key)] = f"secret://{key}"
        else:
            env[str(key)] = text
    return env, tuple(secrets), warnings


def _resolve_value(
    raw: str,
    declared_inputs: Mapping[str, Any],
    warnings: list[str],
) -> str | None:
    """Resolve a ``${...}`` reference, or report that the user must supply it."""
    match = _ENV_REFERENCE.match(raw.strip())
    if not match:
        return raw
    reference = match.group(1)
    if reference in declared_inputs:
        declared = declared_inputs[reference]
        if isinstance(declared.get("value"), str):
            return str(declared["value"])
        warnings.append(f"{reference} is a value this configuration expected you to enter")
        return None
    warnings.append(f"{reference} is read from the environment; provide it here")
    return None


def _is_secret_name(name: str) -> bool:
    """Whether a header or variable name reads as carrying a secret."""
    return bool(_SECRET_NAME.search(name))


def _paths(args: Iterable[str]) -> tuple[str, ...]:
    """Local paths an argument vector names, for the mount decision."""
    found: list[str] = []
    pending_flag = False
    for arg in args:
        if pending_flag:
            if _PATH_LIKE.match(arg):
                found.append(arg)
            pending_flag = False
            continue
        if arg in _PATH_FLAGS:
            pending_flag = True
            continue
        for flag in _PATH_FLAGS:
            prefix = f"{flag}="
            if arg.startswith(prefix) and _PATH_LIKE.match(arg[len(prefix) :]):
                found.append(arg[len(prefix) :])
        if _PATH_LIKE.match(arg):
            found.append(arg)
    seen: dict[str, None] = {}
    for path in found:
        seen.setdefault(path, None)
    return tuple(seen)


def mounts_for(paths: Sequence[str], *, target_root: str = "/workspace/mcp") -> tuple[Mount, ...]:
    """Turn approved paths into read-only mounts for a managed worker."""
    mounts: list[Mount] = []
    for index, path in enumerate(paths):
        mounts.append(
            Mount(
                source=path,
                target=f"{target_root.rstrip('/')}/{index}",
                read_only=True,
            )
        )
    return tuple(mounts)


__all__ = [
    "ImportDraft",
    "ImportError_",
    "ImportResult",
    "ImportSecret",
    "mounts_for",
    "parse_import",
]
