<!-- run A: `scripts/bench_operation_overhead.py --startup --json runA.json --markdown runA.md`
     executed 2026-09-20 on branch codex/perf/startup-measurement-switch at d5d99b3 + the review fixes;
     this file is the script's own --markdown output, unedited. -->

# Operation-profiling business overhead

- python 3.13.9 on macOS-15.7.7-arm64-arm-64bit-Mach-O, cpus 14
- postgres 15.17 (Homebrew), docker used, iterations 150 (warmup 20)
- percentiles: nearest-rank; budget: max(10 ms, off_p95 x 1%); baseline: `off`; rounds: 4

| operation | cohort | n | p50 | p95 | mean | min | max | delta p95 | delta p95 % | budget | verdict |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| files.list.root | bare | 600 | 2.911 | 5.692 | 3.587 | 2.373 | 45.008 | - | - | - | - |
|  | off | 600 | 2.901 | 7.706 | 3.864 | 2.320 | 53.736 | - | - | - | - |
|  | on-local | 600 | 3.230 | 6.325 | 3.866 | 2.605 | 32.305 | -1.381 | -17.92 | 10.00 | pass |
|  | on-persist | 600 | 3.268 | 6.688 | 3.816 | 2.441 | 23.888 | -1.018 | -13.21 | 10.00 | pass |
| files.list.subdir | bare | 600 | 2.985 | 7.269 | 3.772 | 2.390 | 29.502 | - | - | - | - |
|  | off | 600 | 2.926 | 6.605 | 3.658 | 2.200 | 40.230 | - | - | - | - |
|  | on-local | 600 | 3.283 | 7.050 | 3.968 | 2.668 | 40.756 | +0.445 | +6.74 | 10.00 | pass |
|  | on-persist | 600 | 3.314 | 6.665 | 4.022 | 2.689 | 57.522 | +0.060 | +0.91 | 10.00 | pass |
| files.open | bare | 600 | 2.929 | 6.740 | 3.742 | 2.366 | 63.785 | - | - | - | - |
|  | off | 600 | 3.006 | 8.294 | 3.873 | 2.132 | 39.568 | - | - | - | - |
|  | on-local | 600 | 3.299 | 6.573 | 3.914 | 2.517 | 39.463 | -1.721 | -20.75 | 10.00 | pass |
|  | on-persist | 600 | 3.296 | 6.805 | 3.945 | 2.654 | 26.635 | -1.489 | -17.96 | 10.00 | pass |
| terminal.list | bare | 600 | 3.289 | 7.989 | 4.109 | 2.604 | 36.259 | - | - | - | - |
|  | off | 600 | 3.363 | 7.029 | 4.075 | 2.448 | 54.846 | - | - | - | - |
|  | on-local | 600 | 3.674 | 6.648 | 4.239 | 2.618 | 53.695 | -0.381 | -5.42 | 10.00 | pass |
|  | on-persist | 600 | 3.599 | 6.227 | 4.405 | 2.946 | 54.800 | -0.802 | -11.41 | 10.00 | pass |
| terminal.create | bare | 600 | 3.368 | 7.854 | 4.239 | 2.618 | 34.651 | - | - | - | - |
|  | off | 600 | 3.282 | 7.555 | 4.150 | 2.503 | 50.389 | - | - | - | - |
|  | on-local | 600 | 3.967 | 9.113 | 4.889 | 2.909 | 42.041 | +1.557 | +20.62 | 10.00 | pass |
|  | on-persist | 600 | 3.683 | 7.883 | 4.547 | 2.839 | 59.644 | +0.328 | +4.35 | 10.00 | pass |
| terminal.stream_attach | bare | 600 | 3.398 | 10.066 | 4.556 | 2.483 | 44.043 | - | - | - | - |
|  | off | 600 | 3.064 | 7.098 | 3.857 | 2.118 | 40.130 | - | - | - | - |
|  | on-local | 600 | 3.518 | 6.041 | 3.978 | 2.474 | 30.605 | -1.056 | -14.88 | 10.00 | pass |
|  | on-persist | 600 | 3.419 | 8.048 | 4.169 | 2.386 | 29.643 | +0.951 | +13.39 | 10.00 | pass |

