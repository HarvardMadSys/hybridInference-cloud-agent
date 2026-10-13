"""Keep control-plane discovery aligned with executable host adapters."""

from __future__ import annotations

import subprocess
import sys

from cloud_agent_host import runtimes
from cloud_agent_shared import runtime_catalog


def test_shared_catalog_matches_the_host_registry() -> None:
    """Admission advertises the same IDs and capabilities the host can run."""
    assert runtime_catalog.registered_runtimes() == runtimes.registered_runtimes()
    assert runtime_catalog.runtimes_supporting_mcp() == runtimes.runtimes_supporting_mcp()
    assert runtime_catalog.runtime_reasoning_efforts() == runtimes.runtime_reasoning_efforts()

    definitions = runtime_catalog.RUNTIME_CATALOG
    assert len({definition.name for definition in definitions}) == len(definitions)
    for definition in definitions:
        adapter = runtimes.get_runtime(definition.name)
        capabilities = adapter.capabilities()
        assert capabilities.mcp == definition.mcp
        assert capabilities.reasoning_efforts == definition.reasoning_efforts


def test_discovery_returns_independent_mutable_projections() -> None:
    """A caller editing its response cannot change another job's admission."""
    names = runtime_catalog.registered_runtimes()
    names.clear()
    mcp_names = runtime_catalog.runtimes_supporting_mcp()
    mcp_names.clear()
    efforts = runtime_catalog.runtime_reasoning_efforts()
    efforts["claude-code"].clear()
    efforts.pop("codex")

    assert runtime_catalog.registered_runtimes() == runtimes.registered_runtimes()
    assert runtime_catalog.runtimes_supporting_mcp() == runtimes.runtimes_supporting_mcp()
    assert runtime_catalog.runtime_reasoning_efforts() == runtimes.runtime_reasoning_efforts()


def test_catalog_import_does_not_load_host_implementation() -> None:
    """A fresh control-plane process can discover runtimes without host code."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from cloud_agent_shared import runtime_catalog; "
            "assert runtime_catalog.registered_runtimes(); "
            "assert not any(name == 'cloud_agent_host' or "
            "name.startswith('cloud_agent_host.') for name in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
