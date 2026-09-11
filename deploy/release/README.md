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

The publication job (P2b) builds each artifact with
`--build-arg SOURCE_COMMIT=<sha>`, then writes one input file:

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

## Registry layout is not part of this contract

Artifacts are addressed by digest, so which package or tag an image lives in is
storage layout rather than interface. The role is carried by the image's own
`io.hybridinference.artifact.role` label, not by the repository path, so the
layout can change without the manifest becoming a lie.

The plan, decided when the first push happens:

| Package | Contents | Why |
|---|---|---|
| `ghcr.io/harvardmadsys/cloud-agent` | control, web (both variants), runner, tunnel, egress — one tag per role and commit | Access control in GHCR is per package, and none of these needs to differ. |
| `ghcr.io/harvardmadsys/cloud-agent-sandbox` | sandbox | It bundles third-party Agent CLIs whose redistribution is a separate question, so it needs a visibility switch of its own. |

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

* **P2b**: the publication workflow — build and start each artifact, push by
  digest on a green `dev`, and publish the manifest and recipe as release assets.
  The seven-artifact CI smoke matrix, including both Web variants, is already
  implemented.
* **P3 acceptance**: run the packaged deployment against real candidate images
  on the intended control-plane and Kata hosts, including failure/recovery
  checks. The source-free script path is covered by the tests above.
* **P4**: the resolver — the registry-side half of these checks, and
  `agent.lock` pinning this document's hash.
