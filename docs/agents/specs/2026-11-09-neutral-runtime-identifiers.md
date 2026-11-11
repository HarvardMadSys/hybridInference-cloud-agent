# Neutral runtime identifiers: inventory and migration off `freeinference`

- **Status:** design. Class (a) is implemented on `wt/runtime-identifiers`; every
  other class is a proposal for the pull requests in § 8.
- **Date:** 2026-11-09
- **Scope:** every FreeInference-named identifier in `apps/`, `deploy/`,
  `scripts/`, `tests/` and `.github/` (`grep -rni freeinference`, which also
  catches `FREEINFERENCE`): 114 lines in `apps/backend`, 60 in
  `apps/frontend`, 150 in `deploy`, 0 in `scripts`, 310 in `tests`, 2 in
  `.github`.
- **Out of scope:** user- and agent-visible text, which the change that adds
  this spec makes neutral (`cloud_agent_host/platform_instructions.py`, the
  GitHub relay's refusals, the archive-bucket examples in
  `cloud_agent/settings.py` and `SandboxArchiveSettings.tsx`), and
  `cloud_agent/accounts/import_accounts.py`. No identifier here depends on that
  text: the platform instructions name no environment variable (§ 3.4).
- **Supersedes:** the note in `deploy/release/README.md:210-212` that runtime
  labels "remain `org.freeinference.runtime.*`" until "a separate dual-read
  migration". This document describes that migration.

## 1. Problem

The Cloud Agent was extracted from the FreeInference codebase. It is meant to
be deployment-neutral, but Docker objects, sandbox paths, environment
variables and image labels still carry the FreeInference name. Many of these
identifiers outlive a release:

- A retained container keeps its labels, name and image for as long as its
  group exists. Groups do not age out. They are deleted only on request
  (`cloud_agent/workers/sandbox_deletion.py`).
- A checkpoint or snapshot image keeps the labels of the container it was
  committed from.
- Archives in S3 are kept indefinitely.
- Container names are stored in the control plane's database.

A rename that changes only the constants breaks every retained sandbox on the
first host that deploys it (§ 4). This document lists every identifier,
explains why each one is hard to change, and gives a migration sequence in
which every step is safe to roll back.

## 2. Decisions

**D1. One namespace, written in two forms.** The namespace is the project
itself, *HybridInference Cloud Agent*, not any deployment of it.

- **Docker and OCI label keys** use `io.hybridinference.` followed by the
  current suffix. For example, `org.freeinference.chat-sandbox` becomes
  `io.hybridinference.chat-sandbox`.
- **All other names** use `cloud-agent`: container, network and image names,
  paths, markers, temporary-file prefixes and wrapper binaries. Environment
  variables use `CLOUD_AGENT_`, and identifiers that cannot contain hyphens
  use `cloud_agent_`.

Reasons for the label form:

1. The label namespace is already published. Every artifact carries
   `io.hybridinference.artifact.role`:
   - `deploy/control-plane/Dockerfile:85`
   - `deploy/control-plane/Dockerfile.web:92-93`
   - `deploy/host/Dockerfile.agent-runner:97`
   - `deploy/host/Dockerfile.agent-ssh-tunnel:35`
   - `deploy/sandbox/Dockerfile.agent-sandbox:629`

   These labels are checked by `deploy/release/accept_artifacts.py:130-141`,
   `deploy/release/smoke.py:730-740` and `deploy/release/publish_inputs.py:44-45`.
   The control plane is labelled
   `io.hybridinference.deployment.sha` (`deploy/control-plane/docker-compose.yml:48`,
   read by `cloud_agent/providers/compute/setup_tunnels.py:271`), and CI
   smoke containers are labelled `io.hybridinference.smoke.run`
   (`.github/workflows/ci.yml:1318`).
2. The downstream FreeInference deployment already reads this namespace and
   nothing else from our images: its release resolver reads
   `io.hybridinference.artifact.{role,base_path}`, the labels
   `deploy/release/README.md` tells a downstream resolver to verify, and no
   downstream code reads any `org.freeinference.*` key this repository writes.
   Switching to this namespace therefore adds no new namespace downstream.
3. Mapping a key is one mechanical prefix swap, so code that reads both forms
   only translates keys and can be tested against every key. None of the
   suffixes in use today collide: `artifact.*`, `deployment.*` and `smoke.*` on
   one side; `chat-*`, `sandbox-namespace`, `runtime-download.*`, `runtime.*`,
   `helper.*` and `storage-attestation` on the other.
4. Reverse-DNS key names are the Docker and OCI convention. A key name does not
   claim ownership of the domain, and this one is already published (reason 1).

Reasons for the flat form: reverse-DNS cannot be used in file names,
environment variable names or container names. The repository already uses
`cloud-agent` for the names it owns:

- In-sandbox tools: `/usr/local/bin/cloud-agent-browser`,
  `/usr/local/bin/cloud-agent-mcp` (`Dockerfile.agent-sandbox:386-389`).
- Paths: `/workspace/.cloud-agent-artifacts`, and the `CLOUD_AGENT_*`
  variables in `Dockerfile.agent-sandbox:491-498`.
- Machine state: `/var/lib/cloud-agent-{transfer,tunnels,node}`.
- The managed host state root `/var/lib/cloud-agent/<host>`.
- The `cloud_agent_` nftables tables (`cloud_agent_shared/egress.py:110`).
- The temporary file `.cloud-agent-` (`setup_tunnels.py:510`).

Rejected alternatives:

- `io.hybridinference.cloud-agent.*` makes keys longer and departs from the
  published `io.hybridinference.artifact.*`. No other HybridInference
  component labels Docker objects on these hosts, so the extra segment would
  not distinguish anything.
- Inventing a namespace such as `org.cloud-agent.*` adds a third namespace.
- Keeping `org.freeinference.*` contradicts the goal of deployment neutrality.

The one outlier, `hybridinference.environment-test`
(`chat_sandbox_models.py:36`), joins the namespace as
`io.hybridinference.environment-test` and migrates like the other labels.
In-container paths under `/var/lib/hybridinference/*`, such as the runner's
credential and the broker's archive state, are already deployment-neutral.
They are out of scope.

**D2. A recorded identity is never re-derived.** Today, container names,
network names and checkpoint or snapshot image references are re-derived from
a prefix and a hash, then compared with what Docker or the database holds:

- `exact_container.py:357-398`
- `group_reservation.py:159`
- `chat_sandbox_network.py:1116`

After a rename, a new name applies only to objects created after the switch.
An existing object keeps its name for life. Code that compares names accepts
the recorded name when it matches either derivation, and code that operates
on an existing object uses that object's own recorded name.

**D3. The layout belongs to the content.** Paths inside the sandbox are
declared by whatever holds them; the host's release never decides them.

- **Image-resident paths** (helpers, wrappers) are declared by the image,
  through a new image label `io.hybridinference.sandbox-layout="2"`. An
  absent label means layout 1, today's paths.
