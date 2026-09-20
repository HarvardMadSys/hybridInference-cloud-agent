<!-- run B: `scripts/bench_operation_overhead.py --startup --json runB.json --markdown runB.md`
     executed 2026-09-20 on branch codex/perf/startup-measurement-switch at d5d99b3 + the review fixes;
     this file is the script's own --markdown output, unedited. -->

# Operation-profiling business overhead

- python 3.13.9 on macOS-15.7.7-arm64-arm-64bit-Mach-O, cpus 14
- postgres 15.17 (Homebrew), docker used, iterations 150 (warmup 20)
- percentiles: nearest-rank; budget: max(10 ms, off_p95 x 1%); baseline: `off`; rounds: 4

| operation | cohort | n | p50 | p95 | mean | min | max | delta p95 | delta p95 % | budget | verdict |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| files.list.root | bare | 600 | 2.942 | 7.590 | 3.654 | 2.075 | 25.888 | - | - | - | - |
|  | off | 600 | 3.098 | 6.999 | 3.739 | 2.162 | 32.636 | - | - | - | - |
|  | on-local | 600 | 3.347 | 9.005 | 4.595 | 2.429 | 58.353 | +2.006 | +28.66 | 10.00 | pass |
|  | on-persist | 600 | 3.749 | 9.276 | 4.782 | 2.458 | 41.687 | +2.277 | +32.54 | 10.00 | pass |
| files.list.subdir | bare | 600 | 3.035 | 7.322 | 3.761 | 2.176 | 32.420 | - | - | - | - |
|  | off | 600 | 3.417 | 8.539 | 4.365 | 2.296 | 44.659 | - | - | - | - |
|  | on-local | 600 | 3.746 | 15.776 | 5.819 | 2.639 | 55.817 | +7.236 | +84.74 | 10.00 | pass |
|  | on-persist | 600 | 3.750 | 8.757 | 4.696 | 2.696 | 53.921 | +0.218 | +2.55 | 10.00 | pass |
| files.open | bare | 600 | 2.891 | 6.094 | 3.402 | 2.222 | 33.192 | - | - | - | - |
|  | off | 600 | 3.478 | 7.338 | 4.113 | 2.390 | 58.099 | - | - | - | - |
|  | on-local | 600 | 3.884 | 11.530 | 5.538 | 2.717 | 74.785 | +4.193 | +57.14 | 10.00 | pass |
|  | on-persist | 600 | 4.192 | 12.112 | 5.965 | 2.758 | 276.041 | +4.774 | +65.07 | 10.00 | pass |
| terminal.list | bare | 600 | 3.561 | 7.099 | 4.156 | 2.443 | 49.241 | - | - | - | - |
|  | off | 600 | 3.510 | 8.237 | 4.387 | 2.645 | 40.143 | - | - | - | - |
|  | on-local | 600 | 4.129 | 11.605 | 5.519 | 2.725 | 102.606 | +3.368 | +40.89 | 10.00 | pass |
|  | on-persist | 600 | 4.349 | 13.128 | 5.851 | 2.813 | 92.355 | +4.891 | +59.38 | 10.00 | pass |
| terminal.create | bare | 600 | 3.489 | 7.275 | 4.150 | 2.561 | 21.589 | - | - | - | - |
|  | off | 600 | 3.965 | 10.471 | 5.180 | 2.520 | 49.701 | - | - | - | - |
|  | on-local | 600 | 4.146 | 13.359 | 5.985 | 2.817 | 105.495 | +2.888 | +27.58 | 10.00 | pass |
|  | on-persist | 600 | 4.303 | 7.716 | 4.935 | 3.015 | 33.801 | -2.756 | -26.32 | 10.00 | pass |
| terminal.stream_attach | bare | 600 | 3.012 | 5.712 | 3.503 | 2.305 | 27.419 | - | - | - | - |
|  | off | 600 | 3.932 | 10.614 | 5.135 | 2.511 | 52.284 | - | - | - | - |
|  | on-local | 600 | 4.267 | 20.359 | 7.048 | 3.018 | 86.460 | +9.745 | +91.81 | 10.00 | pass |
|  | on-persist | 600 | 3.931 | 8.195 | 4.643 | 2.735 | 30.787 | -2.420 | -22.80 | 10.00 | pass |

| operation | noise floor abs delta p50 | noise floor abs delta p95 |
| --- | ---: | ---: |
| files.list.root | 0.250 | 2.149 |
| files.list.subdir | 0.183 | 1.684 |
| files.open | 0.479 | 0.803 |
| terminal.list | 0.594 | 4.018 |
| terminal.create | 0.044 | 3.616 |
| terminal.stream_attach | 0.823 | 10.202 |

| operation | off p50 per round | on-local p50 per round | on-persist p50 per round |
| --- | --- | --- | --- |
| files.list.root | 2.94 3.22 3.39 2.65 | 2.93 3.92 3.30 3.29 | 3.41 3.92 3.62 3.83 |
| files.list.subdir | 3.02 4.22 3.72 2.79 | 4.20 3.53 3.61 4.00 | 3.49 3.91 3.57 3.98 |
| files.open | 2.81 3.72 3.99 3.44 | 3.71 4.00 3.81 4.04 | 4.25 4.03 3.75 4.84 |
| terminal.list | 3.23 4.40 3.35 3.38 | 4.37 4.11 3.81 4.61 | 3.85 4.22 4.27 4.96 |
| terminal.create | 3.71 4.44 4.10 3.67 | 4.48 3.64 4.12 4.81 | 4.01 4.46 4.32 4.38 |
| terminal.stream_attach | 3.22 4.07 3.68 4.72 | 3.96 5.99 3.77 4.23 | 3.99 3.62 3.94 4.02 |

