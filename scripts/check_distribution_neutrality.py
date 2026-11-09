"""Keep the FreeInference deployment's name from spreading through the public code.

    uv run python scripts/check_distribution_neutrality.py          # compare with the allowlist
    uv run python scripts/check_distribution_neutrality.py --write  # rewrite it to today's counts

This repository is deployment-neutral: FreeInference is one deployment that
consumes its releases, and its own configuration lives in its own repository.
The name still appears in tracked files for reasons listed in ``CATEGORIES``,
and ``tests/unit/distribution_neutrality_allowlist.json`` records, per
category, how many times each file mentions it.
``tests/unit/test_distribution_neutrality.py`` fails when a file mentions it
more often than its entry allows, when a file without an entry mentions it at
all, and, like the test baseline in ``test_file_size_budget.py``, when an entry
is higher than its file's count: the counts only move down.

A mention is a case-insensitive match anywhere in a tracked file's path or
text, so ``deploy/sandbox/pi-freeinference`` counts its own name once. Binary
files count their path only. ``EXCLUDED`` lists the tracked files that are not
counted and why.

``--write`` regenerates the allowlist from the files as they are: it lowers
and removes entries after mentions go, keeps each listed file under its
category, files a new record under ``docs/agents/`` as ``historical-record``,
and files any other new mention as ``unclassified``, which the test refuses
until someone moves it under a real category or removes the mention. It raises
counts too, so read the summary it prints, and the diff, before committing.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ALLOWLIST = Path("tests/unit/distribution_neutrality_allowlist.json")
NAME = re.compile(r"freeinference", re.IGNORECASE)

#: Why a file may still mention the deployment, in the order the allowlist lists them.
CATEGORIES = {
    "persistent-identifier": (
        "A name stored outside this code: Docker labels, container, image and network names, "
        "paths inside sandboxes and on hosts, wrapper binaries baked into images, environment "
        "variables, defaults existing deployments rely on, and the tests that pin them. Renaming "
        "one strands what older hosts, images or saved state carry, so each goes through the "
        "dual-read rename in docs/agents/specs/2026-11-09-neutral-runtime-identifiers.md."
    ),
    "historical-record": (
        "A dated design record or review under docs/agents/, which keeps the names it was "
        "written with."
    ),
    "provider-example": (
        "Names FreeInference as one model provider among others: its key format, a sample "
        "provider in a fixture, or interface copy that lists providers."
    ),
    "hosted-deployment-mention": "Points readers at the hosted FreeInference deployment.",
    "migration-source": (
        "Names the FreeInference system the one-time account import reads from; it stays while "
        "the importer does."
    ),
    "neutrality-guard": (
        "A test that refuses the name somewhere else, so it has to spell it: in the text it "
        "searches for and in the samples it must catch."
    ),
    "deployment-branding": (
        "Text, a sample value or a default that still presents the FreeInference deployment as "
        "the product. Debt: make it neutral or configurable, then lower the entry."
    ),
}
#: The category ``--write`` gives a new mention outside docs/agents/; the test refuses it.
UNCLASSIFIED = "unclassified"
RECORDS = ("docs/agents/", "historical-record")

#: Tracked files that are not counted, and why. A prefix ends with ``/``.
EXCLUDED = {
    "scripts/check_distribution_neutrality.py": "this check, which names what it looks for",
    "tests/unit/test_distribution_neutrality.py": "its test, whose samples name it",
    str(ALLOWLIST): "the allowlist, whose keys are paths that name it",
    "uv.lock": "generated from pyproject.toml, which is counted",
    "apps/frontend/package-lock.json": "generated from apps/frontend/package.json, which is counted",
    "apps/backend/openapi.json": (
        "generated from the backend's routes and settings, which are counted; "
        "tests/unit/test_api_surface_contract.py keeps the two equal"
    ),
    "docs/developer/locale/": (
        "translation catalogs: `make docs-translate` copies each page's English text into them, "
        "and the pages are counted"
    ),
}

Allowlist = dict[str, dict[str, int]]


def tracked(repo: Path = REPO) -> list[str]:
    """Every path ``git ls-files`` lists, relative to ``repo``."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, capture_output=True, check=True
    ).stdout
    return [name.decode() for name in out.split(b"\0") if name]


