# Releases

One Cloud Agent version is one document. This directory produces it, and the
document is the whole contract between this repository and whoever deploys it:
a distribution pins the sha256 of the manifest's bytes and nothing else, so
everything a deployment needs to know about a version is in there — the commit,
every artifact by digest, the platforms each was built on, the Agent CLIs the
sandbox contains, the deployment recipe, and what the release has actually been
exercised against.

| File | What it is |
|---|---|
| `manifest.schema.json` | The published contract. A consumer validates a received manifest against it before believing anything in it. |
| `release_manifest.py` | The generator. Reads what a build produced and refuses to write a document that describes an incomplete release. |

## What the generator checks, and what it deliberately does not

It checks the shape of a release: every role present exactly once, web present
once per mount shape, no digest standing in for two artifacts, each artifact on
exactly the platforms this release builds it for, image references that are
digests under this organization's registry. A missing artifact is not a smaller
release — it is one that would leave a machine half updated — and one image
answering for two roles is a build matrix that quietly stopped building
something.

That shape is stated twice — here and in `manifest.schema.json` — and the
generator validates its own output against the schema before writing anything,
so the two cannot drift apart quietly. The schema carries the cardinality per
role and per web variant (`contains` with `minContains`/`maxContains`) rather
than counting entries, because a release that lost its sandbox and gained a
second control plane has exactly the right number of artifacts.

It does **not** verify the images themselves. That a digest exists, that its
index really carries the declared platforms, that each platform image's
`org.opencontainers.image.revision` equals `source_commit`, and that its
`io.hybridinference.artifact.role` label matches the artifact it is standing in
for are facts about the registry, checked where the release is *consumed*: the
distribution's resolver treats this document as a claim and goes and looks.
That split is the point of the file existing where the build does.

Two invariants worth naming because they cannot be enforced in the same place:

* **Membership.** `runtime_catalog.<runtime>.default` must appear in that
  runtime's `versions`. JSON Schema cannot express "this string is one of that
  array", so the generator refuses it and the resolver has to check it again —
  it is the one shape check the published schema does not carry.
* **Provenance of versions.** `deploy_protocol` is read from
  `deploy/DEPLOY_PROTOCOL`, `database_revision` from Alembic's script directory,
  and the runtime catalog from the sandbox image's own labels. None is typed
  here: a typed version is one that can disagree with the artifact it describes.

## Producing one

`.github/workflows/publish.yml` produces a release for every CI run that
concludes green on a push to `dev`, and for a `workflow_dispatch` naming a dev
commit whose own CI is green. In order:

1. **Vet the commit.** It has to be on `dev`, its own CI has to have concluded
   success on a push, and no release may exist for it yet. A release that
   exists is left exactly as it is: a manifest that could be overwritten is
   one whose sha256 nobody can pin.
2. **Build, push and start each artifact** — one cell per artifact, on the
   architecture it is deployed to, from the same matrix CI verifies
   (`test_publish_workflow.py` fails if the two differ). Each cell pushes by
   digest with `--build-arg SOURCE_COMMIT=<sha>`, pulls that digest back, and
   runs `smoke.py` on the pulled image: the check is applied to the bytes the
   registry holds, and the pull is the first proof they can be pulled at all.
   It then records the digest and the image's own labels in one small file.
3. **Assemble the manifest.** `publish_inputs.py assemble` turns the seven
   records into the generator's input (below), `build_recipe.py` packages the
   recipe, and `release_manifest.py` writes the document — only if every
   check passes.
4. **Publish a release** tagged `agent-<sha>`, created as a draft, with
   `recipe.tar.gz` and `release.json` attached, and made visible only once
   both assets are on it. The release notes and the run summary carry the
   sha256 of `release.json`, which is the value a distribution's lock pins.

Nothing on this path deploys, and nothing on it is reachable from a pull
request: there is no `pull_request` trigger, and `workflow_run` executes the
copy of the file on the default branch. That is also the one operational
caveat: `workflow_run` fires only for a workflow file that is on the default
branch, so until `publish.yml` has been synced to `main`, dev pushes do not
publish on their own and a dispatch on `dev` is how a release is produced.

