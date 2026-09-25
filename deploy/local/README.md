# Complete local stack

This is the development topology that exercises the product rather than only
starting the control-plane half of it. It runs entirely in Docker and includes:

- the web app and a same-origin reverse proxy;
- a loopback-only gateway implementing PKCE/JWKS, account/model lookups,
  inference grants, and a deterministic model response;
- Postgres plus migrations;
- an enrolled runner and workspace/session broker;
- the real sandbox image and all five pinned Agent CLIs;
- a read-only `local/demo` Git repository on a private Docker network.

No production authentication bypass is enabled. The browser performs the real
PKCE flow, the control plane verifies an RS256 identity token through JWKS, and
the sandbox receives a real short-lived grant-shaped credential. The local
gateway and repository are deterministic substitutes for external services,
not alternate code paths in the application.

## Run

Prerequisites: Docker 28 or newer with Compose, `openssl`, `curl`, and Python 3
(the sandbox network attaches its relays with a gateway priority). The script
prefers the Compose v2 plugin and falls back to the standalone command. The
single runner container runs up to `LOCAL_HOST_SLOTS` turns at once (default 2),
which the concurrent-fork smoke needs; the admin Servers page can change that
limit while the stack runs. Give Docker Desktop at least 4 GiB of memory; 8 GiB is
recommended for the all-five-runtime and concurrent-fork smoke. A roughly
2 GiB VM can OOM Kilo before the session protocol is exercised.

```bash
./deploy/local/dev.sh up
./deploy/local/dev.sh smoke
```

Then open <http://127.0.0.1:13002>. The first sign-in displays an explicit
local consent page. The generated credentials live in the git-ignored
`deploy/local/.env.local` with mode `0600`.

To use another loopback port, change `LOCAL_EDGE_PORT` in `.env.local` before
starting the stack, or export it for that invocation. `dev.sh` derives
readiness checks, smoke-test URLs, console output, and the default OAuth
callback allowlist from that value. An explicit
`LOCAL_IDENTITY_ALLOWED_REDIRECTS` remains authoritative when testing a custom
identity setup.

Sandboxes use the same one network as production. Each retained sandbox
group gets its own labelled, routable `freeinference-chat-net-*` bridge, and
the runner and broker first install this stack's host nftables table on the
Docker engine: public IPv4 on TCP 80/443, the resolver in
`LOCAL_EGRESS_DNS_SERVER`, and the local inference and MCP relays; not the
engine's own addresses, private ranges, other groups, or IPv6. The relay
network only lets that lifecycle find the two relays; no sandbox joins it.
`dev.sh` records the resolver the engine already hands containers (Colima's
`192.168.5.1`, for example) the first time it writes `.env.local`.

For an isolated second stack, also export a distinct `LOCAL_PROJECT_NAME`,
`LOCAL_SANDBOX_IMAGE`, `LOCAL_RELAY_NETWORK`, `LOCAL_CHAT_NETWORK_POOL`,
`AGENT_NETWORK_NAME`, and the four published ports. New env files derive a
project-scoped sandbox image tag automatically; an explicit value remains
necessary when overriding an existing env file. This keeps its Docker images,
volumes, service discovery, host rules and group addresses separate from
another checkout's local stack; of two stacks whose pools overlap, the host
rules refuse the one started later.
`up`, `start`, and `smoke` fail before changing containers when either named
network already belongs to a different Compose project; Compose's warning is
not safe here because duplicate service aliases can route bootstrap or agent
traffic into the other stack.

`up` may take a while the first time because the sandbox image installs exact
versions of Claude Code, Codex, Pi, OpenCode, and Kilo. Later builds use Docker
layer cache. The bootstrap also writes a stack-scoped completed-deployment
marker into `local-host-state`; local runners and the control plane use that
same marker, so local claims exercise the production exact-generation fence
rather than bypassing it.

### Building through a local proxy

On Docker Desktop, a proxy listening on the host (for example Clash on port
7890) is reachable from builds through `host.docker.internal`. Prebuild with
Docker's standard proxy arguments, then start the stack using those cached
images:

```bash
./deploy/local/dev.sh build \
  --build-arg HTTP_PROXY=http://host.docker.internal:7890 \
  --build-arg HTTPS_PROXY=http://host.docker.internal:7890 \
  --build-arg NO_PROXY=localhost,127.0.0.1
./deploy/local/dev.sh up --no-build
```

Use the same project, image and port environment overrides for both commands
when running an isolated second stack. `up --no-build` starts the exact images
you prebuilt, without rebuilding with the default mirror arguments. Re-run
`build` after code changes before using this option. The proxy must already accept the
Docker Desktop connection. These arguments apply to build downloads; no proxy
environment is added to the running agent containers. Python dependency
downloads allow 120 seconds of inactivity by default; the corresponding
`PIP_DEFAULT_TIMEOUT` and runner `UV_HTTP_TIMEOUT` build arguments can be
overridden for slower links. Certificate and package-signature checks remain
enabled.

If Debian's default mirror returns intermittent errors, select another Debian
mirror at build time, for example `--build-arg
DEBIAN_MIRROR=http://mirrors.aliyun.com`. The default remains
`http://deb.debian.org`; the override changes both the archive and security
mirror while preserving the distribution, components and signed keyring.
Index downloads retry transient failures and fail the build if an index is
still unavailable, rather than proceeding with an incomplete package list.
The sandbox also retries package downloads and keeps Debian/npm downloads in
BuildKit caches, so retrying a failed build can reuse completed downloads.
These build caches are not mounted into running agent containers.

`smoke` logs in through PKCE, runs a first turn and native-session follow-up
through **each of those five real CLI binaries**, waits for every chat group to
stop at the idle deadline, and proves each third turn restores the same
mount-free container id. It then native-forks each session at its second turn
while the source is already at turn three, verifies the child contains exactly
that historical prefix, and proves source and child are independently LIVE inside the same
container/workspace, holds the local model responses long enough to observe
both post-fork turns simultaneously `RUNNING`, and requires both CLI processes
to overlap at a two-party local inference barrier before either receives its
response. It then wakes the stopped group from the child. A passing result
therefore covers
`NEW → LIVE → stopped → RESTORED`, historical prompt-free same-container Fork, concurrent
source/child admission, group stop/restore, and completion of the final
quiescent publication snapshot for all five CLIs.

The local model answers at once. To act while a turn is still running — send
messages that queue behind it, edit or delete them, press Stop — put
`[local-hold:N]` in that turn's message: the gateway holds its answer for `N`
seconds (at most 120). A marker in an earlier message has no effect.

To send a waiting message *into* a running claude-code turn, the turn needs a tool
in flight — that is when Claude takes one. `[local-tool:N]` makes the turn run
`sleep N` through Bash first (at most 60 s); press ↑ on a waiting message while it
runs, and the answer after the tool quotes what you sent. A codex turn takes one
at any point, so `[local-hold:N]` is enough there.

Useful commands:

```bash
./deploy/local/dev.sh status
./deploy/local/dev.sh logs runner
./deploy/local/dev.sh stop
./deploy/local/dev.sh start
```

`stop` uses `docker compose stop`, not `down`: service containers, named
volumes, the private networks, and retained chat containers remain Docker-owned
and can be started again. There are no host workspace or database bind mounts;
the Docker socket is the runner/broker control channel, and Docker's own data
root holds the state.

The local chat inactivity timeout is 15 seconds so stop/resume is easy to see
and the five-runtime smoke does not keep five heavyweight CLI groups resident
at once on a small Docker Desktop VM. Active turns are never stopped by this
idle clock.
Production remains 1800 seconds unless configured otherwise.
