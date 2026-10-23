import { listThreadQuestions, type AgentQuestionsApi } from './questions';

export const QUESTION_READ_TTL_MS = 500;

/** How often one consumer needs a conversation's questions read. */
export interface QuestionPace {
  /** The longest this consumer may wait between two reads. */
  intervalMs: number;
  /**
   * Keep reading while the tab is hidden. Only a consumer that acts for an
   * owner who is not looking, such as a desktop notification, needs to; one
   * that only draws on the page waits until the tab is shown again.
   */
  whileHidden?: boolean;
}

export interface QuestionReadScope {
  listThreadQuestions: (threadId: string, signal?: AbortSignal) => Promise<AgentQuestionsApi>;
  /** Drop cached snapshots and fence pending reads after a turn change or answer. */
  invalidate: (threadId?: string) => void;
  /**
   * Call `read` on this tab's one clock for `threadId`: now, then on every
   * tick, while the consumer is due. The clock ticks as often as its most
   * frequent due consumer needs, and every due consumer reads on each tick,
   * so they join one request. Returns the function that leaves the clock.
   */
  watch: (threadId: string, pace: QuestionPace, read: () => void) => () => void;
}

interface QuestionRead {
  promise: Promise<AgentQuestionsApi>;
  controller: AbortController;
  settledAt: number | null;
}

interface QuestionWatcher extends QuestionPace {
  read: () => void;
}

interface QuestionClock {
  watchers: Set<QuestionWatcher>;
  intervalMs: number | null;
  timer: ReturnType<typeof setInterval> | null;
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

function tabHidden(): boolean {
  return document.visibilityState === 'hidden';
}

function isDue(watcher: QuestionWatcher): boolean {
  return watcher.whileHidden === true || !tabHidden();
}

/**
 * One reference-counted clock per conversation, and the one place that pauses
 * them while the tab is hidden. A clock exists while a consumer watches its
 * conversation and stops with the last one.
 */
export function createQuestionClocks(): Pick<QuestionReadScope, 'watch'> {
  const clocks = new Map<string, QuestionClock>();

  function tick(clock: QuestionClock): void {
    for (const watcher of [...clock.watchers]) {
      if (clock.watchers.has(watcher) && isDue(watcher)) watcher.read();
    }
  }

  /** Tick as often as the most frequent due consumer needs; not at all if none is due. */
  function schedule(clock: QuestionClock): void {
    let intervalMs: number | null = null;
    for (const watcher of clock.watchers) {
      if (isDue(watcher)) intervalMs = Math.min(intervalMs ?? Infinity, watcher.intervalMs);
    }
    if (intervalMs === clock.intervalMs) return;
    if (clock.timer !== null) clearInterval(clock.timer);
    clock.intervalMs = intervalMs;
    clock.timer = intervalMs === null ? null : setInterval(() => tick(clock), intervalMs);
  }

  function onVisibilityChange(): void {
    for (const clock of [...clocks.values()]) {
      schedule(clock);
      if (tabHidden()) continue;
      // What the page shows paused while the tab was hidden: catch it up now.
      for (const watcher of [...clock.watchers]) {
        if (clock.watchers.has(watcher) && !watcher.whileHidden) watcher.read();
      }
    }
  }

  return {
    watch: (threadId, { intervalMs, whileHidden }, read) => {
      const watcher: QuestionWatcher = { intervalMs, whileHidden, read };
      let clock = clocks.get(threadId);
      if (!clock) {
        clock = { watchers: new Set(), intervalMs: null, timer: null };
        if (clocks.size === 0) document.addEventListener('visibilitychange', onVisibilityChange);
        clocks.set(threadId, clock);
      }
      const held = clock;
      held.watchers.add(watcher);
      schedule(held);
      if (isDue(watcher)) read();
      return () => {
        if (!held.watchers.delete(watcher)) return;
        schedule(held);
        if (held.watchers.size > 0) return;
        clocks.delete(threadId);
        if (clocks.size === 0) document.removeEventListener('visibilitychange', onVisibilityChange);
      };
    },
  };
}

/**
 * Each owner's shell shares pending reads, briefly shares settled poll
 * results, and reads each conversation on one clock however many consumers
 * show it.
 */
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
    ...createQuestionClocks(),
  };
}
