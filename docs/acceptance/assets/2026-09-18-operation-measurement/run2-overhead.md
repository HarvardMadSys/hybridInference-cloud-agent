# Operation-profiling business overhead

- python 3.13.9 on macOS-15.7.7-arm64-arm-64bit-Mach-O, cpus 14
- postgres 15.17 (Homebrew), docker used, iterations 150 (warmup 20)
- percentiles: nearest-rank; budget: max(10 ms, off_p95 x 1%); baseline: `off`; rounds: 4

| operation | cohort | n | p50 | p95 | mean | min | max | delta p95 | delta p95 % | budget | verdict |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| files.list.root | bare | 600 | 2.888 | 5.015 | 3.286 | 2.196 | 20.875 | - | - | - | - |
|  | off | 600 | 2.804 | 5.486 | 3.266 | 2.289 | 23.250 | - | - | - | - |
|  | on-local | 600 | 3.375 | 9.133 | 4.428 | 2.628 | 50.435 | +3.647 | +66.47 | 10.00 | pass |
|  | on-persist | 600 | 3.265 | 5.370 | 3.639 | 2.443 | 17.094 | -0.117 | -2.13 | 10.00 | pass |
| files.list.subdir | bare | 600 | 2.767 | 4.505 | 3.145 | 2.258 | 23.022 | - | - | - | - |
|  | off | 600 | 2.868 | 4.453 | 3.164 | 2.322 | 17.921 | - | - | - | - |
|  | on-local | 600 | 3.265 | 7.630 | 4.011 | 2.399 | 44.432 | +3.177 | +71.34 | 10.00 | pass |
|  | on-persist | 600 | 3.124 | 4.870 | 3.505 | 2.454 | 55.273 | +0.417 | +9.37 | 10.00 | pass |
| files.open | bare | 600 | 2.963 | 6.284 | 3.580 | 2.240 | 36.592 | - | - | - | - |
|  | off | 600 | 2.830 | 6.214 | 3.424 | 2.293 | 31.581 | - | - | - | - |
|  | on-local | 600 | 3.079 | 6.070 | 3.477 | 2.612 | 12.394 | -0.144 | -2.32 | 10.00 | pass |
|  | on-persist | 600 | 3.160 | 5.409 | 3.583 | 2.541 | 20.454 | -0.805 | -12.95 | 10.00 | pass |
| terminal.list | bare | 600 | 3.216 | 6.177 | 3.810 | 2.483 | 27.537 | - | - | - | - |
|  | off | 600 | 3.283 | 6.260 | 3.817 | 2.426 | 30.375 | - | - | - | - |
|  | on-local | 600 | 3.462 | 5.218 | 3.818 | 2.764 | 27.527 | -1.042 | -16.64 | 10.00 | pass |
|  | on-persist | 600 | 3.408 | 7.175 | 4.126 | 2.595 | 31.633 | +0.915 | +14.61 | 10.00 | pass |
| terminal.create | bare | 600 | 3.440 | 7.104 | 4.145 | 2.338 | 44.339 | - | - | - | - |
|  | off | 600 | 3.172 | 5.140 | 3.528 | 2.353 | 38.422 | - | - | - | - |
|  | on-local | 600 | 3.449 | 5.117 | 3.773 | 2.486 | 23.519 | -0.023 | -0.44 | 10.00 | pass |
|  | on-persist | 600 | 3.494 | 6.370 | 4.244 | 2.790 | 98.280 | +1.231 | +23.94 | 10.00 | pass |
| terminal.stream_attach | bare | 600 | 3.072 | 5.662 | 3.638 | 2.232 | 59.051 | - | - | - | - |
|  | off | 600 | 2.891 | 7.364 | 3.683 | 2.124 | 40.001 | - | - | - | - |
|  | on-local | 600 | 3.254 | 5.904 | 3.733 | 2.465 | 23.415 | -1.460 | -19.82 | 10.00 | pass |
|  | on-persist | 600 | 3.272 | 5.027 | 3.624 | 2.515 | 23.106 | -2.337 | -31.74 | 10.00 | pass |

| operation | noise floor abs delta p50 | noise floor abs delta p95 |
| --- | ---: | ---: |
| files.list.root | 0.182 | 1.826 |
| files.list.subdir | 0.196 | 0.604 |
| files.open | 0.015 | 1.778 |
| terminal.list | 0.141 | 0.066 |
| terminal.create | 0.027 | 0.637 |
| terminal.stream_attach | 0.404 | 1.257 |

| operation | off p50 per round | on-local p50 per round | on-persist p50 per round |
| --- | --- | --- | --- |
| files.list.root | 2.75 2.66 2.74 3.20 | 3.07 3.01 4.74 3.58 | 3.17 3.30 3.09 3.55 |
| files.list.subdir | 2.77 3.01 2.83 2.94 | 3.11 3.18 3.35 3.78 | 3.30 3.00 2.96 3.27 |
| files.open | 2.79 2.76 2.86 2.92 | 2.96 2.93 3.12 3.77 | 3.06 3.10 3.42 3.14 |
| terminal.list | 3.51 3.00 3.09 3.39 | 3.49 3.29 3.38 3.60 | 3.33 3.30 3.62 3.60 |
| terminal.create | 3.39 3.05 2.89 3.25 | 3.47 3.18 3.44 3.64 | 3.55 3.31 4.07 3.44 |
| terminal.stream_attach | 3.65 2.57 2.75 2.95 | 3.22 3.03 3.36 3.62 | 3.24 3.21 3.71 3.21 |