- **Paths in the container's writable layer, `/agent-state` and member homes**
  are declared by the container, through a container label
  `io.hybridinference.chat-layout="2"` set at creation. An absent label means
  layout 1.
- **Copies:** `docker commit` copies container labels into the image
  configuration. Checkpoint and snapshot images therefore carry the layout of
  their source, and a restore must keep that layout rather than apply the
  host's default.

**D4. Read both forms, prefer the new one, and write both before writing only
the new one.**

- **Reading:** the new key wins whenever it is present. The legacy key is read
  only when the new one is absent.
- **Listing:** Docker combines repeated `--filter label=…` options with AND.
  Every listing must therefore run once per namespace and merge the results
  by object ID. A single query naming both keys finds only objects that carry
  both.
- **Where to translate:** keys are translated once, where labels are parsed:
  - containers: `container/chat_sandbox_inspect.py:155-169`;
  - networks: `network/chat_network_ownership.py:26` and
    `network/chat_sandbox_network.py:1108`;
  - images: `container/sandbox.py:117-148`.
- **Stale inherited labels:** `--label` overrides an inherited image label
  with the same key, so writing both forms never leaves stale values. When
  writing switches to the new form only, container creation also sets each
  legacy key to an empty value. `ENVIRONMENT_TEST_LABEL` already uses this
  technique (`chat_sandbox_models.py:32-35`), and it keeps stale inherited
  values out of `docker inspect` output.

**D5. Removal depends on observed absence, not on a date alone.** Docker
cannot relabel or rename an image, network or container in place, and
retained groups never age out. Code that reads legacy identifiers from
Docker objects or sandbox content can be removed only after every host
reports that no such object remains. Its `COMPAT` marker carries a review
date and that condition. Code kept only for the rollback window (§ 7) carries
an ordinary date.

## 3. Inventory

The tables below group identifiers by class. Line numbers are at `7031c75`.
"Lifetime" means how long a value written today can still be read.

### 3.1 Class (a): process-local or ephemeral

**Status:** renamed on this branch. These names have no reader outside the
creating process. `tempfile.TemporaryFile` is unnamed on Linux (`O_TMPFILE`).
`TemporaryDirectory` is removed on exit. No sweeper, glob or test matches these
prefixes (checked with `grep -rn` across the repository).

| Identifier | Where | Writer → reader | Lifetime |
|---|---|---|---|
| `freeinference-workspace-download-` | `cloud_agent_host/manager_api/workspaces.py:159` | sandbox manager spool → same response | one download |
| `freeinference-workspace-export-` | `cloud_agent_host/manager_api/workspaces.py:164` | sandbox manager → same call | one call |
| `freeinference-base-` (directory) | `cloud_agent_host/turn/source_preparation.py:757` | runner → same function | one fetch |
| `freeinference-source-` (file, `.tar`) | `cloud_agent_host/turn/source_prefetch.py:31` | prefetch thread → `import_into`/`discard` | one prefetch |
| `freeinference-source-` (directory) | `cloud_agent_host/turn/source_prefetch.py:84` | prefetch thread → same | one prefetch |
| `freeinference-member-pipe-` ×2 | `cloud_agent_host/container/chat_sandbox_members.py:786-787` | `pipe_member_exec` stderr | one pipe |
| `freeinference-docker-cp-error-` | `cloud_agent_host/container/chat_sandbox_workspace.py:410` | `docker cp` stderr | one export |
| `freeinference-final-archive-marker-` | `cloud_agent_host/container/chat_sandbox_markers.py:487` | marker read | one read |
| `freeinference-final-archive-marker-write-` | `cloud_agent_host/container/chat_sandbox_markers.py:539` | marker write | one write |
| `freeinference-source-manifest-` | `cloud_agent_shared/git/source_archive.py:238` | manifest builder | one build |
| C include guards `FREEINFERENCE_TOOL_MARK_H`, `FREEINFERENCE_GUEST_OBSERVER_H` and the `FI_*` macros | `deploy/sandbox/tool-mark.h:2-3`, `guest-observer.h:1-2` and others | compiler only | build |

The C identifiers last only during a build, but they live in the sandbox
helper sources. Python test helpers mirror the `FI_*` macros
(`tests/unit/guest_serializer.py`, `test_cross_artifact_contracts.py`). They
are therefore deferred to the image PR of § 8 (PR 9), so that the helper
sources change once.

### 3.2 Class (b): names and labels on Docker objects that outlive a release

| Identifier | Where | Writer → reader | Lifetime |
|---|---|---|---|
| `org.freeinference.chat-sandbox`, `.sandbox-namespace`, `.chat-thread-id`, `.chat-sandbox-id`, `.chat-generation`, `.chat-runtime`, `.chat-runtime-version`, `.chat-image-digest`, `.chat-checkpoint`, `.chat-memory-limit`, `.chat-cpu-limit`, `.chat-disk-limit-mib`, `.chat-stop-timeout-seconds`, `.chat-shared-live`, `.chat-resident-slot-capacity`, `.chat-probe-created` | `cloud_agent_host/container/chat_sandbox_models.py:18-46`, written via `docker_labels()` at `:214` and `chat_sandbox_create.py:197` | host creates the container. Readers: identity check `exact_container.py:357-398`; label → spec `network/guarded_workloads.py:97-118`; listings `chat_sandbox.py:576-586`, `network/idle_host_rules.py:46-49`, `observe/sandbox_inventory.py:322-323`, `chat_sandbox_observation.py:131-132`, `runtime_images.py:198-204`; control plane `providers/compute/release_scripts.py:48-51,116`; tests `tests/support/host_docker.py:10-44` | container lifetime, which is unbounded; copied into checkpoint and snapshot images by `docker commit` |
| `org.freeinference.chat-sandbox-network`, `.chat-service-aliases`, `.chat-network-pool` (plus the shared `.chat-sandbox-id`, `.chat-generation`, `.sandbox-namespace`) | `chat_sandbox_models.py:47-51`; written `network/chat_sandbox_network.py:179-187` | host → adoption `chat_sandbox_network.py:1084-1130`, ownership `chat_network_ownership.py:23-34`, `release_scripts.py:125` | one group generation |
| `org.freeinference.chat-environment-snapshot` | `cloud_agent_shared/environment_snapshots.py:65` | host → `exact_container.py:379` | container and snapshot image |
| `hybridinference.environment-test` (outlier) | `chat_sandbox_models.py:36` | host → `guarded_workloads.py:103`, `release_scripts.py:67` | container and snapshot image |
| `org.freeinference.runtime-download.{namespace,runtime,version,work,source}` | `cloud_agent_host/runtime_images.py:46-59` | runner downloads → runner menu `runtime_images.py:160-230`; `release_scripts.py:53-68`; `tests/external/test_runtime_updates_live.py:162` | downloaded image (unbounded) |
| `org.freeinference.storage-attestation` | `deploy/host/storage_attestation.py:358` | attestation `docker run --rm` → `release_scripts.py:68` (treated as "not our work") | seconds, but a killed run can leave it |
| Container name `freeinference-chat-<32hex>` | `chat_sandbox_models.py:52,208-212` | host derives it → stored in **`agent_sandboxes.container_name` (UNIQUE, `migrations/versions/0001_schema_baseline.py:310`)** through `turn/group_reservation.py:237`; compared at `group_reservation.py:159` and `exact_container.py:397` | group lifetime (unbounded) |
| Network name `freeinference-chat-net-<24hex>` | `network/chat_sandbox_network.py:55,160-173`; comment on sort order `:60-66` | host → adoption filter `:1116`; bridge interface name is a hash of it (`cloud_agent_shared/egress.py:152-155`) | one group generation |
| Checkpoint image `freeinference-checkpoint/<32hex>:<id>` | `cloud_agent_host/archive/sandbox_checkpoints.py:82,163-166` | host commit → `exact_container.py:390-394` (expected `Config.Image`), restore `sandbox_checkpoints.py:446,528` | local image plus each container recreated from it; **archives in S3 are renamed on load** (`archive/sandbox_checkpoint_oci.py:531-541` rewrites `index.json`/`manifest.json` to the requested reference) |
| Snapshot image `freeinference-environment/<32hex>:<id>` | `environment_snapshots.py:63,114-118` | host commit → listing `sandbox_checkpoints.py:506` (`reference=freeinference-environment/*:*`), `exact_container.py:381-385` | team-wide, long-lived |
| Inherited runtime/helper prefixes `org.freeinference.runtime.`, `.helper.` | `release_scripts.py:58` | control plane → classifies work containers | see class (e) |

