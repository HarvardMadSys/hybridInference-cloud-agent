"""Each registered CLI's live session, the driver that opens it, and its launch steps.

The one module that knows every CLI's session class and member launch steps
(:class:`~cloud_agent_host.runtimes.member_launch.MemberLaunch`). It is not part of the
package's public API in :mod:`cloud_agent_host.runtimes`: the sessions build
on :mod:`cloud_agent_host.drivers`, which imports that API, so importing them
from the package itself would be circular.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent_host.drivers import DialectRuntime, LiveDriver, SessionType
from cloud_agent_host.runtimes import get_runtime
from cloud_agent_host.runtimes.claude import member as claude_member
from cloud_agent_host.runtimes.claude.session import ClaudeCodeLiveSession
from cloud_agent_host.runtimes.codex import member as codex_member
from cloud_agent_host.runtimes.codex.session import CodexAppServerSession
from cloud_agent_host.runtimes.http_bridge import member as http_member
from cloud_agent_host.runtimes.http_bridge.session import HttpAgentLiveSession
from cloud_agent_host.runtimes.pi import member as pi_member
from cloud_agent_host.runtimes.pi.session import PiRpcSession

if TYPE_CHECKING:
    from cloud_agent_host.runtimes import AgentRuntime
    from cloud_agent_host.runtimes.member_launch import MemberLaunch


#: Each registered CLI: the session class that speaks its live protocol, and
#: whether a message reaches its running turn.
_LIVE_SESSIONS: dict[str, tuple[SessionType, bool]] = {
    "claude-code": (ClaudeCodeLiveSession, True),
    "codex": (CodexAppServerSession, True),
    "pi": (PiRpcSession, False),
    "opencode": (HttpAgentLiveSession, False),
    "kilo": (HttpAgentLiveSession, False),
}

#: Each registered CLI's own steps in a member's launch.
_MEMBER_LAUNCHES: dict[str, MemberLaunch] = {
    "claude-code": claude_member.LAUNCH,
    "codex": codex_member.LAUNCH,
    "pi": pi_member.LAUNCH,
    "opencode": http_member.OPENCODE_LAUNCH,
    "kilo": http_member.KILO_LAUNCH,
}


def member_launch(runtime_id: str) -> MemberLaunch:
    """A registered CLI's own launch steps. Raises ``KeyError`` for any other id."""
    return _MEMBER_LAUNCHES[runtime_id]


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
