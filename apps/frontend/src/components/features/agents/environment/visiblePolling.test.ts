// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { pollWhileVisible } from './visiblePolling';

beforeEach(() => {
  vi.useFakeTimers();
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
});
afterEach(() => vi.useRealTimers());

it('waits for a slow request before scheduling another', async () => {
  let finish!: (delay: number) => void;
  const read = vi.fn(
    () =>
      new Promise<number>((resolve) => {
        finish = resolve;
      }),
  );
  const stop = pollWhileVisible(read);
  await vi.advanceTimersByTimeAsync(60000);
  expect(read).toHaveBeenCalledTimes(1);
  finish(5000);
  await vi.advanceTimersByTimeAsync(4999);
  expect(read).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(read).toHaveBeenCalledTimes(2);
  stop();
});

it('cancels hidden work, stays quiet, then reconciles immediately on return', async () => {
  const signals: AbortSignal[] = [];
  const read = vi.fn((signal: AbortSignal) => {
    signals.push(signal);
    return new Promise<number>((resolve) => signal.addEventListener('abort', () => resolve(5000)));
  });
  const stop = pollWhileVisible(read);
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  document.dispatchEvent(new Event('visibilitychange'));
  expect(signals[0].aborted).toBe(true);
  await vi.advanceTimersByTimeAsync(60000);
  expect(read).toHaveBeenCalledTimes(1);
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  document.dispatchEvent(new Event('visibilitychange'));
  expect(read).toHaveBeenCalledTimes(2);
  stop();
  expect(signals[1].aborted).toBe(true);
});

it('uses the returned settled delay without stopping reconciliation forever', async () => {
  const read = vi.fn().mockResolvedValue(30000);
  const stop = pollWhileVisible(read);
  await vi.advanceTimersByTimeAsync(29999);
  expect(read).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(read).toHaveBeenCalledTimes(2);
  stop();
  await vi.advanceTimersByTimeAsync(60000);
  expect(read).toHaveBeenCalledTimes(2);
});
