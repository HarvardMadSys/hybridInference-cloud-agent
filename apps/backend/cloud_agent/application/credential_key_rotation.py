"""Rotate one of the two credential encryption keys without stranding a stored value.

``credential_key`` (params ``key``: ``user`` or ``compute``):

``stage``         one settings update makes the new key current and keeps the
                  old one as ``<key>_previous`` (decrypt only); for the user
                  key, lookup digests stay on the old key
                  (``credential_index_uses_previous``). Done when every current
                  process has adopted it and declares the capability.
``reencrypt``     every family of :mod:`cloud_agent.credential_families`, in
                  batches from a cursor kept on the workflow, each row a
                  compare-and-swap; then a pass proving every value opens with
                  the new key alone.
``switch_index``  (user key) digests move to the new key; adopted by everyone.
``reindex``       (user key) environment fingerprints of bindings and
                  snapshots move from the old key's digest to the new one's.
``retire``        the previous key is cleared; adopted by everyone.

Before ``reencrypt`` starts, going back is writing the old key back (nothing is
encrypted with the new one that the old one cannot be restored over by the
same rotation in reverse); once it starts, the workflow only goes forward.
"""

from __future__ import annotations

import uuid
from typing import Any

from cloud_agent.application.workflows import ADVANCERS, WorkflowRefused, save
from cloud_agent_shared.deployment_contract import key_identifier

CAPABILITY = "credential-key-rotation-v1"
KEYS = {
    "user": ("source_control_encryption_key", "source_control_encryption_key_previous"),
    "compute": ("compute_encryption_key", "compute_encryption_key_previous"),
}
#: Batches one advance runs before handing the tick back.
BATCHES_PER_TICK = 10
#: Full re-encryption passes before a value that still is not current fails the step.
MAX_PASSES = 3


def _seal(secret: str) -> dict[str, str]:
    from cloud_agent.application.key_rotation import _seal_at_rest

    return _seal_at_rest(secret)


def _open(sealed: dict[str, str]) -> str:
    from cloud_agent.application.key_rotation import _open_at_rest

    return _open_at_rest(sealed)


async def _saved(storage: Any) -> tuple[int, dict[str, Any]]:
    from cloud_agent import settings

    revision, encoded = await storage.settings.read_settings()
    return revision, settings._decode(encoded)


async def start_credential_key_rotation(
    storage: Any, *, key: str, secret: str | None, admin_id: str
) -> dict[str, Any]:
    """Validate and start rotating one credential encryption key."""
    from cloud_agent.instances import fleet_supports

    if key not in KEYS:
        raise WorkflowRefused("key is user or compute")
    if not secret or len(secret.encode()) < 32:
        raise WorkflowRefused("the new key must be at least 32 bytes")
    name, previous = KEYS[key]
    _revision, values = await _saved(storage)
    if not values[name]:
        raise WorkflowRefused(
            "this key is not configured yet; set it on the Settings page instead of rotating it"
        )
    if values[previous]:
        raise WorkflowRefused("an earlier rotation of this key still keeps its previous key")
    if secret == values[name]:
        raise WorkflowRefused("the new key equals the current one")
    verdict = await fleet_supports(storage.instances, CAPABILITY)
    if not verdict.supported:
        raise WorkflowRefused("the key cannot be rotated yet: " + verdict.reason())
    workflow = await storage.deployments.create_workflow(
        workflow_id=uuid.uuid4().hex,
        kind="credential_key",
        step="stage",
        params={
            "key": key,
            "new_key_id": key_identifier(secret),
            "old_key_id": key_identifier(str(values[name])),
        },
        created_by=admin_id,
    )
    if workflow is None:
        raise WorkflowRefused("a credential key rotation is already running")
    await save(storage, workflow, data={"next_key": _seal(secret)})
    await ADVANCERS["credential_key"](storage, workflow)
    return workflow


def _label(instance: dict[str, Any]) -> str:
    return f"{instance['role']}@{instance['machine']}"


