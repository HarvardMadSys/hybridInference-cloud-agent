"""Each registered CLI's live session, and the driver that opens it.

The one module that knows every CLI's session class. It is not part of the
package's public API in :mod:`cloud_agent_host.runtimes`: the sessions build
on :mod:`cloud_agent_host.drivers`, which imports that API, so importing them
from the package itself would be circular.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent_host.drivers import DialectRuntime, LiveDriver, SessionType
from cloud_agent_host.runtimes import get_runtime
from cloud_agent_host.runtimes.claude.session import ClaudeCodeLiveSession
from cloud_agent_host.runtimes.codex.session import CodexAppServerSession
from cloud_agent_host.runtimes.http_bridge.session import HttpAgentLiveSession
from cloud_agent_host.runtimes.pi.session import PiRpcSession

if TYPE_CHECKING:
    from cloud_agent_host.runtimes import AgentRuntime


#: Each registered CLI: the session class that speaks its live protocol, and
#: whether a message reaches its running turn.
_LIVE_SESSIONS: dict[str, tuple[SessionType, bool]] = {
    "claude-code": (ClaudeCodeLiveSession, True),
    "codex": (CodexAppServerSession, True),
    "pi": (PiRpcSession, False),
    "opencode": (HttpAgentLiveSession, False),
    "kilo": (HttpAgentLiveSession, False),
}


def get_driver(runtime_id: str) -> LiveDriver[DialectRuntime]:
    """Resolve a registry id to its driver. Raises ``KeyError`` like get_runtime."""
    return driver_for(get_runtime(runtime_id))


def driver_for(runtime: AgentRuntime) -> LiveDriver[DialectRuntime]:
    """The driver for an already-resolved dialect adapter.

    A new one each time: the adapter carries one session's state. A dialect
    adapter with no live session is refused with ``KeyError``.
    """
    session_type, mid_turn = _LIVE_SESSIONS[runtime.name]
    return LiveDriver(runtime, session_type=session_type, mid_turn=mid_turn)
