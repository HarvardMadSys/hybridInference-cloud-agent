# Minimal Agent identity

Cloud Agent stores the Gateway user ID and the issuer that minted it. It does
not copy an account profile or derive permissions from account roles. Current
access comes from the [Agent admission contract](agent-access-policy.md).

## Tokens and sessions

| Credential | Claims retained by Agent | Validation |
| --- | --- | --- |
| Gateway identity token | `iss`, `aud`, `sub`, `iat`, `exp` | RS256 signature through JWKS, exact configured issuer, `cloud-agent` audience and expiry |
| Agent session | `sub`, `gateway_issuer`, `iat`, `exp` | HS256 signature with `AGENT_SESSION_SECRET`, exact issuer, expiry and exact claim set |

Older Gateways may send additional profile claims. Agent discards them at
verification. The session's `gateway_issuer` names the upstream identity
authority; Agent signs the session itself. Sessions last 30 days. Legacy cookies
missing the issuer or containing profile claims, and cookies from another issuer,
are rejected. Users must sign in again after upgrading from those formats.

An active account without Agent permission may establish a minimal session for
the access-required page. A missing/inactive account is refused through the
access contract's typed `subject_unavailable` error. Unreadable responses and
proxy failures cannot establish access.

`GET /v1/session/me` returns `id` and `agent_access`. Settings offer theme and
interface-language preferences; account profiles belong to the Gateway. Tasks,
messages, connection identifiers and encrypted Git/MCP credentials remain
Agent-owned data. User-authored content is unaffected.

There is no individual session-revocation table. A copied cookie remains valid
until expiry; protected requests still require current Gateway access. Rotating
`AGENT_SESSION_SECRET` invalidates every session. Withdrawing Gateway permission
prevents subsequent protected requests.

## Database identity boundary

`agent_identity_namespace` has one record binding the database to a
`GATEWAY_ISSUER`. Both the main API and MCP listener validate the binding before
starting workers. The binding never rewrites owner IDs or infers their issuer
from old rows. Its table is part of `0001_schema_baseline`; the retired migration
chain called this change `0029_identity_namespace`.

| Database state | Startup/check behavior |
| --- | --- |
| Empty, no binding, issuer configured | Bind automatically to that issuer |
| Owner data present, no binding, issuer configured | Refuse until an operator explicitly adopts the original issuer |
| Bound to the configured issuer | Continue |
| Bound to another issuer, or issuer removed after binding | Refuse |
| Neither Gateway configuration nor binding | Allow local infrastructure administration; identity/session authentication stays unavailable |

Concurrent initialization is serialized. Adoption can fill an absent binding;
it cannot replace one. Use the exact `iss` signed by the original Gateway.
`GATEWAY_BASE_URL` and JWKS URLs are transport locations and can change without
changing the identity namespace. Independent Gateways must use distinct issuers.
Replacing the Gateway's account database with unrelated identities under the
same issuer is not a supported migration.

## Upgrade an existing deployment

Prepare each environment before its distribution advances the Agent release.
Merging upstream source does not deploy a named environment, and existing owner
rows do not reveal the original Gateway issuer.

1. Verify the original Gateway's effective issuer from its running environment.
   In the Gateway implementation used by the original rollout,
   `serving.utils.identity_tokens.issuer()` reads `IDENTITY_ISSUER`, falls back
   to `base_url`, and removes trailing slashes. Pin the verified effective value
   as Gateway `IDENTITY_ISSUER` and Agent `GATEWAY_ISSUER`. Verify staging and
   production separately.
2. Back up the Agent database. If it still uses the migration chain from before
   2026-09-10, follow the conversion/rebuild procedure in
   [operations](operations.md) before running migrations. `upgrade head` cannot
   directly convert a retired revision stamp. With the supported schema, migrate
   to head using the prepared release image.
