# Compute resources and the global sandbox location

This runbook covers the administrator Servers page, the existing-SSH and
CloudLab adapters, and the control-plane resource worker. The lifecycle design
is [the compute resource design](specs/2026-10-04-compute-resource-lifecycle-design.zh-CN.md).

The global location controls **all new, still-unassigned sandboxes**. Switching
it does not move a retained sandbox, including a stopped one. Existing chats
and their Fork members continue on their original Host. `Automatic` clears
the global restriction; an unavailable explicitly selected Host never silently
falls back to another machine.

## What must remain on stable infrastructure

Keep the control plane, Postgres, compute encryption key, deployment profiles,
provider credentials and background worker on stable infrastructure. The
resource being renewed only runs the Host, workspace broker, tunnels and
sandboxes. Closing the browser does not stop renewal. Expiry is a provider
limit, not a promise of perpetual uptime.

The service stores API tokens and management SSH keys encrypted using
`AGENT_COMPUTE_SECRET_KEY`. Supply at least 32 bytes of independent random
material through the existing deployment secret mechanism and back it up
alongside the encrypted database. Do not reuse gateway, session, enrollment or
broker keys. Losing this key makes stored compute connections unreadable;
restoring only the database is insufficient. No compute credentials are copied
to a sandbox or to the deployed Host.

The Host still needs its existing purpose-scoped tunnel credentials and the
pool's existing `AGENT_WORKSPACE_BROKER_TOKEN`. These are separate from the
management SSH key used to install it. The current broker token is pool-wide:
the template value must match the stable control plane's value. This feature
does not introduce per-host broker authentication.

## Supported initial deployment

The shared SSH bootstrap supports Ubuntu 22.04 or 24.04 on x86-64, Python 3,
passwordless `sudo`, and working KVM. With `install_prerequisites=true`, it
installs the Ubuntu `docker.io`, `docker-compose-v2`, `curl`, `ca-certificates`,
`xz-utils` and `nftables` packages, starts Docker, then invokes the repository's
checksum-pinned Kata installer. Package repositories and the pinned Kata
release must be reachable. With the flag false, Docker/Compose and the approved
Kata runtime must already be installed. This flag never switches to plain
Docker isolation.

A bare SSH-accessible supported node does not need Cloud Agent preinstalled.
The bootstrap transfers the trusted source bundle and configuration, runs
`deploy/host/sandbox_host.sh up`, then `check`. The existing recipe remains the
authority for Kata, guest observer, broker, relay and nftables verification;
there is no alternative reduced-security deployment path. Control-plane
admission remains closed until those checks, enrolled Host heartbeat and a
control-plane-to-broker health check succeed.

Initial automatic deployment supports one node per allocation. Multi-node
CloudLab experiments can be observed in full, but attempting automatic Host
deployment does not silently adopt only one node. A failed or unsupported
installation leaves the resource visible and does not automatically destroy it.

## Prepare a trusted deployment profile

The browser only selects a profile ID. It cannot supply scripts, source paths,
Docker flags or environment values. Start from:

- [`compute.example.json`](../deploy/control-plane/compute.example.json)
- [`compute-host.env.example`](../deploy/control-plane/compute-host.env.example)
- the full field descriptions in [`deploy/host/.env.example`](../deploy/host/.env.example)

Copy these to the control-plane deployment's private configuration directory.
Replace every example address, token, network and all-zero digest/revision.
Keep the resulting files outside Git. Secret files must be readable by the
control-plane container's UID 10001 and not world-readable; mount the directory
read-only. An encrypted compute connection is referenced by its connection ID;
a deployment secret uses a separate `deployment:<name>` namespace and an
operator-configured file. Deployment files cannot reference compute connections.

Build the source bundle from the reviewed commit that the control plane allows
Hosts to advertise. Use the same release revision across the deployment:

```sh
COMPUTE_DEPLOY_SHA=$(git rev-parse HEAD)
git archive --format=tar.gz --output=/private/compute/bundles/host.tar.gz "$COMPUTE_DEPLOY_SHA"
shasum -a 256 /private/compute/bundles/host.tar.gz
```

Put the full 40-character revision into `deployment_sha`, the printed digest
into `source_sha256`, and the container-visible absolute path into
`source_bundle`. The digest is checked before any upload. Bundles must contain
the reviewed repository root, not a containing directory or a working tree
with secrets. The bootstrap refuses archive symlinks, traversal and private
deployment state. It injects `.dockerignore` exclusions so deployed tunnel
files cannot enter Docker build contexts.

