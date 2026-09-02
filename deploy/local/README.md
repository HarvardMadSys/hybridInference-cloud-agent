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

Prerequisites: Docker with Compose, `openssl`, `curl`, and Python 3. Give Docker
Desktop at least 4 GiB of memory; 8 GiB is recommended for the all-five-runtime
and concurrent-fork smoke. A roughly 2 GiB VM can OOM Kilo before the session
protocol is exercised.

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

For an isolated second stack, also export a distinct `LOCAL_PROJECT_NAME`,
`LOCAL_AGENT_EGRESS_NETWORK`, `AGENT_NETWORK_NAME`, and the four published
ports. This keeps its Docker volumes, service discovery, and sandbox egress
backplane separate from another checkout's local stack. Each retained sandbox
then gets its own labelled internal `freeinference-chat-net-*` bridge; the
backplane is used only to discover the local inference and MCP relay endpoints.
`up`, `start`, and `smoke` fail before changing containers when either named
network already belongs to a different Compose project; Compose's warning is
not safe here because duplicate service aliases can route bootstrap or agent
traffic into the other stack.

`up` may take a while the first time because the sandbox image installs exact
versions of Claude Code, Codex, Pi, OpenCode, and Kilo. Later builds use Docker
layer cache.

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