async def _everyone_adopted(
    storage: Any, workflow: dict[str, Any], revision: int, what: str
) -> bool:
    """Every current process uses ``revision`` and declares the capability; else wait, naming them.

    No report at all is not everyone. There is no time limit: a process that
    never adopts is named until an administrator stops it or it comes back.
    """
    instances = [i for i in await storage.instances.list_instances() if i["current"]]
    laggards = (
        [
            _label(i)
            for i in instances
            if i["adopted_settings_revision"] < revision or CAPABILITY not in i["capabilities"]
        ]
        if instances
        else ["no control-plane process reports"]
    )
    if laggards:
        detail = f"waiting for every process to {what}: " + ", ".join(laggards[:10])
        if workflow["state"] != "waiting" or workflow["detail"] != detail:
            await save(storage, workflow, state="waiting", detail=detail)
        return False
    return True


def _cipher_for(key: str, domain: bytes) -> Any:
    from cloud_agent.credential_cipher import (
        CredentialCipher,
        compute_key_ring,
        user_credential_cipher,
    )

    if key == "user":
        return user_credential_cipher(domain)
    current, previous = compute_key_ring()
    return CredentialCipher(current, domain=domain, previous=previous)


def _current_key_id(key: str) -> str:
    from cloud_agent.credential_cipher import compute_key_ring, user_key_ring

    return key_identifier(user_key_ring()[0] if key == "user" else compute_key_ring()[0])


async def _stage(storage: Any, workflow: dict[str, Any]) -> bool:
    from cloud_agent import settings

    params = workflow["params"]
    if "stage_revision" not in workflow["data"]:
        name, previous = KEYS[params["key"]]
        revision, values = await _saved(storage)
        changes: dict[str, Any] = {}
        if key_identifier(str(values[name])) != params["new_key_id"]:
            if key_identifier(str(values[name])) != params["old_key_id"] or values[previous]:
                await save(
                    storage,
                    workflow,
                    state="failed",
                    detail="the key changed since this rotation started; nothing was changed",
                )
                return False
            changes = {name: _open(workflow["data"]["next_key"]), previous: values[name]}
            if params["key"] == "user":
                changes["credential_index_uses_previous"] = True
        if changes:
            revision, _encoded, _previous = await storage.settings.update_settings(
                settings.encode_changes(changes), expected_revision=revision
            )
        data = {k: v for k, v in workflow["data"].items() if k != "next_key"}
        if await save(storage, workflow, data={**data, "stage_revision": revision}) is None:
            return False
    if not await _everyone_adopted(
        storage, workflow, workflow["data"]["stage_revision"], "hold the new key and the old one"
    ):
        return False
    return (
        await save(
            storage,
            workflow,
            state="running",
            step="reencrypt",
            detail="",
            data={**workflow["data"], "family": 0, "cursor": "", "pass": 1, "totals": {}},
        )
        is not None
    )


async def _reencrypt(storage: Any, workflow: dict[str, Any]) -> bool:
    from cloud_agent import settings
    from cloud_agent.credential_families import families

    params = workflow["params"]
    if settings.adopted_revision() < workflow["data"]["stage_revision"] or (
        _current_key_id(params["key"]) != params["new_key_id"]
    ):
        return False  # this process has not adopted the new key yet; another tick will
    order = families(params["key"])
    for _ in range(BATCHES_PER_TICK):
        data = dict(workflow["data"])
        index = int(data["family"])
        if index >= len(order):
            break
        family = order[index]
        cursor, counts = await storage.credential_rotation.reencrypt_batch(
            family, _cipher_for(params["key"], family.domain), after=str(data["cursor"])
        )
        totals = dict(data.get("totals") or {})
        for name, count in counts.items():
            totals[name] = int(totals.get(name, 0)) + count
        data.update(totals=totals, cursor=cursor or "", family=index if cursor else index + 1)
        if (
            await save(
                storage,
                workflow,
                state="running",
                detail=f"re-encrypting {family.name}",
                data=data,
            )
            is None
        ):
            return False
    if int(workflow["data"]["family"]) < len(order):
        return False
    remaining, unreadable = 0, 0
    for family in order:
        result = await storage.credential_rotation.not_current(
            family, _cipher_for(params["key"], family.domain)
        )
        remaining += result["not_current"]
        unreadable += result["unreadable"]
    data = {**workflow["data"], "unreadable": unreadable}
    if remaining:
        if int(data["pass"]) >= MAX_PASSES:
            await save(
                storage,
                workflow,
                state="failed",
                detail=f"{remaining} stored values still do not open with the new key after "
                f"{MAX_PASSES} passes; the previous key is kept",
                data=data,
            )
            return False
        data.update(family=0, cursor="", **{"pass": int(data["pass"]) + 1})
        await save(storage, workflow, detail="another re-encryption pass", data=data)
        return False
    next_step = "switch_index" if params["key"] == "user" else "retire"
    return await save(storage, workflow, step=next_step, detail="", data=data) is not None


