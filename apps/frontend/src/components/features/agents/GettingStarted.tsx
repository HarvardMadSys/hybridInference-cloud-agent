'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useCallback, useEffect, useId, useRef, useState } from 'react';

import { useT } from '@/components/providers';

import { NEW_TASK_PROMPT_ID, useFirstRun, type FirstRunStep } from './firstRun';
import { githubAccessUrl, leaveFor } from './githubAccess';
import { rememberReturn, requestGitHubAccess } from './githubReturn';

// One quiet row above Settings for someone who has not started a task yet:
// the next thing to do, how far along they are, and a way to put it away.
//
// It is not a second place to explain anything, and it decides nothing: which
// step is next comes from the app's first-run answer (see firstRun.tsx). Each
// step goes to the screen that does it — GitHub, or the new-task box. Skip is
// undone here for a few seconds, and from Settings after that.

const ROW =
  'ui-focus-ring group mt-2 flex min-h-10 w-full items-center gap-2.5 rounded-xl px-2 py-2 text-left text-[13px] font-medium text-foreground transition-colors hover:bg-surface-hover disabled:cursor-wait disabled:opacity-70 [@media(pointer:coarse)]:min-h-11';

function StepIcon({ step }: { step: FirstRunStep['next'] }) {
  if (step === 'task') {
    return (
      <svg
        aria-hidden="true"
        className="h-4 w-4 shrink-0 text-muted"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2}
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M16.86 4.49a1.5 1.5 0 0 1 2.12 0l.53.53a1.5 1.5 0 0 1 0 2.12L8.53 18.12 4 19.5l1.38-4.53L16.86 4.49Z"
        />
      </svg>
    );
  }
  return (
    <svg
      aria-hidden="true"
      className="h-4 w-4 shrink-0 text-muted"
      viewBox="0 0 16 16"
      fill="currentColor"
    >
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

/** How long the note after Skip stays, once nothing is holding it. */
export const UNDO_SKIP_MS = 6000;

// Said in the row's place right after Skip: where it went, and a way back for
// a Skip pressed by mistake. It leaves after a few seconds, but not while it
// is pointed at or focused — not from under a hand reaching for Undo.
function SkippedNote({
  focusUndo,
  onUndo,
  onGone,
}: {
  focusUndo: boolean;
  onUndo: (fromKeyboard: boolean) => void;
  onGone: () => void;
}) {
  const t = useT();
  const undo = useRef<HTMLButtonElement>(null);
  const [pointedAt, setPointedAt] = useState(false);
  const [focused, setFocused] = useState(false);

  useEffect(() => {
    if (focusUndo) undo.current?.focus();
  }, [focusUndo]);

  useEffect(() => {
    if (pointedAt || focused) return undefined;
    const timer = window.setTimeout(onGone, UNDO_SKIP_MS);
    return () => window.clearTimeout(timer);
  }, [pointedAt, focused, onGone]);

  return (
    <div className="shrink-0 px-3 pb-3">
      <div
        role="status"
        onMouseEnter={() => setPointedAt(true)}
        onMouseLeave={() => setPointedAt(false)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        className="flex items-center gap-2 rounded-xl bg-surface-hover px-2.5 py-2 text-[12px] leading-snug text-muted"
      >
        <span className="min-w-0 flex-1">{t('gettingStarted.hidden')}</span>
        <button
          ref={undo}
          type="button"
          // A press from the keyboard has no click count.
          onClick={(event) => onUndo(event.detail === 0)}
          className="ui-focus-ring shrink-0 rounded-lg px-1.5 py-1 font-medium text-foreground transition-colors hover:bg-surface [@media(pointer:coarse)]:min-h-11"
        >
          {t('gettingStarted.undo')}
        </button>
      </div>
    </div>
  );
}

export function GettingStarted() {
  const t = useT();
  const pathname = usePathname() ?? '';
  const router = useRouter();
  const headingId = useId();
  const { step, skip, hidden, show } = useFirstRun();
  const [leaving, setLeaving] = useState(false);
  // Set by Skip until the note after it goes. A Skip pressed from the keyboard
  // hands its place to Undo, and Undo hands it back, so focus is not dropped.
  const [note, setNote] = useState<{ fromKeyboard: boolean } | null>(null);
  const noteGone = useCallback(() => setNote(null), []);
  const skipButton = useRef<HTMLButtonElement>(null);
  const refocusSkip = useRef(false);

  // Brought back from Settings while the note was still up: it has nothing
  // left to say.
  useEffect(() => {
    if (!hidden) setNote(null);
  }, [hidden]);

  useEffect(() => {
    if (!refocusSkip.current || !skipButton.current) return;
    refocusSkip.current = false;
    skipButton.current.focus();
  });

  if (note && hidden) {
    return (
      <SkippedNote
        focusUndo={note.fromKeyboard}
        onUndo={(fromKeyboard) => {
          refocusSkip.current = fromKeyboard;
          setNote(null);
          show();
        }}
        onGone={noteGone}
      />
    );
  }

  if (!step) return null;

  const label =
    step.next === 'connect'
      ? t('gettingStarted.connect')
      : step.next === 'repository'
        ? t('gettingStarted.repository')
        : t('gettingStarted.firstTask');

  async function goToGitHub(kind: 'connect' | 'manage') {
    // The new-task box holds the draft: when it is on screen, it makes the trip.
    if (requestGitHubAccess(kind)) return;
    setLeaving(true);
    rememberReturn('/');
    try {
      const url = await githubAccessUrl(kind);
      if (url) {
        leaveFor(url);
        return;
      }
      router.push('/settings/integrations');
    } catch {
      // The Integrations page is where a failed lookup is reported and retried.
      router.push('/settings/integrations');
    }
    setLeaving(false);
  }

  const content = (
    <>
      <StepIcon step={step.next} />
      <span className="min-w-0 flex-1 truncate">{leaving ? t('connect.leaving') : label}</span>
      <svg
        aria-hidden="true"
        className="h-3.5 w-3.5 shrink-0 text-muted opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2}
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h14m0 0-6-6m6 6-6 6" />
      </svg>
    </>
  );

  return (
    <section aria-labelledby={headingId} className="shrink-0 px-3 pb-4 pt-3">
      <div className="flex items-center justify-between pl-2 pr-0.5 text-[11px] text-muted">
        <h2 id={headingId} className="font-medium tracking-[0.04em]">
          {t('gettingStarted.title')}
        </h2>
        <span className="flex items-center gap-1">
          <span className="tabular-nums">
            {t('gettingStarted.progress', { done: step.done, total: step.total })}
          </span>
          <span aria-hidden="true">·</span>
          <button
            ref={skipButton}
            type="button"
            onClick={(event) => {
              skip();
              setNote({ fromKeyboard: event.detail === 0 });
            }}
            className="ui-focus-ring rounded-lg px-1.5 py-1 transition-colors hover:bg-surface-hover hover:text-foreground [@media(pointer:coarse)]:min-h-11"
          >
            {t('gettingStarted.skip')}
          </button>
        </span>
      </div>
      <div aria-hidden="true" className="mx-2 mt-2 flex gap-1">
        {Array.from({ length: step.total }, (_, index) => (
          <span
            key={index}
            className={`h-[3px] flex-1 rounded-full ${
              index < step.done ? 'bg-accent/70' : 'bg-border'
            }`}
          />
        ))}
      </div>
      {step.next === 'task' ? (
        <Link
          href="/"
          onClick={(event) => {
            // Already on the new-task page: the box is right there.
            if (pathname !== '/') return;
            event.preventDefault();
            document.getElementById(NEW_TASK_PROMPT_ID)?.focus();
          }}
          className={ROW}
        >
          {content}
        </Link>
      ) : (
        <button
          type="button"
          disabled={leaving}
          onClick={() => void goToGitHub(step.next === 'connect' ? 'connect' : 'manage')}
          className={ROW}
        >
          {content}
        </button>
      )}
    </section>
  );
}
