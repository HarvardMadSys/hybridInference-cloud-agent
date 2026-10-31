"""Host configuration left over from the removed Gateway inference relay.

A host runs every member on its owner's own model connection, reached through
the public egress its group bridge already allows. A host still configured for
the Gateway relay, or with an account mode, would expect a tunnel and a
credential that no longer exist, so it stops at startup with the variables
named rather than starting without them.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Set (non-empty) on a host configured for the removed Gateway integration.
RETIRED_HOST_VARIABLES = ("AGENT_AUTH_MODE", "AGENT_GATEWAY_URL")


def refuse_retired_host_configuration(environ: Mapping[str, str] | None = None) -> None:
    """Refuse a host environment that still configures the Gateway.

    Raises:
        ValueError: Names each retired variable that is set.
    """
    env = os.environ if environ is None else environ
    found = [name for name in RETIRED_HOST_VARIABLES if (env.get(name) or "").strip()]
    if found:
        raise ValueError(
            f"remove {', '.join(found)} from this host's configuration: hosts no longer "
            "use a Gateway inference relay or an account mode; members run on their "
            "owners' model connections"
        )
