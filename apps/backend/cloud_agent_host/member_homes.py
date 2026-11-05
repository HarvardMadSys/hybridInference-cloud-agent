"""Where each member keeps its private provider home, and how a member claims it.

Codex, OpenCode and Kilo keep their provider state in a home private to the
member's UID (:data:`PRIVATE_MEMBER_STATE_ROOTS`). Each CLI's
own member script (:mod:`cloud_agent_host.runtimes.codex.member`,
:mod:`cloud_agent_host.runtimes.http_bridge.member`) and the Fork copy into a
child's home start with one of these prologues, so every start judges the home
the same way before the CLI's own preparation runs.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox import (
    CHAT_STATE_DIR,
    LEGACY_MEMBER_HOME_COLLECTIONS,
    PRIVATE_MEMBER_HOME_COLLECTIONS,
)

if TYPE_CHECKING:
    from cloud_agent_host.resident_transport import ResidentLaunch

# Provider homes contain files on which the owning CLI performs owner-only
# chmod, SQLite, or project-lock operations. Shared workspace membership uses
# distinct UIDs and distinct live processes, so fork siblings cannot safely
# point those CLIs at one common provider home. The workspace remains shared;
# the state captured at the native fork boundary is copied into the child's
# private home before the two processes resume independently.
PRIVATE_MEMBER_STATE_ROOTS = dict(
    zip(("codex", "opencode", "kilo"), PRIVATE_MEMBER_HOME_COLLECTIONS, strict=True)
)
_LEGACY_MEMBER_STATE_ROOTS = dict(
    zip(("codex", "opencode", "kilo"), LEGACY_MEMBER_HOME_COLLECTIONS, strict=True)
)

#: Exit status of a member script that found its collection untrusted. The
#: caller secures the collections as the sandbox user and asks once more.
UNTRUSTED_COLLECTION_EXIT = 75

# Each home is private to its member UID, not only distinct: its config and
# plugins run at every process start as that member, with that member's grant
# and MCP token. Mode 0700 keeps a sibling out of the home; the collection
# around it keeps a sibling from renaming the home away and putting its own
# tree at the same path between this check and the CLI's start. Every path
# component is therefore checked first: /agent-state and the collection must be
# sticky and owned by the sandbox user (``$trusted``), so no member UID can
# rename an entry it does not own (see
# ChatSandboxManager.ensure_private_member_collections). A home that is a
# symlink or belongs to another UID was put there by someone else and is
# refused rather than adopted. A sibling can still squat on an unused name,
# which only refuses that start. Shell prologue; expects ``$target`` and
# ``$trusted``.
_PRIVATE_MEMBER_HOME_CHECK = (
    "collection=${target%/*}; root=${collection%/*}; "
    'if test -z "$(find "$root" -maxdepth 0 -type d -uid "$trusted" -perm -1000)" '
    '|| test -z "$(find "$collection" -maxdepth 0 -type d -uid "$trusted" '
    '-perm -3770 ! -perm /0007)"; then '
    'echo "member provider collection is not trusted" >&2; '
    f"exit {UNTRUSTED_COLLECTION_EXIT}; fi; "
    "umask 0077; "
)
# An earlier release kept the home in a member-created, group-writable
# collection, where a sibling could rename or replace it at any time. The
# member moves whatever is at that path into the trusted collection once, and
# only then judges it, where no sibling can swap it any more; a moved home is
# also narrowed to the member alone. Expects ``$legacy`` as well.
_PRIVATE_MEMBER_HOME_MIGRATE = (
    "migrated=; "
    'if test ! -e "$target" && test ! -L "$target" '
    '&& { test -e "$legacy" || test -L "$legacy"; }; then '
    'mv -T -- "$legacy" "$target"; migrated=1; fi; '
)
_PRIVATE_MEMBER_HOME_CLAIM = (
    'mkdir -p -- "$target"; '
    'if test -L "$target" || test ! -d "$target" '
    '|| test "$(stat -c %u -- "$target")" != "$(id -u)"; then '
    'echo "member provider home is not owned by this member" >&2; exit 1; fi; '
    'chmod u=rwx,go=,g-s -- "$target"; '
)
# Judged on every start, not only after a move: whether a home is adopted must
# never depend on an earlier start having managed to set a bad one aside. Every
# legitimate writer of a provider home runs as its member -- the CLI, plugin
# installation, lock cleanup, and the fork copy's unpack -- so an entry owned
# by any other UID was put there by someone else (through an earlier release's
# group-writable home). Such a home is refused, and first renamed to an
# unpredictable name so the next start can begin clean: a sibling can create
# entries in the sticky collection, and a guessable name it had taken would
# make the rename fail. If the rename still fails, the start is refused all the
# same. A walk that cannot be completed is refused too. The walk stops at the
# first foreign entry; a clean home costs one lstat per entry.
_PRIVATE_MEMBER_HOME_VERIFY = (
    'foreign=$(find "$target" -xdev ! -uid "$(id -u)" -print -quit); '
    'if test -n "$foreign"; then '
    "attempt=0; "
    'until aside=$(mktemp -u -- "$target.refused-XXXXXXXXXXXXXXXX") '
    '&& mv -T -- "$target" "$aside" 2>/dev/null; do '
    'attempt=$((attempt + 1)); test "$attempt" -lt 3 || break; done; '
    'echo "member provider home holds another UID files" >&2; exit 1; fi; '
)
_PRIVATE_MEMBER_HOME_NARROW_MIGRATED = (
    'if test -n "$migrated"; then '
    'find "$target" -xdev -mindepth 1 ! -type l -exec chmod go=,g-s -- {} +; fi; '
)
PRIVATE_MEMBER_HOME_PROLOGUE = (
    _PRIVATE_MEMBER_HOME_CHECK
    + _PRIVATE_MEMBER_HOME_MIGRATE
    + _PRIVATE_MEMBER_HOME_CLAIM
    + _PRIVATE_MEMBER_HOME_VERIFY
    + _PRIVATE_MEMBER_HOME_NARROW_MIGRATED
)
#: For a home that has no earlier location: a fork child's.
NEW_PRIVATE_MEMBER_HOME_PROLOGUE = _PRIVATE_MEMBER_HOME_CHECK + _PRIVATE_MEMBER_HOME_CLAIM


def member_state_dir(launch: ResidentLaunch) -> str:
    """This member's private provider home, or the shared state root for a CLI without one."""
    root = PRIVATE_MEMBER_STATE_ROOTS.get(launch.runtime_id)
    if root is None:
        return CHAT_STATE_DIR
    member_key = hashlib.sha256(launch.thread_id.encode("utf-8")).hexdigest()
    return f"{root}/{member_key}"


def legacy_member_state_dir(launch: ResidentLaunch) -> str:
    """Where an earlier release kept this member's provider home."""
    member_key = hashlib.sha256(launch.thread_id.encode("utf-8")).hexdigest()
    return f"{_LEGACY_MEMBER_STATE_ROOTS[launch.runtime_id]}/{member_key}"
