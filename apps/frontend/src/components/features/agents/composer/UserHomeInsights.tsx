'use client';

import { useId } from 'react';
import { useT } from '@/components/providers';
import type { WorkInsights } from '@/lib/api/insights';

const WORK_AREAS = [
  ['backend', 'bg-[#5268A0]', 'M8 5L3 12L8 19M16 5L21 12L16 19M14 4L10 20'],
  [
    'frontend',
    'bg-[#A05E7D]',
    'M6 4h12a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3ZM3 9H21M10 9V20',
  ],
  ['testing', 'bg-[#377968]', 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM7 12L11 16L17 8'],
  [
    'debugging',
    'bg-[#8D7944]',
    'M12 8a4 4 0 0 1 4 4v4a4 4 0 0 1-8 0v-4a4 4 0 0 1 4-4ZM9 8V6L7 4M15 8V6L17 4M4 10H8M16 10H20M4 15H8M16 15H20M5 20L8 18M16 18L19 20M12 9V18',
  ],
  ['security', 'bg-[#7B609A]', 'M12 3L20 6V11Q20 17 12 21Q4 17 4 11V6ZM8 11L11 14L16 9'],
  [
    'data',
    'bg-[#3F7787]',
    'M20 6a8 3 0 1 1-16 0 8 3 0 0 1 16 0ZM4 6V18C4 22 20 22 20 18V6M4 12C4 16 20 16 20 12',
  ],
  [
    'deployment',
    'bg-[#AF684B]',
    'M7 19H18C24 19 24 10 19 9C18 2 8 2 6 9C0 9 1 19 7 19ZM12 16V8M9 11L12 8L15 11',
  ],
] as const;

export function UserHomeInsights({
  view,
  loading,
  error,
  onRetry,
}: {
  view: WorkInsights | null;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  const t = useT();
  const headingId = useId();

  return (
    <section aria-labelledby={headingId} className="mt-8 border-t border-border pt-6">
      <h2 id={headingId} className="text-sm font-semibold text-foreground">
        {t('home.suggestions.title')}
      </h2>
      <p className="mt-2 text-xs leading-relaxed text-muted">{t('home.suggestions.description')}</p>
      {loading ? (
        <p role="status" className="mt-3 text-sm text-muted">
          {t('home.suggestions.loading')}
        </p>
      ) : error ? (
        <div role="status" className="mt-3 text-sm text-muted">
          <p>{t('home.suggestions.error')}</p>
          <button
            type="button"
            onClick={onRetry}
            className="ui-button ui-button-secondary mt-2 min-h-11"
          >
            {t('home.suggestions.retry')}
          </button>
        </div>
      ) : view?.suggestions.length ? (
        <ul className="mt-4 grid gap-3 sm:grid-cols-2">
          {view.suggestions.map((suggestion) => (
            <li key={suggestion.id} className="rounded-xl border border-border bg-surface p-4">
              <h3 className="break-words text-sm font-medium text-foreground">
                {suggestion.title}
              </h3>
              <p className="mt-2 whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">
                {suggestion.reason}
              </p>
              <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground">
                {suggestion.action}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm leading-relaxed text-muted">{t('home.suggestions.empty')}</p>
      )}

      <details className="mt-5 rounded-xl border border-border bg-surface">
        <summary className="cursor-pointer rounded-xl px-4 py-3 text-sm font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
          {t('home.avatar.title')}
        </summary>
        <div className="border-t border-border p-4">
          <p className="mb-3 text-sm leading-relaxed text-muted">{t('home.avatar.choice')}</p>
          <p className="text-sm leading-relaxed text-muted">{t('home.avatar.generation')}</p>
          <dl className="mt-4 grid gap-4 text-sm sm:grid-cols-2">
            {(['character', 'theme', 'badges', 'default'] as const).map((aspect) => (
              <div key={aspect}>
                <dt className="font-medium text-foreground">{t(`home.avatar.${aspect}`)}</dt>
                <dd className="mt-1 leading-relaxed text-muted">
                  {t(`home.avatar.${aspect}Description`)}
                </dd>
              </div>
            ))}
          </dl>
          <h3 className="mt-5 text-sm font-medium text-foreground">{t('home.avatar.legend')}</h3>
          <ul className="mt-3 grid gap-3 text-xs sm:grid-cols-2">
            {WORK_AREAS.map(([area, color, path]) => (
              <li key={area} className="flex items-center gap-2 text-muted">
                <span
                  aria-hidden="true"
                  className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-white ${color}`}
                >
                  <svg
                    width="20"
                    height="20"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d={path} />
                  </svg>
                </span>
                <span>{t(`home.avatar.legend.${area}`)}</span>
              </li>
            ))}
          </ul>
          <p className="mt-4 text-xs leading-relaxed text-muted">{t('home.avatar.controls')}</p>
        </div>
      </details>
    </section>
  );
}