async def _settings_step(
    storage: Any,
    workflow: dict[str, Any],
    marker: str,
    changes: dict[str, Any],
    next_step: str | None,
    what: str,
) -> bool:
    from cloud_agent import settings

    if marker not in workflow["data"]:
        revision, _values = await _saved(storage)
        revision, _encoded, _previous = await storage.settings.update_settings(
            settings.encode_changes(changes), expected_revision=revision
        )
        if await save(storage, workflow, data={**workflow["data"], marker: revision}) is None:
            return False
    if not await _everyone_adopted(storage, workflow, workflow["data"][marker], what):
        return False
    if next_step is None:
        unreadable = int(workflow["data"].get("unreadable") or 0)
        await save(
            storage,
            workflow,
            state="succeeded",
            detail="every stored value is encrypted with the new key"
            + (
                f"; {unreadable} values could not be opened with either key and were left as they were"
                if unreadable
                else ""
            ),
        )
        return False
    return await save(storage, workflow, state="running", step=next_step, detail="") is not None


async def _reindex(storage: Any, workflow: dict[str, Any]) -> bool:
    from cloud_agent import settings
    from cloud_agent.environments import EnvironmentCipher

    if settings.adopted_revision() < workflow["data"]["index_revision"]:
        return False
    for _ in range(BATCHES_PER_TICK):
        data = dict(workflow["data"])
        owners = await storage.credential_rotation.fingerprint_owners(
            after=str(data.get("owner_cursor") or ""), limit=20
        )
        if not owners:
            return (
                await save(storage, workflow, state="running", step="retire", detail="", data=data)
                is not None
            )
        moved = dict(data.get("reindexed") or {})
        for owner in owners:
            counts = await storage.credential_rotation.reindex_owner(owner, EnvironmentCipher())
            for name, count in counts.items():
                moved[name] = int(moved.get(name, 0)) + count
        data.update(owner_cursor=owners[-1], reindexed=moved)
        if (
            await save(storage, workflow, detail="rebuilding environment fingerprints", data=data)
            is None
        ):
            return False
    return False


async def _advance(storage: Any, workflow: dict[str, Any]) -> None:
    params = workflow["params"]
    _name, previous = KEYS[params["key"]]
    while workflow["state"] in {"running", "waiting"}:
        step = workflow["step"]
        if step == "stage":
            moved = await _stage(storage, workflow)
        elif step == "reencrypt":
            moved = await _reencrypt(storage, workflow)
        elif step == "switch_index":
            moved = await _settings_step(
                storage,
                workflow,
                "index_revision",
                {"credential_index_uses_previous": False},
                "reindex",
                "build lookup digests with the new key",
            )
        elif step == "reindex":
            moved = await _reindex(storage, workflow)
        elif step == "retire":
            changes: dict[str, Any] = {previous: ""}
            if params["key"] == "user":
                changes["credential_index_uses_previous"] = False
            moved = await _settings_step(
                storage, workflow, "retire_revision", changes, None, "drop the previous key"
            )
        else:
            return
        if not moved:
            return


def register() -> None:
    """Add the credential key rotation's advancer."""
    ADVANCERS["credential_key"] = _advance


__all__ = ["CAPABILITY", "register", "start_credential_key_rotation"]
