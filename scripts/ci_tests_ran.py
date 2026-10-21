"""Fail unless every test in a pytest junit report actually ran.

    uv run python scripts/ci_tests_ran.py "${RUNNER_TEMP}/report.xml"

The external tests CI runs are handed what they need inline, such as an image,
a binary or an SDK path, and each one skips without it. That is right on a
laptop and the failure mode in CI: a renamed variable, a moved path or an image
that was never pulled turns the check into a green step that ran nothing, and
pytest exits 0 when every test skips. So the report is read instead of the exit
code. A test that skipped fails this, whether it skipped while running or its
module skipped at collection, and so does a report without a single test. An
expected failure does not: junit records it as a skip of its own type, but it
ran.

The database tier is read the same way, with a floor:

    uv run python scripts/ci_tests_ran.py --min 100 --running-skips-only "${RUNNER_TEMP}/dbtest.xml"

A `pytest -m` that stops selecting the tier exits 0 having run nothing, or
almost nothing. The floor only has to be high enough that such a run cannot
clear it, so adding tests never fails it.

A run over the whole tree imports every test module to select the tier by
marker, and one outside the tier that skips at import says nothing about the
database. So the tier passes `--running-skips-only`, which counts only the skips
junit gives a type, and a module skipped at collection has none.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def declined(report: Path, *, running_only: bool = False) -> tuple[int, list[str]]:
    """Count the report's test cases and name every one that skipped.

    With ``running_only``, a module skipped at collection is not named: junit
    gives that skip no type.
    """
    ignored = {"pytest.xfail", None} if running_only else {"pytest.xfail"}
    cases = list(ET.parse(report).getroot().iter("testcase"))
    skipped = [
        f"{case.get('classname', '')}::{case.get('name', '')}"
        for case in cases
        for skip in case.findall("skipped")
        if skip.get("type") not in ignored
    ]
    return len(cases), skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reports", nargs="+", type=Path, help="files written by --junitxml")
    parser.add_argument(
        "--min", type=int, default=1, metavar="N", help="fail a report with fewer than N tests"
    )
    parser.add_argument(
        "--running-skips-only",
        action="store_true",
        help="count only tests that skipped while running, not modules skipped at collection",
    )
    args = parser.parse_args(argv)
    failed = False
    for report in args.reports:
        ran, skipped = declined(report, running_only=args.running_skips_only)
        print(f"{report}: {ran} collected, {len(skipped)} skipped")
        if not ran:
            print(f"{report}: no test ran", file=sys.stderr)
            failed = True
        elif ran < args.min:
            print(f"{report}: only {ran} ran, fewer than the {args.min} expected", file=sys.stderr)
            failed = True
        for name in skipped:
            print(f"{report}: {name} skipped; what it needs never arrived", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
