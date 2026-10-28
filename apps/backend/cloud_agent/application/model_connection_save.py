"""Saving an owner's model connection: verify what the save would add, then commit it.

There is no unverified connection and no manual declaration of APIs. A create,
and an edit that changes the key or Base URL or adds models, verifies the
models it would list *inside the saving request*, against the exact
normalized endpoint, key and model ids it is about to commit, for the
authenticated owner. Nothing a client sends about capabilities is read, and no
earlier result is reused for a new endpoint. Only after every verified model
has an agent that can use it is anything written:

- a create inserts the row, its encrypted key and its facts at once; a failed
  verification stores nothing, not even the key;
- an edit commits only while the row is still the one the verification read
  (both revisions and the model list), so a rotation, disable, delete or model
  edit made meanwhile is never undone by a late result.

Each verified model costs one of the owner's hourly checks, all reserved before
any provider call, whether or not a connection exists yet. No database
transaction is open during the calls. A rename, removing models, enabling or
disabling needs no verification and makes no provider call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent.application.model_connection_check import CHECKS_PER_HOUR, verify_models
from cloud_agent.application.model_connection_status import verification_status
from cloud_agent.domain.model_connections import current_check

if TYPE_CHECKING:
    from cloud_agent.ports import ModelConnectionRepository


class CheckLimitReached(Exception):
    """The owner's hourly checks cannot cover this save; no provider was called."""


class VerificationRefused(Exception):
    """A model the save would list has no agent that can use it; nothing was written."""

    def __init__(self, statuses: list[dict[str, Any]]) -> None:
        """Keep every verified model's status, the usable ones too, for the owner."""
        super().__init__("verification refused")
        self.statuses = statuses


@dataclass(frozen=True)
class EditPlan:
    """What an edit actually changes, and which models it must verify first."""

    #: Stored columns that differ, except the key (``new_key``).
    changes: dict[str, Any] = field(default_factory=dict)
    #: A key that differs from the stored one, in plaintext; ``None`` keeps it.
    new_key: str | None = None
    #: The models to verify before committing; empty commits without a call.
    verify: list[str] = field(default_factory=list)

    @property
    def endpoint_changed(self) -> bool:
        """Whether the Base URL or the key changes, which re-verifies every model."""
        return "base_url" in self.changes or self.new_key is not None


def plan_edit(
    row: dict[str, Any],
    *,
    requested: dict[str, Any],
    new_key: str | None,
) -> EditPlan:
    """The real changes ``requested`` makes to ``row``, and what to verify.

    ``requested`` holds normalized ``name``, ``base_url``, ``models`` and
    ``enabled``, each only when sent. A value equal to the stored one is not
    a change. ``new_key`` is a key the caller found to differ from the stored
    one. A new endpoint verifies every model the edit would list; otherwise
    only the added models are verified.
    """
    changes = {
        column: value
        for column, value in requested.items()
        if value is not None and value != row.get(column)
    }
    models = list(changes.get("models", row["models"]))
    stored = set(row["models"])
    endpoint = "base_url" in changes or new_key is not None
    verify = models if endpoint else [model for model in models if model not in stored]
    return EditPlan(changes=changes, new_key=new_key, verify=verify)


def kept_checks(row: dict[str, Any], models: list[str]) -> dict[str, dict[str, Any]]:
    """The current facts of those of ``models`` the row already holds, for an unchanged endpoint."""
    kept: dict[str, dict[str, Any]] = {}
    for model in models:
        entry = current_check(row, model)
        if entry is not None:
            kept[model] = {key: value for key, value in entry.items() if key != "revision"}
    return kept


async def verify_for_save(
    store: ModelConnectionRepository,
    *,
    user_id: str,
    base_url: str,
    api_key: str,
    models: list[str],
) -> dict[str, dict[str, Any]]:
    """Verify ``models`` at this endpoint for a save, or refuse it.

    Reserves one hourly check per model, all or none, before any call
    (``CheckLimitReached``). Answers each model's native facts when every one
    has an agent that can use it; otherwise raises ``VerificationRefused``
    with every model's status. A failing model is never dropped from the save.
    """
    reserved = await store.reserve_model_connection_checks(
        user_id=user_id, count=len(models), limit=CHECKS_PER_HOUR, seconds=3600
    )
    if not reserved:
        raise CheckLimitReached
    results = await verify_models(base_url=base_url, api_key=api_key, models=models)
    statuses = [verification_status(model, results[model]) for model in models]
    if any(not status["runtimes"] for status in statuses):
        raise VerificationRefused(statuses)
    return results


__all__ = [
    "CheckLimitReached",
    "EditPlan",
    "VerificationRefused",
    "kept_checks",
    "plan_edit",
    "verify_for_save",
]
