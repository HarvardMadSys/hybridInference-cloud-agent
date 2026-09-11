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

## What is still owed

* **P2b**: the publication workflow — build and start each artifact, push by
  digest on a green `dev`, publish the manifest as a release asset; plus the two
  web variants in the CI build matrix, which is where the image is first
  verified on its own architecture rather than only as a bundle.
* **P3**: artifact-mode deployment — recipes without `build:`, `pull` plus
  `up --no-build`, and the same host-side protocol checks.
* **P4**: the resolver — the registry-side half of these checks, and
  `agent.lock` pinning this document's hash.