| operation | noise floor abs delta p50 | noise floor abs delta p95 |
| --- | ---: | ---: |
| files.list.root | 0.021 | 2.555 |
| files.list.subdir | 0.190 | 2.700 |
| files.open | 0.388 | 4.169 |
| terminal.list | 0.351 | 0.473 |
| terminal.create | 0.518 | 4.754 |
| terminal.stream_attach | 0.516 | 2.141 |

| operation | off p50 per round | on-local p50 per round | on-persist p50 per round |
| --- | --- | --- | --- |
| files.list.root | 2.65 2.90 3.71 2.88 | 3.26 3.15 3.19 3.36 | 3.17 3.31 3.07 3.68 |
| files.list.subdir | 2.86 2.82 3.13 2.87 | 3.19 3.27 3.25 3.52 | 3.18 3.17 3.28 3.98 |
| files.open | 3.04 2.94 3.45 2.74 | 3.21 3.32 3.21 3.44 | 3.11 3.12 3.27 3.92 |
| terminal.list | 3.13 3.33 3.92 3.15 | 3.41 3.68 3.82 3.79 | 3.43 3.36 3.60 4.10 |
| terminal.create | 3.08 3.13 4.22 3.14 | 3.56 3.62 4.93 4.26 | 3.43 3.60 3.41 4.54 |
| terminal.stream_attach | 2.88 2.92 3.76 2.85 | 3.35 3.55 3.03 4.25 | 3.25 3.27 3.28 4.08 |

| family | bare analogue route | n | p50 | p95 |
| --- | --- | ---: | ---: | ---: |
| files | PUT /files (write route -> broker.write_file) | 600 | 3.563 | 12.722 |
| terminals | POST /terminals/{id}/resize (-> broker.resize_terminal) | 600 | 3.517 | 9.796 |
| startup path | cohort | n | p50 | p95 | delta p50 | delta p95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| new_group | bare | 6 | 543.773 | 567.099 | -43.385 | -107.886 |
|  | off | 6 | 587.158 | 674.985 | - | - |
|  | on-local | 6 | 577.258 | 1462.670 | -9.900 | +787.685 |
|  | on-persist | 6 | 581.899 | 692.638 | -5.259 | +17.653 |
| new_group actions | container={'create': 24} resident={'new': 24} | | | | | |
| resume_stopped | bare | 6 | 585.512 | 3081.763 | +35.671 | +1818.639 |
|  | off | 6 | 549.840 | 1263.124 | - | - |
|  | on-local | 6 | 551.711 | 585.128 | +1.871 | -677.997 |
|  | on-persist | 6 | 573.143 | 950.891 | +23.303 | -312.233 |
| resume_stopped actions | container={'start': 24} resident={'new': 24} | | | | | |
| resume_running | bare | 6 | 179.307 | 197.528 | -1.457 | -7.308 |
|  | off | 6 | 180.764 | 204.836 | - | - |
|  | on-local | 6 | 179.633 | 182.263 | -1.131 | -22.572 |
|  | on-persist | 6 | 178.117 | 183.654 | -2.647 | -21.182 |
| resume_running actions | container={'reuse': 24} resident={'resume': 24} | | | | | |
| reuse_live | bare | 6 | 167.117 | 173.005 | -1.101 | -61.881 |
|  | off | 6 | 168.218 | 234.886 | - | - |
|  | on-local | 6 | 169.699 | 209.699 | +1.481 | -25.187 |
|  | on-persist | 6 | 168.467 | 171.024 | +0.249 | -63.862 |
| reuse_live actions | container={'reuse': 24} resident={'reuse': 24} | | | | | |