### 3.3 Class (c): paths and files inside the image or the container

A retained container keeps its image across releases. Checkpoint and snapshot
images extend the image their source container ran.

| Identifier | Where | Writer → reader | Lifetime |
|---|---|---|---|
| `/usr/local/libexec/freeinference-guest-observer` (+ `.bpf.o`) | `observe/guest_observer.py:48`; `deploy/sandbox/guest-observer.c:42-43`; `Dockerfile.agent-sandbox:51-53,390-393,401,406` | image → host privileged exec `guest_observer.py:295,343,398,407,596`, preflight `chat_sandbox_observation.py:245` | image |
| `/usr/local/libexec/freeinference-tool-mark` | `observe/tool_windows.py:84`; `tool-mark.h:13`; `Dockerfile.agent-sandbox:57,394-395,402` | image → member hook command `tool_windows.py:568-572` (**silently does nothing when the path is missing**), preflight `chat_sandbox_observation.py:252,272`, **process classifier `guest_observer.py:1327` (exact executable path)** | image; hooks are rewritten on each member preparation |
| `/usr/local/libexec/freeinference-member-window` | `observe/member_resources.py:1159`; `guest-observer.h:118`; `Dockerfile.agent-sandbox:55,396-397,403` | image → host `member_resources.py:1235-1267` | image |
| `/run/freeinference-observer` (tmpfs), `…/member-window.sock` | `guest_observer.py:45`; `guest-observer.c:39`; `guest-observer.h:117` | **host mounts it at creation** (`chat_sandbox_create.py:278`); the image's observer has the path compiled in | container (mount is fixed at create) |
| `/run/freeinference-tool-mark.sock` | `tool-mark.h:12` | image ↔ image | image |
| Self-test identity lines `freeinference-tool-mark-v1`, `freeinference-guest-observer-v<N>` | `tool-mark.c:401`, `guest-observer.c:1851` → `chat_sandbox_observation.py:256,310-319` | image → host preflight | image |
| Wrappers `pi-freeinference`, `opencode-freeinference`, `kilo-freeinference` | `deploy/sandbox/*-freeinference`; `Dockerfile.agent-sandbox:373-375,398-399`; `runtimes/pi/adapter.py:73`, `http_bridge/opencode.py:24`, `http_bridge/kilo.py:28`; `member_setup/mcp_helpers.py:19-21`; `deploy/sandbox/http-agent-live-bridge:35,42`; runner copies `deploy/host/Dockerfile.agent-runner:77-78`, `deploy/release/build_host_bundle.py:59-61` | image → host argv; launch-root probes compare bytes with the runner's copy | image |
| Provider id `freeinference` (and the display name `"FreeInference"`) | `runtimes/pi/adapter.py:159`; `pi-freeinference:125`; `opencode-freeinference:159,163`; `kilo-freeinference:162,166`; `http-agent-live-bridge:810,815,870` | wrapper config ↔ CLI; **written into native session history** | session lifetime |
| `freeinference-instructions.md`, `~/.{opencode,kilo}-freeinference.json` | `opencode-freeinference:140,174`; `kilo-freeinference:145,177` | wrapper → CLI (activation tmpfs; the home-directory fallback is not retained) | one run |
| `/etc/freeinference/` (`github-relay-ca.pem`, `ca-bundle.pem`) | `cloud_agent_host/github_relay_trust.py:36-40` | host installs it on each start → member environment points at it; **live member processes keep the path** | container |
| `/home/agent/.freeinference-platform` (`PLATFORM_STATE_DIR`, fork journals) | `cloud_agent_host/resident_transport.py:89,899,930` | host ↔ host, exactly-once receipts in the writable layer | container, checkpoint |
| `paths/freeinference-platform.tar` | `archive/sandbox_archive_bundle.py:84-87` | host → S3 final bundle (manifest names container and archive path) | archive (indefinite) |
| `/var/lib/freeinference-broker` (initial/final archive markers, and the environment setup marker beside them) | `container/chat_sandbox_markers.py:40-41,628-665`; pinned by `tests/unit/test_sandbox_manager_compat.py:120` ("keep the on-disk path so existing retained containers keep their archive fences") | sandbox manager ↔ sandbox manager | container, checkpoint, environment snapshot image (team-wide) |
| `# BEGIN/END FREEINFERENCE RELAYS` in guest `/etc/hosts` | `container/guest_root_exec.py:34-35` | host rewrites the block in place; the first matching line wins name resolution | container (Kata guest) |
| `.freeinference-member-state-v1` | `runtimes/codex/member.py:89`, `runtimes/http_bridge/member.py:102`, `resident_transport.py:518` | host → host (member home initialised or forked) | `/agent-state` |
| `.freeinference-plugins.json`, `.freeinference-plugin-progress.json`, `.freeinference-plugins.lock`, `.freeinference-hooks.lock` | `runtimes/codex/plugins.py:24,27`, `runtimes/claude/plugins.py:23`, `member_setup/plugins.py:142`, `member_setup/member_preparation.py:312` | host scripts run in the member → same | `/agent-state` |
| `.freeinference-runtime` (download activation marker) | `member_setup/runtime_install.py:42,103,141,154` | host → host | container |

### 3.4 Class (d): environment variables between host and sandbox

