# Local accounts and the waiting list

Cloud Agent can run with its own accounts instead of HybridInference Gateway
sign-in. Set `AGENT_AUTH_MODE=local` and people sign up with an email address
and a password, verify the address, sign in, and apply for Agent use through a
waiting list that administrators review. Gateway mode remains the default, and
nothing in this page changes it.

The design is
[independent accounts and waiting list](../agents/specs/2026-10-27-independent-accounts-and-waitlist-design.zh-CN.md).
This page describes what is implemented.

## What local mode changes

| Concern | Gateway mode (default) | Local mode |
|---|---|---|
| Sign-in | PKCE round trip to the Gateway (`/v1/session/login`) | `POST /v1/auth/login`; Gateway sign-in routes answer 404 |
| Session | HS256 cookie `__Host-cloud_agent_session`, not revocable | Server-side row, cookie `__Host-cloud_agent_local_session`, revocable |
| Identity and admission | Gateway `agent-access` lookup | This database, read on every check, with no cache |
| Profile and team emails | Gateway user status | The local account's email |
| Administration | `ADMIN_TOKEN` or Gateway `agent.admin` | `ADMIN_TOKEN` or a local account with `agent.admin` |
| Inference | Gateway grants and catalog | None: no Gateway grant is minted for a local account (see [Models](#models)) |

In local mode `app.state.gateway` is `LocalModeGateway`
(`cloud_agent/accounts/directory.py`). It has the methods the Gateway client
has. Identity and admission are answered from this database, and each
Gateway-only method is refused with a typed error, without a network call.
Every existing owner check therefore reads the local decision: request
admission, claims, goal rounds, waiting messages, team approval and ownership
transfer, the on-call service account, Profile and team rosters. A local
account ID never reaches the Gateway.

## Enabling it

Local mode needs its own database. At startup it binds an empty database to
local identity (`agent_local_identity_namespace`). It refuses a database bound
to a Gateway issuer, or one that holds owner data without a binding. Gateway
mode, in turn, refuses a database bound to local identity. Both checks take the
same table lock, so two listeners that start in different modes cannot each
bind the database. No setting skips these checks. Moving an existing
Gateway-bound database to local accounts is a separate identity migration
(phase A3 in the design), and it is not implemented.

| Variable | Purpose |
|---|---|
| `AGENT_AUTH_MODE=local` | Selects local accounts. Set it on both listeners. |
| `AGENT_LOCAL_SESSION_SECRET` | At least 32 random bytes, different from `AGENT_SESSION_SECRET`. Keys the stored hashes of sessions, emailed tokens and abuse counters. |
| `AGENT_LOCAL_SIGNUP_ENABLED` | On by default. Set `false` to close public sign-up. |
| `AGENT_SMTP_*` | The mail relay. Without a host and a sender, sign-up is closed and recovery answers 503. |
| `AGENT_WEB_BASE_URL` | The only base used for emailed links. A request's `Host` is never used. |

The web server must reach the same control plane (`AGENT_CONTROL_PLANE_URL`).
The web app reads `GET /v1/auth/config` to choose its sign-in pages.

### The first administrator

No endpoint grants the first administrator, and the first account to sign up
gets nothing. Someone who holds the deployment's database credential and
local secret runs:

```bash
AGENT_AUTH_MODE=local python -m cloud_agent.accounts.bootstrap_admin --find-email admin@example.org
AGENT_AUTH_MODE=local python -m cloud_agent.accounts.bootstrap_admin --account-id <id> --approve
```

The account must exist and must have verified its email. `--approve` also
approves its Agent application, because administration is granted only to an
approved account. Each step goes through the same audited, versioned action
the administration API uses, with the actor `deployment-bootstrap`.

## The HTTP surface

Errors use the usual envelope, `{"detail": {"error": {"type", "message"}}}`.
Every response is `no-store`. A refused body is reported by field and rule
only, so a 422 never repeats a password or a token.

| Route | Notes |
|---|---|
| `GET /v1/auth/config` | `{mode, signup_enabled}`. Public, in both modes. |
| `POST /v1/auth/signup` | `{ok: true}` for every address. A registered address gets a password-reset link, not a second account, and its password is not changed. |
| `POST /v1/auth/login` | Sets the cookie and returns the session body. `invalid_credentials` for an unknown email and for a wrong password alike. `email_not_verified` and `account_suspended` only after the right password. |
| `POST /v1/auth/verify-email`, `/resend-verification`, `/forgot-password`, `/reset-password` | `{ok: true}`. Tokens are single-use and expire (24 hours to verify, 1 hour to reset); a newer token replaces older ones. A reset verifies the email and ends every session of the account. |
| `GET /v1/session/me` | Adds `auth_mode`, `email` and `waitlist` for a local account. The Gateway-mode body is unchanged. |
| `POST /v1/session/logout` | Revokes the local session, then clears the cookie. |
| `GET`/`POST /v1/waitlist` | Needs a verified, signed-in account only. Submitting again while pending changes nothing. A rejected or revoked application is submitted again only when its owner asks. |
| `GET /v1/admin/accounts`, `PATCH /v1/admin/accounts/{id}` | `approve`, `reject`, `revoke`, `suspend`, `restore`, `grant_admin`, `revoke_admin`, at `expected_version`. |

Mail links point at the web app's `/verify-email#token=…` and
`/reset-password#token=…`. A browser never sends the fragment to a server, so
the token stays out of access logs. The page sends the token in an explicit
POST, and following a link changes nothing by itself.

Each unsafe `/v1/auth/*` request is checked by the cross-site write guard,
whether or not it carries a cookie, and the body must be JSON. A cross-site
sign-in would otherwise put an attacker's account in the victim's browser.

## Identity and admission

An account is who signs in. Its admission is whether that person may use
Agent. These are separate records:

| Account and admission | Signs in, reads the application | Owner routes, tasks, terminals, MCP | Administration |
|---|---|---|---|
| Email unverified | Verification and recovery only | No | No |
| Verified, not applied or pending | Yes | No | No |
| Verified, approved | Yes | Yes, with ownership checks | No |
| Verified, rejected or revoked | Yes, with the reason | No | No |
| Approved and marked administrator | Yes | Yes | Yes |
| Suspended | No; every session is revoked | No | No |

`agent.use` needs a verified, active, approved account. `agent.admin` also
needs the explicit administrator flag; the flag alone grants nothing. Granting
administration never approves an account. Suspension revokes the account's
sessions, clears the administrator flag and turns an approval into a
revocation. Restoring an account lets it sign in and apply again, and grants
nothing back. An administrator cannot suspend themself or remove their own
access; the `ADMIN_TOKEN` can.

Every account or admission change increments the account's `version`, which is
`WaitlistEntry.version`. An administrator action names the version it read and
is refused with 409 `version_conflict` when the account has changed since. The
change and its `agent_admin_audit_log` row commit in one transaction. The
audit row records the states, the version and whether a reason was given; it
never records an email address, password, hash or token. The acting
administrator's own permission is checked again inside that transaction, so
an administrator who is suspended while a request is in flight changes
nothing (`actor_not_admin`).

## Sessions and credentials

- Passwords use HybridInference's Argon2id parameters and rules (see
  [Reused code](#reused-code)). Hashing runs on a pool of four threads; at most
  64 hashes may wait, and beyond that a request is refused with 503 instead of
  being queued.
- A session value is `cals1.` followed by 256 random bits. The database keeps
  only an HMAC of it, keyed from `AGENT_LOCAL_SESSION_SECRET`. A Gateway-mode
  cookie or JWT is never read in local mode.
- A session lasts 30 days and is never extended. Sign-out, a password reset
  and a suspension revoke it.
- Opening a session takes a lock on the account row before it reads the
  password, so a session cannot be opened with a password that a concurrent
  reset has replaced, or for an account that a concurrent suspension has
  suspended.
- Abuse limits are fixed-window counters in Postgres, so every worker and
  replica shares them. Their keys are keyed hashes. Sign-in allows 5 attempts
  per email and address and 50 per email in 15 minutes, and 20 per address in
  an hour. Sign-up allows 5 per address in an hour and 10 in a day. Mail allows
  3 per email in an hour, 10 per email in a day and 10 per address in an hour.
  Token redemption allows 30 per address in 15 minutes.
- Neither a recipient address nor a link is written to a log.

## Stopping an account's work

Revoking or suspending an account commits the change and opens a
termination record (`agent_local_access_terminations`) for the new version in
the same transaction. From that commit on, every new owner request, claim,
goal round and waiting message reads the denial. Residency establishment and
residency heartbeats carry the same condition in their own SQL, so an attempt
admitted before the revocation cannot establish or extend a residency
afterwards.

`terminate_owner_access` (`application/owner_termination.py`) then stops the
work already running, using the paths that already end work safely. It
cancels queued turns, asks running turns to stop, pauses waiting messages and
active goals, cancels environment tests and prebuilds, stops environment
actions, and releases every residency. It closes the released member
processes. In each sandbox group whose members all belong to the account, it
freezes the group's terminals, kills them and lifts the freeze. Groups shared
with another owner's chat are never touched. A terminal that a request
admitted before the revocation opens afterwards is killed by that request
itself.

The record stays open until a pass finds the terminal cleanup acknowledged
and none of the account's turns running. The background pass retries open
records every 30 seconds. A per-account advisory lock serializes passes
across processes. Approving the account or granting it administration needs
that same lock and no open record (otherwise 409 `access_cleanup_pending`), so
a delayed pass never stops work that a newer approval allows. Restoring access
replays nothing: cancelled turns stay cancelled, and paused goals and queues
wait for their owner.

| Work | Stops within (default settings) |
|---|---|
| New requests, claims, goal rounds, waiting messages, residency establishment and heartbeats | At commit |
| Open owner event, change and terminal streams | 30 s (`LOCAL_STREAM_RECHECK_S`) plus one keepalive |
| A running turn | At its next host heartbeat, about 30 s, or when its 120 s attempt lease lapses if the host is silent |
| Resident member processes | When the pass closes them, or at the host's next residency heartbeat |
| Group terminals and environment actions | When the pass reaches the group; retried every 30 s until acknowledged |

## Models

Local accounts have no Gateway inference. A model lookup raises
`ModelConnectionRequired`. Job, turn and model-preference requests answer 409
`model_connection_required`, a waiting message is held with that reason, and
`GET /v1/agent/config` reports `model_connection_required: true` with no
models. A Gateway grant is never minted for a local account, and its ID is
never sent to the Gateway's catalog or quota.

Personal model connections are a separate feature on another branch. They
plug into `LocalModeGateway(catalog=...)`, the model catalog seam. Running a
task end to end in local mode needs that branch; this one does not claim it.
Repository, team, profile and other account workflows that need no model are
unaffected.

The Gateway-only per-request usage views (`/admin/agent/runs/{id}/llm-requests`,
`/admin/agent/steps`, and an owner's per-message request detail) answer 503
`gateway_feature_unavailable` in local mode, without a Gateway call.

## Data model

Migration `0106_local_accounts` adds empty tables. Gateway mode never writes
them.

| Table | Holds |
|---|---|
| `agent_local_accounts` | ID (ULID), status, administrator flag and version |
| `agent_local_password_logins` | Email, normalized email (unique), Argon2id hash, verification time |
| `agent_local_admissions` | Application status, purpose, public review reason, review time and reviewer |
| `agent_local_sessions` | Session HMAC, expiry and revocation |
| `agent_local_auth_tokens` | Hashes of emailed tokens, their purpose, expiry and consumption |
| `agent_local_auth_rate_limits` | Abuse counters |
| `agent_local_access_terminations` | One record per revocation or suspension, until its cleanup is acknowledged |
| `agent_local_identity_namespace` | The marker that binds a database to local identity |

## Tests

- `tests/unit/test_local_account_rules.py` and
  `tests/unit/test_local_account_credentials.py` cover the rules, the password
  policy, credentials, configuration and mail.
- `tests/servers/test_local_auth_surface.py` checks that Gateway mode is
  unchanged, and covers the cross-site guard, JSON-only bodies, 422 bodies
  without secrets, the refusal of Gateway sessions in local mode, and the
  Gateway-only views.
- The real-Postgres tier (`make test-db-local`) has three files:
  `tests/integration/storage/test_local_account_store.py` (races, replay,
  versions, the reset and suspension barriers, shared counters and the
  namespace), `tests/integration/servers/test_local_access_termination.py`
  (stopping work, the residency fence, cleanup barriers and delayed passes) and
  `tests/integration/servers/test_local_accounts_flow.py` (the whole flow over
  HTTP).

## Reused code

The password hashing and policy, the sign-up, sign-in, verification and
recovery behaviour, the mail templates and the abuse limits are adapted from
HybridInference (`HarvardMadSys/hybridInference` at
`321e9a6c457dcc80d35977aaac472d54041204a8`, MIT License, Copyright (c) 2026
Harvard SEAS: the same license and holder as this repository). Each adapted
module names its source file. Nothing imports `serving.*`, and nothing shares
HybridInference's database, sessions, secrets or tokens. The deliberate
differences: a pending account can sign in, a sign-up with a registered address
changes nothing, the abuse counters are persisted rather than kept per
process, tokens travel in URL fragments, and `argon2-cffi` is called directly
rather than through `passlib`.
