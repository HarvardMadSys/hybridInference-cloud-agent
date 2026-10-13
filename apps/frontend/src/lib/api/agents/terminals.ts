import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

export interface AgentTerminalApi {
  id: string;
  shell: string;
  state: string;
  cwd: string;
  rows: number;
  cols: number;
  last_seq: number;
}

export interface AgentTerminalListApi {
  terminals: AgentTerminalApi[];
  /** Per-workspace creation limit advertised by the owning sandbox manager. */
  max_sessions?: number | null;
  /** Maximum input bytes in one request, advertised by the owning sandbox manager. */
  max_input_bytes?: number | null;
}

/** List the PTY sessions that still belong to this workspace. */
export function listAgentTerminals(jobId: string): Promise<AgentTerminalApi[]>;

export function listAgentTerminals(
  jobId: string,
  options: { includeLimits: true },
): Promise<AgentTerminalApi[] | AgentTerminalListApi>;

export async function listAgentTerminals(
  jobId: string,
  options?: { includeLimits: true },
): Promise<AgentTerminalApi[] | AgentTerminalListApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/terminals`,
  );
  const body = await jsonOrThrow<AgentTerminalApi[] | AgentTerminalListApi>(resp);
  if (options?.includeLimits) return body;
  return Array.isArray(body) ? body : body.terminals;
}

/** Start an isolated shell that shares the job's retained workspace. */
export async function createAgentTerminal(
  jobId: string,
  size: { rows: number; cols: number } = { rows: 24, cols: 80 },
): Promise<AgentTerminalApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/terminals`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(size),
    },
  );
  return jsonOrThrow<AgentTerminalApi>(resp);
}

function terminalPath(jobId: string, terminalId: string): string {
  return `/v1/agent/jobs/${encodeURIComponent(jobId)}/terminals/${encodeURIComponent(terminalId)}`;
}

const MAX_TERMINAL_INPUT_BYTES = 64 * 1024;

function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function byteChunks(encoded: Uint8Array, maximum: number): Uint8Array[] {
  if (encoded.byteLength === 0) return [];
  const chunks: Uint8Array[] = [];
  let offset = 0;
  while (offset < encoded.byteLength) {
    let end = Math.min(offset + maximum, encoded.byteLength);
    if (end < encoded.byteLength) {
      // Prefer not to split a UTF-8 character. A character has at most three
      // continuation bytes, so anything longer is not text and is cut as is.
      let boundary = end;
      while (boundary > end - 4 && boundary > offset && (encoded[boundary] & 0xc0) === 0x80) {
        boundary -= 1;
      }
      if (boundary > offset && (encoded[boundary] & 0xc0) !== 0x80) end = boundary;
    }
    chunks.push(encoded.subarray(offset, end));
    offset = end;
  }
  return chunks;
}

/**
 * Send keystrokes, including control sequences, to a running PTY.
 *
 * Text is sent as UTF-8. Bytes are sent as they are: xterm reports some mouse
 * events as binary that is not valid UTF-8.
 */
export async function writeAgentTerminalInput(
  jobId: string,
  terminalId: string,
  data: string | Uint8Array,
  maxInputBytes = MAX_TERMINAL_INPUT_BYTES,
): Promise<void> {
  // Four bytes can hold any UTF-8 character; smaller or malformed limits
  // cannot safely delimit a chunk and use the compatibility fallback.
  const maximum =
    Number.isInteger(maxInputBytes) && maxInputBytes >= 4
      ? Math.min(maxInputBytes, MAX_TERMINAL_INPUT_BYTES)
      : MAX_TERMINAL_INPUT_BYTES;
  const encoded = typeof data === 'string' ? new TextEncoder().encode(data) : data;
  for (const chunk of byteChunks(encoded, maximum)) {
    const resp = await fetchWithAuth(API_BASE, `${terminalPath(jobId, terminalId)}/input`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: bytesToBase64(chunk) }),
    });
    if (!resp.ok) await jsonOrThrow(resp);
  }
}

