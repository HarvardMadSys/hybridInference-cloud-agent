<!-- run C: `scripts/bench_operation_overhead.py --startup --json runC.json --markdown runC.md`
     executed 2026-09-20 on branch codex/perf/startup-measurement-switch at b3bfb8c + the round-3 review fixes;
     this file is the script's own --markdown output, unedited. It supersedes runs A/B,
     whose CPU denominator and startup threading were wrong. -->

# Operation-profiling business overhead

- python 3.13.9 on macOS-15.7.7-arm64-arm-64bit-Mach-O, cpus 14
- postgres 15.17 (Homebrew), docker used, iterations 150 (warmup 20)
- percentiles: nearest-rank; budget: max(10 ms, off_p95 x 1%); baseline: `off`; rounds: 4

| operation | cohort | n | p50 | p95 | mean | min | max | delta p95 | delta p95 % | budget | verdict |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| files.list.root | bare | 600 | 3.508 | 14.405 | 5.427 | 2.284 | 88.372 | - | - | - | - |
|  | off | 600 | 3.276 | 10.133 | 4.677 | 2.211 | 59.195 | - | - | - | - |
|  | on-local | 600 | 4.142 | 11.767 | 5.419 | 2.464 | 48.717 | +1.634 | +16.13 | 10.00 | pass |
|  | on-persist | 600 | 3.868 | 8.683 | 4.758 | 2.395 | 46.859 | -1.450 | -14.31 | 10.00 | pass |
| files.list.subdir | bare | 600 | 4.019 | 15.620 | 6.033 | 2.485 | 58.496 | - | - | - | - |
|  | off | 600 | 3.299 | 9.537 | 4.619 | 2.182 | 86.558 | - | - | - | - |
|  | on-local | 600 | 4.226 | 13.485 | 5.508 | 2.503 | 32.667 | +3.948 | +41.40 | 10.00 | pass |
|  | on-persist | 600 | 3.843 | 8.876 | 4.490 | 2.482 | 39.004 | -0.661 | -6.94 | 10.00 | pass |
| files.open | bare | 600 | 3.716 | 9.277 | 4.606 | 2.370 | 28.381 | - | - | - | - |
|  | off | 600 | 3.297 | 9.035 | 4.352 | 2.233 | 53.025 | - | - | - | - |
|  | on-local | 600 | 4.272 | 16.068 | 6.092 | 2.713 | 63.545 | +7.033 | +77.83 | 10.00 | pass |
|  | on-persist | 600 | 3.746 | 9.090 | 4.567 | 2.431 | 41.587 | +0.055 | +0.60 | 10.00 | pass |
| terminal.list | bare | 600 | 3.742 | 10.741 | 4.890 | 2.580 | 42.249 | - | - | - | - |
|  | off | 600 | 4.173 | 12.963 | 5.539 | 2.627 | 47.547 | - | - | - | - |
|  | on-local | 600 | 5.046 | 16.509 | 7.251 | 3.038 | 131.628 | +3.546 | +27.36 | 10.00 | pass |
|  | on-persist | 600 | 4.348 | 12.102 | 5.565 | 2.813 | 91.737 | -0.860 | -6.64 | 10.00 | pass |
| terminal.create | bare | 600 | 3.851 | 14.377 | 5.554 | 2.629 | 65.504 | - | - | - | - |
|  | off | 600 | 3.890 | 13.550 | 5.497 | 2.557 | 62.535 | - | - | - | - |
|  | on-local | 600 | 4.760 | 13.424 | 6.142 | 2.911 | 70.984 | -0.126 | -0.93 | 10.00 | pass |
|  | on-persist | 600 | 4.550 | 18.719 | 6.939 | 2.888 | 74.842 | +5.169 | +38.15 | 10.00 | pass |
| terminal.stream_attach | bare | 600 | 3.460 | 11.049 | 4.794 | 2.296 | 52.080 | - | - | - | - |
|  | off | 600 | 3.818 | 11.970 | 5.040 | 2.597 | 42.781 | - | - | - | - |
|  | on-local | 600 | 4.579 | 12.398 | 5.651 | 2.884 | 39.415 | +0.427 | +3.57 | 10.00 | pass |
|  | on-persist | 600 | 3.667 | 10.916 | 5.311 | 2.570 | 79.171 | -1.054 | -8.81 | 10.00 | pass |

| operation | noise floor abs delta p50 | noise floor abs delta p95 |
| --- | ---: | ---: |
| files.list.root | 0.067 | 4.061 |
| files.list.subdir | 0.047 | 3.445 |
| files.open | 0.034 | 4.756 |
| terminal.list | 0.680 | 3.519 |
| terminal.create | 0.842 | 3.088 |
| terminal.stream_attach | 0.118 | 4.127 |

| operation | off p50 per round | on-local p50 per round | on-persist p50 per round |
| --- | --- | --- | --- |
| files.list.root | 2.81 5.01 3.60 2.60 | 3.42 4.18 4.93 3.90 | 3.09 4.74 3.93 3.92 |
| files.list.subdir | 3.10 4.15 3.45 2.54 | 3.88 3.36 6.35 4.00 | 3.07 3.84 3.78 4.33 |
| files.open | 3.28 4.19 3.31 2.64 | 4.30 4.23 5.55 3.64 | 3.20 3.92 3.98 3.69 |
| terminal.list | 4.24 4.87 4.68 3.19 | 4.25 4.92 5.96 4.98 | 3.48 5.19 4.19 4.55 |
| terminal.create | 3.86 4.78 4.46 2.92 | 4.55 4.98 5.32 3.88 | 4.16 7.78 4.22 4.01 |
| terminal.stream_attach | 3.48 3.95 4.54 3.66 | 4.05 4.55 5.33 3.93 | 3.44 5.82 3.33 3.33 |