| Identifier | Where | Writer → reader | Lifetime |
|---|---|---|---|
| `OPENCODE_FREEINFERENCE_CONFIG_DIR`, `KILO_FREEINFERENCE_CONFIG_DIR` | `runtimes/http_bridge/adapter.py:51` → `opencode-freeinference:135`, `kilo-freeinference:140` | host → image wrapper | process |
| `OPENCODE_FREEINFERENCE_INSTRUCTIONS`, `KILO_FREEINFERENCE_INSTRUCTIONS` | `runtimes/http_bridge/member.py:76` → `opencode-freeinference:175`, `kilo-freeinference:178` | host → image wrapper | process |
| `FREEINFERENCE_GIT_COAUTHOR` | written at `launch/github_relay.py:68`; asserted in `tests/unit/test_sandbox_github_auth.py:85` | host → **nothing**: the platform instructions write the trailer value itself (`platform_instructions.py:51-54`) and no code reads the variable | live member process |
| Reserved owner-variable prefix `FREEINFERENCE_` | `cloud_agent_shared/environment.py:85`, enforced by `validate_name` `:113-123` in a shared model that **both** the control plane and the host validate | control plane and host | stored environments |
| Inventory of known names | `tests/support/env_inventory.py:572-577` | tests | — |

`CLOUD_AGENT_` is **not** a reserved prefix today, although the image already
sets platform variables under it (`Dockerfile.agent-sandbox:491-498`).

### 3.5 Class (e): published contract

| Identifier | Where | Writer → reader | Lifetime |
|---|---|---|---|
| `org.freeinference.runtime.<id>.{version,versions,source,mcp-isolation}` | written `deploy/sandbox/Dockerfile.agent-sandbox:344,353-363` | host capabilities `container/sandbox.py:67-70,117-133`; release `deploy/release/release_manifest.py:100,140-197` → **`runtime_catalog` in the manifest (normalized; no label key in the document)**; `publish_inputs.py:204`; `smoke.py:69,617-625`; inherited by runtime-download images (`release_scripts.py:58`) and by every container's labels | image (plus retained containers and images built from it) |
| `org.freeinference.helper.<id>.version` | `Dockerfile.agent-sandbox:345-352` | `sandbox.py:80,136-148`; **CI `ci.yml:519` uses `sed` to read one line from the Dockerfile**; `tests/unit/test_cross_artifact_contracts.py:78` | same |
| Text naming the prefix | `deploy/release/manifest.schema.json:5,144`; `release_manifest.py:3`; `deploy/release/README.md:210-212` (a docs-site page) | documentation | — |

Downstream reads nothing in this table directly. The manifest exposes the
normalized `runtime_catalog`. FreeInference pins the manifest's SHA-256 and
reads only `io.hybridinference.artifact.*` from images. The only known
downstream mention is a dated, one-off operator record that filters Docker
objects on `org.freeinference.sandbox-namespace`; the release notes in
§ 10.5 cover it.

### 3.6 Class (f): everything else

| Identifier | Where | Writer → reader | Lifetime |
|---|---|---|---|
| Kata epoch directory `/var/lib/freeinference/kata-runtime` | `deploy/host/kata_runtime_epoch.py:21`; `deploy/host/sandbox_host.sh:1007`; `deploy/host/docker-compose.sandbox.yml:406` | installer → every stack on the machine (read-only bind mount) | machine; **shared by stacks on different releases** |
| Default sandbox image `freeinference-cloud-agent-job:latest` | `deploy/host/sandbox_host.sh:244` | deployment default | config |
| `DEPLOY_ROOT` default `/srv/freeinference-cloud-agent` | `deploy/control-plane/deploy.sh:42,76` | deployment default | config, **existing checkouts live there** |
| Example and error-message names (`freeinference-cloud-agent-sandbox-*`, `/var/lib/freeinference-cloud-agent-*`) | `deploy/host/.env.example:27-60`, `deploy/host/deploy.sh:169`, `sandbox_host.sh:224` | documentation | — |
| Default S3 archive bucket and URI `s3://freeinference/cloud-agent/sandbox` | `cloud_agent_shared/sandbox_archive_settings.py:37-38`; `cloud_agent/schemas/archive_settings.py:47`; **host↔control-plane value: an older sandbox manager accepts only this exact URI** (`cloud_agent/api/hosts.py:609-628`); `apps/backend/openapi.json:16039,20785`; `apps/frontend/src/lib/api/adminArchiveSettings.ts:16` | control plane → host | config default; stored settings |
| GitHub relay CA common name `FreeInference GitHub relay CA` | `deploy/control-plane/github-relay-certs.sh:31` | operator script → CA certificate | until the CA is re-issued |
| Preflight User-Agent `FreeInference-Context-Preflight/1.0` | `deploy/preflight/check_model_context.py:227` | outbound HTTP | request |
| Local development defaults (`freeinference-local`, `freeinference-local-agent-relays`, `freeinference-local-control`, `freeinference-cloud-agent-sandbox-*:local`, `/var/lib/freeinference/*` mount targets, `FreeInference Cloud Agent (local)`, the git identity in the repository fixture) | `deploy/local/dev.sh:47,57,111,210,214,277-293`; `deploy/local/docker-compose.yml:67-177`; `deploy/local/.env.example:27-50`; `deploy/local/Dockerfile.repo:13-14`; `deploy/local/README.md:95` (a docs-site page) | developer `.env` (written once by `dev.sh`; existing values are kept) | developer machine |
| CI | `.github/workflows/ci.yml:519` (Dockerfile `sed`, class e), `:1667` (`COMPOSE_PROJECT=freeinference-cloud-agent-sandbox-ci`) | CI | run |
| nftables tables `cloud_agent_*`, bridge interfaces `ca<5><8>` | `cloud_agent_shared/egress.py:110-160` | already neutral; **no action** | — |

**Not identifiers (keep):**

- References to FreeInference as a provider or an external product:
  - `frontend/.../modelSources.ts:7,32`, `LandingSections.tsx:192`,
    `TeamPage.tsx:3`, `next.config.js:13`;
  - `tests/unit/test_no_committed_credentials.py:34` (the gateway key format);
  - `test_local_byok_acceptance.py` (a provider fixture);
  - recorded CLI streams in `tests/fixtures/agent_runtime_streams/`;
  - `accounts/import_accounts.py` (out of scope).
- User- and agent-visible text, made neutral by the change that adds this
  spec: `platform_instructions.py:40`, `github_relay_policy.py:35`,
  `api/github_relay.py:112,329`, `settings.py:548,551`,
  `SandboxArchiveSettings.tsx:457`, and the docstring at
  `runtimes/base.py:330`.

**Tests.** 310 lines in 88 test files repeat the identifiers above, mostly as
expected values. Each test moves in the same PR as the code it covers. § 9
lists the test files for each class.

## 4. Hazards: what a constants-only rename breaks

