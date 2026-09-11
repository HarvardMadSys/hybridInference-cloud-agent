// UI types for the cloud agent sandbox (roadmap: issue #1041).
//
// The normalized event kinds mirror the agent_job_events schema decided in the
// roadmap — thinking | message | tool_use | tool_result | diff | usage |
// error | lifecycle. Tool results are folded into their matching activity when
// possible, but remain renderable on their own for older/partial event logs.

export type AgentJobState = 'queued' | 'running' | 'needs_review' | 'done' | 'failed' | 'cancelled';

export interface AgentAttempt {
  id?: number;
  no: number;
  status: 'live' | 'superseded' | 'finished';
  /** Shown in the audit banner when a lease takeover superseded the attempt. */
  note?: string;
  /** Metrics from this attempt only; absent on servers predating this contract. */
  telemetry?: {
    continuityMode?: 'hot' | 'warm_native' | 'cold_native' | 'replay';
    contextStatus?: 'new' | 'live' | 'restored' | 'lost';
    durationMs?: number;
    resources?: TurnResources;
  };
}

/**
 * One rendered row of the run's activity.
 *
 * `sourceEventId` is the stored event's global id, kept so a row that survives
 * folding keeps a durable identity. The conversation's reading anchor is
 * restored by row, so an identity that shifts when a tool result is folded in
 * — or when React re-keys the list — would move a reader who never scrolled.
 * Display-only: it is never sent back to the server or to the model.
 */
export type AgentEvent =
  | { kind: 'lifecycle'; text: string; attemptNo?: number; sourceEventId?: number }
  | { kind: 'thinking'; text: string; attemptNo?: number; sourceEventId?: number }
  | { kind: 'message'; text: string; attemptNo?: number; sourceEventId?: number }
  | {
      kind: 'tool_use';
      tool: string;
      detail: string;
      id?: string;
      /** Event time, ISO. Present so per-tool durations are derivable. */
      at?: string | null;
      /**
       * Resolved when this row absorbed its result during folding — the only
       * moment both ends of the call are still in the same list.
       */
      durationMs?: number;
      /** Collapsed tool output preview (mono block), e.g. pytest tail. */
      output?: string[];
      outputIsError?: boolean;
      /** e.g. "+12 −3" for Edit rows. */
      diffStat?: string;
      attemptNo?: number;
      sourceEventId?: number;
    }
  | {
      kind: 'tool_result';
      text: string;
      toolUseId?: string;
      isError: boolean;
      /** Event time, ISO — paired with its tool_use to time the call. */
      at?: string | null;
      attemptNo?: number;
      sourceEventId?: number;
    }
  | {
      kind: 'terminal';
      text: string;
      isError?: boolean;
      attemptNo?: number;
      sourceEventId?: number;
    }
  | {
      kind: 'egress_denied';
      host: string;
      attempts: number;
      attemptNo?: number;
      sourceEventId?: number;
    }
  | { kind: 'usage'; text: string; attemptNo?: number; sourceEventId?: number };

export interface AgentThreadMessage {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  jobId: string;
  createdAt: string | null;
}

/** One completed turn reconstructed in the same order the agent produced it. */
export interface AgentHistoryTurn {
  jobId: string;
  turnNo: number;
  /**
   * State of the job that produced this turn, when the thread reported one.
   * Absent on older payloads; a preview says nothing rather than guessing that
   * a turn it cannot classify succeeded.
   */
  state?: AgentJobState;
  /** Only a completed native assistant boundary is a valid fork anchor. */
  forkable: boolean;
  messages: AgentThreadMessage[];
  events: AgentEvent[];
}

export interface DiffLine {
  marker: 'hunk' | 'ctx' | 'add' | 'del';
  text: string;
}

export interface AgentDiffFile {
  path: string;
  add: number;
  del: number;
  lines: DiffLine[];
}

export interface AgentGate {
  label: string;
  state: 'pass' | 'pending' | 'hold';
  detail?: string;
}

/**
 * What one tool call consumed, as far as the turn's readings can say.
 *
 * Current shared-member turns do not sample the whole container, so
 * `sampleCount` is 0 and those sampled counters are null. Historical rows may
 * contain periodic readings; fewer than two still means "not measured", and
 * `overlapN` above 1 means a historical measurement was shared by calls.
 */
export interface ToolCallResources {
  toolUseId: string;
  toolName: string;
  /**
   * Where the window came from. `exact` is the sandbox's own hooks marking
   * the boundaries; `message` only brackets the call from outside, from when
   * the platform parsed its events.
   */
  timingSource: 'exact' | 'message';
  durationMs: number | null;
  sampleCount: number;
  overlapN: number;
  measuredSpanMs: number | null;
  cpuUsec: number | null;
  memPeakBytes: number | null;
  /** Whether the two counters above are this call's alone. */
  exclusive: boolean;
  processCount?: number;
  /**
   * What the processes this call alone started consumed, accounted by the
   * guest kernel as each exited. Independent of the sampler: a 35 ms call
   * has exact numbers here if its child exited. CPU and I/O are sums, the
   * peak is the largest single process. Null when no exited process belonged
   * to this call alone, or the kernel does not account that counter.
   */
  processCpuUsec?: number | null;
  processMemPeakBytes?: number | null;
  processIoReadBytes?: number | null;
  processIoWriteBytes?: number | null;
  /**
   * A process this call may own was left out of those numbers — shared with
   * an overlapping call, or still running when the turn ended — so they are
   * floors rather than totals.
   */
  processResourcesPartial?: boolean;
}

