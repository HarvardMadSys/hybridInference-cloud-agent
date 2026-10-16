# Agent admission and minimal identity: local verification (2026-09-10)

Moved on 2026-10-16 from the ends of
[agent admission policy](../../developer/agent-access-policy.md) and
[minimal identity](../../developer/minimal-identity.md). These runs checked the
candidate sources of that date over local HTTP listeners and fresh databases;
they did not verify a staging or production deployment.

## Agent admission

From [agent-access-policy.md](../../developer/agent-access-policy.md).

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

## Minimal identity

From [minimal-identity.md](../../developer/minimal-identity.md).

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
