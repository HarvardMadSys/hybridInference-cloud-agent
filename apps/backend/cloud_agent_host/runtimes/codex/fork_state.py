"""The script that rebases a forked Codex home's rollout index into the child home."""

from __future__ import annotations

# Codex stores absolute rollout paths in its private SQLite index. A private
# fork copies that database and rollout files into a different member home, so
# its paths must be rebased before the child app-server can resume a thread.
#
# This reads Codex's private schema: ``state_5.sqlite`` and its ``threads``
# table, as written by the pinned Codex release. A Codex upgrade that renames
# the database or moves ``rollout_path`` makes every Codex fork fail here, by
# design, until this script follows it.
#
# Codex records each path under the CODEX_HOME string it was started with, so
# the homes are matched as the literal paths the broker passes, never resolved:
# a resolved prefix would miss those rows, and resolving the source home would
# need the child UID to reach a home it must not open. Resolution is used only
# to prove that a rebased path stays inside the child home. The child UID runs
# this with ``-I``, so neither its environment nor its user site-packages can
# change the code that checks containment.
CODEX_REBASE_ROLLOUT_PATHS = r"""
import os
import sqlite3
import sys

target, source = sys.argv[1], sys.argv[2]
for home in (target, source):
    if not os.path.isabs(home) or os.path.normpath(home) != home:
        raise SystemExit("Codex fork homes must be normalized absolute paths")
prefix = source + "/"
real_target = os.path.realpath(target)
database = os.path.join(target, "state_5.sqlite")
if os.path.islink(database) or not os.path.isfile(database):
    raise SystemExit("Codex fork state has no regular state_5.sqlite")
connection = sqlite3.connect(database, isolation_level=None)
try:
    connection.execute("BEGIN IMMEDIATE")
    rows = connection.execute("SELECT id, rollout_path FROM threads").fetchall()
    changed = 0
    for thread_id, rollout_path in rows:
        if not (isinstance(rollout_path, str) and rollout_path.startswith(prefix)):
            continue
        relative = rollout_path[len(prefix):]
        # normpath rewrites any "..", "." or empty component, so a path it
        # changes is one that is not plainly a file under the source home.
        if not relative or os.path.normpath(rollout_path) != rollout_path:
            raise SystemExit("Codex fork rollout path escaped its private home")
        rebased = os.path.join(target, relative)
        real = os.path.realpath(rebased)
        if os.path.commonpath([real, real_target]) != real_target:
            raise SystemExit("Codex fork rollout path escaped its private home")
        connection.execute(
            "UPDATE threads SET rollout_path = ? WHERE id = ?",
            (rebased, thread_id),
        )
        changed += 1
    remaining = connection.execute(
        "SELECT COUNT(*) FROM threads WHERE substr(rollout_path, 1, ?) = ?",
        (len(prefix), prefix),
    ).fetchone()[0]
    if changed == 0 or remaining:
        raise SystemExit("Codex fork rollout path rebase was incomplete")
    # Checked inside the transaction, so a damaged index is never committed.
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("Codex fork index failed integrity check")
    connection.execute("COMMIT")
finally:
    # Closing with the transaction still open rolls every update back.
    connection.close()
"""
