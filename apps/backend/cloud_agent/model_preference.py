"""Which model a new task should start on, and every layer that decides it.

Four layers, in priority order, first offered one wins:

1. ``default_model`` — the owner's explicit pin, set in My Settings.
2. ``last_model`` — the model their most recent task actually ran on, which is
   the behaviour somebody means by "remember my model".
3. The deployment default an administrator saves under Execution.
4. Nothing at all: the model control stays empty and the task waits for the
   owner to choose. There is deliberately no "first model in the list"
   fallback — see :func:`resolve_model`.

**The deployment default sits below history on purpose.** Lowering it to second
place would mean an administrator changing one setting silently moves every
existing user onto a different model — a change nobody asked for, on tasks
already under way. Below history, it only reaches users who have no history to
lose, which is exactly the population it is meant to serve.

None of the four is authority. Each is a hint, applied only when the gateway
still offers that model to this owner: a retired pin falls through to the next
layer, and a retired last-used model falls through again. Nothing is ever
pre-selected from a model the create endpoint would refuse — and when every
layer is unavailable, nothing is pre-selected at all.

``last_model`` is **derived, never written**: it is read from the owner's
newest turn, so the single source of truth stays the job row that was really
created. It carries that turn's source: a model last run on the owner's own
connection is offered again on that connection, never as the Gateway model of
the same name, and the pin and the deployment default are Gateway models. A separate "last chosen" column would need a write on every creation
path and would disagree with the jobs table the moment one of them missed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent import settings as deployment_settings

#: Long enough for any gateway model id, short enough to stay a scalar.
MAX_MODEL_NAME = 200


class ModelPreferenceView(BaseModel):
    """An owner's pinned model, the one they used last, and what that resolves to."""

    #: The pin exactly as saved. Reported even when the catalog no longer offers
    #: it, so the settings card can say *why* it is not being applied.
    default_model: str | None = None
    last_model: str | None = None
    #: The owner connection ``last_model`` ran on; null is the Gateway.
    last_model_connection_id: str | None = None
    #: The model a new task would actually start on, narrowed to the catalog.
    #: Null only when no layer resolves to something this owner can call.
    effective_model: str | None = None
    #: The deployment's own starting point, for a user who has chosen nothing.
    #: Reported so "follow my last model" can say where it lands before they
    #: have any history; null means this deployment set no default.
    deployment_default: str | None = None

    @classmethod
    def of(
        cls,
        default_model: str | None,
        last_model: str | None,
        last_model_connection_id: str | None = None,
    ) -> ModelPreferenceView:
        """Describe both stored facts, before any catalog is consulted.

        ``effective_model`` here is the unresolved preference — the pin, else
        the remembered model. ``None`` means this owner has expressed no choice
        yet. Callers that have the catalog narrow it with
        :func:`resolve_model`.
        """
        return cls(
            default_model=default_model,
            last_model=last_model,
            last_model_connection_id=last_model_connection_id,
            effective_model=default_model or last_model,
        )


class UpdateModelPreference(BaseModel):
    """Pin a default model, or (``null``) go back to following the last used one.

    The field is required rather than defaulting to ``None``: omitting it is a
    malformed request, while sending ``null`` is the deliberate act of clearing
    the pin. Treating the two alike would let a client clear a choice by
    forgetting to send one.
    """

    model_config = ConfigDict(extra="forbid")

    default_model: str | None = Field(..., max_length=MAX_MODEL_NAME)


def resolve_model(
    models: list[str],
    preference: ModelPreferenceView,
    deployment_default: str | None = None,
) -> str | None:
    """The first of the four layers this owner can still reach.

    Order matters and is the whole rule: an explicit pin outranks history, and
    history outranks the deployment default. A choice that is no longer offered
    does not shadow a lower layer that is.

    Empty ``models`` means the catalog could not be read, and then **no** choice
    is offered — not even the deployment default. The composer must not
    pre-select a model the create endpoint might refuse, which is the whole
    reason ``/config`` answers with the create endpoint's own predicate.

    When all three layers are missing or retired the answer is ``None``, and
    that is a real state rather than a gap to fill: the model decides cost,
    speed and capability, so the composer leaves its control empty and waits.
    Choosing one from the catalog order would run the owner's task on a model
    they never picked, which is the same mistake as choosing their repository.
    """
    if preference.last_model_connection_id is not None:
        # The owner last ran on their own connection. Only their explicit pin
        # outranks that; neither the Gateway model of the same name nor the
        # deployment default stands in for it -- when that connection cannot
        # serve a new task, the control stays empty until the owner chooses
        # (``resolve_connection_default``).
        pinned = preference.default_model
        return pinned if pinned and pinned in models else None
    for candidate in (preference.default_model, preference.last_model, deployment_default):
        if candidate and candidate in models:
            return candidate
    return None


def resolve_connection_default(
    models: list[str],
    preference: ModelPreferenceView,
    connections: list[dict[str, object]],
) -> tuple[str, str] | None:
    """``(model, connection_id)`` when a new task should start on the owner's connection.

    Only from history, only when no Gateway pin outranks it, and only while
    that connection is still enabled and still lists the model. ``models`` is
    the Gateway catalog, consulted only for the pin: an unreadable catalog does
    not stop an owner's own connection from being offered.
    """
    if preference.default_model and preference.default_model in models:
        return None
    connection_id = preference.last_model_connection_id
    if not connection_id or not preference.last_model:
        return None
    for connection in connections:
        listed = connection.get("models")
        if (
            connection.get("id") == connection_id
            and connection.get("enabled")
            and isinstance(listed, list)
            and preference.last_model in listed
        ):
            return preference.last_model, connection_id
    return None


def deployment_default_model() -> str | None:
    """The model an administrator made this deployment's starting point.

    Empty is a real answer: this deployment never set one, and an owner with no
    choice of their own is then asked to pick. Read through the settings
    snapshot rather than the environment, so a saved change applies without a
    restart the way every other administrator setting does.
    """
    return str(deployment_settings.get_setting("default_model") or "").strip() or None
