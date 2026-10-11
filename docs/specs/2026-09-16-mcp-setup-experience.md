# MCP setup experience

## Goal

Make MCP servers easy to connect: search the official MCP directory or paste its
`mcpServers` JSON, then see whether it connected and which tools it
offers. The previous UI separated templates, import and manual configuration,
exposed protocol and execution details up front, and required a separate Test
action after adding a server or returning from OAuth.

## Interaction

- Integrations lists MCP servers with a connection state and tool count. Protocol,
  runtime and credential metadata are available under Server details.
- Add server opens built-in presets for GitHub, Slack, Google Drive, Linear,
  Notion and Context7, available independently of the directory sync. Each preset
  supplies the provider's remote endpoint and setup guidance. GitHub defaults to
  a personal access token on its read-only endpoint and also offers OAuth.
- MCP directory opens Recommended: a small editorial shortlist of GitHub, Context7,
  Playwright, Linear, Notion, Figma, Supabase and Sentry for common coding workflows.
  All servers opens the full synchronized directory; typing in search always searches
  the full directory, including when starting from Recommended. Cards show the
  publisher identifier as well as the display name.
  Select an endpoint or package, review its generated JSON, then add it. Remote
  endpoints can use automatic authorization discovery or a Bearer access token.
  Built-in presets remain available as a fallback when
  the directory has not synced or cannot be loaded.
- Add with JSON opens a `mcp.json` editor. URL and command examples explain the
  accepted shape. Validation runs after typing stops and shows the servers that
  will be added, missing credential values and any rejected entries.
- Missing credentials are entered separately for each server, even when several
  servers use the same header or environment-variable name. Saving preserves the
  authentication scheme declared in the configuration.
- JSON, preset and directory tabs retain the JSON draft when switching. Saving adds the
  displayed servers to existing connections; this is not a global config-file
  replacement or an editor for existing connections.
- URL servers use the remote endpoint. Command servers require a choice of the
  user's connected MCP connector or a cloud worker; local files do not become
  available in the cloud merely by pasting a config.
- Saving starts connection discovery automatically. Returning from successful
  OAuth also discovers tools, after checking that a credential was stored. A
  single newly added URL that requires OAuth starts sign-in. Existing connections
  still offer Sign in explicitly. Failed discovery remains an error and offers retry.
- Servers without automatic OAuth client registration ask for a client ID and,
  when required, a masked client secret. The form shows the deployment's exact
  callback URI to register with the provider, including its public API prefix.
  Slack needs a registered internal or Marketplace app. Google Drive needs
  Workspace Developer Preview access, the Drive and Drive MCP APIs enabled in a
  Google Cloud project, and a registered OAuth client. The setup guides explain
  these provider requirements before connection creation.
  Token exchange uses the authentication method advertised by the provider;
  Slack uses `client_secret_post` and its published origin as the OAuth resource
  while MCP requests use `/mcp`. Google authorization requests offline access
  and consent so persistent connections can obtain refresh tokens.
- Connected servers are available automatically in new and existing conversations.
  Every turn resolves the current account connections before configuring the agent;
  opening Tools is not required. All five supported runtimes use the same resolution.
- Integrations has an account-wide enable switch. Turning a server off preserves
  its credentials and conversation choices, immediately blocks subsequent calls,
  and expires pending approvals. Connection diagnostics cannot turn it back on.
- Tools shows server status and an immediate per-conversation switch. Tools and
  permissions is collapsed by default; users can allow, confirm or deny individual
  tools, resources and prompts. Default availability requires confirmation before
  every call, including tools a server adds later. Existing explicit rules survive.
- Each server's settings save independently, preserving other servers' policy
  revisions and approvals. Disabling or removing a selection stores an opt-out so
  automatic discovery cannot recreate it. Reset restores ask-before-call behavior.
- Newly enabled tools become available on the next message; restrictions apply on
  subsequent calls immediately. Native agent processes already restart/resume when
  their MCP configuration changes. A disconnected or expired server stays actionable
  in Tools and does not block an unrelated follow-up or another healthy server.

The importer accepts JSON and TOML configurations. It recognizes
`${env:NAME}` and `${input:NAME}` references in environment/header values, including
Bearer headers, and asks for missing values. This is not full emulation of every
client-specific config field or interpolation rule.

