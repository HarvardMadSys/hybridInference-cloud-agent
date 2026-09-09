"""The lookup responses this service reads are ones the gateway declares.

Written after the same drift was found twice. `GrantClient.mint` sent a field
the gateway had stopped declaring; this document declared an endpoint the
gateway had stopped serving (`/internal/mcp-registry`) and did **not** declare
`aliases`, which the catalog had grown. Each repository tested its own half,
and the halves disagreed for as long as nobody looked.

The reads are named here rather than discovered. Discovering them would mean
parsing `.get("...")` calls out of two modules, which is a fragile way to learn
something a short list says plainly — and the list failing to match a rename is
the same signal, arriving in the same place.

**What this cannot do.** The contract file is a transcription of the gateway's
routers: a second record, not a second source. When it and the client are stale
in the same direction — which is exactly what happened to `allowed_mcp` — this
passes. Comparing it against a real gateway response is what catches that, and
it needs both repositories at once; `docs/smoke.md` records the last such run
and what it covered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "internal-lookups.openapi.yaml"

#: Field → the code that reads it. A read that is not here is one nobody
#: checked against the gateway.
CATALOG_READS = {
    "models": "GatewayClient.model_catalog_with_aliases",
    "aliases": "GatewayClient.model_catalog_with_aliases",
}

STATUS_READS = {
    "exists": "deps.may_sign_in",
    "active": "deps.may_sign_in",
    "status": "deps.may_sign_in (the named-status fallback)",
    "role": (
        "session._establish_session — the role the session is minted with; and "
        "routes.require_agent_owner, which re-reads it on every write"
    ),
    "email": "session._establish_session — preferred over the identity token's",
}


def _declared(path: str) -> set[str]:
    document: dict[str, Any] = yaml.safe_load(CONTRACT.read_text())
    schema = document["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    return set(schema["properties"])


@pytest.mark.parametrize(
    ("path", "reads"),
    [
        ("/internal/model-catalog", CATALOG_READS),
        ("/internal/users/{user_id}/status", STATUS_READS),
    ],
)
def test_every_field_this_service_reads_is_declared(path: str, reads: dict[str, str]) -> None:
    """A field read here is one the gateway's contract says exists."""
    undeclared = sorted(set(reads) - _declared(path))
    assert not undeclared, "\n".join(
        [f"{CONTRACT.name} does not declare these, but this service reads them:"]
        + [f"  {field} — {reads[field]}" for field in undeclared]
    )


def test_the_registry_endpoint_is_not_still_documented() -> None:
    """`/internal/mcp-registry` left with the ownership amendment.

    A documented endpoint the gateway does not serve is worse than an
    undocumented one: it reads as available, and the code that was written
    against it 404s at runtime with the contract file agreeing it should have
    worked. One such method survived here until it was deleted.
    """
    document = yaml.safe_load(CONTRACT.read_text())
    assert "/internal/mcp-registry" not in document["paths"]
