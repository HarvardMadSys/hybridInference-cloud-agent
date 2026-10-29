"""Migration-chain hygiene: one head, unique numbers, and no orphaned tables.

The chain is healthy in the ways that matter and untidy in ways that cost
reviewers. Both facts are worth pinning, because the failure modes are quiet:

* **A duplicate revision number** is legal to Alembic -- revisions are keyed by
  the full id, not the number -- so ``0046_job_claim_retry`` and
  ``0046_admin_sandbox_trajectory`` both exist and both revise ``0045``. The
  cost is human: two branches picked the same number, noticed at merge time,
  and the resolution needed a merge revision. A third collision is not a
  disaster; it is a sign nobody is checking, so this checks.

* **An orphaned table** is a table the chain creates and nothing ever reads.
  ``agent_pr_archive_checks`` belongs to a feature deleted in #736 and the
  table stayed. It is harmless in production and it is a lie in the schema: a
  reader of the baseline sees a feature that no longer exists.

The distinction this file cares about is *live* versus *created*. Several
tables are created and dropped inside the chain -- ``agent_thread_residencies``
(created in the baseline, dropped by ``0004``), and the whole
``agent_mcp_legacy_bindings`` family (created and dropped by ``0008``). Those
are history, correct and finished. Only a table that is still standing after
the last revision can be an orphan, so the set is computed by replaying the
chain rather than by scanning for ``CREATE TABLE``.

Deliberately not asserted: that the chain has no churn at all. Rewriting the
history to squash ``0008``'s create-then-drop into the baseline would change
every deployed revision id, which is a far worse problem than an untidy chain.
"""

from __future__ import annotations

import ast
import collections
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "migrations/versions"
BACKEND = REPO / "apps" / "backend"

#: Revision-number collisions that already happened and were resolved by hand.
#: Listed so a *new* collision fails while these three stay documented. Each
#: entry is the number and the revisions that share it.
KNOWN_NUMBER_COLLISIONS: dict[str, tuple[str, ...]] = {
    "0046": ("0046_admin_sandbox_trajectory", "0046_job_claim_retry"),
    "0051": ("0051_host_drain_fence_epoch", "0051_web_search_admission"),
    "0053": ("0053_gitlab_refresh_fence", "0053_project_memory"),
}

#: Tables the chain leaves standing that no Python code reads. Each needs a
#: reason, because "nobody got to it" is how the next one gets added.
KNOWN_ORPHANS: dict[str, str] = {
    "agent_pr_archive_checks": (
        "created by 0040_pr_merge_archive; the feature was removed in #736 and "
        "the table was left behind. Dropping it is a one-line migration."
    ),
    "agent_mcp_catalog_cache": (
        "created by 0014_mcp_catalog_cache and written by 0061 in SQL only; the "
        "runtime reads agent_mcp_catalog_snapshots instead, so no Python reader "
        "exists. Removing it needs the 0061 writer removed first."
    ),
}


