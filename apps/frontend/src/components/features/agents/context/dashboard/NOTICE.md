# Adapted from dsh-context

Parts of the Context dashboard in this directory are adapted from
[bowenliang123/dsh-context](https://github.com/bowenliang123/dsh-context) at
commit `32982aa6aa2c70fbda6b81281de98a72ea625001` (release 0.78.2), under the
Apache License 2.0 reproduced in `LICENSE.dsh-context` (Copyright 2025
bowenliang123). The upstream repository has no NOTICE file.

Each adapted file names its upstream source in its header comment.

| This file                           | Upstream file                                                                                                                                    | Changes                                                                                                                                                                                   |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Donut.tsx`                         | `src/client/components/donut.tsx`                                                                                                                | Same ring geometry, dash arcs and segment gap; Tailwind classes instead of CSS variables.                                                                                                 |
| `SliceList.tsx`                     | `src/client/components/sliceList.tsx`                                                                                                            | Rows can say "not recorded" instead of a number.                                                                                                                                          |
| `TokenStats.tsx`                    | `src/client/components/statsTokens.tsx`, `tokenRows.ts`                                                                                          | Two bases: recorded content (estimate) and provider-reported usage (exact); no anchoring of estimates to billed input.                                                                    |
| `TimingStats.tsx`, `timingModel.ts` | `src/client/components/statsTiming.tsx`, `timingStrip.tsx`                                                                                       | Strip geometry, `bandAt` and the gradient fallback kept; the kinds are this platform's (Gateway TTFT and generation, tool windows, not attributed).                                       |
| `WindowCard.tsx`                    | `src/client/components/currentComposition.tsx`, `reserve.ts`                                                                                     | Two tracks (observed, recorded) instead of one bar; no reserve or residual category is computed.                                                                                          |
| `ContextTrend.tsx`, `trendModel.ts` | `src/client/components/trendChart.tsx`, `trendWindow.ts`, `trendOverview.tsx`, `trendDnaBars.tsx`, `trendDurationCurve.tsx`, `src/client/dna.ts` | Step/Turn, Total/Delta, Adaptive, DNA, Duration and the overview window; drawn as stacked areas in SVG; duration only from a latency joined by response id or an event-derived step time. |
| `ContextBrowser.tsx`                | `src/client/components/browser.tsx`, `browserRows.ts`                                                                                            | Items are the platform's reconstruction from stored events; changes by stored-event identity.                                                                                             |
| `palette.ts`                        | `src/client/categories.ts`, `src/client/styles/*.css`                                                                                            | Colours re-expressed as Tailwind classes with dark-mode counterparts.                                                                                                                     |

**This is a copy, not a dependency.** Nothing from dsh-context is installed or
imported at build or run time, and the upstream repository was not changed.