The generator's input, written by `publish_inputs.py assemble` from the cell
records:

```json
{
  "source_commit": "4af1b771ac8f736a1be24f20cb307012af114a18",
  "artifacts": [
    { "role": "control", "image": "ghcr.io/…/cloud-agent@sha256:…", "platforms": ["linux/arm64"] },
    { "role": "web", "base_path": "/agents", "image": "…@sha256:…", "platforms": ["linux/arm64"] }
  ],
  "runtime_labels": { "org.freeinference.runtime.codex.version": "0.145.0", "…": "…" }
}
```

`runtime_labels` is the sandbox image's label map, verbatim from
`docker image inspect`: the same labels the host runner reads when it stamps an
attempt. The manifest therefore describes the image that was built rather than
the Dockerfile defaults CI may have overridden, and there is no second place
where a CLI version is written down.

```bash
python -m deploy.release.release_manifest \
  --inputs inputs.json \
  --recipe-url https://github.com/HarvardMadSys/hybridInference-cloud-agent/releases/download/agent-4af1b77/recipe.tar.gz \
  --recipe-sha256 <64 hex> \
  --publisher-run-url https://github.com/HarvardMadSys/hybridInference-cloud-agent/actions/runs/<id> \
  --output release.json
```

The manifest is written only after every check passes, in one piece: a
half-written document with a correct-looking hash is worse than no document.

## Starting, not just building

An image that builds is an image whose Dockerfile parses. `deploy/release/smoke.py`
starts each artifact the way its deployment does and asserts what only starting
shows; every entry says what it proves *and* what it does not, because a check
that quietly proves less than its name is worse than no check.

| Artifact | Starting it proves |
|---|---|
| control | migrates a real database with `alembic upgrade head`, then answers `/healthz` |
| web (both variants) | serves its sign-in page under its own mount prefix, with the identity read at runtime |
| runner | its modules import and the Docker CLI it shells out to is present |
| sandbox | every Agent CLI reports the version its own image label claims |
| tunnel | the entrypoint runs and refuses an unreachable peer loudly |
| egress | comes up under a rendered config and accepts connections |

The same script is what CI runs on a pull request and what the publication
workflow runs over the images it is about to push. One implementation, because
the check that matters is the one applied to the bytes that get published, and
two copies of "it starts" would drift apart.

Each run also asserts the image's own identity labels —
`org.opencontainers.image.revision`, `io.hybridinference.artifact.role`, and
`base_path` for the web variants. The Dockerfiles default the revision to
`unknown`, so a build argument that fails to arrive produces an image every
resolver refuses; this is the last place that can be seen before it is pushed.

## Control-plane integration acceptance

A release is published only if it deploys. Between pushing the images and
creating the release, the publication workflow installs the candidate the way a
distribution will: `deploy/release/acceptance.py` extracts the recipe into a
directory with no source, writes a deployment `.env`, and runs the real
`deploy/control-plane/deploy.sh` in artifact mode against a real Docker daemon,
pulling this run's own digests from the registry. Then it asks each running
container what it is and compares that with the manifest.

| It proves | It does not prove |
|---|---|
| The recipe installs with no checkout, no Dockerfile, no build | Anything about the execution host, Kata, or a real job |
| The images pull by digest and the containers carry those digests | That a second architecture would work; this starts the arm64 half |
| Alembic migrates a real database from the control image | That the gateway contract works; there is no gateway |
| The identity namespace check runs, and both listeners answer `/healthz` | That sign-in works end to end |
| The web service runs the variant the manifest named, by its own labels | That the console renders for a user |
| A deployment naming its own image is refused before anything is pulled | That a *registry* would refuse it; that is the resolver's job, in P4 |

