# Design: Computer Use Base Capability for Cloud Agent

Status: Phase 0 done, Phase 1 implemented · 2026-11-02, updated 2026-11-03

## Summary

Cloud Agent should ship computer use as a **platform base capability**, not as a choice of tool. The platform provides a browser (later a desktop) inside each task sandbox, a stable environment and connection contract, lifecycle and isolation. Users pick the MCP server or CLI that drives it; we ship one tested default template.

We land this in three phases:

1. **Browser runtime in the sandbox image.** Agents can drive it right away through any browser CLI they install in their home directory. No MCP changes.
2. **In-sandbox MCP servers.** A user-selected MCP server runs next to the browser, admitted per session by the trusted host, for all five runtimes.
3. **Live view, takeover and recordings.** A session-scoped, authenticated channel from the UI to the sandbox browser.

The Phase 0 spike answered the main open risk: Chromium runs under our current limits (uid 10001, `--cap-drop ALL`, `no-new-privileges`, `--pids-limit 512`, 4 GiB memory), but only without its own sandbox, because Docker's default seccomp profile refuses unprivileged user namespaces. [Phase 0 findings](#phase-0-findings) has the measurements and the decision.

## Background

Today no Cloud Agent runtime can see or operate a UI. Three facts cause this.

**The sandbox has no browser.** `deploy/sandbox/Dockerfile.agent-sandbox` installs no Chromium, no display server and none of Chromium's shared libraries. The agent runs as uid 10001 (or a member uid) without sudo, so it cannot `apt-get install` them during a task.

**Native computer use does not apply to us.** Claude Code computer use needs macOS, an interactive session and a claude.ai login; we run `claude -p --input-format stream-json`. Codex computer use ships in the desktop app; Codex Cloud lists browser and computer use as roadmap. pi, OpenCode and Kilo have no native equivalent.

**Every MCP server runs outside the sandbox.** The agent only sees the platform relay:

- Claude Code starts with `--strict-mcp-config --mcp-config <relay>` and `--setting-sources user`.
- Codex reads a root-owned `/etc/codex/requirements.toml` with an empty `[mcp_servers]` allowlist; the trusted host admits only the relay, as `session`.
- pi reaches MCP through `pi-mcp-bridge.ts`; OpenCode and Kilo are configured the same relay-only way.
- Remote MCP servers run in the control plane. Stdio servers are meant for a managed worker or a user connector, and `mcp/host.py` refuses them until those exist.

The relay-only wiring is deliberate: a repository cannot auto-start its own MCP servers. But it also means a user can already *select* Playwright MCP from the Recommended list in `mcp/catalog.py`, yet it would run where it cannot reach the task's `localhost`, its files or a browser in the sandbox.

Other platforms close this gap with an in-environment browser or desktop: Cursor Cloud Agents and Devin give the agent a full desktop with recordings and takeover; GitHub Copilot's cloud agent runs Playwright MCP against `localhost` by default; Replit runs an App Testing browser. Which driver tool is best is not settled: public benchmarks (WebdriverIO's tool comparison, Browser Use's BU Bench) show no stable winner across models.

## Goals and non-goals

**Goals**

- An agent in any of the five runtimes can open, read and operate a web page served by the task itself (`localhost`) or by an allowed external host.
- The user chooses the driver tool (MCP server or CLI); the platform does not hard-code one.
- One default template works out of the box for users who do not want to choose.
- Browser state is scoped to a chat session and reclaimed with it.
- Existing guarantees hold: repositories cannot auto-start MCP servers, egress stays guarded, no credentials leak into the sandbox.

**Non-goals**

- Building our own UI-understanding model, planner or browser agent.
- A full graphical desktop in phase 1. The contract leaves room for it; we add it only when a concrete task needs non-browser apps.
- Operating the user's own machine or logged-in browser.
- Picking the "best" tool in this doc. The default is chosen by the evaluation in a later section.

## Requirements

Each requirement names what the platform owns; everything else belongs to the user's agent and tool.

