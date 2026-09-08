// Adapts the agent-jobs API shape to what the UI renders.
//
// The UI was designed against a richer mock than the API currently returns.
// This module makes that gap explicit rather than papering over it: fields the
// backend genuinely provides are mapped, and fields it does not are left empty
// so the UI renders an honest blank instead of an invented value.

import type {
  AgentJobApi,
  AgentJobEventApi,
  AgentAttemptTelemetryApi,
  AgentAttemptSummaryApi,
  AttemptResourcesApi,
  AgentThreadApi,
  AgentThreadEventApi,
  AgentThreadMessageApi,
  ToolCallResourcesApi,
  ProcessObservationApi,
} from '@/lib/api/agents';

import type {
  AgentDiffFile,
  AgentEvent,
  AgentHistoryTurn,
  AgentAttempt,
  AgentJob,
  AgentJobState,
  DiffLine,
  ToolCallResources,
  ProcessObservation,
  TurnResources,
} from './types';

/**
 * Map a server state onto a display state.
 *
 * `succeeded` splits in two, because those are different things to a user: a
 * job whose patch is already a draft PR is done, while one that finished
 * without a PR is waiting on the publish step (or produced no changes) and
 * still wants a human.
 */
export function toDisplayState(job: AgentJobApi): AgentJobState {
  if (job.state === 'waiting' || job.state === 'queued') return 'queued';
  if (job.state === 'running' || job.state === 'publishing') return 'running';
  // `succeeded` splits in two because those are different things to a user: a
  // job whose patch is already a draft PR is done, while one that finished
  // without a PR is waiting on the publish step (or produced no changes).
  if (job.state === 'succeeded') return job.published_pr_url ? 'done' : 'needs_review';
  if (job.state === 'failed') return 'failed';
  if (job.state === 'cancelled') return 'cancelled';
  return 'queued';
}

function asText(payload: Record<string, unknown> | null, ...keys: string[]): string {
  for (const key of keys) {
    const value = payload?.[key];
    if (typeof value === 'string' && value) return value;
  }
  return '';
}

function readableValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function commandText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const input = value as Record<string, unknown>;
    const command = input.command ?? input.cmd;
    if (typeof command === 'string') return command;
    if (Array.isArray(command) && command.every((part) => typeof part === 'string')) {
      return command.join(' ');
    }
  }
  return readableValue(value);
}

function toolOutput(payload: Record<string, unknown>): string[] | undefined {
  const output =
    readableValue(payload.output) ||
    readableValue(payload.aggregated_output) ||
    readableValue(payload.stdout);
  return output ? output.split('\n') : undefined;
}

/** Map one stored event onto a UI row, or null when it has nothing to show. */
/**
 * The span between a tool call and its result, when both carry a time.
 *
 * Message-level by construction: the window opens when the model asked for
 * the call rather than when the sandbox began running it, so it includes
 * whatever thinking preceded execution. Returns an empty object rather than
 * a null field so a row without usable times carries no duration at all.
 */
export function durationBetween(
  startedAt: string | null | undefined,
  endedAt: string | null | undefined,
): { durationMs?: number } {
  if (!startedAt || !endedAt) return {};
  const start = Date.parse(startedAt);
  const end = Date.parse(endedAt);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return {};
  return { durationMs: end - start };
}

