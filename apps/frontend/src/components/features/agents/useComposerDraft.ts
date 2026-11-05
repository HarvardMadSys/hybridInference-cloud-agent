'use client';

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
  type Dispatch,
  type SetStateAction,
} from 'react';

export interface ComposerDraftSnapshot {
  readonly scope: string;
  readonly value: string;
  readonly revision: number;
}

/**
 * A chat's draft without its text: what a page needs to send it.
 *
 * Holding this does not draw anything again when the text changes. Only the
 * component that shows the text reads it, with `useComposerDraftValue`, so a
 * keystroke redraws the composer and not the conversation around it.
 */
export interface ComposerDraftStore {
  setValue: Dispatch<SetStateAction<string>>;
  capture: () => ComposerDraftSnapshot;
  acknowledge: (sent: ComposerDraftSnapshot) => void;
  subscribe: (onChange: () => void) => () => void;
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

/**
 * The mounted composers. A composer left while its send was out is not the
 * one on screen when that send answers: the chat opened again read the draft
 * before the answer, so the one that sent tells these that it went.
 */
const mountedComposers = new Set<
  (sent: ComposerDraftSnapshot, cleared: ComposerDraftSnapshot) => void
>();

interface ComposerDraftOptions {
  userId?: string;
  threadId: string;
  /**
   * For a caller that keeps its own copy of the text: the draft it shows went
   * with a send made by a composer that has since been left.
   */
  onSentElsewhere?: () => void;
}

/** Text only. Uploads keep their existing lifecycle outside the draft store. */
export function useComposerDraft(options: ComposerDraftOptions) {
  const draft = useComposerDraftStore(options);
  const value = useComposerDraftValue(draft);
  const { setValue, capture, acknowledge } = draft;
  return { value, setValue, capture, acknowledge };
}

/** The draft's current text, for the component that draws it. */
export function useComposerDraftValue(draft: ComposerDraftStore): string {
  // Each change replaces the snapshot, so a new one is exactly a change.
  return useSyncExternalStore(draft.subscribe, draft.capture, draft.capture).value;
}

/**
 * `useComposerDraft` without the text: its owner is not drawn again on every
 * keystroke. The composer that shows the text subscribes to it instead.
 */
export function useComposerDraftStore({
  userId,
  threadId,
  onSentElsewhere,
}: ComposerDraftOptions): ComposerDraftStore {
  const entries = useRef(new Map<string, DraftEntry>());
  const listeners = useRef(new Set<() => void>());
  // Redraws what shows the text — the subscribers — and nothing else.
  const rerender = useCallback(() => {
    for (const listener of [...listeners.current]) listener();
  }, []);
  const subscribe = useCallback((onChange: () => void) => {
    listeners.current.add(onChange);
    return () => {
      listeners.current.delete(onChange);
    };
  }, []);
  const storageKey = userId ? composerDraftStorageKey(userId, threadId) : null;
  const scope = storageKey ?? `anonymous:${encodeURIComponent(threadId)}`;
  const activeScope = useRef(scope);
  activeScope.current = scope;
  const mounted = useRef(false);
  const onSentElsewhereRef = useRef(onSentElsewhere);
  onSentElsewhereRef.current = onSentElsewhere;

  useEffect(() => {
    // Still holding exactly what was sent, this copy is that message rather
    // than a new draft, and showing it would offer the message a second time.
    const clearIfSent = (sent: ComposerDraftSnapshot, cleared: ComposerDraftSnapshot) => {
      const current = entries.current.get(sent.scope);
      if (
        !current ||
        current.snapshot.revision !== sent.revision ||
        current.snapshot.value !== sent.value
      )
        return;
      current.snapshot = cleared;
      if (activeScope.current !== sent.scope) return;
      rerender();
      onSentElsewhereRef.current?.();
    };
    mounted.current = true;
    mountedComposers.add(clearIfSent);
    // A send this composer shows may have answered after the render that read
    // the draft and before it listened here: storage then holds a newer one.
    const current = entries.current.get(activeScope.current);
    if (current?.storageKey) {
      const saved = readDraft(current.storageKey);
      if (saved.persisted && saved.revision > current.snapshot.revision) {
        const sentElsewhere = saved.value === '' && current.snapshot.value !== '';
        current.snapshot = {
          scope: current.snapshot.scope,
          value: saved.value,
          revision: saved.revision,
        };
        rerender();
        if (sentElsewhere) onSentElsewhereRef.current?.();
      }
    }
    return () => {
      mounted.current = false;
      mountedComposers.delete(clearIfSent);
    };
  }, [rerender]);

  if (!entries.current.has(scope)) {
    const saved = readDraft(storageKey);
    entries.current.set(scope, {
      snapshot: { scope, value: saved.value, revision: saved.revision },
      storageKey,
      persisted: saved.persisted,
    });
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
    [rerender, scope],
  );

  // The ref also sees typing and functional updates in the same event, before
  // React has committed another render.
  const capture = useCallback(
    (): ComposerDraftSnapshot => entries.current.get(scope)!.snapshot,
    [scope],
  );

  const acknowledge = useCallback(
    (sent: ComposerDraftSnapshot) => {
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
      // Left while the send was out, so the draft on screen is another composer's.
      if (!mounted.current) {
        for (const composer of [...mountedComposers]) composer(sent, current.snapshot);
      }
    },
    [rerender],
  );

  return useMemo(
    () => ({ setValue, capture, acknowledge, subscribe }),
    [setValue, capture, acknowledge, subscribe],
  );
}
