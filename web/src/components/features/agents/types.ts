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

export type AgentEvent =
  | { kind: 'lifecycle'; text: string; attemptNo?: number }
  | { kind: 'thinking'; text: string; attemptNo?: number }
  | { kind: 'message'; text: string; attemptNo?: number }
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
    }
  | {
      kind: 'tool_result';
      text: string;
      toolUseId?: string;
      isError: boolean;
      /** Event time, ISO — paired with its tool_use to time the call. */
      at?: string | null;
      attemptNo?: number;
    }
  | { kind: 'terminal'; text: string; isError?: boolean; attemptNo?: number }
  | { kind: 'egress_denied'; host: string; attempts: number; attemptNo?: number }
  | { kind: 'usage'; text: string; attemptNo?: number };

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
 * Usually they cannot say anything. The sandbox is read once a second and
 * most calls are far shorter than that, so `sampleCount` is normally 0 and
 * every counter null — which the UI must render as "not measured", never as
 * zero. `overlapN` above 1 means the call ran alongside others and its
 * numbers are an upper bound shared with them.
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
  /** Exit-time accounting for the whole thread group; null until it exited. */
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
  /** Conversation-level pin time. Every turn in the same thread shares it. */
  pinnedAt?: string | null;
  title: string;
  /** Complete current-turn prompt. Older fixtures may only have `title`. */
  prompt?: string;
  state: AgentJobState;
  /** Short badge text next to the title, e.g. gate hold or failure reason. */
  stateNote?: string;
  repo: string;
  baseRef?: string;
  baseSha: string;
  /** Output branch, agent/<thread-id> for conversations or agent/<job-id> for legacy jobs. */
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
  events: AgentEvent[];
  /** Bottom-of-stream live line while running, e.g. "turn 15 · …". */
  liveNote?: string;
  eventCount: number;
  diffFiles: string[];
  diffFileDetails?: AgentDiffFile[];
  diffStat?: { add: number; del: number };
  diffLines: DiffLine[];
  rawLines: string[];
  gates: AgentGate[];
  usage: { tokensIn: string; tokensOut: string; cachePct: number; turns: number };
  /**
   * How this turn was continued. `replay` means it was rebuilt from the
   * transcript with the agent's working memory lost — the honest answer to
   * "why did it forget", and the reason this is surfaced at all.
   */
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
