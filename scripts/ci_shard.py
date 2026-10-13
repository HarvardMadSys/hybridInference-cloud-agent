"""Print the test files one CI shard runs, one per line.

    uv run python scripts/ci_shard.py --shard 2/3 -- -m "not external and not dbtest"

Everything after ``--`` is handed to ``pytest --collect-only``, so a shard
splits exactly the tests that one unsharded run would select. Files are the
unit, never single tests: both tiers run under ``--dist loadfile``, and the
database tier's fixtures are written per file.

Files are dealt largest-first to whichever shard has the fewest tests so far,
ties broken by path. The split is a function of the collected files alone, so
every shard of a run computes the same one, and between them they cover each
file exactly once.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter


def partition(counts: dict[str, int], total: int) -> list[list[str]]:
    """Split files into ``total`` groups of roughly equal test counts."""
    loads = [0] * total
    shards: list[list[str]] = [[] for _ in range(total)]
    for path, tests in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        target = min(range(total), key=lambda index: (loads[index], index))
        loads[target] += tests
        shards[target].append(path)
    return [sorted(files) for files in shards]


def collect(pytest_args: list[str]) -> dict[str, int]:
    """Count the tests pytest would run for these arguments, by file."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *pytest_args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"collection failed:\n{result.stdout}{result.stderr}")
    return dict(
        Counter(line.split("::", 1)[0] for line in result.stdout.splitlines() if "::" in line)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shard", required=True, help="INDEX/TOTAL, counting from 1")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    index, total = (int(part) for part in args.shard.split("/", 1))
    if not 1 <= index <= total:
        parser.error(f"--shard {args.shard}: INDEX must be between 1 and TOTAL")
    pytest_args = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    counts = collect(pytest_args)
    if not counts:
        raise SystemExit("pytest collected no tests for these arguments")
    files = partition(counts, total)[index - 1]
    # Empty would make pytest fall back to every test path, not run nothing.
    if not files:
        raise SystemExit(f"shard {args.shard} received no files; use fewer shards")
    print("\n".join(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