| family | bare analogue route | n | p50 | p95 |
| --- | --- | ---: | ---: | ---: |
| files | PUT /files (write route -> broker.write_file) | 600 | 3.948 | 15.340 |
| terminals | POST /terminals/{id}/resize (-> broker.resize_terminal) | 600 | 3.794 | 12.370 |
| startup path | cohort | n | p50 | p95 | delta p50 | delta p95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| new_group | bare | 6 | 560.000 | 655.144 | +32.281 | +107.581 |
|  | off | 6 | 527.719 | 547.563 | - | - |
|  | on-local | 6 | 573.186 | 636.648 | +45.467 | +89.085 |
|  | on-persist | 6 | 534.359 | 555.602 | +6.640 | +8.039 |
| new_group actions | container={'create': 24} resident={'new': 24} | | | | | |
| resume_stopped | bare | 6 | 578.817 | 660.468 | +22.604 | -405.555 |
|  | off | 6 | 556.212 | 1066.023 | - | - |
|  | on-local | 6 | 585.142 | 647.439 | +28.930 | -418.583 |
|  | on-persist | 6 | 562.393 | 633.800 | +6.181 | -432.223 |
| resume_stopped actions | container={'start': 24} resident={'new': 24} | | | | | |
| resume_running | bare | 6 | 185.096 | 193.718 | -3.643 | -9.693 |
|  | off | 6 | 188.738 | 203.411 | - | - |
|  | on-local | 6 | 179.811 | 190.100 | -8.928 | -13.311 |
|  | on-persist | 6 | 180.717 | 188.753 | -8.021 | -14.658 |
| resume_running actions | container={'reuse': 24} resident={'resume': 24} | | | | | |
| reuse_live | bare | 6 | 165.034 | 167.843 | -12.467 | -34.037 |
|  | off | 6 | 177.501 | 201.880 | - | - |
|  | on-local | 6 | 166.017 | 173.315 | -11.484 | -28.565 |
|  | on-persist | 6 | 161.965 | 177.199 | -15.536 | -24.681 |
| reuse_live actions | container={'reuse': 24} resident={'reuse': 24} | | | | | |

Note: p95 over 6 samples is the largest sample under the nearest-rank rule, not an estimate of a 95th percentile: read the p50 here, and raise --startup-iterations if a tail estimate is what is wanted. The deltas on new_group and resume_stopped are dominated by Docker create/start, which is hundreds of milliseconds against a collector cost of a fraction of one.
- on-local: 24 sink records, 45645 bytes
- on-persist: 24 sink records, 45687 bytes, delivery {'rows_visible_before_cohort_end': 23, 'delivered': 24, 'dropped': 0, 'drain_timeouts': 0, 'expected': 24}, postgres rows 24 (48717 record bytes)

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
| bare | 4 | 14.731 | — | n/a | 249.4 |
| off | 4 | 10.377 | — | n/a | 249.4 |
| on-local | 4 | 14.302 | +3.925 | +962.0 | 249.4 |
| on-persist | 4 | 13.619 | +3.242 | +794.6 | 249.3 |

One number per cohort, and it is the whole process: `time.process_time()` sums every thread, so the collector's exporter and the writer's tasks cannot be charged separately from the request path here. The delta against `off` is the configuration's extra process CPU on the same request set; because the client and server share this process, part of it is the load generator's own extra work. The per-record figure divides by the whole window's request count (warmup plus timed), which is what the CPU was spent on. `bare` is not comparable: it sends two extra sibling requests per round.

Not measured: network bytes (no portable per-socket counter for loopback HTTP); per-thread CPU (`resource.RUSAGE_THREAD` is unavailable here); WAL/index/disk bytes (only payload bytes are reported); container CPU and memory (no container is involved in a server-request cohort).

## Synthetic arms

### slow_sink

Synthetic: the bench-local on-local destination, delayed 20.0 ms per record, and left to accumulate across operations so consecutive bursts stack in the bounded queue. This is a stand-in for a slow control-plane-side destination, not the control plane's ingest endpoint: a route-rooted record is persisted in-process, so there is no HTTP ingest hop in this path to delay. The request path, the collector and the writer queue are untouched

| operation | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| files.list.root | 150 | 3.191 | 6.506 | 77.596 |
| files.list.subdir | 150 | 3.819 | 12.457 | 91.269 |
| files.open | 150 | 3.792 | 6.502 | 14.250 |
| terminal.list | 150 | 3.829 | 6.385 | 14.090 |
| terminal.create | 150 | 3.908 | 9.050 | 29.031 |
| terminal.stream_attach | 150 | 3.985 | 6.527 | 31.497 |

- writer counts: `{'stored': 662, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 358, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'stored': 662, 'queue_count': 358}}`
- local destination: 662/1020 records written, 1811666 bytes

### slow_db

Synthetic: PostgreSQL, by an ACCESS EXCLUSIVE lock this script held on agent_operation_measurements from a second connection

| operation | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| files.list.root | 60 | 3.614 | 9.932 | 15.533 |
| files.list.subdir | 60 | 4.360 | 6.002 | 19.895 |
| files.open | 60 | 4.394 | 5.881 | 42.233 |
| terminal.list | 60 | 3.556 | 4.525 | 18.570 |
| terminal.create | 60 | 3.881 | 6.390 | 9.534 |
| terminal.stream_attach | 60 | 3.201 | 4.239 | 4.548 |

- requests answered while the table was locked: 360
- writer during the lock: `{'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104}}`
- writer at the end of the hold: `{'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2}}`
- after release: `{'stored': 254, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2, 'stored': 254}}`, recovered after 0.553s (budget 22.0s)
- Postgres rows for the arm: 254 (412660 payload bytes)
