"""Report which routes still take the old store, and what each one uses.

The migration of ``cloud_agent/api/routes.py`` happens one handler at a time, so
at any moment the file is half-moved. This says exactly which half: for every
function that resolves the store from a dependency, which capabilities its body
actually calls. That turns "how much is left" into a list rather than an
impression, and it attributes each remaining handler to the ports it needs
before anyone edits it.

Nothing here rewrites code. The first attempt at this migration did, with text
substitution, and pointed several handlers at the wrong capability because their
parameter is also named ``store``. Deciding a rewrite is safe needs the parse
tree, and applying one needs a test run per handler; this tool is the first half.

Usage:
    python tools/store_boundary_report.py
    python tools/store_boundary_report.py --remaining    # only the unmoved ones
"""

from __future__ import annotations

import argparse
import ast
import collections
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import split_store as s

ROUTES = REPO / "cloud_agent" / "api" / "routes.py"

#: Parameter names that carry the old store.
STORE_PARAMS = {"store", "job_store"}


def port_for(method: str) -> str | None:
    """Return the port that owns an old store method, or ``None``."""
    target = s.APPENDIX.get(method)
    return target.port if target else None


def survey(path: pathlib.Path) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """Return, per function, the ports it uses and the methods this cannot place.

    A function is "moved" when it no longer calls anything on the store; a
    function that takes the store only to pass it on is reported with the
    methods it uses indirectly through its callees.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    direct: dict[str, set[str]] = {}
    unknown: dict[str, list[str]] = {}
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not ({a.arg for a in func.args.args + func.args.kwonlyargs} & STORE_PARAMS):
            continue
        methods: set[str] = set()
        for node in ast.walk(func):
            if not isinstance(node, ast.Attribute):
                continue
            # ``store.method`` and ``_require_store(store).method`` are the same
            # call written two ways. Missing the second is how this report once
            # claimed three handlers were already moved when they were not.
            value = node.value
            if (isinstance(value, ast.Name) and value.id in STORE_PARAMS) or (
                isinstance(value, ast.Call)
                and getattr(value.func, "id", None) == "_require_store"
                and value.args
                and isinstance(value.args[0], ast.Name)
                and value.args[0].id in STORE_PARAMS
            ):
                methods.add(node.attr)
        if not methods:
            continue
        ports: set[str] = set()
        unplaced: list[str] = []
        for method in methods:
            port = port_for(method)
            if port is None:
                unplaced.append(method)
            else:
                ports.add(port)
        direct[func.name] = ports
        if unplaced:
            unknown[func.name] = sorted(unplaced)
    return direct, unknown


def main(argv: list[str] | None = None) -> int:
    """Print the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--remaining",
        action="store_true",
        help="list only functions that still call the store directly",
    )
    args = parser.parse_args(argv)

    direct, unknown = survey(ROUTES)
    totals: collections.Counter[str] = collections.Counter()
    for ports in direct.values():
        totals.update(ports)

    if not args.remaining:
        print(f"functions still calling the store directly: {len(direct)}")
        print("\ncapabilities they need, by number of functions:")
        for port, count in totals.most_common():
            print(f"  {port:26s} {count:3d}")
    else:
        for name, ports in sorted(direct.items()):
            print(f"  {name:44s} {sorted(ports)}")

    if unknown:
        print("\nmethods this package cannot place (a real gap, not a leftover):")
        for name, methods in sorted(unknown.items()):
            print(f"  {name}: {methods}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
