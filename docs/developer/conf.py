"""Sphinx configuration for the Cloud Agent developer documentation.

The site is plain MyST Markdown -- no page uses autodoc, so nothing here
imports the application. `sphinx-build` needs only Sphinx, myst-parser and the
theme, and makes no network requests, so an offline contributor gets the same
result as CI.

Pages link freely to repository files that are not part of the site: scripts,
examples, tests and dated design records. Those links work when the Markdown is
read on GitHub, so the build rewrites each one to the same file on GitHub
instead of reporting it as broken. A few root-level guides are shown on the
site through `{include}` wrappers; links to them go to their site page. A link
to a path that does not exist in the repository is still a build error.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from docutils import nodes
from sphinx import addnodes
from sphinx.transforms import i18n as _sphinx_i18n
from sphinx.util import logging

# -- Project information -----------------------------------------------------

project = "HybridInference Cloud Agent"
copyright = "2026, The HybridInference contributors"
author = "The HybridInference contributors"

REPOSITORY_URL = "https://github.com/HarvardMadSys/hybridInference-cloud-agent"
# Links to repository files point at the integration branch, which is what the
# published site is built from.
REPOSITORY_BRANCH = "dev"

# -- General configuration ---------------------------------------------------

extensions = [
    "myst_parser",
]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "tasklist",
]

# Generate implicit anchors for h1-h4 headings so GitHub-style in-page links
# (`[text](#some-heading)`) resolve. Without this the Markdown renders fine on
# GitHub but Sphinx reports `myst.xref_missing`, which fails the `-W` build.
myst_heading_anchors = 4

exclude_patterns = ["_build"]
templates_path = ["_templates"]

# -- Translations ------------------------------------------------------------
# The pages are written in English and translated with Sphinx's gettext
# workflow: `make docs-gettext` extracts one catalog per page, a translator
# fills in the `msgstr` entries, and `make docs-lang DOCS_LANG=<code>` builds
# that language. Every string without a translation falls back to the English
# source, so a partly-translated language still builds a complete site -- and
# when an English paragraph changes, its `msgid` changes with it and the page
# falls back to English rather than serving a translation that no longer
# matches. See translations.md.
language = os.environ.get("DOCS_LANGUAGE", "en").strip() or "en"
locale_dirs = ["locale"]

# One catalog per page rather than one per directory, so a pull request shows
# which page a translation touches.
gettext_compact = False

# Languages the published site offers, as `code:endonym` pairs, in switcher
# order. The first is the root language, published at the site root
# (`/operations.html`); every other language goes in a directory named for its
# code (`/zh_CN/operations.html`), which keeps every URL the English site had.
#
# The default is deliberately not empty: the published site is built by one
# `sphinx-build` whose command line lives in the hosting project's settings,
# not in this repository, so the list has to travel here. The root-language
# build emits every other language beneath its own output directory
# (`_build_nested_languages` below). `DOCS_LANGUAGES="en:English"` builds the
# root language alone.
_languages = []
for _entry in os.environ.get("DOCS_LANGUAGES", "en:English,zh_CN:简体中文").split(","):
    _code, _, _label = _entry.strip().partition(":")
    # A code reaches an href, so accept only the shape a locale code has.
    if _code.replace("_", "").isalnum() and _label.strip():
        _languages.append({"code": _code, "label": _label.strip()})
if len(_languages) < 2:
    # One language needs no switcher, and a malformed value must not render a
    # half-built one.
    _languages = []
_root_language = _languages[0]["code"] if _languages else language

# A translated paragraph's links are re-parsed from its msgstr, and Sphinx then
# copies each original link's attributes onto the translated link with the same
# (domain, type, target) key. MyST keeps a link's anchor apart from its target,
# in `reftargetid`, so two links to different headings of one page share a key
# and the last one's anchor overwrote the others: "see [A](ops.md#a) and
# [B](ops.md#b)" rendered both links to #b. Keep the anchor the translation
# itself names; the translation check confirms it matches the English.
_sphinx_i18n.EXCLUDED_PENDING_XREF_ATTRIBUTES = (
    *_sphinx_i18n.EXCLUDED_PENDING_XREF_ATTRIBUTES,
    "reftargetid",
)

# Mermaid diagrams and dotenv examples have no Pygments lexer. They render as
# plain preformatted text, which is how GitHub shows the dotenv blocks anyway.
suppress_warnings = ["misc.highlighting_failure"]

source_suffix = {
    ".md": "markdown",
}

# -- Options for HTML output -------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_title = "Cloud Agent documentation"

html_context = {
    "doc_languages": _languages,
    "doc_root_language": _root_language,
}

html_theme_options = {
    "navigation_depth": 4,
    "collapse_navigation": False,
    "sticky_navigation": True,
    "includehidden": True,
    "titles_only": False,
}

# -- Repository files shown or linked from the site -------------------------

_SOURCE_DIR = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _SOURCE_DIR.parent.parent

# Pages that show a Markdown file kept elsewhere in the repository, through a
# one-line `{include}` wrapper of the same name. The file stays where readers
# and tests already expect it; links inside it are written relative to it, so
# they are resolved from there rather than from the wrapper.
INCLUDED_PAGES = {
    "configuration": "ENVIRONMENT.md",
    "local-stack": "deploy/local/README.md",
    "releases": "deploy/release/README.md",
    "gateway-contracts": "gateway_contracts/README.md",
    "security": "SECURITY.md",
    "contributing": "CONTRIBUTING.md",
}
_PAGE_FOR_SOURCE = {
    (_REPOSITORY_ROOT / source).resolve(): page for page, source in INCLUDED_PAGES.items()
}


def _repository_url(path: Path, anchor: str) -> str:
    relative = path.relative_to(_REPOSITORY_ROOT).as_posix()
    kind = "tree" if path.is_dir() else "blob"
    url = f"{REPOSITORY_URL}/{kind}/{REPOSITORY_BRANCH}/{relative}"
    return f"{url}#{anchor}" if anchor else url


def _classify(path: Path) -> tuple[str, str | Path] | None:
    """Say whether a linked repository path is a site page or a repository file.

    Returns ``("page", docname)``, ``("repository", path)``, or ``None`` for a
    path Sphinx should resolve and check itself -- including one that does not
    exist, which then fails the build.
    """
    path = path.resolve()
    if not path.is_relative_to(_REPOSITORY_ROOT) or not path.exists():
        return None
    if path in _PAGE_FOR_SOURCE:
        return "page", _PAGE_FOR_SOURCE[path]
    if path.is_relative_to(_SOURCE_DIR):
        if path.suffix == ".md" and path.parent == _SOURCE_DIR:
            return "page", path.stem
        return None
    return "repository", path


def _linked_path(node, base: Path) -> tuple[Path, str] | None:
    """Return the repository path and anchor a MyST link names."""
    if node.get("refdomain") == "doc":
        # MyST resolved the link to a document. A page inside the site is a
        # plain docname and needs nothing; one outside it is an absolute path
        # without its suffix.
        target = Path(node.get("reftarget", ""))
        if not target.is_absolute():
            return None
        return target.with_name(target.name + ".md"), node.get("reftargetid") or ""
    path, _, anchor = node.get("reftarget", "").partition("#")
    return (base / path, anchor) if path else None


def _rewrite_repository_links(app, doctree):
    """Point links at site pages or, for other repository files, at GitHub."""
    docname = app.env.docname
    if docname in INCLUDED_PAGES:
        base = (_REPOSITORY_ROOT / INCLUDED_PAGES[docname]).parent
    else:
        base = Path(app.env.doc2path(docname)).parent

    links = [
        node for node in doctree.findall(addnodes.pending_xref) if node.get("reftype") == "myst"
    ]
    for node in links + list(doctree.findall(addnodes.download_reference)):
        if isinstance(node, addnodes.download_reference):
            target = node.get("reftarget", "")
            linked = (base / target, "") if target else None
        else:
            linked = _linked_path(node, base)
        classified = _classify(linked[0]) if linked else None
        if classified is None:
            continue
        kind, value = classified
        anchor = linked[1]
        if kind == "page":
            uri = app.builder.get_relative_uri(docname, value)
            reference = nodes.reference(
                "", "", refuri=f"{uri}#{anchor}" if anchor else uri, internal=True
            )
            if anchor:
                # Checked once every page has been read; see _check_anchors.
                reference[_ANCHOR_CHECK] = f"{value}#{anchor}"
        else:
            reference = nodes.reference(
                "", "", refuri=_repository_url(value, anchor), internal=False
            )
        reference += node.children
        node.replace_self(reference)


_ANCHOR_CHECK = "cloud_agent_anchor"


def _check_anchors(app, doctree, docname):
    """Fail the build for a rewritten link to a heading that does not exist.

    MyST checks the anchors of links it resolves itself. The links rewritten
    above bypass that, so the same check runs here against MyST's heading slugs.
    """
    for node in doctree.findall(nodes.reference):
        target = node.get(_ANCHOR_CHECK)
        if not target:
            continue
        page, _, anchor = target.partition("#")
        if anchor not in app.env.metadata.get(page, {}).get("myst_slugs", {}):
            logging.getLogger(__name__).warning(
                "local id not found in doc %r: %r",
                page,
                anchor,
                location=node,
                type="myst",
                subtype="xref_missing",
            )


# Set in the child processes below so a nested build does not nest again.
_NESTED_ENV = "DOCS_NESTED_BUILD"


def _build_nested_languages(app, exception):
    """Build every non-root language into ``<outdir>/<code>/``.

    Sphinx builds one language per invocation, and the published site gets one
    invocation this repository does not control. Running the other languages
    from ``build-finished`` is what lets that single command publish all of
    them. The child inherits ``-W``, so a translation that breaks the build
    fails the parent too. Doctrees go to a temporary directory so the published
    tree contains only HTML.
    """
    if exception is not None or os.environ.get(_NESTED_ENV) == "1":
        return
    # Only the root-language build nests; a `-D language=zh_CN` build is a
    # translator iterating on one language or the child process below.
    if app.builder.name != "html" or app.config.language != _root_language:
        return
    nested = [entry["code"] for entry in _languages if entry["code"] != _root_language]
    if not nested:
        return

    env = {**os.environ, _NESTED_ENV: "1"}
    scratch = tempfile.mkdtemp(prefix="sphinx-i18n-")
    try:
        for code in nested:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "sphinx",
                    "-b",
                    "html",
                    str(app.srcdir),
                    os.path.join(str(app.outdir), code),
                    "-d",
                    os.path.join(scratch, code),
                    "-D",
                    f"language={code}",
                    "-W",
                    "--keep-going",
                    "-q",
                ],
                env=env,
                check=True,
            )
            print(f"  translated site: {os.path.join(str(app.outdir), code)}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def setup(app):
    """Register the repository-link rewrite, its anchor check and translations."""
    # Before the download collector, so repository files are not copied into
    # the site as downloads.
    app.connect("doctree-read", _rewrite_repository_links, priority=400)
    app.connect("doctree-resolved", _check_anchors)
    app.connect("build-finished", _build_nested_languages)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
