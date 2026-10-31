# Complete local stack

Run the browser, control plane, runner, and real Agent CLIs together without
production accounts or credentials. This Docker topology includes:

- the web app and a same-origin reverse proxy;
- the account system, with its first administrator created at setup;
- Postgres, migrations, host enrollment, and the sandbox manager;
- the sandbox image with pinned Claude Code, Codex, Pi, OpenCode, and Kilo CLIs;
- a read-only `local/demo` Git repository on the private control-plane network.

Accounts work as they do in production. Turns run on a model connection the
account adds in its settings, with its own provider key; the stack ships no
model. Sandboxes reach public addresses only, so that provider must be a public
one. The stack uses plain Docker isolation for trusted development; public
multi-tenant deployments require Kata.

## Run

Run these commands from the repository root. You need Docker 28 or newer with
Compose, `openssl`, `curl`, and Python 3. Docker 28 supplies the route-priority
support used when attaching the relay to sandbox networks. `dev.sh` prefers the
Compose v2 plugin and falls back to `docker-compose`.

Give Docker Desktop at least 4 GiB of memory; 8 GiB is recommended when running
turns. A roughly 2 GiB VM can run out of memory while starting Kilo.

```bash
./deploy/local/dev.sh up
./deploy/local/dev.sh smoke
```

Open <http://127.0.0.1:13002/setup/administrator> and create the first
administrator with the one-time code that `./deploy/local/dev.sh logs
control-plane` shows. It signs in with a login name and needs no mail server.
Sign-up by email stays closed until `AGENT_SMTP_*` is configured.

The first `up` builds all images, verifies the database credentials, applies
migrations, starts the services, and waits for readiness. Installing five pinned
CLIs can make the first build slow; later builds reuse Docker's layer cache.
The bootstrap records a deployment marker in `local-host-state`, and claims use
the same exact-generation fence as production.

`dev.sh` generates local credentials in the git-ignored `deploy/local/.env.local`
with mode `0600`. Keep this file with the stack's retained database. If it is
missing while the Postgres volume still exists, the script refuses to generate
replacement credentials. Restore the original file, especially
`AGENT_POSTGRES_PASSWORD` and `AGENT_DATABASE_URL`, before retrying. Existing
environment files are upgraded without replacing credentials or selected ports.

Useful commands:

| Command | Use |
|---|---|
| `./deploy/local/dev.sh status` | List service state. |
| `./deploy/local/dev.sh logs runner` | Follow a service's recent logs; omit `runner` for all services. |
| `./deploy/local/dev.sh config` | Validate Compose configuration without printing secrets. |
| `./deploy/local/dev.sh stop` | Stop service containers and retain their state. |
| `./deploy/local/dev.sh start` | Verify credentials, apply migrations, and start existing services. |
| `./deploy/local/dev.sh up` | Rebuild and reconcile services after code or configuration changes. |

`stop` uses Compose's `stop` command. Service containers, named volumes,
networks, and retained chat containers remain available. The stack has no host
workspace or database bind mounts. The runner and sandbox manager control Docker through
its socket, and Docker's data root holds the retained state.

### Ports and parallel stacks

Set `LOCAL_EDGE_PORT` in `.env.local`, or export it before running `dev.sh`, to
change the browser port. Readiness checks, smoke URLs, and browser URLs follow
that port.

For a second checkout on the same Docker engine, give it distinct values for
all of these settings and use them for every `dev.sh` invocation:

- `LOCAL_PROJECT_NAME`, `LOCAL_SANDBOX_IMAGE`, `AGENT_NETWORK_NAME`, and
  `LOCAL_RELAY_NETWORK`;
- `LOCAL_CHAT_NETWORK_POOL`, with no overlap with the first stack's pool;
- `LOCAL_EDGE_PORT`, `AGENT_CONTROL_PLANE_PORT`, `AGENT_MCP_RELAY_PORT`, and
  `AGENT_WEB_PORT`.

When no sandbox image override exists, the image tag is derived from the project
name. An existing `.env.local` already contains an explicit tag, so override it
when changing that stack's project name. These settings separate images,
volumes, service discovery, ports, host rules, and group addresses.

`up`, `start`, and `smoke` refuse networks owned by another Compose project
before changing containers. The host rules also refuse overlapping sandbox
address pools; accepting Compose's network warning can route traffic to the
wrong stack.

### Sandbox networking

Each retained sandbox group gets a routable, labelled
`freeinference-chat-net-*` bridge. Before a sandbox joins it, the runner or
sandbox manager installs and verifies this stack's nftables rules on the Docker engine.
The rules allow public IPv4 on TCP 80/443, DNS through
`LOCAL_EGRESS_DNS_SERVER` on TCP/UDP 53, and that group's MCP relay on its
service port. They block the engine's own addresses, private
and special-purpose ranges, other groups, other ports, and IPv6.

The relay network is used to discover the relay; sandboxes never join it.
This is the same network policy used in production. On first configuration,
`dev.sh` records a non-loopback IPv4 resolver supplied by the Docker engine
(for example, Colima's `192.168.5.1`), falling back to `1.1.1.1` if none is found.
Change `LOCAL_EGRESS_DNS_SERVER` if that resolver is unavailable in your setup.

### Building through a local proxy

On Docker Desktop, builds can reach a proxy on the host through
`host.docker.internal`. Start the proxy, prebuild with Docker's standard proxy
arguments, then reuse those images:

```bash
./deploy/local/dev.sh build \
  --build-arg HTTP_PROXY=http://host.docker.internal:7890 \
  --build-arg HTTPS_PROXY=http://host.docker.internal:7890 \
  --build-arg NO_PROXY=localhost,127.0.0.1
./deploy/local/dev.sh up --no-build
```

Use the same project, image, network, and port overrides for both commands.
Re-run `build` after code changes before using `up --no-build`. Proxy arguments
apply to build downloads; they do not configure a runtime sandbox proxy.
Certificate and package-signature verification remain enabled.

Python downloads allow 120 seconds of inactivity by default. For slow links,
override `PIP_DEFAULT_TIMEOUT` and the runner's `UV_HTTP_TIMEOUT` build
arguments. For Debian mirror failures, add a build argument such as
`--build-arg DEBIAN_MIRROR=http://mirrors.aliyun.com`; the default is
`http://deb.debian.org`. This changes archive and security mirror URLs while
keeping the distribution, components, and signed keyring. Index downloads retry
and fail the build if still unavailable. Sandbox Debian and npm downloads use
BuildKit caches, which are never mounted into running sandboxes.

### What the smoke test covers

`smoke` crosses the boundary a new deployment starts from: first-run setup with
the code from the control plane's log, the session cookie, an administrator
reading settings, `model_connection_required` in the configuration, the 409 a
turn gets without a connection, and sign-out. On a stack that already has an
administrator, setup cannot run again: the smoke passes that step only when the
server answers `setup_complete`, then checks that no session exists. While
setup is still pending and the log shows no setup code, it fails.

It runs no turn: the stack has no model, and a sandbox cannot reach one on a
private address. To exercise the CLIs, add a model connection for a public
provider in the browser and start a task. The runner defaults to two concurrent
turns; `LOCAL_HOST_SLOTS` must be at least 2 for concurrent forks, and the admin
Servers page can also change capacity while the stack runs. The local idle
timeout is 15 seconds, compared with production's default 1800 seconds. Active
turns are never stopped by this idle clock.
