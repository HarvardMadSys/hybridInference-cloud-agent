"""Optional container telemetry stays bounded without rejecting host liveness."""

import pytest
from pydantic import ValidationError

from cloud_agent.schemas import RetainedSandboxBeat
from cloud_agent_shared.sandbox_resources import SandboxResourceSample


def sample(**overrides):
    """A container sample whose counters and interval rates are independent."""
    return {
        "schema_version": 1,
        "source": "docker-stats",
        "scope": "container",
        "sample_id": "resource-sample-1",
        "sample_age_ms": 1250,
        "cpu_cores": 1.25,
        "cpu_window_seconds": 2.0,
        "memory_bytes": 512 * 1024**2,
        "memory_limit_bytes": 4 * 1024**3,
        "pids": 12,
        "io_read_bytes": 1000,
        "io_write_bytes": 2000,
        "net_rx_bytes": 3000,
        "net_tx_bytes": 4000,
        "io_read_bytes_per_second": 10.0,
        "io_write_bytes_per_second": 20.0,
        "net_rx_bytes_per_second": 30.0,
        "net_tx_bytes_per_second": 40.0,
        **overrides,
    }


def beat(**overrides):
    """The lifecycle fence remains valid when optional telemetry is invalid."""
    return {
        "thread_id": "athr_resource",
        "sandbox_id": "asbx_resource",
        "container_id": "0123456789abcdef",
        "generation": 1,
        "activation_generation": 1,
        "running": True,
        **overrides,
    }


def test_sample_preserves_units_source_and_nullable_measurements():
    measured = SandboxResourceSample.model_validate(sample())
    assert measured.model_dump() == sample()
    unknown = SandboxResourceSample.model_validate(
        sample(cpu_cores=None, pids=None, net_rx_bytes_per_second=None)
    )
    assert unknown.cpu_cores is None
    assert unknown.pids is None
    assert unknown.net_rx_bytes_per_second is None
    assert SandboxResourceSample.model_validate(sample(cpu_cores=0.0, pids=0)).pids == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("source", "host-memory"),
        ("scope", "host"),
        ("sample_id", ""),
        ("sample_id", "x" * 65),
        ("sample_age_ms", -1),
        ("sample_age_ms", 300001),
        ("sample_age_ms", float("nan")),
        ("sample_age_ms", float("inf")),
        ("cpu_cores", -0.1),
        ("cpu_cores", float("nan")),
        ("cpu_cores", float("inf")),
        ("cpu_window_seconds", -1),
        ("cpu_window_seconds", float("inf")),
        ("memory_bytes", -1),
        ("memory_bytes", True),
        ("memory_bytes", "1024"),
        ("memory_limit_bytes", -1),
        ("pids", -1),
        ("io_read_bytes", -1),
        ("io_write_bytes", -1),
        ("net_rx_bytes", -1),
        ("net_tx_bytes", -1),
        ("io_read_bytes_per_second", -1),
        ("io_write_bytes_per_second", float("nan")),
        ("net_rx_bytes_per_second", float("inf")),
        ("net_tx_bytes_per_second", -1),
    ],
)
def test_invalid_samples_cannot_masquerade_as_measurements(field, value):
    with pytest.raises(ValidationError):
        SandboxResourceSample.model_validate(sample(**{field: value}))


def test_unknown_sample_fields_cannot_enter_admin_telemetry():
    with pytest.raises(ValidationError):
        SandboxResourceSample.model_validate(sample(task_prompt="PRIVATE prompt"))


@pytest.mark.parametrize("invalid", ["broken", [], {}, sample(memory_bytes=-1)])
def test_malformed_optional_resource_sample_is_dropped(invalid):
    observed = RetainedSandboxBeat.model_validate(beat(resource_usage=invalid, terminal_count=2))
    assert observed.running is True
    assert observed.resource_usage is None
    assert observed.terminal_count == 2


def test_telemetry_is_optional_for_older_hosts():
    observed = RetainedSandboxBeat.model_validate(beat())
    assert observed.resource_usage is None
    assert observed.terminal_count is None
    assert "resource_usage" not in observed.model_fields_set
    assert "terminal_count" not in observed.model_fields_set


@pytest.mark.parametrize("count", [-1, 4097, True, "2", 2.5])
def test_invalid_terminal_count_is_unknown_without_losing_lifecycle(count):
    observed = RetainedSandboxBeat.model_validate(
        beat(terminal_count=count, resource_usage=sample())
    )
    assert observed.terminal_count is None
    assert observed.resource_usage is not None


@pytest.mark.parametrize("count", [0, 4096])
def test_terminal_count_accepts_observed_bounds(count):
    assert RetainedSandboxBeat.model_validate(beat(terminal_count=count)).terminal_count == count
