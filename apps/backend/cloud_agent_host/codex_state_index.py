"""Codex's private session index, and the rebase a fork copy needs.

Codex records each rollout's absolute path in its session index. Every member
home holds its own copy of that index, so the paths a fork copied still name the
*source* member's home: the child can open neither the directory nor the file,
and cannot resume its own forked thread.

Kept out of ``resident_transport`` because it is a script run inside the sandbox
rather than host code, and because the transport module is at its size budget.
"""

from __future__ import annotations

from types import MappingProxyType

from cloud_agent_shared.shared_protocol import SHARED_SANDBOX_RUNTIME_PINS

#: The file each reviewed Codex release keeps its session index in, beside the
#: rollout files it names. Keyed by version rather than stated once, so the pin
#: (``SHARED_SANDBOX_RUNTIME_PINS``) cannot move to a release that renamed it
#: without a test saying so: a stale name would have the rebase find no index,
#: call that nothing to do, and hand every fork child a thread it cannot resume.
#: A release with no entry here is never guessed at: a fork with it is refused,
#: and a member's own start leaves its index as copied.
CODEX_STATE_INDEX_BY_VERSION = MappingProxyType({"0.145.0": "state_5.sqlite"})


def codex_state_index(version: str | None) -> str | None:
    """The session index file ``version`` keeps, or ``None`` when none is on record.

    A launch without a version runs the image's own Codex, which is the pin.
    """
    return CODEX_STATE_INDEX_BY_VERSION.get(version or SHARED_SANDBOX_RUNTIME_PINS["codex"])