3. Adopt existing owner data into its original issuer, then verify the binding.
   In an environment with the Agent package and database configuration:

   ```bash
   python -m cloud_agent.identity_namespace --adopt-existing --issuer '<original-issuer>'
   python -m cloud_agent.identity_namespace --check
   ```

   The adoption argument must exactly match `GATEWAY_ISSUER`. It changes only an
   absent binding, preserving tasks, connections and owner IDs. There is no
   persistent auto-adoption switch.

   For Compose, use the existing project name, active environment file and
   prepared checkout:

   ```bash
   export AGENT_DEPLOYMENT_SHA="$(git rev-parse HEAD)"
   dc() {
     docker compose -p '<existing-project>' --env-file '<active-env-file>' \
       -f deploy/control-plane/docker-compose.yml "$@"
   }
   dc build control-plane migrate
   dc run --rm --no-deps migrate
   dc run --rm --no-deps control-plane python -m cloud_agent.identity_namespace \
     --adopt-existing --issuer '<original-issuer>'
   dc run --rm --no-deps control-plane python -m cloud_agent.identity_namespace --check
   ```

   These commands require the existing Postgres service to be running. They
   prepare the binding without replacing the API or MCP relay.
4. Release Agent backend and web app together through the deployment workflow.
   Verify fresh login, `/me`, owner/admin permissions and ownership of existing
   tasks. Confirm legacy cookies are rejected.

`deploy/control-plane/deploy.sh` runs the identity check after migration and
before bootstrap, host-readiness waiting or API/MCP replacement. Unadopted owner
data, a missing required issuer or a conflicting binding stops that deployment.
The host rollout can run in parallel, so prepare the identity boundary first.
Both Compose listeners use the same issuer.

A permission-capable Gateway may remove profile claims before or after this
Agent release; Agent treats them as optional. Older Gateways without the access
contract remain incompatible. The local Docker stack uses the fixed issuer
`freeinference-local`; existing unbound demo owner data needs explicit adoption
of that issuer too.

### Rollback and restore constraints

Do not use `alembic downgrade base` to undo identity binding: the baseline drops
owner tables and `agent_identity_namespace` together. No partial downgrade
preserves owner data while removing only this record.

- Re-upgrading an empty database automatically binds the currently configured
  issuer. It cannot protect history already deleted by the downgrade.
- Restoring owner rows without their matching binding causes the identity check
  to fail. Restore the original binding or explicitly adopt the verified
  original issuer using the procedure above.

An older Agent binary may accept profile cookies and ignore the stored issuer.
Prefer a compatible forward fix, or stop Agent while keeping the Gateway
available. Back up and restore the binding with the data whose owners it names.

The implementation is in
[`identity.py`](../apps/backend/cloud_agent/identity.py),
[`identity_namespace.py`](../apps/backend/cloud_agent/identity_namespace.py) and
[`lifespan.py`](../apps/backend/cloud_agent/bootstrap/lifespan.py).

## Local verification (2026-09-10)

Gateway `make all` passed (5,254 tests; 48 skipped). Agent `make check` passed
(2,161 tests), as did its 719 web tests, lint, type checking and production build.
Fifteen additional PostgreSQL integration cases exercised initial binding,
explicit adoption, preservation of existing owners, concurrent startup and
the actual deployment-check/adoption CLI.

Native Gateway and Agent HTTP listeners also completed PKCE code exchange,
JWKS verification, minimal session creation, `/me` and an owner API request
against separate fresh PostgreSQL databases with production schemas. The
Gateway identity token and Agent cookie had exactly the documented fields;
legacy and foreign-issuer cookies were rejected, and no account-status request
occurred. Docker Compose rendered each listener's environment separately;
the control plane and MCP relay both started against the bound database with
those isolated environments. Compose also refused an omitted issuer. Deploy
script tests verified that a failed identity check neither bootstraps nor
replaces listeners. This local check supplied a seeded Gateway login dependency and
access policy; it did not exercise the main site's password login or deploy
either service.