**The host readiness fence is expected to refuse, and that is the point.**
`deploy_readiness` waits for a heartbeat from an execution host running this
exact commit, and CI has no such host. Standing one up would make the check
prove something about the fixture, and weakening the fence for CI would remove
the only thing between a new control plane and a pool that has not been replaced
with it. So the deploy is expected to reach the fence and stop, which is itself
asserted: reaching it means every stage before it passed, in order. The stages
are listed in `acceptance.py` as `STAGES`, and the module says what each one
stands for.

One service is left over by that stop. `web` starts after the fence, so the
acceptance brings it up itself, from the same generated Compose file and under
the same `--no-build --pull never` restrictions the deploy uses. That is an
explicit step of the acceptance rather than something the deploy did.

The comparison is in `accept_artifacts.py`, as pure functions with no daemon, so
what "is what is running what the release named" means can be read and tested on
its own. Two things it deliberately gets right: a registry digest is taken from
`RepoDigests` rather than the image ID, which is a different object; and a role
is checked against the image's own label rather than the registry path, because
which package an image lives in is storage layout.

## Registry layout is not part of this contract

Artifacts are addressed by digest, so which package or tag an image lives in is
storage layout rather than interface. The role is carried by the image's own
`io.hybridinference.artifact.role` label, not by the repository path, so the
layout can change without the manifest becoming a lie.

Decided, and written into `publish.yml`:

| Package | Contents | Why |
|---|---|---|
| `ghcr.io/harvardmadsys/cloud-agent` | control, web (both variants), runner, tunnel, egress — one tag per role and commit | Access control in GHCR is per package, and none of these needs to differ. |
| `ghcr.io/harvardmadsys/cloud-agent-sandbox` | sandbox | It bundles third-party Agent CLIs whose redistribution is a separate question, so it needs a visibility switch of its own. |

Each digest also gets one tag for people, `<cell>-<sha>` — `control-<sha>`,
`web-agents-<sha>`, `sandbox-<sha>` and so on — written only after the image
has started. Nothing selects on it.

Moving between layouts later is possible but not free: a faithful copy of a
multi-architecture index keeps its digests, while re-creating an index — a
platform-specific push, a format conversion — produces a different index digest
and therefore a reviewed lock bump. Keep the old address until the rollback
window closes.

## Where this differs from the design document

The design is `docs/specs/2026-09-10-cloud-agent-release-fi-deploy-design.zh.md`,
committed as written. Recorded here rather than edited into it:

* **The deployment protocol is v3, not v2.** The design says
  `shared-live-exact-sha-v2`; `deploy/DEPLOY_PROTOCOL` and both deploy scripts
  say `shared-live-exact-sha-v3` as of this change. The schema pins what the
  file says, and a test fails if the two ever disagree.
* **Seven images, six Dockerfiles.** Web is one Dockerfile built twice, because
  `basePath` is resolved while the bundle is compiled. The design's "seven
  logical artifacts" is what that adds up to; no dependency-resolution layer was
  introduced along the way.
* **`egress` stays an owned image.** Its startup command, its effective user and
  the path of the config it reads are runtime behaviour this project has already
  paid for once — the proxy crash-looped 2814 times on staging before the
  `chown` in its `CMD` existed. Trading that for a third-party base plus a
  recipe-level command override would add a validation path and remove no
  component, so the image stays ours.
* **Runtime labels keep their namespace.** They are
  `org.freeinference.runtime.*` today, and the manifest reads them as they are.
  Moving them to a neutral namespace is a dual-read migration of its own (D8),
  not something a release should force.

## Deploying a recipe

The recipe contains deployment helpers, configuration examples, and Compose
templates with no `build:` entries. Image variables in those templates are bound
to the manifest's digests before any Compose startup command. The same recipe
supports both Web variants; select `AGENT_WEB_VARIANT=root` or `agents` in the
deployment configuration. If omitted, the existing `AGENT_WEB_BASE_PATH` selects
the variant, with an empty/unset path selecting `root`.

Extract the archive into a release directory. Install its
`deploy/release/requirements.txt` into a Python 3.12+ virtual environment and
point `AGENT_DEPLOY_PYTHON` at that interpreter. The host needs Docker Compose
and its existing Kata provisioning; the deployment does not install a runtime.

