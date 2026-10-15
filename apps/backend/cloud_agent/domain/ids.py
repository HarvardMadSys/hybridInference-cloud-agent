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


def new_session_fork_id() -> str:
    """Generate one shared native-session fork operation id."""
    return f"asfork_{secrets.token_hex(16)}"


def new_pending_message_id() -> str:
    """Generate the id of one message waiting in a conversation's queue."""
    return f"apmsg_{secrets.token_hex(12)}"


def new_team_id() -> str:
    """Generate the stable, opaque id of a team; its name carries no authority."""
    return f"ateam_{secrets.token_hex(8)}"


def new_team_invitation_id() -> str:
    """Generate the id of one single-use team invitation (not its link token)."""
    return f"atinv_{secrets.token_hex(12)}"


def new_team_join_request_id() -> str:
    """Generate the id of the join request a claimed invitation creates."""
    return f"atjr_{secrets.token_hex(12)}"
