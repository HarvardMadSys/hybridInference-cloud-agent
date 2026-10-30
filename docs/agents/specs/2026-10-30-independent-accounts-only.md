# Independent accounts only: removing Gateway mode

- **Date**: 2026-10-30.
- **Status**: implementation scope and acceptance criteria. This document supersedes the requirement, in the [account parity table](2026-10-29-account-feature-parity.zh-CN.md) and the [local accounts design](2026-10-27-independent-accounts-and-waitlist-design.zh-CN.md), to keep Gateway mode selectable.

## Decision

Cloud Agent has **one** account system: the one built as "local accounts". It covers registration, sign-in, sessions, the waiting list and administration in Cloud Agent's own database. It is the only product path, not a mode, and nothing selects it.

Gateway sign-in, Gateway admission and Gateway inference are removed. Inference runs only on each owner's own model connections, so keys, quotas and billing stay with the provider. FreeInference is an ordinary provider an owner adds with their own key.

**Governing rule**: what local mode does today becomes the only behaviour. Gateway branches, Gateway clients and the objects that stood in for the Gateway in local mode (`LocalModeGateway`, `LocalModeGrantClient`) are deleted, not renamed into a compatibility mode.

## Deletion and change inventory

### Configuration and startup

- `AGENT_AUTH_MODE` is gone, and so are `local_mode()`, `auth_mode()` and every branch on them, in both the control plane and the host.
- **Stale configuration is refused at startup, not reinterpreted.** If any of the following variables is set, the process exits with an error that names the variable and points to the cutover runbook:
  - `AGENT_AUTH_MODE`;
  - the Gateway-only variables: `GATEWAY_BASE_URL`, `GATEWAY_JWKS_URL`, `GATEWAY_ISSUER`, `GATEWAY_AUTHORIZE_URL`, `GATEWAY_GRANT_*`, `AGENT_GATEWAY_URL`;
  - the Gateway identity variables: `IDENTITY_JWT_PRIVATE_KEY`, `IDENTITY_JWT_RETIRING_PUBLIC_KEYS`, `IDENTITY_ISSUER`, `IDENTITY_ALLOWED_REDIRECTS`.
- `AGENT_LOCAL_SESSION_SECRET` keeps its name, since it is the account system's key. `AGENT_SESSION_SECRET` stays only as the existing fallback root for encrypted settings, and it must still differ from the account key.
- A new deployment boots with no Gateway variable, secret, URL or service, and goes straight to first-administrator setup.

### Database identity

- `identity_namespace` keeps one source: Cloud Agent accounts.
- An empty database is bound to it at startup. A database bound to it starts normally.
- These are refused with actionable cutover instructions:
  - a database bound to a Gateway issuer;
  - a database holding owner data with no binding.
- Nothing adopts existing owner rows, maps Gateway users to accounts, or reuses Gateway signing secrets.

### Control-plane identity

- **Deleted**:
  - `identity.py`: Gateway token verification, the Gateway-mode session cookie, PKCE and login-flow keys;
  - `gateway.py`: the Gateway HTTP client;
  - the `/v1/session/login` and `/v1/session/callback` routes;
  - `accounts/directory.py`'s `LocalModeGateway`;
  - the Gateway branches in `api/deps.py`, `api/_owner_auth.py` and `api/session.py`.
- **Kept and made direct**:
  - every owner and administrator check reads the account directory (`app.state.owners`), with no Gateway-shaped wrapper;
  - `/v1/session/me` and `/v1/session/logout` stay as the account system's own;
  - access revocation, the termination fence and the `X-Agent-Account` pin are unchanged.

### Inference

- **Deleted**:
  - the Gateway grant client and grant minting on claims (`grants.py`);
  - grant renewal and revocation (`workers/attempts.py` grant paths);
  - usage readback (`grant_usage.py`, `ledger.py`, `api/usage.py`);
  - Gateway TTFT (`model_ttft.py`) and admin request detail (`api/admin_llm_requests.py`);
  - the Gateway catalog (`visible_models.py`) and Gateway model preferences (`model_preference.py`, `api/model_preferences.py`);
  - Gateway-grant prompt rewrite (`prompt_rewrite.py`, `api/prompt_rewrite.py`);
  - `LocalModeGrantClient`.
