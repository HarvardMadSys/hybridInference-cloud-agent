"""SSH server values the API, the application layer and the store share."""

from __future__ import annotations

from typing import Literal

SshServerStatus = Literal["pending", "ready"]

#: Characters an owner's note to the agent may hold.
MAX_DESCRIPTION_CHARS = 200
#: Characters of a domain name or IPv4 address as typed.
MAX_ADDRESS_CHARS = 253
#: Checks one owner may run per hour, across every control-plane replica.
#: A check is the platform dialling an address the owner typed before anyone
#: has shown it is theirs, so this bounds how far it can be used to probe
#: other people's ports.
CHECKS_PER_HOUR = 20
#: Event kinds that count as a check.
CHECK_EVENTS = ("scanned", "verified", "check_failed")
#: Why a check failed, as the owner is told it. Never the remote's own output.
CheckError = Literal[
    "not_public",
    "ipv6_only",
    "unresolvable",
    "refused",
    "timeout",
    "no_host_key",
    "host_key_changed",
    "auth_failed",
    "failed",
]


class SshServerConflict(Exception):
    """Another of the owner's servers already has this name."""


class SshServerUnavailable(Exception):
    """A chat asked for servers that are not the owner's ready servers."""

    def __init__(self, server_ids: list[str]) -> None:
        """Name the servers that were refused."""
        super().__init__(", ".join(server_ids))
        self.server_ids = server_ids


__all__ = [
    "CHECKS_PER_HOUR",
    "CHECK_EVENTS",
    "MAX_ADDRESS_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "CheckError",
    "SshServerConflict",
    "SshServerStatus",
    "SshServerUnavailable",
]
