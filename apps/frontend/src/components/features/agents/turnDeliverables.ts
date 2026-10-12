import type { AgentJobApi, AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';
import { toDiffFileDetails } from './adapt';
import type { AgentJobState, DiffLine } from './types';

/** A file from a recorded native diff, never the shared workspace's live Git view. */
export interface RecordedTurnFile {
  path: string;
  lines: DiffLine[];
  /** Absent for binary and metadata-only changes. */
  add?: number;
  del?: number;
}

export interface RecordedTurnOutputs {
  /** The producing turn, retained when opening a historical file. */
  jobId: string;
  files: RecordedTurnFile[];
  prUrl?: string;
}

export interface RecordedFileSelection {
  jobId: string;
  file: RecordedTurnFile;
}

type RecordedTurnJob = Pick<
  AgentJobApi,
  'id' | 'state' | 'published_pr_url' | 'current_attempt_id'
>;
type RecordedTurnEvent = AgentJobEventApi | AgentThreadEventApi;

export interface RecordedOutputsSelector {
  select(
    jobs: readonly RecordedTurnJob[],
    events: readonly RecordedTurnEvent[],
  ): Map<string, RecordedTurnOutputs | null>;
}

export function hasSettledTurn(state: AgentJobState | AgentJobApi['state'] | undefined): boolean {
  return (
    state === 'done' ||
    state === 'needs_review' ||
    state === 'succeeded' ||
    state === 'failed' ||
    state === 'cancelled'
  );
}

export function recordedResultUrl(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? value : undefined;
  } catch {
    return undefined;
  }
}

/** Validate hunk sizes so a clipped native diff cannot advertise complete line counts. */
function hasCompleteHunks(lines: DiffLine[]): boolean {
  let remainingOld: number | undefined;
  let remainingNew: number | undefined;
  for (const { text } of lines) {
    if (text.startsWith('@@')) {
      if (remainingOld !== undefined && (remainingOld !== 0 || remainingNew !== 0)) return false;
      const hunk = /^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@/.exec(text);
      if (!hunk) return false;
      remainingOld = hunk[1] === undefined ? 1 : Number(hunk[1]);
      remainingNew = hunk[2] === undefined ? 1 : Number(hunk[2]);
      continue;
    }
    if (remainingOld === undefined || remainingNew === undefined) continue;
    if (text.startsWith('\\ No newline at end of file')) continue;
    if (text.startsWith('+')) remainingNew -= 1;
    else if (text.startsWith('-')) remainingOld -= 1;
    else if (text.startsWith(' ')) {
      remainingOld -= 1;
      remainingNew -= 1;
    } else if (text !== '') return false;
    if (remainingOld < 0 || remainingNew < 0) return false;
  }
  return remainingOld === undefined || (remainingOld === 0 && remainingNew === 0);
}

function recordedFiles(payload: AgentJobEventApi['payload']): RecordedTurnFile[] {
  const text = payload?.text;
  // Native adapters clip diff previews at 4,000 characters, sometimes without
  // a flag. At that boundary we cannot prove the final file or hunk survived.
  // Other runtimes emit a change marker or JSON rather than a unified diff.
  if (
    typeof text !== 'string' ||
    text.length >= 4000 ||
    payload?.truncated === true ||
    !text.startsWith('diff --git a/')
  ) {
    return [];
  }
  const headers = text.split('\n').filter((line) => line.startsWith('diff --git '));
  const files = toDiffFileDetails(text);
  if (
    !files.length ||
    files.length !== headers.length ||
    new Set(files.map((file) => file.path)).size !== files.length
  ) {
    return [];
  }
  // The shared parser handles plain paths. Quoted/escaped Git paths need a
  // decoder before they can become usable file actions, so keep them out.
  if (files.some((file) => !file.path || file.path.includes('\\') || file.path.startsWith('"')))
    return [];
  // A path containing " b/" can confuse an unquoted diff header. The new-file
  // marker, when present, must agree before we advertise a selectable path.
  if (
    files.some((file) =>
      file.lines.some((line) => line.text.startsWith('+++ b/') && line.text.slice(6) !== file.path),
    )
  ) {
    return [];
  }
  if (
    files.some(
      (file) =>
        !hasCompleteHunks(file.lines) ||
        !file.lines.some(
          (line) =>
            line.marker === 'hunk' ||
            /^(?:Binary files |GIT binary patch|(?:new file|deleted file|old|new) mode |rename (?:from|to) |copy (?:from|to) )/.test(
              line.text,
            ),
        ),
    )
  ) {
    return [];
  }

  return files.map((file) => {
    const hasHunks = file.lines.some((line) => line.marker === 'hunk');
    return {
      path: file.path,
      lines: file.lines,
      ...(hasHunks ? { add: file.add, del: file.del } : {}),
    };
  });
}

