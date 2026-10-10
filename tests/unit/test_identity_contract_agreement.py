"""The audience this service requires is the one the gateway stamps.

`verify_identity_token` refuses any token whose `aud` is not `AUDIENCE`. That
string is only meaningful next to the gateway's `CLOUD_AGENT_CLIENT_ID`, which
lives in another repository this one cannot import — so each side has always
tested its own half of a two-sided agreement, and a rename on either would pass
both suites and fail at the first sign-in.

`gateway_contracts/identity.openapi.yaml` is the closest thing here to the other side:
hand-transcribed from the gateway's routers, and independent of
`identity.py` in the sense that matters — nobody editing the verifier is
editing it too.

So this is not the constant compared to itself. It fails when the two records
of the same agreement drift, which is the moment to go and check whether the
gateway actually changed.

Verified against real code on both sides on 2026-08-04 — a token minted by the
gateway's `mint_identity_token`, verified here over an HTTP JWKS fetch,
including both directions of a key rotation. See `docs/smoke.md`.
"""

from __future__ import annotations

from pathlib import Path

from cloud_agent.identity import AUDIENCE

CONTRACT = Path(__file__).resolve().parents[2] / "gateway_contracts" / "identity.openapi.yaml"


def test_the_audience_matches_the_transcribed_gateway_contract() -> None:
    """A rename here has to be a decision about the gateway too."""
    assert f"`{AUDIENCE}`" in CONTRACT.read_text(), (
        f"this service requires aud={AUDIENCE!r}, which {CONTRACT.name} does not "
        "name as the registered client — one of the two is wrong, and the "
        "gateway is the one that decides"
    )
