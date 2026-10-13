"""Refuse new sandbox allocations on known Docker storage pressure."""

from __future__ import annotations

from cloud_agent_host.host_capacity import StorageSample, build_storage_probe
from cloud_agent_host.logging_utils import get_logger

logger = get_logger(__name__)


def new_sandbox_storage_error(*, minimum_mib: float) -> str | None:
    """Read fresh attested capacity, never substitute this process's arbitrary root.

    Unknown/disabled capacity already has an explicit status in the heartbeat.
    It cannot safely establish either pressure or sufficient space, so this
    admission check does not block existing deployments that cannot measure it.
    """
    probe = build_storage_probe()
    if probe is None:
        return None
    try:
        sample = probe.sample()
    except Exception:
        logger.warning("agent_storage_admission_measurement_failed", exc_info=True)
        return None
    if not isinstance(sample, StorageSample):
        return None
    if sample.available_bytes >= minimum_mib * 1024 * 1024:
        return None
    return (
        "new sandbox allocation declined: verified Docker writable-layer storage has "
        f"{sample.available_bytes // (1024 * 1024)} MiB available, below the "
        f"{minimum_mib:g} MiB reserve. Free storage or adjust the storage reserve setting; "
        "existing sessions can still resume."
    )