| # | Requirement | Platform owns | Phase |
| --- | --- | --- | --- |
| R1 | Runtime environment | Chromium and its libraries in the image; a display only when a desktop is added | 1 |
| R2 | Reach the task | Browser runs in the task's network namespace, sees its `localhost` and files | 1 |
| R3 | Perception and input | Nothing beyond R1: accessibility tree, DOM, screenshots and input come from CDP, used by the tool | 1 |
| R4 | Coding-loop signals | Console, network and page errors reachable from the same tool session | 1 |
| R5 | Tool attachment | A user-chosen CLI or MCP server can run next to the browser, with a well-known connection contract | 1 (CLI), 2 (MCP) |
| R6 | Session continuity | Cookies, tabs and storage persist across turns of one chat | 1 |
| R7 | Lifecycle | Start on demand, idle reclaim, crash recovery, kill with the sandbox | 1 |
| R8 | Isolation | No browser shared between chats or members; no control endpoint reachable from outside the sandbox | 1 |
| R9 | Evidence | Screenshots and traces land in the workspace and show in the UI | 1 (files), 3 (recordings) |
| R10 | Live view and takeover | User watches, pauses and takes control of the browser | 3 |

## Options considered

We choose C first, then B; A is deferred until live view needs an external channel anyway. The question is where the user's tool runs relative to the browser.

| Option | Where the tool runs | What changes | Main cost | Verdict |
| --- | --- | --- | --- | --- |
| A. Expose CDP to external MCP | Control plane or user connector | Sandbox publishes a per-session CDP endpoint through an authenticated tunnel; relay-hosted MCP connects with `--cdp-endpoint` | CDP is full browser control (read any page, run any script, read cookies); needs per-session auth, tunnel, revocation | Defer to phase 3 |
| B. In-sandbox MCP server | Inside the sandbox | Trusted host admits one user-chosen stdio server per session, next to the relay, in all five runtimes | Five runtime wirings; must keep repository configs out; pinned install inside the sandbox | Phase 2 |
| C. In-sandbox CLI | Inside the sandbox | Nothing in MCP; agent calls a browser CLI from its shell | No structured tool schema; quality depends on the agent reading CLI help | Phase 1 |

Why this order:

- **C needs only R1.** Playwright CLI, agent-browser and Browser Use CLI install with `npm` or `uv` into the user's home. Every runtime already has a shell tool.
- **B keeps control local.** The browser's control port never leaves the sandbox, so there is no new remote attack surface. The new surface is admission, which the trusted host already does for the relay.
- **A is the only option that needs a channel from outside into the sandbox.** Phase 3 must build that channel for live view and takeover. A then becomes cheap to add on the same channel.

## Phase 0 findings

