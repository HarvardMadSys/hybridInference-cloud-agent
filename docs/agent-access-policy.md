# Agent admission policy

The deployment decides who can use Agent. HybridInference verifies the account
and runs the deployment's registered policy; this service consumes the decision
from `GET /internal/users/{user_id}/agent-access` using its dispatch credential.
FreeInference's policy currently grants `agent.use` and `agent.admin` only to
active admin accounts. A different deployment can grant different accounts
access without changing this service.

`agent.use` permits the owner API, subject to the existing job ownership checks.
`agent.admin` additionally permits the runner-host administration API. The latter
requires `agent.use`. Session `role` remains profile metadata and is never a
substitute for either permission. The Agent database needs no account table or
schema migration for this change.

Login checks the current decision and verifies that the account still exists
and is active before issuing a session. An active account with no Agent permission
can sign in to see the access-required page; its session grants no protected
owner or admin access. Missing and inactive accounts receive no new session.
Existing sessions
are checked again on each protected owner/admin request and on `/v1/session/me`.
The session response includes `agent_access: {allowed, permissions}` so the web
app can render the same decision. Missing, malformed, unavailable or contradictory
decisions never grant access. The frontend must also deny when an older `/me`
response lacks `agent_access`.

When current access cannot be checked, `/me` returns 503 and the web app hides
protected content and offers a retry without sending the user through sign-in.
Callback outages redirect to the public login page with a retry action and the
original destination, clearing the consumed login-flow cookie. An account refusal
uses a separate explanation. Only a 403 explicitly typed `subject_unavailable`
is an account denial; an untyped or proxy-generated 403 is an operational failure.

The local `ADMIN_TOKEN` remains an infrastructure credential independent of
Gateway availability. The dedicated on-call token must additionally pass the
configured service account's `agent.use` check before creating jobs. Its existing
polling/cancellation credentials remain available for cleanup. Worker credentials,
lease fences and per-job ownership checks retain their separate purposes.

Checks apply when a request is admitted. This change does not terminate an
already-open stream or already-running job when the deployment policy changes.
There is no session permission cache; each new protected request asks the Gateway.
The Gateway retains its existing account-row cache. Normal account management
invalidates it, while direct database edits or updates from another process can
remain unseen until that cache expires.

## Rollout

1. Stage the FreeInference policy registration. Its compatibility path permits
   the currently pinned older Gateway, which has no Agent access hook yet.
2. Publish and validate a Gateway build with the policy hook and access endpoint,
   then update FreeInference's upstream lock to the real backend and frontend
   artifacts. The frontend must include the explicit internal access-path rewrite.
   Verify that an active admin receives both permissions, a non-admin receives
   an explicit denial, and inactive accounts are refused.
3. Release this Agent backend and web app together. Verify existing sessions as
   well as a fresh login, owner requests and runner-host administration.

A neutral Gateway with no registered policy returns a denial. An older Gateway
without the endpoint is incompatible with the permission-consuming Agent and
causes protected requests to fail closed. To roll back the Gateway to such a
version, first roll back the Agent backend and web app to a mutually compatible
release. Never compensate by restoring role-based fallback in the new Agent.

The API's wire schema and errors are recorded in
[`internal-lookups.openapi.yaml`](../contracts/internal-lookups.openapi.yaml).
Source changes alone do not update pinned images or deploy any service.

## Local verification (2026-09-10)

The candidate sources were exercised through native HTTP listeners: the Gateway's
production internal router and PostgreSQL operational store, the actual
FreeInference backend extension loader, and the Agent's `create_app` control
plane. Gateway and Agent used separate fresh PostgreSQL databases with their
production schemas. The Agent database contained no `users` table.

Twenty-five assertions passed: current FreeInference admission, inactive/missing
account refusal, owner and host APIs, old-session revocation, `/me` permissions,
a custom policy admitting non-admin accounts, separate use/admin permissions,
neutral-policy denial, policy exception handling, an old Gateway lacking the
endpoint, and local infrastructure-token access in each mode.

This run checks local HTTP authorization and database separation. It does not
claim a staging deployment, a browser login redirect round trip, or execution of
a sandbox job. No deployment lock or published image was changed.

The login follow-up was also checked in an actual browser against the production
Next.js build and Agent application, using a local HTTP Gateway fixture with
PKCE code exchange and signed identity tokens. An active account without Agent
permissions reached the access-required page. An existing session stayed on a
retry page during a Gateway outage and recovered without signing in again.
An outage during the callback returned a retry link that started a fresh login;
an HTML proxy 403 showed the temporary-failure page. This fixture-based browser
run does not claim staging deployment or sandbox execution.
