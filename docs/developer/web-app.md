# The owner web app

This page is for developers changing the owner-facing web app. It records the
rules each feature follows, the endpoints behind it and what only a real browser
can check. How to use the app is user documentation, which belongs to a
distribution; FreeInference's is the
[Cloud Agents guide](https://doc.freeinference.org/agents.html).

## Sidebar and conversation list

Grouping (by project or in one list), ordering (last updated or manual),
expanded projects, manual positions and added workspaces are stored per
signed-in user in the current browser. Conversation ids are stored for ordering
only once manual order is chosen, and switching back to last updated keeps the
saved positions. Pinned conversations have one manual order shared by both
groupings, appear once outside the project folders, and return to their usual
place when unpinned. Conversations without a repository are grouped in a
**Start from scratch** folder.

Search matches conversation titles and full repository names across history.
Its shortcut is Command+K on a Mac and Ctrl+K elsewhere; on a Mac, Control+K
stays the text field's own key. Each result shows the conversation's original
title and opens its latest turn. Restoring an archived conversation refreshes
its sidebar row at once without changing its activity time.

A completed turn shows an unread dot until it is opened, and a completion
already on screen is marked read. Read receipts are saved to the account:
open tabs synchronize on focus, on reconnection and every 30 seconds while
visible, fetching only changes after the first sync. Receipts a browser held
before cloud synchronization are imported the next time it opens the app;
offline reads are cached and uploaded when the connection returns. Pinning,
archiving and renaming do not reset read state.

### Creating a repository

**Create repository**, in the workspace and new-task repository pickers,
creates a repository in the signed-in user's personal GitHub account with a
README, so it has a default branch; organization repositories are not offered.
It needs the GitHub App's **Repository creation: write** permission, or
**Administration: Read and write** (see [Credentials](operations.md#2-credentials)).
Creating a repository does not give the App access to it, and repository-access
checks still apply. After a connection failure the request may have succeeded
on GitHub, so the dialog keeps what was entered and does not retry by itself.

### Getting started

An account with no conversations, archived ones included, sees a **Getting
started** row: **Connect GitHub**, **Grant a repository**, then **Run your first
task**. Connection and repository state come from `/config`, and the row is
hidden when the deployment has no GitHub App configured. The sidebar, the
new-task page and the conversation explanations share one first-run check,
`GET /v1/agent/conversations?include_archived=true&limit=2`, which refreshes on
page changes until two conversations exist. **Skip** hides the row per account
in the current browser, with an **Undo** notice; the settings page brings it
back.

### Conversations API

`GET /v1/agent/conversations` returns `conversations` and `next_cursor`. Each
row carries the latest `job`, the canonical `title`, `archived` and
`last_activity_at`.

| Parameter | Meaning |
| --- | --- |
| `q` | Case-insensitive literal substring of the title or full repository name; at most 200 characters. `%` and `_` match themselves |
| `repo` | Exact repository |
| `temporary` | `true` lists only conversations without a repository; not combined with `repo` |
| `include_archived` | Include archived conversations; defaults to false |
| `pinned` | `true` or `false` restricts to one pin partition; omitted, both |
| `limit` | Page size, 1–200; defaults to 50 |
| `cursor` | Opaque cursor from the previous response, scoped to the owner and the filters |

Rows are ordered by pin time, then meaningful activity, then thread id.
Conversation and message updates and job transitions count as activity; lease
heartbeats do not. A conversation can move ahead of an older cursor, so a
client refreshes from the first page when activity changes. The grouped view
requests unpinned rows for each expanded repository and one global pinned
list, and refreshes loaded rows in batches of up to 200. The backend finds each
matching conversation's most recently updated job through the thread/update
index, applies the page boundary, and only then reads the latest turn for that
page; keyset pagination does not make the query constant-time across all of an
owner's conversations. `/jobs` keeps its separate semantics.

Sources: [`AgentsSidebar.tsx`](../../apps/frontend/src/components/features/agents/AgentsSidebar.tsx),
[`sidebarPreferences.ts`](../../apps/frontend/src/components/features/agents/sidebarPreferences.ts),
[`catalog.py`](../../apps/backend/cloud_agent/api/catalog.py) and
[`conversation_queries.py`](../../apps/backend/cloud_agent/storage/repositories/conversation_queries.py).

## Conversation view

### The turn rail

The rail sits in the Chat column's right gutter. It appears when the
conversation has at least two visible turns, the column is at least 720px wide,
the reading area is at least 200px high and the device reports a fine pointer;
Trajectory has none. The current turn has the longest, darkest mark, and a
hovered or focused mark widens slightly. Mark lengths show interaction state
only, never message length, duration, tokens or progress. Marks keep their
spacing in long conversations; the rail scrolls on its own, with faded ends,
and scrolling it does not scroll the transcript.

A preview shows the question as sent, not the conversation title, and the
attempt's last visible reply once the turn is terminal, so a running answer
does not change beneath the pointer. It excludes reasoning, tool arguments,
tool output and runtime records, and caps the question at 50 characters and the
reply at 120. Copied Fork history uses durable messages for both transcript and
preview, even where those turns have no stored event log.

A jump places the turn's first question about 24px below the top of the reading
area, or a turn without a question at its first visible content. It changes
neither the URL, the selected attempt nor the workspace state. A later jump,
manual scrolling or leaving the conversation cancels an unfinished one; a target
that cannot be found leaves the transcript in place and announces that the turn
is unavailable. Hovering and jumping send no request and persist nothing. A
historical turn link limits the rail to the turns that view rendered.

The rail is a named navigation region of real buttons with one mark in the tab
order; arrow keys, Home and End reach the others. The current mark has
`aria-current`, the preview is the mark's description, and a requested jump is
announced once, while streaming output announces nothing.

### Reading position

The transcript either follows new output at its end or holds the reader's text
in place as output arrives. It opens following, and follows again after **Back
to bottom**, scrolling to the live end, or acceptance of a follow-up the reader
sent. Scrolling up, jumping through the rail or selecting an older attempt holds
the position. While held, new content adds a dot to **Back to bottom** and
updates its name, without pulling the reader back. Switching to Trajectory and
back keeps the position; reloading opens at the end.

### Tasks and subagents

The **Tasks and subagents** window shows the selected turn and attempt's latest
recorded task list and delegated agents. It appears only when one of them has
meaningful recorded data. A task creation result links the assigned id to the
task's name, so later status updates change the same row; **Status not
recorded** means the selected events hold no status for it.

A subagent card's reply, under **Returned to the main agent**, comes from where
each runtime records it:

- Claude Code's Agent or Task, and OpenCode's and Kilo's task, return the
  agent's answer as the call's own result; Claude records the agent's status
  beside it.
- Codex returns a finished agent's answer, or its error, as the message of the
  agent state its `wait` call reports.
- pi has no subagents.

A tool from an MCP server is never shown as a task list or a subagent, whatever
its name; Codex, pi, OpenCode and Kilo sessions recorded before each call stored
its MCP server can still show one as a subagent. A card says when a reply is
still pending, was not recorded or was stored in part, and a background launch
says it returned only a launch notice. Trajectory links a child step to its
delegating step through runtime ownership records: Claude Code records the
delegating call, and Codex the subagent's own thread. Sessions recorded before
those records keep a flat step list.

### Workspace pane keyboard

Workspace, Git, file-list and terminal tabs select a view with Left/Right, Home
and End while focus stays in the tab strip. Narrow Git panes use a changed-file
selector so the diff keeps the width, and the diff and file preview are
keyboard-scrollable regions. Opening a file in a narrow pane focuses its
preview; **Back** or Escape returns to the list and focuses that file.

### Checking it in a browser

Unit and DOM tests cover the navigation data, focus and an injected box model
(`useConversationScroll.dom.test.tsx`). Markdown reflow, the sticky composer,
resize timing and real pixel positions need a browser and a conversation with
several turns:

1. Click a middle mark. Its question lands about 24px below the reading area's
   top and the mark becomes current.
2. Scroll well into a long turn until its question is off-screen. That turn's
   mark stays current.
3. Scroll up while output streams. The text keeps its screen position within a
   few pixels and **Back to bottom** appears.
4. Use **Back to bottom**, or scroll to the end. Following resumes; scroll up
   again and it stays paused.
5. Try columns on both sides of 720px, reading areas on both sides of 200px, and
   touch input. Without the rail, the transcript still scrolls normally.
6. Expand a tool row, resize the composer, and open and close the workspace
   while reading history. The text under the reader stays in place.
7. With about 100 turns, check mark spacing, rail scrolling and keyboard access
   to every mark.
8. Fork a conversation; the copied turns have marks and previews.
9. Switch to Trajectory and back: no rail there, and Chat keeps its position.
10. Repeat the keyboard checks in both languages, both themes and with reduced
    motion.

The design and its sizing measurements are in the
[turn navigation design](../agents/specs/2026-09-11-conversation-turn-navigation-design.zh.md).
Sources: [`JobDetail.tsx`](../../apps/frontend/src/components/features/agents/JobDetail.tsx),
[`TurnNavigator.tsx`](../../apps/frontend/src/components/features/agents/TurnNavigator.tsx),
[`conversationTurnNavigation.ts`](../../apps/frontend/src/components/features/agents/conversationTurnNavigation.ts)
and [`useConversationScroll.ts`](../../apps/frontend/src/components/features/agents/useConversationScroll.ts).

## Tool approvals

The **Approvals** inbox lists pending MCP calls across the owner's
conversations, including ones a collapsed task list hides. Its badge counts the
loaded calls, with a trailing **+** when more exist. **Approve** releases that
one held request under its current authorization and attempt fence; **Deny**
refuses it. The inbox never changes a conversation's permission rules. The
queue refreshes when the owner's change stream reports a relevant change, with
polling as a fallback. An expired call leaves the queue even before the worker
records the expiry, and a decision that loses a race with expiry or another tab
cannot revive it.

**Always allow**, in a conversation's tools panel, records the approval and the
future permission in one server-side transaction, so the owner can leave or
refresh after submitting. The rule is saved once no call on that conversation's
connection is waiting for approval or dispatch, and covers only the named
capability in that conversation. Before saving, the server checks the owner,
connection and policy revisions and the capability fingerprint again: a
reconnection, a disabled connection, a permission change or a changed
capability definition discards the intent instead of restoring a revoked
permission. The call history shows whether the save is pending, applied or
discarded.

A held call waits up to ten minutes. Claude Code's per-server HTTP request
timeout and remote idle timeout are set to the same 26-minute window as its
overall tool timeout; raising only the overall timer would leave the network
timers able to abandon a request that Approvals still shows as pending. A
client that accepts SSE gets an immediate response and keepalive comments
during the wait, then the call's single JSON-RPC result or error. Disconnecting
cancels the wait and expires the approval; nothing keeps dispatching on behalf
of an abandoned request. An expired approval was never dispatched, and a changed
session authorization also stops a held call before dispatch. An error after
dispatch can leave the outcome unknown.

`GET /v1/agent/mcp/invocations?pending_only=true` reads the authenticated
owner's pending inbox; `job_id` or `thread_id` narrows it. History reads need
one of those scopes, so omitting both never exposes account-wide history.
`limit` defaults to 50 and accepts 1 to 100. A response carries `invocations`,
`has_more` and `next_before_id`; while `has_more` is true, pass `next_before_id`
as `before_id`. Ids run newest first and the cursor is exclusive, so new calls
do not shift older pages. Reads and decisions are scoped to the owner and omit
deleted conversations, whose audit history is kept. Reading never changes an
invocation or grants a permission. The relay side is in
[MCP servers](operations.md#authorization-and-routing).

## Memory, avatars and home suggestions

Memory is the lasting rules agents follow for an owner. Avatars and home
suggestions are derived from recent requests on each read; nothing about them is
stored, given to agents or affected by memory settings. The
[memory redesign](../agents/specs/2026-10-13-memory-redesign.zh-CN.md) explains
the split.

### Memory

Each memory is one rule in one of two scopes: user memory applies to every
project, and project memory to one repository, where it takes precedence.

- **Tools.** Every task can call `memory_save`, `memory_update` and
  `memory_delete` on the platform's MCP endpoint; the agent decides when, and
  writes each memory as one English sentence. No extra model call is made.
  Updating or deleting archives the old memory, owner-written ones included.
- **Injection.** When a task starts, its repository's active memories and the
  owner's user memories are added before the request with the ids the tools
  take, up to 12,000 characters: repository memories first, and within a scope
  the most recently written or edited first. Only the memories added get a new
  `last_used_at`; nothing is added when there are none.
- **Fence.** Every write checks, in its own transaction, that the task's
  attempt is still the running one, so a stopped or replaced task cannot change
  memory even with a request under way.
- **Scope.** An agent saves to its task's repository unless told a rule applies
  to every project; a chat without a repository saves user memories. An agent
  sees and changes only its task's repository memories and the owner's user
  memories.
- **Limits.** Each scope has its own switch; a scope that is off is neither
  injected nor written, and keeps its memories. A scope keeps 30 active memories
  of at most 240 characters. When it is full, the least recently used
  agent-saved memory is archived to make room; owner-written or edited memories
  never are, so a scope full of them refuses a new one. Archived memories can be
  restored and are deleted after 90 days, by an hourly purge in every
  control-plane process. Adding a memory identical to an active one in the same
  scope, ignoring case and final punctuation, returns the existing memory.

`GET /v1/agent/memories` returns every project's memories, and
`?repo=owner/repository` one repository's, as `{repo, enabled, memories,
archived}`. `POST` with `{content, repo}` adds an owner-written memory (`repo` is
`null` for every project). `PATCH /v1/agent/memories/{id}` with `{content, repo,
expected_version}` rewrites or moves it, `DELETE` with `{expected_version}`
deletes it, and `POST /v1/agent/memories/{id}/restore` with
`{expected_version}` restores an archived one. `DELETE /v1/agent/memories`
clears one scope, archived memories included. `PUT /v1/agent/memories/settings`
with `{repo, enabled}` turns a scope on or off, and
`GET /v1/agent/memories/projects` lists the repositories the owner worked in or
keeps memories for. Refusals carry a typed reason: `memory_changed` (409, a
stale version), `memory_scope_full` (409), `memory_invalid` (422: credentials,
over 240 characters, or limited to the current task) and `memory_not_found`
(404). No response echoes memory text. The tables and their migrations are in
the [upgrade notes](upgrade-notes.md).

### Avatars and home suggestions

Without an uploaded profile photo, an account is shown as a generated Pokémon
companion: one of seven, with an expression and pose fixed for the account. The
account menu and **Profile** show its fixed form, which
`GET /v1/agent/profile` returns as `companion_data_url`, derived from the account
alone so showing it on every page reads no requests. On the homepage, the
habitat colors and orbiting badges follow the work areas detected, with English
keywords, in the owner's latest 50 requests. Every shape is drawn locally as a
standalone SVG; account identifiers and request text are never embedded, and no
image service or model is used.

The homepage also lists up to three fixed suggestions, chosen by counting
English keywords in the same 50 requests: change requests without success checks
or verification, failure reports without reproduction steps, and reviews without
a stated scope. Only the owner's own lines count: quoted text, code blocks,
assistant replies, forked copies, automatic goal turns and deleted chats are
ignored. `GET /v1/agent/insights` returns `{focus_areas, suggestions, avatar}`,
derived on each read.

## Workspace files

The **Files** pane reads the retained sandbox's current repository and is
read-only. Paths use forward slashes and stay inside the repository: absolute
paths, parent traversal and `.git` paths are refused, and symlinks are shown
without being followed. A folder listing is bounded: entries past the limit,
and names that are not UTF-8, are left out, and the folder says so instead of
reading as complete. An entry left out for the limit can still be opened by
path. Returning to the workspace root cancels an unfinished path read, so a late
response cannot move the tree. A file the run deleted reports that rather than
a failed read.

Copy and download offer only complete contents within the preview size limit;
an oversized file shows the limit instead of a partial copy. Raster images get a
preview, and HTML and SVG stay escaped text. **Download workspace**, on each
turn's result in a conversation without a repository, saves the workspace as
one `.tar.gz` without `.git` directories and metadata files; other hidden files
are included and `.gitignore` does not filter it. A workspace over the server's
download limit is refused rather than saved in part. See
[Temporary workspaces](operations.md#temporary-workspaces).

## Smaller features

- **Notifications.** **My Settings → Notifications** enables browser
  notifications for agent questions and finished turns, each toggled
  separately and stored per account. While the app is open they cover every
  chat, and clicking one opens its turn; nothing is delivered while the browser
  is closed.
- **Plugins.** Chosen with **+ → Plugins** before a chat's first task. Claude
  Code and Codex accept `name@marketplace` with a GitHub repository or public
  HTTPS marketplace source; pi, OpenCode and Kilo accept npm package names,
  optionally pinned to an exact version such as `@example/plugin@1.2.3`. Up to
  eight per chat. The sandbox installs them before the first prompt, and a
  failed installation stops startup as a task error. Selections persist with
  the chat and are inherited by Fork.
- **Prompt rewriting.** See
  [Draft prompt rewriting](operations.md#draft-prompt-rewriting).
- **Review.** Starts an independent code-review subagent in a workspace tab,
  covering every change since the conversation's original base, current
  workspace changes included, and instructed not to edit files. It needs an
  available native Fork, so it cannot start from an archived chat or one whose
  context was lost. Selecting it again reopens the chat's existing review.
- **Watch PR and merge.** Starts a follow-up in the same chat that watches
  checks and reviews, fixes failures and feedback, and merges once the current
  head meets the required checks and reviews; repository rules decide what may
  merge without a person. It keeps unsent composer text and attachments, and
  starts only while no chat sharing the workspace has active work or waiting
  messages. The agent needs source-control tools that read checks and reviews,
  mark drafts ready and merge: the GitHub preset with a token holding Contents
  and Pull requests read and write. A connection to `/mcp/readonly` must be
  reconnected with the preset. Watching ends with the turn's timeout, and a turn
  that made fixes ends so the platform can publish them.