Measured on 2026-11-03 with Debian bookworm's `chromium` 156.0.8078.79 in a `node:22-bookworm-slim` container started with the retained sandbox's flags (`--user 10001`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 512`, `--memory 4g`, `--init`). The Kata guest uses the same OCI seccomp profile (`Seccomp=2`, see `chat_sandbox.py`), so the namespace result carries over; Kata itself was not available on the test host.

| Question | Result |
| --- | --- |
| Unprivileged user namespaces | Refused (`unshare --user`: operation not permitted) |
| Chromium with its own sandbox | Fails at start: "No usable sandbox!" |
| Chromium with `--no-sandbox` | Starts and renders headless |
| Image growth | About 274 MB uncompressed for Chromium, its libraries and two font packages |
| Idle browser | 11 processes, about 140 threads, 174 MiB |
| Browser with 4 real pages (GitHub, Hacker News, Wikipedia, MDN) | 15 processes, about 213 threads, 321 MiB |

**Decision on Chromium's sandbox.** The managed browser runs with `--no-sandbox` when the guest refuses user namespaces, and `cloud-agent-browser status` reports `chromium_sandbox: false`. Reasons:

- The member UID already runs unvetted repository code; the image's threat model treats that side as untrusted. A compromised renderer gains what that code already has.
- The boundary that matters, the Kata VM plus the member UID, is unchanged.
- The platform cannot enforce Chromium's sandbox anyway: a member can start the same binary, or a browser a tool downloads, with `--no-sandbox` itself.
- The alternatives (a custom seccomp profile that allows `CLONE_NEWUSER`, or a sandbox per browser-using member) widen the kernel surface or cost a VM each, for no gain against the threat above.

The launcher prefers Chromium's sandbox whenever user namespaces work, so a later seccomp change takes effect without a new image. `ALLOW_UNSANDBOXED` in the launcher is the switch if this decision is reversed.

**Threads.** One browser with a few pages stays well inside 512, but the limit is per group and counts threads, and a Fork's sibling may run its own browser. The launcher caps renderers at four (`--renderer-process-limit=4`). We keep the group limit as it is and watch it (see [Resources and performance](#resources-and-performance)).

## Proposed design

The platform ships Chromium, a launcher and a small environment contract. Tools are attached on top of that contract and never baked in.

```mermaid
flowchart LR
    subgraph CP["Control plane"]
        relay["MCP relay<br/>remote servers"]
        ui["Chat UI<br/>files today, live view later"]
    end
    subgraph SB["Task sandbox · Kata VM, guarded egress"]
        subgraph M["Member slot agent-member-N"]
            rt["Agent runtime<br/>Claude Code, Codex, pi, OpenCode, Kilo"]
            cli["Browser CLI<br/>phase 1, via shell"]
            mcp["MCP server<br/>phase 2, admitted"]
            launcher["cloud-agent-browser<br/>CDP WebSocket on 127.0.0.1, token-guarded"]
            chrome["Chromium, headless<br/>per-chat profile"]
        end
        art["Artifacts dir<br/>screenshots, traces"]
        dev["Task dev server<br/>on localhost"]
    end
    rt -- unchanged --> relay
    rt --> cli
    rt --> mcp
    cli --> launcher
    mcp --> launcher
    mcp --> art
    launcher -- pipe --> chrome
    chrome --> dev
    art --> ui
    ui -. "phase 3: live view" .-> launcher
```

The launcher is the contract boundary: everything above it is the user's choice, everything below it is the platform's. Only the existing relay leaves the sandbox until phase 3 adds the live-view channel (dashed).

### Browser runtime (phase 1)

- **Binary.** Debian's `chromium` package (full Chromium, not headless-shell, so a headed mode stays possible for phase 3), plus `fonts-liberation` and `fonts-noto-color-emoji`, in its own layer of `Dockerfile.agent-sandbox`. Debian's build rather than a pinned Chrome for Testing download: it exists for amd64 and arm64 (Chrome for Testing has no arm64 Linux build), it takes Debian's security updates with every rebuild, and its dependencies are exactly the shared libraries that a browser a tool downloads for itself needs. CDP clients connect to any current Chromium, so an exact Playwright-matched build buys nothing here.
- **Launcher.** `/usr/local/bin/cloud-agent-browser`, root-owned and mode 0555, Python standard library only, with `start`, `status`, `stop` and `self-test`. `start` reuses or launches one headless Chromium for the calling member UID (`--headless=new`, `--disable-dev-shm-usage`, background networking, component updates, crash upload and metrics off, at most four renderers, the per-chat profile). It prints the CDP WebSocket URL.
- **One pipe, many clients.** Chromium speaks CDP only over `--remote-debugging-pipe` to the launcher's daemon. The daemon serves it as a WebSocket on `127.0.0.1`, at a random port, behind a random path token (`ws://127.0.0.1:<port>/<token>/devtools/browser`). Each client gets its own browser-level session (`Target.attachToBrowserTarget`). Its command ids are rewritten into one space, and every reply and event goes back only to the client that owns the session it came from. Two Playwright clients and two Puppeteer clients worked at the same time in the spike. `Browser.close` from a client ends only that client's connection, because the browser belongs to the member, not to one tool.
- **No HTTP discovery.** There is no `/json/version` endpoint. Clients resolve it against the server's root, which would drop the token from the path. Every client we target accepts a WebSocket URL directly.
- **Downloads stay possible.** Egress allows them (public IPv4 TCP 80/443), and a repository's own tests may depend on the exact browser its tooling downloads, so the image sets no `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD` or `PUPPETEER_SKIP_DOWNLOAD`. It sets `CHROME_PATH`, which tools that look for an installed Chrome read. The preinstalled browser still matters: it needs no download, and its libraries are what lets a downloaded browser start at all as a non-root member.
- **Tool install.** Users install a CLI into `~/.local` or `~/.npm-global` from their environment setup, as with any other dev tool. Pinning is the user's choice in phase 1.

### Environment and connection contract (v1)

This is the stable surface that templates and user tools rely on. It is versioned; additions are backward-compatible.

| Name | Kind | Meaning |
| --- | --- | --- |
| `cloud-agent-browser start` | command | Ensures the browser runs; prints the CDP WebSocket URL (`ws://127.0.0.1:<port>/<token>/devtools/browser`) |
| `cloud-agent-browser status` | command | JSON: `running`, `endpoint`, `browser`, `chromium_sandbox`, `profile`, `artifacts_dir` |
| `cloud-agent-browser stop` | command | Stops the browser; the profile is kept |
| `cloud-agent-browser self-test` | command | Starts the browser and checks it end to end (two clients, a page, a screenshot, session isolation) |
| `CLOUD_AGENT_BROWSER_CONTRACT` | env, image | Contract version, `1` |
| `CLOUD_AGENT_BROWSER_BIN` | env, image | `/usr/bin/chromium` |
| `CLOUD_AGENT_ARTIFACTS_DIR` | env, image | `/workspace/.cloud-agent-artifacts`: screenshots and traces; shown in the workspace files, kept out of Git |
| `CLOUD_AGENT_BROWSER_STATE_DIR` | env, host | `/run/agent-members/<slot>/browser`: the 0700 directory holding the endpoint file (0600) and the daemon's log |
| `CLOUD_AGENT_BROWSER_PROFILE` | env, host | `/home/agent-member-<slot>/.cloud-agent-browser/profiles/<sha256 of the chat>` |
| `CLOUD_AGENT_BROWSER_IDLE_S` | env, optional | Idle timeout in seconds; default 900 |

"env, image" values are the same in every sandbox. "env, host" values are set per member by the resident launch (`cloud_agent_host/sandbox_browser.py`), for all five runtimes. Without them, the launcher falls back to directories under `$HOME`.

**Managed vs self-launched browsers.** Only a browser started by the launcher is *managed*: it gets the guarantees in this doc (idle reclaim, session continuity, token-guarded CDP, per-member isolation, operator signals). A tool may still launch `CLOUD_AGENT_BROWSER_BIN` itself; that browser is an ordinary user process. It is killed with the sandbox and counted against its limits, but the platform makes no promise about its debug port, profile or lifetime. Templates always use the managed browser.

### In-sandbox MCP servers (phase 2)

- **New location.** Add `sandbox` beside `remote`, `managed-worker` and `user-connector` in the MCP connection model. Only servers with a catalog template or an explicit user entry (package, pinned version, args) can use it.
- **Admission stays with the trusted host.** At session start the host installs the pinned package with the existing `mcp/launch.py` policy (allowed commands, pinned versions, `--ignore-scripts`, no shell) and adds exactly that server to each runtime's generated config. Repository configs remain ignored.
- **Per-runtime wiring.** Claude Code: one more entry in the host-generated `--mcp-config`, still under `--strict-mcp-config`. Codex: the host admits one more name in the root-owned `requirements.toml`, beside `session`. pi: `pi-mcp-bridge.ts` gains a stdio target. OpenCode and Kilo: one more local server in the generated config.
- **Templates.** Catalog entries carry the args that bind a server to the contract, for example Playwright MCP `--cdp-endpoint <CDP URL>` and Chrome DevTools MCP `--wsEndpoint <CDP URL>` (its `--browser-url` takes an HTTP endpoint, which the launcher does not expose). Users can copy and edit a template or add a custom server.
- **Change and revocation.** The runtime CLIs stay resident across turns, and an in-sandbox server is not behind the relay, so the relay cannot cut it off. The trusted host owns the server's lifecycle instead. Each admitted server carries a config fingerprint (package, version, args). At every turn start the host compares it with the user's current selection. If the server was disabled, it is stopped and removed from the runtime config before the turn runs. If the config or version changed, the old process is stopped and the new one starts at that turn. A change never applies in the middle of a turn. A runtime that cannot reload its MCP config in place is restarted at the turn boundary.
- **Results.** Image results already flow through `protocol_compat/tool_images.py`; templates set an output dir under `CLOUD_AGENT_ARTIFACTS_DIR` so large screenshots are files, not inline tokens.

### Live view and takeover (phase 3)

The launcher becomes a platform-owned browser supervisor. It streams frames over CDP `Page.startScreencast` to the UI through an authenticated, session-scoped channel, and forwards user input during takeover while the agent's turn is paused. The same channel can later carry option A (external CDP) and a desktop display.

## Security and isolation

The browser adds no new trust boundary in phases 1 and 2: it runs as the agent's uid, inside the same Kata VM and guarded network, and can do nothing the agent's shell cannot already do.

- **CDP is token-guarded on loopback.** One sandbox hosts several resident member slots (`agent-member-N`), and loopback is shared by every UID in it. So Chromium never opens a TCP debug port itself; only the launcher's WebSocket is on `127.0.0.1`. Its token lives in the member's 0700 tmpfs, the path is compared in constant time, and the `Host` header must name loopback. This is the same loopback-plus-secret shape as the OpenCode and Kilo bridge. No port is published from the sandbox until phase 3, and that channel is authenticated per session and revocable.
- **Clients are isolated from each other.** A client can only send commands on sessions it created; any other session id gets CDP's "session not found" error, and events reach only the owning client. Pages themselves are shared: every client of the member's browser sees every tab.
- **Chromium's own sandbox.** `--cap-drop ALL` and `no-new-privileges` rule out the setuid sandbox, and Docker's default seccomp profile refuses the user namespaces its namespace sandbox needs. The managed browser therefore runs with `--no-sandbox`, a decision recorded with its reasons in [Phase 0 findings](#phase-0-findings). It is visible (`chromium_sandbox` in `status`) and reversible in one place.
- **Egress.** Browser traffic uses the sandbox's single guarded network and resolver. A page can reach only what the agent could reach with `curl`. No new allowlist entries are added for browsing.
- **Untrusted page content.** Pages can carry prompt injection. This is the same risk as a fetched web page today; templates default to the task's `localhost` and allowed hosts, and the agent sees page text as tool output, not instructions.
- **Repository configs stay inert.** In-sandbox MCP servers are admitted only by the trusted host from the user's selection. `.mcp.json`, `.codex/config.toml` and similar files in the repo still start nothing.
- **Relay authorization.** An in-sandbox server is not behind the relay's authorization layer. That is acceptable because its power equals the agent's shell; audit comes from the runtime's tool-call events, as for shell commands.
- **Credentials.** The profile holds only cookies created during the task. We do not import user browser profiles. Profiles are deleted with the chat unless the user opts into keeping a login (open question).

## Session semantics

A browser belongs to exactly one chat member, the same unit that owns a resident slot and its private tmpfs. Nothing is shared between members, even inside one sandbox.

| Event | Browser process | Profile (cookies, storage) | Open tabs |
| --- | --- | --- | --- |
| New turn in the same chat | Reused if alive, else relaunched | Kept | Kept if the process survived |
| Idle past the reclaim timeout | Stopped by the launcher | Kept | Lost |
| Sandbox stop or eviction | Killed with the sandbox | Kept: homes live in the retained container's writable layer | Lost |
| Fork | Not copied; the fork starts its own browser on first use | Fresh, empty profile | Lost |
| Chat's member cleaned up | Killed with the member's processes | Kept on disk until the container is removed; no other chat can open it | Lost |
| Two tools or subagents in one chat | Shared, one daemon per member | Shared | Shared; each client has its own CDP sessions and should open its own tab |

Rules behind the table:

- **Fork starts clean.** `session_fork.py` materializes residencies and credentials for a new job. A live CDP connection cannot be forked, and copying cookies would hand the fork the parent's access to every site it logged into. That is a product decision about who may act as whom, not an implementation convenience. So a fork gets a fresh profile, and login inheritance is designed separately (open question).
- **One browser per profile, not one client.** Chromium's profile lock only stops two browser processes from opening the same directory. It does not stop several CDP clients (two tools, or two subagents) from driving the same managed browser and interfering with each other's tabs, cookies or downloads. The launcher accepts any number of CDP clients; coordinating them is the tools' job (next rule).
- **Tab ownership is the tool's job.** The platform does not arbitrate tabs between subagents; templates open a new page per agent by default.

## Resources and performance

A browser that is never started costs only image size; the costs below apply only to tasks that use it. All numbers are to be measured in the phase 1 spike, not assumed.

| Limit today | Where | Why the browser stresses it | Mitigation |
| --- | --- | --- | --- |
| 4 GiB memory default | `chat_sandbox_models.py` `memory_limit`, `sandbox_memory_mib` setting | Chromium plus a dev server plus the agent CLI share one cgroup; an OOM kill takes the whole sandbox | Measured 174 MiB idle, 321 MiB with 4 pages. Launch on demand, idle reclaim, at most 4 renderers. No change to the default |
| `--pids-limit 512` | `chat_sandbox.py` | The limit counts threads, and Chromium runs many processes and threads | Measured about 140 threads idle and 213 with 4 pages. Renderers capped at 4. No change now; raise it for the group if browser starts begin to fail on it |
| `/dev/shm` 64 MiB (Docker default) | container runtime | Chromium uses shared memory for rendering | `--disable-dev-shm-usage` in the launcher |
| Image size (about 5.3 GB today) | `Dockerfile.agent-sandbox` | Chromium and its libraries add a layer | About 274 MB uncompressed, in its own cacheable layer |
| CPU limit | `cpu_limit` | Rendering and screenshots compete with builds | Headless only in phases 1 and 2 |

Startup is paid once per member per idle period, not per tool call, because the launcher reuses the process.

## Evidence, observability and UX

Phase 1 shows evidence as files; phase 3 adds a live view. No new UI is needed for phase 1.

- **Screenshots and traces.** Tools write into `CLOUD_AGENT_ARTIFACTS_DIR` (`/workspace/.cloud-agent-artifacts`). The workspace file listing walks the directory tree and skips only `.git`, and `WorkspaceFiles.tsx` already previews PNG, JPEG, GIF and WebP, so the user can open what the agent saw. `cloud-agent-browser start` creates the directory and adds it to `.git/info/exclude`, the way the environment tools directory is excluded, so it never enters a diff or a published branch.
- **Inline images.** When a tool returns an image in a tool result, `protocol_compat/tool_images.py` already moves it into a user message for runtimes that need that.
- **Operator signals.** The launcher logs start, stop, crash and reclaim with the member slot and duration. We add counters for browser starts, crashes, OOM kills of sandboxes with a running browser, and peak threads.
- **Live view (phase 3).** A browser pane in the chat shows the screencast. Takeover pauses the agent's turn, forwards the user's input, and hands control back with a short note to the agent about what changed.
- **Recordings (phase 3).** Screencast frames or a Playwright trace, saved to the artifacts dir and linked from the turn.

## Rollout plan

Each phase is useful on its own, and a gate must pass before the next one starts. Dates are set after the spike.

```mermaid
flowchart TB
    p0["<b>Phase 0 · Spike</b> (done)<br/>Chromium under Kata, uid 10001, cap-drop ALL, pids 512, 4 GiB<br/>Measure image delta, memory, threads, start time"]
    g0{{"Gate: Chromium starts reliably within the sandbox limits; security review of the Chromium sandbox mode"}}
    p1["<b>Phase 1 · Browser runtime</b> (CLI path, no MCP change)<br/>Chromium and launcher in the sandbox image<br/>Contract v1; browser CLIs work in all five runtimes"]
    g1{{"Gate: baseline acceptance passes (see below)"}}
    p2["<b>Phase 2 · In-sandbox MCP</b> (user picks the server)<br/>New sandbox location; host-admitted, pinned install<br/>Wiring for Claude Code, Codex, pi, OpenCode, Kilo<br/>Catalog templates bound to the CDP URL"]
    g2{{"Gate: security review passed; repository MCP configs still start nothing"}}
    p3["<b>Phase 3 · Live view and takeover</b><br/>Platform browser supervisor; screencast to the UI<br/>Pause, take over, hand back; recordings<br/>Later: external CDP (option A) and a desktop"]
    p0 --> g0 --> p1 --> g1 --> p2 --> g2 --> p3
```

Phase 1 already unblocks most use cases: an agent can verify its own frontend change with any browser CLI. Phases 2 and 3 improve tool choice and user trust, not basic capability.

**Baseline acceptance (Phase 1 gate).** The gate checks the platform, not which tool is best:

- At least two independent tools (for example Playwright CLI and agent-browser) connect to the managed browser through the contract.
- Each opens the task's own `localhost` app, completes a multi-step interaction and writes a screenshot to `CLOUD_AGENT_ARTIFACTS_DIR`.
- Isolation tests pass: another member uid in the same sandbox cannot connect to the CDP endpoint or read its token.
- Lifecycle tests pass: idle reclaim, crash relaunch, kill with the sandbox, no orphaned processes.

The default-tool evaluation below runs in parallel and does not gate any phase.

**Phase 1 acceptance so far.** In a container with the sandbox's flags, against a fixture app on `localhost`: two Playwright clients (`connectOverCDP`) at once, one clicking and screenshotting into the artifacts directory; two Puppeteer clients (`browserWSEndpoint`), one of them calling `browser.close()` without ending the browser for the others; a member UID other than the browser's unable to read the endpoint file and refused (404) on a guessed path; a killed Chromium cleaning up its endpoint and restarting on the next `start`; an idle browser stopping with no process left; a different chat's profile never handed over on the same slot. Still to run: the same on Kata, through a real chat turn.

**Delivery.** Phases 1 and 2 ship as two pull requests:

1. **Base capability** (this doc, Phase 0 findings, Phase 1): the sandbox image with Chromium, the `cloud-agent-browser` launcher, contract v1 env injection, the artifacts dir, and the isolation, lifecycle and baseline acceptance tests.
2. **In-sandbox MCP** (Phase 2): the `sandbox` connection location with its migration, catalog templates, MCP directory UI, host admission and change/revocation lifecycle, and wiring for all five runtimes. Branched from `dev` after PR 1 merges.

Phase 3 gets its own design doc once both are in production.

## Evaluation plan for the default template

The default is the tool with the highest success rate on our own tasks; cost and stability break ties. Public benchmarks are inputs, not the decision: their winners change with the model.

**Task set.** 20 to 30 tasks drawn from real Cloud Agent work, each with a scripted pass check:

- Verify a UI change on the task's own dev server and attach a screenshot.
- Reproduce a reported frontend bug and name the failing console or network error.
- Complete a multi-step form or login flow on a local fixture app.
- Check a page for broken layout at two viewport sizes.
- Read data from a page that needs scrolling or pagination.

**Candidates.** Playwright CLI, Playwright MCP, agent-browser, Chrome DevTools MCP and Browser Use CLI. Stagehand needs its own model calls, so it is measured but billed separately.

**Runs.** Claude Code and Codex, the two runtimes most users pick; three runs per task and tool.

**Metrics.** Pass rate, tokens and cost per task, wall time, tool or browser crashes, peak sandbox memory and threads.

**Output.** A results table in the repo and a one-line decision in this doc. Until then the catalog marks no tool as default. We rerun when a runtime or model changes.

## Open questions

- [x] Do unprivileged user namespaces work in our Kata guest? No (Docker's default seccomp profile); the managed browser runs with `--no-sandbox`, see [Phase 0 findings](#phase-0-findings).
- [x] Do the 4 GiB and 512-pid limits hold? Yes for one browser with a few pages; confirm on Kata with a heavy SPA and two browsers in one group.
- [x] Is the member's home persisted across sandbox eviction? Yes, in the retained container's writable layer.
- [ ] Should profiles of chats whose member was cleaned up be deleted before the container is removed?
- [ ] How do agents learn the browser exists? Phase 1 relies on `chromium` and `cloud-agent-browser` being on `PATH` and on the environment; a short platform instruction to every runtime is a follow-up.
- [ ] Login inheritance: should a user be able to opt a fork, or a new chat, into a saved login? Who approves it, and how is it scoped?
- [x] Where does `CLOUD_AGENT_ARTIFACTS_DIR` live? `/workspace/.cloud-agent-artifacts`, excluded through `.git/info/exclude`.
- [ ] Phase 2: do we let users enter arbitrary in-sandbox servers, or only catalog templates at first?
- [ ] Phase 3: is takeover input forwarded through the existing session channel, or a new one?
- [ ] When does a full desktop (display server, non-browser apps) become worth its cost?

## References

Repository (branch `dev`):

- `deploy/sandbox/Dockerfile.agent-sandbox`, `deploy/sandbox/codex-requirements.toml`
- `apps/backend/cloud_agent_host/chat_sandbox.py`, `chat_sandbox_models.py` (limits, member slots, tmpfs)
- `apps/backend/cloud_agent_host/runtimes/claude.py`, `codex.py`, `pi.py`, `opencode.py`, `kilo.py`
- `apps/backend/cloud_agent/mcp/catalog.py`, `launch.py`, `host.py`, `transports.py`
- `apps/backend/cloud_agent/application/session_fork.py`
- `docs/agents/specs/2026-09-16-mcp-setup-experience.md`

Driver tools:

- [Playwright MCP](https://github.com/microsoft/playwright-mcp)
- [Playwright CLI](https://github.com/microsoft/playwright-cli)
- [agent-browser](https://github.com/vercel-labs/agent-browser)
- [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp)
- [Browser Use](https://github.com/browser-use/browser-use)
- [Stagehand](https://github.com/browserbase/stagehand)

Benchmarks: [OSWorld](https://os-world.github.io/), WebArena-Verified, Online-Mind2Web, the WebdriverIO browser-tool comparison and Browser Use's BU Bench. Figures from these were gathered during earlier research and are not restated here.
