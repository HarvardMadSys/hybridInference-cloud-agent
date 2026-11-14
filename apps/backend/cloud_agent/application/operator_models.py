"""Models the deployment provides: what an owner is offered, and closing what a change left behind.

An administrator configures a *provided* connection: a provider's Base URL,
the deployment's own key and the models it lists. Every admitted owner may run
turns on it with no setup, within a daily request allowance per model. Turns
resolve it through :mod:`cloud_agent.application.model_connections`, like an
owner's own connection; the model relay (:mod:`cloud_agent.api.model_relay`)
is what holds the key. This module is the rest: what the composer is told, and
what happens to running processes when the connection changes.

See ``docs/agents/specs/2026-11-12-platform-model-relay.zh-CN.md``.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent.application.model_connection_status import model_efforts, usable_runtimes
from cloud_agent.application.residency import close_released_residencies
from cloud_agent.domain.operator_models import next_reset
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.ports import HostPoolRepository, OperatorModelRepository

logger = get_logger(__name__)


class OperatorModelStorage(Protocol):
    """What reading and invalidating a provided connection reaches for."""

    operator_models: OperatorModelRepository
    host_pool: HostPoolRepository


def new_operator_connection_id() -> str:
    """A fresh id for a provided connection."""
    return f"oconn_{secrets.token_hex(16)}"


async def provided_sources(
    operator_models: OperatorModelRepository, *, user_id: str
) -> list[dict[str, Any]]:
    """Every provided connection as ``user_id``'s composer offers it.

    Each enabled connection with the models this owner may use: one whose
    allowance is zero, or has none set, is left out as if it were not
    provided, and a connection left with no model is not listed at all. The
    row is narrowed to those models, so the helpers that decide which agents
    can run what answer for exactly what is offered.
    """
    sources: list[dict[str, Any]] = []
    for row in await operator_models.operator_sources_for_user(user_id=user_id):
        allowances = row["allowances"]
        offered = [model for model in row["models"] if allowances[model].offered]
        if not offered:
            continue
        narrowed = {**row, "models": offered}
        default_model = row.get("default_model")
        runtimes = usable_runtimes(narrowed)
        sources.append(
            {
                "id": row["id"],
                "name": row["name"],
                "is_default": bool(row.get("is_default")),
                "default_model": default_model if default_model in runtimes else None,
                "models": offered,
                "enabled": True,
                "model_runtimes": runtimes,
                "model_efforts": model_efforts(narrowed),
                "allowances": {
                    model: {
                        "daily_requests": allowances[model].limit,
                        "used_today": allowances[model].used,
                    }
                    for model in offered
                },
                "resets_at": next_reset(),
            }
        )
    return sources


def provided_default(sources: list[dict[str, Any]]) -> tuple[str, str] | None:
    """``(model, connection_id)`` an owner with no history starts on, when there is one.

    The default connection's default model, while this owner may use it and
    has requests left today.
    """
    for source in sources:
        model = source.get("default_model")
        if not source.get("is_default") or not model:
            continue
        allowance = source["allowances"][model]
        limit = allowance["daily_requests"]
        if limit is None or allowance["used_today"] < limit:
            return str(model), str(source["id"])
    return None


async def invalidate_operator_connection(store: OperatorModelStorage, *, connection_id: str) -> int:
    """Close every member process still running on an older state of a provided connection.

    Called after the change committed, as for an owner's connection: a
    residency bound before it is found here, and one bound after was bound at
    the new revision or refused. Only a new Base URL, disabling and deleting
    leave anything to close; a rotated key does not, since no process holds
    the key.
    """
    released = await store.operator_models.release_stale_operator_residencies(
        connection_id=connection_id
    )
    if released:
        logger.info(
            "agent_operator_model_connection_residencies_released",
            extra={
                "event": "agent_operator_model_connection_residencies_released",
                "connection_id": connection_id,
                "count": len(released),
            },
        )
        await close_released_residencies(store.host_pool, released)
    return len(released)


__all__ = [
    "OperatorModelStorage",
    "invalidate_operator_connection",
    "new_operator_connection_id",
    "provided_default",
    "provided_sources",
]
