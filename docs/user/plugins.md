# Plugins

To install plugins for a new chat, select **+ → Plugins** before sending the
first task. Claude Code and Codex accept `name@marketplace` with a GitHub
repository or public HTTPS marketplace source. pi, OpenCode, and Kilo accept
npm package names, optionally pinned to an exact version such as
`@example/plugin@1.2.3`. Add up to eight plugins. The sandbox installs them
before the first prompt; an installation failure stops startup and appears
as a task error. Selections persist with the chat and are inherited by Fork.