function outputsWithFiles(
  job: RecordedTurnJob,
  files: RecordedTurnFile[],
): RecordedTurnOutputs | null {
  const prUrl = recordedResultUrl(job.published_pr_url);
  return files.length || prUrl ? { jobId: job.id, files, ...(prUrl ? { prUrl } : {}) } : null;
}

interface IndexedDiff {
  event: RecordedTurnEvent;
  seq: number;
  order: number;
}

function latestDiff(
  a: IndexedDiff | undefined,
  b: IndexedDiff | undefined,
): IndexedDiff | undefined {
  if (!a) return b;
  if (!b) return a;
  return b.seq > a.seq || (b.seq === a.seq && b.order < a.order) ? b : a;
}

interface CachedOutputs {
  attemptId: RecordedTurnJob['current_attempt_id'];
  state: RecordedTurnJob['state'];
  prUrl: RecordedTurnJob['published_pr_url'];
  latest: RecordedTurnEvent | undefined;
  outputs: RecordedTurnOutputs | null;
}

/**
 * One selector belongs to one mounted conversation. Index the event log once
 * per selection, then reuse historical results while unrelated events stream.
 * Event objects are immutable snapshots: replacing an event, even with the
 * same stored id, invalidates its parsed diff. Removed jobs leave the cache.
 */
export function createRecordedOutputsSelector(): RecordedOutputsSelector {
  let cachedOutputs = new Map<string, CachedOutputs>();
  const parsedFiles = new WeakMap<RecordedTurnEvent, RecordedTurnFile[]>();

  return {
    select(jobs, events) {
      const labelled = new Map<string, Map<number, IndexedDiff>>();
      const unlabelled = new Map<number, IndexedDiff>();
      for (let order = 0; order < events.length; order += 1) {
        const event = events[order];
        if (event.event_type !== 'diff') continue;
        let attempts = unlabelled;
        if ('job_id' in event) {
          const existing = labelled.get(event.job_id);
          attempts = existing ?? new Map<number, IndexedDiff>();
          if (!existing) labelled.set(event.job_id, attempts);
        }
        const diff = { event, seq: event.seq, order };
        const previous = attempts.get(event.attempt_id);
        // Equal stored sequences retain the first event in the input log.
        if (!previous || diff.seq > previous.seq) attempts.set(event.attempt_id, diff);
      }

      const results = new Map<string, RecordedTurnOutputs | null>();
      const nextCache = new Map<string, CachedOutputs>();
      for (const job of jobs) {
        const settled = hasSettledTurn(job.state);
        const attemptId = job.current_attempt_id;
        const latest =
          settled && attemptId !== null
            ? latestDiff(labelled.get(job.id)?.get(attemptId), unlabelled.get(attemptId))?.event
            : undefined;
        const cached = cachedOutputs.get(job.id);
        if (
          cached &&
          cached.attemptId === attemptId &&
          cached.state === job.state &&
          cached.prUrl === job.published_pr_url &&
          cached.latest === latest
        ) {
          nextCache.set(job.id, cached);
          results.set(job.id, cached.outputs);
          continue;
        }

        let outputs: RecordedTurnOutputs | null = null;
        if (settled) {
          let files: RecordedTurnFile[] = [];
          if (latest) {
            const cachedFiles = parsedFiles.get(latest);
            files = cachedFiles ?? recordedFiles(latest.payload);
            if (!cachedFiles) parsedFiles.set(latest, files);
          }
          outputs = outputsWithFiles(job, files);
        }
        nextCache.set(job.id, {
          attemptId,
          state: job.state,
          prUrl: job.published_pr_url,
          latest,
          outputs,
        });
        results.set(job.id, outputs);
      }
      cachedOutputs = nextCache;
      return results;
    },
  };
}

/**
 * Read only this turn's own output evidence. Native diffs are recorded
 * snapshots, not a promise that every workspace edit was captured. In
 * particular, never substitute AgentJob.diffFiles or /git for missing events.
 * Input attachments have no generated-artifact contract and are not results.
 */
export function recordedTurnOutputs(
  job: RecordedTurnJob,
  events: readonly RecordedTurnEvent[],
): RecordedTurnOutputs | null {
  if (!hasSettledTurn(job.state)) return null;
  const diffs = events.filter(
    (event) =>
      event.event_type === 'diff' &&
      (!('job_id' in event) || event.job_id === job.id) &&
      job.current_attempt_id !== null &&
      event.attempt_id === job.current_attempt_id,
  );
  const latest = diffs.reduce<AgentJobEventApi | undefined>(
    (previous, event) => (!previous || event.seq > previous.seq ? event : previous),
    undefined,
  );
  const files = latest ? recordedFiles(latest.payload) : [];
  return outputsWithFiles(job, files);
}
