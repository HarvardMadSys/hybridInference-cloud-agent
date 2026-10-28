# Sandbox start latency: where a turn's start goes, and what changed

Reviewed against `dev` at `3f60b6a`, 2026-10-28. The question was where the time
goes between a host claiming a turn and the prompt reaching the agent CLI, on
each of the four startup paths (`new_group`, `resume_stopped`,
`resume_running`, `reuse_live`), and what can be removed without touching a
security boundary in `AGENTS.md` §6–7.

Four reviews covered the host and control plane, the container lifecycle and
group network, the work inside the guest before the prompt, and the platform
(sandbox image, Kata configuration, archives). One of them drove the real
`SharedChatSessionSupervisor` → `ChatSandboxManager` → `ChatSandboxNetwork` →
`HostNetworkGuard` code against a local Docker daemon and the host's nftables,
logging every Docker call; the tables below come from that harness, rerun on
this revision. The host is runc on 4 vCPUs. Kata was not available, so every
Kata figure here is an estimate, marked as such.

## Where a start spends its time

| Operation (runc, measured) | Cost |
|---|---|
| One `docker` CLI call, even a trivial `inspect` | 27–35 ms, nearly all of it the binary's own start |
| The same inspect over the Engine API's unix socket | 0.3–0.5 ms |
| `docker exec` into a running container | 90–110 ms (100–300 ms through kata-agent, estimated) |
| One host-rule helper (`docker run --rm` of the runner image, read-only) | 380–470 ms |
| `docker start` | 165–205 ms (a VM boot under Kata, 1–3 s, estimated) |

Before this revision, an activation was mostly reads. Acquiring a running
group for a follow-up turn was ten sequential container and network
inspects and nothing else; a cold start was 26–32 inspects among its creates,
connects, helpers and execs. The guest side of a follow-up turn added two
probes whose answers could not have changed: whether the member-process
reaper is installed (a root exec at each census and reap, queued behind any
runtime install into the same container), and whether the live CLI's binary
exists (an inspect and an exec from the runner).

## Changed in this revision

### Plain inspects read from the Engine API

`cloud_agent_host/docker_engine.py`. `ChatSandboxManager._run` now asks the
backend's engine first; it answers exactly `docker container inspect` and
`docker network inspect` with references and no flags, over the daemon's unix
socket, shaped as the CLI prints them: the JSON array of what was found, one
`Error response from daemon: …` line per reference that was not, exit status 1
when any was not. Every caller's parsing, identity, mount and membership
checks run unchanged on that answer, and every fresh read is still a fresh
read: nothing is cached. Mutations, execs, copies and any read with flags run
the CLI as before, as does a read the API does not answer plainly (an
unreachable socket, a status other than 200 or 404, a body that is not JSON).
A daemon that does not answer within the call's timeout fails the call as the
CLI's timeout would, rather than being asked twice.

