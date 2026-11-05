import { listAgentConversations } from '@/lib/api/agents';

export type ConversationReader = typeof listAgentConversations;
type Query = Parameters<ConversationReader>[0];
type Page = Awaited<ReturnType<ConversationReader>>;
type Read = {
  query: Query;
  signal?: AbortSignal;
  resolve: (page: Page) => void;
  reject: (cause: unknown) => void;
  detach: () => void;
};

/** Bound initial loads and legacy/full resyncs without discarding any loaded depth. */
export function createSidebarReadQueue(read: ConversationReader = listAgentConversations) {
  const waiting: Read[] = [];
  let active = 0;
  function drain() {
    while (active < 2 && waiting.length) {
      const next = waiting.shift()!;
      if (next.signal?.aborted) {
        next.detach();
        continue;
      }
      active += 1;
      void Promise.resolve()
        .then(() => read(next.query, next.signal))
        .then(next.resolve, next.reject)
        .finally(() => {
          next.detach();
          active -= 1;
          drain();
        });
    }
  }
  const enqueue: ConversationReader = (query, signal) =>
    new Promise<Page>((resolve, reject) => {
      const abort = () => reject(new DOMException('Aborted', 'AbortError'));
      if (signal?.aborted) return abort();
      signal?.addEventListener('abort', abort, { once: true });
      waiting.push({
        query,
        signal,
        resolve,
        reject,
        detach: () => signal?.removeEventListener('abort', abort),
      });
      drain();
    });
  return enqueue;
}