| family | bare analogue route | n | p50 | p95 |
| --- | --- | ---: | ---: | ---: |
| files | PUT /files (write route -> broker.write_file) | 600 | 3.253 | 5.585 |
| terminals | POST /terminals/{id}/resize (-> broker.resize_terminal) | 600 | 3.319 | 6.098 |
| startup path | cohort | n | p50 | p95 | delta p50 | delta p95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| new_group | bare | 6 | 538.890 | 583.883 | +3.638 | +13.276 |
|  | off | 6 | 535.252 | 570.606 | - | - |
|  | on-local | 6 | 554.710 | 628.885 | +19.457 | +58.278 |
|  | on-persist | 6 | 516.359 | 521.154 | -18.893 | -49.452 |
| new_group actions | container={'create': 24} resident={'new': 24} | | | | | |
| resume_stopped | bare | 6 | 548.743 | 1312.761 | -4.387 | +651.584 |
|  | off | 6 | 553.130 | 661.178 | - | - |
|  | on-local | 6 | 539.858 | 717.022 | -13.271 | +55.844 |
|  | on-persist | 6 | 525.444 | 672.202 | -27.686 | +11.024 |
| resume_stopped actions | container={'start': 24} resident={'new': 24} | | | | | |
| resume_running | bare | 6 | 181.679 | 183.449 | +4.649 | +4.238 |
|  | off | 6 | 177.030 | 179.211 | - | - |
|  | on-local | 6 | 180.157 | 274.530 | +3.127 | +95.319 |
|  | on-persist | 6 | 179.488 | 188.736 | +2.458 | +9.525 |
| resume_running actions | container={'reuse': 24} resident={'resume': 24} | | | | | |
| reuse_live | bare | 6 | 163.769 | 173.443 | -1.977 | -54.765 |
|  | off | 6 | 165.745 | 228.207 | - | - |
|  | on-local | 6 | 171.087 | 198.474 | +5.342 | -29.733 |
|  | on-persist | 6 | 168.933 | 178.244 | +3.187 | -49.963 |
| reuse_live actions | container={'reuse': 24} resident={'reuse': 24} | | | | | |

Note: p95 over 6 samples is the largest sample under the nearest-rank rule, not an estimate of a 95th percentile: read the p50 here, and raise --startup-iterations if a tail estimate is what is wanted. The deltas on new_group and resume_stopped are dominated by Docker create/start, which is hundreds of milliseconds against a collector cost of a fraction of one.
- on-local: 24 sink records, 36437 bytes
- on-persist: 24 sink records, 36480 bytes, delivery {'delivered': 24, 'duplicate': 0, 'dropped': 0, 'retried': 0}, postgres rows 24 (38520 record bytes)

## Drops and destinations

| cohort | sink written/expected | writer stored | dropped_full | dropped_timeout | rejected | failed | destination |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| bare | 0/0 | 0 | 0 | 0 | 0 | 0 | none (profiler off) |
| off | 0/0 | 0 | 0 | 0 | 0 | 0 | none (profiler off) |
| on-local | 4080/4080 | 4080 | 0 | 0 | 0 | 0 | local JSONL (bench-local capability) |
| on-persist | 4080/4080 | 4080 | 0 | 0 | 0 | 0 | Postgres agent_operation_measurements |

## Background cost

| cohort | rounds | CPU s | request-window CPU s | other threads CPU s | peak RSS MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| bare | 4 | 11.219 | 9.847 | 1.372 | 159.9 |
| off | 4 | 8.331 | 7.286 | 1.045 | 160.7 |
| on-local | 4 | 10.755 | 9.328 | 1.427 | 163.6 |
| on-persist | 4 | 11.319 | 9.716 | 1.604 | 159.2 |

Not measured: network bytes (no portable per-socket counter for loopback HTTP); per-thread CPU (`resource.RUSAGE_THREAD` is unavailable here); WAL/index/disk bytes (only payload bytes are reported); container CPU and memory (no container is involved in a server-request cohort).

## Synthetic arms

### slow_sink

Synthetic: the bench-local on-local destination, delayed 20.0 ms per record, and left to accumulate across operations so consecutive bursts stack in the bounded queue. This is a stand-in for a slow control-plane-side destination, not the control plane's ingest endpoint: a route-rooted record is persisted in-process, so there is no HTTP ingest hop in this path to delay. The request path, the collector and the writer queue are untouched

| operation | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| files.list.root | 150 | 2.987 | 4.149 | 13.326 |
| files.list.subdir | 150 | 3.199 | 5.040 | 14.756 |
| files.open | 150 | 2.996 | 6.127 | 12.592 |
| terminal.list | 150 | 3.174 | 4.437 | 11.884 |
| terminal.create | 150 | 3.276 | 4.943 | 24.398 |
| terminal.stream_attach | 150 | 2.945 | 4.536 | 7.847 |

- writer counts: `{'stored': 592, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 428, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'stored': 592, 'queue_count': 428}}`
- local destination: 592/1020 records written, 1618832 bytes

### slow_db

Synthetic: PostgreSQL, by an ACCESS EXCLUSIVE lock this script held on agent_operation_measurements from a second connection

| operation | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| files.list.root | 60 | 2.772 | 8.090 | 11.204 |
| files.list.subdir | 60 | 2.657 | 3.270 | 3.571 |
| files.open | 60 | 2.824 | 4.122 | 5.944 |
| terminal.list | 60 | 3.083 | 4.722 | 24.112 |
| terminal.create | 60 | 3.105 | 4.466 | 6.113 |
| terminal.stream_attach | 60 | 2.987 | 6.100 | 7.922 |

- requests answered while the table was locked: 360
- writer during the lock: `{'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104}}`
- writer at the end of the hold: `{'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2}}`
- after release: `{'stored': 254, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2, 'stored': 254}}`, recovered after 1.005s (budget 22.0s)
- Postgres rows for the arm: 254 (412554 payload bytes)
