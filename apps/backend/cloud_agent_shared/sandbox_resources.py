"""Optional whole-container resource readings shared by sandbox manager and control plane."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, ValidationError


class SandboxResourceSample(BaseModel):
    """One Docker container reading, with rates over its preceding sample interval.

    Null counters mean Docker did not measure them. CPU is expressed in cores,
    so one fully occupied CPU reports ``1.0`` regardless of the host's size.
    Network and block I/O byte counters are cumulative Docker counters; their
    rates, and CPU, describe the interval between two usable observations.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1] = 1
    source: Literal["docker-stats"] = "docker-stats"
    scope: Literal["container"] = "container"
    sample_id: str = Field(min_length=1, max_length=64)
    sample_age_ms: StrictFloat = Field(ge=0, le=300_000)
    cpu_cores: StrictFloat | None = Field(None, ge=0)
    cpu_window_seconds: StrictFloat | None = Field(None, gt=0, le=300)
    memory_bytes: StrictInt | None = Field(None, ge=0)
    memory_limit_bytes: StrictInt | None = Field(None, ge=0)
    pids: StrictInt | None = Field(None, ge=0)
    io_read_bytes: StrictInt | None = Field(None, ge=0)
    io_write_bytes: StrictInt | None = Field(None, ge=0)
    net_rx_bytes: StrictInt | None = Field(None, ge=0)
    net_tx_bytes: StrictInt | None = Field(None, ge=0)
    io_read_bytes_per_second: StrictFloat | None = Field(None, ge=0)
    io_write_bytes_per_second: StrictFloat | None = Field(None, ge=0)
    net_rx_bytes_per_second: StrictFloat | None = Field(None, ge=0)
    net_tx_bytes_per_second: StrictFloat | None = Field(None, ge=0)


def parse_sandbox_resource_sample(value: Any) -> SandboxResourceSample | None:
    """Drop unsupported or malformed telemetry without refusing host liveness."""
    try:
        return SandboxResourceSample.model_validate(value)
    except (ValidationError, TypeError, ValueError):
        return None