Note: p95 over 6 samples is the largest sample under the nearest-rank rule, not an estimate of a 95th percentile: read the p50 here, and raise --startup-iterations if a tail estimate is what is wanted. The deltas on new_group and resume_stopped are dominated by Docker create/start, which is hundreds of milliseconds against a collector cost of a fraction of one.
- on-local: 24 sink records, 45652 bytes
- on-persist: 24 sink records, 45694 bytes, delivery {'delivered': 24, 'dropped': 0, 'drain_timeouts': 0, 'expected': 24}, postgres rows 24 (48724 record bytes)

## Drops and destinations

| cohort | sink written/expected | writer stored | dropped_full | dropped_timeout | rejected | failed | destination |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| bare | 0/0 | 0 | 0 | 0 | 0 | 0 | none (profiler off) |
| off | 0/0 | 0 | 0 | 0 | 0 | 0 | none (profiler off) |
| on-local | 4080/4080 | 4080 | 0 | 0 | 0 | 0 | local JSONL (bench-local capability) |
| on-persist | 4080/4080 | 4080 | 0 | 0 | 0 | 0 | Postgres agent_operation_measurements |

## Process CPU at equal work

| cohort | rounds | process CPU s | vs `off` s | per record µs | peak RSS MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| bare | 4 | 12.438 | — | n/a | 174.9 |
| off | 4 | 8.910 | — | n/a | 174.9 |
| on-local | 4 | 11.391 | +2.481 | +689.1 | 174.9 |
| on-persist | 4 | 11.435 | +2.525 | +701.4 | 174.9 |

One number per cohort, and it is the whole process: `time.process_time()` sums every thread, so the collector's exporter and the writer's tasks cannot be charged separately from the request path here. The delta against `off` is the configuration's extra process CPU on the same request set; because the client and server share this process, part of it is the load generator's own extra work. `bare` is not comparable: it sends two extra sibling requests per round.

Not measured: network bytes (no portable per-socket counter for loopback HTTP); per-thread CPU (`resource.RUSAGE_THREAD` is unavailable here); WAL/index/disk bytes (only payload bytes are reported); container CPU and memory (no container is involved in a server-request cohort).

## Synthetic arms

### slow_sink

Synthetic: the bench-local on-local destination, delayed 20.0 ms per record, and left to accumulate across operations so consecutive bursts stack in the bounded queue. This is a stand-in for a slow control-plane-side destination, not the control plane's ingest endpoint: a route-rooted record is persisted in-process, so there is no HTTP ingest hop in this path to delay. The request path, the collector and the writer queue are untouched

| operation | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| files.list.root | 150 | 3.950 | 7.923 | 55.178 |
| files.list.subdir | 150 | 3.504 | 5.078 | 56.552 |
| files.open | 150 | 3.342 | 5.044 | 6.375 |
| terminal.list | 150 | 3.382 | 5.342 | 31.226 |
| terminal.create | 150 | 3.754 | 5.589 | 9.750 |
| terminal.stream_attach | 150 | 3.454 | 5.474 | 35.815 |

- writer counts: `{'stored': 631, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 389, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'stored': 631, 'queue_count': 389}}`
- local destination: 631/1020 records written, 1726097 bytes

### slow_db

Synthetic: PostgreSQL, by an ACCESS EXCLUSIVE lock this script held on agent_operation_measurements from a second connection

| operation | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| files.list.root | 60 | 3.874 | 8.824 | 41.374 |
| files.list.subdir | 60 | 3.605 | 5.522 | 8.489 |
| files.open | 60 | 3.294 | 4.865 | 5.806 |
| terminal.list | 60 | 3.839 | 5.885 | 10.963 |
| terminal.create | 60 | 3.631 | 5.810 | 12.922 |
| terminal.stream_attach | 60 | 2.901 | 4.352 | 4.844 |

- requests answered while the table was locked: 360
- writer during the lock: `{'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104}}`
- writer at the end of the hold: `{'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2}}`
- after release: `{'stored': 254, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2, 'stored': 254}}`, recovered after 0.333s (budget 22.0s)
- Postgres rows for the arm: 254 (412606 payload bytes)
