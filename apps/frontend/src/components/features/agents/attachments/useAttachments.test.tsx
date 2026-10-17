// @vitest-environment jsdom
import { StrictMode, type ReactNode } from 'react';
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { deleteAttachment, uploadAttachment, type AttachmentApi } from '@/lib/api/attachments';
import { DEFAULT_ATTACHMENT_LIMITS } from '@/lib/attachmentLimits';
import type { Translator } from '@/lib/i18n/translate';
import { useAttachments } from './useAttachments';

vi.mock('@/lib/api/attachments', () => ({
  uploadAttachment: vi.fn(),
  deleteAttachment: vi.fn(),
}));

const t: Translator = (key) => key;
const limits = { ...DEFAULT_ATTACHMENT_LIMITS, max_files: 1 };
const wrapper = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;

function deferredUpload() {
  let resolve!: (saved: AttachmentApi) => void;
  let reject!: (cause: Error) => void;
  const promise = new Promise<AttachmentApi>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function file(name: string, size = 4) {
  return new File(['x'.repeat(size)], name);
}

function saved(id: number): AttachmentApi {
  return { id, path: 'file.txt', size_bytes: 4, content_type: 'text/plain' };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(deleteAttachment).mockResolvedValue(undefined);
});

afterEach(cleanup);

describe('useAttachments reservation ownership', () => {
  it.each(['remove', 'reset'] as const)(
    'keeps file and byte limits after %s and a late rejected upload',
    async (discard) => {
      for (const bounds of [
        { ...limits, max_total_bytes: 100 },
        { ...limits, max_files: 10, max_total_bytes: 4 },
      ]) {
        const removed = deferredUpload();
        const current = deferredUpload();
        vi.mocked(uploadAttachment)
          .mockReset()
          .mockReturnValueOnce(removed.promise)
          .mockReturnValueOnce(current.promise);
        const { result, unmount } = renderHook(() => useAttachments(t, bounds), { wrapper });

        act(() => result.current.add([file('removed.txt')]));
        act(() => {
          if (discard === 'remove') result.current.remove(result.current.entries[0]!.key);
          else result.current.reset();
          result.current.add([file('current.txt')]);
        });
        await act(async () => removed.reject(new Error('late failure')));
        act(() => result.current.add([file('over-limit.txt')]));

        expect(uploadAttachment).toHaveBeenCalledTimes(2);
        expect(result.current.entries).toMatchObject([
          { path: 'current.txt', status: 'uploading' },
          {
            path: 'over-limit.txt',
            status: 'rejected',
            error: bounds.max_files === 1 ? 'attach.errorTooMany' : 'attach.errorTotalTooLarge',
          },
        ]);
        expect(result.current.count).toBe(1);
        expect(result.current.totalBytes).toBe(4);
        await act(async () => current.resolve(saved(2)));
        expect(result.current.ids).toEqual([2]);
        unmount();
      }
    },
  );

  it.each(['remove', 'reset'] as const)(
    'deletes a late successful upload after %s without reviving its entry',
    async (discard) => {
      const removed = deferredUpload();
      const current = deferredUpload();
      vi.mocked(uploadAttachment)
        .mockReturnValueOnce(removed.promise)
        .mockReturnValueOnce(current.promise);
      vi.mocked(deleteAttachment).mockRejectedValueOnce(new Error('cleanup unavailable'));
      const { result } = renderHook(() => useAttachments(t, limits), { wrapper });

      act(() => result.current.add([file('removed.txt')]));
      act(() => {
        if (discard === 'remove') result.current.remove(result.current.entries[0]!.key);
        else result.current.reset();
        result.current.add([file('current.txt')]);
      });
      await act(async () => removed.resolve(saved(1)));
      await act(async () => current.resolve(saved(2)));

      expect(deleteAttachment).toHaveBeenCalledExactlyOnceWith(1);
      expect(result.current.entries).toMatchObject([
        { path: 'current.txt', status: 'ready', id: 2 },
      ]);
      expect(result.current.ids).toEqual([2]);
      expect(result.current.count).toBe(1);
      expect(result.current.totalBytes).toBe(4);
      expect(result.current.uploading).toBe(false);
      act(() => result.current.add([file('over-limit.txt')]));
      expect(uploadAttachment).toHaveBeenCalledTimes(2);
    },
  );

  it('preserves a visible failure and admits a retry with the released slot', async () => {
    const first = deferredUpload();
    const retry = deferredUpload();
    vi.mocked(uploadAttachment)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(retry.promise);
    const { result } = renderHook(() => useAttachments(t, limits), { wrapper });

    act(() => result.current.add([file('first.txt'), file('too-many.txt')]));
    expect(uploadAttachment).toHaveBeenCalledTimes(1);
    await act(async () => first.reject(new Error('server refused upload')));
    expect(result.current.entries[0]).toMatchObject({
      path: 'first.txt',
      status: 'rejected',
      error: 'server refused upload',
    });
    expect(result.current.count).toBe(0);
    expect(result.current.totalBytes).toBe(0);
    expect(result.current.uploading).toBe(false);

    act(() => {
      result.current.remove(result.current.entries[0]!.key);
      result.current.add([file('retry.txt'), file('still-too-many.txt')]);
    });
    expect(uploadAttachment).toHaveBeenCalledTimes(2);
    expect(result.current.entries.at(-1)).toMatchObject({
      path: 'still-too-many.txt',
      status: 'rejected',
      error: 'attach.errorTooMany',
    });
    await act(async () => retry.resolve(saved(7)));
    expect(result.current.ids).toEqual([7]);
  });

  it('removes a ready upload once in StrictMode and preserves admission limits', async () => {
    vi.mocked(uploadAttachment).mockResolvedValueOnce(saved(1)).mockResolvedValueOnce(saved(2));
    const { result } = renderHook(() => useAttachments(t, limits), { wrapper });

    await act(async () => result.current.add([file('first.txt')]));
    const key = result.current.entries[0]!.key;
    act(() => result.current.remove(key));
    await act(async () => {
      result.current.remove(key);
      result.current.add([file('second.txt'), file('too-many.txt')]);
    });

    expect(deleteAttachment).toHaveBeenCalledExactlyOnceWith(1);
    expect(uploadAttachment).toHaveBeenCalledTimes(2);
    expect(result.current.ids).toEqual([2]);
    expect(result.current.count).toBe(1);
    expect(result.current.entries.at(-1)).toMatchObject({ status: 'rejected' });
  });

  it('keeps already ready uploads when resetting after submission', async () => {
    vi.mocked(uploadAttachment).mockResolvedValueOnce(saved(1));
    const { result } = renderHook(() => useAttachments(t, limits), { wrapper });

    await act(async () => result.current.add([file('claimed.txt')]));
    act(() => result.current.reset());

    expect(deleteAttachment).not.toHaveBeenCalled();
    expect(result.current.entries).toEqual([]);
    expect(result.current.count).toBe(0);
    expect(result.current.totalBytes).toBe(0);
  });
});

