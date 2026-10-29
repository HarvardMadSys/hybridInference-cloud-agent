# Local accounts and the waiting list

Cloud Agent can run with its own accounts instead of HybridInference Gateway
sign-in. Set `AGENT_AUTH_MODE=local` and people sign up with an email address
and a password, or with a phone number and a code, verify the address, sign
in, and apply for Agent use through a waiting list that administrators review.
Local accounts have every account feature HybridInference has: login names,
phone codes, several accounts in one browser, changing the password and the
email, deleting the account, a sign-up policy, first-run setup, administrator
mail and the rest (see the
[feature parity table](../agents/specs/2026-10-29-account-feature-parity.zh-CN.md)).
Gateway mode remains the default, and nothing in this page changes it.

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
| Inference | Gateway grants and catalog, or an owner's model connection | The owner's model connection only (see [Models](#models)) |

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
| `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` | Required at local-mode startup. Encrypts personal model keys and other stored credentials in separate domains. |
| `AGENT_MODEL_CONNECTIONS_ENABLED=true` | Enables personal model setup and new turns. Local mode respects this explicit rollout gate; leaving it off does not enable shared inference. |
| `AGENT_WEB_BASE_URL` | The only base used for emailed links. A request's `Host` is never used. |

Everything else is in **Admin → Settings → Accounts**, saved in the database
and imported once from its environment variable (see
[ENVIRONMENT.md](../../ENVIRONMENT.md)): public sign-up, whether a new email
must be verified, the approval policy (`manual`, or `domains` with the signup
domain list), administrator notices and their recipients, administrator email
addresses promoted at sign-in, the Terms of Service link or text, the session
lifetime, the sign-in and sign-up limits (including sign-ups whose client
address cannot be told), the email transport (SMTP, Tencent SES or a
registered one), phone sign-in and its Tencent SMS provider, and Turnstile.
Secrets are encrypted and never shown again. Without mail, email sign-up is
closed while verification is required, and recovery answers 503; with
verification turned off, sign-up works without SMTP.

The web server must reach the same control plane (`AGENT_CONTROL_PLANE_URL`).
The web app reads `GET /v1/auth/config` to choose its sign-in pages and
`GET /v1/auth/options` for what they offer.

### Hosts without a Gateway

Set `AGENT_AUTH_MODE=local` in each host's deployment environment as well as
the control plane. Keep `AGENT_GATEWAY_URL` empty. The supported
`deploy/host/sandbox_host.sh` recipe removes the unused inference tunnel
before Compose resolves required settings; it needs no Gateway SSH address,
key or health check. Source deployments need Python with PyYAML to render
this recipe; artifact deployments use their deployment Python environment.

The host still requires the control-plane and MCP tunnels and their separate
credentials. Personal model calls use the existing public HTTPS egress. No
sandbox gains access to the management API, private networks or another
group. Gateway-mode hosts retain their inference relay and its checks; an
empty URL never turns a Gateway host into a local one.

In **Servers → deployment setup**, local administrators enter only the
control-plane machine. Setup derives the host mode from the control plane,
omits Gateway provisioning, and discards stale Gateway form values. Gateway
deployments still require the Gateway machine. A manually supplied managed
host profile must agree with the control plane's mode.

### The first administrator

The first account to sign up gets nothing. While the deployment has no
administrator, every control-plane process logs a one-time setup code at
startup (`agent_local_setup_pending`, at WARNING). At **/setup/administrator**
the operator exchanges it for an administrator that signs in with a login name
and needs no email or SMTP; wrong codes are limited to 10 per address in 15
minutes, and of two requests with the right code exactly one succeeds. An
administrator made any other way closes setup too.

Someone who holds the deployment's database credential and local secret can
also work from the command line, without mail. The password is read from
standard input or a terminal prompt, never from the command line:

```bash
AGENT_AUTH_MODE=local python -m cloud_agent.accounts.bootstrap_admin --create-login-name ops < password.txt
AGENT_AUTH_MODE=local python -m cloud_agent.accounts.bootstrap_admin --create-email a@lab.edu --member
AGENT_AUTH_MODE=local python -m cloud_agent.accounts.bootstrap_admin --find-email admin@example.org
AGENT_AUTH_MODE=local python -m cloud_agent.accounts.bootstrap_admin --account-id <id> --approve
```

`--create-*` makes a verified, approved account, an administrator unless
`--member`. An existing account must have a verified sign-in identity;
`--approve` also approves its Agent application, because administration is
granted only to an approved account. Each step goes through the same audited,
versioned action the administration API uses, with the actor
`deployment-bootstrap`.

An account whose **proven** email is one of the **administrator email
addresses** setting is approved and made an administrator when it signs in,
as HybridInference's `ADMIN_EMAILS` does. Proven means a link sent to the
address was followed (verification, an address change or a password reset)
or the operator created the account. An address accepted while email
verification is off is not proven, so listing one does not hand the console
to whoever signs up with it first; its holder proves it with a password
reset. The list stays in charge: withdrawing a listed account's
administration or access, or declining it, is refused with 409
`listed_admin_address` until the address is removed from the list (or the
account is suspended), because the next sign-in would grant it back.

## The HTTP surface

Errors use the usual envelope, `{"detail": {"error": {"type", "message"}}}`.
Every response is `no-store`. A refused body is reported by field and rule
only, so a 422 never repeats a password or a token.

| Route | Notes |
|---|---|
| `GET /v1/auth/config` | `{mode, signup_enabled}`. Public, in both modes. |
| `GET /v1/auth/options` | What the account pages offer: email and phone sign-up, mail delivery, phone policy, Turnstile site key, Terms, whether setup is pending, browser limit. Reads only. |
| `POST /v1/auth/signup` | `{email, password, user_name?, use_case?, accepted_tos, turnstile_token?}`; `{ok: true}` for every address. The Terms must be accepted and the captcha (when configured) must verify; disposable and reserved domains are refused. A registered address gets a password-reset link, not a second account, and its password is not changed. A `use_case` submits the Agent application with the sign-up. |
| `POST /v1/auth/login` | `{email, password}`, where `email` may also be a login name. Adds the account to this browser, sets the cookie and returns the session body. `invalid_credentials` for an unknown identifier and for a wrong password alike. `account_deleted`, `account_suspended` (with the administrator's message) and `email_not_verified`, in that order, only after the right password. `browser_full` past five accounts. |
| `POST /v1/auth/phone/code`, `/phone/signup`, `/phone/login` | A six-digit code bound to its purpose (`signup`, `login`, `bind`, `delete_account`, `bind_email`, `set_password`), number and account; the same answer for every number. Every code request carries a Turnstile token when one is configured, and each token is good for one request. |
| `POST /v1/auth/verify-email`, `/resend-verification`, `/forgot-password`, `/reset-password` | `{ok: true}`. Tokens are single-use and expire (24 hours to verify or bind, 1 hour to reset); a newer token replaces older ones, and a used one is refused. A reset verifies the email and ends every session of the account. |
| `GET /v1/auth/accounts`, `POST /v1/auth/switch`, `DELETE /v1/auth/accounts/{id}`, `POST /v1/auth/logout-all` | The accounts signed in on this browser: list, switch, sign one out, sign all out. |
| `GET /v1/auth/account`, `POST /v1/auth/account/password`, `/email`, `/phone`, `/delete`, `PUT /v1/auth/account/locale` | The signed-in account's own changes; see [The owner's own changes](#the-owners-own-changes). |
| `POST /v1/auth/setup/admin` | First-run setup. |
| `GET /v1/session/me` | Adds `auth_mode`, `email`, `waitlist`, `identity` and `is_local_admin` for a local account. The Gateway-mode body is unchanged. |
| `POST /v1/session/logout` | Signs the browser's active account out; the cookie goes with its last account. |
| `GET`/`POST /v1/waitlist` | Needs a verified, signed-in account only. Submitting again while pending changes nothing. A rejected or revoked application is submitted again only when its owner asks. |
| `GET /v1/admin/accounts` | `q` (email, login name, phone or name anywhere, or an id prefix), `status`, `account_status`, `sort` (`created` or `last_login`); the answer has counts per state. |
| `PATCH /v1/admin/accounts/{id}` | `approve`, `reject`, `revoke`, `suspend`, `restore`, `grant_admin`, `revoke_admin`, `delete`, `resume`, at `expected_version`. Approval and rejection are mailed to the account. Withdrawing what the administrator address list grants back is refused (`listed_admin_address`). |
| `PATCH /v1/admin/accounts/{id}/notes`, `GET /{id}/login-events`, `POST /{id}/erase` | The private note and suspension message, sign-in history, permanent erasure. |
| `GET /v1/admin/accounts/{id}/teams`, `POST /{id}/teams/{team}/transfer` | The teams a deleted account owns, and handing one to a member before erasure. |
| `GET`/`POST`/`DELETE /v1/admin/accounts/signup-domains`, `DELETE /v1/admin/accounts/login-events` | The signup domain list; purging sign-in events by age or by account. |
| `/v1/admin/account-mail` | Mail to accounts: `POST /preview` (with `delivery_problem`), `POST /test`, create, list, detail, cancel, each list paged. |

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

An identity is **verified** when the account has no unverified email and at
least one verified sign-in identity: a confirmed email, a login name (made by
the operator, so verified by construction) or a phone number confirmed by a
code. Each identity records what verified it (`verified_by`): `link`, `code`,
`operator`, or `policy` for an email accepted unproven because verification
is off. Only `link` and `operator` prove a mailbox, which the administrator
address list and the signup domain list require. A password reset that
proves a `policy` email is its holder taking the account back: whatever the
first claimant wrote (display name, language, Terms acceptance, application
text) is cleared, a pending application returns to not applied, and the
reclaim is audited; administrators' decisions and the sign-in history stay. One database view, `agent_local_account_access`, answers whether an
account's identity is verified, whether it may sign in and whether it may use
Agent; every SQL gate reads it, and it is built so that an account missing a
row has no access.

| Account and admission | Signs in, reads the application | Owner routes, tasks, terminals, MCP | Administration |
|---|---|---|---|
| Identity unverified | Verification and recovery only | No | No |
| Verified, not applied or pending | Yes | No | No |
| Verified, approved | Yes | Yes, with ownership checks | No |
| Verified, rejected or revoked | Yes, with the reason | No | No |
| Approved and marked administrator | Yes | Yes | Yes |
| Suspended | No; every session is revoked | No | No |
| Deleted | No; off every browser, every session and link ended | No | No |

With the `domains` approval policy an account whose proven email domain is on
the signup domain list (exactly, or as a subdomain of a `*.` entry) is
approved, audited as `signup-policy`, as soon as its identity is verified;
with an empty list every verified account is. A phone account has no domain, so it
is approved automatically only while the list is empty.

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

- The cookie names a **browser**, which holds up to five signed-in accounts,
  each through its own session row, and which one is active. Every sign-in
  rotates the cookie's value. Switching needs no password while the chosen
  account's own session lasts. Every request of the web app names the account
  its tab shows (`X-Agent-Account`; streams use `agent_account`); once the
  browser's active account is another, the request is refused with 409
  `account_switched` and the tab reloads, so a tab left on one account never
  acts as the next one. An open stream re-checks every 30 s and ends once the
  cookie no longer resolves to its account.
- A browser that signed in to an account before is recognized by a signed
  device cookie (`__Host-cloud_agent_device`, keyed from the local secret) and
  counted in a sign-in window of its own, so others guessing the account's
  password from the same address cannot lock it out there.
- Passwords use HybridInference's Argon2id parameters and rules (see
  [Reused code](#reused-code)). Hashing runs on a pool of four threads; at most
  64 hashes may wait, and beyond that a request is refused with 503 instead of
  being queued.
- A session value is `cals1.` followed by 256 random bits. The database keeps
  only an HMAC of it, keyed from `AGENT_LOCAL_SESSION_SECRET`. A Gateway-mode
  cookie or JWT is never read in local mode.
- A session lasts 30 days by default (`accounts.session_ttl_days`) and is
  never extended. Sign-out, a password change or reset, a suspension and a
  deletion revoke it.
- Opening a session takes a lock on the account row before it reads the
  password, so a session cannot be opened with a password that a concurrent
  reset has replaced, or for an account that a concurrent suspension has
  suspended.
- Abuse limits are fixed-window counters in Postgres, so every worker and
  replica shares them. Their keys are keyed hashes, by the normalized
  identifier. Sign-in allows 5 attempts per identifier and address and 50 per
  identifier in 15 minutes, counting only attempts the per-address windows let
  through, and 20 per address in an hour; all three are settings. Sign-up
  allows 5 per address in an hour and 10 in a day, or one shared budget for
  sign-ups whose address cannot be told. Mail allows 3 per email in an hour,
  10 per email in a day and 10 per address in an hour. Token redemption allows
  30 per address in 15 minutes. Phone codes, by default and each a setting:
  one every 60 s per number, 5 an hour and 10 a day per number, 20 an hour and
  100 a day per address, 5 checks per code, and check windows per number and
  per address.
- A hash cancelled while it runs keeps its slot in the pool until it ends.
  Two sign-ins racing to upgrade an outdated hash both succeed: the one whose
  compare-and-swap loses re-reads the winner's hash.
- Neither a recipient address nor a link is written to a log.

## The owner's own changes

Each change is proven again the way the account signs in: its current
password, counted against the sign-in limits (with the device window), or for
an account without one a code sent to its own verified phone.

- **Password**: change it, or give a phone account its first one. Every other
  session ends, open reset links stop working, and a verified email is told.
- **Email**: a link goes to the new address, which becomes the account's only
  when it is followed; the old address is told and its reset links stop
  working. Whether the address is taken is answered only after the proof.
- **Phone**: bind a number proven by a code to an account that has none. As in
  HybridInference, a bound number is not removed or replaced.
- **Delete**: type the account's identifier, give the proof. An administrator
  cannot delete itself. The account is soft-deleted (below).
- **Language**: kept with the account; the most recent choice wins, including
  one made on this browser while signed out, which is carried into the
  account with its time at the next sign-in.

## Deleting and erasing

Deleting an account (by its owner or an administrator) marks it `deleted` in
one transaction with: every session revoked, the account taken off every
browser, open links and phone codes ended, approved access turned into a
revocation, an audit row, and a termination record of action `delete`, which
stops its work as below. Its data stays. An administrator may **resume** it
once that cleanup is acknowledged: it signs in again, without Agent access.

An administrator may then **erase** it, typing its id to confirm. Erasure is
refused (`erase_blocked`) until the account is deleted, its termination
acknowledged, no turn of it queued, running or resident, and every sandbox it
owns deleted through the sandbox deletion path. It is also refused while a
host still has to clean up one of the account's environment runs
(`host_cleanup_pending`, naming the runs and hosts): a host learns what to
clean only from those rows, so they stay until it reports them clean, which
an offline host does when it reconnects.

**Teams** are shared, so erasure settles them first. A team the account owns
alone is deleted, as its owner would delete it. A team other members still
use refuses the erasure (`team_transfer_required`, naming each team); the
account's review panel lists those teams with their members, and an
administrator hands each to one of them, as the owner's own ownership
transfer would (`GET /v1/admin/accounts/{id}/teams`, `POST
.../teams/{team}/transfer`; only for a deleted account). Invitations the
account issued are revoked, and a team environment it made stays with its
team, recorded as the team owner's.

Erasure then removes every row of the account's Agent data, walking the
foreign keys from the catalog (children first), and never touches another
owner's row: a row of another owner that refers to the account's data
blocks the erasure. Its identifiers, password, sessions, tokens, codes,
sign-in events, mail recipients and earlier audit rows go too. The account row
stays as a tombstone, erased, which every gate keeps denying and which can
never be resumed. The erasure's own audit row records the rows removed, the
teams deleted, the team environments handed over and the sandbox deletion
archive objects left in the bucket, which the control plane may not delete
and which follow the bucket's retention policy; the administrator sees the
same, archive keys included.

What stays are ids in columns that name a person without owning a row: a
deleted team's `owner_user_id` and `created_by_user_id`, team audit events'
`actor_user_id` and `target_user_id`, invitations' `created_by_user_id` and
join requests' `decided_by_user_id`. They name the erased account, which
nothing can sign in as or resolve to a person again.

## Mail to accounts

Administrators mail active or suspended accounts chosen by account state and
application state, with a ready-made message (maintenance, announcement) or a
subject and a Markdown body (raw HTML is not rendered). They preview the
recipient count and the message, send it to themselves as a test, then send
it now or at a chosen time, confirming the count. A preview belongs to the
exact message and audience it showed; any change asks for a new one. The
recipients are fixed when it is created, and only verified addresses receive
it. A message the selected transport would refuse is refused at preview,
test and creation with the reason: Tencent SES sends only approved templates,
and a message without its own goes through the notice template, whose
variables are limited to 800 bytes; SMTP takes any message.

One process at a time sends a message: a claim with its own token, held
before every message and taken over once it is 15 minutes old. Each
recipient's outcome is recorded as soon as its message is handed over, so a
sender that stops resends nothing; one that stalled and resumes after a
takeover finds its claim gone and stops. At most the one message in flight
when the claim changed hands can arrive twice. An account deleted since is
skipped.

## Stopping an account's work

Revoking, suspending or deleting an account commits the change and opens a
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
itself, after it has reopened the termination record, so the next pass kills
the terminal if that request cannot.

The record stays open until a pass finds the terminal cleanup acknowledged
and none of the account's turns running. Each reopen increments the record's
revision. A pass reads the revision before it starts and closes the record
only at that revision, so a terminal reopened while a pass is running keeps
the record open for the next pass. The background pass checks open records
every 30 seconds, up to 50 accounts per pass. A per-account advisory lock
serializes passes across processes. Approving the account or granting it
administration needs that same lock and no open record, at any revision
(otherwise 409 `access_cleanup_pending`), so a delayed pass never stops work
that a newer approval allows. Restoring access
replays nothing: cancelled turns stay cancelled, and paused goals and queues
wait for their owner.

No step has a fixed wall-clock deadline. A host that keeps failing, or a
backlog of more than one batch of accounts, delays completion. What is fixed
is when each kind of work is refused or checked, and that the record stays
open, blocking re-approval, until every step is acknowledged.

| Work | When it is refused or checked (default settings) |
|---|---|
| New requests, claims, goal rounds, waiting messages, residency establishment and heartbeats | Refused from the commit |
| Open owner event, change and terminal streams | Re-checked every 30 s (`LOCAL_STREAM_RECHECK_S`), between frames |
| A running turn | Asked to stop by the first pass; it stops at its host's next heartbeat (every 30 s), or when its 120 s attempt lease lapses if the host is silent |
| Resident member processes | Closed by a pass, or at the host's next residency heartbeat, which no longer extends them |
| Group terminals and environment actions | Killed by a pass; retried at each check until the host acknowledges |

## Models

After approval, an owner opens **My settings → Model connections**, enters a
provider's Base URL, API key and model IDs, and chooses **Verify and save**.
FreeInference can be used as that provider with the owner's own key. The
service checks compatibility before saving; a failed check saves no new
connection or replacement key. Only the connection's owner can use it. If the
account loses access while verification is in flight, saving rechecks admission
under the account lock and refuses the late result without storing a key.

Local accounts have no shared model, default provider credential or inference
allowance. Inference costs and limits belong to the supplied provider key.
Separate Cloud Agent accounts do not create separate provider quotas: keys
from the same FreeInference account may still spend that account's shared
upstream allowance. Cloud Agent's sandbox and concurrency limits are separate.

`GET /v1/agent/config` reports `gateway_inference: false`, an empty Gateway
catalog, and the owner's usable connections. Without a usable connection it
sets `model_connection_required: true`; the composer links to setup when the
feature is enabled and explains when the deployment has disabled it. Gateway
model-preference requests answer 409 `model_connection_required`. No local
account ID is sent to the Gateway, and no Gateway grant is minted, including
for publication titles. Prompt rewriting through the Gateway is unavailable.

A task records both the model ID and connection ID. Follow-ups, queued turns,
goals, Fork and residency keep that source; an unavailable connection never
falls back to a same-named Gateway model or another connection. Saving,
disabling or deleting a connection refreshes open composers, which retain an
unavailable selection and block submission until the owner chooses a usable
source. See [owner model connections](operations.md#owner-model-connections)
for verification, key handling and rollout requirements.

The Gateway-only per-request usage views (`/admin/agent/runs/{id}/llm-requests`,
`/admin/agent/steps`, and an owner's per-message request detail) answer 503
`gateway_feature_unavailable` in local mode, without a Gateway call.

## Data model

Migration `0107_local_accounts`, after `0106_user_model_connections`, adds the
tables; `0108_local_account_parity` splits sign-in identities from passwords
(carrying every account over) and adds the rest. Gateway mode never writes them.

| Table | Holds |
|---|---|
| `agent_local_accounts` | ID (ULID), status (`active`, `suspended`, `deleted`), administrator flag, version, name, note, suspension message, Terms acceptance, last sign-in, deletion and erasure times, language |
| `agent_local_login_identities` | One email, login name and phone number per account, each unique, with its verification time |
| `agent_local_password_credentials` | The Argon2id hash, for an account that has a password |
| `agent_local_account_access` (view) | Identity verified, may sign in, has Agent use |
| `agent_local_admissions` | Application status, purpose, public review reason, review time and reviewer |
| `agent_local_sessions`, `agent_local_browser_sessions`, `agent_local_browser_accounts` | Session HMACs, browsers and the accounts signed in on each |
| `agent_local_auth_tokens` | Hashes of emailed tokens, their purpose (and the address a binding proves), expiry and consumption |
| `agent_local_phone_codes` | Keyed hashes of phone codes, their purpose, attempts and state |
| `agent_local_auth_rate_limits` | Abuse counters |
| `agent_local_login_events` | Sign-in attempts and their outcome |
| `agent_local_signup_domains` | The signup domain list |
| `agent_local_setup` | First-run setup |
| `agent_local_broadcasts`, `agent_local_broadcast_recipients` | Mail to accounts and each recipient's outcome |
| `agent_local_access_terminations` | One record per revocation, suspension or deletion, until its cleanup is acknowledged |
| `agent_local_identity_namespace` | The marker that binds a database to local identity |

## Tests

- `tests/unit/test_local_account_rules.py` and
  `tests/unit/test_local_account_credentials.py` cover the rules, the password
  policy, credentials, configuration and mail.
- `tests/servers/test_local_auth_surface.py` checks that Gateway mode is
  unchanged, and covers the cross-site guard, JSON-only bodies, 422 bodies
  without secrets, the refusal of Gateway sessions in local mode, and the
  Gateway-only views.
- The real-Postgres tier (`make test-db-local`) covers local accounts in six
  files: `tests/integration/storage/test_local_account_store.py` (races, replay,
  versions, the reset and suspension barriers, shared counters and the
  namespace), `tests/integration/servers/test_local_access_termination.py`
  (stopping work, the residency fence, cleanup barriers and delayed passes),
  `tests/integration/servers/test_local_accounts_flow.py` (the whole flow over
  HTTP), `tests/integration/storage/test_independent_accounts_acceptance.py`
  (the public account lifecycle through the real application with Gateway HTTP
  forbidden), `tests/integration/storage/test_local_session_reset_race.py`
  (a login waiting on a password reset cannot open a session with the old
  password), and
  `tests/integration/storage/test_local_cleanup_lock_acceptance.py` (cancelling
  a cleanup lock acquisition before its result arrives releases the lock).
- `tests/unit/test_local_account_parity.py`,
  `tests/integration/storage/test_local_account_parity_store.py` and
  `tests/integration/servers/test_local_account_parity_flow.py` cover the
  account features added for HybridInference parity: login names and setup,
  phone codes and providers, browsers and pinned tabs, password and email
  changes, deletion, resumption and erasure, the sign-up policy,
  administration, mail to accounts and the language.
  `tests/integration/storage/test_local_account_review_store.py` pins the
  findings of the independent review: proof of a mailbox before the
  administrator and domain lists trust it, reclaiming an address, host
  cleanup and teams before erasure, one notice per application, broadcast
  claims and crash recovery, lock order when signing out everywhere, and
  audiences. `tests/unit/test_local_account_mail_delivery.py` covers the
  Tencent SES transport and its size bound. In the web app,
  `AccountParity.test.tsx`, `AccountCaptcha.test.tsx` (Turnstile on every
  phone code, one token per request), `AccountAdministration.test.tsx`,
  `AccountsPageErase.test.tsx` and `AccountLocaleSync.test.tsx` cover the
  pages with mocked API answers.
- `tests/integration/servers/test_local_byok_acceptance.py` and
  `test_local_model_onboarding.py` exercise approval, verified connection
  setup, task creation, follow-ups, owner isolation, unavailable sources and
  the explicit feature gate with real accounts and Postgres. They use a fake
  provider transport and forbid Gateway calls; they are not live-provider or
  Kata-host acceptance.

## Reused code

The administration pages for local accounts follow the administrator's
language (English or Simplified Chinese); the rest of the console is English.

The password hashing and policy, the sign-up, sign-in, verification and
recovery behaviour, the mail templates and the abuse limits are adapted from
HybridInference (`HarvardMadSys/hybridInference` at
`321e9a6c457dcc80d35977aaac472d54041204a8`, and the features added since at
`8b49872d1dcbed5c4bc8132b026b6740c3437aa7`, MIT License, Copyright (c) 2026
Harvard SEAS: the same license and holder as this repository). Each adapted
module names its source file. Nothing imports `serving.*`, and nothing shares
HybridInference's database, sessions, secrets or tokens. The deliberate
differences: a pending account can sign in, a sign-up with a registered address
changes nothing, a used link is refused rather than reported on, the abuse
counters are persisted rather than kept per process, tokens travel in URL
fragments, setup waits for an administrator rather than closing sign-up, an
erased account keeps a tombstone, administrator mail goes only to verified
addresses and renders Markdown without raw HTML, an address accepted while
verification is off is not trusted by the administrator or domain lists
until it is proven, the administrator list refuses withdrawals it would undo,
erasure hands shared teams over rather than deleting them, mail Tencent SES
cannot send is refused before it is queued, and `argon2-cffi` is called
directly rather than through `passlib`.
