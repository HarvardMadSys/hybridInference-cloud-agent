<!-- run D: `scripts/bench_operation_overhead.py --startup --json runD.json --markdown runD.md`
     executed 2026-09-20 on branch codex/perf/startup-measurement-switch at b3bfb8c + the round-3 review fixes;
     this file is the script's own --markdown output, unedited. It supersedes runs A/B,
     whose CPU denominator and startup threading were wrong. -->

# Operation-profiling business overhead

- python 3.13.9 on macOS-15.7.7-arm64-arm-64bit-Mach-O, cpus 14
- postgres 15.17 (Homebrew), docker used, iterations 150 (warmup 20)
- percentiles: nearest-rank; budget: max(10 ms, off_p95 x 1%); baseline: `off`; rounds: 4

| operation | cohort | n | p50 | p95 | mean | min | max | delta p95 | delta p95 % | budget | verdict |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| files.list.root | bare | 600 | 2.916 | 5.169 | 3.249 | 2.108 | 17.338 | - | - | - | - |
|  | off | 600 | 2.684 | 6.477 | 3.328 | 2.232 | 30.552 | - | - | - | - |
|  | on-local | 600 | 3.302 | 7.062 | 3.977 | 2.556 | 34.177 | +0.585 | +9.03 | 10.00 | pass |
|  | on-persist | 600 | 3.263 | 6.545 | 3.800 | 2.674 | 33.514 | +0.067 | +1.04 | 10.00 | pass |
| files.list.subdir | bare | 600 | 2.896 | 5.010 | 3.226 | 2.187 | 15.783 | - | - | - | - |
|  | off | 600 | 2.844 | 6.109 | 3.408 | 2.168 | 36.409 | - | - | - | - |
|  | on-local | 600 | 3.371 | 7.228 | 4.446 | 2.633 | 80.072 | +1.119 | +18.32 | 10.00 | pass |
|  | on-persist | 600 | 3.578 | 7.647 | 4.226 | 2.731 | 28.154 | +1.538 | +25.18 | 10.00 | pass |
| files.open | bare | 600 | 3.109 | 8.294 | 4.092 | 2.208 | 40.847 | - | - | - | - |
|  | off | 600 | 2.807 | 5.928 | 3.419 | 2.278 | 26.218 | - | - | - | - |
|  | on-local | 600 | 3.615 | 10.722 | 4.769 | 2.747 | 38.232 | +4.793 | +80.85 | 10.00 | pass |
|  | on-persist | 600 | 3.376 | 7.106 | 4.038 | 2.441 | 28.900 | +1.177 | +19.86 | 10.00 | pass |
| terminal.list | bare | 600 | 3.846 | 10.701 | 5.029 | 2.543 | 54.537 | - | - | - | - |
|  | off | 600 | 3.296 | 7.359 | 4.163 | 2.556 | 46.143 | - | - | - | - |
|  | on-local | 600 | 4.200 | 14.809 | 5.793 | 3.045 | 48.683 | +7.450 | +101.23 | 10.00 | pass |
|  | on-persist | 600 | 3.468 | 7.106 | 4.037 | 2.838 | 25.189 | -0.253 | -3.43 | 10.00 | pass |
| terminal.create | bare | 600 | 3.319 | 9.306 | 4.357 | 2.527 | 66.463 | - | - | - | - |
|  | off | 600 | 3.222 | 6.601 | 3.830 | 2.597 | 25.705 | - | - | - | - |
|  | on-local | 600 | 3.973 | 10.432 | 5.222 | 3.059 | 67.469 | +3.831 | +58.04 | 10.00 | pass |
|  | on-persist | 600 | 3.714 | 7.057 | 4.273 | 2.692 | 51.329 | +0.456 | +6.91 | 10.00 | pass |
| terminal.stream_attach | bare | 600 | 3.071 | 8.202 | 4.179 | 2.303 | 57.444 | - | - | - | - |
|  | off | 600 | 2.911 | 6.400 | 3.560 | 2.397 | 29.043 | - | - | - | - |
|  | on-local | 600 | 3.833 | 8.818 | 4.828 | 2.536 | 120.894 | +2.418 | +37.79 | 10.00 | pass |
|  | on-persist | 600 | 3.568 | 5.926 | 3.971 | 2.764 | 24.100 | -0.474 | -7.41 | 10.00 | pass |

| operation | noise floor abs delta p50 | noise floor abs delta p95 |
| --- | ---: | ---: |
| files.list.root | 0.011 | 2.938 |
| files.list.subdir | 0.175 | 1.071 |
| files.open | 0.299 | 0.503 |
| terminal.list | 0.387 | 2.248 |
| terminal.create | 0.544 | 2.732 |
| terminal.stream_attach | 0.140 | 1.908 |

| operation | off p50 per round | on-local p50 per round | on-persist p50 per round |
| --- | --- | --- | --- |
| files.list.root | 2.70 2.64 2.58 2.79 | 3.21 3.25 3.93 2.98 | 3.14 3.37 3.17 3.55 |
| files.list.subdir | 3.01 2.86 2.80 2.59 | 3.06 3.21 3.94 3.52 | 3.30 3.42 3.46 4.15 |
| files.open | 3.10 2.72 2.78 2.61 | 3.30 3.10 4.59 3.71 | 3.27 3.53 3.29 3.42 |
| terminal.list | 3.46 3.11 3.49 3.07 | 3.78 3.59 4.79 4.63 | 3.36 3.54 3.44 3.62 |
| terminal.create | 3.35 3.07 3.68 2.99 | 3.48 3.66 4.52 4.91 | 3.38 3.75 3.72 3.97 |
| terminal.stream_attach | 2.92 2.86 3.00 2.79 | 3.52 3.56 4.11 3.96 | 3.25 3.30 4.01 3.80 |

