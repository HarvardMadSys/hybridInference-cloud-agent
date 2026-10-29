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

### A new group offered a snapshot no longer waits for its archive

`cloud_agent_host/source_prefetch.py`. A new chat's source is cloned and
archived on a thread of its own while its container starts. A group handed an
installation snapshot chooses it by the checkout's dependency manifests before
it is created, and that lookup waited for the whole prepared source, archive
included, so for exactly those groups the archive was built before the
snapshot load and the boot instead of beside them. The prefetch now publishes
the manifests on an event of their own as soon as Git lists them. A
preparation that fails before the listing answers no manifests, as before; one
that fails after it fails the turn at its checkout, as every failure did; a
discard still stops at the next entry or chunk and removes the checkout. Off
the path before the acquire: 0.18 s of archive for this repository, 1.6 s for
a 30,000-file tree.

### The source archive builder walks strings and writes whole-second mtimes

`cloud_agent_shared/git/source_archive.py`. The builder parsed a `pathlib`
path per entry on both of its passes, and wrote each member's fractional stat
mtime as a 1 KiB pax header: spooled, copied into the container and counted
against the import limit. It now walks strings, and members carry whole-second
mtimes, which the ustar header holds. Members still come from `tarfile`'s own
`add`, so the hard-link, file-type and setuid/setgid handling is unchanged; the
escape check splits the name on `/` and refuses exactly what `PurePosixPath`
refused. Git in the container compares content after the copy whatever the
mtime, because each file's inode, ctime and owner already differ from its
index. With integer mtimes the output is byte for byte what the previous
builder wrote. A 30,000-file tree: 4.1 → 1.3 s and 67.6 → 38.3 MiB.

The archive's cancellation test depended on the order the filesystem lists
`.git` and `payload`, and failed CI whenever `payload` came first. It now
cancels after whichever is statted first and fails on any stat of the other,
in either order.

### A spare turn child waits for the next claim

`cloud_agent_host/turn_launch.py`. Every turn started a fresh interpreter only
after its claim, and the host claimed nothing else while that child imported
the runner and answered. The host now keeps one spare, `runner --turn
--spare`, started after each handover: it imports everything, warms the HTTP
transport stack with a throwaway client, and blocks reading its standard
input, where the next claim writes the turn exactly as it would to a cold
child. The turn and its credentials still travel only on standard input. A
spare's command line holds only this host's configuration, so the per-turn
worker id now travels in the turn, set by the host after the claim's own
underscore keys are stripped. The preflight and the network check still run
once the turn has arrived.

A spare that has exited, or written to its answer descriptor before it was
handed anything, is never given a turn; a cold child takes it. End-of-file
retires a spare without running the probe, which stays the deployment
canary's: a plain `--turn` on empty input still runs it. The host retires its
spare when it stops and starts none while stopping, and a spare whose host
dies reads end-of-file and exits. With a preflight that costs nothing, the
child's answer came 237–267 ms after the handover from a cold child and
0.6 ms from a spare; a runc preflight adds its own ~40 ms to either. The first
turn after a host starts is still cold, an idle spare holds about 45 MiB, and
the next spare's imports share the CPU with the turn that was just handed
over.

### One `docker version` per runc preflight

A runc child ran `docker version` twice before answering its host: once to
learn the daemon answers, and again for the versions the gateway-priority
check reads. The second form cannot render without the daemon, so its exit
status answers the first question; preflight now runs it once (CLI time
44 → 21 ms). `check_network()`, which the host service and a Kata child
holding its host's verdict call, still asks Docker itself every time.

### Settled root markers are checked once per container

Before a member's CLI started, the host asked the container, as root and
under its install lock, whether a downloaded runtime version is copied in,
whether Kilo's build carries the isolation patch, and whether each MCP adapter
matches the one this host ships. Each answer is a root-owned file in a
root-owned directory that no member can write and only a platform exec as root
writes, so `ProvenContainers` now remembers it per exact container id, as it
does the reaper's (`chat_sandbox_exec.install_once`): checked before the
install lock, noted only after a probe or install that succeeded. An adapter
is keyed by its content digest too, and replacing one forgets what was found
there. The Codex MCP policy is still asked every launch: its content follows
the relay each launch names. A resumed Claude launch on a downloaded version
went from 5 execs to 4, 615 → 532 ms.

### A member is prepared in one exec

