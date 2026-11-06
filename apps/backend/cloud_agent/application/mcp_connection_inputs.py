"""What an owner may save as an MCP connection, checked before anything is written.

A connection arrives three ways -- added by hand, imported from a pasted
configuration, or edited -- and each passes the same checks here: a transport
and run location the platform can carry, an address the egress policy admits
or a command that builds a launch plan, a header the platform would send, and
credentials encrypted before the row that holds them exists. Nothing here
writes a connection; the routes do, with what these checks rendered.

Secrets are write-only. A static credential is reduced to a two-and-two hint
before it is stored, and a program's keys (its launch's ``secret://NAME``
values) are sealed under the MCP key domain; a new key too short to be masked
is refused (``sandbox_servers.short_key_refusal``).

Refusals are ``Refused`` and ``Unavailable`` in the words the routes always
answered with; the API maps them onto HTTP (``cloud_agent.api.errors``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from cloud_agent.crypto.credential_cipher import CredentialCipherError
from cloud_agent.domain.errors import Refused, Unavailable
from cloud_agent.mcp import config_import, launch, sandbox_servers as sandbox
from cloud_agent.mcp.launch import LaunchPlanError, ManagedLaunchPolicy, build_launch_plan
from cloud_agent.mcp.models import (
    AuthKind,
    ExecutionLocation,
    HeaderAuth,
    LaunchSpec,
    Mount,
    Transport,
)
from cloud_agent.mcp.net_policy import EndpointRejected, policy_from_environment, validate_endpoint
from cloud_agent.mcp.registry import mcp_cipher

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cloud_agent.ports import McpConnectionRepository


def _invalid(message: str) -> Refused:
    """A request whose input cannot be turned into a connection."""
    return Refused("invalid", "invalid_request_error", message)


def _no_encryption_key() -> Unavailable:
    """The deployment cannot hold a credential at all."""
    return Unavailable("server_error", "this deployment has no MCP encryption key configured")


def parse_transport(value: str) -> Transport:
    """Read a transport name, refusing one this platform cannot carry."""
    try:
        return Transport(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in Transport)
        raise _invalid(f"unknown transport {value!r}; use one of: {allowed}") from exc


def parse_location(value: str) -> ExecutionLocation:
    """Read an execution location name."""
    try:
        return ExecutionLocation(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ExecutionLocation)
        raise _invalid(f"unknown run location {value!r}; use one of: {allowed}") from exc


def parse_auth_kind(value: str) -> AuthKind:
    """Read an authentication kind."""
    try:
        return AuthKind(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in AuthKind)
        raise _invalid(f"unknown auth kind {value!r}; use one of: {allowed}") from exc


def build_auth(
    auth_kind: str, header_name: str | None, header_scheme: str | None
) -> tuple[AuthKind, dict[str, Any]]:
    """Validate an authentication choice and render its non-secret settings."""
    kind = parse_auth_kind(auth_kind)
    if kind is AuthKind.STATIC:
        name = (header_name or "Authorization").strip()
        scheme = (
            header_scheme
            if header_scheme is not None
            else ("Bearer" if name.lower() == "authorization" else "")
        ).strip()
        # Validated here rather than at dispatch time, so a header the platform
        # would refuse is reported while the user is still on the form.
        try:
            HeaderAuth(header_name=name, scheme=scheme).render("x")
        except EndpointRejected as exc:
            raise _invalid(str(exc)) from exc
        return kind, {"header_name": name, "scheme": scheme}
    if kind is AuthKind.OAUTH:
        return kind, {}
    return kind, {}


def validate_target(
    *,
    transport: Transport,
    location: ExecutionLocation,
    url: str | None,
    launch: Mapping[str, Any] | None,
    mounts: Sequence[Mount] = (),
) -> tuple[str | None, dict[str, Any] | None]:
    """Check a connection's target and return its storable form.

    ``mounts`` are the ones the platform derived and the owner was shown; a
    submitted launch document's own ``mounts`` are never read.
    """
    if location is ExecutionLocation.REMOTE:
        if transport is Transport.STDIO:
            raise _invalid("a remote connection cannot use the stdio transport")
        if not url:
            raise _invalid("a remote connection needs a URL")
        try:
            # The relay host dials under exactly the policy this validates
            # against: two doors, one policy.
            endpoint = validate_endpoint(url, policy_from_environment())
        except EndpointRejected as exc:
            raise _invalid(str(exc)) from exc
        return endpoint.url, None

    if transport is not Transport.STDIO:
        raise _invalid("a local process needs the stdio transport")
    if not launch or not launch.get("command"):
        raise _invalid("a stdio connection needs a command")
    try:
        spec = LaunchSpec(
            command=str(launch["command"]),
            args=tuple(str(arg) for arg in launch.get("args") or ()),
            env={str(k): str(v) for k, v in (launch.get("env") or {}).items()},
            cwd=str(launch["cwd"]) if launch.get("cwd") else None,
            mounts=tuple(mounts),
        )
        plan = build_launch_plan(
            spec, connection_id="draft", location=location, policy=ManagedLaunchPolicy()
        )
    except LaunchPlanError as exc:
        raise _invalid(str(exc)) from exc
    rendered: dict[str, Any] = {
        "command": spec.command,
        "args": list(spec.args),
        "env": dict(spec.env),
        "cwd": spec.cwd,
        "mounts": [
            {"source": mount.source, "target": mount.target, "read_only": mount.read_only}
            for mount in plan.mounts
        ],
    }
    return None, rendered


def static_credential(
    *,
    secret: str,
    header_name: str | None,
    header_scheme: str | None,
    fallback_header: str,
) -> tuple[str, dict[str, Any]]:
    """Validate and encrypt one static secret. No connection is written."""
    _kind, config = build_auth(AuthKind.STATIC.value, header_name or fallback_header, header_scheme)
    try:
        HeaderAuth(header_name=config["header_name"], scheme=config["scheme"]).render(secret)
    except EndpointRejected as exc:
        raise _invalid(str(exc)) from exc
    # Two characters and two more, so a person can tell *which* credential is
    # stored without the page ever holding one it could be made to render.
    config["hint"] = f"{secret[:2]}{secret[-2:]}" if len(secret) > 4 else ""
    try:
        ciphertext = mcp_cipher().encrypt(secret)
    except CredentialCipherError as exc:
        raise _no_encryption_key() from exc
    return ciphertext, config


def optional_static_credential(
    *,
    secret: str | None,
    header_name: str | None,
    header_scheme: str | None,
    fallback_header: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Encrypt a secret when the request carried one."""
    if not secret:
        return None, None
    return static_credential(
        secret=secret,
        header_name=header_name,
        header_scheme=header_scheme,
        fallback_header=fallback_header,
    )


