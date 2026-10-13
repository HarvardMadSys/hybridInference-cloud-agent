'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';

export type TranscriptPresentation = 'compact' | 'standard' | 'detailed';

export const transcriptPresentationKey = (userId: string, threadId: string) =>
  `agents.transcript.presentation:${encodeURIComponent(userId)}:${encodeURIComponent(threadId)}`;

function parsePresentation(value: string | null): TranscriptPresentation {
  return value === 'compact' || value === 'detailed' ? value : 'standard';
}

/** An account's preference belongs to this conversation, including all its turns. */
export function useTranscriptPresentation(userId: string | undefined, threadId: string) {
  const key = userId ? transcriptPresentationKey(userId, threadId) : null;
  const owner = useRef(key);
  owner.current = key;
  const [saved, setSaved] = useState<{ key: string | null; mode: TranscriptPresentation }>({
    key: null,
    mode: 'standard',
  });

  useEffect(() => {
    const read = () => {
      let mode: TranscriptPresentation = 'standard';
      try {
        if (key) mode = parsePresentation(localStorage.getItem(key));
      } catch {
        // Storage is optional; the current conversation remains usable.
      }
      setSaved({ key, mode });
    };
    read();
    const onStorage = (event: StorageEvent) => {
      if (owner.current === key && key && (event.key === key || event.key === null)) read();
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [key]);

  const update = useCallback(
    (mode: TranscriptPresentation) => {
      if (owner.current !== key) return;
      if (key) {
        try {
          localStorage.setItem(key, mode);
        } catch {
          // A blocked write must not block the user's display choice.
        }
      }
      setSaved({ key, mode });
    },
    [key],
  );

  return [saved.key === key ? saved.mode : 'standard', update] as const;
}

export function TranscriptPresentationControl({
  value,
  onChange,
}: {
  value: TranscriptPresentation;
  onChange: (value: TranscriptPresentation) => void;
}) {
  const t = useT();
  return (
    <label className="inline-flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
      <span className="hidden sm:inline">{t('transcript.details')}</span>
      <select
        aria-label={t('transcript.details')}
        value={value}
        onChange={(event) => onChange(parsePresentation(event.target.value))}
        className="ui-input ui-focus-ring min-h-9 rounded-md px-2 py-1 text-xs [@media(pointer:coarse)]:min-h-11"
      >
        <option value="compact">{t('transcript.compact')}</option>
        <option value="standard">{t('transcript.standard')}</option>
        <option value="detailed">{t('transcript.detailed')}</option>
      </select>
    </label>
  );
}
