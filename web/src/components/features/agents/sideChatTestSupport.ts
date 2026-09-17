import { vi } from 'vitest';

/** Model the browser's named, cross-context exclusive lock in jsdom. */
export function installSideChatLocks() {
  const queues = new Map<string, Promise<unknown>>();
  const request = vi.fn((name: string, action: () => unknown) => {
    const result = (queues.get(name) ?? Promise.resolve()).then(action);
    queues.set(
      name,
      result.catch(() => {}),
    );
    return result;
  });
  Object.defineProperty(navigator, 'locks', { configurable: true, value: { request } });
  return request;
}