export function toDisplayEvent(event: AgentJobEventApi): AgentEvent | null {
  const payload = event.payload ?? {};
  const type = event.event_type;

  // Never carry chain-of-thought into the display model. The UI represents
  // this as a compact reasoning status, not as model-authored hidden text.
  if (type === 'thinking') return { kind: 'thinking', text: '' };
  if (type === 'message') return { kind: 'message', text: asText(payload, 'text') };
  if (type === 'usage') return { kind: 'usage', text: asText(payload, 'text') };

  if (type === 'tool_use') {
    const name = asText(payload, 'name', 'tool') || 'Bash';
    const exitCode = payload.exit_code;
    const output = toolOutput(payload);
    const outputIsError =
      (typeof exitCode === 'number' && exitCode !== 0) || payload.status === 'failed';
    return {
      kind: 'tool_use',
      tool: name,
      id: asText(payload, 'id') || undefined,
      ...(event.created_at ? { at: event.created_at } : {}),
      detail: asText(payload, 'detail', 'text') || commandText(payload.input) || 'Running tool',
      ...(output ? { output } : {}),
      ...(outputIsError ? { outputIsError: true } : {}),
    };
  }

  if (type === 'tool_result') {
    return {
      kind: 'tool_result',
      text:
        readableValue(payload.content) ||
        readableValue(payload.output) ||
        readableValue(payload.aggregated_output) ||
        asText(payload, 'text', 'detail'),
      toolUseId: asText(payload, 'tool_use_id') || undefined,
      isError: payload.is_error === true,
      ...(event.created_at ? { at: event.created_at } : {}),
    };
  }

  if (type === 'error') {
    // Egress denials arrive as an error subtype but are a first-class row:
    // "the sandbox tried to reach X" is a different story from "it failed".
    const host = asText(payload, 'host');
    if (host) {
      const attempts = typeof payload.attempts === 'number' ? payload.attempts : 1;
      return { kind: 'egress_denied', host, attempts };
    }
    return { kind: 'lifecycle', text: asText(payload, 'detail', 'text') || 'error' };
  }

  if (type === 'attempt_superseded') {
    const reason = asText(payload, 'reason') || 'lease expired';
    return { kind: 'lifecycle', text: `attempt superseded (${reason})` };
  }
  if (type === 'lifecycle') {
    const phase = asText(payload, 'phase', 'text') || 'lifecycle';
    // The runner reports whether it reused the tree; the milestone reads
    // differently depending on the answer, and only the runner can know it.
    if (phase === 'checked_out' && payload.reused === true) {
      return { kind: 'lifecycle', text: 'workspace_reused' };
    }
    return { kind: 'lifecycle', text: phase };
  }
  if (type === 'raw') {
    // Raw records are unclassified runtime protocol data. They remain available
    // in the diagnostic Raw events drawer, but are not terminal commands or output.
    return null;
  }

  // Diff is rendered from the retained workspace rather than a stream marker
  // that only says whether files changed.
  if (type === 'diff') return null;

  // An unrecognized kind still appears, rather than silently disappearing.
  return { kind: 'lifecycle', text: type };
}

/** Split a unified diff into the marker-tagged lines the diff view renders. */
export function toDiffLines(patch: string): DiffLine[] {
  if (!patch) return [];
  return patch
    .split('\n')
    .filter((line) => !line.startsWith('diff --git') && !line.startsWith('index '))
    .map((text): DiffLine => {
      if (text.startsWith('@@')) return { marker: 'hunk', text };
      if (text.startsWith('+++') || text.startsWith('---')) return { marker: 'ctx', text };
      if (text.startsWith('+')) return { marker: 'add', text };
      if (text.startsWith('-')) return { marker: 'del', text };
      return { marker: 'ctx', text };
    });
}

/** Extract the file list a patch touches, for the diff header. */
export function toDiffFiles(patch: string): string[] {
  const files = new Set<string>();
  for (const line of patch.split('\n')) {
    const match = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (match) files.add(match[2]);
  }
  return [...files];
}

/** Split a patch into independently selectable files with per-file stats. */
export function toDiffFileDetails(patch: string): AgentDiffFile[] {
  if (!patch) return [];
  const files: AgentDiffFile[] = [];
  let current: AgentDiffFile | null = null;

  for (const line of patch.split('\n')) {
    const header = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (header) {
      current = { path: header[2], add: 0, del: 0, lines: [] };
      files.push(current);
      continue;
    }
    if (!current || line.startsWith('index ')) continue;

    let marker: DiffLine['marker'] = 'ctx';
    if (line.startsWith('@@')) marker = 'hunk';
    else if (line.startsWith('+') && !line.startsWith('+++')) {
      marker = 'add';
      current.add += 1;
    } else if (line.startsWith('-') && !line.startsWith('---')) {
      marker = 'del';
      current.del += 1;
    }
    current.lines.push({ marker, text: line });
  }
  return files;
}

function diffStat(patch: string): { add: number; del: number } | undefined {
  if (!patch) return undefined;
  let add = 0;
  let del = 0;
  for (const line of patch.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) add += 1;
    else if (line.startsWith('-') && !line.startsWith('---')) del += 1;
  }
  return { add, del };
}

