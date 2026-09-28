# Sidebar navigation

The project heading identifies the repository containing the open conversation, or
selected for a new task. Its crimson highlight is independent of running-task
indicators, with no extra “Current” badge. Switching between loaded conversations
in the same project preserves the highlight while details load. Opening a historical turn keeps the conversation selected and links
back to its latest turn.

Pinned conversations appear in a dedicated **Pinned** section below **New task**
and above **Projects**, including pins from collapsed projects. They appear once,
outside both project folders and the ordinary flat list. Unpinning returns a
conversation to its project or flat list. The section is hidden when empty and
has its own **Show more** control. Search remains one unified result list.

The folder-plus button beside the search and view controls adds a workspace.
Choose a repository from the same authorized list used by the task composer;
it appears immediately even without conversations. Added workspaces are saved
per account in this browser independently of manual ordering. The picker links
to Integrations when repository access needs configuring.

The view menu has two independent choices:

- Group: **By project** (default) or **In one list**.
- Order: **Last updated** (default) or **Manual**.

Manual mode supports dragging the project name or task row directly, without
visible drag handles. Drop above or below a row’s midpoint to insert before or
after it; an insertion line previews the result.
It also supports **Alt+ArrowUp/ArrowDown**
while a row is focused. Reordering changes display order, never repository
ownership. Pinned tasks have their own manual order, shared by both grouping modes. Manual order, grouping,
sorting, and expanded projects are remembered for each signed-in user in the
current browser; switching to Last updated retains the manual order.
Conversation IDs are persisted only after manual ordering is enabled. The former
shared project preference key is removed without assigning it to any account.

Use the search icon or **Command+K / Ctrl+K** to search conversation titles and full
repository names across all history. Search can be restricted to the current
project and can include archived conversations using the two compact filter
buttons below the input. The inline close button resets search and filters. Arrow keys navigate results,
Enter opens a result, and Escape closes search. “Show more” loads further results.
Search results use the original conversation title and open its latest turn.
Restoring an archived conversation invalidates its sidebar state immediately,
including conversations outside the first page, without changing its activity time.

While a task is queued or running, its row shows a spinner. A completed turn
shows a blue unread dot until the conversation is opened; a completed turn
already being viewed is marked read automatically. Read receipts are remembered
per signed-in user in the current browser. New turns or retried completions can
show a new dot, while pinning and archiving do not reset read status. Detailed
success and failure states remain in the conversation view.

Someone who has not started a conversation yet — archived ones count — sees a
**Getting started** row above **Settings**. It shows only the next step
(**Connect GitHub**, **Grant a repository**, then **Run your first task**), how
many of the three are done, and **Skip**. Each step is ticked off by what the
owner actually did: the connection and repositories `/config` reports, then the
first conversation, when the row disappears. The GitHub steps leave through the
new-task composer when it is on screen, so a task already written there makes
the trip too. The row is left out while the deployment's GitHub App is
unavailable or not configured, since no step can be taken.

**Skip** hides the row for that account in this browser. For about six seconds
its place says where it went, with **Undo**; the note stays while it is pointed
at or focused, and a Skip pressed from the keyboard moves focus to Undo. After
that, **Settings** has a **Getting started** entry with **Show again**. The
entry is only there for someone who is still new and skipped it; nobody else
has anything to bring back.

The app asks `GET /v1/agent/conversations?include_archived=true&limit=2` once
when it loads, and the sidebar, the new-task page and the conversation page all
read that one answer: the new-task page's first-visit line and the first turn's
explanations come from it too. It is asked again on each page change only until
the owner has two conversations; after that nothing depends on it.

## API

`GET /v1/agent/conversations` accepts `q`, `repo`, `include_archived`, `pinned`,
`limit`, and an opaque `cursor`. `pinned=true` or `pinned=false` restricts the pin
partition; omitting it includes both. It returns `conversations` and `next_cursor`. Each row includes
the latest `job`, canonical `title`, `archived`, and `last_activity_at`.

Search is a case-insensitive literal substring match, including `%` and `_`.
The owner is derived from authentication; cursors are scoped to that owner and
the search filters. Results are ordered by pin time, meaningful activity, and
thread ID. Activity includes conversation updates and job transitions, but lease
heartbeats do not move tasks. The existing `/jobs` endpoint is unchanged.

The query aggregates matching job activity by conversation before applying the
page boundary, then reads the latest job for only that page using the existing
thread/turn index. It still aggregates matching history; keyset pagination does
not make this an index-only, constant-time query. Grouped view fetches expanded
repositories with `pinned=false` plus a separate global `pinned=true` list. Refreshes batch
already loaded rows in requests of up to 200 rather than repeating 50-row queries.
