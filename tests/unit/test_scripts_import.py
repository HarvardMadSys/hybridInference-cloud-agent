"""Every script under ``scripts/`` still imports against the code it drives.

Nothing runs these on a schedule: they are measurement and maintenance tools
an operator reaches for by hand, so a rename in a package they import breaks
them silently. ``measure_admin_usage.py`` imported the store module the
storage-boundary refactor deleted, and stayed broken for a month without
anything noticing. Importing each one is cheap and catches exactly that,
without running what it measures.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.mark.parametrize("name", sorted(path.stem for path in SCRIPTS.glob("*.py")))
def test_the_script_imports(name: str) -> None:
    importlib.import_module(f"scripts.{name}")