def _revision_pair(path: Path) -> tuple[str, tuple[str, ...]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            target = getattr(node.targets[0], "id", None)
            if target in ("revision", "down_revision"):
                values[target] = ast.literal_eval(node.value)
    parents = values.get("down_revision")
    if parents is None:
        parent_tuple: tuple[str, ...] = ()
    elif isinstance(parents, str):
        parent_tuple = (parents,)
    else:
        parent_tuple = tuple(parents)  # type: ignore[arg-type]
    return str(values["revision"]), parent_tuple


def _chain() -> dict[str, tuple[Path, tuple[str, ...]]]:
    chain: dict[str, tuple[Path, tuple[str, ...]]] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        revision, parents = _revision_pair(path)
        chain[revision] = (path, parents)
    return chain


def _upgrade_text(path: Path) -> str:
    """The string literals an ``upgrade()`` body runs, and nothing from ``downgrade()``.

    See ``test_lifecycle_vocabulary`` for why the split matters: several
    migrations re-create in ``downgrade()`` what ``upgrade()`` removed, so a
    whole-file scan reads the reverse migration as the forward one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            literals = [
                (child.lineno, child.value)
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            ]
            literals.sort(key=lambda item: item[0])
            return "\n".join(value for _, value in literals)
    return ""


def _live_tables() -> set[str]:
    """Tables standing after the last revision, computed by replaying the chain."""
    chain = _chain()
    ordered: list[str] = []
    done: set[str] = set()
    while len(ordered) < len(chain):
        progressed = False
        for revision, (_, parents) in chain.items():
            if revision in done or not all(parent in done for parent in parents):
                continue
            ordered.append(revision)
            done.add(revision)
            progressed = True
        assert progressed, "the migration chain has a cycle or a missing parent"

    live: set[str] = set()
    for revision in ordered:
        path = chain[revision][0]
        body = (
            path.read_text(encoding="utf-8")
            if revision.startswith("0001_")
            else _upgrade_text(path)
        )
        for match in re.finditer(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", body):
            live.add(match.group(1))
        for match in re.finditer(r"DROP TABLE (?:IF EXISTS )?(\w+)", body):
            live.discard(match.group(1))
    return live


def _backend_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in BACKEND.rglob("*.py")
        if "__pycache__" not in str(path)
    )


# ── the chain's shape ─────────────────────────────────────────────────────


def test_the_chain_has_exactly_one_head() -> None:
    """Two heads make ``alembic upgrade head`` ambiguous and deploys non-deterministic."""
    chain = _chain()
    parents = {parent for _, revision_parents in chain.values() for parent in revision_parents}
    heads = [revision for revision in chain if revision not in parents]
    assert len(heads) == 1, f"the chain has {len(heads)} heads: {sorted(heads)}"


def test_every_down_revision_names_a_file_that_exists() -> None:
    chain = _chain()
    for _revision, (path, parents) in chain.items():
        for parent in parents:
            assert parent in chain, (
                f"{path.name} revises {parent!r}, which no revision file declares"
            )


def test_no_revision_number_is_used_twice_without_being_known() -> None:
    """A new collision means two branches picked the same number again.

    Alembic tolerates it (ids are the key, not numbers), so nothing else in the
    system notices until a human reads the directory. Reusing a number makes
    "which file is 0046?" ambiguous in review, in this file, and in the release
    manifest's ``database_revision`` when someone pastes a short form.
    """
    numbers = collections.defaultdict(list)
    for path in VERSIONS.glob("*.py"):
        revision, _ = _revision_pair(path)
        numbers[revision.split("_")[0]].append(revision)
    found = {
        number: tuple(sorted(revisions))
        for number, revisions in numbers.items()
        if len(revisions) > 1
    }
    assert found == KNOWN_NUMBER_COLLISIONS, (
        "the set of duplicated revision numbers changed. Absorb the new pair into "
        f"KNOWN_NUMBER_COLLISIONS with a note, or renumber it: {found}"
    )


def test_each_known_collision_is_still_real() -> None:
    """Guard against a stale allow-list: silence about fixed problems is worse."""
    numbers = collections.defaultdict(list)
    for path in VERSIONS.glob("*.py"):
        revision, _ = _revision_pair(path)
        numbers[revision.split("_")[0]].append(revision)
    for number, known in KNOWN_NUMBER_COLLISIONS.items():
        assert sorted(numbers[number]) == sorted(known), (
            f"{number} no longer collides as recorded ({sorted(numbers[number])}); "
            "drop the entry so the register stays true"
        )


# ── reaches every revision ────────────────────────────────────────────────


def test_every_revision_is_reachable_from_the_root() -> None:
    """A revision off to the side would be silently skipped by ``upgrade head``."""
    chain = _chain()
    roots = [revision for revision, (_, parents) in chain.items() if not parents]
    assert roots == ["0001_schema_baseline"], roots
    reachable = {"0001_schema_baseline"}
    while True:
        newly = {
            revision
            for revision, (_, parents) in chain.items()
            if parents and set(parents) <= reachable
        }
        updated = reachable | newly
        if updated == reachable:
            break
        reachable = updated
    assert reachable == set(chain), f"unreachable revisions: {sorted(set(chain) - reachable)}"


# ── orphaned tables ───────────────────────────────────────────────────────


def test_no_table_stands_after_the_chain_without_a_reader_or_a_reason() -> None:
    """A live table nothing reads is a schema that advertises a dead feature."""
    live = _live_tables()
    assert live, "the table replay produced nothing; the parser lost its target"
    source = _backend_source()
    orphans = {table for table in live if table not in source}
    unexplained = orphans - set(KNOWN_ORPHANS)
    assert not unexplained, (
        f"tables created by the chain and read by no Python code: {sorted(unexplained)}. "
        "Either drop them in a migration or add them to KNOWN_ORPHANS with the reason"
    )


def test_the_orphan_register_does_not_rot() -> None:
    """Every table recorded as an orphan must still be an orphan.

    Without this, the register becomes a place orphan tables go to be forgotten:
    a table gets a reader and the entry stays, so the next reader believes the
    schema still carries dead weight that it does not.
    """
    live = _live_tables()
    source = _backend_source()
    for table in KNOWN_ORPHANS:
        if table not in live:
            continue  # dropped -- the register entry is merely historical
        assert table not in source, (
            f"{table} is recorded as an orphan but Python code now reads it; "
            "drop its KNOWN_ORPHANS entry"
        )


def test_tables_that_were_created_and_dropped_are_not_treated_as_orphans() -> None:
    """The distinction the replay exists for.

    ``agent_thread_residencies`` is created in the baseline and dropped by
    ``0004``. A scan for ``CREATE TABLE`` alone would call it an orphan and
    invite a "fix" that re-creates a table the design retired.
    """
    live = _live_tables()
    retired_in_chain = {
        "agent_thread_residencies",  # 0001 -> dropped by 0004
        "agent_mcp_legacy_bindings",  # created and dropped by 0008
        "agent_mcp_transport_sessions",  # created and dropped by 0008
    }
    still_standing = retired_in_chain & live
    assert not still_standing, (
        f"tables the chain retires are standing again: {sorted(still_standing)}"
    )
