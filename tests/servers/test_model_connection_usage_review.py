"""Independent source-attribution regressions: unknown upstream bills are never zero."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from tests.servers.test_admin_runs import ADMIN, PREFIX, setup as setup
from tests.unit.test_ledger_snapshots import ledger_row

from cloud_agent.ledger import summarize_conversation


@pytest.mark.parametrize("external", [False, True])
async def test_admin_usage_preserves_the_turns_provider_source(setup, external):
    app, queries, store, _ = setup
    queries.get_run.return_value["model_connection_id"] = "mconn_" + "a" * 32 if external else None
    store.ledger_rows_for_job.return_value = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX + "/ajob_other", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["usage"]["external_provider"] is external


def test_mixed_conversation_cannot_claim_its_gateway_subtotal_covers_a_personal_turn():
    """Missing upstream usage events do not prove that a personal turn cost nothing."""
    known = ledger_row(job_id="gateway-turn", job_state="succeeded")
    external = {
        "job_id": "personal-turn",
        "job_state": "failed",
        "attempt_id": 99,
        "attempt_no": 1,
        "attempt_status": "failed",
        "model_usage_reported": False,
        "id": None,
        "snapshot": None,
        "model_connection_id": "mconn_" + "a" * 32,
    }
    summary = summarize_conversation([known, external])
    assert summary.turn_count == 2
    assert summary.totals is not None
    assert summary.totals.spent_usd == "0.00012345"
    assert summary.incomplete, "a known Gateway subtotal must exclude unknown provider charges"
