# Product: one MCP concept for users

Status: decided · 2026-11-04

## Summary

A user adds an MCP server and the agent can use it. That is the whole concept. Where the server runs is decided by what the user adds, not by a choice the user makes:

- **An address** (`https://…`) is a service on the internet. The platform connects to it, as today.
- **A command** (`npx pkg@1.2.3`, `uvx pkg==1.2.3`) is a program. It runs inside the chat's sandbox, next to the agent, so it reaches the task's `localhost`, its files and the sandbox browser.

"Sandbox MCP", "remote MCP" and "run location" stay internal words. The UI, the docs for users and the agent's own instructions do not use them.

## Why

PR #1097 (in-sandbox MCP servers) made a command-type server useful in a cloud chat for the first time. It also exposed the implementation as a product concept:

- Adding a command asks the user to pick where it runs: their computer, a "cloud worker" that does not exist yet, or "each chat's sandbox".
- Two servers the user thinks of as the same kind of thing behave differently. One may hold a credential, the other may not. One asks for approval before a tool call, the other does not. One lists its tools in settings, the other shows none.

Other cloud agents do not ask users this question. As far as their public documentation and community reports show, a cloud session runs the project's MCP configuration where the agent runs: Claude Code's cloud sessions load the repository's `.mcp.json`. Cloud support for command servers elsewhere is limited: Codex documents that local servers may not be available in the cloud, and Cursor users report servers added to background agents not working. Nothing we found makes "where it runs" a user setting. We should not either.

## The rules

### Adding a server

| The user gives | It is | Runs | Reaches |
| --- | --- | --- | --- |
| An `https://` address, a catalog service (GitHub, Slack, …) | A service | Where it is hosted; the platform connects to it | The internet |
| A command: `npx` or `uvx` and one package | A program | In each chat's sandbox, with the agent | The task's `localhost`, files and browser, and the internet |

- The command form keeps today's guarantees: one exactly pinned package, started by `npx` or `uvx`; no shell and no unpinned `latest`.
- **A bare package name is pinned for the user.** READMEs write `npx -y @scope/server` or `@scope/server@latest`, and almost nobody adds a version. Adding such a command asks the registry for the current release and stores that exact version (`@scope/server@1.4.0`, `server==1.4.0`), shown before the user confirms. It changes only when the user edits the server.
- A pasted configuration (Claude, Cursor, VS Code JSON) is classified the same way, entry by entry. A command entry becomes a sandbox program, instead of today's default, "your computer".
- **Running on the user's own computer** (the MCP connector) is removed. No connector runtime ever shipped, so the option could only fail.
- **The managed worker** is removed too: it never had a runtime behind it.
- A connection stored at either one becomes a program (*Removing the dead locations*, below).

### Credentials

**Decided: a program may use credentials, with no warning in the UI.** The product already puts the owner's own secrets into the sandbox: their environment variables reach every command the agent runs. A ban on credentials for programs protected nothing that was not already there, and it was the difference a user would hit first ("why can't my server have its API key?"). A warning would explain a distinction users should not need to know.

A program's credentials come from two places:

- **Its own configuration.** A key pasted with the server (`"env": {"API_KEY": "…"}`) is sealed at rest, as an imported command's keys are today, and decrypted only at the claim that runs it.
- **The chat's environment.** `${NAME}` in a program's arguments or `env` is filled from the chat's environment variables.

Neither value is written where another slot, or the gateway, can read it. The host hands them to the slot's runner as that runner's own user, into its private directory. The admission, which every slot can read, names only the variable names.

### Approval

A service's tools go through the relay's per-tool policy (allow, confirm, deny). A program in the sandbox has the agent's own power, the same as its shell, and is not behind the relay.

- **Now:** the per-tool policy controls are hidden for a program rather than shown and ignored. No extra explanation: there is nothing for the user to decide.
- **Later, if needed:** the sandbox gateway can enforce allow and deny per tool, and forward confirm to the control plane.

### Tools in settings

Both kinds list their tools. A program's list is reported by the sandbox the first time a chat lists it, and whenever it changes, and stored as a service's discovered capabilities are. The report is a JSON-RPC method on the relay's own endpoint (`cloud_agent/sandbox_tools`), so the sandbox still reaches the relay and nothing beneath it. Until then, the card says "its tools appear once a chat has used it", instead of showing zero.

### Errors in the user's words

| Situation | Message |
| --- | --- |
| Command with a range (`^1.2`), or a package the registry cannot find | "Name one release of pkg, for example `pkg@1.2.3`." A bare name or `latest` is pinned instead. |
| Command that is not `npx`/`uvx` | "Add a command that starts with `npx` or `uvx` and one package." |
| A program that failed to start in a chat | The chat goes on without it, and its CLI reports the server as failed. A clearer message in the chat is *later*. |

Today's extras-syntax bug (`uvx --from 'pkg[extra]==1.2.3' …` rejected as unpinned) is fixed as part of this.

## What changes

- **UI.** Remove the run-location picker, "Cloud worker" and "your computer". A command entry runs in the sandbox. Cards say *Online service* or *Program*. Hide approval controls for programs, and list their tools once reported.
- **API and data.** No new location: `remote` and `sandbox` are the only stored values, chosen from what was added. Import defaults command entries to `sandbox` and pins a bare package name to its current release (`cloud_agent/mcp/package_pins.py`). A program's sealed keys and `${NAME}` references are accepted.
- **Host.** Resolve a program's credentials (its sealed keys from the claim, `${NAME}` from the member's environment) and hand them to the slot's runner, as that runner's user, into its private directory; a change restarts only the affected servers. Report a program's tool list back to the control plane once per change.
- **Docs and agent instructions.** Say "MCP servers"; describe programs as "running next to you in the sandbox".

## Removing the dead locations

A follow-up change removes `managed-worker` and `user-connector` from the code and the schema. On staging on 2026-11-04, no connection, connector or connector link was stored at either one, in the current database or in the archived one.

- **API.** Both values are refused, as is `connector_id`; a connection view no longer carries `connector_id`. The import derives no mounts from a command's paths: it still warns that a chat cannot reach them.
- **Data.** `0115_mcp_retire_dead_locations` moves every connection stored at either location to `sandbox`, with its launch as stored, and narrows the location check to `remote` and `sandbox`. Its configuration revision and authorization epoch move on, as a location edit moves them, and a failing diagnosis for the old location is cleared. Nothing is deleted.
- **What the owner sees.** A moved connection is a *Program* card. A chat runs it when its command passes the sandbox's rules; otherwise the claim leaves it out, as it does any program that does not. Its owner can disconnect it and add it again, which pins a bare package name, or edit its command through the API.
- **Rollout.** The previous release still reads and maintains the connector tables and `agent_mcp_connections.connector_id`, so they stay in the schema, unused, and a later release drops them. Its writes of a removed location are refused while it serves beside the new one. Rolling back needs no downgrade: the previous release reads a moved connection as a program.

## Not in scope

- Approval enforcement for programs (above, *later*).
- A program's start failure shown in the chat, in the user's words.
- A full desktop, or live view and takeover (phase 3 of computer use).

## Decided after review

1. A program is addable by a bare package name ("`@playwright/mcp`"); the platform pins the current release when it is added (*Adding a server*, above).
2. "Run on my computer instead" is removed: it offered a connector that no longer runs.
3. The two locations with no runtime are removed from the code and the schema, and a connection stored at one becomes a program (*Removing the dead locations*).
