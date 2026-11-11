# Operator-provided model connections

Status: proposed, design only, nothing implemented · 2026-11-09

When accepted, this supersedes three earlier non-goals: "administrator-paid"
inference in the [model connection design](2026-10-27-user-model-connections.zh-CN.md)
(§二, "团队共享连接、管理员代付"), and "no auto-configured FreeInference
connection" and "no account mapping" in the
[FreeInference account import](2026-11-05-freeinference-account-import.zh-CN.md)
(§7). The import itself still creates no connection. Everything else in those
designs and in the [independent-accounts decision](2026-10-30-independent-accounts-only.md)
still holds.

## Summary

A deployment should be able to give every approved user a working model source
with no setup, while users can still bring their own key for that provider or
any other. The feature must be neutral. An operator configures it, and
FreeInference is only one example of a value.

Recommendation:

1. **Phase 1, operator presets.** The operator declares a provider (name, Base
   URL, suggested model ids, a "get a key" link). Settings → Models offers it
   as a prefilled form, so bringing your own key takes one field. There is no
   provider dependency and no security change.
2. **Phase 2, provisioned connections.** For a preset whose provider
   implements a small key-provisioning contract (modelled on OpenRouter's
   provisioning-keys API), the control plane holds the operator's
   *provisioning credential*. On a user's first use it mints a key **for that
   user alone**. The minted key becomes an ordinary model connection owned by
   that user. Claim, residency, sandbox, verification gate and invalidation
   paths are unchanged. The sandbox holds only that user's key. The
   provisioning credential never leaves the control plane. Revoking an
   account disables the user's key upstream.
3. **Rejected:** putting the operator's shared key in every sandbox (option 2).
   **Not planned:** a control-plane inference relay (option 3), unless a
   provider without provisioning must be the default and the operator accepts
   proxy cost.

Provisioning is a provider API. It is not a Gateway sign-in, admission, grant
or catalog, so it does not conflict with AGENTS.md § 5 under the conditions
in [Does option 4 conflict with § 5?](#does-option-4-conflict-with--5).
[Decisions needed](#decisions-needed) lists what the user must confirm.

## How it works today

Every claim below is the current code at `7031c75`.

**Storage and encryption.** A connection is a row of
`agent_model_connections`. It holds a name, a Base URL, one to eight models,
`api_key_ciphertext`, `credential_revision`, `config_revision`, `enabled`,
per-model `model_checks` and a tombstone
(`apps/backend/migrations/versions/0106_user_model_connections.py:25-60`). The
key is encrypted under `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`
(`crypto/credential_cipher.py:14`) in the domain
`hybridinference/model-connection-key/v1`
(`application/model_connections.py:58`, `:84-89`). A separately-domained scan
copy is kept per sandbox group a key was bound to (`:59-62`, `:237-257`; table
at `0106…:97-112`). Turns name a connection by `model_connection_id` with no
foreign key, so a deletion can never rewrite a turn's source (`0106…:113-116`).
The repository selects explicit columns (`storage/repositories/model_connections.py:20-24`),
which matters for an additive migration. No owner route returns the key
(`api/model_connections.py:1-32`).

**Model-id verification.** Verification happens before saving: a create, or
an edit that changes the key or Base URL or adds models, verifies inside the
saving request (`application/model_connection_save.py:1-23`, `:117-155`). Each
model gets a streaming tool round trip on Chat Completions, Responses and
Anthropic Messages, with at most six requests. Results are `supported`,
`unsupported` or `failed` (`application/model_connection_check.py:1-35`).
Verification reserves the owner's hourly checks (20,
`model_connection_check.py:97`) all or none, with a 240 s deadline
(`model_connection_save.py:44`). The control plane dials only public IPv4
HTTPS, pinned, with no redirects and no proxy (`model_connection_check.py:24-31`,
`guard_base_url` at `:226`). Reasoning levels come from the provider's own
listings (`application/model_connection_efforts.py:62`, `:97`;
[upstream reasoning effort](2026-11-05-upstream-reasoning-effort.md),
[listing formats](2026-11-08-effort-levels-listing-and-gateway-mapping.md)).

**How a turn selects a connection.** A job carries `model` and
`model_connection_id` (an `mconn_` id, `schemas/jobs.py:83`, `:114-126`).
Admission refuses a turn without one (`application/job_admission.py:84-94`,
`:140-165`) and checks it with `require_connection_source` (`:111-137`). That
calls `resolve_turn_connection`, which reads the row **scoped to the owner**
(`application/model_connections.py:116-135`), and then the single predicate
`require_usable` (`domain/model_connections.py:262-298`). Follow-ups
(`application/follow_ups.py:121`), Fork (`application/session_fork.py:135`),
waiting messages and goal rounds ask the same predicate. Nothing falls back to
another connection, model or API. A new task starts where the owner's newest
turn ran, and there is deliberately no "first model" fallback
(`profile/model_preference.py:1-42`).

**How the key reaches the member sandbox.** At claim time,
`_mint_credentials` calls `claim_model_connection`
(`application/turn_claim.py:539-575`; `application/model_connections.py:309-349`).
The claim re-reads the connection, decrypts the key and returns a
`ModelConnectionAccess` (`cloud_agent_shared/model_connections.py:183-210`).
When the member process starts, `POST /worker/jobs/{id}/sandbox/residency`
(`api/worker.py:828-905`) checks the connection again. It stamps the revision
on the residency under the connection's lock
(`application/model_connections.py:373-397`; `storage/repositories/model_connections.py:348`)
and returns the key as the member's credential (`:400-440`). The host writes
it into the CLI's environment (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`,
`cloud_agent_host/runtimes/base.py:554-575`) and its activation tmpfs, and
masks it in durable output. A new key or Base URL, disabling or deleting
releases every residency on the old revision and closes its process
(`application/model_connections.py:260-287`;
`storage/repositories/model_connections.py:427`).