def sealed_launch_spec(
    launch_spec: Mapping[str, Any] | None,
    secrets: Mapping[str, str],
    *,
    location: ExecutionLocation,
    label: str | None = None,
) -> dict[str, Any] | None:
    """Copy a launch spec with its ``secret://`` values encrypted.

    The reference stays ``secret://NAME``; the value lives as ciphertext under
    the same name. ``None`` means the spec declares nothing to seal. A missing
    encryption key, and a program's key too short to be masked, are refused
    here, before a connection row exists; ``label`` names an imported draft in
    the refusal.
    """
    spec = dict(launch_spec) if isinstance(launch_spec, Mapping) else {}
    env = {str(key): str(value) for key, value in (spec.get("env") or {}).items()}
    wanted = launch.declared_secret_names(env, {})
    if not wanted:
        return None
    sealing = {name: secrets[name] for name in wanted if name in secrets}
    if refusal := sandbox.short_key_refusal(location, sealing):
        raise _invalid(f"{label}: {refusal}" if label else refusal)
    try:
        cipher = mcp_cipher()
    except CredentialCipherError as exc:
        raise _no_encryption_key() from exc
    stored = {str(key): str(value) for key, value in (spec.get("env_secrets") or {}).items()}
    for name in wanted:
        if name in secrets:
            stored[name] = cipher.encrypt(secrets[name])
    spec["env_secrets"] = stored
    return spec


def edited_launch_spec(
    existing: Mapping[str, Any],
    submitted: Mapping[str, Any] | None,
    keys: Mapping[str, str],
    *,
    transport: Transport,
    location: ExecutionLocation,
) -> dict[str, Any]:
    """The launch an edit stores: the submitted one, or the stored one, with its keys.

    Checked as create checks it; only the form it renders is stored, with the
    keys ``launch.edited_secret_env`` keeps and sets, the ones set sealed here.
    """
    launch_spec = None
    if submitted is not None:
        _unused_url, launch_spec = validate_target(
            transport=transport, location=location, url=None, launch=submitted
        )
    try:
        launch_spec, keys = launch.edited_secret_env(existing, launch_spec, keys)
    except LaunchPlanError as exc:
        raise _invalid(str(exc)) from exc
    if keys:
        launch_spec = sealed_launch_spec(launch_spec, keys, location=location) or launch_spec
    return launch_spec


def without_short_keys(
    result: config_import.ImportResult, locations: dict[str, str]
) -> config_import.ImportResult:
    """The paste, with each program whose pasted key is too short moved to ``rejected``.

    Reported with the other entries that could not become a draft, in the
    preview as at the save, so the owner sees it before anything is stored. A
    key the owner types in later is checked when it is sealed.
    """
    drafts: list[config_import.ImportDraft] = []
    rejected = list(result.rejected)
    for draft in result.drafts:
        pasted = {
            secret.name: secret.value
            for secret in draft.secrets
            if secret.target == "env" and secret.value is not None
        }
        location = parse_location(locations.get(draft.label, draft.location))  # as at the save
        refusal = sandbox.short_key_refusal(location, pasted)
        if refusal:
            rejected.append((draft.label, refusal))
        else:
            drafts.append(draft)
    return replace(result, drafts=tuple(drafts), rejected=tuple(rejected))


