# Review and Watch PR

## Review a finished chat

After a chat with a draft PR finishes, select **Review** beside **Open draft
PR** to start an independent code-review subagent in a workspace tab. The
review covers every change since the conversation's original base, including
current workspace changes. The reviewer is instructed to report findings
without editing files. Selecting Review again reopens that chat's existing
review. Review needs an available native Fork; it cannot start from an archived
chat or a chat whose context has been lost.

## Watch a pull request until it merges

Select **Watch PR and merge** beside a published PR to start a follow-up in the
same chat. The follow-up monitors tests and reviews, addresses failures and
feedback, and merges once the current PR head satisfies its required checks and
review requirements. Starting it preserves unsent composer text and
attachments. Before watching can begin, every chat that shares the workspace
must have no active work or waiting messages.

The agent needs source-control tools that can read checks and reviews, mark
drafts ready, and merge. The supplied GitHub MCP template supports write tools.
Use a token with Contents and Pull requests read and write permissions for the
repositories the agent should update. Existing connections using `/mcp/readonly`
need to be reconnected with the GitHub preset to enable write tools. The agent
reports missing access and other blockers. Watching is limited by the agent turn's
timeout. If the agent makes fixes, the turn ends so the platform can publish them.
After publication, select the button again to continue watching.

Branch protection and other GitHub rules on the repository decide what may
merge without a person.
