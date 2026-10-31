# Agent admission policy

Cloud Agent decides who may use it from its own accounts. Each protected
owner or administration request, and `/v1/session/me`, reads the account as
it is now from this database, through the account directory
(`cloud_agent/accounts/directory.py`). Nothing is cached, and no other service
is asked. [Accounts and the waiting list](local-accounts.md) describes how an
account is created, approved and administered.

## Permissions and request checks

| Permission | Granted when | Access |
| --- | --- | --- |
| `agent.use` | The account's waiting-list application is approved | Owner APIs, subject to per-resource ownership checks |
| `agent.admin` | The account is approved and an administrator | Administration, including runner-host APIs; requires `agent.use` |

An account that is unknown, unverified, suspended or deleted is refused with
`subject_unavailable`, which every caller treats as a denial. An account that
can sign in but holds no permission sees the waiting-list page. Permissions
must be unique, use recognized values and include `agent.use` whenever
`agent.admin` is present; a decision for another account, or a malformed or
contradictory one, cannot grant access. Account roles and profile fields are
not authorization inputs. The session response exposes
`agent_access: {allowed, permissions}`, and the web app denies access if that
field is missing.

Checks govern admission of each request. A permission withdrawn by an
administrator also stops the account's work (see
[stopping an account's work](local-accounts.md#stopping-an-accounts-work)). An
owner stream (a job's events, the account's change feed, a terminal's output)
is admitted when it opens and ends after about 15 minutes; the client
reconnects from where it left off and is admitted again, so a withdrawn
permission or an ended session stops a stream within that bound. An MCP OAuth
callback that returns after the account lost `agent.use` is refused without
consuming the authorization or exchanging its code.

## Failure behavior

| Condition | Result |
| --- | --- |
| No session, or a session that was revoked or expired | HTTP 401; the web app returns to sign-in |
| An account without `agent.use` | Protected APIs answer 403; the web app shows the waiting list |
| An unavailable account (`subject_unavailable`) | Sign-in is refused, and existing sessions receive a denied decision |
| The account store cannot be read | HTTP 503; retry once the database recovers |
| Missing account configuration (`AGENT_LOCAL_SESSION_SECRET`, `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`) or a retired Gateway variable | The control plane does not start, and names what to set or remove |

When `/me` cannot confirm current access, the web app hides protected content
and offers a retry without forcing another sign-in.

## Infrastructure credentials

`ADMIN_TOKEN` independently authorizes infrastructure administration and the
recovery settings page, even before any administrator account exists.
Host/worker credentials, attempt lease fences and owner checks are separate
controls.

The implementation is in
[`agent_access.py`](../../apps/backend/cloud_agent/agent_access.py),
[`accounts/directory.py`](../../apps/backend/cloud_agent/accounts/directory.py),
[`deps.py`](../../apps/backend/cloud_agent/api/deps.py) and
[`session.py`](../../apps/backend/cloud_agent/api/session.py).
