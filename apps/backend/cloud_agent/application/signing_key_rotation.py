"""Rotate the host and worker signing key without making any host re-enroll.

``stage``         the new key signs; the previous one keeps verifying
                  (``control_signing_secret_previous``).
``adopt``         every current process has adopted that; the moment is kept as
                  ``signing_since``.
``renew_hosts``   every enrolled host holds a credential the new key minted.
                  Hosts renew at their heartbeats (``api.hosts``); an offline
                  host, a runner that cannot renew, or an unknown key keeps this
                  waiting, named, until it comes back or is removed.
``drain_tokens``  no running attempt or hot residency started before
                  ``signing_since``: their tokens may be the previous key's.
``retire``        the previous key and every host's replaced credential stop
                  verifying; done when every process has adopted that.

The waits have no time limit: retiring early is the outage this replaces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cloud_agent.application.key_rotation import _label, _open_at_rest, _verify
from cloud_agent.application.workflows import WorkflowRefused, save


async def _stage(storage: Any, workflow: dict[str, Any]) -> bool:
    from cloud_agent import settings

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
    return (
        await save(
            storage,
            workflow,
            state="running",
            step="adopt",
            detail="",
            data={**data, "stage_revision": new_revision},
        )
        is not None
    )


def _behind(target: int) -> Any:
    return lambda instances: [
        _label(i) for i in instances if i["adopted_settings_revision"] < target
    ]


async def _adopt(storage: Any, workflow: dict[str, Any]) -> bool:
    if not await _verify(
        storage,
        workflow,
        _behind(workflow["data"]["stage_revision"]),
        "renew_hosts",
        "waiting for every process to sign with the new key",
    ):
        return False
    # From here on every process signs with the new key: a token signed
    # before this moment may still be the previous key's.
    return (
        await save(
            storage,
            workflow,
            data={**workflow["data"], "signing_since": datetime.now(UTC).isoformat()},
        )
        is not None
    )


def host_lag(host: dict[str, Any]) -> str:
    """Why one host does not hold a credential the new key minted yet."""
    name = str(host["host"])
    if host["status"] != "online":
        return f"{name} is {host['status']}"
    if not host["renews"]:
        return (
            f"{name} cannot renew its credential (its runner predates renewal, or its "
            "credential is injected); re-enroll it"
        )
    if host["renewal_pending"]:
        return f"{name} has not used its renewed credential yet"
    return f"{name} has not received a renewal yet"


async def _wait(storage: Any, workflow: dict[str, Any], detail: str) -> bool:
    if workflow["state"] != "waiting" or workflow["detail"] != detail:
        await save(storage, workflow, state="waiting", detail=detail)
    return False


async def _renew_hosts(storage: Any, workflow: dict[str, Any]) -> bool:
    key_id = workflow["params"]["new_key_id"]
    lagging = [
        host_lag(h)
        for h in await storage.host_credentials.credential_status()
        if h["credential_key_id"] != key_id
    ]
    if lagging:
        return await _wait(
            storage,
            workflow,
            "waiting for these servers to switch to a renewed credential: "
            + "; ".join(lagging[:10]),
        )
    return (
        await save(storage, workflow, state="running", step="drain_tokens", detail="") is not None
    )


async def _drain_tokens(storage: Any, workflow: dict[str, Any]) -> bool:
    held = await storage.host_credentials.tokens_signed_before(
        datetime.fromisoformat(workflow["data"]["signing_since"])
    )
    if held["attempts"] or held["residencies"]:
        return await _wait(
            storage,
            workflow,
            f"waiting for {held['attempts']} running turns and {held['residencies']} resident "
            "sessions that started before the new key signed",
        )
    return await save(storage, workflow, state="running", step="retire", detail="") is not None


async def _retire(storage: Any, workflow: dict[str, Any]) -> bool:
    from cloud_agent import settings

    if "retire_revision" not in workflow["data"]:
        revision, _encoded = await storage.settings.read_settings()
        retired, _encoded, _previous = await storage.settings.update_settings(
            settings.encode_changes({"control_signing_secret_previous": ""}),
            expected_revision=revision,
        )
        await storage.host_credentials.clear_previous()
        if (
            await save(storage, workflow, data={**workflow["data"], "retire_revision": retired})
            is None
        ):
            return False
    await _verify(
        storage,
        workflow,
        _behind(workflow["data"]["retire_revision"]),
        None,
        "waiting for every process to stop verifying with the previous key",
    )
    return False


_STEPS = {
    "stage": _stage,
    "adopt": _adopt,
    "renew_hosts": _renew_hosts,
    "drain_tokens": _drain_tokens,
    "retire": _retire,
}


async def advance_control_signing(storage: Any, workflow: dict[str, Any]) -> None:
    """Advance as many steps as the evidence allows."""
    while workflow["state"] in {"running", "waiting"}:
        step = _STEPS.get(workflow["step"])
        if step is None or not await step(storage, workflow):
            return


async def retire_control_signing(storage: Any, workflow_id: str) -> dict[str, Any]:
    """Re-check a signing key rotation now; it retires the previous key only on evidence.

    Kept for the earlier route, which no longer skips the hosts' renewal or
    the tokens still signed with the previous key.
    """
    workflow = await storage.deployments.get_workflow(workflow_id)
    if workflow is None or workflow["kind"] != "control_signing":
        raise WorkflowRefused("this is not a signing key rotation")
    await advance_control_signing(storage, workflow)
    if workflow["state"] != "succeeded" and workflow["step"] != "retire":
        raise WorkflowRefused(
            "the previous key is retired only once nothing needs it: " + workflow["detail"]
        )
    return workflow


__all__ = ["advance_control_signing", "host_lag", "retire_control_signing"]
