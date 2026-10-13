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
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def declined(report: Path) -> tuple[int, list[str]]:
    """Count the report's test cases and name every one that skipped."""
    cases = list(ET.parse(report).getroot().iter("testcase"))
    skipped = [
        f"{case.get('classname', '')}::{case.get('name', '')}"
        for case in cases
        for skip in case.findall("skipped")
        if skip.get("type") != "pytest.xfail"
    ]
    return len(cases), skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reports", nargs="+", type=Path, help="files written by --junitxml")
    args = parser.parse_args(argv)
    failed = False
    for report in args.reports:
        ran, skipped = declined(report)
        print(f"{report}: {ran} collected, {len(skipped)} skipped")
        if not ran:
            print(f"{report}: no test ran", file=sys.stderr)
            failed = True
        for name in skipped:
            print(f"{report}: {name} skipped; what it needs never arrived", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
