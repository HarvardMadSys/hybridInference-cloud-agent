# Releases

A Cloud Agent release consists of `release.json` and `recipe.tar.gz`. The
manifest identifies one source commit, every owned image by registry digest,
each image's platforms, the sandbox runtime catalog, the database revision,
the deployment protocol, and recorded compatibility checks. The recipe contains
scripts and configuration templates for installing those images without a
source checkout.

A downstream distribution pins the SHA-256 of the manifest's exact bytes in its
own reviewed lock. The manifest, in turn, pins the recipe archive and images.

| File | Purpose |
|---|---|
| [manifest.schema.json](manifest.schema.json) | Published manifest schema; consumers validate against it. |
| [release_manifest.py](release_manifest.py) | Generates a complete manifest from build records. |
| [build_recipe.py](build_recipe.py) | Packages the explicit recipe file list into a deterministic archive. |
| [release_env.py](release_env.py), [artifact_compose.py](artifact_compose.py) | Validate release configuration and render digest-bound deployment inputs. |

## What the generator checks, and what it deliberately does not

The generator requires all six artifacts, with distinct digests and exactly
these platforms:

| Role | Variant | Platform |
|---|---|---|
| `control` | — | `linux/arm64` |
| `web` | `root`, base path `""` | `linux/arm64` |
| `web` | `agents`, base path `/agents` | `linux/arm64` |
| `runner` | — | `linux/amd64` |
| `sandbox` | — | `linux/amd64` |
| `tunnel` | — | `linux/amd64` |

Image references must use digests under this organization's registry. The
schema enforces role and web-variant cardinality, and the generator validates
its output against that schema before writing it. The generator also checks
that each runtime's `default` appears in its `versions`; this relationship
cannot be expressed by the published JSON Schema, so consumers must check it
separately.

Version fields come from their sources: `deploy_protocol` from
`deploy/DEPLOY_PROTOCOL`, `database_revision` from Alembic's script directory,
and `runtime_catalog` from the sandbox image's labels. Runtime versions should
not be supplied as a second hand-maintained list.

The generator does not query the registry. A downstream resolver must verify
that each digest exists, carries the declared platforms, and has the expected
`org.opencontainers.image.revision` and `io.hybridinference.artifact.role`
labels on each platform image. Manifest validation alone does not prove those
claims.

## Producing one

[The publication workflow](../../.github/workflows/publish.yml) handles pushes
to `dev`, successful CI completion, and manual dispatch for a `dev` commit with
successful CI. A push can build alongside CI, but publication waits for the
newest push CI run on that exact commit to succeed. A successful CI rerun can
trigger publication later.

The workflow:

1. Verifies that the commit is on `dev` and leaves an existing release unchanged.
2. Builds and pushes each artifact by digest with `SOURCE_COMMIT` set to the
   full commit SHA. It pulls the digest back, smoke-tests the pulled image, and
   records its digest, platform, and labels.
3. Assembles the six cell records, builds the recipe, and writes the manifest.
4. Runs control-plane integration acceptance on those exact candidate files.
5. Waits for CI success, creates a draft release tagged `agent-<full-sha>`,
   uploads both assets, then makes the release visible. A failed upload deletes
   the draft.

Failed or cancelled CI leaves any built images in the registry without a
published manifest. The release workflow has no pull-request trigger and runs
merged code. It publishes artifacts; it does not deploy a named environment.

For a manual generation run from the repository root, collect the six
publication cell JSON files in `cells/`. Set `SOURCE_COMMIT` to their full
40-character SHA and `PUBLISHER_RUN_URL` to the actual publisher's Actions run
URL, then use the same generation commands as the workflow:

```bash
mkdir -p release
uv run python -m deploy.release.publish_inputs assemble \
  --cells cells \
  --source-commit "${SOURCE_COMMIT:?Set the build commit}" \
  --output release/inputs.json
uv run python deploy/release/build_recipe.py \
  --output release/recipe.tar.gz --report
RECIPE_SHA256=$(sha256sum release/recipe.tar.gz | cut -d' ' -f1)
uv run python -m deploy.release.release_manifest \
  --inputs release/inputs.json \
  --recipe-url "https://github.com/HarvardMadSys/hybridInference-cloud-agent/releases/download/agent-${SOURCE_COMMIT}/recipe.tar.gz" \
  --recipe-sha256 "${RECIPE_SHA256}" \
  --publisher-run-url "${PUBLISHER_RUN_URL:?Set the publisher Actions run URL}" \
  --output release/release.json
```

`inputs.json` contains `source_commit`, `artifacts`, and `runtime_labels`.
`runtime_labels` is the sandbox's label map from `docker image inspect`, matching
what the host reads when stamping attempts. The manifest is written atomically
only after all checks pass.