**The operator switch.** The composer's "disabled" copy says *"The operator
has disabled personal model connections for this deployment"*
(`apps/frontend/src/lib/i18n/dictionaries/modelSources.ts:9-11`, shown by
`composer/ModelConnectionNotice.tsx:9-31`). The backend switch is
`AGENT_MODEL_CONNECTIONS_ENABLED`. It is on unless set to `0`, `false` or `no`
(`application/model_connections.py:100-108`; `ENVIRONMENT.md:418`), and it is
read only from the environment because it is not in the settings registry.
Since connections are the only inference, it is in effect the switch for all
inference. `/config` offers nothing while it is off (`api/config.py:120-122`).
Admission refuses with `feature_disabled` (`job_admission.py:126-127`), and the
connection routes refuse to add or re-enable (`api/model_connections.py:372-378`).

**Settings → Models.** `ModelsSettingsView` renders `ModelConnectionsSettings`
(`apps/frontend/src/components/features/settings/ModelsSettingsView.tsx:7-14`):
an Add button, a form and one card per personal connection
(`ModelConnectionsSettings.tsx:80-157`). The composer offers each enabled
connection's models that the chosen agent can run (`composer/modelSource.ts:61-69`).
A choice is always the pair (model, connection). The label adds the
connection's name only when two sources list the same model
(`modelSource.ts:114-127`). The setup copy names FreeInference in the neutral
repository (`modelSources.ts:5-8`).

**Usage.** Turns record only what agents report (`usage/runtime_usage.py:1-23`).
`JobUsageResponse.external_provider` marks a turn that ran on a connection
(`usage/turn_report.py:34-47`), and the UI says "your provider bills it"
(`modelSources.ts:19-20`). There is no cost, no quota and no budget
(AGENTS.md § 6).

**Account revocation.** Revoking or suspending an account opens a termination
record. `terminate_owner_access` cancels and stops turns, releases residencies,
kills terminals, and keeps the record open, retrying every 30 s, until every
step is acknowledged (`application/owner_termination.py:1-38`, `:101-168`).
Erasure walks every table with a `user_id` (`storage/_sql/local_erasure.py:1-38`,
`:279`). Neither path does anything outside Cloud Agent, because nothing
outside it was ever issued for the user.

**Precedent: a credential the sandbox must not hold.** The GitHub relay gives
each member an `agh.` placeholder (`source_control/git_identity.py:14-29`,
`tokens.py:374`). The relay is a separate listener reached through a
per-host tunnel (`app.py:449-477`; `cloud_agent_shared/egress.py:123-124`,
`:250-256`). It resolves the placeholder to a live, leased turn
(`storage/repositories/github_relay.py:22-45`), injects the owner's token only
for the conversation's repository (`api/github_relay.py:277-331`;
`source_control/github_relay_policy.py:1-17`), and passes the sandbox's own
credentials through unchanged (`api/github_relay.py:267-275`). It exists because
"a GitHub user token cannot be narrowed to one repository"
([PR design](2026-11-04-agent-native-pull-requests.zh-CN.md) §3, line 38).
Built-in web search is a second precedent. An administrator's provider key
stays in encrypted settings, and the MCP relay adds it to one fixed request
per tool call (`providers/web_search.py:1-8`).

**Boundaries any design must keep:**

- AGENTS.md § 5: no `serving.*` import, and no Gateway sign-in,
  admission, grant or catalog. Retired Gateway variables are refused at startup
  (`accounts/config.py:81-102`).
- AGENTS.md § 7, SECURITY.md:29 and README.md:129: a sandbox
  holds its owner's own provider key and scoped tokens. Platform-held
  long-lived credentials never enter it.
- The decision that Gateway inference, grant minting on claims and the
  inference relay on each group bridge were removed
  ([decision](2026-10-30-independent-accounts-only.md) lines 10, 51, 72).

## Options

### 1. Operator preset only

**What it is.** The operator declares a provider: display name, Base URL,
suggested model ids and a link to where a user gets a key. Settings → Models
offers "Add your *Name* key", which opens the existing form prefilled. The user
pastes a key, and the existing verified save stores a personal connection.

**Fit with the boundaries.** Nothing changes in security, the claim or the
sandbox. There is no provider dependency.

**Cost.** Small. It is configuration, one card and form defaults. It also fixes
the hardcoded FreeInference copy.

**Verdict.** It does **not** meet "no setup", because every user still needs a
key. It is the right first step and the base the other phases build on.

### 2. Operator-shared key in the sandbox

**What it is.** Every user's turn gets the operator's single key through the
existing claim and residency.

**Why it fails.** Any user's agent can read it. So can any repository code,
dependency or prompt injection the agent meets. It is in the CLI's
environment, and everything the CLI runs inherits it. Even the
protocol-translation helper runs as the member's own UID (AGENTS.md § 7, owner
model connections). It can leave through the public 443 egress, which nothing
inspects (`egress.py:42-47`).

**Consequences.** One leak spends the whole allowance for every user. Misuse
cannot be attributed to a user. One user cannot be revoked without rotating
everyone's key, and rotation closes every running member on the deployment.
The scan copies would store the operator key once per sandbox group. It
plainly violates "never holds a platform credential" (SECURITY.md:29) and
"platform-held … service credentials never enter the sandbox" (AGENTS.md § 7).
Masking and short-lived environments do not help, because the CLI itself must
hold the key.

**Verdict.** Rejected, and not offered as an opt-in flag either.

### 3. Control-plane model relay

**What it is.** It mirrors the GitHub relay. Members get a residency-scoped
placeholder and a platform-supplied Base URL on the group bridge. A new
inference relay listener exchanges the placeholder for the operator key,
fenced to a running turn, and forwards to the provider. It enforces per-user
rate limits and model allowlists.

**What it gets right.** It is the strongest boundary. The key never leaves the
control plane, a leaked placeholder is useless outside the bridge, and
revocation takes effect on the next request.

**What it costs.**

- **The control plane becomes an inference proxy on every turn's critical
  path.** Every prompt passes through it, often hundreds of kilobytes of
  context and tool output per call and dozens of calls per turn. So does every
  token of streams that last minutes. That path is sandbox → bridge → host
  tunnel → control plane → provider. It adds latency and puts head-of-line
  blocking on the host's single tunnel. A control-plane deploy or restart cuts
  every in-flight model stream, which is not true today.
- **Abuse controls become Cloud Agent's job.** Rate limits, concurrency caps,
  model locks and, for any per-user allowance, token accounting across three
  protocols would all live in Cloud Agent. Cloud Agent becomes the "routing or
  billing gateway" and "key pool" the 2026-10-27 design declined
  (§二, line 38). It also introduces a local quota, which AGENTS.md § 6 says
  does not exist.
