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
a *control-plane* concern: the host agent and the broker never read it, and a
name they cannot act on is a name they should not be able to hold.
"""

from __future__ import annotations

import os
import re

#: The deployment namespace the control row is keyed by.
ENV_PROFILING_CONTROL_DEPLOYMENT = "AGENT_OPERATION_PROFILING_DEPLOYMENT"

#: Used when the deployment does not name itself.
DEFAULT_DEPLOYMENT = "default"

#: Longest accepted name, and the characters it may use. The name is a database
#: key and appears in logs; bounding it here keeps a misconfiguration from
#: becoming an unbounded row key.
MAX_DEPLOYMENT_CHARS = 64
_DEPLOYMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def profiling_control_deployment() -> str:
    """Return this process's control namespace.

    A name that is missing, too long or malformed falls back to the default
    rather than raising: the alternative is a control plane that refuses to
    start because of a cosmetic misconfiguration, and the failure mode of the
    fallback is a page that shows the wrong experiment -- which is visible --
    rather than an outage.
    """
    raw = (os.environ.get(ENV_PROFILING_CONTROL_DEPLOYMENT) or "").strip()
    if not raw:
        return DEFAULT_DEPLOYMENT
    if len(raw) > MAX_DEPLOYMENT_CHARS or _DEPLOYMENT_PATTERN.fullmatch(raw) is None:
        return DEFAULT_DEPLOYMENT
    return raw


__all__ = [
    "DEFAULT_DEPLOYMENT",
    "ENV_PROFILING_CONTROL_DEPLOYMENT",
    "MAX_DEPLOYMENT_CHARS",
    "profiling_control_deployment",
]
