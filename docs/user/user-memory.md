# Avatars, user memory, and project memory

The homepage displays an automatically generated Pokémon companion for your
account, suggestions for improving your next request, and an expandable guide
to the avatar's colors and badges. **Setting → User memory** manages your saved
preferences, and **Setting → Project memory** manages memory for a selected
repository. Your account is assigned Pikachu, Bulbasaur, Charmander, Squirtle,
Eevee, Gengar, or Jigglypuff, with a consistent expression and pose across devices.
Each companion is assembled from local vector layers: its silhouette, distinctive
ears, tail, bulb or shell, and expressive facial details. Its habitat backdrop,
halo, platform colors, and orbiting work badges evolve with your recent work
areas, such as APIs, UI, testing, or data storage. The most-used work areas lead
the theme; the same recent-work profile produces the same image. It is an
assigned companion and does not attempt to infer your appearance or ability.
No image service, inference quota, or uploaded photo is required to generate the avatar.

Themes follow the work areas detected with English keywords. Requests without a
recognized work area keep the default theme.

The largest badge at the top right shows your most common work area. The left
badge shows your second area, and smaller badges along the bottom show the rest
of the ranked mix. The companion keeps its own colors as its habitat changes.
With no recognized work area, it uses a consistent account palette and decorative
sparkles. All shapes are generated locally as a standalone SVG; account
identifiers and request text are never embedded in the artwork.

Memory has two independently controlled scopes:

- **User memory** contains preferences that apply across projects. An automatic
  preference from a request without a repository belongs here. A preference
  from a project request belongs here only when it explicitly applies across
  projects, such as “I prefer concise replies across all projects.” Task
  references from requests without a repository also belong here. Saved edits
  and validated consolidation can explicitly select user scope.
- **Project memory** contains preferences and compact task references for one
  repository. Unqualified automatic preferences from repository requests, such
  as “Always run tests.”, stay in that project. Each project belongs to the
  account that created its requests; another account's work in the same
  repository does not contribute to it.

User memory and the account's work profile build from up to your 50 most recent
eligible requests. Each project's memory builds from up to its own 50 most
recent eligible requests, so activity in another project cannot displace those
requests. Settings shows separate user and project memory sections alongside
recent work areas and commonly used agent settings. User suggestions appear on
the homepage; project suggestions stay in the selected project's memory settings.
Suggestions address concrete patterns such as requests without
verification, success checks, or failure reproduction steps. These keyword-based
suggestions are guidance, rather than an assessment of your abilities.

Automatic preferences beginning with phrases such as “I prefer” or “Remember
that” can guide future tasks. Owner-edited and consolidated preferences can
apply without an “I prefer” prefix. Newly claimed tasks receive enabled user
preferences and enabled preferences from their matching project. Project
preferences take precedence when they conflict with user preferences. A current
task takes precedence over remembered context. Task titles remain visible as
source-linked history; they are not added to future task prompts. Editing a task
reference does not turn it into a preference. Instructions limited to the
current task, quoted text, fenced code, and recognized credential-like text are
excluded from automatic preferences.

You can examine saved entries' original chat, repository, and date. Saved
memory is bounded to 12 entries, 240 characters per entry, and 2,400 characters
total per scope. User memory has at most three entries that apply across
repositories. The API uses `user` and `repo` to identify these scopes; repository
scope is the default for project conventions and technical choices.

## Controls

- User and project memory each have their own **Refresh**, automatic memory
  setting, and clear control. Select a repository to inspect and manage its
  project memory.
- Turning off automatic user memory pauses updates to user memory and the
  avatar's usage theme, and stops adding user preferences to newly claimed
  tasks. Turning off automatic project memory pauses that project's updates
  and stops adding its preferences. Saved entries remain visible.
- **Edit** opens a saved entry for correction. **Save** persists the correction
  across automatic updates, while **Cancel** leaves it unchanged. Text must be
  nonempty, at most 240 characters, and contain no recognized credentials.
  Preference edits must apply to future tasks rather than a temporary request.
  The scope editor can move an entry between user memory and its source
  repository's project memory. Its source references remain intact.
- **Remove** asks for confirmation and excludes that entry from subsequent
  refreshes. The original chat remains available.
- **Consolidate** uses the model you choose from your account's available models,
  under your account's permissions and quota, to merge duplicates, shorten
  preferences, and decide their scope. It favors repository memory and reserves
  user memory for general communication and collaboration preferences. It drops
  temporary instructions and task references. Saving is revision-checked so a
  slow model cannot overwrite newer edits or a clear. An empty model result
  leaves saved memory unchanged. Consolidation is disabled while an editor is
  open or no model is available.
- Clearing user memory removes its saved preferences, task references,
  corrections, removals, the account's recent-work profile, and suggestions.
  Its avatar returns to the default habitat while keeping the same companion. Project
  memory keeps its own entries and setting.