- **The egress rules change.** It needs a new relay endpoint on every group
  bridge and a new host tunnel. That is the "inference relay on each group
  bridge, and its port in the host table" deleted on 2026-10-30 (decision
  line 72). AGENTS.md § 7 allows the bridge "nothing else" beyond its listed
  relays, so this is a design change, not configuration.

**Why the GitHub precedent does not carry over.** That relay exists because
GitHub cannot scope a user token to one repository. Here, option 4 can scope a
key to one user at the provider. Git traffic is also occasional, while
inference is continuous and latency-sensitive. Web search is relayed, but it
is one small request per tool call.

**Variant 3b, an injector on each host.** It saves the control-plane hop but
puts the shared operator key on every host. Today a host holds only the keys
of turns it is running.

**Verdict.** Not planned. Revisit it only if an operator must default to a
provider that cannot mint per-user keys and accepts these costs.

### 4. Per-user provisioned keys

**What it is.** The operator gives the control plane a provisioning credential
for a provider's key-management API. On a user's first use, the control plane
mints a key for that user, with per-key limits from an operator template. It
stores the key as that user's own model connection. From then on everything is
the existing path: verification gate, claim, residency, member environment,
masking, scan copy and invalidation. Revoking the account disables the key
upstream.

**The precedent.** OpenRouter's provisioning keys can manage API keys but not
run inference. `POST /api/v1/keys` returns the new key's secret once, beside an
identifier (`hash`). Keys are listed, read, updated (`disabled`, `limit`) and
deleted by that identifier. `GET /api/v1/key` reports the calling key's own
usage and limit. FreeInference would implement the same small contract on its
side.

**What the sandbox holds.** It holds a key issued for its owner alone. If it
leaks, the exposure is that user's per-key allowance until the provider limit
is reached, the key is reissued or the account is revoked. The leak is
attributable to one key and one user. Revoking it touches nobody else.

**The trade-off.** The leaked key spends the **operator's** allowance, not the
owner's. That is a real change to "accepted owner-key exposure", and it is
bounded by the per-key limit the operator sets. For a provider whose keys are
free to any user anyway (FreeInference is one example), the marginal risk is
close to nil.

**Cost.** Medium. It needs a provisioning client, derived connections, a small
upstream-operations queue, a hook into termination and erasure, an admin page
and UI. It requires the provider to implement the contract.

**Verdict.** Recommended for "no setup".

### 5. Considered: provider OAuth ("Connect *Provider*")

Some providers, OpenRouter among them, let an app obtain a key on the **user's
own** provider account through an OAuth/PKCE flow. That keeps "the owner's own
key and bill" exactly. It is a good later improvement to bring-your-own-key
(BYO), but it needs a provider account and a sign-in, so it does not meet "no
setup". It is listed as an open question rather than as a phase.

### Comparison

| | 1 Preset | 2 Shared key | 3 Relay | 4 Provisioned |
|---|---|---|---|---|
| No setup for users | No | Yes | Yes | Yes |
| What the sandbox holds | Owner's own key | Operator's key | Placeholder only | A key minted for that owner alone |
| If a sandbox leaks it | Owner's own quota | Everyone's allowance | Nothing usable outside | That owner's per-key allowance, until revoked |
| Revoke one user | — | Impossible without breaking all | Immediate | Disable that key upstream |
| Control plane on the inference path | No | No | Yes | No |
| New sandbox egress | None | None | A relay on every bridge | None |
| Provider requirement | None | None | None | Key-provisioning contract |
| AGENTS.md § 7 / SECURITY.md | Unchanged | Violated | Unchanged | Reworded exposure, bounded |
| Effort | Small | Small | Large | Medium |

### Does option 4 conflict with § 5?

**The case that it does:**

- FreeInference *is* the HybridInference Gateway. A control-plane call that
  mints an inference credential there for a Cloud Agent user is functionally
  what the deleted grant path did: "grant minting on claims" (decision
  line 51), renewal, and revocation by workers.
- It re-couples onboarding to the availability of a Gateway API.
- The account-import decision declined both auto-configured FreeInference
  connections and any FreeInference↔Cloud Agent account mapping (import spec
  lines 58, 60). A key per Cloud Agent account looks like such a mapping.

**The case that it does not:**

- § 5 forbids four things: sign-in, admission, grant and catalog. Provisioning
  is none of them. It authenticates no one into Cloud Agent and decides no
  admission, and that stays with `accounts/agent_access.py`. It is not
  consulted per turn or per attempt. It supplies no model list: models still
  come from operator configuration and Cloud Agent's own verification.
- The grant path's coupling was structural. The Gateway checked Cloud Agent's
  attempt leases (`agent_grants` as "the fence",
  [gateway removal](gateway-removal.md) § 1.1), and grants were minted at every
  claim. Inference also ran through a relay into the Gateway. Here there is one
  creation call per user. No claim, residency or running turn ever calls the
  provisioning API, and the provider never calls Cloud Agent.
- The contract is generic. OpenRouter already offers it, and any provider can
  implement it. It is configured by URL, with no `serving.*` import and none
  of the retired `GATEWAY_*` names.
- The key's name is an opaque per-deployment HMAC tag on the operator's own
  provider account. It is not a link to anyone's FreeInference account, and
  nothing synchronizes identities.

**Conclusion.** There is no conflict, provided the following hold, and the
implementing PR should state them in § 5:

1. The contract stays a key-management API. It carries no user identity
   (email, login), no admission decision and no model catalog. If a future
   need adds any of these, it is a § 5 change that needs its own review.
2. Admission never depends on it. A failed provision never blocks sign-in or
   bring-your-own-key.
3. No turn's start or continuation calls it.
4. The provider is configured by URL through the neutral contract, never by
   a Gateway-specific client.

## Recommendation

### Vocabulary

- **Operator connection.** A deployment-level provider entry the operator
  configures, with id `oconn_<32 hex>`. Its mode is `preset` or `provisioned`.
- **Provided connection.** A provisioned key a user received from an operator
  connection, stored as that user's row in `agent_model_connections`, with a
  normal `mconn_` id and `operator_connection_id` set.