Each profile needs a dedicated remote `deploy_root`. The first installation
writes an identity marker binding the directory to its allocation, node and
Host. Retries reuse that marker and the persisted enrollment credential. An
unmarked pre-existing installation or a different marker is refused rather
than overwritten. Existing manually enrolled Hosts can continue through the
original management interface; this bootstrap does not silently adopt their
files.

The first version reserves a profile for one present allocation because its
broker reverse-forward endpoint is fixed. For an overlapping old/new deployment,
prepare a second profile with its own reverse port, source deployment directory
and dedicated tunnel authorization. Simply changing the profile name while
reusing its broker endpoint does not create a separate endpoint.

### Three tunnels and the broker route

Provision tunnel authorization on the stable gateway/control-plane machines
before adding a server. The bootstrap deliberately cannot grant itself access
there. Use three different keys with no shell authority and narrowly permitted
forwarding:

| Tunnel | Remote destination | Audience |
| --- | --- | --- |
| Inference | Gateway's inference port, default `127.0.0.1:8080` | Runner and sandbox relay |
| MCP | Control plane's **MCP-only** listener, default `127.0.0.1:8001` | Sandbox tool relay |
| Host control | Main API, default `127.0.0.1:8000` | Trusted runner only |

The Host control connection also carries a reverse forward from the stable
control-plane machine to `workspace-broker:8092`. Bind it to the actual
control-plane Docker bridge gateway address, never loopback or `0.0.0.0`.
Configure `GatewayPorts clientspecified` and restrict that key's reverse listen
address/port as well as its forward destination. Set the profile's
`AGENT_WORKSPACE_BROKER_ADVERTISE_URL` to the resulting URL reachable by the
control-plane container. Include its hostname/IP in
`AGENT_BROKER_URL_ALLOWED_HOSTS` when that allowlist is configured. A distinct
reverse port is required for every simultaneously deployed Host.

Verify both node and tunnel server host keys through a trusted source before
adding them to their respective `known_hosts` files. `ssh-keyscan` alone only
collects a candidate key; it does not authenticate it. The transport always
uses strict host-key checking and isolated keys, ignores ambient SSH config
and agents, and never enables trust-on-first-use. CloudLab VM access uses the
manifest's SSH port rather than assuming port 22. When a newly allocated
CloudLab node has an unknown host key, deployment blocks until the administrator
verifies it and updates the selected SSH connection; retry then uses the same
resource.

Choose `AGENT_EGRESS_CHAT_NETWORK_POOL`, resolver and protected CIDRs for the
actual environment. The sample range is illustrative. Preserve the target
release's per-group nftables policy; setting up cloud resources is not a reason
to widen it. The profile's environment must contain single-line values; it is
parsed as dotenv data, never sourced as shell code. The supported substitutions
are `{deploy_root}`, `{host}`, `{node_id}` and `{allocation_id}`.

## Enable the control plane

Set:

```text
AGENT_COMPUTE_DIRECTORY=/private/compute
AGENT_COMPUTE_SECRET_KEY=<independent random secret supplied by deployment>
AGENT_CLOUDLAB_API_URL=<operator-verified HTTPS Portal API base URL>
```

Put `compute.json`, `bundles/host.tar.gz` and the `secrets/` files under the
absolute host directory named by `AGENT_COMPUTE_DIRECTORY`. When this variable
is set in the deployment environment or persistent `.env`, the existing
control-plane `deploy.sh` adds `docker-compose.compute.yml` automatically in
both source and packaged artifact mode. It mounts that directory read-only at
`/run/compute` and sets `AGENT_COMPUTE_CONFIG=/run/compute/compute.json` for the
main control-plane service. The MCP-only listener receives neither these
credentials nor this mount. For manual Compose commands, include the same
`-f deploy/control-plane/docker-compose.compute.yml` overlay explicitly. A
non-Compose operator may set `AGENT_COMPUTE_CONFIG` and provide equivalent
read-only mounts directly. The service image includes `openssh-client`; no
Docker socket or root privilege is added to the control-plane container.
Database migrations must be applied before starting the new worker.

