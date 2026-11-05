"""The deployment's site files: Terms of Service, the Team page and the source link."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException, Response

from cloud_agent import site_content
from cloud_agent.api import admin_settings, auth, site_config

if TYPE_CHECKING:
    from pathlib import Path

SITE_YAML = """\
source_url: https://github.com/example/cloud-agent
members:
  - name: Ada Lovelace
    website: https://ada.example.org/
    badge: {en: Lead, zh-CN: 负责人}
    roles:
      - {en: Professor, zh-CN: 教授}
      - Visiting scholar
  - name: Grace Hopper
"""


@pytest.fixture
def site_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(site_content, "SITE_CONTENT_DIR", tmp_path)
    return tmp_path


def test_absent_directory_means_nothing_is_configured(tmp_path: Path) -> None:
    assert site_content.site_content(tmp_path / "missing") == site_content.SiteContent()


def test_reads_terms_by_locale_and_the_team(site_dir: Path) -> None:
    (site_dir / "terms.en.txt").write_text("Terms.\n\nSecond paragraph.\n")
    (site_dir / "terms.zh-CN.txt").write_text("条款。\n")
    (site_dir / "site.yaml").write_text(SITE_YAML)

    content = site_content.site_content()

    assert content.terms == {"en": "Terms.\n\nSecond paragraph.", "zh-CN": "条款。"}
    assert content.source_url == "https://github.com/example/cloud-agent"
    assert [member.public() for member in content.team] == [
        {
            "name": "Ada Lovelace",
            "website": "https://ada.example.org/",
            "badge": {"en": "Lead", "zh-CN": "负责人"},
            "roles": [
                {"en": "Professor", "zh-CN": "教授"},
                {"en": "Visiting scholar", "zh-CN": "Visiting scholar"},
            ],
        },
        {"name": "Grace Hopper", "website": "", "badge": {}, "roles": []},
    ]


def test_a_changed_file_is_read_again(site_dir: Path) -> None:
    terms = site_dir / "terms.en.txt"
    terms.write_text("First.")
    assert site_content.site_content().terms == {"en": "First."}
    terms.write_text("Second, longer.")
    stat = terms.stat()
    os.utime(terms, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert site_content.site_content().terms == {"en": "Second, longer."}


async def test_site_config_publishes_a_configured_portrait(site_dir: Path) -> None:
    (site_dir / "site.yaml").write_text(
        "members:\n  - name: Ada\n    photo_url: https://example.org/ada.jpg\n"
    )
    published = (await site_config.site_config(Response()))["content"]
    assert published["team"][0]["photoUrl"] == "https://example.org/ada.jpg"


def test_inline_raster_photo_is_preserved_without_reencoding() -> None:
    photo = "data:image/jpeg;base64,/9j/2Q=="
    _, members = site_content.parse_site_yaml(f"members:\n  - name: Ada\n    photo_url: {photo}\n")
    assert members[0].public()["photoUrl"] == photo


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/a.jpg",
        "javascript:alert(1)",
        "https://user:pass@example.org/a.jpg",
        "data:image/svg+xml;base64,PHN2Zy8+",
        "data:image/jpeg;base64,not-base64!",
        "data:image/jpeg;base64,",
    ],
)
def test_portraits_reject_unsafe_or_malformed_sources(url: str) -> None:
    with pytest.raises(site_content.SiteContentError, match=r"members\[0\].photo_url"):
        site_content.parse_site_yaml(f"members:\n  - name: Ada\n    photo_url: {url}\n")


def test_blank_terms_file_counts_as_absent(site_dir: Path) -> None:
    (site_dir / "terms.en.txt").write_text("  \n\n")
    assert site_content.site_content().terms == {}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("- just a list", "must be a mapping"),
        ("owner: someone", "unknown keys: owner"),
        ("source_url: javascript:alert(1)", "absolute http(s) URL"),
        ("members:\n  - website: https://x.example/", "members[0].name"),
        ("members:\n  - name: A\n    email: a@example.org", "unknown keys: email"),
        ("members:\n  - name: A\n    badge: {fr: Chef}", "unknown locales: fr"),
        ("members:\n  - name: A\n    website: ftp://x.example/", "members[0].website"),
        ("members: [", "not valid YAML"),
    ],
)
def test_invalid_site_yaml_is_refused_with_where(raw: str, message: str) -> None:
    with pytest.raises(site_content.SiteContentError) as refused:
        site_content.parse_site_yaml(raw)
    assert message in str(refused.value)


def test_invalid_site_yaml_hides_the_team_but_keeps_the_terms(
    site_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (site_dir / "terms.en.txt").write_text("Terms.")
    (site_dir / "site.yaml").write_text("owner: someone")

    content = site_content.site_content()

    assert content.terms == {"en": "Terms."}
    assert content.team == ()
    assert "site_content_ignored" in caplog.text


def test_oversized_terms_file_is_ignored(site_dir: Path) -> None:
    (site_dir / "terms.en.txt").write_bytes(b"x" * (256 * 1024 + 1))
    assert site_content.site_content().terms == {}


async def test_site_config_publishes_the_team_and_source(site_dir: Path) -> None:
    (site_dir / "site.yaml").write_text(SITE_YAML)

    published = (await site_config.site_config(Response()))["content"]

    assert published["sourceUrl"] == "https://github.com/example/cloud-agent"
    assert [member["name"] for member in published["team"]] == ["Ada Lovelace", "Grace Hopper"]


async def test_site_config_without_files_publishes_no_team(site_dir: Path) -> None:
    published = (await site_config.site_config(Response()))["content"]

    assert published["sourceUrl"] == ""
    assert published["team"] == []


def test_terms_files_replace_the_admin_settings(site_dir: Path) -> None:
    (site_dir / "terms.zh-CN.txt").write_text("条款。")
    (site_dir / "terms.en.txt").write_text("Terms.")

    assert auth._terms() == {
        "terms_url": "",
        "terms_text": "Terms.",
        "terms_by_locale": {"en": "Terms.", "zh-CN": "条款。"},
    }


def test_without_terms_files_the_admin_settings_apply(
    site_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "terms_url", lambda: "https://lab.example.edu/terms")
    monkeypatch.setattr(auth, "text_setting", lambda key: " Admin text. ")

    assert auth._terms() == {
        "terms_url": "https://lab.example.edu/terms",
        "terms_text": "Admin text.",
    }


def _view(revision: int, encoded: dict[str, object]) -> dict[str, object]:
    return {
        "revision": revision,
        "definitions": [
            {"key": "accounts.terms_url", "description": "URL", "read_only": False},
            {"key": "accounts.terms_text", "description": "Text", "read_only": False},
            {"key": "site.app_name", "description": "Name", "read_only": False},
        ],
        "values": dict(encoded),
    }


def test_admin_shows_file_terms_read_only(site_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_settings.settings, "settings_response", _view)
    (site_dir / "terms.en.txt").write_text("File terms.")

    view = admin_settings._settings_view(
        3, {"accounts.terms_url": "https://old.example/", "accounts.terms_text": "Old."}
    )

    read_only = {d["key"]: d["read_only"] for d in view["definitions"]}
    assert read_only == {
        "accounts.terms_url": True,
        "accounts.terms_text": True,
        "site.app_name": False,
    }
    assert view["values"] == {"accounts.terms_url": "", "accounts.terms_text": "File terms."}
    with pytest.raises(HTTPException) as refused:
        admin_settings._refuse_managed({"accounts.terms_text": "Edited."})
    assert refused.value.status_code == 409
    admin_settings._refuse_managed({"site.app_name": "Renamed"})


def test_admin_terms_stay_editable_without_files(
    site_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin_settings.settings, "settings_response", _view)

    view = admin_settings._settings_view(3, {"accounts.terms_text": "Admin."})

    assert not any(d["read_only"] for d in view["definitions"])
    assert view["values"] == {"accounts.terms_text": "Admin."}
    admin_settings._refuse_managed({"accounts.terms_text": "Edited."})
