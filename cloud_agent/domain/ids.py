"""Identity for the rows the control plane creates.

Pure, and named once. Each prefix is part of a stored id, so it is a
contract with everything that reads one back.
"""

from __future__ import annotations

import secrets


def new_job_id() -> str:
    """Generate a new agent job id."""
    return f"ajob_{secrets.token_hex(8)}"


def new_thread_id() -> str:
    """Generate a stable conversation id shared by a thread's runs."""
    return f"athr_{secrets.token_hex(8)}"


def new_sandbox_id() -> str:
    """Generate the platform identity of one retained Docker sandbox."""
    return f"asbx_{secrets.token_hex(16)}"


def new_session_fork_id() -> str:
    """Generate one shared native-session fork operation id."""
    return f"asfork_{secrets.token_hex(16)}"