def import_secrets(
    draft: config_import.ImportDraft,
    *,
    env_secrets: Mapping[str, str],
    server_secrets: Mapping[str, Mapping[str, str]],
) -> dict[str, str]:
    """Values an imported draft carries or the caller supplied for it."""
    secrets = {secret.name: secret.value for secret in draft.secrets if secret.value}
    supplied = server_secrets.get(draft.label, {})
    for secret in draft.secrets:
        value = supplied.get(secret.name, env_secrets.get(secret.name))
        if secret.value is None and value is not None:
            secrets[secret.name] = value
    return secrets


def prepare_import_draft(
    draft: config_import.ImportDraft,
    *,
    user_id: str,
    locations: Mapping[str, str],
    env_secrets: Mapping[str, str],
    server_secrets: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """One import draft, with credentials encrypted and nothing written yet."""
    parsed_location = parse_location(locations.get(draft.label, draft.location))
    launch_spec = None
    if draft.launch is not None:
        launch_spec = {
            "command": draft.launch.command,
            "args": list(draft.launch.args),
            "env": dict(draft.launch.env),
            "cwd": draft.launch.cwd,
        }
    # The checks create applies, and only the form they render is stored: the
    # run location must fit the transport, an address must pass the egress
    # policy and a command must build a launch plan. The local paths a command
    # names become a managed worker's mounts under that plan's mount rules.
    url, launch_spec = validate_target(
        transport=draft.transport,
        location=parsed_location,
        url=draft.url,
        launch=launch_spec,
        mounts=config_import.mounts_for(draft.paths)
        if parsed_location is ExecutionLocation.MANAGED_WORKER
        else (),
    )
    auth_config: dict[str, Any] = {}
    if draft.auth_kind is AuthKind.STATIC and draft.secrets:
        header_name = draft.secrets[0].name
        auth_config = {
            "header_name": header_name,
            "scheme": draft.header_auth.scheme
            if header_name == draft.header_auth.header_name
            else "",
        }
    secrets = import_secrets(draft, env_secrets=env_secrets, server_secrets=server_secrets)
    if refusal := sandbox.refusal(parsed_location, draft.auth_kind):
        raise _invalid(f"{draft.label}: {refusal}")
    access_token_ciphertext = None
    auth_config_patch = None
    sealed_spec = None
    if secrets and draft.transport is Transport.STDIO:
        sealed_spec = sealed_launch_spec(
            launch_spec, secrets, location=parsed_location, label=draft.label
        )
    elif secrets:
        header_name = str(auth_config.get("header_name") or "Authorization")
        secret = secrets.get(header_name)
        if secret is not None:
            access_token_ciphertext, auth_config_patch = static_credential(
                secret=secret,
                header_name=header_name,
                header_scheme=str(auth_config.get("scheme") or ""),
                fallback_header=header_name,
            )
    return {
        "user_id": user_id,
        "label": draft.label,
        "transport": draft.transport.value,
        "location": parsed_location.value,
        "url": url,
        "launch_spec": launch_spec,
        "auth_kind": draft.auth_kind.value,
        "auth_config": auth_config,
        "template": None,
        "reuse_pending": not draft.secrets,
        "access_token_ciphertext": access_token_ciphertext,
        "auth_config_patch": auth_config_patch,
        "sealed_launch_spec": sealed_spec,
    }


async def store_static_secret(
    store: McpConnectionRepository,
    *,
    row: Mapping[str, Any],
    secret: str,
    header_name: str | None,
    header_scheme: str | None,
    replaces_identity: bool,
    expected_epoch: int | None = None,
) -> bool:
    """Encrypt and store one static credential."""
    ciphertext, config = static_credential(
        secret=secret,
        header_name=header_name,
        header_scheme=header_scheme,
        fallback_header=header_of(row),
    )
    epoch = int(row["authorization_epoch"]) if expected_epoch is None else expected_epoch
    return await store.set_mcp_credential(
        connection_id=int(row["id"]),
        expected_authorization_epoch=epoch,
        access_token_ciphertext=ciphertext,
        auth_config_patch=config,
        bump_epoch=replaces_identity,
    )


def header_of(row: Mapping[str, Any]) -> str:
    """The header name a connection's static credential is injected as."""
    config = row.get("auth_config")
    settings = config if isinstance(config, Mapping) else {}
    return str(settings.get("header_name") or "Authorization")


async def require_connector(
    store: McpConnectionRepository, *, connector_id: int, user_id: str
) -> dict[str, Any]:
    """Fetch one of this user's connectors, or refuse."""
    row = next(
        (
            item
            for item in await store.list_mcp_connectors(user_id=user_id)
            if int(item["id"]) == connector_id
        ),
        None,
    )
    if row is None:
        raise Refused("not_found", "not_found", "no such connector")
    return row


__all__ = [
    "build_auth",
    "edited_launch_spec",
    "header_of",
    "import_secrets",
    "optional_static_credential",
    "parse_auth_kind",
    "parse_location",
    "parse_transport",
    "prepare_import_draft",
    "require_connector",
    "sealed_launch_spec",
    "static_credential",
    "store_static_secret",
    "validate_target",
    "without_short_keys",
]