`cloud_agent_host/member_preparation.py`. Before a member's CLI spawned, its
own UID reaped its slot, removed stale pidfiles and the last launch's
credentials, prepared its provider home and, for Claude, merged the tool
hooks: two to four `docker exec`s, one per step. They now run in one exec of a
runner the host sends (`python3 -I`), each step still a process of its own,
with its argv, input, timeout and error, in the same order, and a failure
stops the rest. The reaper kills every process of its UID outside its
baseline, so the runner adds its own pid and start time and the reap spares
exactly it. The owner's inference credential is not queued: it is written by
its own exec after the reap, because a process an earlier launch left as this
UID could read the runner's input until the reap kills it; nothing else in the
queue is secret. Medians of 20 resumed launches on runc:

| Runtime | Execs, without / with owner Secrets | ms, without / with |
|---|---|---|
| Claude | 4 → 1 / 5 → 2 | 426 → 223 / 517 → 310 |
| Codex | 3 → 1 / 4 → 2 | 314 → 192 / 399 → 269 |
| OpenCode | 3 → 1 / 4 → 2 | 314 → 191 / 389 → 269 |
| Kilo | 3 → 1 / 4 → 2 | 303 → 193 / 393 → 276 |
| pi | 2 → 1 / 3 → 2 | 218 → 176 / 305 → 255 |

### The census runs beside the tool-mark fence on runc

`cloud_agent_host/turn_arming.py`. Before each prompt the supervisor took the
member-process census and then the observer or tool-mark fence, each a
`docker exec`, on every turn. Under runc, Claude Code's tool-mark fence reads
where the hook-mark file ends as the sandbox user, while the census lists the
member's processes as the member, and neither can see the other; the census
now runs on a worker thread beside the fence, joined however the fence ends,
before admission is rechecked and the prompt is written. A census failure is
still the error the turn reports, through the same cleanup. Under Kata the
census stays strictly before the guest observer's fence: it runs as the
member's UID, and a census exec that reached the guest after the fence was
read would be journaled inside the turn's slice and billed to the member. On
runc, 215 → 129 ms per Claude Code turn.

### A booted Kata guest's observer starts before its first prompt

`cloud_agent_host/guest_observer_warmup.py`. The first prompt after every boot
found the guest observer missing and started it inside its own fence: a
cleanup exec, the detached start, the eBPF load and the polls until the
helper was ready. When acquire has created or started the guest and proved
it, a daemon thread now takes that container's observer lock and calls the
same `ensure()` the fence would, so the first prompt finds the helper running
or waits on that lock. It acts only while the exact container it was asked
for is still the proven one, never while a cursor is open on that guest, and
a failure is only logged, leaving the prompt's own start as before.
Estimated at 0.8–3 s off the first prompt after `new_group` or
`resume_stopped`; not measured, for want of a Kata host.

### No writer freeze for an environment boot with no script to run

`cloud_agent_host/environment_preparation.py`. An environment boot froze and
thawed every writer UID, 11 execs and about 950 ms on runc with two members,
even when no script would run. Writers are now frozen only when one will: the
container's receipt must show setup done and no update pending, and the start
script must be empty or skipped. Both receipt facts only ever become true for
a container, so a receipt read before admission cannot understate the work.
The preparation barrier, the lease and lifecycle fencing and the
exact-container check are unchanged, and the fixed tool-home script still
runs.

## Found, not changed

