# MCP setup experience

## Goal

Make MCP servers easy to connect: choose a known service or paste its
`mcpServers` JSON, then see whether it connected and which tools it
offers. The previous UI separated templates, import and manual configuration,
exposed protocol and execution details up front, and required a separate Test
action after adding a server or returning from OAuth.

## Interaction

- Integrations lists MCP servers with a connection state and tool count. Protocol,
  runtime and credential metadata are available under Server details.
- Add server opens Popular servers: Linear, Notion, Context7 and GitHub. Connect
  creates the connection and starts sign-in. GitHub also accepts a personal access
  token; OAuth can request a client ID when the provider requires one.
- Add with JSON opens a `mcp.json` editor. URL and command examples explain the
  accepted shape. Validation runs after typing stops and shows the servers that
  will be added, missing credential values and any rejected entries.
- Missing credentials are entered separately for each server, even when several
  servers use the same header or environment-variable name. Saving preserves the
  authentication scheme declared in the configuration.
- JSON and preset tabs retain the JSON draft when switching. Saving adds the
  displayed servers to existing connections; this is not a global config-file
  replacement or an editor for existing connections.
- URL servers use the remote endpoint. Command servers require a choice of the
  user's connected MCP connector or a cloud worker; local files do not become
  available in the cloud merely by pasting a config.
- Saving starts connection discovery automatically. Returning from successful
  OAuth also discovers tools, after checking that a credential was stored. A
  pasted URL that requires OAuth offers Sign in. Failed discovery remains an
  error and offers retry.
- Conversations still select their own servers and permissions. Ask before each
  call and Disable all apply to the server's currently discovered capabilities;
  Save selection commits that choice. Adding a server never silently grants its
  tools permission to run.

The importer accepts JSON and TOML configurations. It recognizes
`${env:NAME}` and `${input:NAME}` references in environment/header values, including
Bearer headers, and asks for missing values. This is not full emulation of every
client-specific config field or interpolation rule.

## References

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

Local browser checks exercise the real components with an isolated sample API:
preset selection, JSON add/discovery, invalid input, tab switching, Escape,
mobile layout, dark mode and masked token fields. These checks do not authenticate
real external provider accounts. Provider sign-in still needs a deployed callback
URL and an authorized test account for end-to-end verification.