`AGENT_CLOUDLAB_API_URL` is optional when using only existing SSH servers.
There is no guessed default: the public Portal API specification currently
contains a development HTTP server example, which is not a production endpoint
to receive credentials. Verify the production API base with the CloudLab
operator or your deployment documentation, ensure valid HTTPS, then use
**Connections → Validate**. Validation performs a read-only authenticated
experiment-list request and checks the expected response shape; redirects
are rejected. The official references are the
[CloudLab Portal API manual](https://docs.cloudlab.us/advanced-topics.html) and
[Portal API OpenAPI source](https://gitlab.flux.utah.edu/emulab/portal-api/-/blob/master/openapi.json).
No local downloaded token is automatically uploaded to the server.

## Day-to-day workflow

1. In **Admin → Servers → Connections**, add the management SSH private key
   and verified node host keys. For CloudLab, separately add and validate the
   API token. Responses expose metadata only; credential writes do not return
   the secret value.
2. Choose **Add server**, then existing SSH or CloudLab. For SSH, enter the
   address, port and user. For CloudLab, attach an exact experiment UUID or
   provision a profile in an explicitly selected project. Choose the deployment
   SSH connection, CloudLab SSH username and trusted deployment profile.
3. Run preflight, then submit. Watch the durable operation until it is ready.
   HTTP 202 means an operation was recorded, not that the server is online.
   Allocation, deployment, enrollment and readiness are distinct steps.
4. Set the **global sandbox location** to the ready Host. New, unassigned
   sandboxes follow that target. Already retained groups stay on their original
   Host. The default is not changed merely because a new server became ready.
5. Enable renewal only after reviewing the CloudLab allocation's complete scope.
   For attached/imported resources, explicitly adopt lifecycle management first.
   Record a real reason and ordered lead times. Saving policy does not prove a
   lease extension; compare the actual observed expiration and last result.

An observation-only import registers resource facts without deployment. It can
be used for lease management after explicit adoption, but observing nodes does
not prove their containers or workspaces can be safely discarded. Active Host
bindings and verified inventory are still required before platform-initiated
release can pass its guards.

## Recovery and release boundaries

Operations persist their request ID, exact parameters, step and resource ID.
Use the existing operation after a browser disconnect or worker restart. A
new request ID is a new command, not a substitute for finding the prior result.

| Result | Action |
| --- | --- |
| SSH key or identity refused | Verify the intended machine and key; update the selected connection, then retry the same operation. Never disable host-key checks. |
| Cloud create outcome unknown, no UUID saved | Identify the actual experiment in the exact account/project and use explicit resolution. The worker never repeats POST or adopts a same-name experiment automatically. |
| UUID saved, later observation failed | Repair credentials/connectivity and resume the same resource; do not create another experiment. |
| Host installation/readiness failed | Inspect the operation and trusted host's deployment checks. Retry against the same resource after repairing its prerequisites/profile. No exception handler destroys the server. |
| Extension pending/unknown | Inspect the actual expiry. Retries keep the fixed absolute target; a shorter extension is not marked successful. CloudLab can reject or limit extensions. |
| Resource still has retained state | Keep it available or explicitly manage its chat lifecycle. Stopped containers and archived chats do not prove state is disposable. |
| Release committed, response unknown | Keep admission closed and confirm that exact immutable resource ID. Cancellation cannot undo a committed release. |

CloudLab's documented DELETE response `204` confirms deletion of that exact
experiment UUID. An explicit terminal experiment object with matching UUID and
project also permits recovery. A `404` by itself does not prove deletion: the
selected token's account or project visibility may have changed, even when an
authenticated experiment-list request succeeds. The adapter reports
`resource_visibility_unknown` and retains the resource reservation. If a DELETE
acknowledgement is lost and subsequent queries return `404`, the operation stays
committed and requires provider confirmation; it does not claim success or free
the deployment profile. Restore the correct access and retry the existing
operation. Do not bypass the release fence or edit database state to infer
deletion from absence. The public API provides no project-permission query that
would establish stronger evidence from that `404`.

The worker continues an already enabled renewal policy while retirement waits
on retained state. Manual Drain and resource-owned admission holds remain
separate. A successful renewal does not clear an administrator's Drain or a
retirement hold. The claim transaction also enforces the last known hard lease
cutoff, so a stopped worker cannot keep accepting work beyond that deadline.

`Remove from pool` revokes the Host credential but leaves the machine intact.
`Forget` only cleans up an eligible history record. `Retire/release` is a
separate operation over the whole allocation: for CloudLab that means the
entire experiment, not one row in its node list. It requires complete scope,
no global placement reference, no running/in-flight or retained dependencies,
and fresh broker plus machine inventory. Stopped Docker containers, orphan
networks/volumes, unrecognized containers, offline brokers and incomplete
inventory block release. There is no force bypass. Existing SSH resources do
not have a cloud-destruction capability.

Automatic renewal and successful unit tests do not establish that a particular
CloudLab profile supports the required virtualization and network environment.
Before moving live traffic, run the intended profile through deployment and a
real task, then continue that retained chat and verify its Fork on the same
Host. The adapter tests use mock HTTP/SSH and do not contact CloudLab or spend
an account's resources.
