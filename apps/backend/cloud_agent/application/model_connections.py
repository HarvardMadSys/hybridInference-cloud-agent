"""Owners' model connections: their key, the turns that use them, and invalidation.

Every path that hands a connection to a turn -- admission, a follow-up, a
waiting message, a goal round, a claim, a residency and a Fork -- reads it
scoped to the turn's owner and asks :func:`require_usable`, so a disabled,
deleted, rotated or foreign connection is refused wherever the turn is, and
nothing falls back to the Gateway or to another model.

The key is decrypted only for a claim and a residency of its owner's own turn,
and for the owner's explicit check. A change that must not reach a process
still running on the old state -- a new key, Base URL or protocols, disabling
or deleting -- releases every residency started under the old state and closes
its process, the same way a chat deletion does.

See ``docs/agents/specs/2026-10-27-user-model-connections.zh-CN.md``.
"""

from __future__ import annotations

import contextlib
import secrets
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import HTTPException

from cloud_agent.accounts.config import local_mode
from cloud_agent.application.residency import _mint_residency_grant, close_released_residencies
from cloud_agent.credential_cipher import (
    USER_CREDENTIAL_ENCRYPTION_KEY,
    CredentialCipher,
    CredentialCipherError,
)
from cloud_agent.domain.execution import FAILED, RUNNING
from cloud_agent.domain.model_connections import (
    ModelConnectionUnusable,
    is_connection_id,
    require_usable,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import setting_env
from cloud_agent_shared.model_connections import (
    MODEL_CONNECTION_CAPABILITY,
    MODEL_PROTOCOL_COMPAT_CAPABILITY,
    ModelConnectionAccess,
    ProtocolPlan,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cloud_agent.ports import (
        AttemptCommands,
        GrantRepository,
        HostPoolRepository,
        ModelConnectionRepository,
        ResidencyRepository,
    )

logger = get_logger(__name__)

MODEL_CONNECTION_KEY_DOMAIN = b"hybridinference/model-connection-key/v1"
#: The domain of the scan-only copy kept of each key revision handed to a
#: sandbox group. A ciphertext of one domain cannot be decrypted as the
#: other's, and nothing that starts a process reads the scan copies at all.
MODEL_CONNECTION_SCAN_DOMAIN = b"hybridinference/model-connection-scan-key/v1"
#: How many key revisions one group's trusted snapshot or publication is
#: scanned for. A group handed more refuses to be scanned rather than
#: scanning for some: see :class:`ScanMaterialOverflow`.
MAX_SCAN_KEYS = 200
#: What a translated member's loopback token is a MAC of, under the
#: connection cipher's own platform-held MAC key, ahead of the member's
#: identity. Not the provider key: every member on a connection holds that.
TRANSLATOR_TOKEN_DOMAIN = b"cloud-agent/model-protocol-translation/token/v1"


class ScanMaterialOverflow(ValueError):
    """A group was handed more key revisions than a scan covers; it is not scanned partly."""


class ModelConnectionStorage(Protocol):
    """What resolving and invalidating a connection reaches for."""

    model_connections: ModelConnectionRepository
    host_pool: HostPoolRepository


def model_connection_cipher(secret: str | None = None) -> CredentialCipher:
    """The cipher connection keys are stored under, in their own domain."""
    material = (
        secret if secret is not None else setting_env(USER_CREDENTIAL_ENCRYPTION_KEY, "") or ""
    )
    return CredentialCipher(material, domain=MODEL_CONNECTION_KEY_DOMAIN)


def model_connection_scan_cipher(secret: str | None = None) -> CredentialCipher:
    """The cipher scan-only key copies are stored under."""
    material = (
        secret if secret is not None else setting_env(USER_CREDENTIAL_ENCRYPTION_KEY, "") or ""
    )
    return CredentialCipher(material, domain=MODEL_CONNECTION_SCAN_DOMAIN)


async def known_connection_secrets(
    connections: ModelConnectionRepository, *, sandbox_id: str
) -> dict[str, str]:
    """The key revisions this sandbox group was handed, for its trusted snapshot or publication.

    Only this group's: each was bound to one of its members before that
    member's CLI received it, whichever member, so a same-owner Fork's keys
    are here too, and a key the owner holds but never used in this group is
    never read, decrypted or sent to its host. Rotated and deleted
    connections' keys stay: a workspace file can hold a key long after it
    stopped being used. Read from the scan-only copies, never the execution
    keys. A copy that cannot be decrypted raises ``CredentialCipherError``,
    and more revisions than a scan covers raise ``ScanMaterialOverflow``: a
    scan that skipped a key would pass content it exists to stop.
    """
    ciphertexts = await connections.scan_key_ciphertexts(
        sandbox_id=sandbox_id, limit=MAX_SCAN_KEYS + 1
    )
    if len(ciphertexts) > MAX_SCAN_KEYS:
        raise ScanMaterialOverflow(
            f"this workspace ran on more than {MAX_SCAN_KEYS} model connection keys; "
            "its snapshots cannot be checked for all of them"
        )
    if not ciphertexts:
        return {}
    cipher = model_connection_scan_cipher()
    return {
        f"model-connection-key-{index}": cipher.decrypt(ciphertext)
        for index, ciphertext in enumerate(ciphertexts)
    }


async def trusted_scan_secrets(
    connections: ModelConnectionRepository, environment: Any, *, sandbox_id: str | None
) -> dict[str, str]:
    """What a group's trusted publication refuses: its environment's Secrets and its keys.

    Connection keys are attributed to a sandbox group when its member is
    bound, so a publication with no group was never handed one; every
    snapshot publication names its group.
    """
    values: dict[str, str] = dict(environment.secrets()) if environment is not None else {}
    if sandbox_id:
        values.update(await known_connection_secrets(connections, sandbox_id=sandbox_id))
    return values


def model_connections_enabled() -> bool:
    """Whether this deployment lets owners add connections and start turns on them.

    Off until an operator turns it on after the whole control plane runs a
    release that knows connections (``AGENT_MODEL_CONNECTIONS_ENABLED``): an
    older replica would run a connection's turn as a Gateway turn.

    Always on with local accounts (``AGENT_AUTH_MODE=local``). Their own
    connections are the only inference they have, and no older replica can be
    serving: one that predates connections cannot serve local accounts either.
    """
    if local_mode():
        return True
    raw = setting_env("AGENT_MODEL_CONNECTIONS_ENABLED", "") or ""
    return raw.strip().lower() in ("1", "true", "yes")


def new_connection_id() -> str:
    """A fresh, unguessable connection id."""
    return f"mconn_{secrets.token_hex(16)}"


async def resolve_turn_connection(
    connections: ModelConnectionRepository,
    *,
    user_id: str,
    connection_id: str,
    runtime: str,
    model: str,
    reasoning_effort: str | None = None,
) -> tuple[dict[str, Any], ProtocolPlan]:
    """The owner's connection for one turn and the plan it will run with, or refuse.

    Read scoped to ``user_id``: a connection id of somebody else's reads as
    missing, which is the only answer a forged or shared id ever gets.
    """
    if not is_connection_id(connection_id):
        raise ModelConnectionUnusable("not_found")
    row = await connections.get_model_connection(user_id=user_id, connection_id=connection_id)
    plan = require_usable(row, runtime=runtime, model=model, reasoning_effort=reasoning_effort)
    assert row is not None
    return row, plan


def translator_token(
    cipher: CredentialCipher,
    *,
    user_id: str,
    thread_id: str,
    source: str,
    client_protocol: str,
    model: str,
) -> str:
    """The loopback token of one translated member, for one process identity.

    A MAC under the platform's key over the owner, the member's thread, the
    ``<connection>@<revision>`` source, the CLI's protocol and the model:
    deterministic, so every claim and residency of that member's process
    agrees and its runner masks the very token the CLI holds, and different
    for a sibling, who cannot compute it from the provider key or any public
    id. A changed input -- or a rotated platform key -- changes the token,
    which is part of the process's reuse identity, so the process restarts.
    """
    fields = (user_id, thread_id, source, client_protocol, model)
    if not all(fields) or any("\0" in value for value in fields):
        raise ValueError("a translator token needs a complete member identity")
    return cipher.mac(b"\0".join((TRANSLATOR_TOKEN_DOMAIN, *(f.encode() for f in fields))))


def connection_access(
    row: dict[str, Any],
    plan: ProtocolPlan,
    cipher: CredentialCipher,
    *,
    user_id: str,
    thread_id: str,
    model: str,
) -> ModelConnectionAccess:
    """What a host needs to start ``thread_id``'s member on ``row``: URL, plan and key.

    A translated plan adds the member's loopback token (``translator_token``).
    Raises ``CredentialCipherError`` when the key cannot be decrypted, which is
    the deployment's problem rather than the owner's.
    """
    connection_id, revision = str(row["id"]), int(row["credential_revision"])
    token = ""
    if plan.translated:
        token = translator_token(
            cipher,
            user_id=user_id,
            thread_id=thread_id,
            source=f"{connection_id}@{revision}",
            client_protocol=plan.client_protocol,
            model=model,
        )
    return ModelConnectionAccess(
        connection_id=connection_id,
        revision=revision,
        base_url=str(row["base_url"]),
        protocol=plan.upstream_protocol,
        client_protocol=plan.client_protocol if plan.translated else "",
        api_key=cipher.decrypt(str(row["api_key_ciphertext"])),
        local_token=token,
    )


async def access_for_job(
    connections: ModelConnectionRepository,
    job: Mapping[str, Any],
    cipher: CredentialCipher,
    *,
    member: str | None = None,
) -> ModelConnectionAccess | None:
    """The connection access a job's turn runs on, ``None`` for a Gateway turn.

    ``job`` is a job row: its owner, runtime, model, effort and connection id
    are all re-read from it, never from a request. ``member`` is the thread
    whose process will hold it, when that is not the job's own (a Fork's new
    member runs the source's turn settings under its own token).
    """
    connection_id = job.get("model_connection_id")
    if not connection_id:
        return None
    row, plan = await resolve_turn_connection(
        connections,
        user_id=str(job["user_id"]),
        connection_id=str(connection_id),
        runtime=str(job["runtime"]),
        model=str(job["model"]),
        reasoning_effort=job.get("reasoning_effort"),
    )
    return connection_access(
        row,
        plan,
        cipher,
        user_id=str(job["user_id"]),
        thread_id=member or str(job["thread_id"]),
        model=str(job["model"]),
    )


async def bind_residency_access(
    connections: ModelConnectionRepository,
    *,
    user_id: str,
    access: ModelConnectionAccess,
    thread_id: str,
    generation: int,
) -> bool:
    """Record which connection revision a residency's process runs on, if still current.

    The key's scan-only copy is kept with it, attributed to the residency's
    sandbox group, before the key is handed to that group's CLI.
    """
    return await connections.bind_residency(
        user_id=user_id,
        connection_id=access.connection_id,
        revision=access.revision,
        thread_id=thread_id,
        generation=generation,
        scan_key_ciphertext=model_connection_scan_cipher().encrypt(access.api_key),
    )


async def invalidate_connection(
    store: ModelConnectionStorage,
    grants: Any,
    *,
    user_id: str,
    connection_id: str,
) -> int:
    """Close every member process still running on an older state of this connection.

    Called after the change committed. A residency bound before the change is
    found here; one bound after it was bound at the new revision, or refused.
    Returns how many were released. Best-effort beyond the release itself: a
    host that does not answer closes the process at its next residency
    heartbeat, which the release already refuses.
    """
    released = await store.model_connections.release_stale_residencies(
        user_id=user_id, connection_id=connection_id
    )
    if released:
        logger.info(
            "agent_model_connection_residencies_released",
            extra={
                "event": "agent_model_connection_residencies_released",
                "connection_id": connection_id,
                "count": len(released),
            },
        )
        await close_released_residencies(store.host_pool, grants, released)
    return len(released)


class _ClaimStorage(Protocol):
    attempt_commands: AttemptCommands
    model_connections: ModelConnectionRepository


class _ResidencyStorage(Protocol):
    grants: GrantRepository
    model_connections: ModelConnectionRepository
    residency: ResidencyRepository


class _TurnResidencyStorage(_ResidencyStorage, Protocol):
    attempt_commands: AttemptCommands


def unusable_error(exc: ModelConnectionUnusable, *, status: int) -> HTTPException:
    """The refusal a worker or Fork answers for a turn its connection cannot run."""
    return HTTPException(
        status_code=status,
        detail={
            "error": {
                "type": "model_connection_unusable",
                "reason": exc.reason,
                "message": str(exc),
            }
        },
    )


async def claim_model_connection(
    store: _ClaimStorage, claim: Mapping[str, Any], capabilities: Sequence[str]
) -> ModelConnectionAccess | None:
    """The connection a claimed turn runs on, or ``None`` for a Gateway turn.

    Settled before any credential is minted. A host that cannot launch a
    member on a connection gets the claim back, as does a deployment that
    cannot read the key: neither is the owner's to fix, and both pass. A
    connection the owner disabled, deleted or changed so that it no longer
    serves this turn ends the turn with that reason; it is never run anywhere
    else instead.
    """
    if not claim.get("model_connection_id"):
        return None
    fence = {
        "job_id": claim["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    waiting = HTTPException(
        503, "Waiting for a model-connection-capable host or available encryption"
    )
    if MODEL_CONNECTION_CAPABILITY not in capabilities:
        await store.attempt_commands.release_claim(**fence, retry_after_seconds=30)
        raise waiting
    try:
        access = await access_for_job(store.model_connections, claim, model_connection_cipher())
        # A translated plan also needs a host that can run the helper; an
        # older one gets the claim back rather than a turn it cannot launch.
        needs_compat = access is not None and access.plan.translated
        if needs_compat and MODEL_PROTOCOL_COMPAT_CAPABILITY not in capabilities:
            await store.attempt_commands.release_claim(**fence, retry_after_seconds=30)
            raise waiting
        return access
    except CredentialCipherError:
        await store.attempt_commands.release_claim(**fence, retry_after_seconds=30)
        raise waiting from None
    except ModelConnectionUnusable as exc:
        await store.attempt_commands.transition(
            **fence, from_states=(RUNNING,), to_state=FAILED, detail=str(exc)
        )
        logger.warning(
            "agent_job_model_connection_unusable",
            extra={
                "event": "agent_job_model_connection_unusable",
                "job_id": claim["id"],
                "reason": exc.reason,
            },
        )
        raise unusable_error(exc, status=403) from None


async def connection_residency_credential(
    store: _ResidencyStorage, job: Mapping[str, Any], *, row: Mapping[str, Any]
) -> ModelConnectionAccess:
    """The key a residency on an owner connection runs on, bound to that residency.

    ``job`` is the turn the residency serves; ``row`` the residency just
    established. The connection is checked again here, and its revision is
    stamped on the membership under the connection's lock: a change committed
    first refuses this, and one committed after finds and closes it.
    """
    access = await access_for_job(
        store.model_connections, job, model_connection_cipher(), member=str(row["thread_id"])
    )
    if access is None:
        raise ModelConnectionUnusable("not_found")
    bound = await bind_residency_access(
        store.model_connections,
        user_id=str(job["user_id"]),
        access=access,
        thread_id=str(row["thread_id"]),
        generation=int(row["generation"]),
    )
    if not bound:
        raise ModelConnectionUnusable("changed")
    return access


async def residency_credential(
    store: _TurnResidencyStorage,
    grants: Any,
    *,
    job: Mapping[str, Any],
    row: dict[str, Any],
    host: str,
    fence: Mapping[str, Any],
) -> tuple[str | None, int | None, str | None]:
    """A new residency's credential and, on an owner connection, its revision and plan.

    The host matches both to its claim: the key may have rotated, and a
    model's verification may have moved it between a native and a translated
    plan, while the turn was starting.

    A Gateway turn's residency gets its worker-fenced grant, as it always has.
    A turn on an owner connection gets that connection's key and no grant at
    all: neither the Gateway's grant nor its quota stands between an owner
    and their own provider. When the connection cannot serve it, the residency
    is released before anything holds a credential, and the turn -- ``fence``
    names its attempt -- ends with the reason, as it would have at its claim.
    """
    if not job.get("model_connection_id"):
        return await _mint_residency_grant(store.grants, grants, row=row), None, None
    try:
        access = await connection_residency_credential(store, job, row=row)
    except (ModelConnectionUnusable, CredentialCipherError) as exc:
        with contextlib.suppress(Exception):
            await store.residency.release_residency(
                thread_id=str(row["thread_id"]), generation=int(row["generation"]), host=host
            )
        if isinstance(exc, CredentialCipherError):
            raise HTTPException(503, "Model connection keys cannot be read") from None
        await store.attempt_commands.transition(
            job_id=fence["job_id"],
            attempt_id=fence["attempt_id"],
            lease_generation=fence["lease_generation"],
            from_states=(RUNNING,),
            to_state=FAILED,
            detail=str(exc),
        )
        raise unusable_error(exc, status=403) from None
    return access.api_key, access.revision, access.plan.identity


__all__ = [
    "MAX_SCAN_KEYS",
    "MODEL_CONNECTION_KEY_DOMAIN",
    "MODEL_CONNECTION_SCAN_DOMAIN",
    "TRANSLATOR_TOKEN_DOMAIN",
    "ModelConnectionStorage",
    "ScanMaterialOverflow",
    "access_for_job",
    "bind_residency_access",
    "claim_model_connection",
    "connection_access",
    "connection_residency_credential",
    "invalidate_connection",
    "known_connection_secrets",
    "model_connection_cipher",
    "model_connection_scan_cipher",
    "model_connections_enabled",
    "new_connection_id",
    "residency_credential",
    "resolve_turn_connection",
    "translator_token",
    "trusted_scan_secrets",
    "unusable_error",
]
