"""The Kilo version whose MCP isolation patch was reviewed, and its fail-closed check."""

from __future__ import annotations

import pytest

from cloud_agent_host.kilo_mcp import KILO_MCP_VERSION, ensure_kilo_runtime
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_shared.shared_protocol import SHARED_SANDBOX_RUNTIME_PINS


def test_the_pinned_kilo_is_the_reviewed_isolation_build() -> None:
    """A Kilo pin bump must fail here until the isolation patch is reviewed against it.

    ``KILO_MCP_VERSION`` is a literal rather than the pin, so a bump cannot
    silently become the reviewed version. Review
    ``deploy/sandbox/kilo-project-config.patch`` against the new version, then
    update ``KILO_MCP_VERSION`` to match.
    """
    assert SHARED_SANDBOX_RUNTIME_PINS["kilo"] == KILO_MCP_VERSION


def test_an_unreviewed_kilo_version_is_refused_before_the_sandbox_is_touched() -> None:
    """Any other version fails closed; ``None`` stands in for a sandbox never reached."""
    with pytest.raises(SandboxError, match="no reviewed MCP isolation build"):
        ensure_kilo_runtime(None, None, f"{KILO_MCP_VERSION}.1")
