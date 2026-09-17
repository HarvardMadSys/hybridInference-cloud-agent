# Personal skills in Integrations

Skills are an owner's reusable Markdown instructions. Integrations places the
library below MCP servers, using the existing card, button, and dark-mode styles.
Owners can create, edit, inspect, copy, remove, and choose defaults. Import accepts
a GitHub repository/folder/SKILL.md URL or an uploaded SKILL.md with YAML `name`
and `description` frontmatter. The body must contain Markdown instructions.

## Selection and execution

- A default switch affects new conversations. The task composer starts with the
  owner's defaults; its Skills picker can change or clear the selection. Unchanged
  defaults are resolved by the server and excluded from the submission fingerprint.
  Loading or failing to load the library does not block task creation: the
  composer shows that account defaults will apply and omits `skill_ids` so the
  server resolves them. A failed load offers a retry.
- `POST /v1/agent/jobs` accepts `skill_ids`: omitted/null means account defaults;
  an empty array explicitly selects none.
- Follow-ups accept the same field: omitted/null inherits the latest turn's exact
  saved content, while an explicit array resolves current library revisions.
  The picker sends changes only when the user changes the selected ID set.
- Each accepted turn saves instruction snapshots in `agent_jobs.skills`. Editing
  or removing a library entry does not change prior turns or inherited versions.
  Fork copies each turn's snapshots, and a retried claim uses the same content.
  An idempotent submission replay returns its original accepted selection.
- Worker claim prefixes the saved instructions to the ordinary task prompt.
  The selected descriptions tell the agent when to apply each skill. The prompt
  identifies them as user-level guidance and replaces earlier library selections;
  an empty selection explicitly clears previously selected skills. Conversations
  that have never selected skills keep SQL NULL and their original prompt, even
  when the client explicitly sends `skill_ids: []`. This is model guidance, not an
  enforcement mechanism or a new tool permission.
- The owner's task text and transcript remain unchanged. Job responses include
  skill names, descriptions, IDs, and revisions; full bodies are available through
  the owner's skill detail route. Existing turns with SQL NULL retain their
  original prompt behavior.

The existing retained-session prompt transport delivers this context to all five
harnesses. This feature adds no host protocol, network destination, credential,
or shared workspace file. This first version imports **instructions only**:
referenced scripts, assets, and other files are not installed. This limitation
is shown before confirmation. Repository-native skills remain separate.

## Library API and integrity

Owner-authenticated routes live only on the main API listener:

| Route | Action |
| --- | --- |
| `GET /v1/agent/skills` | List owned summaries |
| `POST /v1/agent/skills` | Create custom or uploaded instructions |
| `GET /v1/agent/skills/{id}` | Read owned content |
| `PATCH /v1/agent/skills/{id}` | Edit at the supplied `revision` |
| `DELETE /v1/agent/skills/{id}?revision=N` | Soft-delete at that revision |
| `POST /v1/agent/skills/import/preview` | Validate a `url` or file `content`, without writes |
| `POST /v1/agent/skills/import` | Confirm GitHub URL, reviewed digest, name, and default |

Names are unique within one owner's live library, ignoring case. The write
`revision` fences all mutations; `content_revision` changes only with name,
description, or instructions and is the version recorded in turn snapshots. Writes serialize
per owner and fence edits/deletions by revision; stale writes return 409. Limits
are 100 library entries, 64 KiB of UTF-8 instruction text per skill, 16 selected
skills, and 128 KiB of combined selected instructions. Default changes enforce
the same selection limits atomically. Summary reads exclude instruction bodies;
selection reads fetch only requested IDs or defaults. The library includes UTF-8
byte sizes and `/config` advertises selection limits so pickers can reject an
oversized combination before submission.

GitHub reads are constructed against the fixed GitHub Contents API host, with
redirects disabled and bounded responses. Connected repositories use the existing repository entitlement check and a
repository-scoped read token held by the control plane, without an anonymous
probe. Their requests reuse the configured GitHub API and pooled client. Other
public repositories remain importable through an anonymous read. Confirmation refetches
and compares the SHA-256 digest of the exact previewed file; changed content
requires a new preview. The GitHub blob SHA is retained as source metadata.
Imported GitHub content is customized by creating a local copy. For branch names
containing `/`, use a commit permalink or encode the branch segment in the URL.

## Rollout

1. Apply additive Alembic revision `0013_personal_skills` before starting the new
   control plane. It adds `agent_skills` and nullable `agent_jobs.skills`; previous
   rows need no content backfill.
2. Upgrade all control-plane instances before enabling the new web UI. Older API
   instances do not resolve or deliver skill selections. No host rebuild is
   needed for the existing retained-session protocol.
3. Deploy the web build. No additional secret or environment variable is needed.

An application rollback may leave the additive schema in place. Drain jobs using
skills before rolling back to a control plane that cannot deliver them. Database
downgrade removes library and snapshot data and must run with the API stopped.

## Verification

Coverage includes parser/import boundaries, preview versus confirmation, private
repository entitlements, owner scoping, concurrent edits and default limits,
immutable follow-ups and Fork, idempotent submissions, worker delivery, and UI
creation/import/removal/default/selection/error flows. Real Postgres tests apply
the migration and exercise the storage transactions. Browser checks use the
actual Next.js components and skill API against an isolated local test database.
