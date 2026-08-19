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
| `--effort low` | `{"effort": "low"}` |
| `--effort max` | `{"effort": "max"}` |

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