const TIER_LABELS: Record<string, string> = {
  platform_only: 'platform relays only',
  trusted: 'allowlist',
  custom: 'custom',
  full: 'open',
};

/** Render an egress tier for a reader who does not know the tier names. */
function tierLabel(tier: string | null): string {
  if (!tier) return '';
  return TIER_LABELS[tier] ?? tier;
}

/** Describe the trusted execution backend reported for the current attempt. */
function sandboxLabel(events: AgentJobEventApi[], currentAttemptId: number | null): string {
  const started = [...events]
    .reverse()
    .find(
      (event) =>
        event.event_type === 'lifecycle' &&
        event.payload?.phase === 'started' &&
        (currentAttemptId === null || event.attempt_id === currentAttemptId),
    );
  if (!started) return '';

  const backend = asText(started.payload, 'sandbox_backend');
  if (!backend) return '';
  const runtime = asText(started.payload, 'sandbox_runtime');
  const image = asText(started.payload, 'sandbox_image');
  const boundary = runtime ? `${backend} (${runtime})` : backend;
  return image ? `${boundary} · ${image}` : boundary;
}

/** Render a token count compactly, or empty when there is no ledger. */
function formatTokens(value: number | null): string {
  if (value === null || value === undefined) return '';
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

function rawEventLine(event: AgentJobEventApi): string {
  if (event.event_type !== 'thinking') return JSON.stringify(event);
  return JSON.stringify({
    ...event,
    payload: { ...(event.payload ?? {}), text: '[reasoning hidden]' },
  });
}

export interface AdaptOptions {
  events?: AgentJobEventApi[];
  threadEvents?: AgentThreadEventApi[];
  patch?: string | null;
  thread?: AgentThreadApi | null;
}

const LIFECYCLE_LABELS: Record<string, string> = {
  // Three phases used to share one label, so the platform starting a turn and
  // the agent announcing itself rendered as the same ticked step, twice. They
  // are different milestones and now say so — the back-to-back collapse below
  // only ever hid the pair when nothing came between them, which on a resident
  // turn it does.
  started: 'Run started',
  init: 'Agent ready',
  system: 'Agent ready',
  checked_out: 'Repository ready',
  // A resident turn skipped the clone: the tree is the one this conversation
  // has been building. Saying "repository ready" either way told the owner
  // their thread had been rebuilt when it had not.
  workspace_reused: 'Reusing this conversation\u2019s workspace',
  context_restored: 'Previous work restored',
  setup: 'Installing project dependencies',
  workspace_preparing: 'Preparing workspace',
  workspace_ready: 'Workspace ready',
  workspace_finalizing: 'Saving workspace changes',
  compact_boundary: 'Context compacted to keep going',
  result: 'Agent finished',
  publishing: 'Opening draft pull request',
  published: 'Draft pull request ready',
  cancelled_by_owner: 'Stopped by you',
};

/**
 * Render one lifecycle phase, and say whether it is a milestone.
 *
 * `milestone: false` is the important half. A phase this build has never heard
 * of used to be title-cased and given a ✓ like a completed step, so a progress
 * counter the CLI happened to emit (`thinking_tokens`) read as 408 finished
 * milestones. An unrecognised phase is a diagnostic: shown, because dropping it
 * hides a real change in what the runtime reports, but never dressed up as
 * something the job accomplished.
 */
export function lifecyclePhaseLabel(value: string): { text: string; milestone: boolean } {
  const phase = value.trim().toLowerCase();
  const known = LIFECYCLE_LABELS[phase];
  if (known) return { text: known, milestone: true };
  if (phase.includes('superseded')) {
    return { text: 'Run restarted after losing its worker', milestone: true };
  }
  return { text: value.replaceAll('_', ' '), milestone: false };
}

/** Preserve attempt boundaries and attach each result to its tool activity. */
function displayEvents(events: AgentJobEventApi[]): AgentEvent[] {
  const attemptIds = [...new Set(events.map((event) => event.attempt_id))].sort((a, b) => a - b);
  const attemptNoById = new Map(attemptIds.map((id, index) => [id, index + 1]));
  const rows: AgentEvent[] = [];
  const toolsById = new Map<string, number>();
  const lastToolByAttempt = new Map<number, number>();

  for (const event of events) {
    const mapped = toDisplayEvent(event);
    if (!mapped) continue;
    const attemptNo = attemptNoById.get(event.attempt_id);
    const row = { ...mapped, attemptNo } as AgentEvent;

    if (row.kind === 'tool_use') {
      const rowIndex = rows.push(row) - 1;
      if (row.id) toolsById.set(`${attemptNo}:${row.id}`, rowIndex);
      if (attemptNo !== undefined) lastToolByAttempt.set(attemptNo, rowIndex);
      continue;
    }

    if (row.kind === 'tool_result') {
      const explicit = row.toolUseId ? toolsById.get(`${attemptNo}:${row.toolUseId}`) : undefined;
      const targetIndex =
        explicit ?? (attemptNo === undefined ? undefined : lastToolByAttempt.get(attemptNo));
      const target = targetIndex === undefined ? undefined : rows[targetIndex];
      if (targetIndex !== undefined && target?.kind === 'tool_use' && !target.output) {
        rows[targetIndex] = {
          ...target,
          output: row.text ? row.text.split('\n') : ['Command completed with no output.'],
          outputIsError: row.isError,
          // Folding is where the pair still exists. Downstream only sees the
          // surviving tool_use row, so a consumer that wanted to time the
          // call — the turn footer does — would find nothing left to pair
          // with and render every call as unfinished. The window is resolved
          // here, while both ends are in hand.
          ...durationBetween(target.at, row.at),
        };
        continue;
      }
    }

    // Reasoning is a state, not a sequence of steps. A run emits one of these
    // per thinking block, and rendering each as its own row turns "the agent
    // thought about it" into a wall of identical lines. Consecutive ones
    // collapse into the single status row that was always intended.
    if (row.kind === 'thinking' && rows[rows.length - 1]?.kind === 'thinking') continue;

    // Same for a milestone repeated back to back: the platform's "started" and
    // the CLI's "init" both mean "setting up environment", so they rendered as
    // two identical ticked-off steps.
    const previous = rows[rows.length - 1];
    if (
      row.kind === 'lifecycle' &&
      previous?.kind === 'lifecycle' &&
      lifecyclePhaseLabel(previous.text).text === lifecyclePhaseLabel(row.text).text
    ) {
      continue;
    }

    rows.push(row);
  }

  return rows;
}

/** Rename the host's rollup into the UI's shape, keeping every null a null. */
export function toTurnResources(api: AttemptResourcesApi): TurnResources {
  const toolCallsTruncated = api.tool_calls_truncated === true;
  const toolCalls = (api.tool_calls ?? []).map((call) => {
    const adapted = toToolCallResources(call);
    if (!toolCallsTruncated) return adapted;
    // A dropped call may have started processes the retained rows now
    // claim; nothing per-call survives a truncated list, exit-accounted or
    // sampled alike.
    return {
      ...adapted,
      sampleCount: 0,
      measuredSpanMs: null,
      cpuUsec: null,
      memPeakBytes: null,
      exclusive: false,
      processCpuUsec: null,
      processMemPeakBytes: null,
      processIoReadBytes: null,
      processIoWriteBytes: null,
      processResourcesPartial: false,
    };
  });
  const processObserver = api.process_observer ?? null;
  const processEventsLost = api.process_events_lost ?? 0;
  const processesTruncated = api.processes_truncated === true;
  const processObservationComplete =
    processObserver !== null &&
    api.process_observation_complete === true &&
    processEventsLost === 0 &&
    !processesTruncated;
  // An incomplete census may have lost a process a call started, so no
  // call's exit-accounted numbers can be presented as its total. The host
  // marks this too; the reader is kept conservative even against an older
  // or wrong row.
  const conservativeCalls = toolCalls.map((call) => {
    if (processObservationComplete || (call.processCount ?? 0) === 0) return call;
    return { ...call, processResourcesPartial: true };
  });
  return {
    cpuUsec: api.cpu_usec,
    memPeakBytes: api.mem_peak_bytes,
    memAvgBytes: api.mem_avg_bytes,
    ioReadBytes: api.io_read_bytes,
    ioWriteBytes: api.io_write_bytes,
    netRxBytes: api.net_rx_bytes,
    netTxBytes: api.net_tx_bytes,
    sampleCount: api.sample_count,
    samplerVersion: api.sampler_version,
    coverage: api.coverage,
    toolTimeMs: toolCallsTruncated ? null : (api.tool_time_ms ?? null),
    toolCalls: conservativeCalls,
    toolCallsTruncated,
    processObserver,
    processObservationComplete,
    processEventsLost,
    processesTruncated,
    processes: (api.processes ?? []).map(toProcessObservation),
  };
}

/**
 * Collapse the exclusive/shared column pair into one number and a flag.
 *
 * The pair exists in the database so a shared measurement cannot be read as
 * an exclusive one by a query that does not know the difference. Here the
 * difference is carried by `exclusive`, and every renderer of these numbers
 * has to answer it — a value with no flag beside it is what this shape makes
 * impossible.
 */
function toToolCallResources(api: ToolCallResourcesApi): ToolCallResources {
  const exclusive = api.cpu_usec_exclusive !== null || api.mem_peak_exclusive_bytes !== null;
  return {
    toolUseId: api.tool_use_id,
    toolName: api.tool_name,
    timingSource: api.timing_source === 'exact' ? 'exact' : 'message',
    durationMs: api.duration_ms,
    sampleCount: api.sample_count,
    overlapN: api.overlap_n,
    measuredSpanMs: api.measured_span_ms,
    cpuUsec: exclusive ? api.cpu_usec_exclusive : api.cpu_usec_shared,
    memPeakBytes: exclusive ? api.mem_peak_exclusive_bytes : api.mem_peak_shared_bytes,
    exclusive,
    processCount: api.process_count ?? 0,
    processCpuUsec: api.process_cpu_usec ?? null,
    processMemPeakBytes: api.process_mem_peak_bytes ?? null,
    processIoReadBytes: api.process_io_read_bytes ?? null,
    processIoWriteBytes: api.process_io_write_bytes ?? null,
    processResourcesPartial: api.process_resources_partial === true,
  };
}

function toProcessObservation(api: ProcessObservationApi): ProcessObservation {
  return {
    processSeq: api.process_seq,
    uid: api.uid,
    pid: api.pid,
    tgid: api.tgid,
    ppid: api.ppid,
    comm: api.comm,
    executable: api.executable,
    startedOffsetMs: api.started_offset_ms,
    durationMs: api.duration_ms,
    exitCode: api.exit_code,
    observedFork: api.observed_fork,
    observedExec: api.observed_exec,
    observedExit: api.observed_exit,
    toolCallSeqs: api.tool_call_seqs,
    cpuUsec: api.cpu_usec ?? null,
    memPeakBytes: api.mem_peak_bytes ?? null,
    ioReadBytes: api.io_read_bytes ?? null,
    ioWriteBytes: api.io_write_bytes ?? null,
    ioRcharBytes: api.io_rchar_bytes ?? null,
    ioWcharBytes: api.io_wchar_bytes ?? null,
  };
}

export function toAttemptTelemetry(
  api: AgentAttemptSummaryApi & Partial<Pick<AgentAttemptTelemetryApi, 'resources'>>,
): NonNullable<AgentAttempt['telemetry']> {
  return {
    ...(api.continuity_mode ? { continuityMode: api.continuity_mode } : {}),
    ...(api.context_status ? { contextStatus: api.context_status } : {}),
    ...(typeof api.duration_ms === 'number' ? { durationMs: api.duration_ms } : {}),
    ...(api.resources ? { resources: toTurnResources(api.resources) } : {}),
  };
}

export function toDisplayJob(job: AgentJobApi, options: AdaptOptions = {}): AgentJob {
  const events = options.events ?? [];
  const patch = options.patch ?? '';
  const renderedEvents = displayEvents(events);

  // Attempts are inferred from the event log rather than fetched: an attempt
  // exists precisely because it wrote events, and a superseded control event
  // is what marks the takeover.
  const attemptIds = [...new Set(events.map((event) => event.attempt_id))].sort((a, b) => a - b);
  const currentAttemptIndex =
    job.current_attempt_id === null ? -1 : attemptIds.indexOf(job.current_attempt_id);
  const telemetryAttemptIndex =
    job.telemetry_attempt_id == null ? -1 : attemptIds.indexOf(job.telemetry_attempt_id);
  const supersededIds = new Set(
    events
      .filter((event) => event.event_type === 'attempt_superseded')
      .map((event) => event.attempt_id),
  );
  const telemetryByAttemptId = new Map(
    (job.attempt_telemetry ?? []).map((attempt) => [attempt.attempt_id, attempt]),
  );
  const attempts = attemptIds.map((id, index): AgentAttempt => {
    const recorded = telemetryByAttemptId.get(id);
    // Only legacy servers need the top-level fallback. Its identity belongs
    // to the fetched snapshot, not whichever attempt SSE has since started.
    let telemetry: AgentAttempt['telemetry'];
    if (recorded) {
      telemetry = toAttemptTelemetry(recorded);
    } else if (
      job.attempt_telemetry === undefined &&
      id ===
        (job.telemetry_attempt_id === undefined ? job.current_attempt_id : job.telemetry_attempt_id)
    ) {
      telemetry = {
        ...(job.continuity_mode ? { continuityMode: job.continuity_mode } : {}),
        ...(job.context_status ? { contextStatus: job.context_status } : {}),
        ...(typeof job.duration_ms === 'number' ? { durationMs: job.duration_ms } : {}),
        ...(job.resources ? { resources: toTurnResources(job.resources) } : {}),
      };
    }
    return {
      id,
      no: index + 1,
      status: supersededIds.has(id)
        ? 'superseded'
        : job.state === 'running' || job.state === 'publishing'
          ? 'live'
          : 'finished',
      note: supersededIds.has(id) ? 'lease expired; a new attempt took over' : undefined,
      ...(telemetry ? { telemetry } : {}),
    };
  });

  const egressDenials = renderedEvents.filter((event) => event.kind === 'egress_denied').length;
  const currentTurn =
    job.turn_no ?? options.thread?.jobs.find((threadJob) => threadJob.id === job.id)?.turn_no;
  let priorJobIds: Set<string> | null = null;
  if (currentTurn !== undefined && options.thread?.jobs.length) {
    priorJobIds = new Set(
      options.thread.jobs
        .filter((threadJob) => (threadJob.turn_no ?? 1) < currentTurn)
        .map((threadJob) => threadJob.id),
    );
  }
  const mapMessage = (message: AgentThreadMessageApi) => ({
    id: message.id,
    role: message.role,
    content: message.content,
    jobId: message.job_id,
    createdAt: message.created_at,
  });
  const priorMessages = (options.thread?.messages ?? [])
    .filter((message) =>
      priorJobIds ? priorJobIds.has(message.job_id) : message.job_id !== job.id,
    )
    .map(mapMessage);
  const historicalJobs = (options.thread?.jobs ?? []).filter(
    (threadJob) =>
      threadJob.id !== job.id &&
      (currentTurn === undefined || (threadJob.turn_no ?? 1) < currentTurn),
  );
  const threadEvents = options.threadEvents;
  let historyTurns: AgentHistoryTurn[] | undefined;
  if (threadEvents !== undefined) {
    historyTurns = historicalJobs.map((threadJob) => ({
      jobId: threadJob.id,
      turnNo: threadJob.turn_no ?? 1,
      // This is stronger than platform success: the API has also proved a
      // settled native ordinal, compatible online host/image, and idle source
      // membership. Showing a button from state alone creates a predictable
      // 409 for failed rollout generations.
      forkable: threadJob.native_fork_available === true,
      messages: (options.thread?.messages ?? [])
        .filter((message) => message.job_id === threadJob.id)
        .map(mapMessage),
      events: displayEvents(threadEvents.filter((event) => event.job_id === threadJob.id)),
    }));
  }
  // A turn copied by fork has no events; its durable messages are the only
  // record of what it said. For a settled job whose event log carries no
  // message rows, render the thread's copy of the turn — prompt included, so
  // the turn keeps its chronology and the prompt card knows to stand down.
  const settled = job.state === 'succeeded' || job.state === 'failed' || job.state === 'cancelled';
  let ownMessages: ReturnType<typeof mapMessage>[] = [];
  if (settled && !events.some((event) => event.event_type === 'message')) {
    ownMessages = (options.thread?.messages ?? [])
      .filter((message) => message.job_id === job.id)
      .map(mapMessage);
  }
  const threadPrUrl = [...(options.thread?.jobs ?? [])]
    .reverse()
    .find((threadJob) => threadJob.published_pr_url)?.published_pr_url;
  const publishedPrUrl = job.published_pr_url ?? threadPrUrl ?? null;

  return {
    id: job.id,
    createdAt: job.created_at,
    pinnedAt: job.pinned_at,
    title: (options.thread?.title ?? job.task_prompt.split('\n')[0]).slice(0, 80),
    prompt: job.task_prompt,
    state: toDisplayState(job),
    stateNote: job.detail ?? (job.state === 'waiting' ? 'Queued after the current run' : undefined),
    repo: job.repo,
    baseRef: job.base_ref ?? undefined,
    baseSha: (job.base_sha ?? '').slice(0, 7),
    // Every turn in a conversation publishes to the same branch/PR.
    branch: job.output_branch ?? `agent/${job.thread_id ?? options.thread?.thread_id ?? job.id}`,
    runtime: job.runtime,
    model: job.model,
    reasoningEffort: job.reasoning_effort ?? undefined,
    // Summed server-side from api_logs. Null there means no ledger is
    // configured; 0 is the honest display for that, but the panel below reads
    // `hasLedger` so it can say so rather than imply the job was free.
    spentUsd: job.spent_usd ?? 0,
    hasLedger: job.spent_usd !== null,
    ...(job.continuity_mode ? { continuityMode: job.continuity_mode } : {}),
    ...(job.context_status ? { contextStatus: job.context_status } : {}),
    ...(job.resident_state ? { residentState: job.resident_state } : {}),
    ...(job.sandbox_id ? { sandboxId: job.sandbox_id } : {}),
    ...(job.workspace_shared ? { workspaceShared: true } : {}),
    ...(job.native_state ? { nativeState: job.native_state } : {}),
    ...(job.native_fork_available ? { nativeForkAvailable: true } : {}),
    ...(job.workspace_publication_state
      ? { workspacePublicationState: job.workspace_publication_state }
      : {}),
    ...(typeof job.workspace_epoch === 'number' ? { workspaceEpoch: job.workspace_epoch } : {}),
    ...(typeof job.duration_ms === 'number' ? { durationMs: job.duration_ms } : {}),
    ...(job.resources ? { resources: toTurnResources(job.resources) } : {}),
    timeoutLabel: '',
    networkSetup: tierLabel(job.setup_egress_tier),
    networkAgent: tierLabel(job.agent_egress_tier),
    sandbox: sandboxLabel(events, job.current_attempt_id),
    attempts,
    attemptTelemetryAvailable: job.attempt_telemetry !== undefined,
    currentAttemptNo: currentAttemptIndex >= 0 ? currentAttemptIndex + 1 : undefined,
    telemetryAttemptNo: telemetryAttemptIndex >= 0 ? telemetryAttemptIndex + 1 : undefined,
    events: renderedEvents,
    eventCount: events.length,
    diffFiles: toDiffFiles(patch),
    diffFileDetails: toDiffFileDetails(patch),
    diffStat: diffStat(patch),
    diffLines: toDiffLines(patch),
    rawLines: events.map(rawEventLine),
    gates: [],
    usage: {
      tokensIn: formatTokens(job.tokens_in),
      tokensOut: formatTokens(job.tokens_out),
      cachePct: 0,
      turns: job.model_calls,
    },
    egressDenials,
    prLabel: publishedPrUrl ? `draft PR ${publishedPrUrl.split('/').pop()}` : undefined,
    prUrl: publishedPrUrl ?? undefined,
    threadId: job.thread_id ?? options.thread?.thread_id ?? undefined,
    parentJobId: job.parent_job_id ?? undefined,
    turnNo: job.turn_no,
    threadMessages: [...priorMessages, ...ownMessages],
    ...(historyTurns ? { historyTurns } : {}),
    historyIncludesPrompt: ownMessages.some((message) => message.role === 'user'),
  };
}
