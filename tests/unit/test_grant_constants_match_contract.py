"""The gateway's clamps are mirrored here; the document is where they are written.

`cloud_agent/grants.py` cannot read these numbers off the wire — neither appears
in a response body — so it copies them.
`gateway_contracts/inference-grants.openapi.yaml` declares them under `x-constants`, and
**nothing read that field** before this file: the same number lived in three
places (the gateway's `serving/grants.py`, the document, and this client) with
nothing comparing any two of them. This compares the client to the document.

It cannot reach the gateway, and that is the half left over. The gateway's own
copy is observable only by asking for a TTL and reading `expires_at` back, which
is a write. `tests/unit/test_grant_lifecycle.py` pins the client's copies against
*each other* — the lifetime must fit under the maximum, the cadence under the
step — so tying them to the document here leaves one hop unpinned, and
`tests/external/test_gateway_contracts_live.py` is where that hop is checked.

The two names differ deliberately, and it is worth knowing before "fixing" it.
The document calls the renewal step `DEFAULT_GRANT_TTL_S`, because at the gateway
it *is* the default TTL and a renewal buys it from now. The client calls the same
number `GATEWAY_RENEWAL_STEP_S`, after what it does to a running attempt.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cloud_agent import grants

CONTRACT = (
    Path(__file__).resolve().parents[2] / "gateway_contracts" / "inference-grants.openapi.yaml"
)


@pytest.fixture(scope="module")
def declared_constants() -> dict:
    """The document's own record of the numbers this client mirrors."""
    return yaml.safe_load(CONTRACT.read_text())["x-constants"]


def test_the_maximum_ttl_is_the_one_the_document_declares(declared_constants: dict) -> None:
    """A clamp undersized here is silent.

    The client asks for more than the gateway allows and is shortened without any
    call failing, which is why the number has to be read rather than discovered.
    """
    assert declared_constants["MAX_GRANT_TTL_S"] == grants.GATEWAY_MAX_TTL_S


def test_the_renewal_step_is_the_one_the_document_declares(declared_constants: dict) -> None:
    """The number a running attempt's renewal cadence has to stay inside."""
    assert declared_constants["DEFAULT_GRANT_TTL_S"] == grants.GATEWAY_RENEWAL_STEP_S


def test_the_token_prefix_is_the_one_the_document_declares(declared_constants: dict) -> None:
    """This service never mints a grant token, so it cannot check the prefix by use.

    What it can do is pin the value the external lifecycle case asserts a live
    gateway honours, so a silent edit here fails rather than quietly changing what
    both records mean by a grant token.
    """
    assert declared_constants["token_prefix"] == "agr."
