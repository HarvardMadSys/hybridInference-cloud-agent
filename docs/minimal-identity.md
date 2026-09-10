# Minimal Agent identity

The Gateway's identity token contains only `iss`, `aud`, `sub`, `iat` and `exp`.
Agent validates the signature, exact configured issuer, audience and expiry.
An older Gateway may still include profile claims during the rollout; the
verifier discards them immediately. Agent no longer calls the account-status
endpoint. Admission comes from the existing `agent-access` contract.

A policy denial for an active account still permits a minimal identity session
so the web app can explain that access is required. An unavailable account is
refused at login using that same access contract's `subject_unavailable` error;
proxy failures or unreadable responses remain temporary failures.

The Agent session contains only `sub`, `gateway_issuer`, `iat` and `exp`, signed
with the Agent's own session secret. `gateway_issuer` identifies the upstream
identity authority; it does not claim the Gateway signed this cookie. Sessions
remain valid for eight hours and permissions are checked on each protected
request. Cookies missing the new issuer field, carrying old profile fields, or
belonging to another issuer are rejected. Users sign in again after upgrading.
There is no session version, revocation table or copied account profile.

`/v1/session/me` returns only `id` and `agent_access`. The web account controls
show a generic account icon and link back to the configured Gateway. Tasks,
messages, Git/MCP connection identifiers and encrypted business credentials
remain Agent-owned data; this change does not filter user-authored content.

## Database identity boundary

Migration `0029_identity_namespace` adds one singleton record. It does not
rewrite owners or infer the original issuer from a URL, a user ID, or old data.
At startup both control-plane and MCP listeners compare `GATEWAY_ISSUER` with
that record before starting background workers. A mismatch, or removing the
issuer after binding, aborts startup. Two different issuers cannot concurrently
initialize the same database.

An empty Agent database records its first configured issuer automatically. An
existing database with tasks, connections or other owner data requires explicit
adoption into its **original** identity namespace. Adoption cannot overwrite a
recorded issuer. A deployment with neither a Gateway configuration nor a stored
issuer can still start for local infrastructure administration; identity login
and session authentication remain unavailable until the issuer is configured.

Use the exact `iss` the original Gateway signs. `GATEWAY_BASE_URL` and JWKS URLs
are transport locations and may change without changing the identity domain.
Distinct Gateways must not reuse an issuer. Replacing a Gateway database with
an unrelated identity domain under the same issuer is not a supported migration.

## Upgrade an existing deployment

Merging to `dev` automatically deploys staging. Complete the identity
preparation below **before merging**, separately for staging and production;
the database contents do not reveal which issuer originally owned them.

1. Verify the original Gateway's effective issuer in its running deployment
   environment. `serving.utils.identity_tokens.issuer()` reads `IDENTITY_ISSUER`,
   falls back to `base_url`, and removes trailing slashes. Pin
   `IDENTITY_ISSUER` to that existing effective value on the Gateway, and set
   `GATEWAY_ISSUER` to the same value in the Agent deployment's `.env`. Do not
   infer either value from a transport hostname or from another environment.
2. Back up the Agent database. Use a checkout and image containing this change
   to run the additive migration and explicit adoption against the existing
   Agent database. Keep the original Gateway configuration unchanged during
   preparation; the old API and MCP relay may remain running because this
   only records their existing identity namespace, without rewriting owners.
3. In the same deployment environment, explicitly acknowledge that ownership:

   ```sh
   python -m cloud_agent.identity_namespace --adopt-existing --issuer '<original-issuer>'
   ```

   The argument must match `GATEWAY_ISSUER`. This command records only an absent
   issuer and preserves every task, connection and owner ID. It refuses to
   change an already-recorded issuer, even with the adoption flag.

   For the Compose deployment, run from the prepared checkout using the
   **existing** Compose project name and the active environment file:

   ```sh
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

   These preparation commands reuse the existing Postgres service and do not
   replace the running API or MCP relay. There is no persistent auto-adoption
   switch; a later unbound database requires its own explicit acknowledgement.
4. Merge and let CD release the Agent backend and web app together. Verify
   sign-in, `/me`, owner/admin permissions and existing task ownership. Old
   cookies must be rejected and replaced through a fresh login.
5. The Gateway profile-removal change may be released before or after this
   Agent version: the current permission-based Agent treats token profiles as
   optional. Agent-first is recommended because it removes the extra profile
   lookup sooner. This compatibility does not cover Agents predating the
   Gateway permissions contract.

`deploy.sh` now runs `identity_namespace --check` after migration and before
bootstrapping either listener, waiting on host readiness, or replacing the
running stack. A missing issuer, a conflicting binding, or unadopted historical
data stops the control-plane deployment before its existing API/MCP containers
are replaced. An empty database may initialize its binding during this check.
Both Compose listeners require the same `GATEWAY_ISSUER`. The independent host
rollout still runs in parallel, so this guard does not replace the pre-merge
preparation above.

For the local Docker demo, `dev.sh` supplies its fixed `freeinference-local`
issuer. An existing demo database still needs explicit adoption with that issuer;
updating the example environment does not silently authorize its history.

The namespace migration's downgrade retains the identity record. Re-upgrading
preserves it. Rolling back to an old Agent binary would restore profile cookies
and would not enforce this record, so it is not a safe identity-boundary rollback;
prefer a compatible fix or stop Agent while leaving the Gateway running.

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
