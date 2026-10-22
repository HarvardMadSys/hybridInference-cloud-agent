"""A turn with MCP tools fails on a residency the control plane gave no MCP credential."""

from __future__ import annotations

import pytest
from tests.unit.test_agent_runner import (  # noqa: F401 - the autouse pytest fixture
    _probe_then_replace,
    _reset_profiling_cache,
    _run_shared_boundary_case,
)


@pytest.mark.parametrize("residency", [{}, {"mcp_credential": ""}])
def test_a_turn_with_mcp_fails_on_a_residency_without_an_mcp_credential(monkeypatch, residency):
    """A process opened without one would hold the relay's address and no token.

    Every tool call would then be refused with a 401 that says nothing of why.
    The turn's own token no longer stands in, so the turn fails before the
    process opens, as it does for a residency without a grant.
    """
    monkeypatch.setenv("AGENT_SANDBOX_MCP_URL", "http://relay")
    result, control, supervisor, finish = _run_shared_boundary_case(
        monkeypatch,
        0,
        None,
        "claude-code",
        configure=lambda control, supervisor, _finish: _probe_then_replace(
            control, supervisor, residency=residency
        ),
        job_changes={"mcp_bindings": [":platform:memory"]},
    )

    assert result == 2
    assert supervisor.ensure_shared_resident.call_count == 1, "a process opened without it"
    assert finish.call_args.args[:2] == (
        "failed",
        "control plane returned no shared residency MCP credential",
    )
    control.admit_chat_turn.assert_not_called()