- **Personal connection.** Today's bring-your-own row, unchanged.

### Invariants

1. **A provided connection is an owner connection.** It belongs to exactly
   one user, its key was minted for that user alone, and every existing gate
   applies unchanged: `require_usable`, claim, residency, Fork and team rules,
   invalidation, masking and scan copies. The host sees the same claim shape.
   It needs no new capability.
2. **The provisioning credential never leaves the control plane.** It is not
   in a claim, a residency, `/config`, a log, an audit detail or any owner or
   admin response. It is stored under the settings root
   (`crypto/domain_cipher.py:23-34`) in its own domain
   (`cloud-agent/operator-provisioning-credential/v1`), next to the other
   platform credentials. Per-user keys stay in
   `hybridinference/model-connection-key/v1` like every owner key.
3. **Nothing falls back.** A provided source that cannot run never moves a
   turn to the user's personal key, and a personal one never moves to the
   operator's allowance. Either way that would bill someone who did not choose
   it. The existing "nothing falls back" rule extends to sources.
4. **Admission is independent of the provider.** See the § 5 conditions.
5. **Revocation reaches the provider.** An account whose access ends has its
   provided keys disabled upstream before its termination record closes.

### Phasing

| Phase | Delivers | Depends on |
|---|---|---|
| 1. Presets | Operator connections in `preset` mode, from configuration or the admin page. A "Recommended" card in Settings → Models that opens the prefilled form. Neutral setup copy naming the preset. Optionally, the `personal_model_connections_enabled` setting. | Nothing external |
| 2. Provisioned connections | `provisioned` mode: the contract client, provided connections, activation, the operations queue, fan-out of operator changes, termination and erasure hooks, admin per-user table, reissue, usage source labels, and the AGENTS.md, SECURITY.md and README wording | A provider implementing the contract (OpenRouter for development; FreeInference in production) |
| 3. Allowance and fleet operations | Provider-reported allowance display, reissue-all, orphan sweeper, optional key expiry | Phase 2 |
| — | Option 3 relay: only on the trigger above | — |

### Data model and migration sketch

One additive revision, the next free one (0116 at the time of writing):

```sql
CREATE TABLE agent_operator_model_connections (
    id TEXT PRIMARY KEY CHECK (id ~ '^oconn_[0-9a-f]{32}$'),
    slug TEXT NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,39}$'),  -- stable handle for declared config
    name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 64 AND name !~ '[[:cntrl:]]'),
    mode TEXT NOT NULL CHECK (mode IN ('preset', 'provisioned')),
    base_url TEXT NOT NULL CHECK (char_length(base_url) BETWEEN 9 AND 512 AND base_url LIKE 'https://%'),
    models TEXT[] NOT NULL CHECK (cardinality(models) BETWEEN 1 AND 8),
    default_model TEXT,                      -- one of models; preselected for a user with no history
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    help_url TEXT,                           -- where a user gets their own key (BYO)
    managed_by TEXT NOT NULL CHECK (managed_by IN ('admin', 'deployment')),
    provisioning_url TEXT,                   -- provisioned mode
    provisioning_credential_ciphertext TEXT, -- provisioned mode; write-only
    key_template JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(key_template) = 'object' AND octet_length(key_template::text) <= 4096),
    key_name_prefix TEXT,                    -- tells deployments apart at one provider
    model_checks JSONB NOT NULL DEFAULT '{}'::jsonb,  -- the operator's verification, same shape as owners'
    config_revision BIGINT NOT NULL DEFAULT 1,
    credential_revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    CONSTRAINT ck_operator_connections_provisioned CHECK (
        mode <> 'provisioned' OR deleted_at IS NOT NULL
        OR (provisioning_url IS NOT NULL AND provisioning_credential_ciphertext IS NOT NULL))
);
CREATE UNIQUE INDEX uq_operator_connections_slug
    ON agent_operator_model_connections (slug) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX uq_operator_connections_default
    ON agent_operator_model_connections ((1)) WHERE is_default AND deleted_at IS NULL;

-- A provided connection is an ordinary owner row with a provenance.
ALTER TABLE agent_model_connections
    ADD COLUMN operator_connection_id TEXT REFERENCES agent_operator_model_connections (id),
    ADD COLUMN provider_key_ref TEXT,    -- the provider's key id (OpenRouter: hash); not secret
    ADD COLUMN provider_key_name TEXT,   -- the opaque name it was created with
    ADD COLUMN provider_key_state TEXT
        CHECK (provider_key_state IN ('active', 'disabling', 'disabled', 'enabling')),
    ADD CONSTRAINT ck_agent_model_connections_provided CHECK (
        operator_connection_id IS NULL
        OR (provider_key_ref IS NOT NULL AND provider_key_name IS NOT NULL
            AND provider_key_state IS NOT NULL));
CREATE UNIQUE INDEX uq_agent_model_connections_provided
    ON agent_model_connections (user_id, operator_connection_id)
    WHERE deleted_at IS NULL AND operator_connection_id IS NOT NULL;
-- An owner may name a personal connection like a provided one.
DROP INDEX uq_agent_model_connections_owner_name;
CREATE UNIQUE INDEX uq_agent_model_connections_owner_name
    ON agent_model_connections (user_id, lower(name))
    WHERE deleted_at IS NULL AND operator_connection_id IS NULL;

-- Upstream key work still owed. No user_id column, so an account erasure
-- (which walks every user_id table) leaves outstanding revocations in place.
CREATE TABLE agent_provider_key_operations (
    id BIGSERIAL PRIMARY KEY,
    operator_connection_id TEXT NOT NULL REFERENCES agent_operator_model_connections (id),
    owner_tag TEXT NOT NULL,             -- HMAC of the account id; see the key name
    kind TEXT NOT NULL CHECK (kind IN ('create', 'disable', 'enable', 'delete')),
    key_name TEXT NOT NULL,
    provider_key_ref TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'sent', 'done', 'failed')),
    attempts INT NOT NULL DEFAULT 0,
    last_error_category TEXT,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX uq_provider_key_create_in_flight
    ON agent_provider_key_operations (operator_connection_id, owner_tag)
    WHERE kind = 'create' AND state IN ('pending', 'sent');
```

