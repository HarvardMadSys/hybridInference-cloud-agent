"""What the control plane and a deployment agent agree an operation may be.

The control plane validates an administrator's intent with this module before
storing it, and the deployment agent validates the same intent again with the
same module before executing it, against its own machine's policy as well. The
module is shipped in the release recipe for the agent and in the control-plane
image, and imports nothing outside the standard library and ``cryptography``.

Nothing here executes anything. There is no free-form command, path, Compose
content or URL: an operation is one of :data:`OPERATIONS`, its parameters
match that operation's schema, and a configuration change names keys of
:data:`CONFIG_KEYS` with values of their declared type. Keys that would weaken
sandbox isolation (``AGENT_SANDBOX_ALLOW_UNISOLATED``,
``AGENT_SANDBOX_EXTRA_ARGS``) are not in the schema at all; boundary keys can
only take values the machine's local policy lists, and protected ranges can
only grow (AGENTS.md section 7).

Secret values travel sealed: the browser encrypts each one to the agent's
public key (ECDH P-256, HKDF-SHA256, AES-256-GCM), so the control plane stores
and relays ciphertext it cannot read. :func:`seal` exists for tests and for
the agent's own local commands; the browser has its own implementation of the
same format in ``apps/frontend``.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["control-plane", "host"]
ROLES: tuple[Role, ...] = ("control-plane", "host")

#: Intent states and the only transitions either side accepts.
INTENT_STATES = (
    "pending",
    "delivered",
    "running",
    "succeeded",
    "failed",
    "rejected",
    "expired",
    "cancelled",
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "rejected", "expired", "cancelled"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"delivered", "expired", "cancelled"}),
    "delivered": frozenset({"running", "rejected", "failed", "succeeded"}),
    "running": frozenset({"succeeded", "failed"}),
}


class ContractError(ValueError):
    """An intent or change outside the contract; the message names no value."""


def can_transition(current: str, new: str) -> bool:
    """Whether an intent may move from ``current`` to ``new``."""
    return new in TRANSITIONS.get(current, frozenset())


# -- configuration keys ------------------------------------------------------

KeyKind = Literal[
    "bind", "port", "name", "bool", "choice", "url", "path", "secret", "cidrs", "text"
]
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_PATH = re.compile(r"^/[A-Za-z0-9_./-]{0,511}$")


@dataclass(frozen=True)
class ConfigKey:
    """One deployment configuration key an agent may write into its ``.env``."""

    name: str
    role: Role
    kind: KeyKind
    item: str
    services: tuple[str, ...]
    choices: tuple[str, ...] = ()
    #: ``policy`` limits the value to the machine's policy list for this key;
    #: ``grow`` additionally refuses removing an entry (protected ranges).
    boundary: Literal["", "policy", "grow"] = ""

    def check(self, value: str) -> str:
        """Return the normalized value, or raise without echoing it."""
        if not isinstance(value, str) or "\n" in value or "\x00" in value or len(value) > 4096:
            raise ContractError(f"{self.name} must be one line of text")
        value = value.strip()
        valid = _KIND_CHECKS[self.kind]
        if not (value in self.choices if self.kind == "choice" else valid(value)):
            raise ContractError(f"{self.name} is not a valid {self.kind} value")
        return value


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _is_cidrs(value: str) -> bool:
    try:
        for part in filter(None, (p.strip() for p in value.split(","))):
            ipaddress.ip_network(part, strict=False)
    except ValueError:
        return False
    return True


_KIND_CHECKS: dict[str, Any] = {
    "bind": _is_ip,
    "port": lambda v: v.isdigit() and 1 <= int(v) <= 65535,
    "name": lambda v: bool(_NAME.fullmatch(v)),
    "bool": lambda v: v in {"true", "false", "True", "False"},
    "choice": lambda v: True,
    "url": lambda v: v == "" or bool(re.fullmatch(r"https?://[A-Za-z0-9.\-:\[\]]+(/[^\s]*)?", v)),
    "path": lambda v: v == "" or (bool(_PATH.fullmatch(v)) and ".." not in v.split("/")),
    "cidrs": _is_cidrs,
    "secret": lambda v: bool(v),
    "text": lambda v: True,
}


def _keys(
    role: Role, item: str, services: tuple[str, ...], *specs: tuple[Any, ...]
) -> list[ConfigKey]:
    result = []
    for spec in specs:
        name, kind, *rest = spec
        extra: dict[str, Any] = {}
        if rest:
            extra = rest[0]
        result.append(
            ConfigKey(name=name, role=role, kind=kind, item=item, services=services, **extra)
        )
    return result


_CP_LISTENERS = ("control-plane", "mcp-relay", "web", "edge")
CONFIG_KEYS: dict[str, ConfigKey] = {
    key.name: key
    for key in (
        *_keys(
            "control-plane",
            "deployment.control_plane_listeners",
            _CP_LISTENERS,
            ("AGENT_CONTROL_PLANE_BIND", "bind"),
            ("AGENT_CONTROL_PLANE_PORT", "port"),
            ("AGENT_MCP_RELAY_BIND", "bind"),
            ("AGENT_MCP_RELAY_PORT", "port"),
            ("AGENT_WEB_BIND", "bind"),
            ("AGENT_WEB_PORT", "port"),
            ("AGENT_EDGE_BIND", "bind"),
            ("AGENT_EDGE_PORT", "port"),
            ("AGENT_NETWORK_NAME", "name"),
        ),
        *_keys(
            "control-plane",
            "deployment.optional_services",
            ("pgadmin", "edge"),
            ("COMPOSE_PROFILES", "choice", {"choices": ("", "edge", "pgadmin", "edge,pgadmin")}),
        ),
        *_keys(
            "control-plane",
            "deployment.pgadmin",
            ("pgadmin", "control-plane"),
            ("AGENT_PGADMIN_BOOTSTRAP_URL", "url", {"boundary": "policy"}),
            ("AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL", "url"),
            ("AGENT_PGADMIN_BOOTSTRAP_DIR", "path"),
            ("AGENT_PGADMIN_COOKIE_SECURE", "choice", {"choices": ("True", "False")}),
            ("AGENT_PGADMIN_URL", "url", {"boundary": "policy"}),
        ),
        *_keys(
            "control-plane",
            "deployment.profiling",
            ("control-plane", "mcp-relay"),
            (
                "AGENT_OPERATION_PROFILING_MODE",
                "choice",
                {"choices": ("disabled", "environment", "admin"), "boundary": "policy"},
            ),
            ("AGENT_OPERATION_PROFILING_DEPLOYMENT", "name"),
        ),
        *_keys(
            "control-plane",
            "deployment.web_routing",
            ("web",),
            ("AGENT_WEB_VARIANT", "choice", {"choices": ("root", "agents")}),
        ),
        *_keys(
            "control-plane",
            "security.admin_token",
            ("control-plane", "mcp-relay"),
            ("ADMIN_TOKEN", "secret"),
        ),
        *_keys(
            "control-plane",
            "security.settings_root_key",
            ("control-plane", "mcp-relay"),
            ("AGENT_SETTINGS_ENCRYPTION_KEY", "secret"),
            ("AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS", "secret"),
        ),
        *_keys(
            "control-plane",
            "database.connection",
            ("control-plane", "mcp-relay"),
            ("AGENT_DATABASE_URL", "secret"),
        ),
        *_keys(
            "host",
            "hosts.topology",
            ("runner", "sandbox-manager"),
            ("AGENT_RUNNER_STOP_GRACE", "text"),
            ("AGENT_SANDBOX_MANAGER_STOP_GRACE", "text"),
            ("AGENT_HOST_CAPACITY_ENABLED", "choice", {"choices": ("1", "0")}),
            ("AGENT_SANDBOX_IMAGE_PULL", "choice", {"choices": ("true", "false")}),
            ("AGENT_RUNTIME_DOWNLOAD_NETWORK", "name"),
        ),
        *_keys(
            "host",
            "hosts.sandbox_boundary",
            ("runner", "sandbox-manager"),
            (
                "AGENT_SANDBOX_RUNTIME",
                "choice",
                {"choices": ("runc", "io.containerd.kata.v2", "kata"), "boundary": "policy"},
            ),
            ("AGENT_EGRESS_DNS_SERVER", "bind", {"boundary": "policy"}),
            ("AGENT_EGRESS_CHAT_NETWORK_POOL", "cidrs", {"boundary": "policy"}),
            ("AGENT_EGRESS_PROTECTED_CIDRS", "cidrs", {"boundary": "grow"}),
        ),
        *_keys(
            "host",
            "hosts.sandbox_manager_token",
            ("sandbox-manager", "runner"),
            ("AGENT_SANDBOX_MANAGER_TOKEN", "secret"),
            ("AGENT_SANDBOX_MANAGER_TOKEN_NEXT", "secret"),
        ),
    )
}
#: The profiling ceiling is shared with hosts under the same name.
CONFIG_KEYS_BY_ROLE: dict[Role, dict[str, ConfigKey]] = {
    role: {name: key for name, key in CONFIG_KEYS.items() if key.role == role} for role in ROLES
}
CONFIG_KEYS_BY_ROLE["host"]["AGENT_OPERATION_PROFILING_MODE"] = ConfigKey(
    name="AGENT_OPERATION_PROFILING_MODE",
    role="host",
    kind="choice",
    item="deployment.profiling",
    services=("runner",),
    choices=("disabled", "environment", "admin"),
    boundary="policy",
)
#: Names never accepted from an intent, whatever a schema change might add:
#: they weaken sandbox isolation or bypass deployment safety checks.
FORBIDDEN_KEYS = frozenset(
    {
        "AGENT_SANDBOX_ALLOW_UNISOLATED",
        "AGENT_SANDBOX_EXTRA_ARGS",
        "AGENT_SANDBOX_DOCKER",
        "ALLOW_DIRTY",
        "ALLOW_STRAY_STACK",
        "AGENT_CONTROL_PLANE_COMPOSE_OVERRIDE_FILE",
    }
)


def check_config_changes(
    role: Role,
    changes: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    current: dict[str, str] | None = None,
    sealed_keys: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Validate a configuration change for one machine; return normalized values.

    ``changes`` holds non-secret values; a ``secret`` key must arrive sealed
    (``sealed_keys``) and is validated by the agent after it opens it.
    ``policy`` is the machine's local policy (``allowed`` lists per key);
    ``current`` is its current ``.env`` for ``grow`` checks. The control
    plane passes the policy summary the agent reported; the agent passes its
    own policy file.
    """
    keys = CONFIG_KEYS_BY_ROLE[role]
    if not changes and not sealed_keys:
        raise ContractError("a configuration change names at least one key")
    for name in [*changes, *sealed_keys]:
        _check_name(keys, role, name, sealed_keys)
    normalized: dict[str, str] = {}
    for name, raw in changes.items():
        key = keys[name]
        value = key.check(raw)
        if policy is not None:
            _check_policy(key, value, (policy.get("allowed") or {}))
        if key.boundary == "grow" and current is not None:
            before = {p.strip() for p in current.get(name, "").split(",") if p.strip()}
            after = {p.strip() for p in value.split(",") if p.strip()}
            if not before <= after:
                raise ContractError(f"{name} can only add ranges; removing one is a design change")
        normalized[name] = value
    return normalized


