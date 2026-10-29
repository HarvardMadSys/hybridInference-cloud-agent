'use client';

import type { ReactNode } from 'react';

/** A titled group of related settings, always visible. */
export function SettingsSection({
  title,
  description,
  children,
  className = 'mt-4',
}: {
  title: string;
  description?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <h3 className="text-base font-semibold text-foreground">{title}</h3>
      {description && <p className="mt-1 text-sm leading-6 text-muted">{description}</p>}
      <div className="mt-3">{children}</div>
    </div>
  );
}
