# Memory, avatars, and home suggestions

**Memory** is the lasting rules agents follow for you: agents save them during
tasks, and you can write your own. **Avatars** and **home suggestions** are
worked out from your recent requests each time you open the homepage. They are
not memory: nothing about them is stored, given to agents, or affected by the
memory settings. See the
[memory redesign](../agents/specs/2026-10-13-memory-redesign.zh-CN.md) for the
reasoning behind this split.

## Memory

Each memory is one short rule, such as "Use pnpm, not npm.", kept until you
change or delete it. **Setting → Memory** shows it in two scopes: **User
memory** applies to every project, and **Project memory** to one repository,
where it takes precedence over user memory.

- **Agents keep them, as Claude Code does.** Every task can call three tools on
  the platform's MCP endpoint: `memory_save`, `memory_update` and
  `memory_delete`. The agent decides when: you ask it to remember something,
  you correct how it works, or a memory turns out to be wrong. It writes each
  memory as one English sentence. No extra model call is made. Updating or
  deleting archives the old memory, which you can restore, including memories
  you wrote yourself.
- **Each task sees them.** When a task starts, the active memories of its
  repository and your user memories are added before the request, with the ids
  the tools take, up to 12,000 characters: repository memories first, and
  within each scope the most recently written or edited first. Nothing is added
  when there are none. Only the memories added get a new `last_used_at`.
- **Only while the task runs.** Every write checks, in its own transaction,
  that the task's attempt is still the running one; a task that was stopped or
  replaced cannot change memory, even with a request already under way.
- **Scope.** An agent saves to its task's repository unless you say a rule
  applies to all projects; a chat without a repository saves user memories. An
  agent can only see and change memories of its task's repository and your user
  memories.
- **Switches.** Each scope has its own **Use memory** switch. A scope that is
  off is neither shown to tasks nor written by agents; its memories are kept.
- **Limits.** Each scope keeps 30 active memories of at most 240 characters.
  When a scope is full, the least recently used memory an agent saved is
  archived to make room. Memories you wrote or edited are never archived this
  way; a scope full of them accepts no new memory. Archived memories can be
  restored and are deleted after 90 days.
- **Duplicates.** Adding a memory identical to an active one in the same scope,
  ignoring case and final punctuation, returns the existing memory.

In Settings you can add, edit and delete memories, restore archived ones, turn a
scope off, or clear it. Each memory shows whether an agent saved it or you wrote
it, and when a task last used it.

### Memory API

`GET /v1/agent/memories` returns the memories for every project, and
`?repo=owner/repository` those of one repository: `{repo, enabled, memories,
archived}`. `POST` with `{content, repo}` adds a memory you wrote (`repo` is
`null` for every project). `PATCH /v1/agent/memories/{id}` with `{content, repo,
expected_version}` rewrites or moves it, `DELETE` with `{expected_version}`
deletes it, and `POST /v1/agent/memories/{id}/restore` with
`{expected_version}` restores an archived one. `DELETE /v1/agent/memories`
clears one scope, archived memories included. `PUT /v1/agent/memories/settings`
with `{repo, enabled}` turns a scope on or off, and
`GET /v1/agent/memories/projects` lists the repositories you worked in or keep
memories for. Refusals carry a typed reason: `memory_changed` (409, a stale
version), `memory_scope_full` (409), `memory_invalid` (422, text with
credentials, over 240 characters, or limited to the current task) and
`memory_not_found` (404). No answer echoes memory text.

## Avatars

Without an uploaded profile photo, you are shown as an automatically generated
Pokémon companion. Your account is assigned Pikachu, Bulbasaur, Charmander,
Squirtle, Eevee, Gengar, or Jigglypuff, with a consistent expression and pose
across devices; that never changes. An uploaded profile photo takes its place
everywhere.

The account menu and **Profile** show the companion in its fixed form: an
account palette and no badges. `GET /v1/agent/profile` returns it as
`companion_data_url`, worked out from the account alone, so showing it on every
page reads no requests.

On the homepage, the companion's habitat (backdrop, halo, and platform colors)
and its orbiting work badges follow the work areas in your latest 50 requests,
such as APIs, UI, testing, or data storage, detected with English keywords.
The largest badge at the top right shows your most common area, the left badge
your second, and smaller badges along the bottom the rest. With no recognized
work area, it looks like the fixed form. All shapes are drawn
locally as a standalone SVG; account identifiers and request text are never
embedded, and no image service or model is used.

## Home suggestions

The homepage lists up to three suggestions for clearer requests, chosen by
counting English keywords in your latest 50 requests: change requests without
success checks or verification, failure reports without reproduction steps, and
reviews without a stated scope. They are fixed guidance, not an assessment of
your abilities.

Only your own lines count for avatars and suggestions: quoted text, code
blocks, assistant replies, forked copies, automatic goal turns, and deleted
chats are ignored. `GET /v1/agent/insights` returns `{focus_areas, suggestions,
avatar}` for the signed-in owner, derived on each read.

## Deployment and storage

Migration `0069_agent_memories` adds `agent_memories`, one row per memory.
`0070_memory_cutover` adds `agent_memory_settings`, one switch per user scope
(`repo` is `''`) or repository, and copies the earlier memory into the new
tables once: each scope's switch, and the preferences tasks were receiving.
Preferences the owner edited or consolidated become `manual` memories, the rest
`agent` memories, at most 30 per scope; task references are not copied. The
copy is idempotent.

`0072_drop_old_memory_tables` drops the earlier tables (`agent_user_memories`,
`agent_project_memories`, `agent_user_memory_edits`,
`agent_project_memory_edits`). Its downgrade recreates them empty, so a rollback
to code from before 0070 starts without the earlier memory. Every control-plane
process purges expired archives hourly.
