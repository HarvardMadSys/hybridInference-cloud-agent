"""Key rotation workflows: each step ends on one-way key identifiers, never on a key.

``admin_token``          every control-plane machine writes the new ADMIN_TOKEN
                         (sealed to its agent) and restarts; done when every
                         current API and MCP process reports its identifier.
``root_key``             separate the root from AGENT_SESSION_SECRET (same
                         bytes) → stage the new key with the old one kept for
                         decrypting → re-encrypt both data families (saved
                         settings secrets, the archive access key) → retire the
                         old key. Refused unless every participant supports it.
``sandbox_manager_token`` every host accepts the next credential beside the
                         current one → the control plane switches its saved
                         credential → hosts promote it.
``control_signing``      the new signing key signs, the previous one keeps
                         verifying; ``retire`` (confirmed) stops it verifying.

The two credential encryption keys rotate in
:mod:`cloud_agent.application.credential_key_rotation`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from cloud_agent.application import deployments
from cloud_agent.application.workflows import (
    ADVANCERS,
    WorkflowRefused,
    current_intents,
    failure_detail,
    save,
    step_intents,
    step_state,
)

#: How long a verification step waits for every participant before failing.
VERIFY_TIMEOUT = timedelta(minutes=10)
ROTATION_CAPABILITY = "settings-key-rotation-v1"


def _cp_agents(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in agents if a["role"] == "control-plane" and a.get("revoked_at") is None]


def _host_agents(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in agents if a["role"] == "host" and a.get("revoked_at") is None]


def _require_online(agents: list[dict[str, Any]], what: str) -> None:
    if not agents:
        raise WorkflowRefused(f"no {what} machine has a deployment agent")
    offline = [a["name"] for a in agents if not deployments.online(a)]
    if offline:
        raise WorkflowRefused("these machines' agents are offline: " + ", ".join(offline))


def _require_sealed(
    agents: list[dict[str, Any]], sealed_by_agent: dict[str, Any], name: str
) -> None:
    missing = [a["name"] for a in agents if name not in (sealed_by_agent.get(a["agent_id"]) or {})]
    if missing:
        raise WorkflowRefused(
            f"{name} must be sealed to each agent; missing for: " + ", ".join(missing)
        )


async def _current_instances(storage: Any) -> list[dict[str, Any]]:
    return [i for i in await storage.instances.list_instances() if i["current"]]


def _timed_out(workflow: dict[str, Any]) -> bool:
    started = workflow["data"].get("verify_started_at")
    if not started:
        return False
    return datetime.now(UTC) - datetime.fromisoformat(started) > VERIFY_TIMEOUT


async def _verify(
    storage: Any, workflow: dict[str, Any], check: Any, next_step: str | None, waiting: str
) -> bool:
    """Wait for ``check(instances)`` to return no laggards, then move on."""
    instances = await _current_instances(storage)
    # No report at all proves nothing: it never counts as everyone confirming.
    laggards = check(instances) if instances else ["no control-plane process reports"]
    if not laggards:
        data = {**workflow["data"], "verify_started_at": None}
        if next_step is None:
            await save(
                storage,
                workflow,
                state="succeeded",
                detail="every participant confirmed",
                data=data,
            )
        else:
            await save(storage, workflow, state="running", step=next_step, detail="", data=data)
        return True
    data = dict(workflow["data"])
    data.setdefault("verify_started_at", datetime.now(UTC).isoformat())
    if _timed_out({**workflow, "data": data}):
        await save(
            storage,
            workflow,
            state="failed",
            detail=f"{waiting}: still not confirmed by " + ", ".join(laggards[:10]),
            data=data,
        )
        return False
    await save(
        storage,
        workflow,
        state="waiting",
        detail=f"{waiting}: " + ", ".join(laggards[:10]),
        data=data,
    )
    return False


def _label(instance: dict[str, Any]) -> str:
    return f"{instance['role']}@{instance['machine']}"


async def _run_agent_step(
    storage: Any,
    workflow: dict[str, Any],
    step: str,
    agent_ids: list[str],
    operation: str,
    params: dict[str, Any],
    sealed_for: Any = None,
) -> str:
    await step_intents(storage, workflow, step, agent_ids, operation, params, sealed_for=sealed_for)
    intents = await current_intents(storage, workflow, step)
    outcome = step_state(intents)
    if outcome == "failed":
        await save(
            storage,
            workflow,
            state="failed",
            detail=failure_detail(step, intents, workflow["params"].get("names", {})),
        )
    elif outcome == "waiting" and workflow["state"] != "waiting":
        await save(storage, workflow, state="waiting", detail=f"waiting for {step}")
    return outcome


# -- start ---------------------------------------------------------------


async def _prepare_admin_token(storage, agents, names, params, sealed_by_agent, secret):  # type: ignore[no-untyped-def]
    cp = _cp_agents(agents)
    _require_online(cp, "control-plane")
    _require_sealed(cp, sealed_by_agent, "ADMIN_TOKEN")
    if not isinstance(params.get("token_id"), str) or len(params["token_id"]) != 16:
        raise WorkflowRefused("token_id is the new token's identifier")
    return (
        {"token_id": params["token_id"], "agents": [a["agent_id"] for a in cp], "names": names},
        {},
        "apply",
    )


async def _prepare_root_key(storage, agents, names, params, sealed_by_agent, secret):  # type: ignore[no-untyped-def]
    from cloud_agent.instances import fleet_supports

    verdict = await fleet_supports(storage.instances, ROTATION_CAPABILITY)
    if not verdict.supported:
        raise WorkflowRefused("the root key cannot be rotated yet: " + verdict.reason())
    cp = _cp_agents(agents)
    _require_online(cp, "control-plane")
    _require_sealed(cp, sealed_by_agent, "AGENT_SETTINGS_ENCRYPTION_KEY")
    if not isinstance(params.get("new_key_id"), str) or len(params["new_key_id"]) != 16:
        raise WorkflowRefused("new_key_id is the new root key's identifier")
    prepared = {
        "new_key_id": params["new_key_id"],
        "agents": [a["agent_id"] for a in cp],
        "names": names,
    }
    return prepared, {}, "separate"


async def _prepare_sandbox_manager_token(storage, agents, names, params, sealed_by_agent, secret):  # type: ignore[no-untyped-def]
    from cloud_agent_shared.deployment_contract import key_identifier

    hosts = _host_agents(agents)
    _require_online(hosts, "host")
    _require_sealed(hosts, sealed_by_agent, "AGENT_SANDBOX_MANAGER_TOKEN_NEXT")
    covered = {str((a.get("report") or {}).get("runner_host") or "") for a in hosts}
    uncovered = [
        h["host"]
        for h in await storage.host_pool.list_runner_hosts()
        if h.get("status") in {"online", "draining"} and h["host"] not in covered
    ]
    if uncovered:
        raise WorkflowRefused(
            "these servers have no deployment agent, so their credential cannot be "
            "switched without an outage: " + ", ".join(uncovered[:10])
        )
    if not secret or len(secret.encode()) < 32:
        raise WorkflowRefused("the new credential must be at least 32 bytes")
    prepared = {
        "new_token_id": key_identifier(secret),
        "agents": [a["agent_id"] for a in hosts],
        "names": names,
    }
    return prepared, {"next_token": _seal_at_rest(secret)}, "stage"


async def _prepare_control_signing(storage, agents, names, params, sealed_by_agent, secret):  # type: ignore[no-untyped-def]
    if not secret or len(secret.encode()) < 32:
        raise WorkflowRefused("the new signing key must be at least 32 bytes")
    return {"names": names}, {"next_secret": _seal_at_rest(secret)}, "stage"


_PREPARE = {
    "admin_token": _prepare_admin_token,
    "root_key": _prepare_root_key,
    "sandbox_manager_token": _prepare_sandbox_manager_token,
    "control_signing": _prepare_control_signing,
}


async def start_rotation(
    storage: Any,
    *,
    kind: str,
    params: dict[str, Any],
    sealed_by_agent: dict[str, dict[str, Any]],
    secret: str | None,
    admin_id: str,
) -> dict[str, Any]:
    """Validate and start one rotation workflow."""
    prepare = _PREPARE.get(kind)
    if prepare is None:
        raise WorkflowRefused("unknown rotation")
    agents = await storage.deployments.list_agents()
    names = {a["agent_id"]: a["name"] for a in agents}
    prepared, data, first_step = await prepare(
        storage, agents, names, params, sealed_by_agent, secret
    )
    workflow = await storage.deployments.create_workflow(
        workflow_id=uuid.uuid4().hex,
        kind=kind,
        step=first_step,
        params=prepared,
        created_by=admin_id,
    )
    if workflow is None:
        raise WorkflowRefused("this rotation is already running")
    await save(storage, workflow, data={**data, "sealed_by_agent": sealed_by_agent})
    await ADVANCERS[kind](storage, workflow)
    return workflow


def _seal_at_rest(secret: str) -> dict[str, str]:
    """Keep a secret the workflow needs later encrypted under the settings root key."""
    from cloud_agent.credential_cipher import CredentialCipher
    from cloud_agent.domain_cipher import settings_root_key

    cipher = CredentialCipher(settings_root_key(), domain=b"cloud-agent/rotation-secret/v1")
    return {"ciphertext": cipher.encrypt(secret)}


def _open_at_rest(sealed: dict[str, str]) -> str:
    from cloud_agent.credential_cipher import CredentialCipher
    from cloud_agent.domain_cipher import decrypt_with_root

    return decrypt_with_root(
        lambda key: CredentialCipher(key, domain=b"cloud-agent/rotation-secret/v1"),
        sealed["ciphertext"],
    )


def _sealed_for(workflow: dict[str, Any], name: str) -> Any:
    sealed = workflow["data"].get("sealed_by_agent") or {}
    return lambda agent_id: {name: sealed[agent_id][name]}


def _forget_sealed(workflow: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in workflow["data"].items() if k != "sealed_by_agent"}


# -- advancers -----------------------------------------------------------


async def _advance_admin_token(storage: Any, workflow: dict[str, Any]) -> None:
    params = workflow["params"]
    if workflow["step"] == "apply":
        outcome = await _run_agent_step(
            storage,
            workflow,
            "apply",
            params["agents"],
            "apply_config",
            {"changes": {}},
            sealed_for=_sealed_for(workflow, "ADMIN_TOKEN"),
        )
        if outcome != "succeeded":
            if outcome == "failed":
                await save(
                    storage,
                    workflow,
                    detail=workflow["detail"]
                    + "; machines that applied it use the new token, the others keep the old one",
                    data=_forget_sealed(workflow),
                )
            return
        await save(
            storage,
            workflow,
            state="running",
            step="verify",
            detail="",
            data=_forget_sealed(workflow),
        )
    if workflow["step"] == "verify":
        await _verify(
            storage,
            workflow,
            lambda instances: [
                _label(i)
                for i in instances
                if i["details"].get("admin_token_id") != params["token_id"]
            ],
            None,
            "waiting for every API and MCP process to use the new ADMIN_TOKEN",
        )


async def _advance_root_key(storage: Any, workflow: dict[str, Any]) -> None:
    params = workflow["params"]
    agents = params["agents"]
    while workflow["state"] in {"running", "waiting"}:
        step = workflow["step"]
        if step == "separate":
            outcome = await _run_agent_step(
                storage,
                workflow,
                "separate",
                agents,
                "rotation_step",
                {
                    "workflow": "root_key",
                    "operation_id": workflow["workflow_id"],
                    "step": "separate",
                },
            )
            if outcome != "succeeded":
                return
            if not await _verify(
                storage,
                workflow,
                lambda instances: [
                    _label(i)
                    for i in instances
                    if i["details"].get("root_key_source") != "dedicated"
                ],
                "stage",
                "waiting for every process to use a dedicated root key",
            ):
                return
            instances = await _current_instances(storage)
            ids = {i["details"].get("root_key_id") for i in instances}
            if len(ids) != 1:
                await save(
                    storage,
                    workflow,
                    state="failed",
                    detail="the processes report different root keys",
                )
                return
            await save(storage, workflow, data={**workflow["data"], "old_key_id": ids.pop()})
        elif step == "stage":
            outcome = await _run_agent_step(
                storage,
                workflow,
                "stage",
                agents,
                "rotation_step",
                {"workflow": "root_key", "operation_id": workflow["workflow_id"], "step": "stage"},
                sealed_for=_sealed_for(workflow, "AGENT_SETTINGS_ENCRYPTION_KEY"),
            )
            if outcome != "succeeded":
                return
            old = workflow["data"].get("old_key_id")
            if not await _verify(
                storage,
                workflow,
                lambda instances, old=old: [
                    _label(i)
                    for i in instances
                    if i["details"].get("root_key_id") != params["new_key_id"]
                    or i["details"].get("root_key_previous_id") != old
                ],
                "reencrypt",
                "waiting for every process to hold the new root key and keep the old one for decrypting",
            ):
                return
            await save(storage, workflow, data=_forget_sealed(workflow))
        elif step == "reencrypt":
            from cloud_agent.instances import root_key_state

            if root_key_state().get("root_key_id") != params["new_key_id"]:
                # This process has not restarted on the new key; another one will.
                return
            counts = await reencrypt_root_families(storage)
            await save(
                storage,
                workflow,
                state="running",
                step="retire",
                detail="",
                data={**workflow["data"], "reencrypted": counts},
            )
        elif step == "retire":
            outcome = await _run_agent_step(
                storage,
                workflow,
                "retire",
                agents,
                "rotation_step",
                {"workflow": "root_key", "operation_id": workflow["workflow_id"], "step": "retire"},
            )
            if outcome != "succeeded":
                return
            await _verify(
                storage,
                workflow,
                lambda instances: [
                    _label(i) for i in instances if i["details"].get("root_key_previous_id")
                ],
                None,
                "waiting for every process to drop the old root key",
            )
            return


async def reencrypt_root_families(storage: Any) -> dict[str, int]:
    """Re-encrypt every value the settings root key protects, then prove it opens with the new key alone.

    The two families are the saved settings' secret fields and the sandbox
    archive access key in the runner policy. Each is rewritten from the value
    decrypted with the current-then-previous key, then decrypted again with
    the current key only; any failure stops the workflow before the old key is
    retired.
    """
    from cloud_agent import settings
    from cloud_agent.credential_cipher import CredentialCipher
    from cloud_agent.domain_cipher import settings_root_key

    revision, encoded = await storage.settings.read_settings()
    values = settings._decode(encoded)
    secrets_ = {s.key: values[s.key] for s in settings.SETTINGS if s.secret and values[s.key]}
    if secrets_:
        changes = settings.encode_changes(secrets_)
        revision, encoded, _previous = await storage.settings.update_settings(
            changes, expected_revision=revision
        )
    root = settings_root_key()
    for setting in settings.SETTINGS:
        stored = encoded.get(setting.storage_key)
        if setting.secret and isinstance(stored, dict) and "ciphertext" in stored:
            CredentialCipher(
                root, domain=b"cloud-agent/admin-settings/v1/" + setting.storage_key.encode()
            ).decrypt(stored["ciphertext"])
    archive = await storage.root_key_families.reseal_sandbox_archive_secret()
    return {"settings": len(secrets_), "archive": int(archive)}


async def _advance_sandbox_manager_token(storage: Any, workflow: dict[str, Any]) -> None:
    from cloud_agent import settings

    params = workflow["params"]
    agents = params["agents"]
    while workflow["state"] in {"running", "waiting"}:
        step = workflow["step"]
        if step == "stage":
            outcome = await _run_agent_step(
                storage,
                workflow,
                "stage",
                agents,
                "rotation_step",
                {
                    "workflow": "sandbox_manager_token",
                    "operation_id": workflow["workflow_id"],
                    "step": "stage",
                },
                sealed_for=_sealed_for(workflow, "AGENT_SANDBOX_MANAGER_TOKEN_NEXT"),
            )
            if outcome != "succeeded":
                return
            reports = {
                a["agent_id"]: a.get("report") or {}
                for a in await storage.deployments.list_agents()
            }
            lagging = [
                params["names"].get(a, a)
                for a in agents
                if (reports.get(a, {}).get("secret_ids") or {}).get(
                    "AGENT_SANDBOX_MANAGER_TOKEN_NEXT"
                )
                != params["new_token_id"]
            ]
            if lagging:
                await save(
                    storage,
                    workflow,
                    state="waiting",
                    detail="waiting for hosts to report the next credential: " + ", ".join(lagging),
                )
                return
            await save(
                storage,
                workflow,
                state="running",
                step="switch",
                detail="",
                data=_forget_sealed(workflow),
            )
        elif step == "switch":
            secret = _open_at_rest(workflow["data"]["next_token"])
            revision, _encoded = await storage.settings.read_settings()
            new_revision, _encoded, _previous = await storage.settings.update_settings(
                settings.encode_changes({"sandbox_manager_token": secret}),
                expected_revision=revision,
            )
            data = {k: v for k, v in workflow["data"].items() if k != "next_token"}
            await save(
                storage,
                workflow,
                state="running",
                step="adopt",
                detail="",
                data={**data, "switch_revision": new_revision},
            )
        elif step == "adopt":
            target = workflow["data"]["switch_revision"]
            if not await _verify(
                storage,
                workflow,
                lambda instances, target=target: [
                    _label(i) for i in instances if i["adopted_settings_revision"] < target
                ],
                "promote",
                "waiting for every process to use the new credential",
            ):
                return
        elif step == "promote":
            outcome = await _run_agent_step(
                storage,
                workflow,
                "promote",
                agents,
                "rotation_step",
                {
                    "workflow": "sandbox_manager_token",
                    "operation_id": workflow["workflow_id"],
                    "step": "promote",
                },
            )
            if outcome == "succeeded":
                await save(
                    storage,
                    workflow,
                    state="succeeded",
                    detail="every host uses the new credential",
                )
            return


async def _advance_control_signing(storage: Any, workflow: dict[str, Any]) -> None:
    from cloud_agent import settings

    if workflow["step"] == "stage":
        revision, encoded = await storage.settings.read_settings()
        current = settings._decode(encoded)["control_signing_secret"]
        secret = _open_at_rest(workflow["data"]["next_secret"])
        new_revision, _encoded, _previous = await storage.settings.update_settings(
            settings.encode_changes(
                {"control_signing_secret": secret, "control_signing_secret_previous": current}
            ),
            expected_revision=revision,
        )
        data = {k: v for k, v in workflow["data"].items() if k != "next_secret"}
        await save(
            storage,
            workflow,
            state="running",
            step="adopt",
            detail="",
            data={**data, "stage_revision": new_revision},
        )
    if workflow["step"] == "adopt":
        target = workflow["data"]["stage_revision"]
        await _verify(
            storage,
            workflow,
            lambda instances, target=target: [
                _label(i) for i in instances if i["adopted_settings_revision"] < target
            ],
            "retire_pending",
            "waiting for every process to sign with the new key",
        )
    if workflow["step"] == "retire_pending" and workflow["state"] != "waiting":
        await save(
            storage,
            workflow,
            state="waiting",
            detail=(
                "the new key signs and the previous one still verifies. Re-enroll every host, "
                "then retire the previous key; credentials signed before the rotation stop working then"
            ),
        )


async def retire_control_signing(storage: Any, workflow_id: str) -> dict[str, Any]:
    """Stop verifying with the previous signing key (an administrator's confirmed step)."""
    from cloud_agent import settings

    workflow = await storage.deployments.get_workflow(workflow_id)
    if (
        workflow is None
        or workflow["kind"] != "control_signing"
        or workflow["step"] != "retire_pending"
    ):
        raise WorkflowRefused("this signing key rotation is not waiting to retire")
    revision, _encoded = await storage.settings.read_settings()
    await storage.settings.update_settings(
        settings.encode_changes({"control_signing_secret_previous": ""}), expected_revision=revision
    )
    await save(
        storage,
        workflow,
        state="succeeded",
        step="retired",
        detail="the previous signing key no longer verifies",
    )
    return workflow


def register_advancers() -> None:
    """Add every rotation's advancer to the workflow engine."""
    from cloud_agent.application.credential_key_rotation import register as register_credential
    from cloud_agent.application.database_workflows import register as register_database

    ADVANCERS["admin_token"] = _advance_admin_token
    ADVANCERS["root_key"] = _advance_root_key
    ADVANCERS["sandbox_manager_token"] = _advance_sandbox_manager_token
    ADVANCERS["control_signing"] = _advance_control_signing
    register_database()
    register_credential()


__all__ = [
    "ROTATION_CAPABILITY",
    "reencrypt_root_families",
    "register_advancers",
    "retire_control_signing",
    "start_rotation",
]
