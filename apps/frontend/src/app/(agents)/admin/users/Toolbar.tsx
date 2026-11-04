'use client';

// The tinted head of the People list, as HybridInference's users toolbar: the
// quick views as pills, then the search and filters, a way back to every
// account, and the row density. The filters live in the address bar.

import { useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { Select } from '@/components/ui/Select';
import type { TranslationKey } from '@/lib/i18n/translate';
import type { Density } from './density';
import { filtersFromQuery, queryFromFilters, type FilterChange, type UserFilters } from './filters';
import { COUNT_VIEWS, FILTERS, SORTS, STATES, translated } from './options';

/** How long typing pauses before it searches, as HybridInference's filter bar. */
const SEARCH_DELAY_MS = 300;
/** A filter's select: sharing the row on a phone, its own width beside the search. */
const SELECT = 'ui-input h-8 min-w-[8.5rem] flex-1 text-[13px] sm:flex-none';
/** The list's small outlined buttons: Refresh, Previous and Next. */
export const SMALL_BUTTON =
  'ui-focus-ring h-8 rounded-md border border-border bg-surface px-2.5 text-[12px] font-medium text-foreground transition-colors hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50';
const DENSITIES: { value: Density; label: TranslationKey }[] = [
  { value: 'compact', label: 'adminAccounts.density.compact' },
  { value: 'comfortable', label: 'adminAccounts.density.comfortable' },
];

/** The quick views, each with the accounts it counts; one may be the view shown. */
function Views({
  counts,
  filters,
  onChange,
}: {
  counts: Record<string, number> | undefined;
  filters: UserFilters;
  onChange: (change: FilterChange) => void;
}) {
  const t = useT();
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span aria-hidden className="mr-1 text-[11px] font-medium uppercase tracking-wide text-muted">
        {t('adminAccounts.viewsLabel')}
      </span>
      <div role="group" aria-label={t('adminAccounts.counts')} className="flex flex-wrap gap-1.5">
        {COUNT_VIEWS.map((view) => {
          const active = filters.status === view.status && filters.state === view.state;
          const count = counts ? (counts[view.key] ?? 0) : undefined;
          // Applications waiting are the one count that asks for something.
          const waiting = !active && view.key === 'admission:pending' && !!count;
          return (
            <button
              key={view.key}
              type="button"
              aria-pressed={active}
              onClick={() =>
                onChange((current) => ({ ...current, status: view.status, state: view.state }))
              }
              className={`ui-focus-ring inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] font-medium transition-colors ${
                active
                  ? 'border-foreground bg-foreground text-surface'
                  : 'border-border bg-surface text-muted hover:border-border-strong hover:text-foreground'
              }`}
            >
              {t(view.label)}
              {count !== undefined && (
                <span
                  className={`tabular-nums ${waiting ? 'font-semibold text-status-info' : 'opacity-70'}`}
                >
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function DensityToggle({
  density,
  onDensity,
}: {
  density: Density;
  onDensity: (density: Density) => void;
}) {
  const t = useT();
  return (
    <div className="flex items-center gap-2 text-[12px] text-muted">
      <span aria-hidden className="hidden sm:inline">
        {t('adminAccounts.density')}
      </span>
      <div
        role="group"
        aria-label={t('adminAccounts.density')}
        className="inline-flex rounded-md border border-border bg-surface-muted p-0.5"
      >
        {DENSITIES.map((option) => (
          <button
            key={option.value}
            type="button"
            aria-pressed={density === option.value}
            onClick={() => onDensity(option.value)}
            className={`ui-focus-ring rounded px-2.5 py-1 font-medium transition-colors ${
              density === option.value
                ? 'bg-surface text-foreground shadow-subtle'
                : 'text-muted hover:text-foreground'
            }`}
          >
            {t(option.label)}
          </button>
        ))}
      </div>
    </div>
  );
}

function SearchIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 20 20"
      fill="currentColor"
      className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted"
    >
      <path
        fillRule="evenodd"
        d="M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2 9a7 7 0 1112.452 4.391l3.328 3.329a.75.75 0 11-1.06 1.06l-3.329-3.328A7 7 0 012 9z"
        clipRule="evenodd"
      />
    </svg>
  );
}

export function Toolbar({
  filters,
  onChange,
  counts,
  total,
  loading,
  onRefresh,
  density,
  onDensity,
}: {
  filters: UserFilters;
  onChange: (change: FilterChange) => void;
  counts: Record<string, number> | undefined;
  total: number;
  loading: boolean;
  onRefresh: () => void;
  density: Density;
  onDensity: (density: Density) => void;
}) {
  const t = useT();
  const [search, setSearch] = useState(filters.q);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  // Typing settles into the address bar once it pauses; a search that arrives
  // there another way (a link, back and forward) fills the box.
  const type = (value: string) => {
    setSearch(value);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      const q = value.trim();
      onChange((current) => (current.q === q ? current : { ...current, q }));
    }, SEARCH_DELAY_MS);
  };
  useEffect(() => {
    setSearch((current) => (current.trim() === filters.q ? current : filters.q));
  }, [filters.q]);
  const filtered = queryFromFilters(filters) !== '' || search.trim() !== '';
  const clear = () => {
    // A search still waiting to settle would otherwise land after the clear.
    window.clearTimeout(timer.current);
    setSearch('');
    onChange(filtersFromQuery(''));
  };
  return (
    <div className="space-y-2.5 border-b border-border bg-surface-muted/50 p-3">
      <Views counts={counts} filters={filters} onChange={onChange} />
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full sm:w-80">
          <SearchIcon />
          <input
            className="ui-input h-8 w-full pl-8 text-[13px]"
            type="search"
            aria-label={t('adminAccounts.search')}
            placeholder={t('adminAccounts.searchPlaceholder')}
            maxLength={320}
            value={search}
            onChange={(e) => type(e.target.value)}
          />
        </div>
        <div className="flex w-full flex-wrap gap-2 sm:w-auto">
          <Select
            className={SELECT}
            aria-label={t('adminAccounts.applicationStatus')}
            value={filters.status}
            onValueChange={(status) => onChange((current) => ({ ...current, status }))}
            options={translated(FILTERS, t)}
          />
          <Select
            className={SELECT}
            aria-label={t('adminAccounts.accountState')}
            value={filters.state}
            onValueChange={(state) => onChange((current) => ({ ...current, state }))}
            options={translated(STATES, t)}
          />
          <Select
            className={SELECT}
            aria-label={t('adminAccounts.sortLabel')}
            value={filters.sort}
            onValueChange={(sort) => onChange((current) => ({ ...current, sort }))}
            options={translated(SORTS, t)}
          />
        </div>
        {filtered && (
          <button
            type="button"
            onClick={clear}
            className="ui-focus-ring h-8 rounded-md px-2 text-[12px] font-medium text-muted transition-colors hover:bg-surface-hover hover:text-foreground"
          >
            {t('adminAccounts.clearFilters')}
          </button>
        )}
        <div className="ml-auto flex flex-wrap items-center justify-end gap-x-3 gap-y-2">
          <span className="text-[12px] tabular-nums text-muted">
            {t('adminAccounts.total', { count: total })}
          </span>
          <DensityToggle density={density} onDensity={onDensity} />
          <button type="button" className={SMALL_BUTTON} disabled={loading} onClick={onRefresh}>
            {t('adminAccounts.refresh')}
          </button>
        </div>
      </div>
    </div>
  );
}
