"""Prove every moved method is unchanged.

For each of the 108 Appendix A operations this compares the emitted method
against the original store method after applying the same requalification the
splitter is allowed to apply. Two normalisations are needed, and both are
whitespace-only:

1. re-indenting a method into a class body changes a string literal's leading
   indentation, and
2. the original writes long SQL as several implicitly concatenated literals
   while an unparser may reconstruct them differently. Joining adjacent
   literals before comparing removes a distinction that never reaches
   PostgreSQL.

Code, bind-parameter order, branch order and lock order are compared exactly.

Run:  python tools/equivalence_check.py
"""

from __future__ import annotations

import ast
import difflib
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import split_store as s


def merge_adjacent_literals(node: ast.AST) -> None:
    """Join adjacent string literals in every call's argument list."""
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        merged: list[ast.expr] = []
        for arg in call.args:
            previous = merged[-1] if merged else None
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and isinstance(previous, ast.Constant)
                and isinstance(previous.value, str)
            ):
                merged[-1] = ast.Constant(value=previous.value + arg.value)
            elif isinstance(arg, ast.JoinedStr) and isinstance(previous, ast.JoinedStr):
                merged[-1] = ast.JoinedStr(values=[*previous.values, *arg.values])
            else:
                merged.append(arg)
        call.args = merged


def flatten_literals(node: ast.AST) -> None:
    """Collapse whitespace runs inside every string literal."""
    for part in ast.walk(node):
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            part.value = " ".join(part.value.split())
        elif isinstance(part, ast.JoinedStr):
            for value in part.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    value.value = " ".join(value.value.split())


def canon(text: str) -> str:
    """Canonical form of a function, whitespace inside literals normalised."""
    tree = ast.parse(text)
    merge_adjacent_literals(tree)
    flatten_literals(tree)
    return ast.unparse(tree)


def emitted_methods() -> tuple[dict[str, str], dict[str, str]]:
    """Return every emitted method, and the file each one came from."""
    code: dict[str, str] = {}
    where: dict[str, str] = {}
    for path in sorted((REPO / "cloud_agent/storage/repositories").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        code[member.name] = ast.unparse(member)
                        where[member.name] = path.name
    return code, where


def main() -> int:
    """Compare every emitted method with its original."""
    source, _tree, cls = s.load_store()
    public, _private = s.inventory(source, cls)
    code, where = emitted_methods()

    expected = set(s.APPENDIX)
    missing = sorted(expected - set(code))
    print(f"appendix operations  : {len(expected)}")
    print(f"emitted methods      : {len(expected) - len(missing)}")
    if missing:
        print(f"MISSING              : {missing}")
        return 1

    # Each method is requalified for the module it now lives in: a module that
    # owns a moved constant refers to it directly, one that does not gets the
    # alias. Deriving the expectation the same way keeps the check honest.
    different: list[str] = []
    for name in sorted(expected):
        module = s.APPENDIX[name].module
        want = canon(
            s.requalify(
                s.slice_node(source, public[name].node),
                owner=s.repository_path(module),
            )[0]
        )
        got = canon(code[name])
        if got != want:
            different.append(name)

    print(f"identical after split: {len(expected) - len(different)}/{len(expected)}")
    if different:
        print(f"DIFFERENT: {different}")
        name = different[0]
        want = canon(s.requalify(s.slice_node(source, public[name].node))[0])
        for line in difflib.unified_diff(
            want.splitlines(),
            canon(code[name]).splitlines(),
            f"expected/{name}",
            f"emitted/{where[name]}",
            lineterm="",
            n=0,
        ):
            print(line[:400])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
