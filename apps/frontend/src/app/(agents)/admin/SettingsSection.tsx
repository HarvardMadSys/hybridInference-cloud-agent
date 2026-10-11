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
      <details ref={details} className={`${className} rounded-xl border border-border`}>
        <summary className="ui-focus-ring min-h-11 cursor-pointer rounded-xl px-4 py-4 sm:px-5">
          <span className="inline-block align-middle">
            <span className="block text-base font-semibold text-foreground">{title}</span>
            {description && (
              <span className="mt-1 block text-sm leading-6 text-muted">{description}</span>
            )}
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