describe('uploads a composer leaves behind', () => {
  it('deletes ready and late uploads that were never sent', async () => {
    const late = deferredUpload();
    vi.mocked(uploadAttachment).mockResolvedValueOnce(saved(1)).mockReturnValueOnce(late.promise);
    const bounds = { ...limits, max_files: 10 };
    const { result, unmount } = renderHook(() => useAttachments(t, bounds), { wrapper });
    await act(async () => result.current.add([file('ready.txt')]));
    act(() => result.current.add([file('late.txt')]));

    unmount();
    await act(async () => late.resolve(saved(2)));

    expect(
      vi
        .mocked(deleteAttachment)
        .mock.calls.map(([id]) => id)
        .sort(),
    ).toEqual([1, 2]);
  });

  it.each([true, false])(
    'waits for a submission in flight, and keeps what it delivered (accepted: %s)',
    async (accepted) => {
      vi.mocked(uploadAttachment).mockResolvedValueOnce(saved(1));
      const { result, unmount } = renderHook(() => useAttachments(t, limits), { wrapper });
      await act(async () => result.current.add([file('sent.txt')]));
      let answer!: { resolve: () => void; reject: (cause: Error) => void };
      const submission = new Promise<void>((resolve, reject) => (answer = { resolve, reject }));
      const sent = result.current.whileSending(submission).catch(() => {});

      unmount();
      await act(async () => Promise.resolve());
      expect(deleteAttachment).not.toHaveBeenCalled();
      await act(async () => {
        if (accepted) answer.resolve();
        else answer.reject(new Error('refused'));
        await sent;
      });

      if (accepted) expect(deleteAttachment).not.toHaveBeenCalled();
      else expect(deleteAttachment).toHaveBeenCalledExactlyOnceWith(1);
    },
  );

  it.each([
    ['a closed page', false, false, true],
    ['a page kept for Back', true, false, false],
    ['a page closed mid-submission', false, true, false],
  ] as const)('on %s', async (_name, persisted, submitting, deletes) => {
    vi.mocked(uploadAttachment).mockResolvedValueOnce(saved(1));
    const { result } = renderHook(() => useAttachments(t, limits), { wrapper });
    await act(async () => result.current.add([file('draft.txt')]));
    if (submitting) void result.current.whileSending(new Promise(() => {}));

    const event = new Event('pagehide') as PageTransitionEvent;
    Object.defineProperty(event, 'persisted', { value: persisted });
    window.dispatchEvent(event);

    if (deletes) expect(deleteAttachment).toHaveBeenCalledExactlyOnceWith(1, { keepalive: true });
    else expect(deleteAttachment).not.toHaveBeenCalled();
  });
});
