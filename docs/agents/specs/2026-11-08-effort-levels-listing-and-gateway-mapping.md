# Reasoning levels: DeepSeek's `effort.supported_levels`, and a gateway's mapped route

- **Status:** implemented on `codex/fix/upstream-thinking-effort`; not deployed
- **Date:** 2026-11-08
- **Amends:** [2026-11-05-upstream-reasoning-effort.md](2026-11-05-upstream-reasoning-effort.md)
  (adds a third declaration format; everything else there still holds).
- **Companion:** HarvardMadSys/hybridInference `codex/fix/model-effort-capabilities`,
  whose routes can now map public levels to the values their servers take
  (`reasoning_effort_map`) and publish only the public levels.
- **Verification:** [../reviews/2026-11-08-effort-levels-verification.md](../reviews/2026-11-08-effort-levels-verification.md)

## Third declaration format

DeepSeek's List Models API (`GET <Base URL>/models`) declares, per model,

```json
{"id": "…", "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"}}
```

`supported_levels` are the values its Chat Completions `reasoning_effort`
accepts while thinking is on; `none`, which turns thinking off, is excluded by
definition. `domain/model_efforts.effort_levels` reads it:

- a list → `supported` with the readable levels (source `models_listing_effort`);
  `[]` → `unsupported`; no list, or nothing readable in it → `unknown`
  (`not_declared`). Never "every level", and `none` is never added.
- It speaks for **Chat Completions only**. Responses and Messages are not read
  from it; they keep their own declarations or stay unknown.
- An entry carrying both `reasoning.supported_efforts` and `effort` must agree
  for Chat Completions; otherwise Chat Completions is `unknown`
  (`conflicting_declarations`).

`default_level` is not used. Auto is unchanged: Cloud Agent adds no effort
flag or field of its own at Auto. What then reaches the upstream depends on
the CLI and plan: Codex, and every request through the translation helper,
carries no effort, so the server's own default applies; Claude Code on a
direct Messages plan still sends its own implicit `output_config.effort: high`.

## A gateway's mapped route

A HybridInference route with `reasoning_effort_map: {low: high, high: xhigh,
max: max}` publishes `low/high/max` in both listing shapes and translates each
on its side. Cloud Agent stores and sends the **public** level the owner chose
(`low`, `high`, `max`), never the upstream name, on every plan: direct
Messages (`output_config.effort`) and translated Chat (`reasoning_effort`).
Claude Code and Codex are offered Auto, low, high, max; `xhigh`/`medium` are
refused with the offered levels. Persistence across tasks, follow-ups, queued
messages and claims is the 2026-11-05 mechanism, unchanged.

At Auto on such a route the two paths differ. Claude Code direct on Messages
sends its implicit `high`, which that example map sends upstream as `xhigh`;
the translated helper and Codex send no level, so the server's default
applies. For the reference self-hosted engine the operator supplied captures
for (DeepSeek-V4.1-Flash on vLLM `0.1.dev20904+g179dd0fa9`), that is a
thinking effort of 75 versus 50 (its default with no parameter). Those numbers
come from that engine's captures, not from these tests, and hold only for it
and that map.
