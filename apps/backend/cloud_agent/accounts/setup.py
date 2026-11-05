"""First-run setup: the one-time code that creates the first administrator.

Adapted from HybridInference ``setup_state.py`` at
``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c) 2026
Harvard SEAS). While the deployment has no local administrator, one setup code
is stored in ``agent_local_setup`` by the first process to boot, so every
process accepts it and a restart keeps it; each process logs it at startup, at
WARNING. ``POST /v1/auth/setup/admin`` exchanges it for an administrator that
signs in with a login name, which needs no email and no SMTP. The code is
stored in plaintext, as there: whoever can read that table can already read
everything beside it.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.ports.accounts import LocalAccountRepository

logger = get_logger(__name__)

#: Read off a log and typed by hand, so no 0/O or 1/I.
SETUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_setup_code() -> str:
    """Twelve characters (60 bits) in three groups of four."""
    raw = "".join(secrets.choice(SETUP_CODE_ALPHABET) for _ in range(12))
    return "-".join(raw[i : i + 4] for i in range(0, 12, 4))


def normalize_setup_code(value: str) -> str:
    """The code as stored, from what an operator typed."""
    return value.strip().upper().replace(" ", "")


async def announce_setup_code(accounts: LocalAccountRepository) -> None:
    """Log the pending setup code at startup; nothing once an administrator exists."""
    code = await accounts.setup_code(new_setup_code())
    if code is not None:
        logger.warning(
            "agent_local_setup_pending: create the first administrator at /setup/administrator "
            "with this one-time setup code: %s",
            code,
            extra={"event": "agent_local_setup_pending"},
        )


__all__ = ["announce_setup_code", "new_setup_code", "normalize_setup_code"]
