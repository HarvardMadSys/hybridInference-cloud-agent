# Local accounts with personal model connections: integration acceptance

Date: 2026-10-28. Status: implementation validated; not merged or deployed.

## Scope and revision

PR #1030 integrates the independent-account work with personal-model PR #1029
at `671d01c400b56a056a5f3d9ae44bf0111e3c820a`, and includes `dev` at
`8780c99c4af9ea130f8d4eb01290e8ba1d87b984`. Its target remains `dev`.
The final implementation revision is
`9ff4a6fe56436f1d755610953cd241b7c327e0ae`, tree
`9bb4c76c756a928d7d54adad2cdf0523f7c808fc`. The isolated acceptance checkout's
tree was checked against that exact value. This record and the design's
completion note are documentation-only follow-ups.

An owner registers, verifies their email, signs in, applies for Agent access,
and waits for approval. An approved owner supplies a FreeInference or another
compatible provider's Base URL, API key and models. Verification must succeed
before the connection and encrypted key are saved. Local mode supplies no
shared model, platform inference key or inference allowance. The upstream
provider controls billing and quota; keys belonging to the same FreeInference
account can still share that account's quota.

Gateway mode remains the default. Local mode requires its own database and
secrets, with migration `0107_local_accounts` after
`0106_user_model_connections`. There is no public inference API, existing-owner
migration or production cutover in this work.

Claude Code implemented the backend integration using the verified
`claude-opus-5-5` model with high effort. Codex completed the frontend and
standalone-host integration, reviewed the changes, and ran independent
acceptance.

## Validation environment and gates

All integration acceptance ran on the designated Tencent development machine,
in an isolated Linux source checkout and disposable PostgreSQL test databases.
Backend checks used Python 3.12.11; frontend checks ran on that same machine
inside the existing browser container with Node.js 22.23.3. No application
tests, builds or previews ran on the local Mac for this integration. Browser
screenshots were copied back for visual inspection.

| Check | Result |
|---|---|
| `make check` | Format, lint and mypy passed; 13,211 tests passed, 7 existing root-required tests skipped |
| `make test-db-local` | 2,100 passed, 0 skipped |
| Frontend CI (`bash scripts/ci-local.sh frontend`, the `make ci-frontend` entry point) | All stages passed after the test-hook fix: 4,552 Vitest and 29 Playwright tests; lint, format, types and both build guards passed |
| `make docs-translate` / `make docs-verify` | English/Chinese builds and catalog checks passed |
| Gitleaks 8.30.1 | Source tree and full implementation history (1,118 commits) passed with no leaks |

Database acceptance ran at `41b52469`; frontend acceptance completed at
`ce0dd610`, whose only change from that revision is a test setup callback.
The callback accidentally returned a mock, which Vitest treated as teardown;
fixing it retained every assertion and was followed by a full lint, format,
type and unit run. Build guards and all 29 browser tests passed on the same
production frontend. The subsequent `dev` merge changes host/shared code and
its tests only: a Git comparison confirmed that the control plane, migrations,
integration tests and frontend remain byte-for-byte identical. Final backend
and documentation checks cover the merged revision.

The non-root backend tier has seven existing tests that need root/setpriv to
switch member UIDs. The database tier permits no skips. The task-local tool
environment supplies Docker Compose and a process-local OpenSSL configuration
workaround; certificate and hostname verification remain enabled. No global
host configuration or existing application database was changed.

## Integration evidence

- Real FastAPI routes, account cookies, migrated PostgreSQL and the deployment
  cipher cover signup, verification, login, application, approval, model
  setup, task admission and worker credential resolution. Email delivery is
  recorded. Provider transport/DNS are controlled fixtures, while the actual
  streaming protocol verifier performs its tool-call round trip. Unexpected
  Gateway or network calls fail the independent account/model tests.
- A selected personal connection remains owner-private across tasks,
  follow-ups, queued work and Fork. Deleted, disabled or otherwise unusable
  sources are refused rather than replaced by a same-name model. Another
  approved account cannot read, verify or use the connection. Missing
  connections produce setup guidance, and an explicitly disabled rollout gate
  remains disabled even in local mode.
