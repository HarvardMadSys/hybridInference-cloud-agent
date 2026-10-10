# Native user questions

The Claude fixture is a sanitized request and accepted response exercised with
the actual `@anthropic-ai/claude-code` 2.1.220 Linux binary. A local deterministic
Anthropic SSE server returned `AskUserQuestion`, then a closing message. The
resident invocation retained `--permission-mode bypassPermissions` and added
`--permission-prompt-tool stdio`. Claude emitted the fixture's `can_use_tool`
request, accepted its `updatedInput.answers`, and included the chosen answer in
the following tool result. The response envelope agrees with the pinned
`claude-agent-sdk==0.2.148` `_internal/query.py` permission handler. The CLI's
`sdk-tools.d.ts` defines answers by full question text, with comma-separated
strings for multiple choices.

The Codex fixture is synthetic and follows `ToolRequestUserInputParams` and
`ToolRequestUserInputResponse` JSON schemas generated from the actual pinned
`@openai/codex` 0.145.0 Linux binary with
`codex app-server generate-json-schema --experimental --out /tmp/schema`.
Its native request ID may be a number or string, and responses contain
`answers: {question_id: {answers: [strings]}}`. `serverRequest/resolved`
notifications invalidate pending requests using `params.requestId`.

OpenCode 1.18.9 and the reviewed Kilo 7.4.17 source expose
`question.asked` properties `{id, sessionID, questions}`. Questions have
`question`, `header`, `options`, optional `multiple`, and optional `custom`
(default true). Reply requests are `POST /question/{requestID}/reply` with
`{answers: string[][]}` in question order; rejection is
`POST /question/{requestID}/reject` without a body. Terminal events
`question.replied` and `question.rejected` use `properties.requestID`.

- [OpenCode schema at v1.18.9](https://github.com/anomalyco/opencode/blob/v1.18.9/packages/schema/src/v1/question.ts)
- [OpenCode routes at v1.18.9](https://github.com/anomalyco/opencode/blob/v1.18.9/packages/opencode/src/server/routes/instance/httpapi/groups/question.ts)
- [Reviewed Kilo question source](https://github.com/Kilo-Org/kilocode/blob/a0364858a6e1b69a2e2dc5434a82d5cefbe79ea7/packages/opencode/src/question/index.ts)
- [Reviewed Kilo routes](https://github.com/Kilo-Org/kilocode/blob/a0364858a6e1b69a2e2dc5434a82d5cefbe79ea7/packages/opencode/src/server/routes/instance/httpapi/groups/question.ts)
