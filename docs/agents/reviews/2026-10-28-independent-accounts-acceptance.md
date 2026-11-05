# Independent accounts and waiting list acceptance

Date: 2026-10-28. Status: implementation validated; not deployed.

## Scope and revision

The branch implements local registration, email verification, password login
and recovery, revocable sessions, a waiting list, administrator review and
revocation of active access. Password policy, hashing, mail behaviour and the
account forms are adapted from HybridInference; account data, sessions,
credentials and permissions belong to Cloud Agent.

The final implementation revision is
`f7a000272ff9ee0fb245194e632854b73d688833`, based on
`3f60b6afbb86c67422b3c237c3e9ab68ed2effac`. Its Git tree is
`4b18bcb311c9f9d2bf156710f9b505afb43c28a8`. The acceptance checkout was built
from the source snapshot, and its tree was checked against that value.
The additional regression
`tests/integration/storage/test_local_cleanup_lock_acceptance.py` was included
in the final database run. Documentation and its Chinese translation were
finished after the code checks; they do not change the implementation tested.

Claude Code implemented the backend using `claude-opus-5-5`, high effort, in an
isolated Linux checkout. Codex implemented and reviewed the frontend,
integrated the backend, reviewed the account and revocation boundaries, and
ran the independent acceptance below.

## Independent verification

Final acceptance ran on the designated Tencent development machine, using an
isolated source checkout and disposable PostgreSQL test databases. No existing
deployment or application database was used. Earlier exploratory runs on the
collaborator's Linux server and macOS are not the final acceptance evidence.

| Check | Result |
|---|---|
| `make check` | Format, Ruff and mypy passed; 12,478 tests passed, 7 skipped |
| `make test-db-local` | 2,036 tests passed, 0 skipped |
| `bash scripts/ci-local.sh frontend` (`make ci-frontend` entry point) | All eight gates passed; 4,441 unit tests and 15 Playwright tests passed |
| Additional cleanup-lock regression | Included in the database count; Ruff and formatting also passed |
| `make docs-translate` and `make docs-verify` | English and Chinese site builds and catalog checks passed; all 138 local-account entries translated, none fuzzy |

The seven skips require root to switch sandbox/member UIDs. They are existing
tests outside the account flow, and remain unverified by the non-root backend
run. The database tier permitted no skips.

Backend acceptance used Python 3.12.11. Frontend acceptance ran in a container
on the same Tencent host with Node.js 22.23.3. The host needed an isolated
Docker Compose plugin and a process-local `OPENSSL_CONF=/dev/null` because
the standalone Python OpenSSL library could not load the system OpenSSL
configuration. Certificate verification remained enabled (`CERT_REQUIRED` and
hostname verification checked). No global configuration was changed. The
initial environment-related failures were reproduced, corrected and followed
by the successful full runs above.

## What the regressions establish

- The actual FastAPI application and migrated PostgreSQL storage perform
  registration, verification, login, application, approval, revocation,
  suspension, restoration and password reset. Mail delivery is replaced with
  a recorder; Gateway HTTP calls fail the independent lifecycle test.
- Pending and revoked users can read their application but cannot use owner
  routes. Approval grants use, not administration. Restoration does not
  silently restore approval or administrator status.
- Password-reset races are exercised with real PostgreSQL row blocking. A
  session waiting behind a reset cannot authenticate with the replaced
  password; old sessions are revoked. Restoring the old locking query caused
  the independent regression to fail before the fix was accepted.
- Administrator actions recheck the acting administrator and the target
  version inside the transaction. Conflicting or stale actions cannot
  overwrite a newer decision.
- Access termination fences residency establishment and renewal, retains
  durable retry records, and blocks reapproval during cleanup. A late terminal
  reopen increments a revision so an older cleanup pass cannot mark that
  work complete. Cancellation while the result of a PostgreSQL advisory-lock
  acquisition is in flight does not leak the lock, even with the production
  pool's no-op reset.
- Browser tests cover local login to the waiting list, the reused signup
  password rules, and explicit verification with a token kept out of network
  URLs. These browser tests use mocked account HTTP responses; the separate
  API/database tier exercises the real account implementation.

## Remaining release boundaries

Gateway authentication remains the default. Local mode requires
`AGENT_AUTH_MODE=local`, its own secrets and an empty independent database.
Startup refuses a Gateway-bound database or unmapped existing owner data.
No production identity migration, live SMTP delivery or deployment was
performed.

Personal model connections are implemented on a separate branch. This branch
does not run local-account inference: it returns `model_connection_required`
without consulting Gateway models, grants or quota. Combining the two branches
requires migration reconciliation and acceptance of local-account task,
continuation and Fork flows with the model connection. Real provider and
Kata-host execution are not covered by this record.

Revocation rejects new work from the committed decision. Existing work stops
through host acknowledgements, heartbeat/lease expiry and retrying cleanup;
there is no unconditional wall-clock completion guarantee when a host fails.

See the [design](../specs/2026-10-27-independent-accounts-and-waitlist-design.zh-CN.md)
and the [operator guide](../../developer/local-accounts.md).