| family | bare analogue route | n | p50 | p95 |
| --- | --- | ---: | ---: | ---: |
| files | PUT /files (write route -> broker.write_file) | 600 | 3.460 | 7.354 |
| terminals | POST /terminals/{id}/resize (-> broker.resize_terminal) | 600 | 3.385 | 8.447 |
| startup path | cohort | n | p50 | p95 | delta p50 | delta p95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| new_group | bare | 6 | 608.758 | 701.793 | +35.269 | +15.408 |
|  | off | 6 | 573.489 | 686.385 | - | - |
|  | on-local | 6 | 561.386 | 1403.607 | -12.104 | +717.222 |
|  | on-persist | 6 | 665.792 | 1005.077 | +92.302 | +318.692 |
| new_group actions | container={'create': 24} resident={'new': 24} | | | | | |
| resume_stopped | bare | 6 | 739.433 | 2585.419 | +97.657 | -857.543 |
|  | off | 6 | 641.776 | 3442.963 | - | - |
|  | on-local | 6 | 564.831 | 667.339 | -76.944 | -2775.624 |
|  | on-persist | 6 | 565.942 | 702.901 | -75.833 | -2740.062 |
| resume_stopped actions | container={'start': 24} resident={'new': 24} | | | | | |
| resume_running | bare | 6 | 183.123 | 191.567 | +1.016 | +4.605 |
|  | off | 6 | 182.107 | 186.962 | - | - |
|  | on-local | 6 | 186.691 | 191.708 | +4.584 | +4.746 |
|  | on-persist | 6 | 182.631 | 184.637 | +0.524 | -2.325 |
| resume_running actions | container={'reuse': 24} resident={'resume': 24} | | | | | |
| reuse_live | bare | 6 | 169.646 | 174.419 | -6.810 | -19.882 |
|  | off | 6 | 176.456 | 194.301 | - | - |
|  | on-local | 6 | 167.349 | 202.349 | -9.107 | +8.048 |
|  | on-persist | 6 | 173.946 | 222.301 | -2.511 | +28.000 |
| reuse_live actions | container={'reuse': 24} resident={'reuse': 24} | | | | | |

Note: p95 over 6 samples is the largest sample under the nearest-rank rule, not an estimate of a 95th percentile: read the p50 here, and raise --startup-iterations if a tail estimate is what is wanted. The deltas on new_group and resume_stopped are dominated by Docker create/start, which is hundreds of milliseconds against a collector cost of a fraction of one.
- on-local: 24 sink records, 45619 bytes
- on-persist: 24 sink records, 45672 bytes, delivery {'rows_visible_before_cohort_end': 23, 'delivered': 24, 'dropped': 0, 'drain_timeouts': 0, 'expected': 24}, postgres rows 24 (48702 record bytes)

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
| bare | 4 | 12.240 | — | n/a | 175.3 |
| off | 4 | 8.290 | — | n/a | 177.5 |
| on-local | 4 | 12.178 | +3.888 | +952.9 | 182.9 |
| on-persist | 4 | 11.520 | +3.230 | +791.6 | 172.4 |

One number per cohort, and it is the whole process: `time.process_time()` sums every thread, so the collector's exporter and the writer's tasks cannot be charged separately from the request path here. The delta against `off` is the configuration's extra process CPU on the same request set; because the client and server share this process, part of it is the load generator's own extra work. The per-record figure divides by the whole window's request count (warmup plus timed), which is what the CPU was spent on. `bare` is not comparable: it sends two extra sibling requests per round.

Not measured: network bytes (no portable per-socket counter for loopback HTTP); per-thread CPU (`resource.RUSAGE_THREAD` is unavailable here); WAL/index/disk bytes (only payload bytes are reported); container CPU and memory (no container is involved in a server-request cohort).

## Synthetic arms

### slow_sink

Synthetic: the bench-local on-local destination, delayed 20.0 ms per record, and left to accumulate across operations so consecutive bursts stack in the bounded queue. This is a stand-in for a slow control-plane-side destination, not the control plane's ingest endpoint: a route-rooted record is persisted in-process, so there is no HTTP ingest hop in this path to delay. The request path, the collector and the writer queue are untouched

| operation | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| files.list.root | 150 | 4.418 | 25.466 | 66.177 |
| files.list.subdir | 150 | 4.071 | 11.138 | 66.582 |
| files.open | 150 | 3.878 | 12.349 | 35.231 |
| terminal.list | 150 | 4.356 | 9.975 | 24.810 |
| terminal.create | 150 | 5.062 | 18.360 | 37.539 |
| terminal.stream_attach | 150 | 4.538 | 14.846 | 42.872 |

- writer counts: `{'stored': 733, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 287, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'stored': 733, 'queue_count': 287}}`
- local destination: 733/1020 records written, 2005327 bytes

### slow_db

Synthetic: PostgreSQL, by an ACCESS EXCLUSIVE lock this script held on agent_operation_measurements from a second connection

| operation | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| files.list.root | 60 | 4.112 | 10.708 | 13.037 |
| files.list.subdir | 60 | 3.520 | 6.625 | 11.504 |
| files.open | 60 | 4.022 | 8.345 | 19.754 |
| terminal.list | 60 | 4.893 | 31.548 | 50.135 |
| terminal.create | 60 | 4.352 | 16.976 | 81.308 |
| terminal.stream_attach | 60 | 4.297 | 11.347 | 14.935 |

- requests answered while the table was locked: 360
- writer during the lock: `{'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104}}`
- writer at the end of the hold: `{'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2}}`
- after release: `{'stored': 254, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2, 'stored': 254}}`, recovered after 0.274s (budget 22.0s)
- Postgres rows for the arm: 254 (412405 payload bytes)