def _check_name(
    keys: dict[str, ConfigKey], role: str, name: str, sealed_keys: frozenset[str]
) -> None:
    if name in FORBIDDEN_KEYS:
        raise ContractError(f"{name} cannot be changed through a deployment agent")
    key = keys.get(name)
    if key is None:
        raise ContractError(f"{name} is not a {role} deployment setting")
    if key.kind == "secret" and name not in sealed_keys:
        raise ContractError(f"{name} must be sealed to the agent")
    if key.kind != "secret" and name in sealed_keys:
        raise ContractError(f"{name} is not a secret")


def _check_policy(key: ConfigKey, value: str, allowed: dict[str, Any]) -> None:
    """A boundary value must be one the machine's policy lists."""
    if key.boundary != "policy":
        return
    if key.name not in allowed:
        raise ContractError(f"{key.name} needs an allowed list in this machine's policy")
    permitted = allowed[key.name]
    values = [p.strip() for p in value.split(",") if p.strip()] if key.kind == "cidrs" else [value]
    if any(item not in permitted for item in values):
        raise ContractError(f"{key.name} names a value this machine's policy does not allow")


def check_rendered_environment(role: Role, environment: dict[str, str]) -> None:
    """Hold an environment another executor renders to the same key rules.

    The control plane's SSH executor renders a managed server's ``.env``
    itself; it checks every key this contract knows with the same rules an
    agent applies, and refuses the keys no executor may write.
    """
    keys = CONFIG_KEYS_BY_ROLE[role]
    for name, value in environment.items():
        if name in FORBIDDEN_KEYS:
            raise ContractError(f"{name} cannot be written by a deployment executor")
        key = keys.get(name)
        if key is not None and key.kind != "secret":
            key.check(value)


