# Sidebar navigation

Use the sidebar to open conversations, organize projects and search history.
The project heading highlights the repository for the open conversation or the
repository selected for a new task. That highlight remains while another loaded
conversation in the same project opens. A historical turn keeps its conversation
selected and links back to the latest turn.

## Projects and pinned conversations

**Pinned** appears below **New task** and above **Projects** when there are pins.
It includes conversations from collapsed projects and has its own **Show more**
control. Pinned conversations appear once, outside project folders and the flat
list. Unpinning returns them to their ordinary location. Search shows a unified
result list regardless of pins.

Conversations started without a repository are grouped in a **Start from scratch**
folder beside the repository projects. Its new-task button starts another
conversation without a repository.

Use the folder-plus button beside the search and view controls to add a
workspace. Select a repository from the same authorized list as the task
composer. The project appears immediately, even with no conversations. The
picker links to **Integrations** when repository access needs configuration.
Added projects are saved per account in this browser, independently of ordering.

To create a repository before starting a conversation, choose **Create
repository** in the workspace picker. The same action is available in the
new-task repository picker, including when GitHub is connected but no
repositories have been granted yet. Enter a name, an optional description, and
choose **Private** or **Public**; Private is the default. Creation uses your
connected personal GitHub account and adds a README so the repository has a
default branch. Organization repository creation is not offered.

If the GitHub App already has access, the new repository is selected and can be
added to Projects without sending a message. Otherwise the dialog confirms
creation and offers **Configure repository access**, which opens Integrations
in a new tab. Choose **Configure repositories**, grant the App access on GitHub,
return to the dialog, and use **Refresh** to select the repository.
Refreshing access does not create another repository. A creation error leaves
the entered values available; after a connection failure, check GitHub before
trying creation again because the request may have succeeded there.

Repository creation requires the GitHub App's **Repository creation: write**
permission (or **Administration: Read and write**). Existing task execution and
repository-access checks still apply; creating a repository does not itself
grant the agent access. See the
[GitHub App setup](../developer/operations.md) for operator configuration.

## Grouping and ordering

The view menu controls two independent preferences:

| Preference | Options | Default |
| --- | --- | --- |
| Group | **By project**, **In one list** | By project |
| Order | **Last updated**, **Manual** | Last updated |

In Manual mode, drag a project name or conversation row. Drop above or below a
row's midpoint to insert before or after it; an insertion line previews the
position. With a row focused, **Alt+ArrowUp** or **Alt+ArrowDown** also reorders
it. Reordering changes display order, while repository ownership stays the same.
Pinned conversations have one manual order shared by both grouping modes.

Grouping, ordering, expanded projects and manual positions are remembered per
signed-in user in the current browser. Choosing Last updated retains saved
manual positions. Conversation IDs are stored for ordering only after Manual is
enabled. The former shared project-preference key is removed without attributing
it to an account.

## Search

Use the search icon, or **Command+K** on a Mac and **Ctrl+K** elsewhere, while
the sidebar is expanded. On a Mac, Control+K stays the text field's own key.
Search matches conversation titles and full repository names across history.
The filter buttons restrict results to the current project or include archived
conversations. The inline close button clears search and its filters.

Arrow keys select results, Enter opens one, and Escape closes search. **Show
more** loads additional results. Each result uses the conversation's original
title and opens its latest turn. Restoring an archived conversation refreshes
its sidebar state immediately, including rows beyond the first page, without
changing its activity time.

## Running and unread indicators

Queued and running conversations show a spinner. A completed turn shows a blue
unread dot until it is opened. A completion already being viewed is marked read
automatically. Read receipts are stored per signed-in user in this browser.

New turns and newly completed retries can produce another unread dot. Pinning,
archiving and title changes do not reset read state. Read success/failure details
in the conversation view.

## Getting started

Accounts with no conversations, including archived conversations, see a
**Getting started** row above **Settings**. It shows progress through three
steps and only the next action: **Connect GitHub**, **Grant a repository**, then
**Run your first task**. Connection and repository state come from `/config`;
creating the first conversation removes the row. GitHub actions preserve a draft
in the new-task composer when it is open. The row is hidden when the deployment
has no available GitHub App configuration.

**Skip** hides the row for that account in this browser. An **Undo** notice
appears for about six seconds and remains while hovered or focused. Keyboard
Skip moves focus to Undo. After the notice closes, **Settings → Getting started →
Show again** restores the row for accounts that still qualify.

The shared first-run provider checks
`GET /v1/agent/conversations?include_archived=true&limit=2`. The sidebar,
new-task page and conversation explanations share this result. It refreshes on
page changes until two conversations exist, then stops using that check.

## API

`GET /v1/agent/conversations` returns `conversations` and `next_cursor`. Each
row includes the latest `job`, canonical `title`, `archived` and
`last_activity_at`.

| Parameter | Meaning |
| --- | --- |
| `q` | Case-insensitive literal substring of title or full repository name; maximum 200 characters |
| `repo` | Exact repository filter |
| `temporary` | `true` lists only conversations without a repository; cannot be combined with `repo` |
| `include_archived` | Include archived conversations; defaults to false |
| `pinned` | True or false restricts the pin partition; omit to include both |
| `limit` | Page size, 1–200; defaults to 50 |
| `cursor` | Opaque continuation cursor from the previous response |

`%` and `_` are literal search characters. Authentication supplies the owner;
cursors are scoped to that owner and the filters. Results are ordered by pin
time, meaningful activity and thread ID. Conversation/message updates and job
transitions count as activity; lease heartbeats do not. Refresh from the first
page when activity changes, because a conversation can move ahead of an older
cursor. The existing `/jobs` endpoint retains its separate semantics.

Grouped view requests unpinned rows for expanded repositories and a separate
global pinned list. Refreshes batch loaded rows in requests of up to 200. The
backend probes the most recently updated job for each matching conversation
using the thread/update index, applies the page boundary, then reads the latest
turn only for the resulting page. Keyset pagination does not make this a
constant-time query across all owned conversations.

See [`AgentsSidebar.tsx`](../../apps/frontend/src/components/features/agents/AgentsSidebar.tsx),
[`sidebarPreferences.ts`](../../apps/frontend/src/components/features/agents/sidebarPreferences.ts),
[`catalog.py`](../../apps/backend/cloud_agent/api/catalog.py) and
[`conversation_queries.py`](../../apps/backend/cloud_agent/storage/repositories/conversation_queries.py).
