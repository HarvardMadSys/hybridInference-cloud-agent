// A conversation's messages: the one way the page sends a message, and the queue
// of messages written while a turn was still running.
//
// Three rules come from the control plane and shape everything here:
//
// 1. **Sending is one request whatever the conversation is doing.** Idle, the
//    message becomes the next turn at once and the answer carries `job`; busy,
//    it waits in the queue and the answer carries `pending`. The caller never
//    picks a path.
// 2. **Every change to a waiting message names the revision it was read at.**
//    A stale one is refused with `409 revision_conflict`, and the refusal carries
//    the message as it is now (`current`), so the loser of a two-tab race can
//    show what won instead of overwriting it. Nothing here retries a conflict.
// 3. **A send carries an `Idempotency-Key` that outlives a lost response.** The
//    control plane recognises a resend and answers with what the first send
//    made — the turn, or the waiting message — rather than queueing a second.

import { fetchWithAuth, jsonOrThrow } from './client';
import type { AgentJobApi } from './agents';
import { config } from '@/config/env';
import { APIError } from '@/lib/utils/errors';

const API_BASE = config.apiBase;

/** `held`: being edited, or refused by a check (see `last_error`). Nothing goes past it. */
export type PendingMessageState = 'queued' | 'held' | 'sending';

/** Why a queue stopped moving by itself. Only the owner's continue clears it. */
export type QueuePausedReason = 'stopped' | 'failed' | 'archived';

export interface PendingMessageAttachmentApi {
  id: number;
  path: string;
  content_type: string;
  size_bytes: number;
}

export interface PendingMessageApi {
  id: string;
  thread_id: string;
  /** Send order, assigned once. Editing a message never moves it. */
  position: number;
  state: PendingMessageState;
  /** What the next edit or delete must name. */
  revision: number;
  prompt: string;
  skill_ids?: number[] | null;
  attachment_ids?: number[] | null;
  attachments?: PendingMessageAttachmentApi[];
  model?: string | null;
  reasoning_effort?: string | null;
  /** Why the platform held this message instead of sending it. */
  last_error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** What a send or a queue change left behind. */
export interface ThreadMessageResultApi {
  /** The message this request wrote or changed, while it is still waiting. */
  pending: PendingMessageApi | null;
  /** The turn this request started: the message just sent, or an older one that went first. */
  job: AgentJobApi | null;
  queue_paused_reason?: QueuePausedReason | null;
  /**
   * Send-now only: why the live turn cannot have it — `turn_ended`,
   * `unreachable`, … It still waits, and goes when the turn ends. Without one
   * the message is `sending`: it goes in before the model's next tool call, and
   * leaves the queue once the turn's transcript shows it.
   */
  not_delivered_reason?: string | null;
}

export interface ThreadMessageRequest {
  prompt: string;
  /** Omitted inherits the previous turn's skills; `[]` clears them. */
  skill_ids?: number[];
  attachment_ids?: number[];
  model?: string;
  /** The literal `'auto'` resets an inherited effort; omitted inherits it. */
  reasoning_effort?: string;
}

export interface PendingMessageEdit {
  prompt?: string;
  skill_ids?: number[] | null;
  attachment_ids?: number[] | null;
  model?: string | null;
  reasoning_effort?: string | null;
  /** `held` while an editor has it open; `queued` lets it go again. */
  state?: 'held' | 'queued';
}

function threadPath(threadId: string): string {
  return `/v1/agent/threads/${encodeURIComponent(threadId)}`;
}

function messagePath(threadId: string, messageId: string): string {
  return `${threadPath(threadId)}/messages/${encodeURIComponent(messageId)}`;
}

/** The message as it is now, from a `409 revision_conflict`, if the refusal carried it. */
export function conflictCurrent(cause: unknown): PendingMessageApi | null {
  if (!(cause instanceof APIError)) return null;
  const current = cause.details?.current;
  return current && typeof current === 'object' ? (current as PendingMessageApi) : null;
}

/**
 * `jsonOrThrow`, keeping the one detail a queue refusal adds.
 *
 * A conflict's `current` row is what lets the card show what won, and the
 * shared error parser keeps only the type and the message.
 */
async function queueJson<T>(resp: Response): Promise<T> {
  if (resp.ok) return resp.json() as Promise<T>;
  let current: unknown = null;
  try {
    const body = (await resp.clone().json()) as { detail?: { error?: { current?: unknown } } };
    current = body?.detail?.error?.current ?? null;
  } catch {
    // Not JSON: jsonOrThrow classifies it by status.
  }
  try {
    return await jsonOrThrow<T>(resp);
  } catch (cause) {
    if (cause instanceof APIError && current) {
      cause.details = { ...(cause.details ?? {}), current };
    }
    throw cause;
  }
}

/**
 * The key of one pending send, kept until a definitive answer arrives.
 *
 * Scoped to a conversation, and kept in session storage so a reload in the
 * middle of a send cannot turn its retry into a second message. A `409
 * idempotency_conflict` means the key was used for different content — an
 * edited resend after a failure — and that is a new action with a new key.
 */
const pendingKeys = new Map<string, string>();

function keyStorage(scope: string): string {
  return `agent-message-request-${scope}`;
}

function pendingKey(scope: string): string {
  const memory = pendingKeys.get(scope);
  if (memory) return memory;
  try {
    const stored = globalThis.sessionStorage?.getItem(keyStorage(scope));
    if (stored) {
      pendingKeys.set(scope, stored);
      return stored;
    }
  } catch {
    // Blocked storage still leaves in-memory protection for this page.
  }
  const created =
    globalThis.crypto?.randomUUID?.() ??
    `message-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  pendingKeys.set(scope, created);
  try {
    globalThis.sessionStorage?.setItem(keyStorage(scope), created);
  } catch {
    // See above.
  }
  return created;
}

function clearPendingKey(scope: string): void {
  pendingKeys.delete(scope);
  try {
    globalThis.sessionStorage?.removeItem(keyStorage(scope));
  } catch {
    // The answer already arrived; cleanup is best effort.
  }
}

/**
 * Send a message to a conversation: the next turn now, or queued behind the
 * running one. Retrying after a lost response returns what the first send made.
 */
export async function submitThreadMessage(
  threadId: string,
  body: ThreadMessageRequest,
  options: {
    /** A durable coordinator owns this ID and its receipt; this API never rotates it. */
    requestId?: string;
  } = {},
): Promise<ThreadMessageResultApi> {
  const scope = `thread:${threadId}`;
  const send = (key: string) =>
    fetchWithAuth(API_BASE, `${threadPath(threadId)}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify(body),
    });
  let resp = await send(options.requestId ?? pendingKey(scope));
  if (resp.status === 409 && options.requestId === undefined) {
    const type = await errorType(resp);
    if (type === 'idempotency_conflict') {
      clearPendingKey(scope);
      resp = await send(pendingKey(scope));
    }
  }
  if (!resp.ok) {
    // Any other failure keeps the key: the send may have landed, and the retry
    // is how that is found out.
    return queueJson<ThreadMessageResultApi>(resp);
  }
  const result = await queueJson<ThreadMessageResultApi>(resp);
  if (options.requestId === undefined) clearPendingKey(scope);
  return result;
}

