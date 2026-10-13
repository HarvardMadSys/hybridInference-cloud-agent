"""Validation of retained-sandbox settlement boundaries."""

import pytest

from cloud_agent.schemas import WorkerSandboxSettleRequest


@pytest.mark.parametrize("locator", ["", "   ", "\t\r\n"])
def test_settle_request_normalizes_blank_native_locator(locator: str) -> None:
    request = WorkerSandboxSettleRequest(
        sandbox_generation=1,
        activation_generation=1,
        residency_generation=1,
        context_status="live",
        native_session_id=locator,
    )

    assert request.native_session_id is None


def test_settle_request_trims_native_locator() -> None:
    request = WorkerSandboxSettleRequest(
        sandbox_generation=1,
        activation_generation=1,
        residency_generation=1,
        context_status="live",
        native_session_id="  native-session-one\t",
    )

    assert request.native_session_id == "native-session-one"