Each hazard below is a reason for one of the steps in § 6–8.

| # | Hazard | Where |
|---|---|---|
| H1 | `verify_identity` compares every key of `spec.docker_labels()`, the container name and the checkpoint or snapshot `Config.Image` with the container. If the keys, prefix or repository change, **every existing retained container is refused** with "Docker name is occupied by a container outside this chat/generation". | `exact_container.py:350-398` |
| H2 | The host refuses a group when the recorded `container_name` differs from the derived one, so **every existing group fails its next turn**. | `turn/group_reservation.py:158-160`; `agent_sandboxes.container_name` |
| H3 | Network adoption treats a network whose name lacks the current prefix as the legacy topology. It is reported for cleanup, **never adopted or repaired**. | `network/chat_sandbox_network.py:1111-1124` |
| H4 | Docker combines repeated label filters with AND, and each listing filters on a single key. Objects in the other namespace become invisible to discovery, idle-host rules, the inventory and the release scripts. | `chat_sandbox.py:586`, `idle_host_rules.py:47-49`, `sandbox_inventory.py:323`, `chat_sandbox_network.py:1086-1088`, `runtime_images.py:164,199-201`, `release_scripts.py:48-53,125` |
| H5 | `own_work` in the node quiesce script treats unknown `org.freeinference.*` and `hybridinference.*` keys as someone else's container. It does not do this for `io.hybridinference.*` keys. If hosts wrote only new labels while that script was old, **a chat container that still carried a stale work label would be force-removed**. Writing both forms keeps the old script safe. The new script must also treat `io.hybridinference.artifact.*`, which every image inherits (`Dockerfile.agent-sandbox:629`), as inherited, or no work container would ever be cleaned. | `release_scripts.py:53-70`; `tests/unit/test_compute_host_bootstrap.py:1176-1192` |
| H6 | Writing helper labels in both forms gives the CI `sed` two matching lines, so `version` becomes multi-line. | `.github/workflows/ci.yml:519` |
| H7 | A new host calling a new helper path in an old-image container fails without any error: the hook silently does nothing, so tool boundaries fall back to message level. A new path that the classifier does not know would count the marker helper as user workload. | `tool_windows.py:563-572`, `guest_observer.py:1327` |
| H8 | The host mounts the observer's tmpfs at creation, and the image compiles the same path into the observer. They must match for each container, including a container restored from an old checkpoint by a new host. | `chat_sandbox_create.py:278`, `guest-observer.c:39` |
| H9 | A writer using a new relay-block marker would leave the old block in place. The old block comes first, so the stale entries win name resolution for the GitHub and MCP relays. | `guest_root_exec.py:34-48` |
| H10 | Bumping the archive bundle format version changes the bytes of initial bundles. The host rebuilds an initial bundle to match the digest in an existing marker, so the rebuilt bundle would no longer match. Only final bundles contain the platform directory (`sandbox_archive_bundle.py:437`), and final bundles are never rebuilt (`sandbox_archives.py:923-931`). | `sandbox_archive_bundle.py:46-47,81-89`; `sandbox_archives.py:71-87` |
| H11 | Reserving `CLOUD_AGENT_` is validated in a model that both the control plane and the host parse. An owner variable already stored under that prefix would then make claims fail on the host. | `cloud_agent_shared/environment.py:76-123` |
| H12 | Removing or changing the default archive URI breaks hosts whose sandbox manager predates `sandbox-archive-destination-v1`. Such a host is given the default URI or no policy, and **then takes no work**. | `api/hosts.py:609-628` |
| H13 | Moving the Kata epoch directory under `/var/lib/cloud-agent/` would make the node-release inventory count it as unexpected state and block the release. Any other machine-level location needs either a symlink for sibling stacks still on the older release, or both paths mounted. | `release_scripts.py:127-191`; `docker-compose.sandbox.yml:401-410` |
| H14 | Checkpoint archives are portable, because loading renames them. A checkpoint *image* already on a host, and the `Config.Image` of every container recreated from one, keep the old repository name. | `sandbox_checkpoint_oci.py:531-541`; `exact_container.py:390-394` |

## 5. Target names

| Today | Target |
|---|---|
| `org.freeinference.<suffix>` (every Docker label) | `io.hybridinference.<suffix>` |
| `hybridinference.environment-test` | `io.hybridinference.environment-test` |
| (new) | `io.hybridinference.sandbox-layout` (image), `io.hybridinference.chat-layout` (container) |
| `freeinference-chat-<32hex>` | `cloud-agent-chat-<32hex>` |
| `freeinference-chat-net-<24hex>` | `cloud-agent-chat-net-<24hex>` |
| `freeinference-checkpoint/…`, `freeinference-environment/…` | `cloud-agent-checkpoint/…`, `cloud-agent-environment/…` |
| `/usr/local/libexec/freeinference-<helper>` | `/usr/local/libexec/cloud-agent-<helper>` (shorter: fits `FI_EXECUTABLE_LEN` 192) |
| `/usr/local/bin/<cli>-freeinference` | `/usr/local/bin/<cli>-cloud-agent` |
| `/run/freeinference-observer`, `/run/freeinference-tool-mark.sock` | `/run/cloud-agent-observer`, `/run/cloud-agent-tool-mark.sock` |
| `/etc/freeinference/` | `/etc/cloud-agent/` |
| `/home/agent/.freeinference-platform`, `paths/freeinference-platform.tar` | `/home/agent/.cloud-agent-platform`, `paths/cloud-agent-platform.tar` |
| `/var/lib/freeinference-broker` | `/var/lib/cloud-agent-broker` |
| `.freeinference-<marker>` | `.cloud-agent-<marker>` |
| `# BEGIN/END FREEINFERENCE RELAYS` | `# BEGIN/END CLOUD AGENT RELAYS` |
| self-test `freeinference-<helper>-v<N>` | `cloud-agent-<helper>-v<N>` |
| provider id `freeinference` | `cloud-agent` |
| `<CLI>_FREEINFERENCE_<X>`, `FREEINFERENCE_GIT_COAUTHOR` | `<CLI>_CLOUD_AGENT_<X>`, `CLOUD_AGENT_GIT_COAUTHOR` |
| tempfile `freeinference-<x>-` | `cloud-agent-<x>-` (done) |
| `/var/lib/freeinference/kata-runtime` | `/var/lib/cloud-agent-kata-runtime` (machine-level, alongside `/var/lib/cloud-agent-transfer`; not under the per-host root; H13) |
| `FREEINFERENCE_*_H`, `FI_*` | `CLOUD_AGENT_*_H`, `CA_*` |

## 6. Migration by class

Release numbering: **R0** is the release before a step, **R1** is the first
release that contains it, and so on. AGENTS.md § 4 applies throughout: hosts
roll out before the control plane, each side supports only the previous
release, and the host and its sandbox image ship and roll back together
because the host bundle pins the image. "Rollback-safe" below means that R0
can operate everything R1 created.

