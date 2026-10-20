"""A translation behind its English fails a stable page and only warns elsewhere."""

from __future__ import annotations

from pathlib import Path

from scripts import check_docs_translations as check

REPO = Path(__file__).resolve().parents[2]


def _catalog(locale: Path, page: str) -> None:
    """One zh_CN catalog for ``page`` holding a single fuzzy entry."""
    messages = locale / "zh_CN" / "LC_MESSAGES"
    messages.mkdir(parents=True, exist_ok=True)
    (messages / f"{page}.po").write_text(
        'msgid ""\nmsgstr ""\n\n#, fuzzy\nmsgid "Hello."\nmsgstr "你好。"\n', encoding="utf-8"
    )


def test_a_fuzzy_entry_fails_a_stable_page_and_warns_on_another(tmp_path: Path) -> None:
    _catalog(tmp_path, "development")
    _catalog(tmp_path, "operations")

    problems, warnings = check.split_by_page(check.check_catalogs(tmp_path))

    assert len(problems) == 1 and "development.po" in problems[0]
    assert len(warnings) == 1 and "operations.po" in warnings[0]


def test_every_stable_page_is_a_page_of_the_site() -> None:
    pages = {path.stem for path in (REPO / "docs" / "developer").glob("*.md")}
    assert pages >= check.STRICT_PAGES, sorted(check.STRICT_PAGES - pages)