## Starting, not just building

[smoke.py](smoke.py) starts each artifact and checks its identity labels. CI and
publication use the same implementation; publication applies it to images pulled
back from the registry.

| Artifact | Smoke-test evidence |
|---|---|
| Control | Migrates real Postgres with `alembic upgrade head`, then answers `/healthz`. |
| Web, both variants | Serves `/login` under the selected mount prefix and renders runtime site identity. |
| Runner | Imports its modules and provides the Docker CLI and `nft`. |
| Sandbox | Each Agent CLI reports the version declared in its image labels. |
| Tunnel | Runs its entrypoint and fails clearly for an unreachable peer. |

The tests also check `org.opencontainers.image.revision`,
`io.hybridinference.artifact.role`, and the web variant's
`io.hybridinference.artifact.base_path`. Dockerfiles default the revision to
`unknown`; publication must supply the source commit. These checks establish
startup behavior, not successful job execution, real Gateway integration, or
Kata readiness on an execution host.

## Control-plane integration acceptance

[acceptance.py](acceptance.py) installs the candidate from an extracted recipe
in a directory without application source or Dockerfiles. It runs the real
control-plane deployer in artifact mode, pulls the candidate digests, and
compares running containers with the manifest through
[accept_artifacts.py](accept_artifacts.py).

| Acceptance establishes | Remaining environment acceptance |
|---|---|
| Checkout-free installation with no image build | Execution host, Kata, and real jobs |
| Registry pulls and digest identity of running containers | Any additional architecture |
| Real Postgres migrations and identity-namespace check | Gateway contracts and browser sign-in |
| Healthy API and MCP listeners; correct web variant labels | User-visible console behavior |
| Refusal of conflicting configured images before pulling | Independent registry validation by the downstream resolver |

The exact-commit host readiness fence is expected to time out: this acceptance
has no execution host. The test asserts that the deploy reached the fence after
all earlier stages passed. It does not bypass or weaken that check.

Because normal deployment starts `web` after the fence, acceptance starts it
explicitly using the generated Compose file with `--no-build --pull never`.
Container comparisons use registry `RepoDigests`, rather than image IDs, and
role labels, rather than registry paths. The workflow publishes the same
manifest and recipe bytes that passed this acceptance.

## Registry layout is not part of this contract

The image's role label defines its role. Repository names and human-readable
tags are storage conventions; deployments select the manifest's digests.

| Package | Contents |
|---|---|
| `ghcr.io/harvardmadsys/cloud-agent` | Control, both web variants, runner, and tunnel |
| `ghcr.io/harvardmadsys/cloud-agent-sandbox` | Sandbox, with a separate visibility setting for bundled third-party CLIs |

After its smoke test passes, each digest receives a tag such as
`control-<sha>`, `web-agents-<sha>`, or `sandbox-<sha>`. These tags help people
inspect images and are not deployment selectors.

When changing registry layout, verify whether the copy preserves the original
index digest. Recreating an index or converting its format can change that
digest and requires a reviewed lock update. Keep old addresses available until
the rollback window closes.

## Where this differs from the design document

The [original release design](../../docs/specs/2026-09-10-cloud-agent-release-fi-deploy-design.zh.md)
is historical. The implemented differences are:

- **Deployment protocol v4.** `shared-live-exact-sha-v4` replaced the design's
  v2. v3 introduced the schema baseline rewrite; v4 introduced the
  [single sandbox network](../../docs/specs/2026-09-23-agent-public-egress-design.zh-CN.md).
  It retired the Squid `egress` artifact and requires
  `AGENT_EGRESS_CHAT_NETWORK_POOL` and `AGENT_EGRESS_DNS_SERVER` on every runner
  host. v3 deployers cannot install v4 releases, and rollback does not cross
  this protocol boundary.
- **Six images from five Dockerfiles.** The web Dockerfile builds twice because
  the mount prefix is compiled into the bundle. The retired egress image
  accounts for the original design's seventh artifact.
- **Existing runtime-label namespace.** Runtime labels remain
  `org.freeinference.runtime.*`. Renaming them requires a separate dual-read
  migration.

## Deploying a recipe

Verify the manifest against the downstream lock, verify the recipe's SHA-256
against the manifest, and resolve and verify its image digests before replacing
services. Extract `recipe.tar.gz` into a release directory; it creates a
`recipe/` directory containing deployment helpers, examples, and Compose
templates without `build:` entries.

Both machines need Bash, Docker with Compose v2, and Python 3.12 or newer for
recipe helpers. The execution host additionally needs its existing Kata
provisioning; deployment does not install a sandbox runtime. Install the helper
dependencies on each machine:

