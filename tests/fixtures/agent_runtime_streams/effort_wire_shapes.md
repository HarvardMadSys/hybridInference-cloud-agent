# Reasoning-effort wire shapes, captured from the pinned CLIs

Recorded 2026-08-19 against a local capture endpoint, using the exact versions
`deploy/sandbox/Dockerfile.agent-sandbox` pins. Not derived from
documentation — the same discipline as the stream fixtures beside this file.

Reproduce by pointing the CLI's base URL at a server that logs the request body
and returns an error; the shapes below are the last request before it gave up.

## codex 0.145.0 → `POST /v1/responses`

| Invocation | `reasoning` in the body |
|---|---|
| (no override) | `{"summary": "auto"}` |
| `-c model_reasoning_effort="low"` | `{"effort": "low", "summary": "auto"}` |
| `-c model_reasoning_effort="xhigh"` | `{"effort": "xhigh", "summary": "auto"}` |

The gateway's Responses translation maps `reasoning.effort` onto
`reasoning_effort`, so this reaches the model as the parameter it expects.

**The CLI does not validate the value.** `--strict-config` rejects an unknown
*key* (`definitely_not_a_key` → "unknown configuration field") but accepts
`model_reasoning_effort="banana"` without complaint. The level names in
`CodexRuntime.EFFORT_LEVELS` are therefore a vocabulary, not a guarantee; the
model's own declared domain is what actually refuses a nonsense level.

## claude-code 2.1.220 → `POST /v1/messages?beta=true`

| Invocation | `output_config` in the body |
|---|---|
| **(no `--effort`)** | **`{"effort": "high"}`** |
| `--effort low` / `max` / `xhigh` | `{"effort": "low"／"max"／"xhigh"}` |
| `--effort ultra` / `none` / `minimal` | **`{"effort": "high"}`** — silently |

**`xhigh` works, and `--help` does not say so.** The 2.1.90 help string reads
"(low, medium, high, max)"; the first declaration was written from it and
missed a level the product supports. The wire is the authority here, not the
help text.

**An unknown level is not refused — it becomes `high`.** That is why
`ClaudeCodeRuntime.EFFORT_LEVELS` has to be exact in a way the config-file
runtimes' lists do not: a level declared here that the CLI does not know would
run the turn at `high` while the UI reported something else.

**The CLI always sends an effort, defaulting to `high`.** This is the one that
matters operationally: "the platform sent no flag" is *not* the same as "the
model was given no instruction". Two consequences, both real:

1. Our Auto and our `high` are indistinguishable on the wire for this runtime.
   Auto means "we do not instruct the CLI", not "nothing is instructed".
2. The gateway learning to translate `output_config.effort` is a behaviour
   change for existing traffic, not only for turns that opt in: every
   claude-code request to a model declaring `reasoning_effort` starts carrying
   `high` where it previously carried nothing. See the gateway PR's note.

Every turn also carries `thinking: {"type": "adaptive"}`, which the OpenAI
sanitizer drops. That predates this work and is unchanged.

## pi 0.73.1 and 1.2.0 → `POST /v1/chat/completions`

`--thinking <level>`, and the level maps onto `reasoning_effort` directly —
no gateway translation involved, the chat surface already forwards it.
Rechecked on 2026-10-15 against 1.2.0 from `@earendil-works/pi-coding-agent`,
the package pi moved to: every row, and both findings below, are unchanged.

| `--thinking` | `reasoning_effort` on the wire |
|---|---|
| (unset) | absent, but see below |
| `off` | field absent |
| `minimal` / `low` / `medium` / `high` | the same word |
| **`xhigh`** | **`high`** |

Two findings, both load-bearing:

1. **`xhigh` collapses to `high`.** `PiRuntime.EFFORT_LEVELS` therefore omits
   it: offering a level that silently becomes another one is the failure this
   control exists to end, in miniature.
2. **The flag does nothing on its own.** With the provider entry the wrapper
   used to write — `{"id": model}` — *every* level produced a request with no
   `reasoning_effort` at all. The entry must also carry `"reasoning": true`,
   which is why `pi-freeinference` now writes it and why the runner sets
   `PI_GATEWAY_REASONING` only for a turn that asked for a depth: declaring it
   unconditionally makes pi send `medium` on every turn, and Auto would stop
   meaning "as before".

## opencode 1.18.9 and kilo 7.4.17 → `POST /v1/chat/completions`

No command-line flag exists. The level goes in the provider config the wrapper
writes, and exactly one shape works:

| Config shape | `reasoning_effort` on the wire |
|---|---|
| `models.<id>.options.reasoningEffort` | the value |
| `models.<id>.options.reasoning_effort` | absent (silently ignored) |
| `models.<id>.options.providerOptions.*` | absent (`providerOptions` is forwarded verbatim as a body field, which the model will not read) |
| `models.<id>.reasoningEffort` | absent |

Both CLIs pass the value through without validating it, like codex.

Verified end to end through the patched wrappers themselves, not only the raw
CLIs: with the runner's environment variable set the option appears, and
without it the request is byte-identical to what these runtimes sent before.
