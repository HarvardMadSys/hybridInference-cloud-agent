"""Owner-only choice of the model a new task starts on.

Two routes, both scoped to the signed-in owner:

* ``GET`` answers the pin, the model their newest turn used, which of the two
  a new task would start on, and the models this owner may choose from. The
  Settings card needs all of them to explain what "follow my last model"
  currently resolves to, and reading the catalog here spares it the whole
  composer configuration.
* ``PUT`` pins a model, or clears the pin with ``null``.

**A pin is checked against the catalog, not stored blindly.** The gateway owns
which models this owner may call, and a pin naming a retired model would make
every new task start on something the create endpoint refuses. The check is
the create endpoint's own predicate, so the two cannot disagree.

**Local accounts have no pin.** A pin names a Gateway model, and with
``AGENT_AUTH_MODE=local`` there are none: both routes answer 409
``model_connection_required``, without asking any catalog. A new task there
starts on the owner's last connection model, which ``/config`` reports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import Field

from cloud_agent.accounts.config import local_mode
from cloud_agent.accounts.directory import ModelConnectionRequired
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_gateway_client, get_storage, require_store
from cloud_agent.application.job_admission import model_catalog_unavailable
from cloud_agent.model_preference import (
    ModelPreferenceView,
    UpdateModelPreference,
    deployment_default_model,
    resolve_model,
)
from cloud_agent.visible_models import (
    ModelCatalogUnavailable,
    agent_canonical_model,
    agent_visible_models,
)

if TYPE_CHECKING:
    from cloud_agent.ports.model_preferences import ModelPreferenceRepository


router = APIRouter(prefix="/v1/agent/model-preference", tags=["model-preference"])


def _refuse_local_pin() -> None:
    """A local account has no Gateway model to pin; see the module docstring."""
    if local_mode():
        raise model_catalog_unavailable(ModelConnectionRequired())


class _ModelPreferenceStorage(Protocol):
    """The one capability this surface reaches the store for."""

    model_preferences: ModelPreferenceRepository


class ModelPreferenceResponse(ModelPreferenceView):
    """The preference, resolved, beside the catalog it was resolved against."""

    models: list[str] = Field(
        default_factory=list,
        description=(
            "The models this owner may pin, in the gateway's order. Empty means "
            "the catalog could not be read, not that a pin has been retired."
        ),
    )


def _view(preference: ModelPreferenceView, models: list[str]) -> ModelPreferenceResponse:
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
    return ModelPreferenceResponse(
        default_model=preference.default_model,
        last_model=preference.last_model,
        # Without it a model last run on the owner's connection reads as the
        # Gateway model of that name, which a new task will not start on.
        last_model_connection_id=preference.last_model_connection_id,
        effective_model=resolve_model(models, preference, deployment_default),
        deployment_default=(deployment_default if deployment_default in models else None),
        models=models,
    )


@router.get("", response_model=ModelPreferenceResponse)
async def read_model_preference(
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ModelPreferenceStorage | None = Depends(get_storage),
    gateway: Any = Depends(get_gateway_client),
) -> ModelPreferenceResponse:
    """Read what a new task would start on, and where that came from."""
    response.headers["Cache-Control"] = "no-store"
    _refuse_local_pin()
    preference = await require_store(store).model_preferences.get_model_preference(user["user_id"])
    return _view(preference, await agent_visible_models(gateway, user_id=user["user_id"]))


@router.put("", response_model=ModelPreferenceResponse)
async def save_model_preference(
    body: UpdateModelPreference,
    response: Response,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ModelPreferenceStorage | None = Depends(get_storage),
    gateway: Any = Depends(get_gateway_client),
) -> ModelPreferenceResponse:
    """Pin the model a new task starts on, or go back to following the last one.

    ``{"default_model": null}`` is a real choice, distinct from omitting the
    field: it clears the pin. The field is required so a client cannot clear a
    choice by forgetting to send one.
    """
    response.headers["Cache-Control"] = "no-store"
    _refuse_local_pin()
    models = await agent_visible_models(gateway, user_id=user["user_id"])
    requested = body.default_model
    if requested is not None:
        # Canonical ids only: the pin is applied to the composer's model picker,
        # whose options are canonical, and an alias would pre-select nothing.
        # An outage is no verdict on the model: answer it the way task
        # creation does, as a 503 the Settings card can tell from a refusal.
        try:
            canonical = await agent_canonical_model(requested, gateway, user_id=user["user_id"])
        except ModelCatalogUnavailable as exc:
            raise model_catalog_unavailable(exc) from None
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