```bash
python3.12 -m venv /opt/cloud-agent/deployer
/opt/cloud-agent/deployer/bin/python -m pip install \
  -r /opt/cloud-agent/release/recipe/deploy/release/requirements.txt
```

Keep `DEPLOY_ROOT` stable across releases. Its `.env`, `.deployed-sha`, and
`.shared-live-ready` describe the persistent deployment, while each extracted
recipe can live elsewhere. For a new installation, create that root and fill
its `.env` using the appropriate recipe's control-plane or host `.env.example`.
For an existing installation, retain its configuration, Compose project name,
host enrollment root, and Docker data root. Generated Compose files stay under
`DEPLOY_ROOT` so relative mounts preserve their data paths; the edge proxy
configuration comes from the selected recipe.

Select `AGENT_WEB_VARIANT=root` or `agents` in the control-plane configuration.
If omitted, `AGENT_WEB_BASE_PATH` selects the variant: empty or unset selects
`root`, and `/agents` selects `agents`.

Set the artifact environment on the control-plane machine:

```bash
export AGENT_DEPLOY_MODE=artifacts
export AGENT_RELEASE_MANIFEST=/opt/cloud-agent/release/release.json
export AGENT_DEPLOY_PYTHON=/opt/cloud-agent/deployer/bin/python
export DEPLOY_ROOT=/srv/cloud-agent-control
```

Set the same release and interpreter on the execution host, with its own root:

```bash
export AGENT_DEPLOY_MODE=artifacts
export AGENT_RELEASE_MANIFEST=/opt/cloud-agent/release/release.json
export AGENT_DEPLOY_PYTHON=/opt/cloud-agent/deployer/bin/python
export DEPLOY_ROOT=/srv/cloud-agent-host
```

The manifest supplies the commit and owned image references. Remove old image
and commit overrides when moving to artifact mode; any conflicting effective
Compose values are refused before pulling or replacing services. The default
`source` mode remains available for checkout-based deployment.

### Deploying the two in the order the design specifies

Use phases to coordinate the machines. With the environment above set in each
machine's shell, run these commands in order:

1. Prepare both sides; these commands may run in parallel on their respective
   machines and replace no serving services.

   ```bash
   # Control-plane machine
   bash /opt/cloud-agent/release/recipe/deploy/control-plane/deploy.sh --phase prepare
   # Execution host
   bash /opt/cloud-agent/release/recipe/deploy/host/deploy.sh --phase prepare
   ```

2. Prepare the database on the control-plane machine. This migrates and checks
   the identity namespace. On a cold machine it also starts the API and MCP
   listener needed by the host readiness probes; existing listeners stay in place.

   ```bash
   bash /opt/cloud-agent/release/recipe/deploy/control-plane/deploy.sh --phase prepare-database
   ```

3. Activate the execution host. It drains and replaces runners, checks the
   replacement, records the deployment, and publishes its generation.

   ```bash
   bash /opt/cloud-agent/release/recipe/deploy/host/deploy.sh --phase activate
   ```

4. Activate the control plane. The readiness fence must pass before public
   services switch.

   ```bash
   bash /opt/cloud-agent/release/recipe/deploy/control-plane/deploy.sh --phase activate
   ```

Omitting `--phase` runs all phases for that side. Use the phased sequence for a
coordinated rollout; two whole-script invocations cannot express the ordering
above. Phases can be retried. Database preparation retains ownership of any
listeners it started, and activation restarts those stopped during a failed
attempt so they can accept the heartbeat the readiness fence needs.

If the other machine fails and activation is abandoned, clean up listeners
started solely for this cold deployment on the control-plane machine:

```bash
bash /opt/cloud-agent/release/recipe/deploy/control-plane/deploy.sh --phase abandon
```

This leaves previously serving listeners alone. Image pulls and host Kata checks
precede runner drain. Migrations, identity-namespace verification, exact-commit
readiness, reverse-broker probes, and deferred host readiness publication keep
their deployment ordering. A failed readiness check prevents activation of the
replacement control-plane stack.

Script tests run extracted recipes with Docker calls recorded and failures
injected, and use the real Compose environment parser when available. They do
not establish readiness of a particular Linux host, live Kata runtime, or
registry release; verify those during environment cutover.

## What a downstream deployment still owns

The upstream publication workflow does not deploy a named environment. A
consuming distribution must:

- pin the exact manifest bytes in a reviewed lock and verify the schema,
  recipe hash, registry digests, image labels, and platforms;
- check protocol compatibility and the manifest's `verified_upgrade_from` and
  `verified_rollback_to` records; an empty list means that path was not verified;
- run preparation, Kata checks, and environment acceptance on the actual hosts;
- keep environment configuration, deployment records, and recovery material
  outside this upstream repository.
