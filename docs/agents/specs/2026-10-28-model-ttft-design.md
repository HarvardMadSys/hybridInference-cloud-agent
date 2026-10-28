# Model TTFT from the gateway request log

Status: implemented with this change.

## Problem

The chat's conversation bar and the admin run page labelled one interval
"TTFT": the turn's first attempt starting to its first completed `message`,
`thinking` or `tool_use` event. That interval contains sandbox, workspace and
Agent CLI startup, and each of those events is stored once its whole block is
complete. One verified run showed "TTFT 96s" while its first model request
left 85 s after the attempt started and the gateway measured a 10.4 s time to
first token for it.

## Metric

Model TTFT is the gateway's per-request `ttft_ms`: the time from the gateway
receiving a model request to the first streamed chunk it relays back. The
gateway records it only for streamed requests and leaves it null otherwise.

- A request is a sample when `status_code` is present and below 400 (the
  filter the gateway's own latency rollups use) and `ttft_ms` is positive.
  Non-streamed, failed and status-less requests, rows without the field, and
  zero are unmeasured.
- The summary is the request-weighted arithmetic mean of the samples. Every
  sampled request weighs one; a request id read twice counts once.
- With no sample the mean is `null`. No event-derived interval is ever
  substituted.

The event-derived interval keeps its value under its own name,
`first_output_seconds`, on the thread stats and the admin session statistics.
The admin page shows it as "Avg time to first output", described as including
startup; the chat bar does not show it.

## Reading the requests

`cloud_agent.model_ttft.read_model_ttft` reads the same stored ledger
references the admin request pages read, through the same
`llm_request_source_view` decision: grant, fixed `[window_since,
window_until)`, gateway binding and `external_job_id = thread:{thread_id}`.
Aborted attempts never started an agent and are skipped. A reference that is
unattributed, from another gateway installation, or without a usable window is
a gap, never queried.

| Surface | Route | Scope |
| --- | --- | --- |
| Chat conversation bar | `GET /v1/agent/jobs/{job_id}/thread/model-ttft` (owner) | The thread's 8 newest started executions |
| Admin run page | `GET /admin/agent/runs/{job_id}/model-ttft` (admin) | The run's first 10 executions |

Each execution costs one gateway page of at most 100 rows; no continuation is
followed. Reads share the gateway client's request-page slots, run at most four
at a time per summary, and stop at a 10 s deadline, after which the summary is
unavailable rather than built from whatever arrived. The chat bar fetches the
summary when the conversation changes state, never on its two-second totals
poll, and `/thread/stats` stays a database-only read.

## Coverage

`complete` is true only when every started execution in scope was attributed,
read, and fit in one page. Otherwise `reason` names the first gap
(`sources_truncated`, `request_page_truncated`, `gateway_unavailable`,
`request_details_unsupported`, `binding_changed`, `attribution_unavailable`,
`invalid_window`), and both surfaces mark the value "(partial)" with the reason
in its tooltip. `samples`, `requests_read`, `sources_read` and `sources_total`
say what the mean is over.

`status` is `available` with at least one sample, `not_recorded` when the
requests were read but none measured a first token (or nothing started), and
`unavailable` when no reference could be read.

## Limits

- Gateway request logs are written asynchronously. A summary read just as a
  turn ends can miss its last requests until the next state change.
- The gateway's TTFT starts when the gateway receives the request. Network time
  between the sandbox and the gateway and the CLI's own work before sending are
  outside it, as is everything before the first request.
- The thread scope is the newest executions only; older ones are reported as
  a partial sample, not read without bound.

No migration, gateway contract change or background loop is involved.