- Clearing project memory removes that project's saved entries, corrections,
  removals, overlays, and suggestions. User memory and other projects keep their
  entries and settings.
- Each clear excludes earlier requests from rebuilding the cleared scope.
  Eligible new requests can build new memory while that scope is enabled. Your
  original chats remain available.

Deleting a source chat removes its derived memory during the next refresh of
each enabled scope, including its contribution to the avatar's usage theme when
user memory is enabled. Memory is a bounded view of recent activity, so a
preference can also leave its scope's view when its source is older than that
scope's 50-request window. Disable and clear take effect for subsequent task
claims; they cannot remove context already delivered to a running agent or
retained in an existing native session.

Corrections and consolidation remain tied to eligible source evidence; they do
not pin entries beyond their recent-request window or retain deleted chats. If
another tab or a refresh changes memory before you save, the page preserves your
draft and asks you to refresh, review the latest memory, and retry. If the entry
has disappeared, your draft remains available to copy or cancel; it cannot be
saved to a missing entry.

## Deployment and storage

Apply Alembic migrations through `0053_project_memory`, following
`0052_memory_overrides`, before starting the updated control plane.
Use an online Alembic upgrade when saved corrections or overrides exist. Offline
SQL supports empty edit and override data; it raises an error before converting
populated data and directs the operator to run the online upgrade.
`agent_user_memories` stores one user-memory record per account;
`agent_project_memories` stores project records keyed by account and exact
repository name. Each scope stores its own enabled setting and reset fences
that prevent forgotten history from reappearing. Existing repository-derived
memories move to their original project's scope; the migration preserves saved
edits, source-backed overrides, disabled settings, and historical clear fences.
Migrated single-entry corrections keep their original source job and scope;
later requests with the same raw text do not inherit an earlier correction.
Writes and refreshes serialize per account.

The `agent_user_memory_edits` and `agent_project_memory_edits` tables store
corrections and removal markers separately from automatic snapshots. Project
edits are keyed by account, repository, and memory identifier. Consolidated and
scope-edited entries use source-backed overlays in the memory records. Scope
transfers update their source and destination records under the account lock.
Overlays expire with eligible evidence and never pin deleted chats. Clear
removes the selected scope's corrections, removals, and overlays. Hidden copies
of transferred entries become contentless suppression markers, so their text
is forgotten without restoring the entry in its former scope.

The control plane refreshes bounded batches every minute and refreshes the
owner's applicable scopes when claiming a task or opening their memory section.
Enabled project memory continues updating when user memory is disabled. User
API responses derive the avatar from the saved account work profile. The
MCP-only listener serves no memory management routes.

The owner API exposes `GET`, `PATCH`, and `DELETE` at
`/v1/agent/user-memory` and `/v1/agent/project-memory?repo=owner/repository`.
`GET /v1/agent/project-memory/projects` lists the owner's repositories available
for project memory. The authenticated session selects the account; callers
cannot supply a different account identifier. Reads and mutations require
current Agent admission and return `Cache-Control: no-store`. `DELETE` clears
the selected scope and advances its reset fences. Memory entries retain their
original task and repository references. User-memory responses include the
account's generated avatar as a safe SVG data URL; project-memory responses
include the selected repository's work profile, entries, and suggestions.
Automatic refresh makes no additional model calls.

`PATCH` at either memory root accepts an `enabled` boolean or
`{revision, memories: [{id, content, scope}]}`. Revision-checked edits preserve
owned source references and obey all memory limits. Explicit scope transfers
move entries between user memory and their source project atomically.

`PATCH /v1/agent/user-memory/entries/{memory_id}` accepts `content` and a
nonnegative integer `expected_revision`. `DELETE` at the same path accepts
`expected_revision`. Both return the updated profile, reject stale revisions
with `409`, and return `404` when the owner has no such visible entry. Entry
kind and source information cannot be changed. Responses mark corrected entries
with `user_edited`. The project equivalents use
`/v1/agent/project-memory/entries/{memory_id}?repo=owner/repository` and the same
request bodies.

`POST /v1/agent/user-memory/consolidate` accepts `{revision, model}` and returns the
updated profile. The optional `model` must be available to the authenticated
account; unavailable choices return `400`. Omitting it preserves automatic model
selection for existing clients. Model output is checked for owned source
references, duplicate references, cross-repository merges, credentials,
temporary instructions, and all memory limits before saving. Only nonempty
results replace saved memory; empty results return the latest profile without
recording removals. Stale output returns `409` when a replacement is attempted.
`POST /v1/agent/project-memory/consolidate?repo=owner/repository` applies the same
model selection, validation, and empty-result behavior to the selected project's
current revision.

## Future improvements

Useful extensions include choosing avatar styles, adding new preferences,
pinning confirmed preferences beyond the recent-request window, and evaluating
suggestions against task outcomes. Model-assisted extraction could support more
languages and subtler preferences, with explicit source references and the
account's existing model permissions and quota.
