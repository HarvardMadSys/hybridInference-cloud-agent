// The signed-in owner's change stream: one connection per tab
// (`GET /v1/agent/changes`). It pushes each of their jobs and conversations the
// moment it changes, with the row's `revision` and the values the UI shows, and
// a job's usage with its value.
//
// Read with fetch + ReadableStream for the same reason as the job stream: the
// endpoint is authenticated. It replays nothing; `hello` and `resync` tell the
// tab to read its lists again, which is how nothing between two connections is
// lost.
import { config } from '@/config/env';

import type { AgentJobApi, AgentJobUsageApi } from './agents';
import { parseFrame } from './agents';
import { fetchWithAuth } from './client';

const API_BASE = config.apiBase;

/** First and largest wait before reconnecting after a drop. */
export const CHANGES_RECONNECT_MIN_MS = 1_000;
export const CHANGES_RECONNECT_MAX_MS = 30_000;

/** A job as the change stream reports it, or `{id, deleted: true}` once gone. */
export interface AgentJobChangeApi {
  id: string;
  deleted?: boolean;
  thread_id?: string;
  revision?: number;
  state?: AgentJobApi['state'];
  conversation_state?: AgentJobApi['conversation_state'];
  published_pr_url?: string | null;
}

/** A conversation as the change stream reports it, or `{id, deleted: true}`. */
export interface AgentThreadChangeApi {
  id: string;
  deleted?: boolean;
  revision?: number;
  title?: string;
  title_source?: AgentJobApi['title_source'];
  pinned_at?: string | null;
  archived_at?: string | null;
  deleted_at?: string | null;
  updated_at?: string | null;
}

/**
 * Things that changed for a job or a conversation. `usage` and `mcp` name a job;
 * `queue` and `publication` name a conversation. `usage` comes with the job's
 * usage as its usage read returns it; the others, and a `usage` without one,
 * mean: read that one thing again.
 */
export const AGENT_CHANGE_HINTS = ['usage', 'queue', 'mcp', 'publication'] as const;
export type AgentChangeHint = (typeof AGENT_CHANGE_HINTS)[number];

export interface StreamAgentChangesOptions {
  signal: AbortSignal;
  /** `hello` on connect and `resync` after a gap: read lists again. */
  onSync: (live: boolean) => void;
  onJob: (change: AgentJobChangeApi) => void;
  onThread: (change: AgentThreadChangeApi) => void;
  /** `usage` is passed when the stream sent the job's usage with it. */
  onHint: (kind: AgentChangeHint, id: string, usage?: AgentJobUsageApi) => void;
  /** The connection dropped; nothing is pushed until it is back. */
  onDisconnect: () => void;
}

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

/**
 * Hold the owner's change stream open until `signal` aborts, reconnecting with
 * a growing delay after each drop.
 *
 * Resolves `unavailable` when the deployment pushes nothing (404) or the
 * session is no longer valid (401/403): the tab then keeps reading on its own
 * clocks, which is what it did before this stream existed.
 */
export async function streamAgentChanges(
  opts: StreamAgentChangesOptions,
): Promise<'aborted' | 'unavailable'> {
  const { signal } = opts;
  let delay = CHANGES_RECONNECT_MIN_MS;
  while (!signal.aborted) {
    let resp: Response;
    try {
      resp = await fetchWithAuth(API_BASE, '/v1/agent/changes', {
        headers: { Accept: 'text/event-stream' },
        signal,
      });
    } catch {
      if (signal.aborted) break;
      opts.onDisconnect();
      await pause(delay, signal);
      delay = Math.min(delay * 2, CHANGES_RECONNECT_MAX_MS);
      continue;
    }
    if ([401, 403, 404].includes(resp.status)) return 'unavailable';
    if (!resp.ok || !resp.body) {
      opts.onDisconnect();
      await pause(delay, signal);
      delay = Math.min(delay * 2, CHANGES_RECONNECT_MAX_MS);
      continue;
    }

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
          let data: unknown;
          try {
            data = JSON.parse(frame.data);
          } catch {
            continue;
          }
          if (!data || typeof data !== 'object') continue;
          const record = data as Record<string, unknown>;
          if (frame.event === 'hello' || frame.event === 'resync') {
            if (frame.event === 'hello') delay = CHANGES_RECONNECT_MIN_MS;
            opts.onSync(record.live === true);
          } else if (typeof record.id === 'string') {
            if (frame.event === 'job') opts.onJob(record as unknown as AgentJobChangeApi);
            else if (frame.event === 'thread')
              opts.onThread(record as unknown as AgentThreadChangeApi);
            else if ((AGENT_CHANGE_HINTS as readonly string[]).includes(frame.event)) {
              const usage = record.usage;
              if (frame.event === 'usage' && usage && typeof usage === 'object')
                opts.onHint('usage', record.id, usage as AgentJobUsageApi);
              else opts.onHint(frame.event as AgentChangeHint, record.id);
            }
          }
        }
      }
    } catch {
      // A drop mid-stream is expected when the server's stream cap fires.
    } finally {
      reader.releaseLock();
    }
    if (signal.aborted) break;
    opts.onDisconnect();
    await pause(delay, signal);
    delay = Math.min(delay * 2, CHANGES_RECONNECT_MAX_MS);
  }
  return 'aborted';
}
