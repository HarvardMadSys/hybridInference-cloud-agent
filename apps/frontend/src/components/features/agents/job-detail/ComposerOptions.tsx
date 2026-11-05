'use client';

import { useId, useRef, useState, type ReactNode } from 'react';
import { useT } from '@/components/providers';

/**
 * Secondary prompt controls (rewrite, clarification) behind one toolbar
 * button, so the toolbar keeps to attach, model and send.
 *
 * The panel stays mounted while closed: what it holds — a rewrite in
 * flight, its questions, the clarification choice — must survive a toggle.
 * It also stays open while that work is `active`, so its questions and
 * progress are never hidden.
 */
export function useComposerOptions(active: boolean) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const [chosen, setChosen] = useState(false);
  return {
    id,
    trigger,
    open: chosen || active,
    forced: active,
    toggle: () => setChosen((open) => !open),
    close: () => {
      if (active) return;
      setChosen(false);
      trigger.current?.focus();
    },
  };
}

type ComposerOptions = ReturnType<typeof useComposerOptions>;

export function ComposerOptionsButton({ options }: { options: ComposerOptions }) {
  const t = useT();
  return (
    <button
      ref={options.trigger}
      type="button"
      aria-expanded={options.open}
      aria-controls={options.id}
      aria-label={t('composer.options')}
      title={t('composer.options')}
      // Active work keeps the panel open; the button says so rather than
      // appearing to do nothing.
      disabled={options.forced}
      onClick={options.toggle}
      className={`ui-focus-ring inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors disabled:cursor-default [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11 ${
        options.open
          ? 'bg-surface-hover text-foreground'
          : 'text-muted hover:bg-surface-hover hover:text-foreground'
      }`}
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        className="h-[17px] w-[17px]"
      >
        <path strokeLinecap="round" d="M3.5 6h7m4 0h2M3.5 14h2m4 0h7M12.5 4v4M7.5 12v4" />
      </svg>
    </button>
  );
}

export function ComposerOptionsPanel({
  options,
  children,
}: {
  options: ComposerOptions;
  children: ReactNode;
}) {
  const t = useT();
  return (
    <div
      id={options.id}
      role="group"
      aria-label={t('composer.options')}
      hidden={!options.open}
      // Nested handlers (a rewrite's own questions) claim Escape first.
      onKeyDown={(event) => {
        if (event.key !== 'Escape' || event.defaultPrevented) return;
        event.preventDefault();
        options.close();
      }}
      className="mx-3 mb-1 rounded-xl bg-surface-muted py-2 dark:bg-white/[0.04]"
    >
      {children}
    </div>
  );
}
