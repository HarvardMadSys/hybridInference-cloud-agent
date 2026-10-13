# Compute resources and the global sandbox location

Use Admin → Servers to deploy existing SSH machines or CloudLab allocations,
manage their leases, and choose where new sandboxes run. Durable operations and
renewal run in the control plane, so closing the browser does not stop them.
The [compute lifecycle design](specs/2026-10-04-compute-resource-lifecycle-design.zh-CN.md)
records the underlying protocol; [ENVIRONMENT.md](../ENVIRONMENT.md#optional-compute-resource-management)
lists its configuration.

The global location applies to **all new, still-unassigned sandbox groups**.
Retained groups, including stopped ones and their Fork members, keep their
original Host. `Automatic` clears the global restriction. An explicitly
selected unavailable Host keeps work waiting; it never triggers a fallback.

## What must remain on stable infrastructure

Keep the control plane, Postgres, background worker, deployment profiles,
provider credentials and compute encryption key on stable infrastructure. A
managed resource runs only the Host, sandbox manager, tunnels and sandboxes.
Provider expiry remains a hard limit; renewal does not guarantee uptime.

Stored API tokens and management SSH keys are encrypted with
`AGENT_COMPUTE_SECRET_KEY`. The supported deployer generates a random 32-byte
value in its persistent `.env` when none is configured; other deployment
systems must provide equivalent stable key material. Back it up with the
encrypted database: restoring the database without this key cannot recover
compute connections. Do not reuse gateway, session, enrollment or sandbox manager keys.
Compute management credentials never enter sandboxes or the deployed Host.

The Host separately needs purpose-scoped tunnel credentials and
`AGENT_SANDBOX_MANAGER_TOKEN`. The sandbox manager token is currently pool-wide: every
deployment profile must use the same value as the stable control plane.
Management SSH keys and sandbox manager authentication serve different purposes.

## Supported initial deployment

Automatic SSH bootstrap supports:

- Ubuntu 22.04 or 24.04 on x86-64.
- Python 3, passwordless `sudo` and working KVM.
- Reachable package repositories and the pinned Kata release when installing
  prerequisites.

With `install_prerequisites=true`, bootstrap installs `docker.io`,
`docker-compose-v2`, `curl`, `ca-certificates`, `xz-utils` and `nftables`,
starts Docker, and runs the repository's checksum-pinned Kata installer.
With it false, Docker/Compose and the approved Kata runtime must already exist.
Both paths require Kata.

The node does not need Cloud Agent preinstalled. Bootstrap transfers a reviewed
source bundle and configuration, then runs `deploy/host/sandbox_host.sh up`
and `check`. These checks verify Kata, the guest observer, sandbox manager, relays and
nftables. Admission stays closed until deployment checks, an enrolled Host
heartbeat and a control-plane-to-sandbox-manager health check succeed.

Automatic initial deployment supports one node per allocation. Multi-node
CloudLab experiments can be observed, but automatic deployment refuses them
rather than selecting one node. Failed or unsupported installation leaves the
resource visible and does not destroy it.

### Preferred CloudLab node types

New CloudLab allocations try the following default list from top to bottom.
These general-purpose families were introduced in 2021 or later, have at least
16 physical cores and 128 GiB RAM, and each has more than 20 total nodes.
Hardware and totals were checked against the [CloudLab hardware catalog](https://docs.cloudlab.us/hardware.html)
and [public inventory](https://www.cloudlab.us/portal-hardware.php) on October 12, 2026.

| Preference | Type | Introduced | Total nodes | Physical cores | RAM (GiB) |
|---|---|---|---|---|---|
| 1 | `r6615` | 2025 | 80 | 32 | 192 |
| 2 | `c6620` | 2025 | 132 | 28 | 128 |
| 3 | `r650` | 2022 | 32 | 72 | 256 |
| 4 | `r6525` | 2022 | 32 | 64 | 256 |
| 5 | `c6525-100g` | 2021 | 36 | 24 | 128 |
| 6 | `c6525-25g` | 2021 | 144 | 16 | 128 |

Change the ordered list in **Admin → Settings → CloudLab**, or override it for
a particular server or autoscaling policy. The list is saved with each new
allocation. Currently free counts are shown separately and do not change the
preference order. A confirmed capacity rejection advances to the next type;
an uncertain response pauses acquisition for review. If all types lack
capacity, the operation waits and retries from the top. An explicit nonempty
`phystype` profile parameter selects only that type; clear it to resume ordered
selection. Profiles with a different hardware parameter can use
`node_type_binding`; an empty list uses the profile's own choice.

### Sandbox storage path

When adding or creating a server, enter **Sandbox storage path**, for example
`/mydata`. This must be an existing writable mounted data disk on the remote
server.
Bootstrap stores sandbox workspaces and native session data in `<path>/docker`
and `<path>/containerd`, binding these directories to the daemons' standard
`/var/lib/docker` and `/var/lib/containerd` paths. The server overview shows the
saved choice. Observation-only imports do not need a storage path.
Attachment preflight checks the selected disk without changing the host;
deployment checks it again before preparing storage.

The path is saved with the allocation and reused on retries and replacement
nodes, where the same mount must exist. A missing or read-only mount, symlink or
conflicting existing store blocks setup. Any nonempty Docker or containerd store
outside the selected mount also blocks setup. New hosts require empty `docker`
and `containerd` directories on the selected disk. Deployment retries can reuse
nonempty directories only when the existing deployment identity matches and
both daemon directories are already bound to those same storage directories.
Retained data with missing binds requires operator repair before retrying.
Existing `/etc/fstab` entries for either daemon directory must match the selected
storage bind; conflicting or duplicate entries block setup before host changes.
Native systemd mount units, masks, or mount overrides for these directories also
block setup; ordinary `fstab`-generated mount units remain supported.
Preflight does not query or start these daemons, and bootstrap never deletes or
moves existing sandbox data to a different disk. Docker and containerd must use
their default `/var/lib/docker`
and `/var/lib/containerd` data directories. Setup rejects custom daemon storage
configuration before restarting services. A host installed by hand uses the
same binds, `/etc/fstab` lines and systemd drop-ins; see
[Retained Docker storage on a host](operations.md#retained-docker-storage-on-a-host).
Daemon startup requires the configured disk and bind mount after
a reboot. Older API clients that omit `sandbox_storage_path` continue to
use the operator profile's `data_mount` configuration.

## Prepare a trusted deployment profile

### Automatic setup

Open **Admin → Servers → Server setup** and use **Automatic setup**. The
control-plane image includes the Host source bundle for its own release; the
application supplies the revision, environment and existing sandbox manager
token, generates three separate tunnel keys per setup slot, and discovers the
control-plane listener ports and Docker bridge.

Supply the infrastructure inputs once:

1. Add and validate management SSH connections for the stable gateway machine
   and control-plane machine, including independently verified host keys.
2. Select those connections and confirm the machines' SSH addresses and users.
   The gateway address defaults to the saved Gateway API URL's hostname
   (for FreeInference, `freeinference.org`). The supported control-plane deployer
   supplies the service node's FQDN as the control-plane address. Override these
   with `AGENT_GATEWAY_SSH_HOST` and `AGENT_CONTROL_PLANE_SSH_HOST` when SSH uses
   different addresses; other deployers must supply the control-plane value.
   Saved and pending setup inputs take precedence over these first-time defaults.
   Both machines need Python 3 and passwordless `sudo`. The control machine
   must run the supported Docker Compose deployment, and the gateway must
   already listen on the selected inference port.
3. Enter an approved private IPv4 pool for chat networks, a reachable DNS
   resolver outside that pool, and the protected infrastructure CIDRs. These
   values depend on your network and require an administrator's decision.
4. Choose the number of setup slots and prepare the setups. Two slots allow
   separate sandbox manager routes for a current node and its replacement.

The application creates dedicated restricted SSH accounts on the two stable
machines. Inference and MCP credentials permit only their respective local
service destinations; the control credential permits the main API and its
assigned reverse sandbox manager route. Management credentials stay in the control
plane. Existing SSH policies must permit these restricted accounts; setup
refuses incompatible policies instead of broadening them. Co-located gateway
and control services must use separate inference, MCP and main API ports.

Generated keys and configuration are encrypted in Postgres before remote
authorization begins. A setup becomes selectable only after all its slots
finish. If a request fails or the browser closes, reopen Server setup and retry
the pending setup to reuse its exact credentials and routes. Infrastructure
defaults are saved for future setups. **Start new setup** retains those inputs
and creates a new request on submission; it leaves an unfinished setup
unavailable. Tunnel credentials, the install directory, and enrollment stay
fixed. A control-plane release rolls managed hosts forward: stored profiles
that share the previous commit and bundle, and the live allocations using
them, advance together so a later node replacement still sees one revision.
A rollout that takes the running generation down and then fails restores the
previous stored bundle. File-backed profile files are not rewritten.

Automatic setup requires a control-plane image built with its full release
revision, the compute encryption key and the existing sandbox manager token. The form
shows missing prerequisites. Rebuild or update older images to include the
Host bundle, or use advanced manual setup.

### Manual and file-backed setup

**Advanced manual setup** retains the existing form: upload a reviewed source
archive, enter its exact 40-character revision and installation directory, paste the
Host environment, and provide the three tunnel keys and verified known-hosts
files. The control plane validates the archive, encrypts all environment and
credential values, and stores them in Postgres. Profile read APIs expose only
IDs and labels, never generated credentials. Create a new profile to replace
tunnel credentials, the install directory, or the Host environment. A
control-plane release moves the commit of stored profiles that still pin the
previous bundle; it does not rewrite a file-backed profile.

File-backed configuration remains supported for existing deployments and
recovery. Start with these operator templates:

- [`compute.example.json`](../deploy/control-plane/compute.example.json)
- [`compute-host.env.example`](../deploy/control-plane/compute-host.env.example)
- [Host field descriptions](../deploy/host/.env.example)

Copy them to a private configuration directory. Replace every example address,
token, network and all-zero digest/revision. Keep these files outside Git.
Secret files must be readable by the control-plane container's UID 10001 and
not world-readable; mount the directory read-only.

The Servers form selects the first available profile in configuration order.
API clients may select a profile ID. Requests cannot provide scripts, source
paths, Docker flags or environment values. Encrypted compute connections are
referenced by connection ID. Operator-mounted deployment secrets use the separate
`deployment:<name>` namespace; deployment files cannot reference compute
connections.

Build a bundle from the reviewed commit that the control plane permits Hosts
to advertise. All components must use the same release revision:

```sh
COMPUTE_DEPLOY_SHA=$(git rev-parse HEAD)
git archive --format=tar.gz --output=/private/compute/bundles/host.tar.gz "$COMPUTE_DEPLOY_SHA"
shasum -a 256 /private/compute/bundles/host.tar.gz
```

Set the profile fields as follows:

| Field | Value |
| --- | --- |
| `deployment_sha` | Full 40-character revision. |
| `source_sha256` | Printed SHA-256 digest. |
| `source_bundle` | Absolute bundle path visible inside the control-plane container. |
| `deploy_root` | Dedicated installation directory on the remote node. |

The digest is verified before upload. The archive must contain the reviewed
repository root, without a containing directory or working-tree secrets.
Bootstrap rejects symlinks, path traversal and private deployment state, and
adds `.dockerignore` exclusions to keep tunnel files out of image build contexts.

The first installation writes a marker binding `deploy_root` to its
allocation, node and Host. Retries reuse the marker and persisted enrollment
credential. A different marker or an unmarked existing installation is refused.
Manually enrolled Hosts remain manageable through the original interface;
bootstrap does not adopt their files.

Each present allocation reserves a profile because the sandbox manager reverse endpoint
is fixed. Overlapping old/new Hosts need separate profiles with different
reverse ports, installation directories and tunnel authorizations. Renaming a
profile while reusing its endpoint does not separate it. Selection skips live
reservations; only confirmed `terminated` or `absent` allocations free them.
Identical retries retain their operation and selected profile. The allocation
transaction also checks occupancy, so concurrent additions cannot share one.
Refresh availability before retrying a refused addition.

Choose `AGENT_EGRESS_CHAT_NETWORK_POOL`, the resolver and protected CIDRs for
the actual deployment; the sample range is illustrative. Preserve the release's
per-group nftables policy. Environment values must be single-line dotenv data;
they are never sourced as shell code. Supported substitutions are
`{deploy_root}`, `{host}`, `{node_id}` and `{allocation_id}`.

### Three tunnels and the sandbox manager route

Provision tunnel authorization on the stable gateway/control-plane machines
before adding a server. Bootstrap cannot grant access to them. Use three
distinct keys with no shell authority and narrowly permitted forwarding:

| Tunnel | Remote destination | Audience |
| --- | --- | --- |
| Inference | Gateway inference port, default `127.0.0.1:8080` | Runner and sandbox relay |
| MCP | Control plane's **MCP-only** listener, default `127.0.0.1:8001` | Sandbox tool relay |
| Host control | Main API, default `127.0.0.1:8000` | Trusted runner only |

The Host control connection also reverse-forwards from the stable
control-plane machine to `sandbox-manager:8092`. Bind its listener to the
actual control-plane Docker bridge gateway address, never loopback or
`0.0.0.0`. Configure `GatewayPorts clientspecified`, restricting both the
key's reverse listen address/port and forward destination; never `yes`, which
binds every remote forward to the wildcard address. Firewall the reverse ports
on that machine so they answer only on its Docker bridge interface
([sandbox manager route](operations.md#the-way-back-the-control-plane-reaching-this-hosts-sandbox-manager)).
Set
`AGENT_SANDBOX_MANAGER_ADVERTISE_URL` to the resulting URL reachable from the
control-plane container. Add its hostname/IP to
`AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS` when that allowlist is enabled. Every
simultaneously deployed Host needs a distinct reverse port.

Verify existing-server and tunnel-server host keys through a trusted source
before adding them to their respective `known_hosts` files. `ssh-keyscan`
collects a candidate key; it does not authenticate it. Those transports use
strict host-key checking, isolated keys, no ambient SSH configuration or agent,
and no trust-on-first-use.

CloudLab VMs use the manifest's SSH port and, by default, the control plane
connects without verifying the VM host key. This permits automatic setup but is
vulnerable to machine-in-the-middle impersonation of the CloudLab node. Select
**SSH host-key checking → Registered by the node at first boot** when the
Cloud Agent profile is available and host identity must be pinned.

Unchecked intent is stored in a shape the immediately previous release can
read, but that release deliberately fails closed when an SSH-dependent action
encounters it. After an application rollback, existing resource observation,
lease and provider operations remain readable; bootstrap, inventory or
retirement that needs SSH must wait until this release is restored. Redeploy
forward before retrying the operation rather than replacing the marker with
unverified key material.

### CloudLab nodes that register their own host keys

For automatic replacement, select **SSH host-key checking → Registered by the
node at first boot** in the CloudLab server's advanced options. This applies
only to
experiments created by this control plane from the Cloud Agent profile:

1. Publish [`deploy/cloudlab/cloud-agent-node.py`](../deploy/cloudlab/cloud-agent-node.py)
   as a **project-private** CloudLab profile. Set its project and name as the
   server template. Its node, image and data-disk parameters replace
   `small-lan`'s `phystype`, `osImage`, `dataMount` and `dataSize`.
2. Provide a public HTTPS control-plane address through
   `AGENT_SESSION_REDIRECT_URI` or `AGENT_COMPUTE_REGISTRATION_URL`.
3. Each acquisition sends a one-time token in experiment parameters. The control
   plane stores only its hash; it expires after two hours. At first boot, the
   node generates fresh SSH host keys and posts their public halves to
   `/v1/compute/node-registrations` over HTTPS. The control plane pins the
   strongest registered key and uses strict checking against that key alone.

The token registers keys once; it cannot claim work, enroll or rename a Host.
Repeating the same keys succeeds, different keys are refused, and a newer token
from a resubmitted acquisition supersedes the old token. CloudLab project
members can read experiment parameters, including the token, and already
administer all project nodes. Keep the profile private and restrict project
membership.

If an operation stays at `waiting_for_registration` and ends with
`registration_expired`, inspect `/var/lib/cloud-agent-node/registration`
through the CloudLab console and verify that the node can reach the registration
URL. `registration_unavailable` means no public HTTPS control-plane address
is configured.

## Enable legacy file-backed profiles

Admin-managed profiles require no compute directory or JSON configuration.
CloudLab deployments still set the verified Portal API URL; existing-SSH
deployments do not. The variables below are retained for deployments that
continue to mount profiles and credential files.

Set these values in the private deployment environment:

```text
AGENT_COMPUTE_DIRECTORY=/private/compute
AGENT_COMPUTE_SECRET_KEY=<leave blank for deploy.sh to generate, or supply a stable secret>
AGENT_CLOUDLAB_API_URL=<operator-verified HTTPS Portal API base URL>
```

Place `compute.json`, `bundles/host.tar.gz` and `secrets/` under the absolute
host directory named by `AGENT_COMPUTE_DIRECTORY`. In source and packaged
artifact deployments, `deploy/control-plane/deploy.sh` automatically adds
`docker-compose.compute.yml` when this variable is in the environment or
persistent `.env`. The overlay mounts the directory read-only at `/run/compute`
and sets `AGENT_COMPUTE_CONFIG=/run/compute/compute.json` on the main API.
The MCP-only listener receives neither the mount nor compute credentials.

Manual Compose commands must explicitly include
`-f deploy/control-plane/docker-compose.compute.yml`. Other deployment systems
may set `AGENT_COMPUTE_CONFIG` and supply equivalent read-only mounts. The image
includes `openssh-client`; the control plane receives no Docker socket or root
privilege. Apply database migrations before starting the worker.

Existing-SSH deployments can leave `AGENT_CLOUDLAB_API_URL` unset. CloudLab has
no guessed endpoint default: the public specification's development HTTP example
is unsuitable for credentials. Verify the production HTTPS Portal API base
with the provider or deployment documentation, then run **Connections →
Validate**. Validation makes a read-only authenticated experiment-list request,
checks its response shape and refuses redirects. Local downloaded tokens are
never uploaded automatically.

Provider references:

- [CloudLab Portal API manual](https://docs.cloudlab.us/advanced-topics.html)
- [Portal API OpenAPI source](https://gitlab.flux.utah.edu/emulab/portal-api/-/blob/master/openapi.json)

## Day-to-day workflow

Use **Admin → CloudLab** to set the project, initial duration, template project,
template name, SSH user, and the SSH public key installed for that user. These
are non-secret defaults only; an administrator can change them for one
allocation. Leave the public key blank to use keys already on the CloudLab
account.

1. In **Admin → Servers**, choose **Add server** and select existing SSH or
   CloudLab.
2. Create or select credentials inline. Existing SSH needs a private key and
   verified node host keys. CloudLab needs an API token and a management SSH
   private key; its advanced options can replace the default unchecked host with
   first-boot host-key registration. Credential responses expose metadata only.
3. Enter the resource details. Existing SSH needs an address, port and user.
   CloudLab needs an exact experiment UUID to attach, or a profile and explicitly
   selected project to provision, plus its SSH user. A public key set with that
   user is sent as the experiment's additional SSH key when the experiment is
   created.
4. Submit and follow the durable operation until ready. HTTP 202 confirms only
   that the operation was recorded; allocation, installation, enrollment and
   readiness are separate steps.
5. Select the ready Host as the **global sandbox location** if desired. Becoming
   ready does not change the saved location. New unassigned groups follow it;
   retained groups remain on their original Host.
6. Review the allocation's complete scope before enabling renewal. Attached or
   imported resources require explicit lifecycle adoption. Supply a real reason
   and ordered lead times. Confirm extensions from observed expiry and the last
   result; saving policy alone does not extend a lease.

Observation-only imports record resource facts without installation. After
explicit adoption they can support lease management, but observing nodes does
not establish that their retained state is disposable. Release still requires
active Host bindings and verified inventory.

## Recovery and release boundaries

Reuse the recorded operation after browser disconnects or worker restarts. It
persists request ID, exact parameters, step and resource ID. A new request ID
creates a new command.

| Result | Recovery |
| --- | --- |
| SSH key or identity refused | Verify the machine/key, update the selected connection and retry the same operation. Existing-SSH and registered CloudLab connections keep host-key checks enabled. |
| Cloud create outcome unknown, no UUID saved | Identify the actual experiment in the exact account/project and resolve explicitly. The worker never repeats POST or adopts an experiment by name. |
| UUID saved, later observation failed | Repair access and resume the same resource. |
| Installation or readiness failed | Inspect deployment checks, repair prerequisites/profile and retry the same resource. Failure does not destroy it. |
| Extension pending or unknown | Check actual expiry. Retries retain the fixed absolute target; shorter extensions are not successful. The provider may reject or limit extensions. |
| Retained state remains | Keep the resource available or manage its chat lifecycle. Stopped containers and archived chats remain dependencies. |
| Release committed, response unknown | Keep admission closed and confirm the exact immutable resource ID. Cancellation cannot undo release. |

A CloudLab DELETE response of `204` confirms deletion of the exact experiment
UUID. A terminal experiment object with the matching UUID and project also
provides evidence. A `404` alone does not: token visibility can change even
when an authenticated list succeeds. The adapter reports
`resource_visibility_unknown` and keeps the reservation. After a lost DELETE
acknowledgement followed by `404`, the operation remains committed pending
provider confirmation; it neither reports success nor frees the profile.
Restore access and retry the existing operation. Do not bypass fences or edit
database state to infer deletion. The public API has no project-permission
query that strengthens this evidence.

The reconciler can classify a missing experiment as `expired_not_found` after
a previously recorded lease deadline plus ten minutes. Unknown or unexpired
deadlines leave absence ambiguous.

Enabled renewal continues while retirement waits on retained state. Renewal
does not clear manual Drain or resource-owned admission holds. The claim
transaction enforces the last known hard lease cutoff even if the resource
worker stops.

Pool and resource actions have different effects:

| Action | Effect |
| --- | --- |
| `Remove from pool` | Revokes the Host credential; leaves the machine intact. |
| `Forget` | Removes an eligible history record. |
| `Retire/release` | Releases the complete allocation; for CloudLab, the whole experiment. Existing SSH has no cloud-destruction capability. |

Release requires complete scope, no global placement reference, no running,
in-flight or retained dependencies, and fresh sandbox manager and machine inventory.
Stopped or unrecognized containers, orphan networks/volumes, offline sandbox managers
and incomplete inventory block it. There is no force bypass.

## Automatic CloudLab node replacement

Replacement transfers one entire Host, including stopped sandboxes and native
sessions. It preserves the Host name and sandbox namespace and enrolls a fresh
Host credential. It does not rebuild context from transcripts; the control
plane and database remain on stable infrastructure.

Before enabling replacement:

1. Use a **provisioned, managed** single-Host CloudLab allocation with
   `host_key_source=registration` and the private Cloud Agent profile.
2. Prepare two alternating deployment profiles with distinct tunnel keys and
   reverse sandbox manager ports. Both must pin the same `deployment_sha` and
   `source_sha256`; replacement never selects a profile from another release.
   A control-plane host rollout advances every stored profile and live
   snapshot that still pin that pair, including the spare, so the two slots
   stay on one revision. A profile another deployment is installing is left
   until its own rollout.
   Keep Docker/containerd versions compatible. Set `data_mount` to the
   profile's local disk, for example `/mydata`; both daemons use it through
   bind mounts.
3. Enable renewal and replacement in Servers → renewal settings. Supply the
   renewal reason and candidate hardware types in preference order. UI defaults
   are a **180-minute replacement lead time** and **7-day maximum node age**.
   Saving policy does not request an immediate move.
4. Set `AGENT_ALERT_WEBHOOK_URL` for external operational alerts. Without it,
   events remain in structured logs and operation status.

Within the renewal window, extension is tried first. Refusal or unconfirmed
extension starts replacement; maximum-age nodes move instead of extending.
Failed candidates are released before trying the next type. Configuration or
credential errors stop immediately; the same failure class across three types
stops with an alert. Retry resumes the saved operation.

The candidate installs the host recipe and receives a live pre-copy without
enrolling or claiming. Once it is prepared, the source waits for an idle moment.
At the drain deadline, new claims pause even if the candidate is still preparing.
Running turns finish while new messages queue; urgent moves never kill an active
turn. With the default one-hour turn limit, this deadline is 100 minutes before
expiry.

The worker stops the source Host and container daemons, copies final changes,
transfers the database binding, starts the replacement and verifies deployment,
fresh heartbeat and sandbox manager access. The stop takes the runner first,
then the sandbox manager alone for up to 10 minutes: its shutdown stops each running
sandbox cleanly and, when lifecycle archives are enabled, stores its final
archive. Remaining containers stop afterwards. A sandbox that Docker stopped
without the sandbox manager gets no final archive for that stop, but its chat continues
on the replacement. Claims resume only after successful takeover
is recorded durably. Recovery after that boundary can clean up the source; it
cannot restore its stale copy over new work.

Before confirmed takeover, failures restore the source. If the replacement
cannot be confirmed stopped, claims stay paused and the operation blocks for
attention. Cancellation is available before the stop begins; later failures
use recovery/retry. Cleanup failures retry without interrupting the replacement
and remain visible as failures. Source release still passes retained-state and
allocation guards.

### Acceptance before enabling production traffic

Use disposable staging conversations and dedicated profiles. Record operation
IDs, source/replacement node identities, release revision and observed pause
without credentials.

- Create a node through the deployed registration endpoint and verify its pinned
  key is used for SSH. A boot-script-only probe does not cover this path.
- Run Claude Code, its Fork and Codex with files and distinct context markers.
  Use **Replace node now** and verify native continuation, Fork separation,
  files and container IDs. Repeat across CloudLab sites.
- Submit a message during paused claims; it must queue and run once after
  takeover. Verify the source can no longer authenticate after re-enrollment.
- Exercise refused renewal, an unavailable first hardware type, failed final
  copy, failed target startup and a control-plane restart. Verify saved-step
  rollback/continuation, failed-candidate release, and absence of duplicate
  allocations or simultaneous serving copies.
- Run policy for **two weeks with at least two automatic moves**, checking
  alerts, cleanup and retained chats. Unit tests and a manual migration do not
  replace elapsed-time acceptance.

The [2026-10-10 manual staging drill](specs/2026-10-10-cloudlab-node-rotation-design.zh-CN.md#验证二结果2026-10-10staging)
moved real sessions from Clemson to Utah with a 161-second pause. It preceded
the automatic workflow. Abrupt loss of the only source disk or unavailable
replacement capacity before expiry can still lose sessions; keep separate
off-node backups where required.

Before live traffic, verify the intended CloudLab profile through deployment,
a real task, retained continuation and Fork on the same Host. Renewal success
and unit tests do not prove the profile's virtualization or network suitability.
Adapter tests mock HTTP/SSH and neither contact CloudLab nor spend its resources.
