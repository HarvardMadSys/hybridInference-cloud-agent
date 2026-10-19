"""Which models a user's agent jobs may call — asked, no longer computed.

The source walked the gateway's own route registry: published flag, required
role, the visibility resolver's override, disabled-for-user preferences, chat
versus embedding. None of that data exists in this service, so this module
keeps its two functions and its callers, and gets the answer over HTTP.

**The signatures changed and could not not change.** They took a
``router_exec`` and a ``visibility_resolver`` — objects that were the registry.
Keeping them as parameters would have meant either passing ``None`` everywhere
or inventing a local stand-in for a decision the gateway owns; both are worse
than a call site that says what it now does.

**Failure is closed.** A gateway that cannot be reached yields *no* models, not
every model. Getting this backwards would let an outage widen what a job may
call, at exactly the moment nobody is watching.

**And it is not a verdict.** The checks that admit one model or one effort
raise :class:`ModelCatalogUnavailable` rather than refusing the choice as
unknown. A refusal makes its owner fix a request nothing was wrong with, and
holds a waiting message until they release it by hand; an outage only tells
the caller to ask again.
"""

from __future__ import annotations

from cloud_agent.gateway import GatewayClient, GatewayError
from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)


class ModelCatalogUnavailable(Exception):
    """The gateway could not say which models this user's jobs may call."""


async def agent_visible_models(gateway: GatewayClient, *, user_id: str) -> list[str]:
    """Return the canonical chat-model ids this user's agent jobs can call.

    Args:
        gateway: Client for the gateway's internal contracts.
        user_id: The gateway's user identifier.

    Returns:
        Model ids in the gateway's registry order, or an empty list if the
        catalog cannot be read.
    """
    try:
        return await gateway.model_catalog(user_id)
    except GatewayError:
        # Empty, never "everything". A picker with no options is visibly
        # broken; a picker showing models the user cannot call produces a job
        # that dies at its first request, far from the cause.
        logger.warning(
            "agent_model_catalog_unavailable",
            exc_info=True,
            extra={"event": "agent_model_catalog_unavailable", "user_id": user_id},
        )
        return []


async def agent_canonical_model(model: str, gateway: GatewayClient, *, user_id: str) -> str | None:
    """Resolve a requested model name to the canonical id, or refuse it.

    **Translate once, then work in canonical ids.** The gateway owns alias
    resolution and hands over its table; this service looks the name up and
    never mentions an alias again — not to the store, not in a grant, not in
    the scope check the gateway applies to the resulting token. A grant that
    stored an alias would be compared against canonical ids and silently match
    nothing.

    Args:
        model: What the user typed. Either a canonical id or an alias.
        gateway: Client for the gateway's internal contracts.
        user_id: The gateway's user identifier.

    Returns:
        The canonical id, or ``None`` when this user cannot reach the model:
        an unknown name, or an alias for a model outside this user's catalog.

    Raises:
        ModelCatalogUnavailable: The catalog could not be read. Nothing is
            accepted on it, because the alternative to "no" here is a job that
            dies at its first model call, far from the cause; and nothing is
            refused, because the model may be fine.
    """
    try:
        models, aliases = await gateway.model_catalog_with_aliases(user_id)
    except GatewayError as exc:
        logger.warning(
            "agent_model_catalog_unavailable",
            exc_info=True,
            extra={"event": "agent_model_catalog_unavailable", "user_id": user_id},
        )
        raise ModelCatalogUnavailable(str(exc)) from exc

    # An exact catalog entry wins outright. Checking it first means an alias
    # can never shadow a real model, whatever the table says.
    if model in models:
        return model
    canonical = aliases.get(model)
    if canonical is not None and canonical in models:
        return canonical
    # Refused, never dropped: narrowing a requested scope silently is how a job
    # ends up running against models nobody asked for.
    return None


async def agent_model_resolvable(model: str, gateway: GatewayClient, *, user_id: str) -> bool:
    """Whether one requested model would resolve for this user.

    The create-time fail-fast the source had, with the same purpose: rejecting
    an unknown model costs a 400, while accepting it costs a claim, a clone and
    an attempt before the first model call dies.

    Aliases resolve again, as they did before the split — see
    :func:`agent_canonical_model`. Callers that need the resolved id should use
    that directly; this stays for the places that only ask yes or no.

    Args:
        model: The requested model id, canonical or alias.
        gateway: Client for the gateway's internal contracts.
        user_id: The gateway's user identifier.

    Returns:
        Whether the model resolves in this user's catalog; ``False`` when the
        catalog cannot be read.
    """
    try:
        return await agent_canonical_model(model, gateway, user_id=user_id) is not None
    except ModelCatalogUnavailable:
        return False


async def agent_model_reasoning_efforts(
    gateway: GatewayClient, *, user_id: str
) -> dict[str, list[str]]:
    """The reasoning-effort domain of each model this user's jobs can call.

    Same failure posture as :func:`agent_canonical_model`: a gateway that
    cannot be reached admits no effort, and refuses none either. Admitting one
    would produce turns that die at their first model call; refusing one would
    blame a choice the gateway never judged.

    Args:
        gateway: Client for the gateway's internal contracts.
        user_id: The gateway's user identifier.

    Returns:
        ``{model_id: [values]}``.

    Raises:
        ModelCatalogUnavailable: The catalog could not be read.
    """
    try:
        return await gateway.model_reasoning_efforts(user_id)
    except GatewayError as exc:
        logger.warning(
            "agent_model_efforts_unavailable",
            exc_info=True,
            extra={"event": "agent_model_efforts_unavailable", "user_id": user_id},
        )
        raise ModelCatalogUnavailable(str(exc)) from exc
