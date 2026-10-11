# Avatars and personal memory

**Setting → Avatar & memory** displays an automatically generated illustrated
portrait for your account. Its face, hair, and skin stay recognizable across
devices. Its backdrop, clothing colors, and illustrated work motifs evolve with
your recent work areas, such as APIs, UI, testing, or data storage. The most-used
work areas lead the theme; the same recent-work profile produces the same image.
It is an assigned avatar and does not attempt to infer your appearance. No image
service, inference quota, or uploaded photo is required.

Themes follow the work areas shown on the page, detected with English keywords.
Requests without a recognized work area keep the default theme.

Memory builds automatically from up to your 50 most recent requests. The page
shows recent work areas, projects, commonly used agent settings, saved explicit
preferences, compact task references, and suggestions for future requests.
Suggestions address concrete patterns such as requests without verification,
success checks, or failure reproduction steps. These keyword-based suggestions
are guidance, rather than an assessment of your abilities.

Preferences beginning with phrases such as “I prefer” or “Remember that” can
guide future tasks in their original project. A current task takes precedence
over remembered context. Task titles remain visible as source-linked history;
they are not added to future task prompts. Quoted text, fenced code, and
recognized credential-like text are excluded from saved preferences.

## Controls

- **Refresh** rebuilds memory and the avatar's usage theme from currently eligible
  requests.
- Turning off **Automatically build memory** pauses automatic updates and
  stops adding preferences to newly claimed tasks. Saved memory and the current
  avatar theme remain visible.
- **Clear memory** removes the recent-work profile, saved memory, and
  suggestions. Confirming this action excludes all earlier requests from future
  memory creation. The avatar returns to its default theme while keeping the
  same face. New requests can build new memory and update the avatar theme while
  memory is enabled. Your original chats remain available.

Deleting a source chat removes its derived memory during the next refresh while
memory is enabled, including its contribution to the avatar's usage theme.
Memory is a bounded view of recent activity, so a preference can also leave the
view when its source is older than the 50-request window. Disable and clear take
effect for subsequent task claims; they cannot remove context already delivered
to a running agent or retained in an existing native session.

## Deployment and storage

Apply Alembic migration `0047_user_memory` before starting the updated control
plane. The new `agent_user_memories` table stores one derived record per account,
including its enabled setting and a reset fence that prevents forgotten history
from reappearing. Writes and refreshes serialize per account. The control plane
refreshes bounded batches every minute, and refreshes the owner's memory when
claiming a task or opening the Setting page. Each owner API response derives the
avatar from that saved work profile, so usage changes appear on the next page
load or refresh. The MCP-only listener serves no memory management routes.

The owner API exposes `GET`, `PATCH`, and `DELETE` at
`/v1/agent/user-memory`. The authenticated session selects the account; callers
cannot supply a different account identifier. Reads and mutations require
current Agent admission and return `Cache-Control: no-store`. `PATCH` accepts
only an `enabled` boolean; `DELETE` clears derived context and advances the reset
fence. Each response includes the account's generated avatar as a safe SVG data
URL. No additional model calls are made.

## Future improvements

Useful extensions include editing or removing individual memories, choosing
avatar styles, pinning confirmed preferences beyond the recent-request window,
and evaluating suggestions against task outcomes. A model-assisted extraction
option could support more languages and subtler preferences, with explicit
source references and the account's existing model permissions and quota.