export interface ProcessObservation {
  processSeq: number;
  uid: number;
  pid: number;
  tgid: number;
  ppid: number | null;
  comm: string;
  executable: string | null;
  startedOffsetMs: number;
  durationMs: number | null;
  exitCode: number | null;
  observedFork: boolean;
  observedExec: boolean;
  observedExit: boolean;
  toolCallSeqs: number[];
  /** Exit-time accounting for the Linux process group; null until it exited. */
  cpuUsec?: number | null;
  memPeakBytes?: number | null;
  ioReadBytes?: number | null;
  ioWriteBytes?: number | null;
  ioRcharBytes?: number | null;
  ioWcharBytes?: number | null;
}

export interface TurnResources {
  cpuUsec: number | null;
  memPeakBytes: number | null;
  memAvgBytes: number | null;
  ioReadBytes: number | null;
  ioWriteBytes: number | null;
  netRxBytes: number | null;
  netTxBytes: number | null;
  sampleCount: number;
  /**
   * Identifies the accounting semantics, not just a sampler binary. Current
   * shared-member turns use `shared-member-v1`; historical Docker-sampled
   * rows use their sampler version.
   */
  samplerVersion: string | null;
  /**
   * Share of the turn actually sampled. Below 1 means the numbers are a
   * floor, not a total, and the UI says so rather than rounding the gap away.
   */
  coverage: number;
  /**
   * How long the turn spent inside tool calls — the union of their windows,
   * never the sum, so it cannot exceed the turn. Null where it was never
   * measured, which is not zero.
   */
  toolTimeMs: number | null;
  /** One entry per tool call the turn issued, in the order it issued them. */
  toolCalls: ToolCallResources[];
  /** True when the turn issued more calls than `toolCalls` carries. */
  toolCallsTruncated: boolean;
  processObserver?: string | null;
  processObservationComplete?: boolean;
  processEventsLost?: number;
  processesTruncated?: boolean;
  processes?: ProcessObservation[];
}

export interface AgentJob {
  id: string;
  /** Used to group conversation rows in the recent-tasks sidebar. */
  createdAt?: string | null;
  /** Execution timestamps, never the job's submission or last-update time. */
  startedAt?: string | null;
  finishedAt?: string | null;
  /** Conversation-level pin time. Every turn in the same thread shares it. */
  pinnedAt?: string | null;
  archivedAt?: string | null;
  title: string;
  /** Complete current-turn prompt. Older fixtures may only have `title`. */
  prompt?: string;
  state: AgentJobState;
  /** Short badge text next to the title, e.g. gate hold or failure reason. */
  stateNote?: string;
  repo: string;
  baseRef?: string;
  baseSha: string;
  /** Output branch for this retained conversation workspace. */
  branch: string;
  runtime: string;
  runtimeVersion?: string;
  model: string;
  /**
   * How hard this turn asked the model to think. Undefined is Auto — nobody
   * chose — which is every turn created before the control existed.
   */
  reasoningEffort?: string;
  /** Set when the model is served on our own hardware, e.g. "H200". */
  modelLocal?: string;
  spentUsd: number;
  // False when the deployment has no billing ledger, so the UI can say
  // "unknown" instead of implying the job cost nothing.
  hasLedger?: boolean;
  elapsedLabel?: string;
  timeoutLabel: string;
  networkSetup: string;
  networkAgent: string;
  sandbox: string;
  attempts: AgentAttempt[];
  /** Server supports fetching a single attempt's resource detail. */
  attemptTelemetryAvailable?: boolean;
  /** Display number for the server-authoritative current attempt, when it has emitted an event. */
  currentAttemptNo?: number;
  /** Durable attempt identity shared by list and detail projections. */
  currentAttemptId?: number | null;
  /** Display number whose continuity/resources came from the telemetry read. */
  telemetryAttemptNo?: number;
  events: AgentEvent[];
  /** Bottom-of-stream live line while running, e.g. "turn 15 · …". */
  liveNote?: string;
  diffFiles: string[];
  /** Whether the current shared workspace diff was actually readable. */
  workspaceDiffAvailable?: boolean;
  diffFileDetails?: AgentDiffFile[];
  diffStat?: { add: number; del: number };
  gates: AgentGate[];
  usage: { tokensIn: string; tokensOut: string; cachePct: number; turns: number | null };
  /** Attempt continuity telemetry; `replay` can occur only on historical rows. */
  continuityMode?: 'hot' | 'warm_native' | 'cold_native' | 'replay';
  /** Canonical user-facing context state; independent of process-id reuse. */
  contextStatus?: 'new' | 'live' | 'restored' | 'lost';
  /** Process residency is distinct from context correctness. */
  residentState?: 'stopped' | 'starting' | 'live' | 'restarting' | 'lost';
  sandboxId?: string;
  workspaceShared?: boolean;
  nativeState?: 'new' | 'ready' | 'fork_preparing' | 'lost';
  nativeForkAvailable?: boolean;
  workspacePublicationState?:
    | 'pending'
    | 'capturing'
    | 'captured'
    | 'publishing'
    | 'published'
    | 'failed';
  workspaceEpoch?: number;
  /** Wall time of the turn's newest attempt (legacy/default view). */
  durationMs?: number;
  /** Host-measured consumption for the newest attempt (legacy/default view). */
  resources?: TurnResources;
  egressDenials: number;
  /** e.g. "draft PR #1044" once published. */
  prLabel?: string;
  prUrl?: string;
  threadId?: string;
  parentJobId?: string;
  turnNo?: number;
  /** Completed turns before this job. The current prompt/activity are separate. */
  threadMessages?: AgentThreadMessage[];
  /** Historical turns with their tool activity restored from the event log. */
  historyTurns?: AgentHistoryTurn[];
  /**
   * True when threadMessages already carries this job's own turn (a forked
   * copy has no events, so its durable messages are rendered instead) — the
   * separate current-prompt card would be a duplicate.
   */
  historyIncludesPrompt?: boolean;
}