def covers(entry: str, name: str) -> bool:
    """Whether an ``EXCLUDED`` entry, a path or a prefix ending in ``/``, covers ``name``."""
    return name.startswith(entry) if entry.endswith("/") else name == entry


def excluded(name: str) -> bool:
    return any(covers(entry, name) for entry in EXCLUDED)


def _text(path: Path) -> str:
    """A regular file's text; nothing for a binary file, a symlink or a missing file."""
    if path.is_symlink() or not path.is_file():
        return ""
    data = path.read_bytes()
    return "" if b"\0" in data else data.decode("utf-8", "replace")


def mentions(repo: Path = REPO, names: list[str] | None = None) -> dict[str, int]:
    """How often each counted file names the deployment, for files that do."""
    counts = {}
    for name in tracked(repo) if names is None else names:
        if excluded(name):
            continue
        found = len(NAME.findall(name)) + len(NAME.findall(_text(repo / name)))
        if found:
            counts[name] = found
    return counts


def load(path: Path = REPO / ALLOWLIST) -> Allowlist:
    allowlist: Allowlist = json.loads(path.read_text(encoding="utf-8"))
    return allowlist


def allowed(allowlist: Allowlist) -> dict[str, int]:
    """Each listed file's allowance, whatever its category."""
    return {name: count for entries in allowlist.values() for name, count in entries.items()}


def over(counts: dict[str, int], allowlist: Allowlist) -> dict[str, tuple[int, int]]:
    """Files above their allowance, or unlisted ones at all: ``(mentions, allowed)``."""
    limits = allowed(allowlist)
    return {
        name: (count, limits.get(name, 0))
        for name, count in sorted(counts.items())
        if count > limits.get(name, 0)
    }


def stale(counts: dict[str, int], allowlist: Allowlist) -> dict[str, tuple[int, int]]:
    """Entries above their file's count, including files that no longer mention it."""
    return {
        name: (counts.get(name, 0), limit)
        for name, limit in sorted(allowed(allowlist).items())
        if counts.get(name, 0) < limit
    }


def _category(name: str, previous: dict[str, str]) -> str:
    if name in previous:
        return previous[name]
    prefix, category = RECORDS
    return category if name.startswith(prefix) else UNCLASSIFIED


def regenerate(counts: dict[str, int], allowlist: Allowlist) -> Allowlist:
    """The allowlist that matches ``counts``, each file kept under its category."""
    previous = {name: category for category, entries in allowlist.items() for name in entries}
    grouped: Allowlist = {}
    for name, count in sorted(counts.items()):
        grouped.setdefault(_category(name, previous), {})[name] = count
    order = [*CATEGORIES, UNCLASSIFIED, *sorted(grouped.keys() - {*CATEGORIES, UNCLASSIFIED})]
    return {category: grouped[category] for category in order if category in grouped}


def dump(allowlist: Allowlist) -> str:
    return json.dumps(allowlist, indent=2, ensure_ascii=False) + "\n"


def _change(was: int | None, now: int | None) -> str:
    if was is None:
        return "added"
    if now is None:
        return "removed"
    return "raised" if now > was else "lowered"


def changes(before: Allowlist, after: Allowlist) -> list[str]:
    """One line per entry ``--write`` changed, so a raise is read rather than missed."""
    old, new = allowed(before), allowed(after)
    lines = []
    for name in sorted(old.keys() | new.keys()):
        was, now = old.get(name), new.get(name)
        if was == now:
            continue
        lines.append(f"{_change(was, now):8} {name}: {was or 0} -> {now or 0}")
    unclassified = sorted(after.get(UNCLASSIFIED, {}))
    lines.extend(f"choose a category for {name}" for name in unclassified)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write", action="store_true", help=f"rewrite {ALLOWLIST} to the current counts"
    )
    args = parser.parse_args(argv)
    counts, allowlist = mentions(), load()
    if args.write:
        regenerated = regenerate(counts, allowlist)
        (REPO / ALLOWLIST).write_text(dump(regenerated), encoding="utf-8")
        print("\n".join(changes(allowlist, regenerated)) or f"{ALLOWLIST} was already current")
        return 0
    found = {"over": over(counts, allowlist), "stale": stale(counts, allowlist)}
    problems = [
        f"{kind} (mentions, allowed): {entries}" for kind, entries in found.items() if entries
    ]
    print("\n".join(problems) or f"{ALLOWLIST} matches the tree")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