- **PR titles** keep local mode's behaviour: no model call, the deterministic title.
- **Job admission** refuses a turn without a model connection, as local mode already does.

### Retired Gateway-coupled feature

The on-call service door (`api/service_jobs.py`, `AGENT_ONCALL_*`) is retired. It ran as a Gateway service account on Gateway grants, dispatched by the Gateway's alert relay. Reintroducing it would need a Cloud Agent service account with its own model connection. That is follow-up work, not a compatibility path.

### Host and sandbox

- **Deleted**:
  - the Gateway URL (`AGENT_GATEWAY_URL`, `inference_gateway_url`);
  - the `gateway-tunnel` service, its Dockerfile and script, and `inference_compose.py`;
  - the inference relay on each group bridge, and its port in the host table;
  - grant-based runtime environments.
- **Required**: every resident runs on its owner's model connection, reached through the existing public egress.
- **Unchanged**: the MCP-only relay, the control-plane tunnel, the network guard's remaining rules, Kata and the member boundaries.

### Web

- **Deleted**:
  - the Gateway sign-in page and banner;
  - the auth-mode switch in the providers and layout;
  - Gateway links (dashboard and usage);
  - Gateway usage, TTFT and LLM-request admin views;
  - the Gateway model catalog and preference;
  - prompt rewrite.
- **Kept**: every account page from the parity work, plus model connections as the model source in the composer.

### Deploy and documentation

- Compose files, environment examples, deploy scripts and compute setup drop Gateway services and variables.
- `ENVIRONMENT.md` and the maintained guides under `docs/developer/` describe one system, in English and Chinese.
- Gateway contract mirrors that are no longer consumed are removed from `gateway_contracts/`. This service's own API contract stays.
- Historical records under `docs/agents/` are not rewritten.

### Schema

No destructive migration. Tables that only Gateway paths wrote, such as attempt grants and grant usage snapshots, stay in the schema unused, and are listed as a follow-up for a later, separately reviewed migration.

## Cutover (operator-run; not authorized here)

- **Production** is at schema `0025_retained_followup_gate` and holds Gateway-keyed owners. A database bound to a Gateway issuer is refused by design.
- **The non-destructive path**:
  1. Leave the existing database untouched as a retained archive.
  2. Point the new release at a **fresh** database and run `alembic upgrade head`.
  3. Start the release. The setup code appears in its log.
  4. Create the first administrator.
  5. Have the second administrator register, and an administrator grant them administration.
  6. Users re-register.
- **Keeping old data active** would need a separate identity migration that proves each mapping. It is not implemented, and it is not chosen implicitly.
- **Mail**: the existing SMTP service, through `AGENT_SMTP_*` with STARTTLS on 587, reusing the deployment's secret mechanism. `AGENT_WEB_BASE_URL` points at Agent, so reset and verification links open Agent.

## Acceptance criteria

1. No production code reads `AGENT_AUTH_MODE` or has a Gateway branch. A grep finds no `local_mode()`, `auth_mode()`, `LocalModeGateway`, `LocalModeGrantClient`, `GatewayClient` or `GrantClient`.
2. The control plane starts on an empty database with no Gateway variable and offers first-administrator setup.
3. It refuses to start with `AGENT_AUTH_MODE`, or any listed Gateway variable, set; the message is actionable.
4. It refuses a Gateway-bound database, and an unbound database that holds owner data, with cutover instructions.
5. Every parity feature works: email, phone and login-name lifecycle; self-delete, email and password changes; browsers with several accounts; the waiting list; administration (settings, states, permissions, notes, audit, erase, team settlement); notices and bulk mail; locale; first-administrator setup. Every security fix holds.
6. Tasks, follow-ups, the waiting queue, goals and Fork run only on model connections. A turn without one is refused.
7. Hosts need no Gateway URL or tunnel, and the network table has no inference relay.
8. Account ownership, admission, session and security assertions in the tests are not weakened. Gateway-only tests are retired or replaced by rejection and no-dependency tests.
9. `make check`, `make test-db-local`, `make ci-frontend` and `make docs-verify` pass on the final commit.
10. A real-browser to real-API smoke on synthetic data covers: boot, setup, registration, sign-in, user settings, administration and model-connection onboarding. It runs with no Gateway server and no test-only sign-in path.