**Reading a provided row.** It is a complete row. The operator's `base_url`,
`models`, `model_checks` and `name` are **copied** into it. Its `model_checks`
entries carry the provided row's own `config_revision`. So `require_usable`,
the claim and the residency read it exactly as they read a personal row.

The repository adds two terms to the usable state of a provided row: the
operator connection must be enabled and not deleted (a join on reads and
inside `bind_residency`), and `provider_key_state` must be `active`. These
refuse with the new reasons `provided_unavailable` and `provided_inactive`,
which have their own owner-facing sentences (`domain/model_connections.py:51-106`).
Neither is the existing "disabled", whose sentence tells the owner to enable
it themselves.

**Operator changes fan out in one transaction.** This mirrors the owner
invalidation table (model connection design §十二):

| Operator change | Provided rows | Running processes |
|---|---|---|
| New Base URL (verified first) | `base_url` and facts copied; `config_revision` +1, `credential_revision` +1 | Closed (`release_stale_residencies` for every derived row) |
| Verify again, same Base URL | Facts copied; `config_revision` +1 | Kept; the plan is re-checked at the next residency, as today |
| Add models (verified first) | Models and facts copied | Kept |
| Remove models, rename, help link, default model | Copied | Kept |
| Disable | `credential_revision` +1 | Closed |
| Enable | — | — |
| New provisioning credential | — (keys stay manageable, a contract requirement) | Kept |
| New provisioning URL | Not an edit: create a new operator connection and delete this one | — |
| New key template | Applies to keys minted from now on; reissue-all applies it to existing ones | Kept |
| Delete | Tombstoned, key erased, a `delete` operation queued per key | Closed |

Bumping `credential_revision` on each derived row takes that row's lock. So
the existing protocol between `bind_residency` and invalidation (bind first,
then the release finds it; or invalidate first, then the bind is refused)
holds without change.

**Rolling deployment.** The previous release selects explicit columns, so it
reads the new columns safely. It would, however, show a provided row as an
editable personal one. So no provided row may be created until every
control-plane replica runs the release. Operator connections are created
disabled. The runbook, and a downstream distribution's change order, enable or declare them
only after the rollout. Hosts need no change. The downgrade refuses while any
operator connection or provided row exists, as `0106` does (`0106…:138-175`).

**Unverified-key exception.** A provided row's facts come from the operator's
verification of the same endpoint and models, using a key from the same
template. The per-user key itself is not inference-checked; its successful
creation is the evidence. This is a deliberate, documented exception to
"nothing is saved unverified", taken so first use costs no inference.

### The provisioning contract (v1)

Cloud Agent speaks one contract. It is a documented subset of OpenRouter's
provisioning-keys API, so a deployment can point at OpenRouter directly, and
any other provider (FreeInference included) can implement it. Field names
follow OpenRouter. Before implementation, check them against OpenRouter's
current reference; the adapter's tests use recorded fixtures, never OpenRouter
live.

| Call | Request | Answer Cloud Agent reads |
|---|---|---|
| Create | `POST {provisioning_url}/keys`, `{"name": <key name>, …key_template}` | `201`, `{"data": {"hash", "name", "disabled", "limit"?}, "key": <secret>}`. The secret is answered once. |
| List | `GET {provisioning_url}/keys?offset=N` | `{"data": [{"hash", "name", "disabled"}…]}` |
| Read | `GET {provisioning_url}/keys/{hash}` | `{"data": {…}}`. Optional fields `usage`, `limit`, `limit_remaining`, `limit_reset` (phase 3). `404` means gone. |
| Disable / enable | `PATCH {provisioning_url}/keys/{hash}`, `{"disabled": true|false}` | `200` |
| Delete | `DELETE {provisioning_url}/keys/{hash}` | `200` or `204`; `404` counts as done |

Every call carries `Authorization: Bearer <provisioning credential>`. The
provider must guarantee the following:

- The provisioning credential cannot run inference.
- A created key works at the operator connection's Base URL for its models.
- A disabled or deleted key is refused by inference promptly.
- `name` is stored verbatim and listed.
- Keys stay manageable after the provisioning credential is rotated.

The control plane dials `provisioning_url` under the same rules as
verification: public IPv4 HTTPS, pinned, no redirects, no proxy, bounded reads
and a timeout. It never echoes a body. It never logs the secret, the
credential or the template's values.

**Key name.** `<key_name_prefix>:<owner_tag>:<nonce>`, where `owner_tag` is
the first 24 hex digits of an HMAC of the account id under a settings-root
domain key. The name contains no email, login or user id. An administrator can
compute the tag for an account to find its keys at the provider.

**A lost create response.** Before the POST, a `create` operation is recorded
with its exact name. If the answer is lost (timeout, reset, 5xx after send),
the next activation, or the operations worker, lists keys, deletes any key
carrying that name and marks the operation `failed`. Then it creates afresh.
A secret is answered only once, so a found orphan is never adopted.

### Lifecycle

**Activation is first use.** Owner route:
`POST /v1/agent/provided-connections/{oconn_id}/activate`. It is idempotent
and answers the owner's provided connection.

- **When the composer calls it.** On mount, the composer activates the default
  operator connection by itself when the owner has no usable connection at
  all. That is the "no setup" case. Otherwise the composer activates a
  provided source when the owner first picks one of its models, and Settings
  → Models activates it from its card.
- **What the composer submits.** It never submits an `oconn_` id. Admission
  still takes only `mconn_` ids (`schemas/jobs.py:83`) and makes no provider
  call.
- **The steps.**
  1. Require `agent.use`.
  2. Require an operator connection that is enabled, provisioned and verified.
  3. Return the existing row if one exists.
  4. Reserve one of the owner's hourly checks, reusing
     `reserve_model_connection_checks` (`storage/repositories/model_connections.py:228`).
  5. Insert the `create` operation. The partial unique index makes concurrent
     activations single-flight.
  6. POST outside any transaction, with a timeout of about 15 s and a small
     per-process concurrency bound.
  7. Encrypt the key. Then, in one transaction, insert the provided row
     (version 1, facts copied, state `active`) and mark the operation `done`.
- **Nothing at approval.** Keys are never minted when an account is approved,
  which avoids keys for users who never run anything.

