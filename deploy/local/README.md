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

Prerequisites: Docker with Compose, `openssl`, `curl`, and Python 3.

```bash
./deploy/local/dev.sh up
./deploy/local/dev.sh smoke
./deploy/local/dev.sh legacy-smoke
```

Then open <http://127.0.0.1:13002>. The first sign-in displays an explicit
local consent page. The generated credentials live in the git-ignored
`deploy/local/.env.local` with mode `0600`.

`up` may take a while the first time because the sandbox image installs exact
versions of Claude Code, Codex, Pi, OpenCode, and Kilo. Later builds use Docker
layer cache.

`smoke` logs in through PKCE, runs a first turn and native-session follow-up
through **each of those five real CLI binaries**, waits for every chat
container to stop at the idle deadline, and proves each third turn restores
the same container id with no mounts. It then full-forks the latest settled
Claude chat, verifies the complete conversation plus Docker filesystem were
copied into a separate stopped/mount-free container, and resumes that clone.
A passing result therefore covers `NEW → LIVE → stopped → RESTORED` for every
CLI plus the latest-settled full Docker fork path.

`legacy-smoke` is a separate compatibility check for the rollout's
default-off path. It briefly stops the retained runner, launches one
disposable runner with a same-path temporary bind (the topology production
mixed mode requires), and proves that the job reaches Claude Code, exports a
checkpoint, and completes in-sandbox patch generation. The temporary bind is
removed before the retained runner restarts; it is not part of the normal
Docker-only local storage topology.

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

The local chat inactivity timeout is 60 seconds so stop/resume is easy to see.
Production remains 1800 seconds unless configured otherwise.