Keep `DEPLOY_ROOT` stable across releases. Its `.env`, `.deployed-sha`, and
`.shared-live-ready` belong to the deployment, while the extracted recipe can
live in a different directory. Keep the existing Compose project name, host
enrollment root, and Docker data root when migrating an existing installation.
Generated Compose files live under the stable deployment root so relative
mounts continue to point at the same data. The edge proxy's configuration is
read from the selected recipe.

For the control plane:

```bash
AGENT_DEPLOY_MODE=artifacts \
AGENT_RELEASE_MANIFEST=/opt/cloud-agent/release/release.json \
AGENT_DEPLOY_PYTHON=/opt/cloud-agent/deployer/bin/python \
DEPLOY_ROOT=/srv/cloud-agent-control \
bash /opt/cloud-agent/release/recipe/deploy/control-plane/deploy.sh
```

For the execution host, use the same release and interpreter with that host's
own persistent root:

```bash
AGENT_DEPLOY_MODE=artifacts \
AGENT_RELEASE_MANIFEST=/opt/cloud-agent/release/release.json \
AGENT_DEPLOY_PYTHON=/opt/cloud-agent/deployer/bin/python \
DEPLOY_ROOT=/srv/cloud-agent-host \
bash /opt/cloud-agent/release/recipe/deploy/host/deploy.sh
```

### Deploying the two in the order the design specifies

Each command above is the whole of its side, and the two sides are independent
processes on two machines, so running them both is a deployment whose ordering
is whatever the two happen to do. The design sequences them: both sides prepare,
the control plane then prepares its database and checks the identity namespace,
the host then publishes its generation, and only then does the control plane
switch public services.

`--phase` is how a deployer expresses that. Every phase is idempotent and takes
the same environment as the whole script; `prepare` and `prepare-database`
replace nothing that is serving.

```bash
# 1. both sides, in parallel
… deploy/control-plane/deploy.sh --phase prepare
… deploy/host/deploy.sh          --phase prepare

# 2. the control plane's database, alone. On a cold machine this also starts
#    the API and the MCP relay, which the host's readiness probe needs and
#    which nothing is serving yet; a machine that already has them keeps the
#    ones it is running.
… deploy/control-plane/deploy.sh --phase prepare-database

# 3. the host replaces its runners and publishes the target generation
… deploy/host/deploy.sh          --phase activate

# 4. the control plane's fence, then the public services
… deploy/control-plane/deploy.sh --phase activate
```

Passing no `--phase` runs every phase of that side in order, which is what a
deployer that does not need to interleave the two should do.

Every phase is safe to run again. Re-running `prepare-database` does not disown
the listeners an earlier run of it started, and re-running `activate` after a
failure restarts anything that failure's cleanup stopped — a fence waits for a
heartbeat, and only a running API can accept one.

The manifest supplies the commit and all owned image references. Any conflicting
effective Compose configuration is refused before pulling or replacing services.
Remove old image/commit overrides when switching to artifact mode. The default
`source` mode continues to support the existing checkout-based workflows.

Image pulls and the host's Kata probe precede runner drain. Migrations, the
identity namespace check, exact-commit readiness, reverse broker probes, and
deferred host readiness publication retain their existing ordering. A failed
readiness gate does not activate the replacement control-plane stack.

The script tests execute an extracted recipe with Docker actions recorded and
failure-injected. They also exercise the real Compose environment parser when
available. These tests do not establish that a particular Linux host, registry
release, or live Kata runtime is ready; that remains the environment cutover
acceptance.

## What is still owed

* **The execution host's half of the acceptance**: the control-plane stack is
  installed and checked on every release, above. The host stack is not, because
  it needs Kata and a machine that can run it, so it belongs to the staging
  handoff along with the failure and rollback drills.
* **P4**: the resolver — the registry-side half of these checks, and
  `agent.lock` pinning this document's hash.
