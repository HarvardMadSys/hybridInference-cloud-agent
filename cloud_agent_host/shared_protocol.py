"""Version fence shared by the control plane and shared-sandbox runners.

The runner image deliberately ships only :mod:`cloud_agent_host`.  Protocol
constants used while polling must therefore live on this side of the package
boundary; the control plane already ships the host package for shared patch
validation and imports the same values so the two claim gates cannot drift.
"""

from __future__ import annotations

SHARED_SANDBOX_CAPABILITY = "shared-sandbox-v1"
SHARED_SANDBOX_PROTOCOL = "v1"
SHARED_SANDBOX_RUNTIME_PINS = {
    "claude-code": "2.1.220",
    "codex": "0.145.0",
    "pi": "0.73.1",
    "opencode": "1.18.9",
    "kilo": "7.4.17",
}
SHARED_SANDBOX_HELPER_PINS = {
    "claude-agent-sdk": "0.2.148",
    "guest-observer": "1",
    "tool-mark": "1",
}
