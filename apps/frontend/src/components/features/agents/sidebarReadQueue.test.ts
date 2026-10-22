import { expect, it, vi } from 'vitest';
import { createSidebarReadQueue, type ConversationReader } from './sidebarReadQueue';

it('bounds a legacy 20-folder resync to two concurrent reads while completing every folder', async () => {
  const finish: (() => void)[] = [];
  let active = 0;
  let peak = 0;
  const read = vi.fn<ConversationReader>(
    () =>
      new Promise((resolve) => {
        active += 1;
        peak = Math.max(peak, active);
        finish.push(() => {
          active -= 1;
          resolve({ conversations: [], next_cursor: null });
        });
      }),
  );
  const queued = createSidebarReadQueue(read);
  const pending = Array.from({ length: 20 }, (_, i) => queued({ repo: `repo-${i}` }));
  await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  for (let i = 0; i < 20; i += 1) {
    await vi.waitFor(() => expect(finish[i]).toBeTypeOf('function'));
    finish[i]();
  }
  await Promise.all(pending);
  expect(read).toHaveBeenCalledTimes(20);
  expect(peak).toBe(2);
});

it('never starts cancelled queued reads after navigation or an owner switch', async () => {
  let finish!: () => void;
  const held = new Promise<Awaited<ReturnType<ConversationReader>>>((resolve) => {
    finish = () => resolve({ conversations: [], next_cursor: null });
  });
  const read = vi.fn<ConversationReader>(() => held);
  const queued = createSidebarReadQueue(read);
  const first = queued({ repo: 'a' });
  const second = queued({ repo: 'b' });
  const controller = new AbortController();
  const cancelled = expect(
    queued({ repo: 'previous-owner' }, controller.signal),
  ).rejects.toMatchObject({ name: 'AbortError' });
  controller.abort();
  finish();
  await Promise.all([first, second, cancelled]);
  expect(read).toHaveBeenCalledTimes(2);
});
