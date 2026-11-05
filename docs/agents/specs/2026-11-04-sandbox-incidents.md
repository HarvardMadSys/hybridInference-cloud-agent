# Sandbox incidents: recorded, and told to the people and the agent who need them

Status: implemented · 2026-11-05 (revised after review: member scope, background drain, per-thread cursor, immutable summaries, MCP stop reasons, host-first rollout; then to what was built)

## Summary

When something goes wrong inside a chat's sandbox today, nothing records it:

- **Some events leave no trace at all.** A process killed by the out-of-memory killer cannot log its own death, and the resource sampler keeps one reading every 15 seconds, so a spike between two readings is never seen.
- **The rest are logged where nobody looks.** The browser service and the MCP gateway write logs inside the container. They are read one container at a time, they disappear with it, and no question across sandboxes ("how many ran out of memory this week?") can be answered.

This change records a small set of **incidents** for each sandbox, in the table the admin sandbox page already shows (`agent_sandbox_events`). It tells each reader what they need:

| Reader | What they get |
| --- | --- |
| Operators | Every incident, per sandbox, on the admin sandbox page; resource peaks, not just last readings |
| The user | One plain line in the turn it happened in, for the incidents that break a turn, and only in the chat it concerns |
| The agent | At its first turn, that the sandbox has a browser and how to reach it; at later turns, what happened since it was last told |

It adds no new service: no Prometheus, no Grafana, no log store. Those wait until fleet-wide trends are needed (see *Not in scope*).

## What is recorded

Every incident has a **scope**:

- **sandbox:** it concerns everything in the container. The browser is one per sandbox, shared by the chat and its Forks.
- **member:** it concerns one chat. MCP programs run per member slot, so one member's failed program must not be reported to another. A slot is reused after its member leaves, so the host resolves the slot to the member holding it when it *reads* the journal, at most a minute after the failure, and the incident carries that `thread_id`, never only the slot.

| Kind | Scope | Detected by | Told to the user | Told to the agent |
| --- | --- | --- | --- | --- |
| `out_of_memory` | sandbox | Docker `oom` event: the cgroup's OOM killer killed some process in the container | yes | yes |
| `container_exited` | sandbox | Docker `die` event nobody asked for, with its exit code | yes | yes |
| `memory_high`, `pids_high` | sandbox | Host: a 15-second reading crossed 90% of the limit; re-armed below 80% | no | no |
| `browser_crashed` | sandbox | Browser service: Chromium exited with no stop requested (not an idle stop, not `stop`) | no | yes ("open pages are gone") |
| `mcp_server_failed` | member | MCP gateway runner, with a `phase`: `start` (it could not be started, or exited or timed out before answering `initialize`; a package that does not install fails here) or `crashed` (it exited after starting, with no stop requested) | yes | yes |

**A stop the platform asked for is not an incident.** The MCP runner already stops servers on purpose: idle past the timeout, admission withdrawn, launch or keys changed, runner shutting down. It records the reason with each stop, and only an exit with no requested stop is `crashed`. The browser service does the same for an idle stop and `stop`. For a container, Docker itself says so: every stop, idle stop or deletion the platform makes goes through Docker's `kill`, which Docker reports before the `die`; a `die` within two minutes of a `kill` for the same container is not an incident.

Incidents are **content-free**, like every other row in that table: no stderr, no URLs, no command lines, no environment. A program's stderr may hold its keys. A row carries:
- its kind, scope and `thread_id` when member-scoped;
- when it was observed;
- a few typed fields: exit code, signal, limit, peak, MCP alias, phase.

### Repeats and caps, without changing a row

`agent_sandbox_events` is append-only: a trigger refuses `UPDATE` and `DELETE`. So nothing is counted by changing a row that was already written. Every row is final when the host creates it:

- **The first occurrence** of a kind for a subject is sent at once, as its own row. The subject is the sandbox, or the thread plus the alias. People and the agent hear about it without waiting.
- **Repeats** of the same kind and subject within the next 60 seconds are counted on the host. When the window closes, they go out as **one** `incident.<kind>.repeated` row, with `count` and the window's start and end. A window with no repeats sends nothing.
- **The hourly cap.** A sandbox records at most 60 incident rows an hour, summaries included. Past that, the host only counts. At the end of that hour it sends one `incident.dropped` row with the total.

Each row's id is deterministic: `<source>:<subject>:<seq or window start>[:repeated|:dropped]`. A row the host sends again (see the outbox, below) is therefore the same row, and the unique index turns the replay into a no-op.

## How an incident travels

```
Docker events ─────────────┐
                           ├─▶ host outbox (per sandbox) ─▶ heartbeat ─▶ agent_sandbox_events
in-sandbox journals,  ─────┘                               (incidents)   (source = host,
drained by the host every                                                 event_type = incident.<kind>)
60 s, and on a Docker
incident
```

