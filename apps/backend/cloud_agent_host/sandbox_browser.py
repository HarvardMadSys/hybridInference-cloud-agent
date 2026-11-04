"""Where one member's managed browser keeps its state (browser contract v1).

The sandbox image ships Chromium and ``cloud-agent-browser``, the launcher
that runs it for the calling member UID
(docs/agents/specs/2026-11-02-computer-use-base-capability.md). The image
fixes what is the same for everyone; this module names what differs per
member, and the resident launch hands it over in the member's environment:

- ``CLOUD_AGENT_BROWSER_STATE_DIR`` is in the slot's private tmpfs. It holds
  the endpoint file with the CDP token, so it must be readable by this UID
  alone, and must not outlive the container.
- ``CLOUD_AGENT_BROWSER_PROFILE`` is keyed by the chat, under the slot's home,
  as Claude's launch state is. A home outlives the member that last used its
  slot, and a Fork gets a new thread: neither may start on another chat's
  cookies.
"""

from __future__ import annotations

import hashlib

from cloud_agent_host.chat_sandbox_models import MEMBER_TMPFS_ROOT


def member_browser_env(slot_id: int, thread_id: str) -> dict[str, str]:
    """The per-member part of the browser contract's environment."""
    if slot_id < 0 or not thread_id:
        raise ValueError("resident slot and member identity are required")
    chat_key = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return {
        "CLOUD_AGENT_BROWSER_STATE_DIR": f"{MEMBER_TMPFS_ROOT}/{slot_id}/browser",
        "CLOUD_AGENT_BROWSER_PROFILE": (
            f"/home/agent-member-{slot_id}/.cloud-agent-browser/profiles/{chat_key}"
        ),
    }