# Codex records each rollout's absolute path in its private session index. Every
# member home holds its own copy of that index, so the paths a fork copied still
# name the *source* member's home: the child can open neither the directory nor
# the file, and cannot resume its own forked thread.
#
# The rule is about the collection, not about one fork: a rollout path rooted at
# some *other* member home of this collection names a file this member's own copy
# of the tree holds, under its own home. Re-rooting it at $target is therefore
# both what a fork needs and what repairs a child forked by an earlier release --
# so this runs on every Codex start as well as at fork time, and repeating it
# changes nothing.
#
# An earlier release kept every home in another collection, which a member's
# first start moves its home out of. A path rooted at a home there -- a sibling
# this home was forked from, or this very home before the move -- names a file
# this home holds the same way, so it is re-rooted too. That collection may be
# gone, so like the current one it is judged from the path text, and only a
# name a member home has (the hash of its thread) counts.
#
# The index is the CLI's own file, so this reads it as an unknown shape: an
# absent index, a table it cannot query, or no path in another member's home all
# mean there is nothing to do.
#
# Two callers, two answers when it will not rewrite:
#
# - ``strict`` is the fork path. It has a journal and a retry, and shipping a
#   copy this script could not verify would hand the child a thread it cannot
#   resume, so it fails the step and the PREPARING journal is retried. It also
#   names the child's thread, whose row must then name a rollout in this home:
#   that row is the one thing the child will resume from.
# - ``repair`` is the member's own start. There is no retry there: refusing
#   would wedge every future start of that member over state its own CLI is
#   about to read anyway and can report far better. It leaves the copy alone
#   and says so on stderr -- whatever went wrong, including what this script
#   did not foresee.
#
# Neither mode awards this script any trust: the real boundary is the 0700 home
# per member UID. What it must never do is *widen* a path, which is why a rebased
# path is refused if it would resolve outside this home.
#
# Arguments: the home, the mode, the index file name (empty when this Codex
# release has none on record), the earlier release's collection, and in strict
# mode the child's thread id. Each arrives as its own argv entry and reaches the
# index only as a bound parameter.
REBASE_ROLLOUT_PATHS = r"""
import re
import sqlite3
import sys
from pathlib import Path

target = Path(sys.argv[1])
# The fork path can afford to fail; a member's own start cannot.
strict = sys.argv[2] == "strict"
index_name = sys.argv[3]
child = sys.argv[5] if strict else None
# Codex records the literal CODEX_HOME it was given, which need not be resolved
# (a symlinked /agent-state would not be), so match literal prefixes here and
# resolve only to decide whether a rebased path stays inside this home.
home = str(target).rstrip("/")
# (collection, whether a home there with this home's own name is skipped): in
# the current collection that name is this home itself, already rooted right.
collections = (
    (str(target.parent).rstrip("/") + "/", True),
    (sys.argv[4].rstrip("/") + "/", False),
)


def nothing_to_do():
    raise SystemExit(0)


def refuse(reason):
    if strict:
        raise SystemExit(reason)
    print("codex session index left as copied: " + reason, file=sys.stderr)
    raise SystemExit(0)


def in_member_home(path):
    # The part of ``path`` below a member home this home's copy stands in for.
    # A member home is named by the hash of its thread. Read the home out of the
    # path itself rather than probing the directory: the sibling a fork copied
    # from may have been deleted since, and the files it named are in this
    # member's own copy.
    if not isinstance(path, str):
        return None
    for collection, other_only in collections:
        if not path.startswith(collection):
            continue
        name, _, rest = path[len(collection):].partition("/")
        if re.fullmatch(r"[0-9a-f]{64}", name) and not (other_only and name == target.name):
            return rest
    return None


def stays_home(path, suffix, root):
    relative = Path(suffix)
    return (
        bool(relative.parts)
        and ".." not in relative.parts
        and Path(path).resolve(strict=False).is_relative_to(root)
    )


def rebase():
    if not index_name:
        refuse("no session index is on record for this Codex release")
    index = target / index_name
    if index.is_symlink():
        refuse("the session index is not a file in this home")
    if not index.is_file():
        nothing_to_do()
    root = target.resolve(strict=True)
    # A member's CLI may be writing this index, and its own start must not wait
    # on that for long; a fork's copy cannot be in use, so it can afford to wait.
    connection = sqlite3.connect(index, timeout=30 if strict else 5)
    try:
        try:
            rows = connection.execute("SELECT id, rollout_path FROM threads").fetchall()
        except sqlite3.Error as exc:
            # Not a shape this member's CLI owns: its own start reports it.
            nothing_to_do() if "no such table" in str(exc) else refuse(str(exc))
        stored_paths = dict(rows)
        rebased = {}
        for thread_id, path in rows:
            rest = in_member_home(path)
            if rest is not None:
                rebased[thread_id] = (home + "/" + rest, rest)
        for path, suffix in rebased.values():
            if not stays_home(path, suffix, root):
                refuse("a rollout path in another member home escaped this one")
        if child is not None:
            # Whatever the general rewrite did, the fork's own thread is the
            # one the child resumes: its row must name a rollout in this home.
            # (An index that is absent, or has no threads table, holds no row
            # that could send the child elsewhere; that is left to the CLI.)
            if child not in stored_paths:
                refuse("the forked thread has no row in the session index")
            own = rebased[child][0] if child in rebased else stored_paths[child]
            if not (
                isinstance(own, str)
                and own.startswith(home + "/")
                and stays_home(own, own[len(home) + 1:], root)
                and Path(own).is_file()
            ):
                refuse("the forked thread's rollout is not a file in this home")
        if not rebased:
            nothing_to_do()
        try:
            for thread_id, (path, _suffix) in rebased.items():
                connection.execute(
                    "UPDATE threads SET rollout_path = ? WHERE id = ?", (path, thread_id)
                )
        except sqlite3.Error as exc:
            refuse(str(exc))
        # The index carries whatever schema the CLI wrote, triggers included, so
        # a row can be rewritten behind this update. Read every one back before
        # the commit: a rebase that did not stick leaves the thread unable to
        # resume.
        for thread_id, (path, _suffix) in rebased.items():
            stored = connection.execute(
                "SELECT rollout_path FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if stored is None or stored[0] != path:
                refuse("a rollout path rebase did not persist")
        # Only now that rows changed is the index's own health this script's
        # business: an untouched copy is the CLI's to judge, and an integrity
        # complaint about it must not fail a start.
        try:
            healthy = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except sqlite3.Error as exc:
            refuse(str(exc))
        if not healthy:
            refuse("the session index failed its integrity check")
        connection.commit()
    finally:
        connection.close()


try:
    rebase()
except Exception as exc:
    # A start must not fail over this index for any reason, the unforeseen
    # included: a symlink loop under a rebased path, a commit the disk refuses.
    # A rollback is what closing an uncommitted connection does.
    refuse(type(exc).__name__ + ": " + str(exc))
"""
