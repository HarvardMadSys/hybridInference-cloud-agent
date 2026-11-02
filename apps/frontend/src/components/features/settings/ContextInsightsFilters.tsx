'use client';

// The insights' search, project filter and grouping, and the project groups.
//
// Everything here is applied by the server over the whole range before the
// list is ranked and paged, so a search finds conversations that were never
// loaded into the page. Projects are the conversations' repositories; a
// conversation without one is in the explicit "No repository" bucket.

import { useEffect, useState } from 'react';
import { useLocale } from '@/components/providers';
import { Select } from '@/components/ui/Select';
import type { ContextInsightsFilters, ContextProject } from '@/lib/api/ownerUsage';
import {
  hasFilters,
  orderedProjects,
  projectChoice,
  projectFilters,
} from './contextInsightsPresent';
import { compactCount } from './usagePresent';

/** How long typing pauses before the search is sent. */
export const SEARCH_DEBOUNCE_MS = 300;

export function ContextInsightsFilterBar({
  filters,
  projects,
  grouped,
  matching,
  onChange,
  onGroupedChange,
}: {
  filters: ContextInsightsFilters;
  projects: ContextProject[];
  grouped: boolean;
  /** Conversations the filters match; shown while any filter is set. */
  matching: number | null;
  onChange: (next: ContextInsightsFilters) => void;
  onGroupedChange: (grouped: boolean) => void;
}) {
  const { t, locale } = useLocale();
  const [text, setText] = useState(filters.q);
  // A cleared filter (or one set elsewhere) is what the box shows.
  useEffect(() => setText(filters.q), [filters.q]);
  useEffect(() => {
    if (text === filters.q) return undefined;
    const timer = window.setTimeout(() => onChange({ ...filters, q: text }), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [text, filters, onChange]);
  const name = (project: ContextProject) => project.repo ?? t('contextInsights.project.none');
  const options = [
    { value: '', label: t('contextInsights.project.all') },
    ...orderedProjects(projects).map((project) => ({
      value: project.repo === null ? 'temporary' : `repo:${project.repo}`,
      label: t('contextInsights.project.count', {
        name: name(project),
        count: project.conversations.toLocaleString(locale),
      }),
    })),
  ];
  const chosen = projectChoice(filters);
  if (chosen && !options.some((option) => option.value === chosen)) {
    // A project with nothing in this range stays choosable, so it can be cleared.
    options.push({
      value: chosen,
      label: filters.repo ?? t('contextInsights.project.none'),
    });
  }
  return (
    <div className="mt-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={text}
          maxLength={200}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') onChange({ ...filters, q: text });
          }}
          aria-label={t('contextInsights.search')}
          placeholder={t('contextInsights.searchPlaceholder')}
          className="ui-input min-h-9 w-full text-sm sm:w-64 [@media(pointer:coarse)]:min-h-11"
        />
        <Select
          id="settings-context-project"
          value={chosen}
          onValueChange={(value) => onChange({ ...filters, ...projectFilters(value) })}
          aria-label={t('contextInsights.project')}
          className="ui-input min-h-9 w-full text-sm sm:w-64 [@media(pointer:coarse)]:min-h-11"
          options={options}
        />
        <label className="flex min-h-9 items-center gap-2 text-sm text-foreground [@media(pointer:coarse)]:min-h-11">
          <input
            type="checkbox"
            checked={grouped}
            onChange={(event) => onGroupedChange(event.target.checked)}
            className="h-4 w-4"
          />
          {t('contextInsights.groupBy')}
        </label>
      </div>
      {hasFilters(filters) ? (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {matching !== null ? (
            <span role="status" className="text-muted">
              {t('contextInsights.filters.matching', { count: matching.toLocaleString(locale) })}
            </span>
          ) : null}
          {filters.day ? (
            <button
              type="button"
              onClick={() => onChange({ ...filters, day: null })}
              aria-label={t('contextInsights.filters.clearDay')}
              title={t('contextInsights.filters.clearDay')}
              className="ui-focus-ring inline-flex items-center gap-1 rounded-md border border-accent px-2 py-1 text-foreground [@media(pointer:coarse)]:min-h-11"
            >
              {t('contextInsights.filters.day', { day: filters.day })}
              <span aria-hidden="true">×</span>
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => onChange({ q: '', repo: null, temporary: false, day: null })}
            className="ui-focus-ring rounded px-1 text-accent hover:underline [@media(pointer:coarse)]:min-h-11"
          >
            {t('contextInsights.filters.clear')}
          </button>
        </div>
      ) : null}
    </div>
  );
}

/** Each project over the range and search, with a way into its conversations. */
export function ContextProjectGroups({
  projects,
  onOpen,
}: {
  projects: ContextProject[];
  onOpen: (project: ContextProject) => void;
}) {
  const { t, locale } = useLocale();
  if (projects.length === 0) return null;
  const peak = (project: ContextProject) =>
    project.peak_prompt_tokens === null
      ? ''
      : ` · ${t('contextInsights.group.peak', { tokens: compactCount(project.peak_prompt_tokens) })}`;
  return (
    <div className="mt-3">
      <p className="text-xs text-muted">{t('contextInsights.groups.note')}</p>
      <ul className="mt-2 divide-y divide-border rounded-lg border border-border">
        {orderedProjects(projects).map((project) => (
          <li
            key={project.repo ?? '\u0000none'}
            className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5 text-sm"
          >
            <div className="min-w-0">
              <p className="truncate font-medium text-foreground">
                {project.repo ?? t('contextInsights.project.none')}
              </p>
              <p className="text-xs tabular-nums text-muted">
                {t('contextInsights.group.summary', {
                  conversations: project.conversations.toLocaleString(locale),
                  turns: project.turns.toLocaleString(locale),
                  requests: project.main_requests.toLocaleString(locale),
                  compactions: project.compactions.toLocaleString(locale),
                })}
                {peak(project)}
              </p>
            </div>
            <button
              type="button"
              onClick={() => onOpen(project)}
              className="ui-button ui-button-secondary min-h-9 px-3 text-xs [@media(pointer:coarse)]:min-h-11"
            >
              {t('contextInsights.group.open')}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