- A reproduced revocation race previously returned 201 and saved a key after
  access ended while verification was waiting. Four controlled API cases now
  cover create/edit versus revoke/suspend: the late save is refused and
  persisted connection state is unchanged. Two additional storage tests hold
  a real PostgreSQL account lock, observe the blocked saver, commit revocation
  and verify rejection. The save locks the account before a separate fresh
  admission read, holding that lock until its transaction commits.
- Existing termination tests run on an owner's verified personal connection:
  revocation blocks new claims and residency establishment/renewal, closes
  resident access, pauses queued work and keeps reapproval fenced until
  cleanup is acknowledged. All nine cases passed after adapting their fixture
  to the current atomic verified-save contract and explicit feature gate;
  assertions were retained.
- Browser coverage includes local login and waiting-list routing, verification
  token handling, and English desktop/Chinese mobile model onboarding. Saving,
  disabling, enabling and deleting a connection in another tab updates the
  open composer while preserving its draft. Local mode makes no Gateway model
  preference or prompt-rewrite requests. An unavailable source blocks mouse,
  Enter and programmatic submission. Browser HTTP responses are fixtures, not
  a substitute for the separate real API/database tests.
- Controlled promise/timer tests cover stale config responses and slow polling.
  Background polls wait for the current request; owner actions can supersede
  it. A stale completion cannot overwrite current choices or release the
  pending-request guard. The UI retains provider billing attribution before
  usage data arrives.
- Local hosts require no Gateway URL, SSH key, tunnel or Gateway health check.
  The recipe removes the unused inference tunnel before Compose interpolation,
  retaining control-plane/MCP tunnels and the existing network restrictions.
  Actual Docker Compose rendering validates both modes. Host launch, readiness,
  source/artifact packaging and managed setup have targeted regressions.
  Automatic setup derives mode from the control plane, drops stale Gateway
  form values in local mode, and still rejects missing Gateway input in Gateway
  mode. New local session and SMTP secrets are forbidden in host profiles.
- Integrating `dev`'s new spare-process launch path exposed a control-plane URL
  fallback in the spare constructor. Both cold and spare paths now preserve
  the configured empty Gateway URL in local mode, with service lifecycle
  regressions for local and Gateway modes. MCP helper installation retains
  the new container/digest cache and the shared trusted atomic installer.
  Runtime helper preparation was extracted to keep the merged launch function
  within the existing complexity limit; no limit was increased. The focused
  launch/member-preparation run passed 119 tests. A new upstream shell fixture
  compared a redirected `env` with a bare command; on this host bash gives
  those different SHLVL values even without the preparation runner. The
  baseline now uses the identical redirected command and still compares the
  complete environment, including the existing locale assertion.

The full-gate failures found during integration were corrected: a newly split
host-profile writer needed the existing environment inventory classification,
and the termination fixture still used the removed two-step model-save API.
The inventory continues to check real control-plane environment reads and
documentation; no variable exemption or security assertion was removed.

## Release boundaries

No live SMTP delivery, real-provider inference, complete Kata-host execution,
new-machine provisioning, production deployment or existing-account migration
was performed. Compose rendering and mocked manager/SSH calls are not live-host
acceptance. The compatibility CLI evidence recorded by #1029 remains its own
separate acceptance result.

Upgrade hosts before the control plane and enable personal connections only
after the rollout prerequisites are met. Both sides must explicitly use local
mode for a standalone deployment. Revocation denies new work after its
committed decision; stopping existing work still depends on host
acknowledgements, heartbeat/lease expiry and durable retries, without an
unconditional wall-clock guarantee.

The earlier [accounts-only record](2026-10-28-independent-accounts-acceptance.md)
describes the pre-integration revision. See the
[operator guide](../../developer/local-accounts.md) and
[design](../specs/2026-10-27-independent-accounts-and-waitlist-design.zh-CN.md)
for the implemented product and deployment contract.
