import { jsonOrThrow } from '../client';
import type { AgentJobApi } from './jobs';

/** Read typed failures without consuming the response used by jsonOrThrow. */
export async function responseErrorType(resp: Response): Promise<string | null> {
  if (resp.ok) return null;
  try {
    const body = (await resp.clone().json()) as {
      error?: { type?: unknown };
      detail?: { error?: { type?: unknown } };
    };
    const type = body.error?.type ?? body.detail?.error?.type;
    return typeof type === 'string' ? type : null;
  } catch {
    return null;
  }
}

/**
 * A submission is one user action, however many times its request is sent.
 *
 * The control plane recognises a resent create by its `Idempotency-Key` and
 * answers with the job the first send made instead of queueing a second task.
 * (A message to an existing conversation carries its own key the same way; see
 * `submitThreadMessage`.) So the key must outlive a lost response: it is
 * minted per pending submission scope, kept in session storage across a
 * reload, and dropped only once a
 * definitive answer arrives. A `409 idempotency_conflict` means the key was
 * reused for different content — an edited resubmit after a failure — and
 * that is a new action, so the key rotates and the request is sent once more.
 */
const pendingSubmissionIds = new Map<string, string>();

function submissionStorageKey(scope: string): string {
  return `agent-submission-request-${scope}`;
}

function pendingSubmissionId(scope: string): string {
  const memory = pendingSubmissionIds.get(scope);
  if (memory) return memory;
  try {
    const stored = globalThis.sessionStorage?.getItem(submissionStorageKey(scope));
    if (stored) {
      pendingSubmissionIds.set(scope, stored);
      return stored;
    }
  } catch {
    // Privacy settings may block browser storage; in-memory idempotency still
    // protects repeated clicks for the lifetime of this page.
  }
  const created =
    globalThis.crypto?.randomUUID?.() ??
    `submit-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  pendingSubmissionIds.set(scope, created);
  try {
    globalThis.sessionStorage?.setItem(submissionStorageKey(scope), created);
  } catch {
    // See the read path above.
  }
  return created;
}

function clearPendingSubmission(scope: string, requestId: string): void {
  // An older response may arrive after a different submission has rotated
  // this scope's key. It can retire only the request it acknowledges.
  if (pendingSubmissionIds.get(scope) !== requestId) return;
  pendingSubmissionIds.delete(scope);
  try {
    const key = submissionStorageKey(scope);
    if (globalThis.sessionStorage?.getItem(key) === requestId) {
      globalThis.sessionStorage.removeItem(key);
    }
  } catch {
    // A definitive response is already authoritative; cleanup is best effort.
  }
}

export async function submitOnce(
  scope: string,
  send: (requestId: string) => Promise<Response>,
): Promise<AgentJobApi> {
  let requestId = pendingSubmissionId(scope);
  let resp = await send(requestId);
  if (resp.status === 409 && (await responseErrorType(resp)) === 'idempotency_conflict') {
    // Same key, different content: the earlier send was a different action
    // and is not what the user is submitting now.
    clearPendingSubmission(scope, requestId);
    requestId = pendingSubmissionId(scope);
    resp = await send(requestId);
  }
  if (!resp.ok) {
    // Every other failure keeps the key: a lost or ambiguous response may
    // hide a committed job, and the retry is how it is recovered.
    return jsonOrThrow<AgentJobApi>(resp);
  }
  const job = await jsonOrThrow<AgentJobApi>(resp);
  clearPendingSubmission(scope, requestId);
  return job;
}
