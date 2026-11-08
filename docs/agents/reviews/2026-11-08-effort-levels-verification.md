# Verification: `effort.supported_levels` and a gateway's mapped route

- **Date:** 2026-11-08
- **Design:** [../specs/2026-11-08-effort-levels-listing-and-gateway-mapping.md](../specs/2026-11-08-effort-levels-listing-and-gateway-mapping.md)
- **Scope:** local only, fakes and fixtures; no provider, gateway deployment or
  stored key was contacted. Not live deployment acceptance.

## Repository gates

On `07a3d795` (clean tree, Python 3.13, Node 22): `scripts/ci-local.sh all`
(backend: ruff, mypy, non-external/non-db tests; database: the dbtest tier on
a disposable PostgreSQL 15; frontend: lint, format, types, tests, build
guards) and `make docs-verify`. Results: all tiers pass -- ruff, mypy (834 files), pytest 14036 passed and 8 skipped (root/UID-switching and local-Chromium cases), dbtest 2145 passed, vitest 4864, 78 browser tests, docs-verify.

## Focused checks

- Domain: DeepSeek's documented `effort` entry reads as Chat Completions
  `low, high, max`; `[]` is unsupported; a missing list, or nothing readable,
  is unknown (never "every level"); `none` is never added; Responses and
  Messages are not read from it; two disagreeing shapes make Chat Completions
  unknown (`conflicting_declarations`).
- The HybridInference gateway's golden listings (copied from its tests) now
  include a route with a `reasoning_effort_map`: Claude Code and Codex are
  offered Auto, `low`, `high`, `max` on it, on a direct Messages plan and on a
  translated Chat plan; `xhigh`, `medium` and `none` are refused with the
  offered levels. `/config`, task admission (the chosen public level is
  stored), follow-ups and a queued message (dbtest) keep `low`.
- Pinned CLIs (external tier, local fakes): Claude Code 2.1.220 direct sends
  `output_config.effort` `low`/`high`/`max` for the mapped model; translated
  Claude Code and Codex 0.145.0 send `reasoning_effort` `low`/`high`/`max` on
  every Chat request and none at Auto; Codex direct sends `reasoning.effort`.
  19/19, request bodies kept with the delivery evidence.

## Limits

- The gateway's side (public level -> upstream value) is verified in the
  gateway repository; here the agent only ever sends the public level.
- `default_level` is read by nothing: Auto still sends no level.
