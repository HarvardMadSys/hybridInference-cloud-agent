"""Reading an account export: what each row becomes, and what a dry run reports."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from cloud_agent.accounts.import_accounts import (
    ImportReport,
    PreparedAccount,
    import_accounts,
    prepare,
)
from cloud_agent.ports.accounts import ImportedIdentity

HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaA"


def _prepared(row: dict) -> tuple[PreparedAccount | str, Counter[str]]:
    dropped: Counter[str] = Counter()
    return prepare(1, row, dropped), dropped


def test_every_usable_identifier_is_kept_normalized():
    prepared, dropped = _prepared(
        {
            "email": " Ada@Example.EDU ",
            "email_verified": True,
            "login_name": "Ada.L",
            "phone": "+86 138-0013-8000",
            "phone_verified": True,
            "user_name": "Ada",
            "password_hash": HASH,
        }
    )
    assert isinstance(prepared, PreparedAccount)
    assert prepared.identities == [
        ImportedIdentity("email", "ada@example.edu", verified=True),
        ImportedIdentity("login_name", "ada.l", verified=True),
        ImportedIdentity("phone", "+8613800138000", verified=True),
    ]
    assert prepared.password_hash == HASH and prepared.user_name == "Ada"
    assert not dropped


def test_an_unverified_email_stays_unverified():
    prepared, _ = _prepared({"email": "a@example.edu", "email_verified": False})
    assert isinstance(prepared, PreparedAccount)
    assert prepared.identities == [ImportedIdentity("email", "a@example.edu", verified=False)]
    assert prepared.password_hash is None


@pytest.mark.parametrize(
    ("row", "left_out"),
    [
        ({"phone": "+8613800138000", "phone_verified": False}, ["phone (unverified)"]),
        ({"phone": "13800138000", "phone_verified": True}, ["phone (invalid)"]),
        ({"login_name": "ab", "password_hash": HASH}, ["login name (invalid)"]),
        ({"login_name": "ops"}, ["login name (no password)"]),
        (
            {"login_name": "ops", "password_hash": "$2b$12$bcrypt"},
            ["password (not Argon2id)", "login name (no password)"],
        ),
        ({"user_name": "x" * 51}, ["display name"]),
        ({"user_name": "a\nb"}, ["display name"]),
    ],
)
def test_an_unusable_part_is_left_out_and_counted(row, left_out):
    prepared, dropped = _prepared({"email": "a@example.edu", "email_verified": True, **row})
    assert isinstance(prepared, PreparedAccount)
    assert dropped == Counter(left_out)
    assert [i.kind for i in prepared.identities] == ["email"]


def test_a_row_without_any_usable_identifier_is_refused():
    prepared, _ = _prepared({"email": "not-an-address", "login_name": "ops", "phone": "+1"})
    assert prepared == "no usable sign-in identifier"


class _Taken:
    """A repository where some identifiers already have accounts, and nothing may be written."""

    def __init__(self, taken: set[tuple[str, str]]) -> None:
        self.taken = taken

    async def identifier_owner(self, kind: str, normalized: str) -> str | None:
        return "01ARZ3NDEKTSV4RRFFQ69G5FA1" if (kind, normalized) in self.taken else None

    async def import_account(self, **_kwargs):  # pragma: no cover - a dry run never writes
        raise AssertionError("a dry run wrote")


async def test_a_dry_run_reports_without_writing():
    lines = [
        json.dumps({"email": "new@example.edu", "email_verified": True, "password_hash": HASH}),
        "",
        "{not json",
        json.dumps({"email": "old@example.edu", "phone": "+12025550123", "phone_verified": True}),
        json.dumps({"email": "NEW@example.edu"}),
        json.dumps({"login_name": "ops"}),
        json.dumps({"email": "bad", "phone": "+12025550199", "phone_verified": True}),
    ]
    report = await import_accounts(
        _Taken({("phone", "+12025550123")}),  # type: ignore[arg-type]
        lines,
        apply=False,
    )
    assert report.imported == 2
    assert report.skipped == [
        (3, "not a JSON object"),
        (4, "identifier already has an account"),
        (5, "identifier repeated in the input"),
        (6, "no usable sign-in identifier"),
    ]
    # What imported accounts were left without; line 6's login name is not counted.
    assert report.dropped == Counter({"email (invalid)": 1})


def test_the_summary_names_no_identifier():
    report = ImportReport(applied=True, imported=2, skipped=[(7, "not a JSON object")])
    report.dropped["phone (unverified)"] += 1
    assert report.summary().splitlines() == [
        "imported: 2",
        "skipped: 1",
        "  not a JSON object: 1",
        "imported without phone (unverified): 1",
        "line 7: not a JSON object",
    ]
