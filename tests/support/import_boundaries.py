"""Resolve Python imports for source-level dependency boundary checks."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def imported_modules(source: str, *, package: str) -> set[str]:
    """Return import targets, including relative and from-package imports.

    Scan every AST node so imports inside functions and TYPE_CHECKING blocks
    obey the same rules as imports at module scope.
    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = node.module or ""
            if node.level:
                target = resolve_name("." * node.level + target, package)
            modules.add(target)
            modules.update(f"{target}.{alias.name}" for alias in node.names if alias.name != "*")
    return modules


def file_imports(path: Path, *, root: Path, package_name: str) -> set[str]:
    """Resolve a source file's imports against its containing package."""
    relative_parent = path.relative_to(root).parent.parts
    package = ".".join((package_name, *relative_parent))
    return imported_modules(path.read_text(encoding="utf-8"), package=package)


def matches_package(module: str, package: str) -> bool:
    """Match a package itself or a module beneath it."""
    return module == package or module.startswith(f"{package}.")
