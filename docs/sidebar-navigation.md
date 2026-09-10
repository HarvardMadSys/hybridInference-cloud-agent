# Sidebar navigation

The project heading identifies the repository containing the open conversation, or
selected for a new task. Its crimson highlight is independent of running-task
indicators. Opening a historical turn keeps the conversation selected and links
back to its latest turn.

The view menu has two independent choices:

- Group: **By project** (default) or **In one list**.
- Order: **Last updated** (default) or **Manual**.

Manual mode supports dragging projects or task rows, and **Alt+ArrowUp/ArrowDown**
while a row is focused. Reordering changes display order, never repository
ownership. Pinned and unpinned tasks remain separate. Manual order, grouping,
sorting, and expanded projects are remembered for each signed-in user in the
current browser; switching to Last updated retains the manual order.
Conversation IDs are persisted only after manual ordering is enabled. The former
shared project preference key is removed without assigning it to any account.

Use the search icon or **Command+K / Ctrl+K** to search conversation titles and full
repository names across all history. Search can be restricted to the current
project and can include archived conversations. Arrow keys navigate results,
Enter opens a result, and Escape closes search. “Show more” loads further results.
Search results use the original conversation title and open its latest turn.
Restoring an archived conversation invalidates its sidebar state immediately,
including conversations outside the first page, without changing its activity time.

## API

`GET /v1/agent/conversations` accepts `q`, `repo`, `include_archived`, `limit`, and
an opaque `cursor`. It returns `conversations` and `next_cursor`. Each row includes
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
repositories without an additional global conversation list, and refreshes batch
already loaded rows in requests of up to 200 rather than repeating 50-row queries.