async function errorType(resp: Response): Promise<string | null> {
  try {
    const body = (await resp.clone().json()) as { detail?: { error?: { type?: unknown } } };
    const type = body?.detail?.error?.type;
    return typeof type === 'string' ? type : null;
  } catch {
    return null;
  }
}

/** Change a waiting message, at the revision it was read at. */
export async function editPendingMessage(
  threadId: string,
  messageId: string,
  revision: number,
  changes: PendingMessageEdit,
): Promise<ThreadMessageResultApi> {
  const resp = await fetchWithAuth(API_BASE, messagePath(threadId, messageId), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision, ...changes }),
  });
  return queueJson<ThreadMessageResultApi>(resp);
}

/** Withdraw a waiting message; the uploads it held go back to being staged. */
export async function deletePendingMessage(
  threadId: string,
  messageId: string,
  revision: number,
): Promise<ThreadMessageResultApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `${messagePath(threadId, messageId)}?revision=${encodeURIComponent(String(revision))}`,
    { method: 'DELETE' },
  );
  return queueJson<ThreadMessageResultApi>(resp);
}

/**
 * Send a waiting message into the running turn now, at the revision it was
 * read at. With nothing running this is an ordinary send.
 */
export async function sendPendingMessageNow(
  threadId: string,
  messageId: string,
  revision: number,
): Promise<ThreadMessageResultApi> {
  const resp = await fetchWithAuth(API_BASE, `${messagePath(threadId, messageId)}/send-now`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision }),
  });
  return queueJson<ThreadMessageResultApi>(resp);
}

/** Let a paused queue move again, starting from its first message. */
export async function resumeThreadQueue(threadId: string): Promise<ThreadMessageResultApi> {
  const resp = await fetchWithAuth(API_BASE, `${threadPath(threadId)}/queue/resume`, {
    method: 'POST',
  });
  return queueJson<ThreadMessageResultApi>(resp);
}

/** The queue as the server has it, and the conversation's newest turn. */
export interface ThreadQueueApi {
  thread_id: string;
  pending_messages: PendingMessageApi[];
  queue_paused_reason: QueuePausedReason | null;
  latest_job_id: string | null;
  latest_turn_no: number | null;
  latest_job_state: string | null;
  /** Whether a waiting message can be sent into the newest turn while it runs. */
  mid_turn_input_supported?: boolean;
}

/** Read the queue without the transcript; this is what a page polls. */
export async function getThreadQueue(
  threadId: string,
  signal?: AbortSignal,
): Promise<ThreadQueueApi> {
  const resp = await fetchWithAuth(API_BASE, `${threadPath(threadId)}/messages`, { signal });
  return queueJson<ThreadQueueApi>(resp);
}