### 6.1 Class (a): no compatibility code

The names never leave the process. The change is a rename, plus a guard test
that rejects a `tempfile` factory call under `apps/backend`, `deploy` or
`scripts` whose `prefix=` or `suffix=` keyword names the deployment (done with
this spec). It does not see positional arguments, aliased imports or a prefix
held in a constant; review covers those. The C identifiers move with PR 9.

### 6.2 Class (b): Docker labels and names

1. **Read both forms (host, PR 4).** No new writes yet.
   - **Translation:** use one shared vocabulary module (PR 2) and translate
     labels where they are parsed (D4).
   - **Identity check:** `verify_identity` compares the translated labels.
     It accepts the name recorded in the database or on the container,
     matching either derivation (D2), and either repository for the expected
     checkpoint or snapshot image.
   - **Reservation:** `group_reservation.py:159` accepts either derivation.
     `ChatSandboxSpec` gains an explicit `container_name` that is seeded from
     the record (`existing["container_name"]`) and from
     `inspection.name` in `sandbox_spec_from_inspection`. Without a record,
     it falls back to the derived name.
   - **Networks:** adoption (`chat_sandbox_network.py:1116`) accepts both
     prefixes. `network_for` keeps the name of the network that an existing
     generation already has.
   - **Listings:** every listing in H4 and
     `sandbox_checkpoints.py:506` runs once per namespace or repository.
   - **Labels callers cannot set:** the set at `chat_sandbox_models.py:183-201`
     names both forms.
   - **Inventory:** `observe/sandbox_inventory.py` reports
     `legacy_identifiers`, the number of containers, networks and images
     still carrying `org.freeinference.*` labels or legacy names. The admin
     hosts page shows it. D5 depends on this count.
2. **Read both forms (control plane, PR 3).**
   - `_QUIESCE` runs the probe and work filters once per namespace.
   - `own_work` treats platform keys in both namespaces as someone else's,
     except the download keys and the inherited `runtime.`, `helper.` and
     `io.hybridinference.artifact.` prefixes (H5).
   - `_INVENTORY` reads both chat-sandbox labels and both network labels.
3. **Write both forms (host, PR 5).** This covers containers, networks,
   runtime-download images, the storage-attestation container and the
   environment-test label. It is rollback-safe with no ordering requirement:
   R0 readers still find every legacy key, and the R0 `own_work` still sees
   `org.freeinference.chat-sandbox` on chat containers (H5).
4. **New names for new objects (host, PR 7).**
   - New groups get `cloud-agent-chat-*` and new generations get
     `cloud-agent-chat-net-*`.
   - New checkpoints and snapshots use the `cloud-agent-*` repositories.
     A restore loads an old archive under the new name: the rewrite already
     exists, see H14.
   - Ordering: PR 4 must have been on every host for one release, because R0
     must be able to read the new names after a rollback.
   - Existing objects keep their names (D2).
   - The comment at `chat_sandbox_network.py:60-66` and
     `tests/external/test_sandbox_network_boundary.py:323` are updated:
     `cloud-agent-chat-net-*` still sorts before `freeinference-*` and after
     `ca-<node>_*`, and the relay's gateway priority of −1 keeps sort order
     from mattering.
5. **Write only the new form (host, PR 12).** Legacy keys are set to empty
   values at creation (D4).
   - Ordering: PR 3 and PR 4 must be released on every control plane and
     host.
   - Remove the `COMPAT(remove-after=2027-01-31)` markers on the code that
     writes both forms.
6. **Remove legacy reads (PR 15+)** when `legacy_identifiers` is 0 on every
   host. The marker on that code is
   `COMPAT(remove-after=2027-06-30): every host's sandbox inventory reports legacy_identifiers == 0`.
   Expect to move this date. Retained groups do not age out, so if the count
   stays above zero, the open question in § 10 applies.

**Restored archives and retained sandboxes.**

- A retained container keeps its labels and name, and is read through the
  translation.
- A container recreated from a checkpoint or snapshot image gets labels in
  both forms (or only the new form, with legacy keys empty) on top of the
  labels it inherits. Its name comes from the record.
- A checkpoint archive in S3 is renamed on load and needs no compatibility
  code.

### 6.3 Class (c): sandbox paths and in-container state

1. **Host understands both layouts (PR 8).** It still creates only layout-1
   containers.
   - **Choosing paths:** helper and wrapper paths come from the container's
     image label, and state paths from the container label (D3).
   - **Relay block:** the writer removes both the legacy and the new relay
     block, then writes the legacy one (H9).
   - **Self-tests:** both identity prefixes are accepted.
   - **Tool marks:** the classifier accepts both executable paths (H7).
   - **Locks:** a lock file is acquired under the name for the container's
     layout. A container's layout never changes, so two releases always
     agree on the name.
   - **Environment:** the host sets both legacy and new wrapper variables
     (§ 6.4).
2. **Image layout 2 (sandbox image, PR 9).** It may ship in the same release
   as PR 8, never earlier.
   - **Helpers:** installed at the new paths, with **hard links** at the
     legacy paths. Hard links give one inode, so exec, the eBPF executable
     identity and the launch-root byte comparison all behave as before.
     Symlinks are not used.
   - **Wrappers:** `<cli>-cloud-agent`, with hard links under the legacy
     names. They read the new variables first and the legacy ones second.
   - **Provider id:** the wrappers declare `cloud-agent` and keep a
     `freeinference` provider entry with the same options. A native session
     created under the legacy id therefore still resolves on resume.
   - **Observer root:** the observer takes `--root`, with the legacy path as
     the default.
   - **Identity lines:** the helpers print the new identity lines.
   - **Labels:** the image carries `io.hybridinference.sandbox-layout="2"`.
   - **C identifiers:** the class (a) C identifiers move here.
3. **Host creates layout-2 containers (PR 10)** when the image declares
   layout 2.
   - **New paths:** new tmpfs root (passed as `--root`), `/etc/cloud-agent`,
     `/home/agent/.cloud-agent-platform`, `/var/lib/cloud-agent-broker`,
     `.cloud-agent-*` markers, and the new relay marker.
   - **Label:** the container gets `io.hybridinference.chat-layout=2`.
   - **Restore:** a restore takes the layout from the checkpoint or snapshot
     image's inherited labels, not from the host's default.
   - **Archive bundle:** final bundles of layout-2 containers name
     `paths/cloud-agent-platform.tar`. Each manifest already records the
     container and archive path of every entry, so `_FORMAT_VERSION` stays 1
     (H10).
   - **Ordering:** PR 8 must have been on every host for one release, so that
     R0 can operate layout-2 containers after a rollback.
4. **Remove layout-1 support** when the inventory reports no container,
   checkpoint image or snapshot image of layout 1. Use a data-bound
   `COMPAT(remove-after=2027-06-30)` marker. The legacy hard links in the
   image and the `freeinference` provider alias stay until then.
   `test_sandbox_manager_compat.py:120` then becomes a layout-1 assertion.