- **Docker events.** The host already talks to the Docker API (`cloud_agent_host.resources.daemon_client`). It adds one long-lived subscription to `/events`, filtered to `type=container` and `event=oom|die|kill`, and keeps the events of the containers its listing holds. It reconnects with backoff and asks Docker to replay from the last event it saw (`since`), so a gap in the stream still finds an exit.
- **In-sandbox journals.**
  - The browser service appends to `/run/agent-browser/events.jsonl`, and each slot's MCP runner to `/run/agent-mcp-slots/<slot>/events.jsonl`.
  - Each line is `{seq, ts, kind, …}`, and each journal is capped at 256 KiB, dropping its oldest lines.
  - Each is mode 0640, group `agent`, in a directory the group can traverse. The host drains them as root in that group (root needs no capability for a group-readable file), in **one exec per sandbox** that reads every journal from the last `(journal, seq)` the host holds for it. This is the guest observer's pattern; nothing in the sandbox opens a port, which would be one more surface the agent itself could reach.
  - It drains on two occasions:
    - every **60 seconds** while the container runs, so a browser that crashes between turns is recorded before the next turn, not at its end;
    - whenever a **Docker incident** arrives for that container.
  - There is no separate drain at turn end. An MCP program usually fails when the turn first lists its tools, so its row is normally stored, and its line shown, while that turn still runs. One that fails in a turn's last minute is told to the agent at its next turn instead.
- **The outbox.** The sandbox manager keeps at most 64 pending rows per sandbox, dropping the oldest, and counts what it drops into the next `incident.dropped`. It lists them with its groups; the runner carries them in a new field of the heartbeat's `RetainedSandboxBeat`, `incidents` (`cloud_agent_host.incident_relay`). The answer says `incidents: "accepted"` only when every row was stored, and only then does the runner tell the sandbox manager, at its next listing, to forget them; otherwise they are sent again.
- **Never refuse a heartbeat.** As with `resource_usage`, an incident the control plane cannot parse is dropped, not refused, and an unknown kind is ignored.

### When the agent hears about it

A claim never waits on the host: it reads what is recorded.

- An incident recorded before a claim is told at that claim.
- One that happens within about a minute before a claim (one drain plus one beat) can miss that claim. It is told at the next one.
- None is lost or told twice. That is the cursor's job, below.

## Peaks

`SandboxResourceSample` stays as it is, so a control plane that does not know peaks keeps reading every sample. The resource collector hands every 15-second reading to the incident observer as it is taken, with the container's memory and pids limits. A reading at 90% of a limit becomes `memory_high` or `pids_high`, carrying the reading and the limit, and the next one waits until a reading falls below 80%. A beat's own last reading is no longer the only one anybody looks at.

Readings 15 seconds apart still miss shorter spikes. A true peak (cgroup v2 `memory.peak`, `pids.peak`) comes later; the `out_of_memory` event already covers the spike that matters.

## Rollout: hosts first

The repository rolls hosts out before the control plane, and each side supports the previous release only (`AGENTS.md`). The design holds in both mixed states:

- **New host, old control plane.** `RetainedSandboxBeat` ignores fields it does not declare, so `incidents` is ignored and the rest of the beat, `resource_usage` included, is read as before. The old control plane never says `accepted`, so the host keeps its rows in the bounded outbox. The user-facing notices are written by the control plane itself (below), so an older UI never sees one. The new `sandbox-browser-v1` capability is stored like any other and read by nothing.
- **New control plane, old host.** No incidents arrive, no capability is reported, and no browser note is given: the same as today.

Neither side needs code kept for the other, so nothing is marked `COMPAT`.

## Telling the user

For `out_of_memory`, `container_exited` and `mcp_server_failed`, the control plane appends a `lifecycle` event, `{phase: "sandbox_incident", incident, …}`, to the running turns the incident concerns, when it first stores the row. Each append is fenced by its attempt's live lease, as the runner's own are:

- a sandbox-scoped incident goes to every turn running in that sandbox;
- a member-scoped one goes only to that member's own turn, if it has one running.

The chat shows it as one line in the turn it broke:

| Kind | Line |
| --- | --- |
| `out_of_memory` | "The environment ran out of memory, and some processes were stopped." |
| `container_exited` | "The environment stopped unexpectedly. It starts again with your next message." |
| `mcp_server_failed`, phase `start` | "*alias* could not start, so its tools were not available." |
| `mcp_server_failed`, phase `crashed` | "*alias* stopped unexpectedly. It starts again when it is next used." |

No limits, no exit codes and no advice are shown: an operator reads those on the admin page. A turn's failure message does not change. The incident line sits beside it and says why.

## Telling the agent

Both are new producers in `claim_prompt` (`cloud_agent/prompt_injections.py`), recorded by size like the others. `tests/unit/test_context_events.py`, which pins the producers, gains them.

