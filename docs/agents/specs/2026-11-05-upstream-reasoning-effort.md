# Reasoning levels on owner model connections, from the upstream's own declaration

- **Status:** implemented (this branch); not yet deployed or verified against a
  real provider
- **Date:** 2026-11-05
- **Supersedes:** the "no effort on a connection" decision in
  [the model connection design](2026-10-27-user-model-connections.zh-CN.md)
  (§二 "没做的", §五之一) and the "`output_config`（个人连接推理强度固定 Auto）"
  line in [the protocol compatibility design](2026-10-28-model-protocol-compatibility.zh-CN.md).
  The per-runtime CLI mechanisms and the wire shapes in
  [the reasoning effort design](2026-08-19-reasoning-effort-design.md) and
  `tests/fixtures/agent_runtime_streams/effort_wire_shapes.md` still hold.
- **Companion:** HarvardMadSys/hybridInference branch
  `codex/fix/model-effort-capabilities` publishes the levels its routes
  deliver in its model listings.

## Problem

Every turn on a connection ran at the provider's default: the composer offered
nothing, `require_usable` refused any level, and the translator discarded the
level the CLI sent. The reason given was that no reliable capability table
existed. The owner asked for the levels the upstream actually supports, and for
a chosen level to reach the upstream.

## Decision

**Evidence is the provider's own declaration, per model and per API.** Two
documented formats are read, from the provider's model listings, with the
owner's key, under the verification's address rules:

| Listing | Field | Describes |
|---|---|---|
| `GET <Base URL>/models` (bearer) | `data[].reasoning.supported_efforts` (OpenRouter's documented field; the HybridInference gateway now publishes it) | Chat Completions `reasoning_effort` and Responses `reasoning.effort` at that Base URL |
| `GET <Anthropic base>/v1/models?limit=1000` (`x-api-key`, `anthropic-version`) | `data[].capabilities.effort` (Anthropic Models API; the gateway publishes it too) | Anthropic Messages `output_config.effort` |

Per model and API the stored fact is `supported` (with the levels),
`unsupported` (declared: none — `supported: false`, `supported_efforts: []`, or
a `reasoning` object without `supported_efforts`), or `unknown` with a reason:
`not_read` (verified before this existed), `listing_unavailable`,
`not_listed`, `not_declared` (no field, or a malformed one). `supported_efforts:
null` is OpenRouter's "every value accepted" and maps to the documented set,
less `none` when `mandatory`. Nothing is inferred from a model's name, no
default domain is assumed, and an accepted request is not evidence: a provider
may accept and ignore a parameter.

**When it is read.** Beside every verifying save (at most two listing requests
per save, no inference, inside the save's reservation and deadline; a failed
listing never fails the save), and on the owner's **Refresh reasoning levels**
(`POST /v1/agent/model-connections/{id}/refresh-efforts`): one hourly check,
the saved key at the saved Base URL, writes only the effort facts of models
verified at the current state, guarded like an edit (409 if it moved), and
closes no process. That is the path for connections saved before this.

**What is offered.** For (connection, model, runtime): the declared levels of
the plan's *upstream* API (Chat Completions on a translated plan), narrowed to
the runtime's verified CLI vocabulary (`runtime_catalog`), in the runtime's
order; plus Auto. `/config` answers this per model and runtime
(`model_efforts`), with the status and reason, so the composer can say why
only Auto is offered. Unknown and unsupported both offer Auto only, with
different explanations.

**Validation.** `require_usable` — admission, follow-ups, queued messages,
goal rounds, Fork, claims and residencies — refuses a level outside the offer:
`effort_unknown`, `effort_unsupported` or `effort_level_unsupported` (with
`offered_efforts`). A follow-up inherits its parent's level like its model and
the effective level is checked; `"auto"` clears it. Nothing is renamed,
rounded or dropped.

**Wire.** Direct plans use the CLI mechanisms already verified per pin
(`--effort`, `-c model_reasoning_effort` and the `turn/start` effort,
`--thinking`, `options.reasoningEffort`). The translator gets the turn's level
at launch (`AGENT_COMPAT_REASONING_EFFORT`, absent at Auto) and sends it as
`reasoning_effort` on every Chat request; it never forwards the CLI's own
field, because Claude Code sends `output_config.effort: high` at Auto. A level
change already restarts the member process (it is part of the resident launch
identity), so the helper's binding is never stale. At Auto every launch is
byte-identical to before.

## The HybridInference gateway companion

Its listings carried no effort metadata. The companion change publishes, per
model, the values every route delivers, from configuration only: an
`OpenAICompatAdapter` route (the only adapter that forwards `reasoning_effort`)
with the parameter in `supported_params` delivers its declared
`reasoning_efforts`; without the parameter, none (an empty list); any other
adapter, the parameter without a domain, or a malformed domain is unknown
(nothing published). One unknown route makes the model unknown; auto models
publish nothing. Caller visibility and alias de-duplication are unchanged. A
contract test sends every advertised level through `/v1/messages` and checks
the upstream receives it. Its golden listings are this repository's
`tests/fixtures/gateway_model_effort_listing/`.

## Limits

- A provider that declares nothing (OpenAI's own `/models`, most self-hosted
  servers) stays unknown: Auto only. That is the honest outcome, and the
  composer says so.
- An OpenAI-style declaration is applied to both OpenAI APIs at that Base URL;
  neither format distinguishes them.
- The gateway publishes only what its operators configured. Which domains a
  FreeInference deployment declares, for which models, is an operator decision
  this change does not make; until a deployment running the companion declares
  them, its models stay unknown.
- Verified with local fakes and the pinned Claude Code 2.1.220 and Codex
  0.145.0; OpenCode, Kilo and pi rely on their earlier pinned captures. No real
  provider, Kata host or deployment was exercised.
