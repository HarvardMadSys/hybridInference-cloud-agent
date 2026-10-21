"""Owner-only choice of the model a new task starts on.

Two routes, both scoped to the signed-in owner:

* ``GET`` answers the pin, the model their newest turn used, and which of the
  two a new task would start on. The Settings card needs all three to explain
  what "follow my last model" currently resolves to.
* ``PUT`` pins a model, or clears the pin with ``null``.

**A pin is checked against the catalog, not stored blindly.** The gateway owns
which models this owner may call, and a pin naming a retired model would make
every new task start on something the create endpoint refuses. The check is
the create endpoint's own predicate, so the two cannot disagree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Response

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_gateway_client, get_storage, require_store
from cloud_agent.model_preference import (
    ModelPreferenceView,
    UpdateModelPreference,
    deployment_default_model,
    resolve_model,
)
from cloud_agent.visible_models import agent_canonical_model, agent_visible_models

if TYPE_CHECKING:
    from cloud_agent.ports.model_preferences import ModelPreferenceRepository


router = APIRouter(prefix="/v1/agent/model-preference", tags=["model-preference"])


class _ModelPreferenceStorage(Protocol):
    """The one capability this surface reaches the store for."""

    model_preferences: ModelPreferenceRepository


def _view(preference: ModelPreferenceView, models: list[str]) -> ModelPreferenceView:
    """Attach what a new task would actually start on, narrowed to the catalog.

    The pin and the last-used model are reported exactly as recorded — both are
    facts, and the settings card needs them to explain a pin the deployment has
    since retired. Only ``effective_model`` is narrowed: it is the one value the
    composer pre-selects, so it must be one the create endpoint accepts.

    The deployment default is narrowed too, and with the same rule: a card that
    named a model the user cannot call would explain "follow my last model" by
    pointing somewhere the task will not run.
    """
    deployment_default = deployment_default_model()
    return ModelPreferenceView(
        default_model=preference.default_model,
        last_model=preference.last_model,
        effective_model=resolve_model(models, preference, deployment_default),
        deployment_default=(deployment_default if deployment_default in models else None),
    )


@router.get("", response_model=ModelPreferenceView)
async def read_model_preference(
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ModelPreferenceStorage | None = Depends(get_storage),
    gateway: Any = Depends(get_gateway_client),
) -> ModelPreferenceView:
    """Read what a new task would start on, and where that came from."""
    response.headers["Cache-Control"] = "no-store"
    preference = await require_store(store).model_preferences.get_model_preference(user["user_id"])
    return _view(preference, await agent_visible_models(gateway, user_id=user["user_id"]))


@router.put("", response_model=ModelPreferenceView)
async def save_model_preference(
    body: UpdateModelPreference,
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ModelPreferenceStorage | None = Depends(get_storage),
    gateway: Any = Depends(get_gateway_client),
) -> ModelPreferenceView:
    """Pin the model a new task starts on, or go back to following the last one.

    ``{"default_model": null}`` is a real choice, distinct from omitting the
    field: it clears the pin. The field is required so a client cannot clear a
    choice by forgetting to send one.
    """
    response.headers["Cache-Control"] = "no-store"
    models = await agent_visible_models(gateway, user_id=user["user_id"])
    requested = body.default_model
    if requested is not None:
        # Canonical ids only: the pin is applied to the composer's model picker,
        # whose options are canonical, and an alias would pre-select nothing.
        canonical = await agent_canonical_model(requested, gateway, user_id=user["user_id"])
        if canonical is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "model_not_available",
                        "message": (
                            f"Model {requested!r} is not available to agent jobs on this "
                            f"deployment. Available: {', '.join(models[:20]) or 'none'}."
                        ),
                    }
                },
            )
        requested = canonical
    saved = await require_store(store).model_preferences.set_default_model(
        user["user_id"], requested
    )
    return _view(saved, models)