**Retained sandboxes and archives.**

- A retained layout-1 container is operated with layout-1 paths for life.
  Live member processes keep `/etc/freeinference` in their environment, and
  the host keeps installing the CA there.
- Checkpoint and snapshot images keep their layout through `docker commit`
  labels.
- An S3 final bundle is self-describing. No code in this repository reads
  bundles back. A future reader takes paths from the manifest.

### 6.4 Class (d): environment variables

1. **Reserve `CLOUD_AGENT_` (control plane, PR 11a).**
   - Before reserving it, list stored environment variables whose name starts
     with `CLOUD_AGENT_`, with a read-only query against the environment
     tables (`make test-db-local` for the code). If any exist, the owner must
     rename them before the host-side model starts refusing them (H11).
   - The control plane refuses new saves first. The host-side validation
     follows one release later.
2. **Set both wrapper names (host, PR 8).** The host sets the legacy and new
   wrapper variables.
3. **Drop the legacy wrapper names** when layout 1 is gone (§ 6.3, step 4),
   because old wrappers read only the legacy names.
4. **`FREEINFERENCE_GIT_COAUTHOR` needs no compatibility step.** The host sets
   it and nothing reads it: the platform instructions write the trailer value
   itself. Rename it to `CLOUD_AGENT_GIT_COAUTHOR`, or drop it, in any release,
   together with its assertion in `test_sandbox_github_auth.py` (PR 11).
5. **Keep the `FREEINFERENCE_` reservation.** It is harmless and stops an
   owner from recreating a name an old image still reads.

### 6.5 Class (e): runtime and helper image labels

1. **Image writes both forms (PR 6).**
   - The Dockerfile repeats each `LABEL` key under `io.hybridinference.` with
     the same value.
   - `test_sandbox_image_labels.py` and `test_cross_artifact_contracts.py`
     check that the two sets are identical.
   - The CI `sed` (`ci.yml:519`) is anchored to one namespace (H6).
2. **Readers read both forms and check that they agree (PR 4 for the host,
   PR 6 for release tooling).** The image is read only by the host,
   `release_manifest.py`, `publish_inputs.py`, `smoke.py` and
   `release_scripts.py`.
   - If both forms are present and differ, the image is refused, in the same
     way as "the image disagrees with itself" (`release_manifest.py:174-178`).
   - The manifest schema does not change. `runtime_catalog` is normalized, so
     a test asserts that a dual-labelled image produces byte-identical
     manifest output. Downstream needs no change.
   - Only the description text changes (`manifest.schema.json:5,144`,
     `release_manifest.py:3`, `deploy/release/README.md:210-212`). The README
     is a docs-site page, so that PR also runs `make docs-translate` and
     `make docs-verify` (AGENTS.md § 3).
3. **Image writes only the new form (PR 12).** Ordering: every host and
   control plane must read both forms. Hosts read runtime labels from the
   configured image, and from retained containers through their inherited
   labels. The second case is data-bound, so the host reads both forms until
   § 6.2, step 6.

### 6.6 Class (f): defaults, host machine state and CI

- **Kata epoch (PR 13).**
  - The installer migrates the state to `/var/lib/cloud-agent-kata-runtime`
    under the existing lock.
  - It leaves `/var/lib/freeinference/kata-runtime` as a symlink, so a
    sibling stack still on R0 keeps mounting the same epoch. Docker resolves
    a bind-mount source that is a symlink on the host.
  - The new compose file mounts the new path.
  - An R0 installer refuses the symlink: it opens path components without
    following links. This fails closed. Operators run the R1 installer.
  - The symlink carries
    `COMPAT(remove-after=2027-01-31): every stack on each machine mounts the new path`.
- **Defaults (PR 13).**
  - The default image becomes `cloud-agent-sandbox:latest`, in the
    `cloud-agent-sandbox` repository `application/compute_setup.py:370`
    already uses (that line tags it by profile id).
  - The `DEPLOY_ROOT` default changes only for new installations:
    `deploy.sh` keeps `/srv/freeinference-cloud-agent` when that directory
    exists, and otherwise uses `/srv/cloud-agent`.
  - The CA common name applies only to newly issued CAs. Nothing compares it.
  - The User-Agent changes.
  - The local defaults change. `dev.sh` only appends missing keys, so existing
    `.env` files keep their project, and with it their volumes.
  - `deploy/local/README.md` is a docs-site page and needs translation.
  - The CI compose project changes.
- **Archive bucket default (PR 14).**
  - Ordering: every host must advertise `sandbox-archive-destination-v1`,
    which removes the older-manager reduction (H12).
  - A data migration stores the current default explicitly for any
    deployment that never set a bucket.
  - The default then becomes empty, which leaves archiving off until
    configured.
  - Coordinate with the owners of the settings text and the TSX placeholder,
    and regenerate `openapi.json`.

## 7. COMPAT markers

| Code | Marker |
|---|---|
| Writing labels in both forms (container, network, image, runtime/helper labels) | `COMPAT(remove-after=2027-01-31): every host and control plane reads io.hybridinference.* labels (PR 3 and PR 4 released everywhere)` |
| Reading legacy labels, names and repositories from Docker objects | `COMPAT(remove-after=2027-06-30): every host's sandbox inventory reports legacy_identifiers == 0` |
| Layout-1 paths, legacy wrapper variables, legacy hard links, `freeinference` provider alias | `COMPAT(remove-after=2027-06-30): no container, checkpoint or snapshot image of sandbox layout 1 remains (inventory)` |
| Kata epoch symlink | `COMPAT(remove-after=2027-01-31): every stack on each machine mounts /var/lib/cloud-agent-kata-runtime` |

Dates assume at least two production releases between steps. The rollback
window dates follow the repository's existing pattern of a few weeks after
landing. Data-bound dates are review dates (D5).
`tests/unit/test_compat_markers.py` forces each review.

## 8. Pull requests

Each PR is one reviewable unit. "Requires" means the named PR has been
released on every host and control plane, not just merged.

