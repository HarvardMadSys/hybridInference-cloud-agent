"""The host pool's vocabulary: statuses, transitions, and enrollment secrets.

Kept apart from the store and the API on purpose. A status machine that lives
inside SQL statements can only be tested by reaching a database, and a status
machine that lives inside route handlers can only be tested by making HTTP
requests — neither of which is a good way to answer "can a disabled host come
back". Here it is a table and a function.

**Why the transitions are a table and not a chain of ``if``s.** Every real
incident with a host lifecycle is some transition nobody thought about:
a revoked host heartbeating, a draining host being drained again, a host
declared offline by the reaper the instant before its heartbeat lands. Written
as a table, each of those has an answer that can be read off the page and
tested one row at a time. Written as conditionals, the answer is whatever the
order of the branches happens to produce.

The one asymmetry worth naming: **``disabled`` is a sink.** Nothing leaves it.
Disabling a host revokes its credential, so there is no path back that does not
go through an admin issuing a fresh enrollment token — and enrollment writes
the row afresh rather than transitioning it. Making ``disabled`` recoverable by
transition would mean a revoked machine could be re-enabled without anyone
re-issuing the credential it lost.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Registered, credential issued, no heartbeat yet. Cannot claim.
HOST_ENROLLING = "enrolling"
#: Heartbeating and taking work.
HOST_ONLINE = "online"
#: Finishing what it has; no new claims.
HOST_DRAINING = "draining"
#: Credential revoked. A sink — re-enrollment, not a transition, brings it back.
HOST_DISABLED = "disabled"
#: Missed its heartbeats. Comes back on its own if the machine returns.
HOST_OFFLINE = "offline"

HOST_STATUSES: tuple[str, ...] = (
    HOST_ENROLLING,
    HOST_ONLINE,
    HOST_DRAINING,
    HOST_DISABLED,
    HOST_OFFLINE,
)

#: The only status in which a host may take a new claim. Draining hosts keep
#: their running attempts; offline and enrolling ones have not (or no longer)
#: proved they are there; disabled ones have had their credential taken away.
CLAIMABLE_STATUSES: tuple[str, ...] = (HOST_ONLINE,)

#: Which statuses each status may move to, including the no-op self-edge — a
#: heartbeat from an already-online host must not be an error.
_TRANSITIONS: dict[str, frozenset[str]] = {
    HOST_ENROLLING: frozenset({HOST_ENROLLING, HOST_ONLINE, HOST_OFFLINE, HOST_DISABLED}),
    HOST_ONLINE: frozenset({HOST_ONLINE, HOST_DRAINING, HOST_OFFLINE, HOST_DISABLED}),
    HOST_DRAINING: frozenset({HOST_DRAINING, HOST_ONLINE, HOST_OFFLINE, HOST_DISABLED}),
    HOST_OFFLINE: frozenset({HOST_OFFLINE, HOST_ONLINE, HOST_DRAINING, HOST_DISABLED}),
    HOST_DISABLED: frozenset({HOST_DISABLED}),
}

#: How often an enrolled host is expected to report in.
HEARTBEAT_INTERVAL_S = 30.0
#: Consecutive misses before the reaper calls a host offline. Three rather than
#: one because a single missed beat is a network hiccup, and marking a host
#: offline stops it claiming — an over-eager reaper empties the pool during a
#: blip that would have healed itself.
MISSED_HEARTBEATS_BEFORE_OFFLINE = 3
#: Enrollment tokens are typed (or pasted) into a machine that is being set up
#: right now. Long enough to survive walking to the rack; short enough that a
#: token left in a terminal scrollback is worthless by morning.
ENROLL_TOKEN_TTL_S = 900.0


def transition_allowed(current: str, target: str) -> bool:
    """Whether a host in ``current`` may move to ``target``.

    An unknown ``current`` answers ``False``. That is the conservative reading:
    a row holding a status this code does not recognise is a row written by a
    version that is not this one, and moving it is guessing.
    """
    return target in _TRANSITIONS.get(current, frozenset())


def allowed_targets(current: str) -> frozenset[str]:
    """Every status reachable from ``current`` — the table, read directly."""
    return _TRANSITIONS.get(current, frozenset())


def new_enroll_token() -> str:
    """Mint a one-time enrollment secret.

    URL-safe so it survives being pasted into an env file, a compose variable
    or a shell, none of which are reliable about quoting.
    """
    return secrets.token_urlsafe(32)


def hash_secret(value: str) -> str:
    """Hash a credential for storage.

    A plain SHA-256, deliberately: these are 256-bit random tokens, not
    passwords, so there is no low-entropy guess for a slow KDF to defend
    against, and a fast hash keeps the claim path from paying for one.
    """
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_capabilities(values: Iterable[str] | None) -> list[str]:
    """Return capabilities as a sorted, de-duplicated list of non-empty strings.

    Sorted because the list is compared as JSON containment and shown to
    operators; de-duplicated because a host declaring ``kata`` twice is not
    twice as capable, and stable ordering makes two hosts' declarations
    diffable.
    """
    if not values:
        return []
    return sorted({str(value).strip() for value in values if str(value).strip()})
