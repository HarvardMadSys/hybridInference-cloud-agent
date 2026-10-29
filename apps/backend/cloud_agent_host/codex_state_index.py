"""Codex's private session index, and the rebase a fork copy needs.

Codex records each rollout's absolute path in ``state_5.sqlite``. Every member
home holds its own copy of that index, so the paths a fork copied still name the
*source* member's home: the child can open neither the directory nor the file,
and cannot resume its own forked thread.

Kept out of ``resident_transport`` because it is a script run inside the sandbox
rather than host code, and because the transport module is at its size budget.
"""

from __future__ import annotations

#: Codex's private session index, beside the rollout files it names. It tracks
#: the pinned Codex version (``SHARED_SANDBOX_RUNTIME_PINS``); the rebase below
#: treats a different layout as nothing to do rather than a failure, so a
#: future rename costs this repair, not every fork.
CODEX_STATE_INDEX = "state_5.sqlite"

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
# The index is the CLI's own file, so this reads it as an unknown shape: an
# absent index, a table it cannot query, or no path in another member's home all
# mean there is nothing to do.
#
# Two callers, two answers when it will not rewrite:
#
# - ``strict`` is the fork path. It has a journal and a retry, and shipping a
#   copy this script could not verify would hand the child a thread it cannot
#   resume, so it fails the step and the PREPARING journal is retried.
# - ``repair`` is the member's own start. There is no retry there: refusing
#   would wedge every future start of that member over state its own CLI is
#   about to read anyway and can report far better. It leaves the copy alone
#   and says so on stderr.
#
# Neither mode awards this script any trust: the real boundary is the 0700 home
# per member UID. What it must never do is *widen* a path, which is why a rebased
# path is refused if it would resolve outside this home.
REBASE_ROLLOUT_PATHS = r"""
import re
import sqlite3
import sys
from pathlib import Path

target = Path(sys.argv[1])
# The fork path can afford to fail; a member's own start cannot.
strict = len(sys.argv) > 2 and sys.argv[2] == "strict"
# Codex records the literal CODEX_HOME it was given, which need not be resolved
# (a symlinked /agent-state would not be), so match literal prefixes here and
# resolve only to decide whether a rebased path stays inside this home.
home = str(target).rstrip("/")
collection = str(target.parent).rstrip("/") + "/"


def nothing_to_do():
    raise SystemExit(0)


def refuse(reason):
    if strict:
        raise SystemExit(reason)
    print("codex session index left as copied: " + reason, file=sys.stderr)
    raise SystemExit(0)


index = target / "__INDEX__"
if index.is_symlink() or not index.is_file():
    nothing_to_do()
# A member's CLI may be writing this index, and its own start must not wait on
# that for long; a fork's copy cannot be in use, so it can afford to wait.
try:
    root = target.resolve(strict=True)
except OSError as exc:
    refuse(str(exc))
try:
    connection = sqlite3.connect(index, timeout=30 if strict else 5)
except sqlite3.Error as exc:
    refuse(str(exc))
try:
    try:
        rows = connection.execute("SELECT id, rollout_path FROM threads").fetchall()
    except sqlite3.Error as exc:
        # Not a shape this member's CLI owns: its own start reports it.
        nothing_to_do() if "no such table" in str(exc) else refuse(str(exc))
    # A member home is named by the hash of its thread, and every one of them
    # lives in this collection. Read the home out of the path itself rather than
    # probing the directory: the sibling a fork copied from may have been
    # deleted since, and the files it named are in this member's own copy.
    rebased = {}
    for thread_id, path in rows:
        if not isinstance(path, str) or not path.startswith(collection):
            continue
        sibling, _, rest = path[len(collection):].partition("/")
        if not re.fullmatch(r"[0-9a-f]{64}", sibling) or sibling == target.name:
            continue
        rebased[thread_id] = (home + "/" + rest, rest)
    if not rebased:
        nothing_to_do()
    for thread_id, (path, suffix) in rebased.items():
        relative = Path(suffix)
        if not relative.parts or ".." in relative.parts:
            refuse("a rollout path in another member home escaped this one")
        if not Path(path).resolve(strict=False).is_relative_to(root):
            refuse("a rollout path in another member home escaped this one")
    try:
        for thread_id, (path, _suffix) in rebased.items():
            connection.execute(
                "UPDATE threads SET rollout_path = ? WHERE id = ?", (path, thread_id)
            )
    except sqlite3.Error as exc:
        refuse(str(exc))
    # The index carries whatever schema the CLI wrote, triggers included, so a
    # row can be rewritten behind this update. Read every one back before the
    # commit: a rebase that did not stick leaves the thread unable to resume.
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
""".replace("__INDEX__", CODEX_STATE_INDEX)
