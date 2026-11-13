"""Reading which reasoning levels an owner's provider declares for their models.

Two model listings at the connection's endpoint, with the owner's key, under
the same rules as every other call the control plane makes to it
(``model_connection_check``): public IPv4 HTTPS only, pinned, no redirect, no
proxy, a bounded read, and nothing the provider sent echoed back:

- ``GET <Base URL>/models`` with the bearer key, the OpenAI-style listing, read
  for each entry's ``reasoning`` object;
- ``GET <Anthropic base>/v1/models?limit=1000`` with ``x-api-key`` and
  ``anthropic-version``, the Anthropic Models API, read for each entry's
  ``capabilities.effort``. A HybridInference gateway answers this request
  with its Anthropic-shaped listing.

At most those two requests per read, whatever the number of models, and no
inference: a listing costs the provider nothing to answer. A save reads them
beside its verification, within its own reservation and deadline; the
owner's "refresh" reads them for a saved connection for one hourly check. A
listing that fails leaves its APIs ``unknown``, never "unsupported", and never
fails a save. How the answers are read is ``domain.model_efforts``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cloud_agent.application.model_connection_anthropic import anthropic_headers, models_url
from cloud_agent.application.model_connection_check import (
    CheckStop,
    bounded_get,
    guard_base_url,
)
from cloud_agent.domain.model_efforts import ListingRead, effort_facts

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    import httpx

#: The Anthropic Models API pages at 20 by default and allows up to 1000.
ANTHROPIC_LISTING_QUERY = "?limit=1000"
#: The longest one read waits for both listings together.
READ_TIMEOUT_S = 70.0


async def _listing(
    url: str, headers: dict[str, str], transport: httpx.AsyncBaseTransport | None
) -> ListingRead:
    try:
        body = await bounded_get(client_transport=transport, url=url, headers=headers)
    except CheckStop as stop:
        return ListingRead(failure=stop.category)
    try:
        return ListingRead(body=json.loads(body))
    except (ValueError, UnicodeDecodeError):
        return ListingRead(failure="invalid_response")


async def read_effort_facts(
    *,
    base_url: str,
    api_key: str,
    models: list[str],
    resolve: Callable[[str, int], Awaitable[Iterable[str]]] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, dict[str, Any]]:
    """Each of ``models``' effort facts at this endpoint, as stored in its check entry.

    ``{model: {"efforts": {<protocol>: fact}, "efforts_checked_at": iso}}``.
    Never raises for the provider's behaviour: a blocked address, a timeout or
    a refused listing is an ``unknown`` fact with its reason.
    """
    failed = ListingRead(failure="unreachable")
    try:
        async with asyncio.timeout(READ_TIMEOUT_S):
            await guard_base_url(base_url, resolve=resolve)
            openai, anthropic = await asyncio.gather(
                _listing(base_url + "/models", {"authorization": f"Bearer {api_key}"}, transport),
                _listing(
                    models_url(base_url) + ANTHROPIC_LISTING_QUERY,
                    anthropic_headers(api_key),
                    transport,
                ),
            )
    except CheckStop as stop:
        openai = anthropic = ListingRead(failure=stop.category)
    except TimeoutError:
        openai = anthropic = failed
    checked_at = datetime.now(UTC).isoformat()
    facts = effort_facts(models, anthropic=anthropic, openai=openai)
    return {model: {"efforts": facts[model], "efforts_checked_at": checked_at} for model in models}


def with_efforts(
    checks: dict[str, dict[str, Any]], efforts: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """``checks`` with each model's effort facts replaced by ``efforts``'."""
    return {model: {**entry, **efforts.get(model, {})} for model, entry in checks.items()}


__all__ = [
    "ANTHROPIC_LISTING_QUERY",
    "READ_TIMEOUT_S",
    "read_effort_facts",
    "with_efforts",
]
