"""Team membership vocabulary, limits and refusals, independent of storage and HTTP.

A team is a set of accounts with exactly one owner. Besides membership, a
team owns environments its members share (see
``storage/repositories/environments.py``); threads and sandboxes stay each
member's own. See ``docs/agents/specs/2026-10-15-team-shared-workspace-design.zh-CN.md``.

Every refusal here carries a stable ``code`` the API returns as the error type,
so a client can tell "the team changed under you" from "you are not the owner"
without parsing a sentence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import datetime

#: Longest team name, after surrounding whitespace is removed.
MAX_TEAM_NAME_CHARS = 80
#: Active members one team may have, the owner included. Approval refuses past it.
MAX_TEAM_MEMBERS = 50
#: Live teams an account may already own and still create another. Checked on
#: creation only: a transfer can hand someone a team past it.
MAX_OWNED_TEAMS = 20
#: Unexpired, unclaimed invitations one team may have outstanding.
MAX_OPEN_INVITATIONS = 20
#: How long a link stays claimable, and how long the owner has to decide.
INVITATION_TTL = timedelta(hours=24)
#: Rows one listing returns; older invitations and requests age out of view.
LISTING_LIMIT = 50
#: Audit entries one page returns.
AUDIT_PAGE_LIMIT = 100
#: Teams one page of the administrator's list returns.
ADMIN_TEAM_PAGE_LIMIT = 100

TeamRole = Literal["owner", "admin", "member"]
MemberRole = Literal["admin", "member"]
MembershipState = Literal["active", "left", "removed"]
TeamState = Literal["active", "deleted"]
InvitationStatus = Literal["open", "claimed", "revoked", "expired"]
JoinRequestState = Literal["pending", "approved", "declined", "cancelled", "expired"]


class TeamError(Exception):
    """A refusal with a stable code; the API maps the subclass to a status."""

    code = "team_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """Keep a user-readable sentence and, optionally, a narrower code."""
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class TeamNotFound(TeamError):
    """The team, or the row inside it, does not exist for this caller.

    Also raised when the caller is not an active member, so team ids cannot be
    probed for existence across accounts.
    """

    code = "not_found"


class TeamForbidden(TeamError):
    """The caller is a member, but only the owner may do this."""

    code = "forbidden"


class TeamConflict(TeamError):
    """The request is valid but the team's current state does not allow it."""

    code = "conflict"


class InvitationUnavailable(TeamError):
    """The link is unknown, expired, revoked, or already claimed by someone else.

    One refusal for all four: whoever holds a dead link learns only that it is
    dead, not whether it ever existed.
    """

    code = "invitation_unavailable"


@dataclass(frozen=True)
class TeamRecord:
    """One active team as a member sees it."""

    id: str
    name: str
    owner_user_id: str
    revision: int
    member_count: int
    created_at: datetime
    role: TeamRole


@dataclass(frozen=True)
class MemberRecord:
    """An active member, with the display names their own profile chose."""

    user_id: str
    first_name: str
    last_name: str
    joined_at: datetime
    role: TeamRole


@dataclass(frozen=True)
class InvitationRecord:
    """An invitation's metadata. The link token is never stored or returned here."""

    id: str
    created_by_user_id: str
    status: InvitationStatus
    expires_at: datetime
    created_at: datetime
    claimed_by_user_id: str | None
    request_state: JoinRequestState | None


@dataclass(frozen=True)
class ApplicantRecord:
    """A join request as the team's owner reviews it."""

    id: str
    user_id: str
    first_name: str
    last_name: str
    state: JoinRequestState
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class OwnJoinRequestRecord:
    """A join request as its applicant sees it: no team content, only its status."""

    id: str
    team_id: str
    team_name: str
    inviter_user_id: str
    inviter_first_name: str
    inviter_last_name: str
    state: JoinRequestState
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class AuditRecord:
    """One administrative action on a team. Never holds a token or a secret."""

    id: int
    actor_user_id: str
    action: str
    target_user_id: str | None
    target_id: str | None
    created_at: datetime


# ── The platform administrator's read-only view ─────────────────────────────


@dataclass(frozen=True)
class AdminTeamRecord:
    """One team as the platform administrator sees it, deleted ones included."""

    id: str
    name: str
    state: TeamState
    owner_user_id: str
    owner_first_name: str
    owner_last_name: str
    member_count: int
    admin_count: int
    pending_request_count: int
    created_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True)
class AdminMemberRecord:
    """A membership in any state, so departures stay visible to the administrator."""

    user_id: str
    first_name: str
    last_name: str
    role: TeamRole
    state: MembershipState
    joined_at: datetime
    departed_at: datetime | None


@dataclass(frozen=True)
class AdminAuditRecord:
    """An audit event with the names the actor's and target's profiles give."""

    id: int
    actor_user_id: str
    actor_first_name: str
    actor_last_name: str
    action: str
    target_user_id: str | None
    target_first_name: str
    target_last_name: str
    target_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class AdminTeamDetail:
    """Everything the administrator's team page shows, read in one snapshot."""

    team: AdminTeamRecord
    #: Every active member, and the :data:`LISTING_LIMIT` latest departures.
    members: list[AdminMemberRecord]
    join_requests: list[ApplicantRecord]
    #: Invitations by status: open, expired, claimed and revoked.
    invitations: dict[str, int]
    #: Every membership that ended, listed in ``members`` or not.
    departed_count: int