def services_for(role: Role, names: list[str]) -> list[str]:
    """The services a change to ``names`` restarts, in a stable order."""
    keys = CONFIG_KEYS_BY_ROLE[role]
    seen: dict[str, None] = {}
    for name in names:
        for service in keys[name].services:
            seen[service] = None
    return list(seen)


# -- operations --------------------------------------------------------------

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,63}$")
_SERVICE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
#: Services an agent may restart, per role.
SERVICES: dict[Role, tuple[str, ...]] = {
    "control-plane": ("control-plane", "mcp-relay", "web", "edge", "pgadmin"),
    "host": ("runner", "sandbox-manager", "gateway-tunnel", "mcp-tunnel", "control-plane-tunnel"),
}


@dataclass(frozen=True)
class Operation:
    """One operation an agent can be asked to perform."""

    name: str
    roles: tuple[Role, ...]
    #: Whether the request must repeat the operation name as ``confirm``.
    confirm: bool
    description: str
    sealed: bool = False
    params: tuple[str, ...] = field(default_factory=tuple)


OPERATIONS: dict[str, Operation] = {
    op.name: op
    for op in (
        Operation(
            "apply_config",
            ROLES,
            False,
            "Write a new configuration version and restart what it affects.",
            sealed=True,
            params=("changes",),
        ),
        Operation(
            "rollback_config",
            ROLES,
            True,
            "Re-apply an earlier configuration version.",
            params=("to_rev",),
        ),
        Operation(
            "restart_services", ROLES, False, "Recreate named services.", params=("services",)
        ),
        Operation(
            "release_prepare",
            ROLES,
            False,
            "Pull and check a verified release without replacing services.",
            params=("manifest_sha256",),
        ),
        Operation(
            "release_prepare_database",
            ("control-plane",),
            True,
            "Migrate the database for a verified release.",
            params=("manifest_sha256",),
        ),
        Operation(
            "release_activate",
            ROLES,
            True,
            "Replace the running services with a prepared release.",
            params=("manifest_sha256",),
        ),
        Operation(
            "release_abandon",
            ("control-plane",),
            True,
            "Stop listeners started only for an abandoned cold deployment.",
            params=("manifest_sha256",),
        ),
        Operation(
            "kata_install",
            ("host",),
            True,
            "Drain and install a reviewed Kata version and digest.",
            params=("version", "sha256"),
        ),
        Operation(
            "rotation_step",
            ROLES,
            True,
            "Perform one step of a key rotation workflow.",
            sealed=True,
            params=("workflow", "operation_id", "step"),
        ),
        Operation(
            "database_check",
            ("control-plane",),
            False,
            "Prove a candidate connection reaches this same database.",
            sealed=True,
            params=("probe_id", "probe_token", "rotate_password"),
        ),
        Operation(
            "database_switch",
            ("control-plane",),
            True,
            "Switch the control plane to a checked connection.",
            sealed=True,
            params=("probe_id", "rotate_password", "leader"),
        ),
        Operation(
            "database_migrate",
            ("control-plane",),
            True,
            "Copy the database to an empty target and switch to it.",
            sealed=True,
            params=("operation_id",),
        ),
    )
}
ROTATION_WORKFLOWS = frozenset({"root_key", "admin_token", "sandbox_manager_token"})