| family | bare analogue route | n | p50 | p95 |
| --- | --- | ---: | ---: | ---: |
| files | PUT /files (write route -> broker.write_file) | 600 | 3.470 | 7.074 |
| terminals | POST /terminals/{id}/resize (-> broker.resize_terminal) | 600 | 3.552 | 8.019 |
| startup path | cohort | n | p50 | p95 | delta p50 | delta p95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| new_group | bare | 6 | 501.245 | 508.443 | -4.930 | -9.921 |
|  | off | 6 | 506.175 | 518.364 | - | - |
|  | on-local | 6 | 549.091 | 654.694 | +42.916 | +136.330 |
|  | on-persist | 6 | 580.227 | 770.357 | +74.053 | +251.993 |
| new_group actions | container={'create': 24} resident={'new': 24} | | | | | |
| resume_stopped | bare | 6 | 536.883 | 593.665 | +11.328 | +6.021 |
|  | off | 6 | 525.556 | 587.644 | - | - |
|  | on-local | 6 | 560.660 | 583.088 | +35.104 | -4.556 |
|  | on-persist | 6 | 705.577 | 850.258 | +180.021 | +262.614 |
| resume_stopped actions | container={'start': 24} resident={'new': 24} | | | | | |
| resume_running | bare | 6 | 171.687 | 174.761 | -12.941 | -61.574 |
|  | off | 6 | 184.629 | 236.335 | - | - |
|  | on-local | 6 | 187.370 | 310.209 | +2.742 | +73.874 |
|  | on-persist | 6 | 193.087 | 196.203 | +8.458 | -40.132 |
| resume_running actions | container={'reuse': 24} resident={'resume': 24} | | | | | |
| reuse_live | bare | 6 | 159.549 | 175.716 | -8.646 | +0.700 |
|  | off | 6 | 168.195 | 175.017 | - | - |
|  | on-local | 6 | 174.005 | 184.919 | +5.810 | +9.902 |
|  | on-persist | 6 | 180.771 | 296.003 | +12.576 | +120.987 |
| reuse_live actions | container={'reuse': 24} resident={'reuse': 24} | | | | | |

Note: p95 over 6 samples is the largest sample under the nearest-rank rule, not an estimate of a 95th percentile: read the p50 here, and raise --startup-iterations if a tail estimate is what is wanted. The deltas on new_group and resume_stopped are dominated by Docker create/start, which is hundreds of milliseconds against a collector cost of a fraction of one.
- on-local: 24 sink records, 45646 bytes
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
| bare | 4 | 11.714 | — | n/a | 159.3 |
| off | 4 | 9.641 | — | n/a | 159.3 |
| on-local | 4 | 13.466 | +3.825 | +1062.6 | 159.3 |
| on-persist | 4 | 13.270 | +3.630 | +1008.3 | 159.3 |

One number per cohort, and it is the whole process: `time.process_time()` sums every thread, so the collector's exporter and the writer's tasks cannot be charged separately from the request path here. The delta against `off` is the configuration's extra process CPU on the same request set; because the client and server share this process, part of it is the load generator's own extra work. `bare` is not comparable: it sends two extra sibling requests per round.

Not measured: network bytes (no portable per-socket counter for loopback HTTP); per-thread CPU (`resource.RUSAGE_THREAD` is unavailable here); WAL/index/disk bytes (only payload bytes are reported); container CPU and memory (no container is involved in a server-request cohort).

## Synthetic arms

### slow_sink

Synthetic: the bench-local on-local destination, delayed 20.0 ms per record, and left to accumulate across operations so consecutive bursts stack in the bounded queue. This is a stand-in for a slow control-plane-side destination, not the control plane's ingest endpoint: a route-rooted record is persisted in-process, so there is no HTTP ingest hop in this path to delay. The request path, the collector and the writer queue are untouched

| operation | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| files.list.root | 150 | 3.769 | 7.307 | 20.904 |
| files.list.subdir | 150 | 4.236 | 12.951 | 57.946 |
| files.open | 150 | 3.783 | 8.544 | 46.597 |
| terminal.list | 150 | 4.487 | 11.454 | 65.998 |
| terminal.create | 150 | 4.280 | 8.891 | 51.184 |
| terminal.stream_attach | 150 | 3.848 | 9.687 | 55.463 |

- writer counts: `{'stored': 739, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 281, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'stored': 739, 'queue_count': 281}}`
- local destination: 739/1020 records written, 2023715 bytes

### slow_db

Synthetic: PostgreSQL, by an ACCESS EXCLUSIVE lock this script held on agent_operation_measurements from a second connection

| operation | n | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| files.list.root | 60 | 3.108 | 7.941 | 18.241 |
| files.list.subdir | 60 | 3.746 | 10.293 | 70.141 |
| files.open | 60 | 4.408 | 9.494 | 60.614 |
| terminal.list | 60 | 3.305 | 6.060 | 50.074 |
| terminal.create | 60 | 3.425 | 7.432 | 10.738 |
| terminal.stream_attach | 60 | 3.241 | 4.078 | 7.632 |

- requests answered while the table was locked: 360
- writer during the lock: `{'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104}}`
- writer at the end of the hold: `{'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2}}`
- after release: `{'stored': 254, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2, 'stored': 254}}`, recovered after 0.327s (budget 22.0s)
- Postgres rows for the arm: 254 (412658 payload bytes)
