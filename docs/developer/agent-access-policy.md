# Agent admission policy

The Gateway decides who may use Cloud Agent. Cloud Agent represents the account
with its user ID and issuer, then checks the Gateway again on each protected
owner/admin request and on `/v1/session/me`.

To get the current decision, Agent reads
`GET /internal/users/{user_id}/agent-access` over HTTP with its dispatch
credential. The response contains `user_id`, `allowed`, and `permissions`.
Account roles and profile fields are not authorization inputs.

## Permissions and request checks

| Permission | Access |
| --- | --- |
| `agent.use` | Owner APIs, subject to per-resource ownership checks |
| `agent.admin` | Gateway-authorized administration, including runner-host APIs; requires `agent.use` |

`allowed` must agree with the presence of `agent.use`. Permissions must be
unique, use recognized values and include `agent.use` whenever `agent.admin`
is present. A decision for another user, or any malformed or contradictory
response, cannot grant access.

The FreeInference policy documented by the original rollout grants both
permissions to active admin accounts. Another deployment can register a policy
that admits other accounts without changing Agent. A neutral Gateway with no
registered policy denies access. Verify the policy in the deployment being
operated; this repository consumes its decision.

An active account denied Agent permission can still sign in to see the
access-required page. Missing or inactive accounts cannot create a session.
Existing sessions recheck permissions on each protected owner/admin request and
on `/v1/session/me`. Owner requests may be admitted on a grant the Gateway gave
up to 10 seconds earlier (`AGENT_ACCESS_ALLOW_TTL_S`): a page load sends a
burst of owner requests, and concurrent lookups for one account share a single
Gateway call. Only a grant is kept. A denial, an unknown account and an
unavailable Gateway are asked again on every request. Sign-in, `/v1/session/me`,
the admin surface and the checks made when a turn starts never use a kept
grant, and a denial `/v1/session/me` receives ends the kept grant at once. The session response exposes
`agent_access: {allowed, permissions}`. The web app denies access if that field
is missing, including responses from an older backend.

Checks govern admission of each request. A policy change does not itself close
an open stream or cancel a running job. An owner stream (a job's events, the
account's change feed, a terminal's output) is admitted when it opens and ends
after about 15 minutes. The client reconnects from where it left off, and that
request is admitted again, so a withdrawn permission or an expired session
stops a stream within that bound. Agent keeps no permissions in a session,
and an owner request's kept grant lasts at most 10 seconds;
the Gateway's account cache can still delay direct database edits until its
expiry. Ordinary Gateway account-management operations invalidate that cache.

## Failure behavior

| Condition | Result |
| --- | --- |
| Explicit policy denial | Protected APIs refuse access; an active account can see the access-required page |
| Gateway 403 typed `subject_unavailable` | Refuse a new login; existing sessions receive a denied access decision |
| Missing identity configuration, missing access endpoint or rejected dispatch credential | HTTP 503, typed `agent_configuration_error`; correct the deployment |
| Transport/server failure, unreadable decision or untyped/proxy 403 | HTTP 503; retry after the Gateway recovers |
| Expired or invalid login-flow state | Return to login and start a fresh flow |

When `/me` cannot confirm current access, the web app hides protected content
and offers a retry without forcing another sign-in. A callback outage returns
to the public login page with the original destination and a retry action; the
consumed login-flow cookie is cleared. Configuration failures ask the operator
to correct the deployment. An invalid `/me` payload has its own explanation.
Without a configured web destination, the API retains its HTTP error response.
Unverified state and consumed authorization codes are never reused.

## Infrastructure credentials

`ADMIN_TOKEN` independently authorizes infrastructure administration even when
the Gateway is unavailable. The on-call dispatch credential must also pass the
configured service account's `agent.use` check before creating a job; its
polling and cancellation access remains available for cleanup. Host/worker
credentials, attempt lease fences and owner checks are separate controls.

Sessions carry minimal identity only. See
[minimal identity](minimal-identity.md) for cookie contents, issuer binding and
upgrade constraints.

## Verify the Gateway contract

The schema is in
[`internal-lookups.openapi.yaml`](../../gateway_contracts/internal-lookups.openapi.yaml).
Local tests validate its error types against the Agent client and permission
resolver. To validate an isolated or otherwise authorized Gateway over HTTP:

```bash
# Set both values securely in the environment before running; never commit them.
export AGENT_CONTRACT_GATEWAY_URL
export AGENT_CONTRACT_DISPATCH_TOKEN
uv run pytest -q -m external tests/external/test_gateway_access_contract.py
```

The probe queries a randomly named missing account and checks the real 403
against the schema and consumer. It creates no accounts and opens neither
database. It requires explicit Gateway credentials and is excluded from default
CI. Historical verification exercised the production Gateway router/error
handler from `c7987680` (hybridInference#1408) through an isolated empty-store
fixture over loopback HTTP; it did not verify a live deployment.

## Rollout

For a deployment first adopting permission-based access:

1. Configure its Gateway policy registration.
2. Release the Gateway policy hook and access endpoint together with compatible
   distribution backend/frontend artifacts. Verify an admitted account, an
   active account without permission, and a missing/inactive account. Ensure
   the deployment's proxy routes the internal access path correctly.
3. Release Agent backend and web app together. Check fresh login, existing
   sessions, owner APIs and administration.

A Gateway without the access endpoint is incompatible and causes protected
requests to fail closed. Do not add role-based fallback to compensate. After
adopting minimal identity, follow its
[upgrade and rollback constraints](minimal-identity.md#upgrade-an-existing-deployment):
older Agent binaries do not enforce the database issuer, and profile-bearing
cookies require a fresh login. Source changes alone do not update pinned images
or deploy either service.

The implementation is in
[`agent_access.py`](../../apps/backend/cloud_agent/agent_access.py),
[`deps.py`](../../apps/backend/cloud_agent/api/deps.py) and
[`session.py`](../../apps/backend/cloud_agent/api/session.py).
