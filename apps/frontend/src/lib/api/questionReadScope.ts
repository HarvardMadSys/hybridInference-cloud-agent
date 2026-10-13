import { listThreadQuestions, type AgentQuestionsApi } from './questions';

export const QUESTION_READ_TTL_MS = 500;

export interface QuestionReadScope {
  listThreadQuestions: (threadId: string, signal?: AbortSignal) => Promise<AgentQuestionsApi>;
  /** Drop cached snapshots and fence pending reads after a turn change or answer. */
  invalidate: (threadId?: string) => void;
}

interface QuestionRead {
  promise: Promise<AgentQuestionsApi>;
  controller: AbortController;
  settledAt: number | null;
}

/** A caller leaving the page cancels its wait without cancelling another reader. */
function withAbort<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted)
    return Promise.reject(new DOMException('The read was cancelled.', 'AbortError'));
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(new DOMException('The read was cancelled.', 'AbortError'));
    signal.addEventListener('abort', abort, { once: true });
    void promise.then(
      (value) => {
        signal.removeEventListener('abort', abort);
        resolve(value);
      },
      (cause) => {
        signal.removeEventListener('abort', abort);
        reject(cause);
      },
    );
  });
}

/** Each owner's shell shares pending reads and briefly shares settled poll results. */
export function createQuestionReadScope(): QuestionReadScope {
  const reads = new Map<string, QuestionRead>();
  return {
    listThreadQuestions: (threadId, signal) => {
      if (signal?.aborted) {
        return Promise.reject(new DOMException('The read was cancelled.', 'AbortError'));
      }
      const now = Date.now();
      for (const [id, held] of reads) {
        if (held.settledAt !== null && now - held.settledAt >= QUESTION_READ_TTL_MS)
          reads.delete(id);
      }
      let held = reads.get(threadId);
      if (!held) {
        const controller = new AbortController();
        // Fence the promise too: an API adapter that ignores cancellation
        // must not deliver an invalidated snapshot to any remaining reader.
        const promise = withAbort(
          listThreadQuestions(threadId, controller.signal),
          controller.signal,
        );
        held = { promise, controller, settledAt: null };
        reads.set(threadId, held);
        const entry = held;
        void promise.then(
          () => {
            if (reads.get(threadId) === entry) entry.settledAt = Date.now();
          },
          () => {
            if (reads.get(threadId) === entry) reads.delete(threadId);
          },
        );
      }
      return withAbort(held.promise, signal);
    },
    invalidate: (threadId) => {
      if (threadId === undefined) {
        for (const held of reads.values()) held.controller.abort();
        reads.clear();
      } else {
        reads.get(threadId)?.controller.abort();
        reads.delete(threadId);
      }
    },
  };
}