/** Tell the remote PTY its rendered character dimensions. */
export async function resizeAgentTerminal(
  jobId: string,
  terminalId: string,
  rows: number,
  cols: number,
): Promise<void> {
  const resp = await fetchWithAuth(API_BASE, `${terminalPath(jobId, terminalId)}/resize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rows, cols }),
  });
  if (!resp.ok) await jsonOrThrow(resp);
}

/** Kill a PTY and its process tree. */
export async function deleteAgentTerminal(jobId: string, terminalId: string): Promise<void> {
  const resp = await fetchWithAuth(API_BASE, terminalPath(jobId, terminalId), {
    method: 'DELETE',
  });
  if (!resp.ok) await jsonOrThrow(resp);
}

export interface AgentTerminalOutputEvent {
  seq: number;
  data: string;
}

export interface AgentTerminalExitEvent {
  seq: number;
  exit_code: number;
}

export interface AgentTerminalResetEvent {
  seq: number;
  reason: 'output_truncated';
}

/** Sent first on every connection: events up to `seq` predate this viewer. */
export interface AgentTerminalAttachedEvent {
  seq: number;
}

export interface StreamAgentTerminalOptions {
  after?: number;
  signal?: AbortSignal;
  onOutput: (event: AgentTerminalOutputEvent) => void;
  onReset: (event: AgentTerminalResetEvent) => void;
  onExit: (event: AgentTerminalExitEvent) => void;
  /** Where the terminal's history ends; sandbox managers that predate it never call this. */
  onAttached?: (event: AgentTerminalAttachedEvent) => void;
}

/**
 * Stream PTY output over authenticated SSE.
 *
 * `after` is the last applied sequence, so callers can resume a dropped
 * connection without replaying terminal bytes into xterm twice.
 */
export async function streamAgentTerminal(
  jobId: string,
  terminalId: string,
  options: StreamAgentTerminalOptions,
): Promise<void> {
  let cursor = Math.max(0, options.after ?? 0);
  let exited = false;
  let reconnectAttempts = 0;

  const dispatch = (frame: string): boolean => {
    let eventName = '';
    const dataLines: string[] = [];
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) eventName = line.slice(6).trim();
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
    }
    if (!eventName || dataLines.length === 0) return false;
    try {
      const payload = JSON.parse(dataLines.join('\n')) as Record<string, unknown>;
      const seq = payload.seq;
      if (eventName === 'attached') {
        // Not a terminal event: it names where history ends and must not move
        // the resume cursor.
        if (typeof seq === 'number' && Number.isSafeInteger(seq) && seq >= 0) {
          options.onAttached?.({ seq });
        }
        return false;
      }
      if (typeof seq !== 'number' || !Number.isSafeInteger(seq) || seq <= cursor) return false;
      if (eventName === 'output' && typeof payload.data === 'string') {
        cursor = seq;
        reconnectAttempts = 0;
        options.onOutput(payload as unknown as AgentTerminalOutputEvent);
        return true;
      } else if (eventName === 'reset' && payload.reason === 'output_truncated') {
        cursor = seq;
        reconnectAttempts = 0;
        options.onReset(payload as unknown as AgentTerminalResetEvent);
        return true;
      } else if (eventName === 'exit' && typeof payload.exit_code === 'number') {
        cursor = seq;
        exited = true;
        reconnectAttempts = 0;
        options.onExit(payload as unknown as AgentTerminalExitEvent);
        return true;
      }
    } catch {
      // One malformed frame must not discard the rest of a live shell stream.
    }
    return false;
  };

  const reconnectDelay = (delayMs: number) =>
    new Promise<void>((resolve) => {
      if (options.signal?.aborted) {
        resolve();
        return;
      }
      const finish = () => {
        clearTimeout(timer);
        options.signal?.removeEventListener('abort', finish);
        resolve();
      };
      const timer = setTimeout(finish, delayMs);
      options.signal?.addEventListener('abort', finish, { once: true });
    });

  const waitToReconnect = async (): Promise<boolean> => {
    if (options.signal?.aborted) return false;
    const delayMs = Math.min(100 * 2 ** reconnectAttempts, 5_000);
    reconnectAttempts = Math.min(reconnectAttempts + 1, 6);
    await reconnectDelay(delayMs);
    return !options.signal?.aborted;
  };

  while (!exited && !options.signal?.aborted) {
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    let reconnectable = true;
    try {
      const resp = await fetchWithAuth(
        API_BASE,
        `${terminalPath(jobId, terminalId)}/stream?after=${cursor}`,
        { headers: { Accept: 'text/event-stream' }, signal: options.signal },
      );
      if (!resp.ok) {
        // A temporarily unavailable host or rate limit must not disable a
        // retained terminal. Resume from the same cursor after the backoff.
        reconnectable = resp.status === 429 || (resp.status >= 500 && resp.status < 600);
        await jsonOrThrow(resp);
        return;
      }
      if (!resp.body) {
        reconnectable = false;
        throw new Error('Terminal stream response has no body');
      }

      reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (!exited) {
        const { done, value } = await reader.read();
        if (done) {
          buffer += decoder.decode();
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const boundaryPattern = /\r?\n\r?\n/;
        let boundary = boundaryPattern.exec(buffer);
        while (boundary) {
          dispatch(buffer.slice(0, boundary.index));
          buffer = buffer.slice(boundary.index + boundary[0].length);
          boundary = boundaryPattern.exec(buffer);
        }
      }
      if (buffer.trim()) dispatch(buffer);
    } catch (cause) {
      if (options.signal?.aborted) return;
      if (!reconnectable) throw cause;
      if (cause instanceof Error && cause.name !== 'AbortError') {
        if (!(await waitToReconnect())) return;
        continue;
      }
      return;
    } finally {
      if (reader) {
        try {
          await reader.cancel();
        } catch {
          // Abort and network failures may already have errored the body.
        }
        reader.releaseLock();
      }
    }

    if (!exited && !options.signal?.aborted && !(await waitToReconnect())) return;
  }
}
