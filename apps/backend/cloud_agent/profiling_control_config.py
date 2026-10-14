"""Which deployment's control namespace this process commands.

The profiling control row is keyed by a deployment name, and that name decides
which experiment an Admin page can see and stop. It is therefore read from the
process's own configuration and never from a request: a browser that could name
its namespace could read another deployment's experiment, and -- worse -- stop
it, since a Stop only has to name a run id.

The default is deliberately boring. A single-deployment install sets nothing and
gets ``default``; an install that shares one database between staging and
production gives each its own name and the two stop being able to reach each
other's control state.

This lives in its own module rather than in the profiling package because it is
a *control-plane* concern: the host agent and the sandbox manager never read it, and a
name they cannot act on is a name they should not be able to hold.
"""

from __future__ import annotations

import os
import re
import threading

from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)

#: The deployment namespace the control row is keyed by.
ENV_PROFILING_CONTROL_DEPLOYMENT = "AGENT_OPERATION_PROFILING_DEPLOYMENT"

#: Used when the deployment does not name itself.
DEFAULT_DEPLOYMENT = "default"

#: Longest accepted name, and the characters it may use. The name is a database
#: key and appears in logs; bounding it here keeps a misconfiguration from
#: becoming an unbounded row key.
MAX_DEPLOYMENT_CHARS = 64
_DEPLOYMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

#: The refused name already reported. The name is read on every host policy
#: poll and every Admin read, so the warning is written once per value.
_REPORTED_REFUSAL: str | None = None
_REPORT_LOCK = threading.Lock()


def profiling_control_deployment() -> str:
    """Return this process's control namespace.

    A name that is missing, too long or malformed falls back to the default
    rather than raising: the alternative is a control plane that refuses to
    start because of a cosmetic misconfiguration. The fallback is not
    harmless, though -- a deployment that meant to have its own namespace
    shares ``default`` with whoever else uses it, and each side's Admin page
    can then see and stop the other's experiment -- so a refused name is
    logged, with the name used instead.
    """
    raw = (os.environ.get(ENV_PROFILING_CONTROL_DEPLOYMENT) or "").strip()
    if not raw:
        return DEFAULT_DEPLOYMENT
    if len(raw) > MAX_DEPLOYMENT_CHARS or _DEPLOYMENT_PATTERN.fullmatch(raw) is None:
        _report_refusal(raw)
        return DEFAULT_DEPLOYMENT
    return raw


def _report_refusal(raw: str) -> None:
    """Warn, once per refused value, that the configured name is not in use."""
    global _REPORTED_REFUSAL
    with _REPORT_LOCK:
        if raw == _REPORTED_REFUSAL:
            return
        _REPORTED_REFUSAL = raw
    logger.warning(
        "%s=%r is not a usable deployment name (at most %d characters of "
        "letters, digits and ._:-); commanding the %r namespace instead",
        ENV_PROFILING_CONTROL_DEPLOYMENT,
        raw[: 2 * MAX_DEPLOYMENT_CHARS],
        MAX_DEPLOYMENT_CHARS,
        DEFAULT_DEPLOYMENT,
        extra={
            "event": "agent_profiling_deployment_refused",
            "deployment": DEFAULT_DEPLOYMENT,
        },
    )


__all__ = [
    "DEFAULT_DEPLOYMENT",
    "ENV_PROFILING_CONTROL_DEPLOYMENT",
    "MAX_DEPLOYMENT_CHARS",
    "profiling_control_deployment",
]
