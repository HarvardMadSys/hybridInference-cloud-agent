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

`/v1/session/me` returns only `id` and `agent_access`. The web settings contain
theme and interface-language preferences; account profiles are managed on the
Gateway. Tasks, messages, Git/MCP connection identifiers and encrypted business
credentials remain Agent-owned data; this change does not filter user-authored
content.

## Database identity boundary

The identity namespace binding is one singleton record in
`agent_identity_namespace`, created by the `0001_schema_baseline` revision that
replaced the pre-2026-09-10 migration chain (it was `0029_identity_namespace`
in that chain, which no longer exists). It does not
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

An upstream `dev` merge publishes a release but deploys no named environment.
Complete the identity preparation below **before a downstream distribution
advances its lock or deploys**, separately for staging and production; the
database contents do not reveal which issuer originally owned them.

1. Verify the original Gateway's effective issuer in its running deployment
   environment. `serving.utils.identity_tokens.issuer()` reads `IDENTITY_ISSUER`,
   falls back to `base_url`, and removes trailing slashes. Pin
   `IDENTITY_ISSUER` to that existing effective value on the Gateway, and set
   `GATEWAY_ISSUER` to the same value in the Agent deployment's `.env`. Do not
   infer either value from a transport hostname or from another environment.
2. Back up the Agent database. Use a checkout and image containing this change
   to migrate it to head and to run the explicit adoption against the existing
   Agent database. Keep the original Gateway configuration unchanged during
   preparation; the old API and MCP relay may remain running because this
   only records their existing identity namespace, without rewriting owners.
   On a database still stamped with the pre-2026-09-10 chain, `upgrade head`
   cannot run: those databases require either the supported history-preserving
   copy conversion or an intentional selected-state rebuild, as
   `docs/operations.md` describes. The resulting deployment runs
   `identity_namespace --check` before either listener starts, so the issuer
   must be settled before it, not after.
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

**A downgrade is not a way back through this boundary.** The rewritten
`0001_schema_baseline` drops its tables — `agent_identity_namespace` among them
— so a `downgrade` deletes the record that says which Gateway minted the
`user_id`s in the database. **It deletes the owner data with it**: the rewrite is
one baseline revision, so `downgrade base` drops `agent_threads`, the
connections and everything else, not just the binding. There is no partial
downgrade that keeps the rows and drops the record.

What happens on the way back up depends on whether the rows come back with it,
and the two cases are not the same:

- **Re-upgrade with nothing restored** — the database is empty, so it takes the
  empty-database path: `identity_namespace --check` binds whatever
  `GATEWAY_ISSUER` is configured at that moment and exits 0. Verified on a
  throwaway database: issuer A and one thread, `downgrade base`, `upgrade head`,
  then `--check` under issuer B — exit 0, bound to B, zero rows. **A downgrade
  round trip is a total data loss, and the rebind to a different issuer is
  silent**; nothing here refuses it, because there is no history left to
  attribute to anyone.
- **Re-upgrade with owner data restored but not the binding** — this is the case
  the guard exists for. `_has_owner_data()` finds a row keyed by `user_id`,
  `--check` exits 1 with "existing owner data but no identity issuer", and the
  deploy stops before either listener starts. Recovery is the explicit adoption
  below, with the *original* issuer. Verified on the same probe: one thread, no
  binding, issuer B configured — exit 1.

So the guard protects a restore, not the downgrade itself: restoring owner rows
without the matching `agent_identity_namespace` row is what stops the
deployment, and it stops it for the right reason. Rolling back to an old
Agent binary is worse still: it would restore profile cookies and would not
enforce this record. Neither a downgrade nor an old binary is a safe
identity-boundary rollback; prefer a compatible fix, or stop Agent while
leaving the Gateway running.

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
