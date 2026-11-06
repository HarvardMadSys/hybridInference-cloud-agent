"""Copy another service's accounts into this deployment, approved, once.

Used to seed Cloud Agent with FreeInference's existing users: each active
FreeInference account becomes an approved Agent account that signs in with
the same email, login name or phone and the same password. Both services
hash with the same Argon2id parameters, so a copied hash verifies here
unchanged and nobody is asked to reset anything. Nothing links the two
accounts afterwards.

The input is one JSON object per line, as the export query in
``docs/developer/local-accounts.md`` writes it::

    {"email": ..., "email_verified": ..., "login_name": ...,
     "phone": ..., "phone_verified": ..., "user_name": ..., "password_hash": ...}

Run it as the deployment's operator, with ``AGENT_DATABASE_URL`` set::

    python -m cloud_agent.accounts.import_accounts users.jsonl          # dry run
    python -m cloud_agent.accounts.import_accounts users.jsonl --apply

A dry run writes nothing and reports what an apply would do. An account any
of whose identifiers already belongs to someone here is skipped whole and
left untouched, so a second apply imports nothing twice. Each account is
created and approved in one audited transaction (``agent.local_account.import``,
actor ``account-import``); no mail is sent and the Terms are not recorded as
accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent.accounts import tokens
from cloud_agent.application.accounts import (
    InvalidEmail,
    InvalidLoginName,
    normalize_email,
    normalize_login_name,
)
from cloud_agent.ports.accounts import ImportedIdentity

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cloud_agent.ports.accounts import LocalAccountRepository

ACTOR = "account-import"

#: The canonical E.164 form a phone sign-in normalizes to, without this
#: deployment's SMS prefix policy: that limits where codes are sent, not who
#: may hold an account.
_E164 = re.compile(r"\+[1-9][0-9]{7,14}")
_HASH_PREFIX = "$argon2id$"
_MAX_USER_NAME = 50
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ImportRefused(RuntimeError):
    """The import cannot run as asked."""


@dataclass(frozen=True)
class PreparedAccount:
    """One input line, normalized into what an account here is made of."""

    line: int
    identities: list[ImportedIdentity]
    password_hash: str | None = field(repr=False)
    user_name: str | None


@dataclass
class ImportReport:
    """What a run did, or (dry run) would do. Lines are 1-based input line numbers."""

    applied: bool
    imported: int = 0
    skipped: list[tuple[int, str]] = field(default_factory=list)
    #: Accounts imported without part of their input, by what was left out.
    dropped: Counter[str] = field(default_factory=Counter)

    def summary(self) -> str:
        """A few lines for the operator; no identifier appears in it."""
        verb = "imported" if self.applied else "would import"
        lines = [f"{verb}: {self.imported}", f"skipped: {len(self.skipped)}"]
        for reason, count in sorted(Counter(r for _, r in self.skipped).items()):
            lines.append(f"  {reason}: {count}")
        for part, count in sorted(self.dropped.items()):
            lines.append(f"imported without {part}: {count}")
        lines.extend(f"line {line}: {reason}" for line, reason in self.skipped)
        return "\n".join(lines)


def _text(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _email(row: dict[str, Any], dropped: Counter[str]) -> ImportedIdentity | None:
    if not (email := _text(row, "email")):
        return None
    try:
        normalized = normalize_email(email)[1]
    except InvalidEmail:
        dropped["email (invalid)"] += 1
        return None
    return ImportedIdentity("email", normalized, verified=row.get("email_verified") is True)


def _login_name(
    row: dict[str, Any], password_hash: str | None, dropped: Counter[str]
) -> ImportedIdentity | None:
    if not (login_name := _text(row, "login_name")):
        return None
    try:
        name = normalize_login_name(login_name)
    except InvalidLoginName:
        dropped["login name (invalid)"] += 1
        return None
    if password_hash is None:
        dropped["login name (no password)"] += 1
        return None
    return ImportedIdentity("login_name", name, verified=True)


def _phone(row: dict[str, Any], dropped: Counter[str]) -> ImportedIdentity | None:
    if not (phone := _text(row, "phone")):
        return None
    value = re.sub(r"[\s()-]", "", phone)
    if row.get("phone_verified") is not True:
        dropped["phone (unverified)"] += 1
        return None
    if not _E164.fullmatch(value):
        dropped["phone (invalid)"] += 1
        return None
    return ImportedIdentity("phone", value, verified=True)


def prepare(line: int, row: dict[str, Any], dropped: Counter[str]) -> PreparedAccount | str:
    """Normalize one input row, or say why no account can be made of it.

    A part that cannot be used here is left out, and counted in ``dropped``,
    rather than refusing the account: an invalid login name, an unverified
    phone (a phone is only ever a proven sign-in here), a hash in another
    format (its holder sets a new password by email), a display name this
    deployment would not accept. A login name needs the password it signs in
    with, so one without a usable hash is left out too.
    """
    raw_hash = _text(row, "password_hash")
    password_hash = raw_hash if raw_hash and raw_hash.startswith(_HASH_PREFIX) else None
    if raw_hash and password_hash is None:
        dropped["password (not Argon2id)"] += 1
    found = (_email(row, dropped), _login_name(row, password_hash, dropped), _phone(row, dropped))
    identities = [identity for identity in found if identity is not None]
    if not identities:
        return "no usable sign-in identifier"
    user_name = _text(row, "user_name")
    if user_name and (len(user_name) > _MAX_USER_NAME or _CONTROL.search(user_name)):
        dropped["display name"] += 1
        user_name = None
    return PreparedAccount(line, identities, password_hash, user_name)


def read_rows(lines: Iterable[str]) -> Iterable[tuple[int, dict[str, Any] | None]]:
    """Each non-blank input line and its object, or ``None`` if it is not a JSON object."""
    for number, text in enumerate(lines, start=1):
        if not text.strip():
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            row = None
        yield number, row if isinstance(row, dict) else None


async def import_accounts(
    accounts: LocalAccountRepository,
    lines: Iterable[str],
    *,
    apply: bool,
) -> ImportReport:
    """Import every usable line (``apply``), or report what an import would do."""
    report = ImportReport(applied=apply)
    seen: set[tuple[str, str]] = set()
    for number, row in read_rows(lines):
        if row is None:
            report.skipped.append((number, "not a JSON object"))
            continue
        dropped: Counter[str] = Counter()
        prepared = prepare(number, row, dropped)
        if isinstance(prepared, str):
            report.skipped.append((number, prepared))
            continue
        keys = {(i.kind, i.value) for i in prepared.identities}
        if keys & seen:
            report.skipped.append((number, "identifier repeated in the input"))
            continue
        seen |= keys
        if apply:
            created = await accounts.import_account(
                account_id=tokens.new_account_id(),
                identities=prepared.identities,
                password_hash=prepared.password_hash,
                user_name=prepared.user_name,
                actor_id=ACTOR,
            )
        else:
            created = not any(
                [await accounts.identifier_owner(kind, value) for kind, value in sorted(keys)]
            )
        if created:
            report.imported += 1
            report.dropped.update(dropped)
        else:
            report.skipped.append((number, "identifier already has an account"))
    return report


async def _run(args: argparse.Namespace) -> ImportReport:
    import asyncpg

    from cloud_agent.accounts.config import (
        check_local_configuration,
        refuse_retired_configuration,
    )
    from cloud_agent.deployment.identity_namespace import ensure_local_namespace
    from cloud_agent.storage.assembly import Storage

    refuse_retired_configuration()
    check_local_configuration()
    dsn = os.environ.get("AGENT_DATABASE_URL")
    if not dsn:
        raise ImportRefused("AGENT_DATABASE_URL must be set.")
    try:
        handle = open(args.file, encoding="utf-8")  # noqa: SIM115 - closed below
    except OSError as exc:
        raise ImportRefused(f"Cannot read {args.file}: {exc.strerror}") from None
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        await ensure_local_namespace(pool)
        return await import_accounts(Storage(pool).local_accounts, handle, apply=args.apply)
    finally:
        handle.close()
        await pool.close()


def main() -> None:
    """Report, or with ``--apply`` perform, an import."""
    parser = argparse.ArgumentParser(
        description="Copy accounts from another service, approved. A dry run unless --apply."
    )
    parser.add_argument("file", help="One JSON object per line, as the export query writes")
    parser.add_argument("--apply", action="store_true", help="Create the accounts")
    args = parser.parse_args()
    try:
        print(asyncio.run(_run(args)).summary())
    except ImportRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
