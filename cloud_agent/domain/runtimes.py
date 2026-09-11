"""Which CLI version an attempt runs, and why.

A decision over values the caller has already read, kept apart from the
read so it can be exercised without a database.
"""

from __future__ import annotations

from typing import Any

from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)


def resolve_runtime_version(
    *,
    runtime: str,
    thread_version: str | None,
    selected: dict[str, Any],
    available: dict[str, Any],
    host_default: dict[str, Any],
) -> str | None:
    """Which CLI version this attempt runs, in priority order.

    1. **The version its thread already runs.** A thread is a native CLI
       session stored in its retained container. An admin changing the
       selection must not reach into conversations already in flight: it
       applies to new groups, and existing ones keep the version they started
       on so the same native session remains readable.
    2. **The deployment's selection**, but only if this host's image actually
       holds it. A selection the host cannot serve falls through rather than
       failing the claim — an image rebuilt with a shorter menu drops versions
       out from under a stored choice, and a queue that parks on that is a
       worse outcome than a job running the default.
    3. **The host's default**, which is what every claim did before selection
       existed.

    ``None`` means the host has never reported a version for this runtime, and
    is stamped as-is: "unknown" is a truthful record, and inventing one would
    make the attempt row lie about what ran.
    """
    if thread_version:
        return thread_version
    choice = selected.get(runtime)
    if isinstance(choice, str) and choice:
        offered = available.get(runtime)
        if isinstance(offered, list) and choice in offered:
            return choice
        logger.warning(
            "Runtime version selection not offered by the claiming host",
            extra={
                "event": "agent_runtime_version_unavailable",
                "runtime": runtime,
                "selected": choice,
            },
        )
    default = host_default.get(runtime)
    return default if isinstance(default, str) and default else None
