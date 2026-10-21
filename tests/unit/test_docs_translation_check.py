"""A translation behind its English fails a stable page and only warns elsewhere."""

from __future__ import annotations

from pathlib import Path

import pytest
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


_PAGE = (
    '<html><div itemprop="articleBody"><p><code>x</code>{extra}</p></div><footer></footer></html>'
)
_PAGES = ("index", "development", "code-structure", "security", "contributing", "operations")


def _po(*entries: tuple[str, str, str]) -> str:
    out = 'msgid ""\nmsgstr ""\n'
    for comment, msgid, msgstr in entries:
        out += (
            f'\n{comment}\nmsgid "{msgid}"\nmsgstr "{msgstr}"\n'
            if comment
            else (f'\nmsgid "{msgid}"\nmsgstr "{msgstr}"\n')
        )
    return out


def _site(root: Path) -> list[str]:
    """A built site whose zh_CN translation is current, and the checker's arguments."""
    build, locale, gettext = root / "html", root / "locale", root / "gettext"
    (build / "zh_CN").mkdir(parents=True)
    (locale / "zh_CN" / "LC_MESSAGES").mkdir(parents=True)
    gettext.mkdir()
    for page in _PAGES:
        for tree in (build, build / "zh_CN"):
            (tree / f"{page}.html").write_text(_PAGE.format(extra=""), encoding="utf-8")
        (gettext / f"{page}.pot").write_text(_po(("", "Hello.", "")), encoding="utf-8")
        (locale / "zh_CN" / "LC_MESSAGES" / f"{page}.po").write_text(
            _po(("", "Hello.", "你好。")), encoding="utf-8"
        )
    (build / "zh_CN" / "searchindex.js").write_text(
        'Search.setIndex({"terms": {"沙箱": 1}})', encoding="utf-8"
    )
    return [str(build), str(locale), "--gettext-dir", str(gettext)]


def _stale(root: Path, page: str) -> None:
    """The English page gained a sentence that no catalog entry translates."""
    (root / "gettext" / f"{page}.pot").write_text(
        _po(("", "Hello.", ""), ("", "A new sentence.", "")), encoding="utf-8"
    )


def test_a_current_translation_passes(tmp_path: Path) -> None:
    assert check.main(_site(tmp_path)) == 0


def test_a_stale_stable_page_fails(tmp_path: Path) -> None:
    argv = _site(tmp_path)
    _stale(tmp_path, "development")

    assert check.main(argv) == 1


def test_a_stale_page_that_may_trail_only_warns(tmp_path: Path, capsys) -> None:
    argv = _site(tmp_path)
    _stale(tmp_path, "operations")

    assert check.main(argv) == 0
    captured = capsys.readouterr()
    assert "operations.po: no entry" in captured.err
    assert "1 string(s) on other pages are behind" in captured.out


def _block_start(root: Path) -> None:
    (root / "locale" / "zh_CN" / "LC_MESSAGES" / "operations.po").write_text(
        _po(("", "Hello.", "1. 你好。")), encoding="utf-8"
    )


def _structure(root: Path) -> None:
    (root / "html" / "zh_CN" / "operations.html").write_text(
        _PAGE.format(extra="<em>x</em>"), encoding="utf-8"
    )


def _search_index(root: Path) -> None:
    (root / "html" / "zh_CN" / "searchindex.js").write_text(
        'Search.setIndex({"terms": {"sandbox": 1}})', encoding="utf-8"
    )


@pytest.mark.parametrize("break_it", [_block_start, _structure, _search_index])
def test_a_translation_that_renders_wrong_fails_on_any_page(tmp_path: Path, break_it) -> None:
    argv = _site(tmp_path)
    break_it(tmp_path)

    assert check.main(argv) == 1
