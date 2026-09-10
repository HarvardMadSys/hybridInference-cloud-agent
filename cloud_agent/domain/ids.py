"""Identifier minting for the objects the control plane creates.

Ids are opaque and prefixed by kind, which is what makes a log line readable
without a join. They never encode meaning beyond that prefix.
"""

from __future__ import annotations

import secrets


def _new_job_id() -> str:
    """Generate a new agent job id."""
    return f"ajob_{secrets.token_hex(8)}"


def _new_thread_id() -> str:
    """Generate a stable conversation id shared by a thread's runs."""
    return f"athr_{secrets.token_hex(8)}"


def _new_sandbox_id() -> str:
    """Generate the platform identity of one retained Docker sandbox."""
    return f"asbx_{secrets.token_hex(16)}"


def _new_session_fork_id() -> str:
    """Generate one shared native-session fork operation id."""
    return f"asfork_{secrets.token_hex(16)}"