**An environment preparation can trust a stale receipt.**
`EnvironmentPreparation.status` caches the container's inspection for 15 s,
stopped or not, and `prepare_environment` returns a receipt whose `ready` was
computed against that cache. A status read of a stopped environment, such as
the control plane's environment view, less than 15 s before something starts
it (a turn's acquire, a terminal, an action, a retry) makes the boot just
started look prepared: the receipt names the previous boot, and the start
script does not run in the new one. Reproduced against this branch's
`prepare_environment` with the real `EnvironmentPreparation`; the same test
without the earlier status read runs the preparation. The fix is to compare
the receipt's boot with the inspection `prepare_environment` takes after the
start. A follow-up could then answer a prepared boot without the full path;
that was prototyped here, about 15 ms per follow-up on runc and one guest
network sync under Kata, and left out until the check exists.

## Recommended next, not changed here

Ranked by time saved × how often the path runs. Each needs a design decision,
a security review or a Kata host this review did not have.

1. **Host-rule verification on idle hosts.** Nothing renews it while no
   container runs (`guard_network` returns first; `renew_host_rules` requires a
   running container), so the first start after a minute's quiet installs and
   reads the rules back itself: +0.95 s on `resume_stopped`, +1.8 s on
   `new_group`. The runner's own verification is renewed only at a handover,
   costing about 0.5 s on any claim more than 45 s after the last. #944 chose
   this deliberately, for no helper containers on an empty host. Renewing
   from the idle sweep costs two helpers per 45 s per idle host. On the
   runner side, the renewal could instead overlap the child's import.
2. **One exec for a Kata turn's opening observer reads.** The opening read
   and the fence after it are two guest execs. One root script can take both
   (prototyped here), but it pays the helper's two-poll wait inside the exec,
   which the second exec's start used to hide: the clock bridge around the
   reading widened by about 10 ms with a stand-in helper on runc, an
   estimated 25–50 ms under Kata, and attribution windows with it. Its
   fallback must also still restart a helper whose opening read the host
   refuses. Estimated at one kata-agent exec less 50–100 ms per Kata turn.
3. **Environment boots with a script** spend three separate execs on
   preparation in each script phase, and completion is polled every second.
4. **The clone's size watcher** walks the whole checkout again and again while
   Git writes it, backing off to at most a fifth of the time. A 30,000-file
   local clone took 7.7–9.7 s; how much of that the walks cost was not
   measured.
5. **Initial archives** (when archives are enabled) gzip at level 6 on one
   thread before a new chat's first prompt: 18 s for a 402 MB workspace. Level
   1 halves it, and parallel deflate keeping one gzip member does better. The
   previous release's marker rebuild must keep working. One capture slot per
   host also queues urgent work behind background archives, for up to 12
   minutes.
6. **Kata boot configuration.** Apart from the kernel and guest seccomp,
   everything is upstream default. Candidates:
   - `static_sandbox_resource_mgmt = true`, which removes CPU and memory
     hotplug;
   - the uncompressed kernel of the same build;
   - a smaller `default_memory`.

   Each needs Kata qualification. VM templating shares kernel memory and
   RNG/KASLR state across tenants and should stay off.
7. **Archive start fences** re-inspect the container `start()` just
   verified: seven of their ten inspects, about 0.35 s per cold start on runc.
   Most of that cost goes away with the Engine API reads in this revision.
8. **`HostNetworkGuard._lock` serializes relay updates across unrelated
   bridges.** A third concurrent `new_group` waited 0.8 s. A per-bridge lock
   for relay and SSH updates, with installs still exclusive, would remove the
   wait.
9. **A long-lived host-rule helper** (150–176 ms per exec against 409 ms per
   run) needs a security review: it is a standing `NET_ADMIN` container in the
   host's network namespace.
10. **Kata guest network sync on every running-group acquire.** It could be
    skipped when the container, its start time, the bridge and the relay
    addresses are unchanged. Needs a security review, because Docker may
    rewrite the guest's hosts file.
11. **Control-plane round trips before dispatch.** Suspend flushes an event it
    just queued, and suspend and resume each re-register the group with the
    sandbox manager. `report_continuity` is synchronous. The claim route also
    awaits the gateway grant and the GitHub token mints one after another.
    Together that is 50–400 ms across the tunnels.
12. **Restore throughput.** One `get_object` stream goes to a temporary file,
    then a second rewrite, then `docker image load`. Parallel ranged GETs
    pinned to the object version would drop a copy and survive a broken
    stream. Estimated 1 GiB in 8–15 s instead of 20–40 s.
13. **Sandbox image size (pulls only).**
    - The Kilo build is copied in and deleted in a later layer, so it is
      stored twice (≈170 MB).
    - The Claude SDK's venv carries an unused 214 MB CLI.
14. **Smaller items:**
    - Codex's npm shim adds a Node process and 44 ms per member start.
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
- **Not covered:** turn-level CLI start and model time are outside it. The spare child, the archive builder, member preparation and the census were measured on benches of their own, described with each.
- **Kata:** no Kata host was available, so VM boot, kata-agent exec and virtio-fs costs are estimates throughout. The observer warm-up acts only under Kata and was tested with fakes, not measured. Every removed Docker call and exec saves the same round trip there, and under Kata each exec costs more.
