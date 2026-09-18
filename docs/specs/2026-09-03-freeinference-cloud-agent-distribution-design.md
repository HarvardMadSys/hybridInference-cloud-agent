# Cloud Agent upstream / FreeInference distribution boundary

- **Language:** English · [简体中文](2026-09-03-freeinference-cloud-agent-distribution-design.zh-CN.md)
- **Status:** implemented — both environment handoffs were accepted and the
  upstream deployment workflows retired on 2026-09-18. Statements below about
  the “current” direct-deploy path are the pre-cutover baseline.
- **Date:** 2026-09-03
- **Applies to:**
  [HarvardMadSys/hybridInference-cloud-agent](https://github.com/HarvardMadSys/hybridInference-cloud-agent)
  (reusable upstream) and
  [HarvardMadSys/freeInference](https://github.com/HarvardMadSys/freeInference)
  (the private FreeInference distribution)
- **Related:** Cloud Agent pre-publication PRs
  [#98](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/98),
  [#99](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/99),
  [#100](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/100), and
  [#101](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/101);
  FreeInference's
  [upstream consumption contract](https://github.com/HarvardMadSys/freeInference/blob/dev/docs/maintainers/consuming-upstream.md)
- **Supersedes:** direct FreeInference staging and production ownership by this
  repository once the cutover in §10 completes. It does not supersede the
  Cloud Agent extraction plan, gateway HTTP contracts, or the sandbox security
  model.

---

## 0. Decision

Cloud Agent adopts the same product/platform boundary already used by
HybridInference and FreeInference:

> **The public upstream produces neutral, immutable runtime artifacts.
> The private FreeInference repository chooses, configures, deploys, promotes,
> and rolls back those artifacts through one reviewed lock.**

An upstream merge must not reach a FreeInference host. The target flow is:

```text
hybridInference-cloud-agent dev
  -> hosted CI and security gates
  -> immutable GHCR artifacts for one source SHA
  -> no deployment

freeInference bump-cloud-agent
  -> require the exact upstream SHA's green push CI
  -> resolve every required artifact to a digest
  -> verify package, platform, and revision labels
  -> open one cloud-agent.lock PR
  -> FreeInference CI
  -> merge to FreeInference dev
  -> staging deploy by lock
  -> manual promotion through main
  -> production deploy by the same lock
```

This is not merely a branding cleanup. Today this repository still owns
FreeInference runner labels, host paths, Compose projects, build-time web
identity, product-named harness wrappers, and both deployment workflows.
Making the repository public while preserving that arrangement would leave
public source control one successful upstream push away from private
infrastructure. PR runner guards reduce one attack path; they do not create
the missing ownership boundary.

The distribution is primarily a **repository and deployment contract**, not a
runtime Python plugin. Cloud Agent keeps its typed environment interface and
startup validation. The private manifest is validated and rendered into that
interface by FreeInference's deployment tooling; Cloud Agent never imports or
locates a private repository.

## 1. Problem

The service extraction completed the code and data boundary:

- Cloud Agent owns jobs, attempts, hosts, sandboxes, source-control
  integrations, publishing, and its database.
- HybridInference owns identity and inference-grant authority.
- The two services communicate only through the contracts in `contracts/`.
- Gateway signing keys never enter Cloud Agent.

The deployment boundary did not follow the service boundary. On current
`dev`:

1. `.github/workflows/ci.yml` runs for a push to `dev`.
2. `.github/workflows/deploy-staging.yml` listens for that CI workflow to
   complete successfully.
3. Both the control-plane and host jobs run on FreeInference-labelled
   self-hosted runners and deploy the source commit.
4. `deploy/control-plane/deploy.sh` and `deploy/host/deploy.sh` reset
   managed source checkouts, build locally, and replace the running stacks.

Consequently, even a documentation-only merge can enqueue a staging
deployment. Four separately merged publication PRs enqueue four deployments
because the staging deployment concurrency group deliberately does not cancel
an in-progress deploy.

Production is manual today, but it is still owned by the upstream repository:
`.github/workflows/deploy-prod.yml` names FreeInference runner pools, paths,
Compose identities, and environment variables. “Manual” narrows when it runs;
it does not establish the correct repository authority.

The remaining coupling is visible in six categories:

| Category | Current examples | Why it belongs elsewhere |
|---|---|---|
| Product identity | `FreeInference Cloud Agent`, FreeInference links and OpenAPI servers | A downstream distribution chooses its user-facing identity |
| Web build identity | `NEXT_PUBLIC_APP_NAME`, org/status/gateway values baked by `Dockerfile.web` | Baking creates different bytes for staging, production, and another operator |
| Host topology | `deploy-cloud-agent-*-staging`, `/srv/freeinference-cloud-agent` | These identify private machines and their managed checkouts |
| Stack identity | FreeInference Compose projects, networks, image tags, state roots | Changing these can adopt, orphan, or destroy the wrong runtime state |
| Harness integration identity | `pi-freeinference`, `freeinference/<model>`, `org.freeinference.runtime.*` | Some are presentation; others are persisted compatibility identifiers |
| Release authority | direct source checkout, build, deploy, and rollback in the upstream repo | The product distribution must decide which verified upstream release runs |

String replacement alone cannot classify those correctly. A product title can
move immediately. A persisted provider ID or Docker label needs a compatibility
window. A database table, OAuth audience, or environment variable may be a
protocol identifier rather than branding and must not be renamed at all.

## 2. Goals and non-goals

### Goals

1. A merge, release, compromise, or workflow mistake in the public upstream
   changes no FreeInference host until the private distribution accepts a new
   lock.
2. Staging and production consume immutable artifacts from one upstream source
   SHA, by digest, with provenance checked before replacement.
3. FreeInference retains its product name, URLs, GitHub App, operational
   topology, policies, and smoke tests without forking Cloud Agent code.
4. The first distribution-owned deployment preserves existing databases,
   volumes, Docker data roots, networks, container ownership, enrolled-host
   credentials, retained sandboxes, and native session state.
5. A failed cutover can restore the preceding distribution revision and image
   set without rebuilding old source.
6. A third party can run a neutral example without seeing FreeInference host
   inventory or needing a FreeInference account.
7. The public repository contains no secret values and no workflow capable of
   deploying to FreeInference infrastructure.
8. The identity and inference-grant contracts, sandbox egress boundaries, and
   job fencing semantics remain unchanged.

### Non-goals

- Rewriting the scheduler, host protocol, sandbox lifecycle, or gateway
  contracts.
- Moving Cloud Agent back into the gateway or into the FreeInference
  repository.
- Renaming databases, tables, OAuth client IDs, JWT audiences, Compose
  projects, volumes, networks, or host directories during the cutover.
- Supporting arbitrary executable distribution plugins.
- Making every web base path a runtime choice; Next.js structurally compiles a
  base path, so v1 publishes the two supported neutral variants in §6.
- Solving cross-host retained-sandbox migration or persistent cold storage.
- Automatically downgrading database migrations during rollback.
- Publishing the repositories or GHCR packages as part of this design's
  implementation. Visibility changes remain an explicit final operation.

## 3. Decision record

### D1 — `hybridInference-cloud-agent` is reusable upstream

It owns application code, database migrations, public service contracts,
generic Dockerfiles, reference deployment examples, CI, and artifact
publication. It does not own a real deployment after cutover.

The repository may describe how an operator supplies configuration. It must
not contain a real operator's domains, machine labels, managed paths, private
network topology, app registrations, monitoring destinations, or deployment
credentials.

### D2 — `freeInference` owns the FreeInference Cloud Agent distribution

The existing private repository already owns FreeInference's gateway
distribution and its `upstream.lock`. It gains Cloud Agent as a second
upstream rather than creating a third product repository.

Its authority includes:

- product branding and public URLs;
- feature and runtime policy;
- GitHub/GitLab application coordinates;
- Compose topology and host paths;
- deployment workflows, runner labels, health checks, backups, and rollback;
- a `cloud-agent.lock` that pins all Cloud Agent artifacts atomically.

Secret **values** remain in GitHub Environments or untracked host environment
files. A private repository is an ownership boundary, not a secret store.

### D3 — upstream merges publish; they never deploy

After cutover, this repository has no `workflow_run` or dispatch path that
targets FreeInference staging or production. Its CI may publish candidates
only after a successful push run on `dev` or a signed release.

Public pull requests:

- run only on GitHub-hosted or equivalently disposable isolated runners;
- receive no environment secrets;
- never publish a trusted tag;
- never reach a deployment network.

A dedicated self-hosted CI runner is permitted only if it is disposable or has
no deployment credentials/network reachability and never executes fork code.
FreeInference deploy runners are never upstream CI runners.

### D4 — one atomic lock is the deployment coordinate

`freeInference/cloud-agent.lock` records:

- the upstream repository and one 40-hex source commit;
- an optional release tag;
- the distribution/config interface versions;
- every required runtime image by immutable digest.

Every image in one lock carries the same
`org.opencontainers.image.revision`. A lock cannot update only the web image
or only the runner: a partial fleet makes the control-plane/host protocol an
unreviewed rolling-compatibility experiment.

The FreeInference distribution commit is the second version coordinate. The
running system therefore has two explicit revisions:

```text
upstream source revision      what code is inside the images
distribution revision         what configuration and deployment policy selected it
```

### D5 — upstream publishes seven neutral artifacts across six runtime roles

The artifact set is:

1. `control-plane` — API, MCP relay, migration, publisher, and reaper roles
   from the existing shared Python image;
2. `web-root` — Next.js web service compiled for an empty base path;
3. `web-agents` — the same source compiled for `/agents`;
4. `host` — runner, workspace broker, and supervisor Python image;
5. `sandbox` — pinned Agent CLI runtime image;
6. `egress-proxy` and `tunnel` — two small infrastructure images.

Egress proxy and tunnel are separate artifacts even though they form one
infrastructure role. The upstream release set therefore contains seven
digests. A distribution selects one web variant: the FreeInference lock pins
six digests including `web-agents`, while the public example selects
`web-root`.

Each package is built once per source SHA, carries OCI source/revision labels,
and is deployed only by digest. Tags are discovery handles, never deployment
coordinates. V1 publishes the platform set already proved by CI; an additional
architecture enters a manifest only after its image and live conformance gates
exist.

### D6 — product identity is runtime state; `basePath` is a build variant

Today most web identity is compiled through `NEXT_PUBLIC_*`. That would force
each distribution and environment to rebuild the web image, defeating D4.

The web server instead reads a bounded, non-secret `AGENT_WEB_*` allowlist at
runtime and passes a `RuntimeSiteConfig` value into the existing
`SiteConfigProvider` during server rendering. The initial HTML and metadata
therefore carry the correct product identity without a client-side flash and
without fetching a gateway-private endpoint.

The server layout uses runtime rendering (for example, `generateMetadata`
plus a no-store/dynamic boundary) so Next.js cannot freeze the host
environment during `next build`. One image started twice with different
allowed runtime values must render two different titles/config objects while
reporting the same image digest.

Runtime fields include:

- app and organization display names;
- organization, status, and gateway links;
- public feature flags;
- environment and deployed source revision.

Browser-visible API paths are derived from the structural variant and
same-origin routing where possible. No secret may enter this object.

Next.js `basePath` remains build-time. The supported v1 variants are exactly
root and `/agents`; arbitrary prefixes are not silently accepted. Both
variants come from the same commit and are neutral artifacts.

### D7 — the distribution manifest renders configuration; app code does not load it

FreeInference adds
`distributions/freeinference/cloud-agent/distribution.yaml`. Its validator
turns reviewed values into Compose/runtime environment. Cloud Agent continues
to read its existing environment interface.

This rejects two sources of split truth:

- no `DISTRIBUTION_CONFIG_PATH` lookup inside Cloud Agent alongside existing
  Pydantic/environment settings;
- no Python hooks imported from a distribution checkout.

The manifest's `schema_version` is paired with the interface version in
`cloud-agent.lock`. The FreeInference deploy refuses a newer manifest/image
combination it does not understand.

### D8 — compatibility identifiers migrate separately from branding

The following are not bulk-renamed during cutover:

- `cloud-agent` OAuth client/audience identifiers;
- existing environment variable names;
- database/table/column names;
- Compose projects, networks, volumes, host state roots, and enrolled-host
  identity;
- persisted runtime provider IDs in native Agent CLI sessions;
- image labels read by already-deployed host code.

For the product-named harness integration, upstream first adds neutral
canonical names while retaining compatibility aliases:

- new wrappers and provider defaults use `hybridinference`;
- FreeInference explicitly selects provider ID `freeinference` until its
  retained sessions no longer require that id;
- drivers detect the neutral binary first and the legacy binary second while
  old retained containers exist;
- image builders write new `org.hybridinference.cloud-agent.*` labels and
  readers fall back to `org.freeinference.*` for one compatibility release.

Removal requires an inventory proving no live retained container or native
session depends on the old identifier. A public repository containing a
documented compatibility alias is acceptable; silently breaking live sessions
to remove a word is not.

### D9 — production Compose and CD live only in FreeInference

Upstream Dockerfiles remain the build interface. Its Compose files become
reference/example topology only.

FreeInference owns production Compose and deployment scripts. They:

- reference lock-resolved images and contain no `build:` directives;
- preserve the current Compose project, network, volume, and host-state names
  at first cutover;
- validate image package, digest, revision, and host architecture;
- run migrations before replacing the API;
- replace runners with the existing drain/grace semantics;
- run health and real contract smoke gates;
- restore the previous distribution revision and image set on a failed gate.

### D10 — no parallel control planes against the live database

The control plane runs publishers, reapers, schedulers, and token/state
transitions. A “shadow” control plane pointed at the same database is not a
read-only comparison and is forbidden without a separately designed leader
fence.

Candidate artifacts are exercised against an isolated database first. The
staging cutover is then an in-place, health-gated replacement using the
existing database and identities.

### D11 — database migrations stay upstream; backup and invocation are distribution-owned

Migration source belongs with the code that reads the schema. The private
deploy decides when to run it, takes the required backup, and records the
result.

Image rollback is automatic where safe. Schema downgrade is not: before
production, each lock bump classifies migrations as backward-compatible or
requiring an explicit rollback procedure. A candidate without that answer
cannot be promoted.

### D12 — gateway contracts remain public and distribution-neutral

Identity, grants, and internal lookup schemas remain in `contracts/`, with
neutral titles and example servers. FreeInference owns live endpoint smoke
tests and concrete URLs.

Neither repository shares signing secrets. The distribution supplies only the
Cloud Agent-side credentials and public gateway coordinates already required
by the HTTP contract.

### D13 — source history is public input, not a deployment artifact

Deployment hosts no longer reset a checkout of this repository or build from
it. They manage the FreeInference checkout and pull locked images.

The old `/srv/freeinference-cloud-agent` checkouts remain untouched through a
defined rollback window, then are archived or removed by a separate,
explicitly approved operation. No cutover script runs `git clean`, removes a
volume, or deletes a Docker data root.

### D14 — promotion stays asymmetric

- A green upstream `dev` candidate may cause a **lock PR**, not a deployment.
- A green lock/config merge to FreeInference `dev` deploys staging.
- Production requires the pinned distribution revision on `main` and a
  manual deploy.
- Staging and production select the same upstream image digests. Their
  non-secret runtime configuration and secrets differ.

Automatic lock-PR merge is disabled for the first two successful Cloud Agent
promotion cycles. It may later use the existing FreeInference bump bot's
fail-closed verdict model.

## 4. Repository ownership

### 4.1 Public upstream

The terminal upstream shape is:

```text
hybridInference-cloud-agent/
  cloud_agent/                  application and API
  cloud_agent_host/             host, supervisor, drivers, broker
  contracts/                    gateway-facing HTTP schemas
  migrations/                   agent database migrations
  web/                          neutral web application
  deploy/
    images/ or current Dockerfile locations
    example/                    runnable reference Compose, placeholder values
  docs/                         developer, operator-interface, security docs
  .github/workflows/
    ci.yml
    publish.yml                 artifacts only; no environment deployment
```

It owns stable interfaces:

- environment-variable names and validation;
- control-plane/host protocol and schema versions;
- image roles, ports, health endpoints, labels, and volumes expected by a
  consumer;
- supported web variants;
- an example that uses placeholder domains and disposable local state.

### 4.2 Private FreeInference distribution

The target private-repository layout, including the existing console bridge,
is:

```text
freeInference/
  cloud-agent.lock
  distributions/freeinference/cloud-agent/
    distribution.yaml
    branding.yaml
    runtime-policy.yaml
    deploy/
      common.env
      staging.env
      production.env
    smoke/
      control_plane.py
      sandbox_job.py
  deploy/cloud-agent/
    docker-compose.control-plane.yml
    docker-compose.host.yml
    deploy-control-plane.sh
    deploy-host.sh
  deploy/docker-compose.console-agent-network.yml  existing console bridge
  ops/
    validate_cloud_agent_lock.py
    write_cloud_agent_lock.py
    read_cloud_agent_image_revision.py
  .github/workflows/
    bump-cloud-agent.yml
    deploy-cloud-agent-staging.yml
    deploy-cloud-agent-production.yml
```

Committed `.env` files contain non-secret deployment choices only. Secret
values are supplied through GitHub Environments or existing untracked host
files and are referenced by name.

The existing `deploy/docker-compose.console-agent-network.yml` remains a
FreeInference-owned integration contract rather than moving upstream. It
attaches the gateway console frontend to both its normal network and the
external `cloud-agent` network so the `/agents` proxy can reach the private
Cloud Agent web service.

### 4.3 Host/GitHub-owned state

Neither Git repository owns:

- database passwords or DSNs;
- session, control, dispatcher, enrollment, broker, MCP, or grant-dispatch
  secrets;
- GitHub App/GitLab OAuth private credentials;
- SSH private keys or private `known_hosts` inventories;
- live `.env` files, database contents, Docker data roots, workspaces, or
  retained sandbox state.

The distribution documents each required key and where it is provisioned. It
never records the value.

## 5. Lock and artifact contract

### 5.1 `cloud-agent.lock`

The lock is intentionally separate from the gateway's existing
`upstream.lock`. The two upstreams have different release cadence, artifact
sets, rollback risk, and staging gates; putting them in one document would make
an agent-only bump appear to change the gateway deployment coordinate.

Normative v1 shape:

```yaml
schema_version: 1

upstream:
  repository: HarvardMadSys/hybridInference-cloud-agent
  release: null
  source_commit: 0123456789abcdef0123456789abcdef01234567

interfaces:
  distribution: 1
  control_plane_host: 1
  sandbox_runtime: 1
  gateway_contracts: 1

images:
  control_plane: ghcr.io/harvardmadsys/hybridinference-cloud-agent-control-plane@sha256:<64-hex>
  web:
    variant: agents
    image: ghcr.io/harvardmadsys/hybridinference-cloud-agent-web-agents@sha256:<64-hex>
  host: ghcr.io/harvardmadsys/hybridinference-cloud-agent-host@sha256:<64-hex>
  sandbox: ghcr.io/harvardmadsys/hybridinference-cloud-agent-sandbox@sha256:<64-hex>
  egress_proxy: ghcr.io/harvardmadsys/hybridinference-cloud-agent-egress-proxy@sha256:<64-hex>
  tunnel: ghcr.io/harvardmadsys/hybridinference-cloud-agent-tunnel@sha256:<64-hex>
```

The literal package names are proposed and become fixed only when the first
packages are created. The schema properties are the contract.

The validator rejects:

- an unknown key or schema version;
- a repository other than the exact upstream;
- a non-40-hex source commit;
- a tag, mutable reference, missing digest, or duplicate role;
- a package outside the exact role allowlist;
- a missing host architecture;
- an image whose source/revision label differs from `source_commit`;
- image roles built from different source commits;
- an interface version the selected distribution does not support;
- a web variant other than `agents` for FreeInference.

### 5.2 Artifact publication

For a pull request, CI builds every affected image with `push: false`. For a
push to `dev`, publication waits on the same complete CI Gate and then:

1. builds each supported platform from the checked-out full SHA;
2. stamps:
   - `org.opencontainers.image.source`;
   - `org.opencontainers.image.revision`;
   - `org.opencontainers.image.version`;
   - the declared Cloud Agent interface versions;
3. pushes a full-SHA discovery tag such as `dev-<40-hex>`;
4. stitches a multi-platform index where more than one platform is supported;
5. resolves and reports the immutable digest;
6. emits an SBOM and GitHub artifact attestation when available.

The bump bot trusts neither the tag nor workflow success alone. It resolves the
tag, walks the manifest/config, verifies the revision and package name, and
writes the digest. A re-run may move a tag to new bytes; an existing lock never
moves with it.

Release tags use the same images and add a release discovery tag. They do not
rebuild source already published from the release commit.

### 5.3 Supported platform contract

The target v1 publication supports the following platforms:

| Role | Required v1 platforms |
|---|---|
| control plane | `linux/amd64`, `linux/arm64` |
| web root / web agents | `linux/amd64`, `linux/arm64` |
| host | `linux/amd64` |
| sandbox | `linux/amd64` |
| egress proxy | `linux/amd64` |
| tunnel | `linux/amd64` |

U3 adds any build that the current matrix does not yet exercise. The small
infrastructure images may technically build elsewhere; support means the
complete runtime and smoke suite passed there. A distribution cannot infer
support from an image manifest alone.

### 5.4 Bump decision

`freeInference/.github/workflows/bump-cloud-agent.yml` mirrors the existing
gateway bump policy:

```text
upstream dev HEAD differs from cloud-agent.lock
  -> exact SHA has a completed successful push CI run
  -> all required full-SHA artifact tags exist
  -> every digest and revision label verifies
  -> distribution/interface compatibility verifies
  -> open one lock-only PR
  -> all FreeInference checks must pass
  -> human merge during initial rollout
```

Missing CI or artifacts is “not ready”; the bot exits quietly and retries.
Package, revision, schema, or provenance mismatch is a hard red failure.

The installation token used to read upstream and open the PR is scoped to the
two repositories and has no deployment permission. Registry reads use the
least-privileged token accepted by GHCR. The deployment jobs use neither token.

## 6. Distribution configuration contract

### 6.1 Manifest

Proposed initial FreeInference manifest:

```yaml
schema_version: 1

distribution:
  id: freeinference
  display_name: FreeInference Cloud Agent

gateway:
  public_base_url: https://freeinference.org
  authorize_url: https://freeinference.org/authorize

web:
  variant: agents
  base_path: /agents
  public_base_url: https://freeinference.org/agents
  api_base_url: /agents/api
  organization_name: FreeInference
  organization_url: https://freeinference.org
  status_url: ""

features:
  rag_link: true

runtime:
  model_provider_id: freeinference
  default_execution_profile: untrusted-kata-v1

compatibility:
  accept_legacy_gateway_env: true
  read_legacy_runtime_labels: true
```

Concrete production values above live in the private repository by design.
This file contains no token, key, password, DSN, private hostname, runner
label, or filesystem path.

Host topology is deliberately outside this product manifest. It belongs in
the private Compose/env layer because two environments can share one product
identity while using different machines, ports, state roots, and runner
capacity.

Environment rollout switches such as restartable/shared-live preview flags
belong in the staging/production env layer, not in the product manifest. The
two environments may intentionally advance at different times.

### 6.2 Rendering to the existing application interface

The FreeInference validator/renderer maps manifest values to the existing
environment interface. Representative mappings:

| Manifest value | Runtime interface |
|---|---|
| `gateway.public_base_url` | `GATEWAY_BASE_URL`, `AGENT_WEB_GATEWAY_URL` |
| `gateway.authorize_url` | `GATEWAY_AUTHORIZE_URL` |
| `web.public_base_url` | `AGENT_WEB_BASE_URL` |
| callback derived and validated from public URL | `AGENT_SESSION_REDIRECT_URI` |
| `web.*` identity fields | server-runtime `AGENT_WEB_*` |
| `runtime.model_provider_id` | `AGENT_MODEL_PROVIDER_ID` |
| `runtime.default_execution_profile` | `AGENT_DEFAULT_EXECUTION_PROFILE` |

The renderer checks cross-service equalities before deployment:

- the callback is under the selected public base URL;
- it exactly matches the gateway distribution's allowed redirect;
- the gateway base and authorize origins agree unless explicitly reviewed;
- the selected web variant's compiled base path equals the manifest;
- the console's `/agents` proxy target and named Docker network match the
  Cloud Agent Compose;
- staging values cannot name production secrets, databases, projects, or
  state roots, and vice versa.

Generated environment output contains non-secret values only. Secret variable
names are referenced in Compose with required-value expressions and resolved
from the host environment at invocation.

### 6.3 Runtime web identity

The web image is neutral only when these fields stop being
`NEXT_PUBLIC_*` build arguments:

- app name;
- organization name and URL;
- status and gateway URLs;
- feature flags;
- build/deployment display revision.

`web/src/app/layout.tsx`, the server boundary above
`SiteConfigProvider`, reads a strict runtime allowlist from the web
container environment. It serializes only that public object into the page.
Client code never receives the environment wholesale.

The image keeps only structural build inputs:

- `NEXT_PUBLIC_BASE_PATH=` for `web-root`;
- `NEXT_PUBLIC_BASE_PATH=/agents` for `web-agents`.

The API path is derived from the selected topology or supplied as a validated
same-origin relative path at runtime. Cross-origin cookie deployments remain
possible only through an explicit, separately tested configuration.

### 6.4 Harness provider identity

The words presented to a user and the IDs persisted by an Agent CLI are
different fields:

```text
display name       FreeInference       distribution-owned presentation
provider ID        freeinference       compatibility-selected machine ID
gateway URL        agent-gateway       deployment-owned relay coordinate
grant token        per attempt         control-plane-owned secret capability
```

The neutral wrappers accept a provider ID/display name from runner-generated,
reviewed configuration. The untrusted job cannot select a different gateway
URL or inject a long-lived credential.

FreeInference keeps provider ID `freeinference` during the compatibility
window so native-resume state remains readable. A fresh third-party deployment
gets `hybridinference`. After U2, upstream contains no hard requirement that
the two IDs be equal.

## 7. FreeInference deployment model

### 7.1 Distribution-owned Compose

The private Compose files are copied from the current proven topology once,
then changed from source builds to locked images:

```yaml
services:
  control-plane:
    image: ${CLOUD_AGENT_CONTROL_PLANE_IMAGE:?resolved from cloud-agent.lock}
  web:
    image: ${CLOUD_AGENT_WEB_IMAGE:?resolved from cloud-agent.lock}
  runner:
    image: ${CLOUD_AGENT_HOST_IMAGE:?resolved from cloud-agent.lock}
  workspace-broker:
    image: ${CLOUD_AGENT_HOST_IMAGE:?resolved from cloud-agent.lock}
```

The sandbox image, egress proxy, and tunnel digest are resolved through the
same lock and passed to the host topology. No production Compose service has a
`build:` key. The deploy renders locked digest references into a temporary
environment file after validation; an operator cannot substitute a tag through
the ordinary host `.env`.

The first cutover preserves:

- control-plane and sandbox Compose project names;
- the named `cloud-agent` network consumed by the gateway console proxy;
- Postgres volume ownership;
- host credential, workdir, snapshot, and Docker data roots;
- egress networks and tunnel aliases;
- published loopback ports;
- runner replica count and graceful stop behavior.

Those names may be cleaned up later as an explicit state migration. They are
not branding in this phase.

The existing console overlay,
`deploy/docker-compose.console-agent-network.yml`, remains in the
FreeInference deployment. It declares the gateway frontend on both the
`hybridinference` and external `cloud-agent` networks. The Cloud Agent Compose
owns creation of the latter; the console overlay only consumes it. The
existing gateway-console deploy and restore paths must fail before replacing
the console when `EXPECT_CLOUD_AGENT=true` and
`docker network inspect cloud-agent` fails. After replacement, `/agents` is
part of the console health gate. The Cloud Agent workflow separately verifies
the network before replacing its web service and probes the same route after
replacement. This prevents either independently healthy stack from silently
publishing a broken Agent route.

### 7.2 Staging deployment

A merge affecting `cloud-agent.lock`, its manifest, or Cloud Agent deployment
files on FreeInference `dev` triggers one staging workflow:

1. validate the FreeInference distribution revision;
2. parse and validate `cloud-agent.lock`;
3. resolve the host architecture and verify every selected digest;
4. compare the target interface versions with the manifest;
5. record the current distribution revision, lock, running image IDs, and
   health status;
6. pull all candidate images before changing a running container;
7. run an isolated migration/config preflight;
8. deploy control plane and web, then require health, identity/JWKS, session,
   and gateway-grant smoke checks;
9. verify the external `cloud-agent` network and the gateway console's
   `/agents` route through the existing console overlay;
10. drain/replace the host services without deleting retained sandboxes or
   state roots;
11. run a real no-publish sandbox job through the production-shaped relay
    topology;
12. stamp the successful upstream and distribution revisions.

A docs-only change outside the watched distribution/deploy paths does not
deploy.

### 7.3 Production promotion

Production deploy remains manual:

1. staging must report the exact candidate lock and a successful smoke result;
2. the selected FreeInference commit is promoted to `main`;
3. the production workflow verifies `main` contains that exact lock;
4. operators take the migration-classified database backup;
5. the same digest set is deployed with production runtime config;
6. production health and one bounded smoke request pass before success is
   recorded.

No production workflow accepts a free-form upstream SHA or image argument.
Rollback selects a previously committed FreeInference revision/lock pair.

### 7.4 Deployment record

The distribution records, per environment:

```yaml
distribution_revision: <freeInference SHA>
cloud_agent_source_revision: <upstream SHA>
cloud_agent_lock_digest: <sha256 of normalized cloud-agent.lock>
images:
  control_plane: <running digest>
  web: <running digest>
  host: <running digest>
  sandbox: <selected digest>
  egress_proxy: <running digest>
  tunnel: <running digest>
deployed_at: <UTC timestamp>
```

This replaces the ambiguous use of one upstream `.deployed-sha` as both the
code revision and deployment policy revision.

Upstream revision/interface values live on the immutable image labels.
Distribution revision and lock digest live on Compose container labels.
The host passes both coordinates to every dynamically created sandbox label so
a retained container remains attributable after the fleet advances.

## 8. Failure and rollback behavior

### Before replacement

A missing package, digest, platform, revision label, config field, secret,
network, backup, or gateway preflight fails before any container changes.
Already-running services remain untouched.

### Control-plane failure

If migration or control-plane/web health fails:

- restore the previous Compose input and image digests;
- recreate the preceding control-plane/web set;
- re-run its health checks;
- leave the failed deployment record separate from last-known-good.

The workflow does not claim schema rollback. A migration classified as
non-backward-compatible requires an operator-authored downgrade/restore plan
before promotion.

### Host failure

If host replacement or the real sandbox smoke fails:

- stop new claims on the candidate runners;
- restore the previous host/infrastructure image set;
- preserve host credentials and all Docker/state roots;
- allow existing attempt leases to fence killed work normally;
- report any retained sandbox that no longer answers as a cutover failure.

No automatic branch runs `docker compose down -v`, `docker system prune`,
`git clean`, or deletes a managed path.

### Distribution workflow failure

A bump failure leaves the lock unchanged. A staging failure leaves production
unchanged. A production failure restores the previous lock's image set where
schema compatibility allows and pages an operator with both version
coordinates.

## 9. Current-to-target file map

| Current Cloud Agent path/surface | Terminal owner and treatment |
|---|---|
| `cloud_agent/**`, `cloud_agent_host/**`, `migrations/**` | Upstream; neutral application and schema code |
| `contracts/*.openapi.yaml` | Upstream; neutral title/example servers. FreeInference keeps live agreement/smoke tests |
| `deploy/**/Dockerfile*` | Upstream; reproducible artifact build interface |
| `deploy/control-plane/docker-compose.yml` | Upstream reference example only; FreeInference copies and owns production Compose |
| `deploy/host/docker-compose.sandbox.yml` | Upstream reference topology only; FreeInference owns its real host topology |
| `deploy/control-plane/deploy.sh`, `deploy/host/deploy.sh` | Generic helpers may remain for examples; source-reset production behavior moves to digest deployment in FreeInference |
| `.github/workflows/deploy-staging.yml` | Removed from upstream after handoff; replaced by FreeInference lock-based staging CD |
| `.github/workflows/deploy-prod.yml` | Removed from upstream after production handoff; replaced by FreeInference manual CD |
| FreeInference self-hosted runner labels | FreeInference workflows only |
| `web/src/config/env.ts` product identity | Upstream runtime server config; neutral defaults |
| `Dockerfile.web` product build args | Only structural base-path build input remains |
| `pi-freeinference`, `opencode-freeinference`, `kilo-freeinference` | Neutral canonical wrappers upstream plus time-bounded compatibility aliases |
| `freeinference/<model>` provider IDs | Distribution-selected compatibility value; not global search/replace |
| `org.freeinference.runtime.*` labels | Dual-read/write compatibility, then neutral namespace after live inventory clears |
| `FREEINFERENCE_BASE_URL` | Deprecated input alias with warning; canonical interface remains `AGENT_GATEWAY_URL` |
| FreeInference Compose projects, paths, image tags, networks, and state roots | Private distribution; preserve at first cutover |
| `freeInference/deploy/docker-compose.console-agent-network.yml` | Remains private; preserves the gateway frontend's external `cloud-agent` network attachment and `/agents` availability contract |
| `README.md`, `AGENTS.md`, `ENVIRONMENT.md` | Upstream-neutral current documentation |
| `MIGRATION.md`, `docs/cutover.md`, historical design records | May retain historically accurate FreeInference references, clearly marked historical and scrubbed of private values |
| Live domains, OAuth/GitHub App coordinates, alert destinations, backup policy | Private distribution or environment |

A “no FreeInference string” test over the entire upstream tree is therefore
incorrect. The enforceable policy is:

1. no current FreeInference deployment coordinate outside an explicit
   compatibility/historical allowlist;
2. no FreeInference product default in executable code;
3. every allowlisted compatibility identifier has an owner, removal condition,
   and regression test.

## 10. Cutover plan

The handoff is deliberately staged. No phase removes the preceding deployment
path before the replacement has deployed and rolled back successfully.

### Phase 0 — freeze and inventory

Before merging runtime/publication changes:

1. Record the deployed control-plane and host source SHA for staging and
   production.
2. Record running container image IDs, Compose projects, service names,
   networks, volumes, published ports, Docker data root, and all persistent
   state paths.
3. Record a **name-only** inventory of environment and GitHub
   Environment/Repository variables; never export secret values into an
   artifact.
4. Verify control-plane database backup and restore instructions.
5. Count retained/shared sandboxes and record which legacy wrapper/provider
   IDs and image labels they contain.
6. Run current gateway preflight and one no-publish agent smoke.
7. Save the existing source-deploy workflow run URLs as the rollback
   baseline.

During Phases 0–4, open publication PRs are not batch-merged. One merge, one
observed staging result.

### Phase 1 — add a legacy deployment kill switch

Before the new owner is ready, the current staging job gains a repository
variable gate:

```text
LEGACY_DIRECT_STAGING_DEPLOY=true   current behavior
LEGACY_DIRECT_STAGING_DEPLOY=false  jobs skip; no host is touched
```

The variable is set to `true` before the guard lands, so the guard commit's
own green CI still exercises the current pipeline. The gate is checked on both
control-plane and host jobs and covered by a workflow-policy test.

Production's old workflow gains a corresponding fail-closed
`LEGACY_DIRECT_PRODUCTION_DEPLOY` gate. It remains `true` until the private
production workflow has passed a dry run, then is set false before handoff.

This switch is a migration fuse, not the terminal architecture. Both workflows
are deleted from upstream after cutover.

### Phase 2 — make upstream artifacts neutral and publishable

Land, with the legacy staging gate still true:

1. runtime web identity and the two structural web variants;
2. neutral wrapper/label support plus compatibility reads;
3. OCI labels and interface-version constants;
4. full image CI on pull requests;
5. digest publication on a green `dev` push;
6. policy tests proving no publish or deployment path is reachable from a pull
   request.

The resulting merge performs one final source-owned staging deployment. That
is useful: staging proves the same code still works before deployment ownership
changes. Phase 0 must first prove that the current environment explicitly
supplies FreeInference branding; if it does not, provision that value before
the neutral default lands.

### Phase 3 — land the disabled FreeInference consumer

In `freeInference`, add:

- `cloud-agent.lock` pinned to the Phase 2 candidate;
- the manifest, validator, renderer, and tests;
- digest-only control-plane and host Compose;
- bump workflow with auto-merge disabled;
- staging/production workflows initially restricted to manual dry-run;
- deployment-record and rollback tooling.

CI renders `docker compose config` for both environments, verifies no
`build:`, checks all cross-service coordinates, and mutation-tests the lock
validator. Nothing deploys when this phase merges.

### Phase 4 — staging ownership handoff

1. Use the private workflow to validate/pull the lock without replacement.
2. Exercise all images against an isolated database and local contract
   services.
3. Run the private in-place staging deployment using existing project,
   network, volume, and state names.
4. Verify login, JWKS, grant mint/revoke, MCP, source-control connection,
   host registration, job claim, one no-publish turn, terminal/files/Git, and
   retained chat resume.
5. Roll back once to the previous source-owned deployment, repeat the smoke,
   then deploy the locked candidate again. A rollback path not rehearsed is
   not a cutover path.
6. Set `LEGACY_DIRECT_STAGING_DEPLOY=false`.
7. Trigger a harmless upstream CI push and prove that no staging deployment is
   created.
8. Enable automatic staging deployment for the relevant FreeInference paths.

There is never a second live control plane on the staging database.

### Phase 5 — production ownership handoff

1. Keep the exact upstream digest set already exercised on staging.
2. Promote the FreeInference distribution revision to `main`.
3. Run the private production workflow in validate-only mode.
4. Take the classified database backup.
5. Set `LEGACY_DIRECT_PRODUCTION_DEPLOY=false`.
6. Deploy from FreeInference and complete production smoke gates.
7. Keep the old upstream workflow disabled and the old managed checkouts
   intact for the rollback window.
8. Re-enable the old workflow only if the private deploy cannot restore
   service and an operator has explicitly chosen that rollback.

After two successful private production deployments and the agreed rollback
window, delete the old workflows in upstream and retire the source checkouts
through a separate operational change.

### Phase 6 — publication cleanup

Only after both deployment environments are distribution-owned:

- merge/rebase the neutral naming and repository-reference changes;
- replace current product coordinates in public docs and examples;
- retain only documented compatibility/historical references;
- run gitleaks across the complete retained history;
- verify GitHub App/OAuth callbacks and repository access;
- configure branch protection, Actions permissions, GHCR access, security
  reporting, and the public issue/PR templates;
- switch repository and package visibility as separate, auditable actions.

Opening the repository is not a deployment and must not be coupled to one.

## 11. Disposition of current pre-publication PRs

The four open PRs are inputs to this design, not the distribution boundary by
themselves.

| PR | Disposition |
|---|---|
| #98 neutral defaults | Rebase after FreeInference runtime identity is explicit. Its API titles and display defaults are safe; compatibility IDs remain governed by D8 |
| #99 public-safe CI | Land before any external contributor can open a PR. Its fork runner guard is required defense-in-depth, but direct deployment still moves out under D3 |
| #100 SECURITY/CONTRIBUTING | Recheck after private vulnerability reporting and the final contribution flow are configured; prose must describe the real channel |
| #101 repository rename | Rebase after distribution/compatibility work. Repository guards and package metadata change; persisted provider IDs, labels, paths, and projects do not ride along |

Until the Phase 1 fuse exists, merging any of these to `dev` causes a current
staging deployment after green CI, including the documentation-only PR.
Production remains manual.

This design-document PR itself changes no runtime on its branch. Merging it
before Phase 1 likewise causes one no-code staging deployment under the current
workflow and should be observed like any other merge.

## 12. Security invariants

The split is incomplete unless all of these are mechanically enforced:

1. Upstream pull-request jobs use no FreeInference self-hosted runner or
   deployment environment.
2. Upstream workflows contain no FreeInference deployment token, runner label,
   managed host path, or production/staging dispatch.
3. Upstream publication uses only the repository token with package-write
   permission and only for a trusted push/release.
4. A distribution deploy accepts only digest references from the committed
   lock and exact allowlisted packages.
5. A tag cannot override a committed digest.
6. Every running image's revision label matches the one source commit in the
   lock.
7. Secret values are absent from source, generated config artifacts, workflow
   logs, deployment records, and image layers.
8. The sandbox still receives only short-lived per-attempt grants and reaches
   only the reviewed gateway/MCP relay destinations for its current egress
   phase.
9. The distribution cannot widen sandbox egress by changing a presentation
   field.
10. The gateway's signing and API-key secrets never enter either Cloud Agent
    repository, image, or host.
11. Host state roots and retained Docker data are never mounted into an
    untrusted job beyond the existing sandbox boundary.
12. A missing config, secret, platform, image, contract, or health verdict
    fails closed before promotion.

The public history scan covers all refs intended for publication, not only the
working tree. A removed credential remains published if an old retained commit
still contains it.

## 13. Verification and acceptance gates

### 13.1 Upstream gates

- Unit tests for runtime site-config allowlisting and server rendering.
- A two-container test proving one web digest renders two runtime identities
  without rebuilding.
- Web builds and tests for both root and `/agents` variants.
- Contract tests proving browser API/sign-in paths remain correct under both.
- Wrapper tests for neutral defaults, FreeInference compatibility selection,
  missing-variable refusal, and credential non-disclosure.
- Host tests reading new image labels and the allowed legacy fallback.
- OCI build tests for every role/platform.
- Image vulnerability and SBOM gates, plus an explicit redistribution-license
  decision for every bundled Agent CLI before its sandbox package is public.
- Workflow policy tests that mutation-test:
  - a pull request routed to self-hosted;
  - a pull request with package-write permission;
  - an upstream job naming a FreeInference deploy environment;
  - a tag-only deployment reference.
- Full history gitleaks before visibility change.

### 13.2 FreeInference distribution gates

- Strict manifest and lock schemas with unknown-key refusal.
- Mutation cases for missing roles, mixed SHAs, wrong packages, mutable tags,
  unsupported platforms, and incompatible interfaces.
- `docker compose config` for staging and production with no `build:`.
- Tests pinning every preserved project/network/volume/state identity.
- Cross-repository URL, callback, network, and web-variant agreement.
- Deploy and restore tests proving `EXPECT_CLOUD_AGENT=true` fails closed when
  the external `cloud-agent` network is absent, and proving `/agents` is in the
  post-replacement health gate.
- Bump-decision tests for no CI, red CI, missing image, mismatched revision,
  current lock, and a complete candidate.
- Deploy dry-run tests proving all pulls/validation happen before replacement.
- Rollback fixture using two committed lock/distribution pairs.
- Secret-shape scanning of source and generated non-secret env.

### 13.3 Staging acceptance

The cutover is accepted only when all hold:

- the private deployment record matches the running image digests;
- the FreeInference title, links, and `/agents` base path are unchanged for
  users;
- the gateway frontend is attached to the external `cloud-agent` network and
  `/agents` reaches the locked Cloud Agent web digest;
- login/callback/session renewal works;
- identity and grants pass the live contract smoke;
- the control plane sees the expected hosts and capacity;
- a new job runs under the expected isolation and egress policy;
- an existing retained chat resumes with its native state;
- workspace terminal, files, Git status, and publish/no-publish controls work;
- a host replacement does not delete retained sandboxes;
- old-provider/label compatibility is observed where expected;
- one rollback and redeploy both pass;
- a subsequent upstream merge produces artifacts but no deployment.

### 13.4 Production acceptance

- Production uses exactly the upstream digest set accepted on staging.
- Distribution runtime values are production values and no staging coordinate
  appears in rendered Compose.
- Backup and migration classification are recorded.
- Login, one bounded inference-grant call, one no-publish agent job, and
  retained-session access pass.
- The old upstream production workflow cannot run while the new one is
  authoritative.
- The last-known-good lock/distribution pair is immediately selectable.

## 14. Implementation slices and dependency graph

One slice is one reviewable PR in the named repository:

| ID | Repository | Change | Depends on |
|---|---|---|---|
| U0 | Cloud Agent | This design | — |
| U1 | Cloud Agent | Legacy deploy fuses + public workflow policy (#99 rebased as needed) | U0 |
| U2 | Cloud Agent | Runtime web identity, two web variants, neutral wrapper/label compatibility | U0 |
| U3 | Cloud Agent | OCI labels, multi-platform build/publish, artifact policy tests | U1, U2 |
| F1 | FreeInference | `cloud-agent.lock` schema, validator, renderer, manifest skeleton | U0 |
| F2 | FreeInference | Digest-only Compose and deployment-record/rollback tooling, disabled | F1, U3 |
| F3 | FreeInference | Bump bot opening lock-only PRs, auto-merge disabled | F1, U3 |
| F4 | FreeInference | Staging handoff and smoke/rollback evidence | F2, F3 |
| U4 | Cloud Agent | Disable/remove direct staging CD; leave production manual and gated | F4 |
| F5 | FreeInference | Production handoff and old production gate disable | U4 |
| U5 | Cloud Agent | Remove remaining production CD; final neutral/publication cleanup (#98/#100/#101 rebased) | F5 |
| F6 | FreeInference | Optional lock-bump auto-merge after two stable cycles | F5 |

```text
U0 -> U1 -> U3 -------┐
  \-> U2 -/           |
  \-> F1 -> F2 -------+-> F4 -> U4 -> F5 -> U5
          \-> F3 -----/                 \-> F6
```

U3 must not wait on F1 to publish candidates; F1 needs a real candidate to
test. F2/F3 remain deployment-disabled until both sides exist. U4 cannot land
before F4 because that would leave staging with no proven owner.

## 15. Rejected alternatives

### Keep direct deployment and rely on PR runner guards

Rejected. The upstream repository would still own private host authority, and
a trusted-branch mistake or compromised maintainer token would still deploy.
It also makes every upstream change a staging change.

### Put `distributions/freeinference` inside the public Cloud Agent repository

Rejected. Moving secret values elsewhere would not move product topology,
deployment history, or release authority. It would repeat the boundary that
HybridInference has just removed.

### Let FreeInference build from an upstream source checkout

Rejected. The bytes deployed are then a function of registry state, builder
state, architecture, and time rather than one reviewed artifact. Staging and
production can rebuild the same SHA differently, and rollback requires
rebuilding history.

### Publish one web image and rebuild it per distribution

Rejected. Product identity would remain a build input and the distribution
would no longer consume the upstream artifact it tested. Runtime identity plus
two structural base-path variants is the smaller honest contract.

### Load executable distribution plugins in Cloud Agent

Rejected. A deployment gains application-code execution, startup ordering, and
versioning inside the service instead of supplying configuration at its
boundary. Existing typed environment settings are sufficient.

### Rename every `freeinference` identifier before publication

Rejected. Provider IDs and image labels are persisted in retained/native
session state; Compose names own live resources. Their spelling is less
important than continuity. D8 removes product defaults without pretending
machine identifiers are branding copy.

### Run old and new control planes side by side on one database

Rejected. Schedulers, reapers, publishers, and token transitions make the
second process active even if nobody sends it HTTP traffic. Isolated preflight
plus one health-gated in-place cutover is the safe comparison.

## 16. Review decisions

The design recommends defaults below; reviewers must make them explicit before
U3/F1 implementation:

| Decision | Recommended default |
|---|---|
| Cloud Agent GHCR package names | The exact role names in §5.1 |
| Package visibility during migration | Private with FreeInference Actions access; switch visibility separately with the repository |
| Bundled CLI redistribution | Require legal/license approval before a public sandbox package; otherwise keep that package private and document local builds |
| Web variants | Publish root and `/agents`; FreeInference pins only `/agents` |
| Initial host/sandbox architecture | `linux/amd64` only until full runtime conformance exists elsewhere |
| Lock bump auto-merge | Off for two successful staging/production cycles |
| Legacy provider/label removal | Usage inventory at zero plus one full rollback window |
| Old source checkout retirement | After two successful private production deployments and the approved rollback window |
| Historical FreeInference references | Retain when accurate and safe; distinguish them from current instructions |

Approval of this design authorizes implementation PRs, not a deployment,
merge, repository visibility change, package visibility change, host deletion,
or compatibility-identifier removal. Each of those keeps its own review and
operational gate.