Only `build_backend_from_env` attaches an engine, and only where the CLI would
reach the same daemon: the default `docker` executable, and either
`DOCKER_HOST=unix://…` or no `DOCKER_HOST` with the default context (the CLI's
own precedence, including its configuration file's `currentContext`). Every
request is a GET pinned to API 1.48 (`DOCKER_MIN_API`), over the socket the
runner and sandbox manager already mount; no other service gains access to
it.

| Path (runc, medians of two rounds) | CLI | Engine reads |
|---|---|---|
| Follow-up turn: acquire of a running group | 288–298 ms | 15–17 ms |
| `require_network` (SSH changes, admissions without a fresh proof) | 309 ms | 13 ms |
| `new_group`, host-rule verification fresh / lapsed | 2,971 / 3,862 ms | 1,879 / 2,874 ms |
| `resume_stopped`, fresh / lapsed | 2,493 / 3,257 ms | 1,659 / 2,592 ms |
| Idle stop | 1,911 ms | 1,412 ms |

Both runs made the same calls on every path (46, 10, 9, 28, 36, 38 and 48);
all 352 inspects of the engine run were answered by the API, and every flow
passed in both.

### The member-process reaper is checked once per container

`ensure_member_process_reaper` probed, as root and under the container's
install lock, whether the reaper is in place: at the census before every
prompt, and again at every turn's reap. The answer is fixed for a container's
life: the file is root-owned and `0555`, no member is ever root, and only a
platform exec as root installs it. `ProvenContainers` now remembers it per
exact container id, the one the last start or network proof verified, and
forgets it with that proof. A replacement under the same name is probed again;
an unproven container is probed every time, as before. A turn whose answer is
known no longer waits for the install lock, which a runtime or MCP install can
hold for minutes. Saves two root execs per turn, one of them before the prompt.

### A live CLI is not asked whether its binary exists

The runner probed `command -v` for the runtime's binary on every turn, an
inspect and an exec. When the acquire reports the member's CLI as `live`,
that process is the binary, running in this container; the probe is skipped.
Any other resident state still probes, and a missing binary still fails the
turn as before. Saves about 130 ms per follow-up on runc (130–330 ms under
Kata, estimated).

### The host-rule helper starts from compiled bytecode

The network guard runs the runner image as a read-only helper for every
install, read-back and relay update. `python:3.12-slim` ships the standard
library without bytecode, and a read-only filesystem cannot cache it, so every
helper compiled each module it imported again. The image now compiles the
standard library at build time, against source hashes, leaving out
`site-packages` and the GUI and development packages (+13 MB). A helper run
with the guard's own imports fell from 391 to 264 ms. A cold start runs two
helpers (four once the verification has lapsed), a stop one, and the
running-group sweep one every five seconds.

## Recommended next, not changed here

Ranked by time saved × how often the path runs. Each needs a design decision,
a security review or a Kata host this review did not have.

1. **A warm spare turn child.** Each turn starts a fresh interpreter that
   imports the runner (≈330 ms) and builds two HTTP clients (≈80 ms) before it
   can answer the handover, and the host claims nothing else meanwhile. One
   pre-started child, blocked reading its payload, keeps the credential-on-stdin
   handover as it is. The worker id would travel in the payload, and an unused
   spare must be killed, not closed: empty input runs the deployment canary.
   About 0.35–0.43 s on every turn.
2. **Host-rule verification on idle hosts.** Nothing renews it while no
   container runs (`guard_network` returns first; `renew_host_rules` requires a
   running container), so the first start after a minute's quiet installs and
   reads the rules back itself: +0.95 s on `resume_stopped`, +1.8 s on
   `new_group`. The runner's own verification is renewed only at a handover,
   costing about 0.5 s on any claim more than 45 s after the last. #944 chose
   this deliberately, for no helper containers on an empty host. Renewing
   from the idle sweep costs two helpers per 45 s per idle host. On the
   runner side, the renewal could instead overlap the child's import.
3. **Dispatch arming.** Before each prompt the supervisor runs the process
   census and the observer or tool-mark fence one after another. The census
   could run beside them and join before admission. Under Kata the opening
   and bracketed snapshots could be one exec. Estimated 0.3–0.9 s per Kata
   turn.
4. **Member preparation before a CLI spawn:** reap, stale-file removal, state
   preparation, hook merge and `BASH_ENV` are 5–7 execs as the member's UID.
   They could be one reviewed script with its input on stdin. Saves 0.3–1.5 s
   per spawn.
5. **Kata guest observer at first prompt after each boot.** It could start
   in the background once acquire has proved a started guest. Estimated
   0.8–3 s per boot.
6. **Environment boots** freeze and thaw every writer UID (9 execs and 11
   inspects) even when there is no start script. Each script phase also spends
   three separate execs on preparation, and completion is polled every second.
   Saves 1–3 s per environment boot.
7. **Snapshot-offered new groups** wait for the whole source archive before
   creating the container, because `lockfiles()` waits on the archive's
   completion event. Signalling when the lockfiles are read would overlap
   0.9–6.5 s with the boot.
8. **Source archive builder.** Fractional mtimes add a 1 KiB PAX header to
   every member. Truncating them, plus string paths in the hot loop, took a
   30,000-file tree from 6.5 to 2.1 s.
9. **Initial archives** (when archives are enabled) gzip at level 6 on one
   thread before a new chat's first prompt: 18 s for a 402 MB workspace. Level
   1 halves it, and parallel deflate keeping one gzip member does better. The
   previous release's marker rebuild must keep working. One capture slot per
   host also queues urgent work behind background archives, for up to 12
   minutes.
10. **Kata boot configuration.** Apart from the kernel and guest seccomp,
    everything is upstream default. Candidates:
    - `static_sandbox_resource_mgmt = true`, which removes CPU and memory
      hotplug;
    - the uncompressed kernel of the same build;
    - a smaller `default_memory`.

    Each needs Kata qualification. VM templating shares kernel memory and
    RNG/KASLR state across tenants and should stay off.
11. **Archive start fences** re-inspect the container `start()` just
    verified: seven of their ten inspects, about 0.35 s per cold start on runc.
    Most of that cost goes away with the Engine API reads in this revision.
12. **`HostNetworkGuard._lock` serializes relay updates across unrelated
    bridges.** A third concurrent `new_group` waited 0.8 s. A per-bridge lock
    for relay and SSH updates, with installs still exclusive, would remove the
    wait.
13. **A long-lived host-rule helper** (150–176 ms per exec against 409 ms per
    run) needs a security review: it is a standing `NET_ADMIN` container in the
    host's network namespace.
14. **Kata guest network sync on every running-group acquire.** It could be
    skipped when the container, its start time, the bridge and the relay
    addresses are unchanged. Needs a security review, because Docker may
    rewrite the guest's hosts file.
15. **Control-plane round trips before dispatch.** Suspend flushes an event it
    just queued, and suspend and resume each re-register the group with the
    sandbox manager. `report_continuity` is synchronous. The claim route also
    awaits the gateway grant and the GitHub token mints one after another.
    Together that is 50–400 ms across the tunnels.
16. **Restore throughput.** One `get_object` stream goes to a temporary file,
    then a second rewrite, then `docker image load`. Parallel ranged GETs
    pinned to the object version would drop a copy and survive a broken
    stream. Estimated 1 GiB in 8–15 s instead of 20–40 s.
17. **Sandbox image size (pulls only).**
    - The Kilo build is copied in and deleted in a later layer, so it is
      stored twice (≈170 MB).
    - The Claude SDK's venv carries an unused 214 MB CLI.
18. **Smaller items:**
    - Codex's npm shim adds a Node process and 44 ms per member start.
    - A downloaded runtime version's root marker is probed on every launch;
      the reaper cache above would serve it.
    - runc preflight runs `docker version` twice.
    - At capacity, the claim loop polls for a free slot every second.

## Checked, already fine

- **Claim waiting and connections:** an idle host's wait for work is LISTEN/NOTIFY-driven, and claims, waits, per-attempt control-plane and sandbox-manager calls all keep their connections alive.
- **Supervisor locking and persistence:** the supervisor's global lock is never held across a Docker call; creation uses a per-sandbox barrier, and nothing is fsynced on acquire.
- **Host-rule checks:** `ensure()` trusts a verification for 60 s, held relay exceptions skip their helper on reuse, and a turn's child adopts the host's verification instead of starting a helper.
- **Install snapshots:** a snapshot is its base image plus one layer, so repeated snapshots do not deepen the chain.
- **CLI start and execs:** Claude's readiness has no fixed sleep, the container's init is an inert shell, and no login shell runs on the CLI's path.

## Limits

- **Stubbed harness:** the executed inventory stubs four things.
  - It drops the three bridge rules that use `meta ibrname`, which the bench kernel lacks.
  - It answers DNS from a local responder.
  - It runs the helper from `python:3.12-slim` with the host's `nft`.
  - It uses `python:3.12-slim` as the sandbox image.
- **Not covered:** turn-level CLI start and model time are outside it.
- **Kata:** no Kata host was available, so VM boot, kata-agent exec and virtio-fs costs are estimates throughout. The engine reads and the two removed probes save the same Docker and exec round trips there, and under Kata each exec costs more.
