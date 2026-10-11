'use client';

import { useCallback, useReducer, useRef, type Dispatch, type SetStateAction } from 'react';

export interface ComposerDraftSnapshot {
  readonly scope: string;
  readonly value: string;
  readonly revision: number;
}

interface DraftEntry {
  snapshot: ComposerDraftSnapshot;
  storageKey: string | null;
  persisted: boolean;
}

export function composerDraftStorageKey(userId: string, threadId: string): string {
  return `agents.composer.draft.${encodeURIComponent(userId)}:${encodeURIComponent(threadId)}`;
}

function readDraft(storageKey: string | null): {
  value: string;
  revision: number;
  persisted: boolean;
} {
  if (!storageKey) return { value: '', revision: 0, persisted: false };
  try {
    const raw = localStorage.getItem(storageKey);
    const draft: unknown = raw ? JSON.parse(raw) : null;
    if (
      draft &&
      typeof draft === 'object' &&
      'version' in draft &&
      draft.version === 1 &&
      'value' in draft &&
      typeof draft.value === 'string' &&
      'revision' in draft &&
      typeof draft.revision === 'number' &&
      Number.isSafeInteger(draft.revision) &&
      draft.revision >= 0
    ) {
      return { value: draft.value, revision: draft.revision, persisted: true };
    }
    return { value: '', revision: 0, persisted: true };
  } catch {
    return { value: '', revision: 0, persisted: false };
  }
}

function persist(entry: DraftEntry): void {
  if (!entry.storageKey) return;
  try {
    // Keep the revision even when empty: an older request must not clear a new
    // draft that happens to have the same text after this composer remounts.
    localStorage.setItem(
      entry.storageKey,
      JSON.stringify({
        version: 1,
        value: entry.snapshot.value,
        revision: entry.snapshot.revision,
      }),
    );
    entry.persisted = true;
  } catch {
    entry.persisted = false;
  }
}

/** Text only. Uploads keep their existing lifecycle outside the draft store. */
export function useComposerDraft({ userId, threadId }: { userId?: string; threadId: string }) {
  const entries = useRef(new Map<string, DraftEntry>());
  const [, rerender] = useReducer((revision: number) => revision + 1, 0);
  const storageKey = userId ? composerDraftStorageKey(userId, threadId) : null;
  const scope = storageKey ?? `anonymous:${encodeURIComponent(threadId)}`;
  const activeScope = useRef(scope);
  activeScope.current = scope;

  let entry = entries.current.get(scope);
  if (!entry) {
    const saved = readDraft(storageKey);
    entry = {
      snapshot: { scope, value: saved.value, revision: saved.revision },
      storageKey,
      persisted: saved.persisted,
    };
    entries.current.set(scope, entry);
  }

  const setValue: Dispatch<SetStateAction<string>> = useCallback(
    (next) => {
      const current = entries.current.get(scope);
      if (!current) return;
      const value = typeof next === 'function' ? next(current.snapshot.value) : next;
      if (value === current.snapshot.value) return;
      current.snapshot = { scope, value, revision: current.snapshot.revision + 1 };
      persist(current);
      if (activeScope.current === scope) rerender();
    },
    [scope],
  );

  // The ref also sees typing and functional updates in the same event, before
  // React has committed another render.
  const capture = useCallback(
    (): ComposerDraftSnapshot => entries.current.get(scope)!.snapshot,
    [scope],
  );

  const acknowledge = useCallback((sent: ComposerDraftSnapshot) => {
    const current = entries.current.get(sent.scope);
    if (
      !current ||
      current.snapshot.revision !== sent.revision ||
      current.snapshot.value !== sent.value
    )
      return;

    if (current.persisted && current.storageKey) {
      const saved = readDraft(current.storageKey);
      // Another mounted composer or browser tab may have edited this draft
      // while the request was in flight. Its stored revision wins.
      if (saved.persisted && (saved.revision !== sent.revision || saved.value !== sent.value)) {
        current.snapshot = { scope: sent.scope, value: saved.value, revision: saved.revision };
        if (activeScope.current === sent.scope) rerender();
        return;
      }
    }

    current.snapshot = { scope: sent.scope, value: '', revision: sent.revision + 1 };
    persist(current);
    if (activeScope.current === sent.scope) rerender();
  }, []);

  return { value: entry.snapshot.value, setValue, capture, acknowledge };
}
