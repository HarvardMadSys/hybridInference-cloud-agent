'use client';

import type { ReactNode } from 'react';

/** One dashboard card: a titled surface with an optional control on the right. */
export function DashCard({
  title,
  aside,
  children,
  className = '',
  labelledBy,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
  labelledBy?: string;
}) {
  return (
    <section
      aria-labelledby={labelledBy}
      className={`min-w-0 rounded-xl border border-border bg-surface p-3 ${className}`}
    >
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 id={labelledBy} className="text-sm font-semibold text-foreground">
          {title}
        </h3>
        {aside}
      </div>
      {children}
    </section>
  );
}

/** A two-or-more way switch in the dashboard's cards (Step/Turn, Total/Delta…). */
export function Toggle<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string; disabled?: boolean; tip?: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className="inline-flex rounded-md bg-surface-muted p-0.5 text-[11px]"
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          disabled={option.disabled}
          title={option.tip}
          onClick={() => onChange(option.value)}
          className={`ui-focus-ring rounded px-2 py-0.5 [@media(pointer:coarse)]:min-h-11 ${
            value === option.value
              ? 'bg-surface font-semibold text-foreground shadow-sm'
              : 'text-muted hover:text-foreground'
          } disabled:cursor-not-allowed disabled:opacity-50`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/** A share as words: never "0%" for a positive value. */
export function shareText(value: number, total: number): string {
  if (total <= 0 || value <= 0) return '0%';
  const pct = (value / total) * 100;
  if (pct < 1) return '<1%';
  return `${Math.round(pct)}%`;
}
