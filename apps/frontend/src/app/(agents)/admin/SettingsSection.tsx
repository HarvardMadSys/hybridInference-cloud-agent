'use client';

import { useLayoutEffect, useRef, type ReactNode } from 'react';

/** Keep advanced editors mounted so closing a section never discards a draft. */
export function SettingsSection({
  title,
  description,
  children,
  open = false,
  className = 'mt-6',
}: {
  title: 'Important' | 'Advanced';
  description?: string;
  children: ReactNode;
  open?: boolean;
  className?: string;
}) {
  const details = useRef<HTMLDetailsElement>(null);
  useLayoutEffect(() => {
    if (open && details.current) details.current.open = true;
  });

  const heading = <h3 className="text-base font-semibold text-foreground">{title}</h3>;
  const help = description && <p className="mt-1 text-sm leading-6 text-muted">{description}</p>;

  if (title === 'Advanced') {
    return (
      <details
        ref={details}
        className={`group ${className} overflow-hidden rounded-xl border border-border bg-surface`}
      >
        <summary className="ui-focus-ring flex min-h-11 cursor-pointer list-none items-center gap-3 bg-surface-muted/70 px-4 py-3 transition-colors hover:bg-surface-hover marker:content-none sm:px-5 [&::-webkit-details-marker]:hidden">
          <span className="min-w-0 flex-1">
            <span className="block text-sm font-semibold text-foreground">{title}</span>
            {description && (
              <span className="mt-0.5 block text-sm leading-5 text-muted">{description}</span>
            )}
          </span>
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-surface text-muted shadow-sm ring-1 ring-inset ring-border">
            <svg
              aria-hidden
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.75"
              className="h-4 w-4 transition-transform duration-150 group-open:rotate-90 motion-reduce:transition-none"
            >
              <path d="m7 5 5 5-5 5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
        </summary>
        <div className="border-t border-border px-4 py-2 sm:px-5">{children}</div>
      </details>
    );
  }

  return (
    <div className={className}>
      {heading}
      {help}
      <div className="mt-3">{children}</div>
    </div>
  );
}
