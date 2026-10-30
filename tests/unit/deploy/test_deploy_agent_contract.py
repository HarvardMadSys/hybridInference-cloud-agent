"""The agent's contract is byte for byte the control plane's.

The release recipe carries no application source, so the agent ships its own
copy of ``cloud_agent_shared/deployment_contract.py``. A difference would let
the control plane accept an intent the agent refuses, or worse, the reverse.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_the_agents_copy_of_the_contract_is_identical() -> None:
    shared = (REPO / "apps/backend/cloud_agent_shared/deployment_contract.py").read_bytes()
    copy = (REPO / "deploy/agent/deployment_contract.py").read_bytes()
    assert copy == shared, (
        "copy apps/backend/cloud_agent_shared/deployment_contract.py to "
        "deploy/agent/deployment_contract.py"
    )