- **`sandbox_browser`**, at a chat's first turn, and only when the claiming host reports the `sandbox-browser` capability. A host reports it when its sandbox image carries the browser helper label. A chat's first turn is its `turn_no` 1.

  > This sandbox has a Chromium browser, shared with this chat's forks. `cloud-agent-browser start` prints its CDP WebSocket URL; any CDP client can drive it. Save screenshots and downloads under `/workspace/.cloud-agent-artifacts`, where the user sees them.

  It states facts and names no tool, since the platform provides the browser and the user picks the tool. A native session that is rebuilt (lost, then started fresh at a later turn) should get it again, if the claim can tell; see *To verify*.

- **`sandbox_incidents`**, at a turn whose thread has untold, agent-relevant incidents. At most five lines, newest first, times in UTC:

  > Since you were last told: the sandbox ran out of memory at 15:02 and some processes were stopped. The browser restarted at 15:02; pages that were open are gone. `playwright` could not start.

### The cursor

Which incidents a thread has been told is a **per-membership cursor**, not a time. A new column on `agent_thread_sandbox_memberships`, `incidents_told_through`, holds the highest `agent_sandbox_events.id` told to that thread.

- **The claim** selects the sandbox's agent-relevant incidents with an id above the cursor: sandbox-scoped ones, and member-scoped ones carrying this thread's `thread_id`. It renders at most five, and records `through_id`, the highest id it selected, in the injection record. Selecting more than five still moves `through_id` past all of them, and the note says "and *n* more".
- **The cursor advances** to `through_id` only when that attempt's `started` event is stored. The advance is fenced to that attempt and never moves the cursor backwards. A claim that is abandoned or retried before it starts advances nothing, so its retry tells the same incidents again.
- **A new membership** starts at the sandbox's current highest id. A Fork joining a sandbox is not told its parent's past.

Ids order insertion, not observation. That is exactly what makes a late incident safe: it gets a higher id than anything already told, so it is told at the next claim, whenever it was observed. The note sorts what it tells by observation time.

## The admin page

The sandbox trajectory already lists `agent_sandbox_events`. It gains a label and a details line for each `incident.*` kind, including `repeated` and `dropped`, and the thread for a member-scoped one. The sandbox list gains "last incident", the newest incident within 24 hours, so a sandbox in trouble stands out without opening it.

## Not in scope

- **Alerts.** The incidents table is what a rule will read. Where alerts go (a webhook to Slack, Feishu or email) is not decided, so this change records, and alerting follows.
- **Fleet metrics.** A Prometheus endpoint on the runner, VictoriaMetrics or Prometheus, Grafana and node_exporter come when trends across hosts are needed. cAdvisor is not planned: on Kata it reads the host cgroups, which are empty.
- **True `memory.peak` and `pids.peak`** (see *Peaks*).

## To verify

1. **Kata delivers `oom` events to Docker.** The kata shim forwards the guest agent's OOM notifications, but this must be checked on our guest kernel and cgroup version. If it does not, the guest observer reads the guest's `memory.events` `oom_kill` counter instead.
2. **The claim can tell that a native session will start fresh** at a turn after the first, so the browser note is given again.

## Testing

- **Unit, host:**
  - Docker event parsing; the host's own stops, idles and deletions are ignored;
  - reconnect with an `inspect` sweep;
  - MCP and browser stop reasons, so that only an unrequested exit is an incident;
  - a member-scoped incident resolves its slot to the thread, including after the slot is reused;
  - the first occurrence goes out at once, repeats as one summary, and the hourly cap becomes `dropped`, every row final and with a deterministic id;
  - the outbox is held until a response confirms it, and is bounded.
- **Unit, control plane:**
  - heartbeat tolerance of unknown and malformed incidents;
  - a member-scoped incident is told only to its own thread;
  - both prompt producers and their conditions.
- **Database:**
  - the migration, and the unique index that makes a replayed row a no-op;
  - the cursor: it advances only on `started`, only forward, and only for its own attempt;
  - a retried claim tells the same incidents again;
  - an incident recorded late is told at the next claim.
- **Mixed versions:**
  - a new host against the current control plane: the beat is read, `resource_usage` is kept, incidents are held, and no notice or capability is sent;
  - the current host against the new control plane: no incidents arrive.
- **End to end on Docker:**
  - a memory hog in a sandbox: `out_of_memory` is recorded, a line appears in the running turn, and a note in the next turn's prompt;
  - `kill -9` of Chromium between turns: `browser_crashed` is recorded before the next claim and told at it;
  - an MCP package that cannot install: `mcp_server_failed` appears in that member's turn only, with another member's turn running alongside;
  - an idle MCP stop is not an incident.
- **Kata:** item 1 of *To verify*, on a Kata host.
