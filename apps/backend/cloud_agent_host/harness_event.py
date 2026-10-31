"""One item of a turn's stream, as every driver yields it.

Kept apart from :mod:`cloud_agent_host.drivers`, which re-exports both
names, so the modules a driver delegates to can build events without
importing the driver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Yielded for a stdout line the dialect adapter had no event for. Carries the
#: raw line so the caller can keep its tail; never uploaded.
LINE = "line"


@dataclass(frozen=True)
class HarnessEvent:
    """One item of a turn's stream.

    Dialect events carry their ``raw_line``; floor events carry none. A line
    the adapter had no event for is yielded as :data:`LINE` so the caller can
    keep its tail without this layer deciding what to drop.
    """

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    raw_line: str | None = None
