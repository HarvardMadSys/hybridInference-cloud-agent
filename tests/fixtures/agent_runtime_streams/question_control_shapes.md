# Native user interactions

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

The same real binary was driven through
`EnterPlanMode` → `ExitPlanMode` → `Bash` under that production invocation.
Entering plan mode emitted no control request. Exiting emitted `can_use_tool`
with `requires_user_interaction: true`; replying with `behavior: allow` and the
unchanged `updatedInput` restored `bypassPermissions`, and the following Bash
call ran without another prompt. No `updatedPermissions` response was needed.
The observed 2.1.220 exit input was empty; its type also permits the deprecated
`allowedPrompts` field covered by the unit regression.

The plan fixture was captured with the same pinned Claude binary and resident
flags against a deterministic local SSE server. The model called
`EnterPlanMode`, read a workspace file, wrote the native plan file, and called
`ExitPlanMode` with `{}`. Entering plan mode, reading, and writing the plan file
required no stdio permission request. For `ExitPlanMode`, the CLI added `plan`
and `planFilePath` to its `can_use_tool` input and required user interaction.
Returning `behavior: "allow"` with that exact `updatedInput`, without
`updatedPermissions`, produced the approved-plan tool result, the native
exited-plan-mode reminder, and a successful subsequent workspace write. An
exit without a plan file instead sent `{}` as its input and accepted the same
allow response. A deny response produced an error tool result containing the
decline message and recorded the native permission denial; it did not emit
the exited-plan-mode reminder. Plan mode under `bypassPermissions` does not
enforce a filesystem write boundary.

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
