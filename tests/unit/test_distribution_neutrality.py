"""The public code names the FreeInference deployment no more than it already does.

This repository is deployment-neutral; FreeInference is one deployment that
consumes its releases. ``tests/unit/distribution_neutrality_allowlist.json``
lists every tracked file that still mentions it, how often, and under which
reason (``CATEGORIES`` in ``scripts/check_distribution_neutrality.py`` says
what each reason covers). A new mention fails here: name the thing
generically, or make it configurable. One that belongs to an allowed reason
goes into the allowlist under that reason, where review sees it.
``deployment-branding`` is debt: the failure message does not offer it for a
new mention, but nothing stops ``--write`` raising it, so review must.

The counts only move down, as in the test baseline of ``test_file_size_budget.py``:
an entry above its file's count is stale, so removing mentions means lowering
the entry, and removing the last one means removing it.

    uv run python scripts/check_distribution_neutrality.py --write

rewrites the allowlist to the current counts, keeping each file under its
reason. A new file outside ``docs/agents/`` lands under ``unclassified``, which
fails here until it is moved under a real reason or its mention goes. Its
summary names every raised or added entry; read it before committing.

Not counted (``EXCLUDED`` in the script): the check, this test and the
allowlist, which have to name what they look for; ``uv.lock`` and
``package-lock.json``, generated from manifests that are counted;
``apps/backend/openapi.json``, generated from the routes and settings, which
are counted; and the translation catalogs under ``docs/developer/locale/``,
which copy the documentation pages, which are counted.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from scripts import check_distribution_neutrality as neutrality

if TYPE_CHECKING:
    from pathlib import Path

WRITE = "uv run python scripts/check_distribution_neutrality.py --write"
REASONS = ", ".join(neutrality.CATEGORIES)
#: Branding is debt that only shrinks; every other reason may take a new mention.
GROWING = ", ".join(reason for reason in neutrality.CATEGORIES if reason != "deployment-branding")


@pytest.fixture(scope="module")
def counts() -> dict[str, int]:
    return neutrality.mentions()


@pytest.fixture(scope="module")
def allowlist() -> neutrality.Allowlist:
    return neutrality.load()


def test_no_file_names_the_deployment_beyond_its_allowance(
    counts: dict[str, int], allowlist: neutrality.Allowlist
) -> None:
    over = neutrality.over(counts, allowlist)
    assert not over, (
        "names the FreeInference deployment more often than allowed (mentions, allowed). This "
        "repository is deployment-neutral: name the thing generically or make it configurable. "
        f"A mention that is one of the allowed kinds ({GROWING}) goes into "
        f"{neutrality.ALLOWLIST} under its kind: {over}"
    )


def test_the_allowlist_is_exact(counts: dict[str, int], allowlist: neutrality.Allowlist) -> None:
    stale = neutrality.stale(counts, allowlist)
    assert not stale, (
        "allowlist is stale (mentions, allowed); lower the entry after removing mentions, and "
        f"remove it at zero. `{WRITE}` does both: {stale}"
    )


def test_every_entry_carries_a_known_reason(allowlist: neutrality.Allowlist) -> None:
    unknown = {
        category: sorted(entries)
        for category, entries in allowlist.items()
        if category not in neutrality.CATEGORIES
    }
    assert not unknown, (
        f"move each entry under one of {REASONS} in {neutrality.ALLOWLIST}, "
        f"or remove the mention: {unknown}"
    )


def test_only_design_records_are_historical(allowlist: neutrality.Allowlist) -> None:
    prefix, category = neutrality.RECORDS
    misfiled = sorted(
        name
        for reason, entries in allowlist.items()
        for name in entries
        if (reason == category) != name.startswith(prefix)
    )
    assert not misfiled, (
        f"{category} is for the dated records under {prefix}, and every one of them is that: "
        f"{misfiled}"
    )


def test_every_exclusion_still_names_a_tracked_file() -> None:
    names = neutrality.tracked()
    missing = [
        entry
        for entry in neutrality.EXCLUDED
        if not any(neutrality.covers(entry, name) for name in names)
    ]
    assert not missing, f"excluded but not tracked; remove them from EXCLUDED: {missing}"


def _repository(tmp_path: Path, files: dict[str, bytes]) -> Path:
    for name, data in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(data)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_every_spelling_in_a_tracked_path_or_text_counts_and_nothing_else(
    tmp_path: Path,
) -> None:
    repo = _repository(
        tmp_path,
        {
            "deploy/sandbox/pi-freeinference": b"#!/bin/sh\nexec pi --provider FreeInference\n",
            "apps/backend/labels.py": b'LABEL = "org.freeinference.chat"  # FREEINFERENCE_X\n',
            "apps/frontend/logo-freeinference.png": b"\x89PNG\0freeinference",
            "apps/backend/neutral.py": b"LABEL = 'org.cloud-agent.chat'\n",
            "uv.lock": b'name = "freeinference-client"\n',
            "docs/developer/locale/zh_CN/LC_MESSAGES/index.po": b'msgid "FreeInference"\n',
        },
    )
    (repo / "untracked.md").write_text("FreeInference\n")

    assert neutrality.mentions(repo) == {
        "deploy/sandbox/pi-freeinference": 2,
        "apps/backend/labels.py": 2,
        "apps/frontend/logo-freeinference.png": 1,
    }


def test_over_and_stale_name_every_drift() -> None:
    allowlist = {
        "persistent-identifier": {"a.py": 2, "gone.py": 1},
        "provider-example": {"b.md": 3},
    }
    counts = {"a.py": 3, "b.md": 1, "new.py": 1}

    assert neutrality.over(counts, allowlist) == {"a.py": (3, 2), "new.py": (1, 0)}
    assert neutrality.stale(counts, allowlist) == {"b.md": (1, 3), "gone.py": (0, 1)}


def test_write_keeps_each_reason_and_files_new_mentions_for_a_human() -> None:
    allowlist = {
        "deployment-branding": {"apps/x.py": 4, "apps/gone.py": 1},
        "persistent-identifier": {"deploy/y.sh": 2},
    }
    counts = {"apps/x.py": 1, "deploy/y.sh": 3, "docs/agents/specs/new.md": 5, "apps/new.py": 1}

    regenerated = neutrality.regenerate(counts, allowlist)

    assert regenerated == {
        "persistent-identifier": {"deploy/y.sh": 3},
        "historical-record": {"docs/agents/specs/new.md": 5},
        "deployment-branding": {"apps/x.py": 1},
        neutrality.UNCLASSIFIED: {"apps/new.py": 1},
    }
    # The order the file is written in: the reasons as CATEGORIES lists them, then the rest.
    assert list(regenerated) == [
        "persistent-identifier",
        "historical-record",
        "deployment-branding",
        neutrality.UNCLASSIFIED,
    ]
    assert neutrality.changes(allowlist, regenerated) == [
        "removed  apps/gone.py: 1 -> 0",
        "added    apps/new.py: 0 -> 1",
        "lowered  apps/x.py: 4 -> 1",
        "raised   deploy/y.sh: 2 -> 3",
        "added    docs/agents/specs/new.md: 0 -> 5",
        "choose a category for apps/new.py",
    ]


def test_the_allowlist_is_in_the_form_write_produces(allowlist: neutrality.Allowlist) -> None:
    """A hand edit keeps the file's order, so the next ``--write`` changes only counts."""
    on_disk = (neutrality.REPO / neutrality.ALLOWLIST).read_text(encoding="utf-8")
    canonical = neutrality.dump(neutrality.regenerate(neutrality.allowed(allowlist), allowlist))
    assert on_disk == canonical, (
        f"{neutrality.ALLOWLIST} is not in the order `{WRITE}` writes; run it, then check the diff"
    )
