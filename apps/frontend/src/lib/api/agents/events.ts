// Authenticated streams use fetchWithAuth and ReadableStream. Global event
// IDs let reconnects resume after the last received event without losing history.
// The control plane ends each owner SSE stream after a fixed time (15 minutes),
// so a job that outlives one connection is normal, not exceptional.
import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

export interface AgentJobEventApi {
  id: number;
  attempt_id: number;
  seq: number;
  event_type: string;
  payload: Record<string, unknown> | null;
  created_at: string | null;
}

/** One stored event labelled with the conversation turn it belongs to. */
export interface AgentThreadEventApi extends AgentJobEventApi {
  job_id: string;
  turn_no: number;
}

/** Page through every turn's event log in append order. */
export async function listAgentThreadEvents(
  jobId: string,
  after = 0,
  limit = 1000,
  signal?: AbortSignal,
): Promise<{ events: AgentThreadEventApi[]; next_cursor: number }> {
  const path = `/v1/agent/jobs/${encodeURIComponent(jobId)}/thread/events?after=${after}&limit=${limit}`;
  const resp = await (signal
    ? fetchWithAuth(API_BASE, path, { signal })
    : fetchWithAuth(API_BASE, path));
  return jsonOrThrow(resp);
}

/** Read a thread to its end without imposing the server's page size on Chat. */
export async function listAllAgentThreadEvents(
  jobId: string,
  {
    after = 0,
    signal,
    onPage,
  }: {
    /** Start after this event id: the caller already holds the thread up to it. */
    after?: number;
    signal?: AbortSignal;
    /** An immutable snapshot of every page read so far. */
    onPage?: (events: AgentThreadEventApi[]) => void;
  } = {},
): Promise<AgentThreadEventApi[]> {
  const events: AgentThreadEventApi[] = [];
  let cursor = after;
  for (;;) {
    signal?.throwIfAborted();
    const page = await listAgentThreadEvents(jobId, cursor, 1000, signal);
    signal?.throwIfAborted();
    events.push(...page.events);
    if (page.events.length) onPage?.([...events]);
    if (page.events.length < 1000 || page.next_cursor <= cursor) return events;
    cursor = page.next_cursor;
  }
}

export interface StreamAgentJobOptions {
  onEvent: (event: AgentJobEventApi) => void;
  onFinished?: (info: { state: string; published_pr_url: string | null }) => void;
  onError?: (error: unknown) => void;
  /** Each connection the server accepts, before any of its frames. */
  onOpen?: () => void;
  /** Resume point; frames with an id at or below this are not re-sent. */
  lastEventId?: number;
  signal?: AbortSignal;
}

export interface ParsedFrame {
  event: string;
  data: string;
  id?: number;
}

/** One SSE frame's fields; `null` for a comment-only frame such as a keepalive. */
export function parseFrame(raw: string): ParsedFrame | null {
  let event = 'message';
  let id: number | undefined;
  const dataLines: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    // Comment frame (the server's keepalive) — carries no data by design.
    if (line.startsWith(':')) continue;
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    else if (line.startsWith('id:')) {
      const parsed = Number(line.slice(3).trim());
      if (Number.isFinite(parsed)) id = parsed;
    }
  }
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join('\n'), id };
}

/**
 * Stream a job's events, reconnecting from the last id until it finishes.
 *
 * Reconnection is expected behaviour rather than error handling: the control
 * plane caps each stream's lifetime, so a job running longer than that cap
 * ends a connection, without `job_finished`, through no fault of anyone's. The
 * cursor makes that invisible.
 */
export async function streamAgentJob(jobId: string, opts: StreamAgentJobOptions): Promise<void> {
  let cursor = opts.lastEventId ?? 0;
  let finished = false;
  let reconnectAttempts = 0;

  // Empty or repeatedly failing streams must not create a reconnect storm.
  // New event progress restores the short delay for the next dropped stream.
  const waitToReconnect = () =>
    new Promise<void>((resolve) => {
      if (opts.signal?.aborted) {
        resolve();
        return;
      }
      const delayMs = Math.min(100 * 2 ** reconnectAttempts, 5_000);
      reconnectAttempts = Math.min(reconnectAttempts + 1, 6);
      const finish = () => {
        clearTimeout(timer);
        opts.signal?.removeEventListener('abort', finish);
        resolve();
      };
      const timer = setTimeout(finish, delayMs);
      opts.signal?.addEventListener('abort', finish, { once: true });
    });

  while (!finished) {
    if (opts.signal?.aborted) return;

    let resp: Response;
    try {
      resp = await fetchWithAuth(
        API_BASE,
        `/v1/agent/jobs/${encodeURIComponent(jobId)}/stream?after=${cursor}`,
        { headers: { Accept: 'text/event-stream' }, signal: opts.signal },
      );
    } catch (err) {
      if (opts.signal?.aborted) return;
      opts.onError?.(err);
      return;
    }

    if (!resp.ok || !resp.body) {
      await jsonOrThrow(resp).catch((err) => opts.onError?.(err));
      return;
    }
    opts.onOpen?.();

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const boundary = /\r?\n\r?\n/;
        let match: RegExpExecArray | null;
        while ((match = boundary.exec(buffer))) {
          const raw = buffer.slice(0, match.index);
          buffer = buffer.slice(match.index + match[0].length);
          const frame = parseFrame(raw);
          if (!frame) continue;

          if (frame.event === 'job_finished') {
            finished = true;
            try {
              opts.onFinished?.(JSON.parse(frame.data));
            } catch {
              opts.onFinished?.({ state: 'unknown', published_pr_url: null });
            }
            return;
          }

          try {
            const parsed = JSON.parse(frame.data) as AgentJobEventApi;
            // Track the cursor from the frame id so a reconnect resumes
            // exactly here, with no gap and no duplicate.
            const nextCursor = frame.id ?? parsed.id ?? cursor;
            if (nextCursor > cursor) reconnectAttempts = 0;
            cursor = nextCursor;
            opts.onEvent(parsed);
          } catch {
            // A frame we cannot parse is dropped rather than aborting the
            // stream: losing one row beats losing the rest of the job.
          }
        }
      }
    } catch (err) {
      if (opts.signal?.aborted) return;
      // A connection dropped mid-stream: report it, then reconnect from the
      // cursor below. The server's stream cap ends a stream cleanly instead,
      // through `done` above.
      opts.onError?.(err);
    } finally {
      try {
        await reader.cancel();
      } catch {
        // Abort and network failures may already have errored the body.
      }
      reader.releaseLock();
    }

    if (!finished) await waitToReconnect();
  }
}
