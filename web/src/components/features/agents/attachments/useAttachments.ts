'use client';

import { useCallback, useRef, useState } from 'react';
import {
  DEFAULT_ATTACHMENT_LIMITS,
  formatBytes,
  type AttachmentLimits,
} from '@/lib/attachmentLimits';
import { isSafeUploadPath, uploadPathFor } from '@/lib/attachmentPath';
import { deleteAttachment, uploadAttachment } from '@/lib/api/attachments';
import { getErrorMessage } from '@/lib/utils/errors';
import type { Translator } from '@/lib/i18n/translate';

/**
 * One picked file's life in the composer.
 *
 * A rejected entry stays in the list rather than being dropped silently: the
 * user picked it, so the answer to "where did my file go" has to be on screen,
 * next to the file, with the reason. It contributes nothing to the totals and
 * is never sent.
 */
export interface AttachmentEntry {
  key: string;
  path: string;
  size: number;
  status: 'uploading' | 'ready' | 'rejected';
  id?: number;
  error?: string;
}

/** Counts and bytes that a new file has to fit into. */
function accepted(entries: AttachmentEntry[]): AttachmentEntry[] {
  return entries.filter((entry) => entry.status !== 'rejected');
}

export function useAttachments(
  t: Translator,
  limits: AttachmentLimits = DEFAULT_ATTACHMENT_LIMITS,
) {
  const [entries, setEntries] = useState<AttachmentEntry[]>([]);
  const nextKey = useRef(0);
  // Admission is decided against a running tally rather than against `entries`,
  // because a multi-file pick queues its uploads before any of them has landed
  // in state — reading state per file would admit every file in one selection
  // and only then notice the total was blown.
  const tally = useRef({ count: 0, bytes: 0 });

  const add = useCallback(
    (files: File[]) => {
      if (files.length === 0) return;
      const queued: AttachmentEntry[] = [];
      for (const file of files) {
        const key = `a${nextKey.current++}`;
        const path = uploadPathFor(file);
        const entry: AttachmentEntry = { key, path, size: file.size, status: 'uploading' };
        if (!isSafeUploadPath(path, limits.max_path_length)) {
          queued.push({ ...entry, status: 'rejected', error: t('attach.errorPath') });
          continue;
        }
        if (file.size > limits.max_file_bytes) {
          queued.push({
            ...entry,
            status: 'rejected',
            error: t('attach.errorFileTooLarge', { limit: formatBytes(limits.max_file_bytes) }),
          });
          continue;
        }
        if (tally.current.count + 1 > limits.max_files) {
          queued.push({
            ...entry,
            status: 'rejected',
            error: t('attach.errorTooMany', { limit: limits.max_files }),
          });
          continue;
        }
        if (tally.current.bytes + file.size > limits.max_total_bytes) {
          queued.push({
            ...entry,
            status: 'rejected',
            error: t('attach.errorTotalTooLarge', { limit: formatBytes(limits.max_total_bytes) }),
          });
          continue;
        }
        tally.current.count += 1;
        tally.current.bytes += file.size;
        queued.push(entry);
        void uploadAttachment(file, path)
          .then((saved) => {
            setEntries((old) =>
              old.map((row) => (row.key === key ? { ...row, status: 'ready', id: saved.id } : row)),
            );
          })
          .catch((cause: unknown) => {
            // The server refused it after all, so give the slot back — the
            // tally has to describe what is actually attached, or the next
            // pick is admitted against bytes nobody is holding.
            tally.current.count -= 1;
            tally.current.bytes -= file.size;
            setEntries((old) =>
              old.map((row) =>
                row.key === key
                  ? { ...row, status: 'rejected', error: getErrorMessage(cause) }
                  : row,
              ),
            );
          });
      }
      setEntries((old) => [...old, ...queued]);
    },
    [limits, t],
  );

  const remove = useCallback((key: string) => {
    setEntries((old) => {
      const going = old.find((entry) => entry.key === key);
      if (going && going.status !== 'rejected') {
        tally.current.count -= 1;
        tally.current.bytes -= going.size;
      }
      // Best effort: a staged upload nobody claims is collected by the control
      // plane anyway, so a failed delete costs nothing the user can see.
      if (going?.id !== undefined) void deleteAttachment(going.id).catch(() => {});
      return old.filter((entry) => entry.key !== key);
    });
  }, []);

  const reset = useCallback(() => {
    tally.current = { count: 0, bytes: 0 };
    setEntries([]);
  }, []);

  const live = accepted(entries);
  return {
    entries,
    add,
    remove,
    reset,
    /** What the create request sends. Empty until every upload has landed. */
    ids: live.flatMap((entry) => (entry.id === undefined ? [] : [entry.id])),
    uploading: live.some((entry) => entry.status === 'uploading'),
    count: live.length,
    totalBytes: live.reduce((sum, entry) => sum + entry.size, 0),
  };
}
