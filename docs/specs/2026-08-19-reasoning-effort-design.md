# Reasoning effort as a first-class turn attribute

- **Status:** draft v1 — for review (Murphy)
- **Date:** 2026-08-19
- **Companion:** `2026-08-17-session-resident-harness-threads-design.md`
  (the `model_switch` capability this design's effort switch rides on);
  gateway repo `hybridInference` (§6 lands there — the two halves ship as
  one gateway PR and one agent PR)
- **One sentence:** let the owner of a thread choose how hard the model
  thinks — `reasoning_effort` — per turn, carried in-band through the
  harness like `model` is, offered only where both the runtime and the
  model can honestly deliver it.

---

## 1. Problem

Nobody chose the reasoning depth of any agent turn this platform has ever
run. Inventory of what decides it today:

| Layer | What exists | What it means for effort |
|---|---|---|
| Composer / follow-up | `runtime` + `model` only (`schemas.py` `AgentJobCreate`, follow-up `model` override) | The only effort control is "pick a different model" |
| Turn contract | `TurnInput = (prompt, model)` (`drivers.py`) | The harness is never told an effort |
| Runtime adapters | `ClaudeCodeRuntime.prepare` passes `--model`, base URL, credential; no effort flag | CLI runs at its own default |
| Gateway Anthropic facade | `output_config` appears **nowhere** in `apps/backend/serving/` (grep: zero hits); `_sanitize_for_openai_backend` pops `thinking` for OpenAI backends | Whatever the CLI sends about effort is silently discarded |
| Gateway OpenAI surfaces | `reasoning_effort` fully plumbed: request schema (`serving/schemas.py:81`), Responses translation `reasoning.effort → reasoning_effort` (`responses_translator.py:362`), adapter passthrough gated on `supported_params` (`openai_compat.py:304`) | The plumbing exists; no agent path uses it |
| Model registry | `glm-5.3`, `deepseek-v4-flash`, `deepseek-v4-pro` advertise `reasoning_effort`; glm-5.3's reasoning cannot be disabled upstream, `reasoning_effort` is "the supported way to dial thinking down" (models.yaml) | The models are ready; nothing dials them |
| Small-maxtok reroute | Gateway reroutes tiny-budget calls aimed at reasoning models (`anthropic_messages.py:340`, default off) | A patch for one symptom of uncontrolled effort, not a control |

Two consequences:

1. **A working knob is silently a no-op through us.** Claude Code has
   `--effort low|medium|high|max` (verified: `claude --help` at 2.1.90;
   the sandbox pins 2.1.220, `Dockerfile.agent-sandbox:38`), and the CLI
   binary demonstrably builds `output_config: {effort: …}` request bodies
   (string present in the installed binary). Our facade ignores
   `output_config` entirely — not even the dropped-fields warning names
   it. A user who sets effort through the CLI gets default behavior and
   no error. That is worse than the feature missing: it is the feature
   lying.
2. **The free workhorse burns tokens nobody asked for.** glm-5.3 and
   deepseek-v4-flash reason at their upstream defaults on every turn.
   For long agent threads the thinking tokens are a real fraction of
   spend (the #64/#65 footer now shows them), and the one lever that
   trades depth for latency and cost is unreachable.

## 2. Decision

`reasoning_effort` is a **per-turn attribute next to `model`**, flowing
**in-band through the harness**, offered only where **both sides of the
pair can deliver it**, with **auto (null) as the default and the only
fallback**.

- **Per-turn, like model.** Set at create, overridable on any follow-up,
  inherited from the parent turn when omitted — exactly the `model`
  semantics at `routes.py:1486`. Not per-thread config: the same thread
  legitimately wants `high` for the refactor turn and `low` for "now fix
  the typo".
- **In-band, not injected.** The runtime adapter expresses effort in its
  CLI's own dialect (`--effort`, `-c model_reasoning_effort=…`); the
  request then carries it to the gateway like any other parameter. The
  alternative — attaching effort to the inference grant and having the
  gateway mutate requests at auth time — is rejected in §9.
- **Capability-negotiated, never clamped.** The offered value set for a
  `(runtime, model)` pair is the intersection of what the runtime can
  express and what the model accepts. Empty intersection → the picker
  does not render and a non-null value is a 400 at create/follow-up.
  No silent drop, no nearest-value clamping: glm-5.3 has no `medium`,
  and a `medium` that quietly becomes `high` is the `--effort` lie again
  with extra steps.
- **Null means auto — "the platform does not instruct", not "nothing is
  instructed".** No flag on the argv, nothing stored, and the migration
  backfills nothing. The draft claimed this preserved today's behaviour
  bit-for-bit, and the §7.1 capture disproved it: **claude-code 2.1.220
  sends `output_config: {effort: "high"}` whether or not `--effort` was
  passed**, so for that runtime our Auto and our `high` are the same
  request. The honest statement is the one above; the operational
  consequence is in §7.1 and belongs in the gateway PR, not in a
  surprise.

## 3. Vocabulary and capability model

There is no platform-invented effort scale. Values are opaque strings
owned by the two capability declarations, and the platform only
intersects and transports them:

**Model side — gateway registry.** `models.yaml` grows an optional
per-model field:

```yaml
reasoning_efforts: [low, high, max]   # glm-5.3
```

Meaningful only when `reasoning_effort` is already in
`supported_params`. **An absent list means "not offered", with no
default** — v1 changed this from the draft's `[low, medium, high]`
fallback, which contradicted the paragraph after it: deepseek's domain
is unverified, so the fallback would have handed it exactly the guessed
values this field exists to prevent. Declaring the domain is how a
deployment says it checked. Encoded at rollout: glm-5.3 `[low, high,
max]` (from the registry's own comment; the ZAI doc is the authority).
deepseek-v4-flash/-pro stay unlisted, with a comment naming what to
verify before listing them.

**Runtime side — `RuntimeCapabilities`.** One new field, following the
`mcp` precedent (declare only what is verified, both halves or neither):

```python
reasoning_efforts: tuple[str, ...] = ()   # empty = cannot express effort
```

| Runtime | v1 declaration | How it is expressed | Status |
|---|---|---|---|
| claude-code | `("low", "medium", "high", "max")` | `--effort <v>` on the argv, one-shot and `prepare_live` alike | **Verified against the pinned 2.1.220**: reaches the wire as `output_config.effort`. See §7.1 for what that capture also revealed |
| codex | `("none", "minimal", "low", "medium", "high", "xhigh", "max")` | `-c model_reasoning_effort="<v>"` alongside the existing `-c` provider flags | **Verified against the pinned 0.145.0**: reaches the wire as `reasoning.effort` in the `/v1/responses` body, which the gateway's Responses translation already maps. The draft deferred this as unverified; verifying took one local capture and the answer was yes |
| pi | `("minimal", "low", "medium", "high")` | `--thinking <v>` **plus** `"reasoning": true` in the provider entry the wrapper writes | **Verified against the pinned 0.73.1.** Two of its six flag levels are dropped from the declaration: `xhigh` arrives as `high`, `off` sends nothing. The flag is inert without the provider declaration, which is why the wrapper changed too |
| opencode / kilo | `("none", "minimal", "low", "medium", "high", "xhigh", "max")` | `models.<id>.options.reasoningEffort` in the wrapper's config — no CLI flag exists | **Verified against the pinned 1.18.9 / 7.4.17.** Tier 2 for events, which says nothing about this: expressing a documented config option is not the normalization work tiering is about |

**The offered set** for a composer selection is
`runtime.reasoning_efforts ∩ model.reasoning_efforts`. For
claude-code × glm-5.3 that is `{low, high, max}`; for anything × a
non-reasoning model it is empty and the control disappears.

**All five runtimes express it, and the draft was wrong to assume
otherwise.** This table said `()` for three of them on the strength of
"unverified", which is a statement about our knowledge, not about the
CLIs. Capturing all five took one afternoon and every one of them could
already do this. The rule that survives is the *declaration* discipline —
pi's `xhigh` is dropped because it demonstrably becomes `high` — not the
habit of deferring whatever had not been looked at yet.

## 4. Control plane and storage

- **Schemas.** `AgentJobCreate.reasoning_effort: str | None = None`.
  Follow-up: `reasoning_effort: str | None` where absent inherits the
  parent turn (mirror of `model`), and the literal string `"auto"`
  clears back to null — effort needs a way home that `model` never
  needed, because null is a legitimate stored value. On *create* the
  sentinel is also accepted as Auto (v1 addition): there is nothing to
  inherit, so both spellings are the same answer, and 400-ing a caller
  who typed the value the UI shows would be gratuitous.
- **Validation** at create and follow-up, fail-fast like the model
  check: value must be in the pair's offered set, else 400 naming the
  set. The offered set is computed from the runtime registry plus the
  gateway catalog (§6b); a catalog that answers without effort metadata
  yields empty sets everywhere — the feature fails closed to auto.
- **Storage.** `agent_jobs.reasoning_effort TEXT NULL`, one alembic
  revision. Exposed on job responses; no backfill.
- **Turn contract.** `TurnInput` grows `reasoning_effort: str | None`.
  Drivers pass it to `prepare` / `prepare_live` verbatim.
- **Resident sessions.** An effort change is a restart trigger,
  identical to `model_switch: "restart"` in `HarnessCapabilities` — the
  flag is fixed at spawn. The replay world switches per-turn for free,
  as it does for model. No new capability axis: effort rides the
  existing one, and the comparison the runner already does for "did the
  model change" gains one field.
- **Grants: unchanged.** `grants.py` says it plainly: "Models are the
  whole of it. A grant authorizes model inference and nothing else."
  Effort is a generation parameter, not a scope; it never touches the
  grant, the clamp, or the scope check.

## 5. UI

- **Composer:** a "Reasoning" picker next to Model. Options: `Auto` plus
  the offered set for the current (runtime, model) selection; hidden
  when the set is empty. Default Auto.
- **Follow-up composer:** same picker, initialized to the thread's
  current value; changing it on a resident thread restarts the session
  exactly as a model switch does (and should say so the same way).
- **JobDetail header:** show the effort next to the model when non-null.
  Auto renders nothing — absence of a chip is the honest display of
  "nobody chose".
- **`AgentConfigResponse`** grows two maps the composer intersects
  client-side (server re-validates on create regardless):
  `runtime_reasoning_efforts: dict[str, list[str]]` and
  `model_reasoning_efforts: dict[str, list[str]]`. Both empty-safe;
  an old backend answering without them hides the feature.

## 6. Gateway work (hybridInference repo)

Three contained changes; (a) and (c) have value independent of the agent
platform because they fix the lying knob for every Claude Code user
pointed at the gateway, not just sandbox jobs.

- **(a) Anthropic Messages facade: translate effort instead of eating
  it.** When the backend is OpenAI-compatible and the resolved model
  advertises `reasoning_effort`, map inbound `output_config.effort` →
  body `reasoning_effort`. When the value is outside the model's
  `reasoning_efforts`, drop it **with a warning that names it** (public
  CLI users will send `xhigh` at models that never heard of it; the
  agent path cannot reach this branch because create validated). When
  the model has no reasoning support, `output_config` joins the
  `_sanitize_for_openai_backend` dropped-fields warning — today it is
  invisible even to that.
- **(b) Catalog metadata.** `ModelConfig.reasoning_efforts` from
  models.yaml (§3), surfaced as a new top-level key on
  `GET /internal/model-catalog` (`internal_lookups.py:113`):
  `reasoning_efforts: {model_id: [values]}`. Additive; the agent's
  `GatewayClient` treats absence as empty.
- **(c) Nothing else.** No runtime-settings toggle — unlike the
  small-maxtok reroute this changes no request that didn't opt in. The
  reroute itself is untouched: it fires on tiny `max_tokens` (the CLI's
  title/summary helpers), which carry no effort intent and are not the
  turn. No `thinking`-param mapping for the on/off-only models
  (glm-5.1/5.2 et al.) — that is a semantic invention ("does low mean
  off?") deferred until someone actually wants it.

## 7. Verification

### 7.1 Wire shapes — done, and one of them changed the design

Both pinned CLIs were captured against a local recording endpoint on
2026-08-19; the results are in
`tests/fixtures/agent_runtime_streams/effort_wire_shapes.md`.

- **codex 0.145.0** emits `reasoning: {effort: …}` on `/v1/responses`
  exactly as expected, so it is wired in v1 rather than deferred. It does
  *not* validate the value locally (`--strict-config` catches an unknown
  key, not a nonsense level), which is why the model's declared domain is
  the constraint that matters.
- **claude-code 2.1.220 always sends an effort, defaulting to `high`.**
  This was not expected and is the finding with operational weight: the
  gateway learning to translate `output_config.effort` changes existing
  claude-code traffic to every model declaring `reasoning_effort`, not
  only turns that opt in. It is not a reason to hold the change — dropping
  the field was never "no instruction", it was "the instruction the
  harness sent, discarded" — but it must be stated on the gateway PR and
  watched on the first deploy.

Remaining, unchanged in kind: a CLI bump that moves either field should
fail a test before it fails a user, so re-capture on version bumps.
2. **Unit, agent repo:** argv contains `--effort` iff set (one-shot and
   live); 400 on out-of-set values and on effort-with-empty-set;
   follow-up inherit / `"auto"` reset; config maps empty-safe.
3. **Unit, gateway:** `output_config.effort` → `reasoning_effort` for a
   supporting model; out-of-set dropped with warning; unsupported model
   adds `output_config` to the dropped list; Responses path untouched.
4. **Staging e2e:** one thread on glm-5.3, turns at `low` and `max`;
   confirm the forwarded param in gateway logs and a visible
   thinking-token delta in the #64/#65 footer — the observability we
   just shipped is exactly the instrument that proves this knob is
   connected.

## 8. Phasing

| Phase | Repo | Content |
|---|---|---|
| P0 | gateway | §6a facade translation + §6b catalog metadata (+ models.yaml values, DeepSeek domain verified) |
| P1 | agent | §4 schema/store/validation/TurnInput + claude-code `--effort` + §5 UI + §7.1 capture |
| P2 | agent | ~~codex / pi / opencode / kilo verification~~ — all done, all in P1. Nothing is deferred |

P0 merges first; P1 degrades to invisible against an old gateway.

## 9. Alternatives rejected

- **Grant-attached effort, gateway-injected.** Attach effort to the
  inference grant; the gateway mutates every request under that token.
  Uniform across all five runtimes (Tier 2 included) and no restart on
  change — but it breaks the grant's stated boundary ("models are the
  whole of it"), makes the facade rewrite requests behind the harness's
  back, and manufactures precedence conflicts the moment a CLI also
  sends its own value. The runtimes that can't express effort in-band
  don't get a lying knob as compensation; they get no knob, honestly.
- **A platform-normalized three-level scale with per-model clamping.**
  Rejected for the same reason the offered-set model exists: clamping is
  silent meaning-change. The intersection is the scale.
- **Deployment-wide default effort.** A config knob nobody asked for;
  null-is-auto already encodes the deployment default, which is the
  upstream's.
- **Wiring `MAX_THINKING_TOKENS` / thinking budgets instead.** The
  budget-tokens control is the previous generation's dial and is being
  removed upstream; `--effort` / `reasoning_effort` is the current one,
  and it is what our backends accept.