## References

- [GitHub MCP server](https://github.com/github/github-mcp-server): hosted endpoint
  and personal access tokens.
- [Slack MCP](https://docs.slack.dev/ai/slack-mcp-server/): endpoint, app identity
  and OAuth client requirements.
- [Google Drive MCP setup](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server):
  preview access, APIs and OAuth client setup.
- [Linear MCP](https://linear.app/docs/mcp): remote endpoint and OAuth.
- [Notion client guide](https://developers.notion.com/guides/mcp/build-mcp-client):
  remote endpoint and OAuth.
- [Context7 client configurations](https://context7.com/docs/resources/all-clients):
  OAuth endpoint.

## Validation

Component tests cover JSON validation, stale responses, missing credentials,
draft retention, save failures, preset authentication, callback discovery,
connection failures and conversation permissions. Importer tests cover the new
reference syntax and preserving the Bearer scheme.
Provider setup tests cover presets with a synced directory, masked client-secret
submission, callback registration guidance, confidential token exchange and
Google offline authorization. These checks use isolated server responses;
completing real account sign-in requires the user's provider credentials.

Local browser checks exercise the real components with an isolated sample API:
preset selection, JSON add/discovery, invalid input, tab switching, Escape,
mobile layout, dark mode and masked token fields. These checks do not authenticate
real external provider accounts. Provider sign-in still needs a deployed callback
URL and an authorized test account for end-to-end verification.

## Official directory synchronization

The control-plane background task fetches all latest Registry pages hourly. It
keeps active records, follows opaque pagination cursors, and atomically replaces
a durable Postgres snapshot only after the complete fetch succeeds. A failed or
incomplete fetch retains the previous snapshot and retries after five minutes.
The API searches the in-memory snapshot; it never calls the Registry on a user
request. Fresh database snapshots are reused after restart. The relay listener
does not run this task, and application readiness never waits for a sync.

This is a directory, not a popularity ranking or a guarantee that a listed
service is online. The Registry API has no download counts or popularity sort.
The editorial shortlist matches exact publisher identifiers, includes only active
entries with supported configurations, and stays small instead of padding the
home view with arbitrary listings. The same entries appear first in All servers.
Ranking is applied when reading cached snapshots so an update takes effect without
waiting for the next sync. Metadata updates remain independent of this shortlist.
The feed can update descriptions, endpoints and pinned package versions for new
connections. It never modifies existing connections or credentials.

Directory cards display publisher-provided HTTPS icons. Common services without
Registry artwork use their own official icon URLs, with an initial as the fallback
if an image fails to load. The sync timestamp stays in the API for cache handling
and is not displayed in the setup dialog.

Browser authorization reuses the public API prefix from the deployment's existing
`AGENT_SESSION_REDIRECT_URI` unless `AGENT_MCP_PUBLIC_BASE_URL` explicitly overrides
it. Registration, authorization and token exchange share the same MCP callback;
request headers never select its host. This avoids requiring another address
setting for deployments that already support account sign-in.

Generated configurations cover public HTTPS streamable HTTP/SSE endpoints,
single credential headers, and simple pinned npm/PyPI stdio packages. Multiple
endpoints remain explicit choices. Recipes needing unsupported header combinations,
URL interpolation, runtime/package arguments or other package formats remain
listed with a documentation path to manual JSON; required fields are never
silently omitted. Importing still uses the existing target validation, encrypted
credential storage, execution-location choice and per-conversation permissions.

Migration 0013 adds a standalone cache table. Migration 0014 adds an account
`enabled` preference with a default of true, preserving existing connections and
credentials. Both are additive and leave previous API projections readable during
rollout. Automatic binding inserts are scoped to the thread owner and serialized
with conversation edits; no background fan-out or startup backfill is needed.
No feature flag or external API key is required.

Validation includes real full-feed synchronization, round-tripping generated
configurations through the importer, cached restart, interrupted pagination,
deleted records, search races, pagination, endpoint selection, credentials,
fallback/retry and automatic sign-in after adding a URL. Provider-account OAuth
and real package execution remain outside the isolated UI smoke test.

Source: [Official Registry aggregator API](https://modelcontextprotocol.io/registry/registry-aggregators).