**Provisioning failure.** Activation answers `503
provided_connection_unavailable` with `Retry-After` and a category: `provider_unreachable`, `rate_limited`,
`credential_refused` or `provider_error`. The provider's text is never shown.
No provided row is written. The composer keeps the draft and offers "Try
again" and "Add your own key". The admin page shows the last error and since
when, per operator connection. A `credential_refused` is operator-facing ("the
provisioning credential was refused") and is logged at error level.

**Provider outage at inference.** It behaves exactly as for any connection
today: the CLI reports the provider's error and the turn fails with it. There
is no fallback (invariant 3). The admin may disable the operator connection
with a reason, which the composer shows. Personal connections keep working.
Waiting messages and goal rounds on a provided row that becomes unusable are
held with the reason, as they are today for a disabled connection.

**Account revocation and suspension.** `terminate_owner_access` gains a step
after `end_owner_work` (`owner_termination.py:141-168`):

- Each `active` provided row moves to `disabling`, a `disable` operation is
  attempted at once, and it is retried by the 30 s enforcement pass.
- The record closes only when every disable is `done`. The outcome while one is
  outstanding is `provider_keys_pending`.
- An unreachable provider therefore keeps the record open, which blocks
  re-approval. That is intended: a working key must not outlive the access it
  was minted for.

**Restoring access.** Restoring enqueues `enable` for the account's `disabled`
rows (`enabling` → `active`). Re-enabling a credential is not replaying work,
so "restoring access replays nothing" still holds.

**Erasure.** Erasure runs after termination, so the keys are already disabled.
It queues `delete` for each provided key, then the generic walk removes the
rows. The operations table has no `user_id`, so the deletions survive the
erasure and drain afterwards.

**Rotation.**

- **The owner's key.** Owner route
  `POST /v1/agent/provided-connections/{oconn_id}/reissue` ("Get a new key"),
  or the admin action for one account. A new key is minted first. Its
  ciphertext replaces the old one and bumps `credential_revision`, which closes
  running processes. Only then is a `delete` of the old key queued, so the
  owner is never without a key. Each reissue costs one hourly check.
- **The provisioning credential.** The admin sets a new one. It is checked
  with one `List` call, and existing keys are unaffected.
- **Every key (phase 3).** "Reissue all" marks every provided row for
  reissue, closes processes and deletes the old keys in batches. Each owner's
  next activation mints anew.
- **Expiry (optional, phase 3).** If the provider supports key expiry, it is
  handled as a scheduled reissue.

**The operations worker.** A background loop on the main listener claims due
operations (`FOR UPDATE SKIP LOCKED`), retries with backoff, and updates
`provider_key_state` on the matching row when there is one. It prunes `done`
rows after 30 days. Phase 3 adds an orphan sweeper. It lists keys carrying
this deployment's prefix and deletes any that no live row or in-flight
operation names.

### What the operator configures

There are two ways, and the first wins.

**Declared by the deployment.** `AGENT_PROVIDED_MODELS_FILE` names a YAML or
JSON file, and every secret in it is referenced by an environment variable
name. At startup, declared entries are upserted by `slug` with `managed_by =
'deployment'` and shown read-only in the admin page. The precedent is the
Terms that site files replace (`api/admin_settings.py:45-58`). If an endpoint
changed, it is verified in the background, and the entry is unusable until
the verification passes. Startup never waits on a provider. An entry removed
from the file is disabled, not deleted, and an administrator deletes it.

**Through the admin page.** Admin → Settings → Models manages entries that are
not declared (`managed_by = 'admin'`). Edits that change the endpoint or add
models verify inside the save, exactly as owners' do.

Per operator connection:

| Field | Mode | Notes |
|---|---|---|
| `slug`, `name` | both | `name` is what users see ("Provided by *name*") |
| `base_url`, `models` (≤ 8), `default_model` | both | Same rules as owner connections (`domain/model_connections.py:145-194`) |
| `default` | both | At most one; preselected for a user with no history |
| `enabled` | both | Created off |
| `help_url` | both | Shown beside BYO: "Get your own key" |
| `provisioning.url`, `provisioning.credential_env` | provisioned | The credential is written once, write-only, under the settings root |
| `provisioning.key_template` | provisioned | Opaque JSON object, ≤ 4 KB, sent on create. Cloud Agent never interprets it, so it holds no quota semantics. The admin page warns when created keys report no `limit`. |
| `provisioning.key_name_prefix` | provisioned | Distinct per deployment (staging and production) |

Deployment-wide settings:

- `AGENT_MODEL_CONNECTIONS_ENABLED`, unchanged. It remains the master switch
  for all inference, provided sources included. Its composer copy becomes
  "Models are switched off on this deployment" instead of naming personal
  connections.
- New stored setting `personal_model_connections_enabled` (default `true`).
  Off, it refuses new personal connections and does not offer existing ones,
  while provided sources keep working. FreeInference keeps it on. This is a
  decision item.

### Admin API and audit

`/admin/agent/operator-model-connections`, behind `verify_admin_access`, all
`no-store`:

- `GET` lists entries with status, verification per model, provisioned and
  pending counts, the last provisioning error and warnings.
- `POST` creates one. `PATCH /{id}` changes fields, verifying inside the save
  when the endpoint changes or models are added. A secret field is write-only.
- `DELETE /{id}` deletes one.
- `POST /{id}/verify` verifies again. In provisioned mode it mints a temporary
  key named `<prefix>:verify:<nonce>`, runs `verify_models` and
  `read_effort_facts`, and deletes the key. In preset mode it uses a test key
  pasted for that request and never stored. Limit: 10 per hour per entry.
- `GET /{id}/owners?cursor=` pages accounts with their state, when the key was
  provisioned, the key-reference suffix and the last error.
- `POST /{id}/owners/{account_id}/reissue` and `…/disable` act on one account.
- `POST /{id}/reissue-all` (phase 3).

New registry entries in `domain/audit.py`, category `settings`, target type
`operator_model_connection`, target key `connection_id` unless stated:

| Action | Notes |
|---|---|
| `agent.operator_model_connection.create` | |
| `agent.operator_model_connection.update` | Changed fields. Secrets as `[redacted]`, as settings do (`api/admin_settings.py:135-157`) |
| `agent.operator_model_connection.verify` | Per-model status only |
| `agent.operator_model_connection.enable` / `.disable` | Includes the reason shown to users |
| `agent.operator_model_connection.set_default` | |
| `agent.operator_model_connection.delete` | Asynchronous (`correlation_key` = operation id) for the key deletions |
| `agent.operator_model_connection.provisioning_credential_set` | Always `[redacted]`; kept separate so a credential rotation stands out |
| `agent.operator_model_connection.declared_sync` | Actor `deployment-config` (classified `service`), when startup reconciliation changed something |
| `agent.operator_model_connection.reissue_owner` / `.disable_owner` | Category `accounts`, target the account |
| `agent.operator_model_connection.reissue_all` | Phase 3; asynchronous |

Owner activation and reissue are not admin audit material, because owner
connection changes are not audited today. They are logged as structured
events (`agent_provided_key_*`). Upstream disables during termination are
reported on the termination record's outcome.

### Owner API changes

- **`GET /v1/agent/config`.**
  - `model_connections` includes the owner's provided rows, with a new
    `provided_by: {id, name} | null`.
  - New `provided_sources` lists each enabled, verified operator connection:
    `id`, `name`, `mode`, `models`, `model_runtimes`, `model_efforts` (from
    the operator's facts), `default_model`, `default`, `connection_id` (the
    owner's row, or null before activation), `status` and `help_url`.
  - `model_connection_required` is false when a provisioned default source
    exists, even before activation (`api/config.py:150-153`).
  - `model_connections_enabled` keeps its meaning.
- **Default preselection.** `default_model` and `default_model_connection_id`
  are the newest turn's source while usable (unchanged). Otherwise they are the
  default operator connection's `default_model` on the owner's provided row
  once activated. This is an operator's explicit choice, not the "first model"
  fallback that `profile/model_preference.py` rejects.
- **`GET /v1/agent/model-connections`.** It adds `provided`, the view of each
  source with the owner's row status (no key, no provider reference), and
  `personal_enabled`. `connections` stays personal-only.
- **Edits to a provided row.** `PATCH /v1/agent/model-connections/{mconn}`
  accepts only `enabled`. Any other field answers `409
  model_connection_managed`. `DELETE`, `discover-models` and `refresh-efforts`
  on a provided row answer the same.
- **New owner routes.** `POST /v1/agent/provided-connections/{oconn}/activate`,
  `…/reissue`, and in phase 3 `GET …/allowance`.
- **Unchanged.** Jobs, follow-ups, waiting messages, goals, Fork, every worker
  route, the claim and the residency. `apps/backend/openapi.json` is
  regenerated.

### UI

- **Settings → Models.** It has two sections.
  - **Provided by this deployment.** One card per source, showing the name,
    the models with the agents that can use them, and a status: ready,
    setting up, unavailable with the reason, or suspended. A "Use in new
    tasks" switch sets the owner's `enabled`. "Get a new key" reissues. A
    phase 3 line reads "Limit reported by *provider*: …". There is no Base
    URL, key or model editing. The description says usage counts against the
    allowance the deployment set with the provider.
  - **Your connections.** Today's section. Phase 1 adds the preset card above
    it: "Add your own *name* key". It opens the form prefilled with name, Base
    URL and models, plus the `help_url` link. The user can still change every
    field, and the saved row is an ordinary personal connection.
- **Composer.**
  - **Grouping and labels.** The picker groups "Provided" and "Yours". The
    existing pair identity keeps two sources of the same model apart
    (`modelSource.ts:1-23`). When both list a model, the labels read
    "*model* · *provided name*" and "*model* · *your connection name*". A
    choice on a not-yet-activated source activates on pick, and Run waits
    for it.
  - **Bring-your-own precedence.** The last-used source wins, which is
    today's derived rule. Adding a personal key does not silently switch new
    tasks to it. It becomes the default once the owner runs a task on it.
  - **Per-chat choice.** It works as today. Each turn may switch source, and
    switching starts a new process, because the residency identity includes
    `connection@revision`.
  - **Notice.** `ModelConnectionNotice` uses the preset's name instead of the
    hardcoded FreeInference copy (`modelSources.ts:5-8`). It gains states for
    "Setting up *name*…" and "*name* is unavailable: try again or add your
    own key".
- **Admin.** Admin → Settings → Models lists entries, with managed-by, status,
  per-model verification, provisioning health, the per-account table and the
  actions above.

### Usage and allowance

- **Per-turn usage.** It stays `runtime_reported`. `JobUsageResponse` adds
  `source: "personal" | "provided"` and `source_name`. The provided text
  reads "Ran on *name*, provided by this deployment. The provider applies its
  limits; cost is not tracked here." The existing "your provider bills it"
  text stays for personal turns.
- **Usage pages.** Owner and admin Usage pages add a `by_source` breakdown
  (personal, or each operator connection), beside `by_model`
  (`usage/admin_usage.py`).
- **Allowance (phase 3).** It is shown only as the provider reports it,
  through the contract's optional `Read` fields, cached for 5 minutes and
  labelled with the provider's name. It never admits or refuses a turn,
  because the provider enforces its own limits and Cloud Agent keeps no quota
  (AGENTS.md § 6).

### Wording the implementing PR changes

- **SECURITY.md:29.** "It holds a provider key that belongs to the owner
  alone — their own, or one this deployment provisioned for them — for the
  turn it runs, and short-lived scoped tokens." SECURITY.md is on the
  translated docs site, so it needs its zh-CN catalog and `make docs-verify`.
- **AGENTS.md § 5.** Add the four conditions above.
- **AGENTS.md § 6.** "Every turn runs on one of its owner's model connections:
  their own, or one the deployment provisioned for them."
- **AGENTS.md § 7.** Add a bullet, "Operator-provided connections". A
  provisioned key spends the operator's allowance up to its per-key limit. A
  member can leak it like an owner key. The exposure is bounded, attributable
  and revocable per user. The provisioning credential stays in the control
  plane.
- **README.md:** "How it works" and the sandbox sentence.
- **`docs/developer/operations.md`:** a new section.
- **`ENVIRONMENT.md`:** the file variable and the secret names.
- **`docs/developer/web-app.md`.**

All of these are translated where the site includes them.

### What a downstream distribution sets, with FreeInference as the example

This is configuration only, and every value below is FreeInference's to
choose.

Phase 1:

```yaml
# provided-models.yaml, mounted read-only; AGENT_PROVIDED_MODELS_FILE points at it
version: 1
connections:
  - slug: freeinference
    name: FreeInference
    mode: preset
    default: true
    enabled: true
    base_url: https://<FreeInference inference API>/v1
    models: [<model ids FreeInference recommends for agents>]
    default_model: <one of them>
    help_url: https://freeinference.org/<where a user creates a key>
```

Phase 2 changes `mode: provisioned` and adds:

```yaml
    provisioning:
      url: https://<FreeInference key-management API>
      credential_env: AGENT_PROVISIONING_CREDENTIAL_FREEINFERENCE   # value in the deployment .env
      key_name_prefix: cloud-agent-production                       # cloud-agent-staging on staging
      key_template: {<FreeInference's per-key limit fields>}
```

The FreeInference side must:

- implement the contract;
- issue staging and production their own provisioning credentials;
- decide the per-key limits and which reasoning domains its listings declare;
- keep the provisioning credential unable to run inference.

The distribution must add the file only after the release that reads it is
fully rolled out, and keep `personal_model_connections_enabled` on so its
users can still bring their own keys.

## Decisions needed

1. **Approve option 4.** This means option 4 rather than the relay is the "no
   setup" mechanism, with the § 5 reading and conditions above. It also means
   accepting the reworded exposure: a sandbox may hold a key minted for its
   owner alone and billed to the operator, bounded by a per-key limit.
2. **Contract shape.** The recommendation is an OpenRouter-compatible subset,
   which works against OpenRouter on day one, with FreeInference implementing
   the subset. The alternative is a bespoke contract with an idempotency key.
3. **Activation timing.** The recommendation is first use, done
   automatically when the owner has nothing else. The alternatives are at
   approval or at first task submission.
4. **Configuration source.** The recommendation is a declared file, read-only
   in the UI, plus admin-managed entries. The alternative is file-only in v1.
5. **Default preselection.** Should the operator's `default_model` be
   preselected for an owner with no history? That amends the
   "no first-model fallback" rule for an explicit operator choice.
6. **Bring-your-own precedence.** The recommendation is that the last-used
   source wins. The alternative is that a personal key, once added, becomes
   the default.
7. **`personal_model_connections_enabled`.** Add it in phase 1, or not at all.
8. **Termination and an unreachable provider.** Should an unreachable
   provider keep the termination record open, blocking re-approval?
   Recommendation: yes.
9. **Allowance display (phase 3).** Is a provider-reported allowance compatible
   with "no local quota"? Recommendation: yes, as display only.
10. **Several provided sources.** Should a deployment be allowed more than one
    (for example FreeInference and OpenRouter)? Recommendation: yes, with at
    most one default.
11. **Later items.** Key expiry, and provider OAuth (option 5) as a later
    bring-your-own improvement.

## Test plan

**Unit** (`tests/unit/`), with no network:

- Operator connection validation: shares the owner cleaners, `slug`, one
  default, `key_template` bounds, and declared-file parsing with secrets by
  environment name only.
- Key name and `owner_tag`: deterministic, no PII, differs per deployment
  prefix.
- The contract client against an in-process fake provider:
  - create, list, read, patch and delete;
  - a lost create, reconciled by name, with the orphan deleted and never
    adopted;
  - the `401`, `403`, `404`, `429`, `5xx` and timeout categories;
  - redirects refused, pinned dial, protected ranges;
  - the provisioning credential and the key never in logs, errors or
    `repr`.
- `require_usable` on a provided row: identical to a personal row, plus
  `provided_unavailable` and `provided_inactive`.
- The fan-out table, row by row.

**Server** (`tests/servers/`):

- Activation:
  - idempotent;
  - concurrent activations mint one key;
  - a provider failure writes no row and answers `503` with a category;
  - the hourly check is spent;
  - a pending or revoked account is refused.
- `/config` lists provided sources, and `model_connection_required` is false
  before activation.
- Default preselection.
- `409 model_connection_managed` for every refused edit.
- A job on a provided row is admitted with no provider call.
- The claim and residency for a provided row have exactly the personal-row
  shape.
- Operator disable and Base URL change close processes.
- Admin routes: admin-only, `no-store`, one audit row per action, with secrets
  `[redacted]`.
- No owner or admin response, claim, residency or log contains the
  provisioning credential.

**Database** (`make test-db-local`, `tests/integration/storage/`):

- Migration upgrade, and downgrade refusal.
- The unique provided row per owner, and the single-flight create index.
- Fan-out bumps under concurrent `bind_residency`, in both orders.
- Termination: the record stays open until upstream disables are done, and
  closes after.
- Restore enqueues enable.
- Erasure with provided rows leaves `delete` operations that survive the
  walk.
- The operations worker claims with `SKIP LOCKED` across two connections.

**Frontend** (`make ci-frontend`):

- Settings → Models: provided cards with no edit controls, the preset card
  prefilling the form, reissue, and the status states.
- Composer:
  - grouping and disambiguated labels;
  - activation on pick or mount, and the failure notice with retry and BYO;
  - no fallback between sources;
  - follow-ups switching source.
- The usage source text.
- The neutral copy has no hardcoded provider name.

**End to end** (Playwright, fake provider): a new user signs in and the
composer is ready with no setup. A task runs. A personal key for the same
provider is added, and both sources are selectable and labelled. The
administrator revokes the account, and the fake provider sees the key
disabled.

**External** (`tests/external/`, opt-in):

- A contract conformance suite a provider (FreeInference) runs against its
  own implementation.
- The same suite against OpenRouter with a test provisioning key.
- One real Kata-host turn on a provisioned key in staging.

**Repository gates** for each implementing PR: `make check`,
`make test-db-local`, `make ci-frontend`, and `make docs-verify` after
translating the changed site pages.

## Noted, out of scope

The change that adds this design made the GitHub relay's refusals and the
GraphQL refusal neutral. What still names FreeInference in user-visible text
is the setup copy in `modelSources.ts:5-8`, which phase 1 replaces.