def check_operation(
    operation: str,
    role: Role,
    params: dict[str, Any],
    sealed: dict[str, Any] | None = None,
    *,
    policy: dict[str, Any] | None = None,
    current: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate an operation's name, role, parameters and sealed values.

    ``policy`` and ``current`` are the machine's policy and ``.env``; the
    control plane passes the policy the agent last reported (or ``None``
    before one exists), and the agent always passes its own.
    """
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise ContractError("unknown operation")
    if role not in spec.roles:
        raise ContractError(f"{operation} does not run on a {role} machine")
    if not isinstance(params, dict) or set(params) - set(spec.params):
        raise ContractError(f"{operation} takes only: {', '.join(spec.params) or 'no parameters'}")
    if sealed and not spec.sealed:
        raise ContractError(f"{operation} takes no sealed values")
    for name, value in (sealed or {}).items():
        if not isinstance(name, str) or not _is_sealed_box(value):
            raise ContractError("a sealed value is not in the sealed format")
    _PARAM_CHECKS[_family(operation)](operation, role, params, sealed or {}, policy, current)
    return dict(params)


def _family(operation: str) -> str:
    return "release" if operation.startswith("release_") else operation


def _uuid_hex(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", str(value)))


def _check_apply(operation, role, params, sealed, policy, current) -> None:  # type: ignore[no-untyped-def]
    changes = params.get("changes") or {}
    if not isinstance(changes, dict) or not all(isinstance(v, str) for v in changes.values()):
        raise ContractError("changes maps names to text values")
    check_config_changes(
        role, changes, sealed_keys=frozenset(sealed), policy=policy, current=current
    )


def _check_rollback(operation, role, params, sealed, policy, current) -> None:  # type: ignore[no-untyped-def]
    to_rev = params.get("to_rev")
    if not isinstance(to_rev, int) or isinstance(to_rev, bool) or to_rev < 0:
        raise ContractError("to_rev is a configuration version number")


def _check_restart(operation, role, params, sealed, policy, current) -> None:  # type: ignore[no-untyped-def]
    services = params.get("services")
    if (
        not isinstance(services, list)
        or not services
        or any(s not in SERVICES[role] for s in services)
    ):
        raise ContractError(f"services are some of: {', '.join(SERVICES[role])}")


def _check_release(operation, role, params, sealed, policy, current) -> None:  # type: ignore[no-untyped-def]
    if not _SHA256.fullmatch(str(params.get("manifest_sha256", ""))):
        raise ContractError("manifest_sha256 is a lowercase SHA-256")


def _check_kata(operation, role, params, sealed, policy, current) -> None:  # type: ignore[no-untyped-def]
    if not _VERSION.fullmatch(str(params.get("version", ""))) or not _SHA256.fullmatch(
        str(params.get("sha256", ""))
    ):
        raise ContractError("version and sha256 name a reviewed Kata release")
    if policy is not None:
        pairs = {
            (p.get("version"), p.get("sha256"))
            for p in policy.get("kata", [])
            if isinstance(p, dict)
        }
        if (params["version"], params["sha256"]) not in pairs:
            raise ContractError("this Kata version and digest are not in this machine's policy")


def _check_rotation(operation, role, params, sealed, policy, current) -> None:  # type: ignore[no-untyped-def]
    if params.get("workflow") not in ROTATION_WORKFLOWS:
        raise ContractError("unknown rotation workflow")
    if not _uuid_hex(params.get("operation_id")):
        raise ContractError("operation_id is a rotation operation")
    if not _SERVICE.fullmatch(str(params.get("step", ""))):
        raise ContractError("step names a rotation step")


def _check_database(operation, role, params, sealed, policy, current) -> None:  # type: ignore[no-untyped-def]
    if not _uuid_hex(params.get("probe_id")):
        raise ContractError("probe_id names a database probe")
    if operation == "database_check":
        if not re.fullmatch(r"[0-9a-f]{64}", str(params.get("probe_token", ""))):
            raise ContractError("probe_token is the probe's first value")
        if set(sealed) != {"AGENT_DATABASE_URL"}:
            raise ContractError("database_check takes the candidate AGENT_DATABASE_URL, sealed")
    elif set(sealed) != {"AGENT_DATABASE_URL"}:
        raise ContractError("database_switch takes the new AGENT_DATABASE_URL, sealed")
    for flag in ("rotate_password", "leader"):
        if not isinstance(params.get(flag, False), bool):
            raise ContractError(f"{flag} is true or false")


def _check_migrate(operation, role, params, sealed, policy, current) -> None:  # type: ignore[no-untyped-def]
    if not _uuid_hex(params.get("operation_id")):
        raise ContractError("operation_id is a migration operation")
    if set(sealed) != {"AGENT_DATABASE_URL"}:
        raise ContractError("database_migrate takes the target AGENT_DATABASE_URL, sealed")


_PARAM_CHECKS = {
    "apply_config": _check_apply,
    "rollback_config": _check_rollback,
    "restart_services": _check_restart,
    "release": _check_release,
    "kata_install": _check_kata,
    "rotation_step": _check_rotation,
    "database_check": _check_database,
    "database_switch": _check_database,
    "database_migrate": _check_migrate,
}


# -- sealed values -----------------------------------------------------------

SEALED_VERSION = 1


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def decode_b64(text: str) -> bytes:
    """Strict base64, as the sealed format and public keys use it."""
    return base64.b64decode(text.encode(), validate=True)


def _is_sealed_box(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("v") == SEALED_VERSION
        and all(isinstance(value.get(k), str) and value.get(k) for k in ("epk", "iv", "ct"))
        and len(json.dumps(value)) < 16384
    )


def _derive(shared: bytes, purpose: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"cloud-agent sealed value v1",
        info=purpose.encode(),
    ).derive(shared)


def generate_keypair() -> tuple[bytes, str]:
    """A new P-256 private key (PEM) and its public key (base64 raw point)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return pem, _b64(public)


def seal(public_key: str, plaintext: str, purpose: str) -> dict[str, Any]:
    """Encrypt ``plaintext`` to ``public_key`` for ``purpose`` (agent id and key name)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode_b64(public_key))
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    key = _derive(ephemeral.exchange(ec.ECDH(), peer), purpose)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, plaintext.encode(), purpose.encode())
    epk = ephemeral.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {"v": SEALED_VERSION, "epk": _b64(epk), "iv": _b64(iv), "ct": _b64(ct)}


def open_sealed(private_pem: bytes, box: dict[str, Any], purpose: str) -> str:
    """Decrypt a sealed value; raise :class:`ContractError` on any mismatch."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not _is_sealed_box(box):
        raise ContractError("not a sealed value")
    private = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(private, ec.EllipticCurvePrivateKey):
        raise ContractError("the agent key is not a P-256 key")
    try:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode_b64(box["epk"]))
        key = _derive(private.exchange(ec.ECDH(), peer), purpose)
        return (
            AESGCM(key)
            .decrypt(decode_b64(box["iv"]), decode_b64(box["ct"]), purpose.encode())
            .decode()
        )
    except (InvalidTag, ValueError) as exc:
        raise ContractError("a sealed value does not open with this agent's key") from exc


def key_identifier(secret: str) -> str:
    """A one-way identifier for a key: equal keys have equal identifiers.

    HMAC-SHA256 keyed by the secret over a fixed label, truncated. Control-plane
    instances, agents and the browser compute the same value, so a rotation can
    prove which key every participant holds without any of them revealing it.
    """
    import hashlib
    import hmac

    if not secret:
        return ""
    return hmac.new(secret.encode(), b"cloud-agent key identifier v1", hashlib.sha256).hexdigest()[
        :16
    ]


def sealed_purpose(agent_id: str, name: str) -> str:
    """The associated data binding a sealed value to one agent and one key name."""
    return f"{agent_id}:{name}"


__all__ = [
    "CONFIG_KEYS",
    "CONFIG_KEYS_BY_ROLE",
    "FORBIDDEN_KEYS",
    "INTENT_STATES",
    "OPERATIONS",
    "ROLES",
    "SERVICES",
    "TERMINAL_STATES",
    "ConfigKey",
    "ContractError",
    "Operation",
    "can_transition",
    "check_config_changes",
    "check_operation",
    "check_rendered_environment",
    "decode_b64",
    "generate_keypair",
    "key_identifier",
    "open_sealed",
    "seal",
    "sealed_purpose",
    "services_for",
]
