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

1. Stop the old control-plane and MCP listeners so no old process writes data
   while ownership is being established. Back up the Agent database.
2. Set `GATEWAY_ISSUER` to the original Gateway's exact issuer. Run Alembic
   against the Agent database using its existing deployment environment.
3. In that same environment, explicitly acknowledge the existing ownership:

   ```sh
   python -m cloud_agent.identity_namespace --adopt-existing --issuer '<original-issuer>'
   ```

   The argument must match `GATEWAY_ISSUER`. This command records only an absent
   issuer and preserves every task, connection and owner ID. It refuses to
   change an already-recorded issuer, even with the adoption flag.
4. Start the new Agent backend and web app together. Verify sign-in, `/me`,
   owner/admin permissions and existing task ownership. Old cookies must be
   rejected and replaced through a fresh login.
5. Publish the Gateway change removing profile claims. Deploying Agent first
   minimizes its data immediately and permits this ordered rollout. Do not
   deploy the minimal-token Gateway to an Agent predating access permissions.

For the local Docker demo, `dev.sh` supplies its fixed `freeinference-local`
issuer. An existing demo database still needs explicit adoption with that issuer;
updating the example environment does not silently authorize its history.

The namespace migration's downgrade retains the identity record. Re-upgrading
preserves it. Rolling back to an old Agent binary would restore profile cookies
and would not enforce this record, so it is not a safe identity-boundary rollback;
prefer a compatible fix or stop Agent while leaving the Gateway running.

## Local verification (2026-09-10)

Gateway `make all` passed (5,254 tests; 48 skipped). Agent `make check` passed
(2,144 tests), as did its 710 web tests, lint, type checking and production build.
Eight additional PostgreSQL integration cases exercised initial binding,
explicit adoption, preservation of existing owners and concurrent startup.

Native Gateway and Agent HTTP listeners also completed PKCE code exchange,
JWKS verification, minimal session creation, `/me` and an owner API request
against separate fresh PostgreSQL databases with production schemas. The
Gateway identity token and Agent cookie had exactly the documented fields;
legacy and foreign-issuer cookies were rejected, and no account-status request
occurred. This local check supplied a seeded Gateway login dependency and
access policy; it did not exercise the main site's password login or deploy
either service.