| PR | Side | Content | Requires | Rollback-safe because |
|---|---|---|---|---|
| 1 | spec + host | This spec; class (a) tempfile prefixes, with a guard test | — | process-local |
| 2 | shared | `cloud_agent_shared/runtime_identifiers.py`: namespaces, `legacy_key()`, `canonical_labels()`, `dual_labels()`, name prefixes; (the per-file budget for `freeinference` occurrences that may only fall already exists: `tests/unit/test_distribution_neutrality.py`, added with this spec) | — | no behavior change |
| 3 | control plane | Release scripts read both forms (§ 6.2, step 2) | — | only reads |
| 4 | host | Host reads both forms of labels, names, image references and runtime/helper labels; inventory count (§ 6.2, step 1; § 6.5, step 2) | 2 merged | only reads |
| 5 | host | Labels written in both forms (§ 6.2, step 3) | 4 merged | R0 still finds every legacy key |
| 6 | image + release | Runtime/helper labels in both forms; CI `sed`; manifest reader; description text; docs translation | 4 merged | R0 reads legacy |
| 7 | host | New names for new objects (§ 6.2, step 4) | 4 | R0 = PR 4 reads new names |
| 8 | host | Layout-aware host and both sets of wrapper variables (§ 6.3, step 1) | 2 merged | still creates layout 1 |
| 9 | image | Sandbox layout 2 (§ 6.3, step 2), C identifiers | 8 merged (same release or later) | legacy hard links; R0 hosts operate it as layout 1 |
| 10 | host | Create layout-2 containers (§ 6.3, step 3) | 8, 9 | R0 = PR 8 operates layout 2 |
| 11 | control plane, then host | Reserve `CLOUD_AGENT_`, after checking stored names; rename or drop the unread coauthor variable (§ 6.4) | — | additive |
| 12 | host + image | Write only the new form; set legacy keys empty | 3, 4, 5, 6 | every supported reader reads both forms |
| 13 | deploy | Kata epoch path, defaults, local development, CI, CA name, User-Agent (§ 6.6) | — | symlink and legacy detection |
| 14 | control plane | Archive bucket default (§ 6.6) | every host advertises `sandbox-archive-destination-v1` | migration stores the old default |
| 15+ | all | Remove compatibility code as each § 7 condition holds | inventory | — |

PR 3, 4, 8 and 11 can be reviewed in parallel. PR 12 and later are gated on
releases.

## 9. Testing

| Class | Unit (in the PR) | External, Docker (`-m external`) |
|---|---|---|
| a | Guard test that no `tempfile` prefix under `apps/backend` names FreeInference; existing module tests | — |
| b | Every legacy constant maps 1:1 and no two suffixes collide; `verify_identity` with legacy-only, dual, new-only and inherited-stale labels (`test_chat_sandbox.py`, `test_agent_sandbox_manager.py`); reservation with both names (`test_agent_runner.py`, `test_runner_restore_handback.py`); network adoption and pruning (`test_chat_group_network.py`, `test_chat_group_network_prune.py`); listings run per namespace (`test_idle_host_rules.py`, `test_runtime_downloads.py`, `test_sandbox_checkpoints.py`, `test_environment_snapshots.py`); release scripts (`test_compute_host_bootstrap.py`, including an `io.hybridinference.artifact.role` work container and a new-namespace chat container that still has a stale work label) | New `tests/external/test_runtime_identifier_dual_read.py`: create legacy-labelled and legacy-named containers, networks and `freeinference-checkpoint/*` and `freeinference-environment/*` images with real Docker, then assert discovery, `verify_identity`, network adoption, idle rules and the runtime menu. Also run `test_host_release_real_docker.py` with fixtures in both namespaces, `test_checkpoint_restore_roundtrip.py` restoring an archive taken under the legacy repository, `test_environment_snapshot_roundtrip.py` and `test_host_move_out_two_nodes.py` |
| c | Path selection by image and container layout; `test_kata_observer_readiness.py` (both identity lines, both relay blocks, a stale block removed); `test_guest_observer.py` (classifier with both paths); `test_sandbox_archive_bundle.py` (final entry per layout; **the pinned digest of an initial bundle is unchanged**); `test_codex_plugins.py`, `test_plugin_installation.py`, `test_shared_session_supervisor.py`, `test_codex_private_home_fork.py` (markers per layout); `test_deploy_config.py`, `test_other_runtime_plugins.py`, `test_platform_instructions.py` (wrappers read new then legacy variables; provider alias); `test_launch_root_probes.py`, `test_sandbox_python_isolation.py` | Run the new host against the **previous release's published sandbox image**, resolved the way `scripts/check_rolling_upgrade.py --base` resolves the base: `test_runtime_stream_conformance.py`, `test_resident_mcp_parity.py`, `test_tool_hook_passivity.py`, `test_shared_member_permissions.py`. Restore a layout-1 checkpoint with a layout-2 host (`test_checkpoint_restore_roundtrip.py`). Resume a native OpenCode/Kilo/pi session across the provider rename (`test_agent_mcp_end_to_end.py`, `test_mid_turn_input_conformance.py`) |
| d | `test_sandbox_github_auth.py`, `test_git_identity.py`, `test_platform_instructions.py`, `tests/support/env_inventory.py`, environment validation of the reserved prefix; `make test-db-local` for the stored-name check | `test_cli_inference_variables.py` |
| e | `test_sandbox_image_labels.py`, `test_cross_artifact_contracts.py`, `test_runtime_version_pins.py`, `test_agent_sandbox.py`, `deploy/test_release_manifest.py` (byte-identical manifest from a dual-labelled image), `deploy/test_publish_inputs.py`, `deploy/test_artifact_smoke.py`; `make docs-verify` | CI image build and release smoke |
| f | `test_setup_kata_runtime.py`, `test_kata_runtime_epoch.py`, `deploy/test_artifact_compose.py`, `test_sandbox_stack_project.py`, `test_local_dev_script.py`, `test_deploy_config.py`, `test_host_pool_api_archive_bucket.py`; `make docs-verify` for `deploy/local/README.md` | `test_host_release_real_docker.py` (inventory with the new Kata path) |

## 10. Risks and open questions

1. **Legacy containers may never disappear.** Groups have no retention limit,
   and Docker cannot relabel a container. If the inventory count stays above
   zero, choose between two options:
   - keep the legacy readers permanently (small, tested code);
   - add a maintenance action that recreates a group as its next generation
     (stop → checkpoint → recreate), which drains legacy containers.

   Decide after PR 4 has produced real counts.
2. **Container names.** This spec keeps legacy names for existing groups (D2).
   Renaming at recreation would require a fenced control-plane transition
   that updates `agent_sandboxes.container_name`, which is UNIQUE. It was not
   chosen.
3. **Provider id in native sessions.** The alias rests on one invariant: a
   session never changes image, because runtime downloads install CLIs, not
   wrappers. If that ever changes, keep the `freeinference` provider alias
   permanently.
4. **No coordination with visible text.** The platform instructions name no
   environment variable, so the visible-text change and these renames are
   independent. An earlier draft assumed the instructions named
   `FREEINFERENCE_GIT_COAUTHOR`; they write the trailer value itself.
5. **Downstream.** Downstream code is unaffected (§ 3.5). Release notes should
   tell operators that `docker` commands filtering on
   `org.freeinference.sandbox-namespace` must use both namespaces from PR 5,
   and only the new one from PR 12.
6. **`CLOUD_AGENT_` is unreserved today**, although the image sets platform
   variables under it. This gap already exists. PR 11 closes it, with the
   stored-name check from H11.
7. **Docs site.** Edits to `deploy/release/README.md` and
   `deploy/local/README.md` need Chinese catalog entries and
   `make docs-verify`. Specs under `docs/agents/` do not.
