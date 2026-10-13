'use client';

import { useLocale } from '@/components/providers';
import type { MemoryView } from './MemoryPanel';
import { ChevronIcon } from './icons';

/**
 * The derived profile, folded away below the saved memories. Project memory also
 * lists its suggestions; user suggestions live on the homepage.
 */
export function MemoryDetails({
  view,
  project,
}: {
  view: Pick<MemoryView, 'portrait' | 'suggestions'>;
  project: boolean;
}) {
  const { t } = useLocale();
  const { portrait } = view;
  const facts = [
    ['settings.memory.projects', portrait.projects.join(', ')],
    ['settings.memory.focusAreas', portrait.focus_areas.join(', ')],
    ['settings.memory.runtime', portrait.preferred_runtime],
    ['settings.memory.model', portrait.preferred_model],
  ] as const;
  const shownFacts = facts.filter(([, value]) => value);

  return (
    <details className="group mt-4 rounded-lg border border-border">
      <summary className="ui-focus-ring flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-lg px-3 text-sm font-medium text-foreground hover:bg-surface-hover [&::-webkit-details-marker]:hidden">
        <ChevronIcon className="h-4 w-4 shrink-0 text-muted transition-transform group-open:rotate-90" />
        {t(project ? 'settings.projectMemory.details' : 'settings.memory.portrait')}
      </summary>
      <div className={`grid gap-5 border-t border-border p-3 ${project ? 'sm:grid-cols-2' : ''}`}>
        <div>
          {project ? (
            <h3 className="mb-2 text-sm font-semibold text-foreground">
              {t('settings.projectMemory.portrait')}
            </h3>
          ) : null}
          {portrait.summary || shownFacts.length ? (
            <>
              {portrait.summary ? (
                <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-muted">
                  {portrait.summary}
                </p>
              ) : null}
              {shownFacts.length ? (
                <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
                  {shownFacts.map(([label, value]) => (
                    <div key={label} className="contents">
                      <dt className="text-muted">{t(label)}</dt>
                      <dd className="break-words text-foreground">{value}</dd>
                    </div>
                  ))}
                </dl>
              ) : null}
            </>
          ) : (
            <p className="text-sm text-muted">{t('settings.memory.emptyPortrait')}</p>
          )}
        </div>

        {project ? (
          <div>
            <h3 className="text-sm font-semibold text-foreground">
              {t('settings.memory.suggestions')}
            </h3>
            {view.suggestions.length ? (
              <ul className="mt-2 space-y-3">
                {view.suggestions.map((suggestion) => (
                  <li key={suggestion.id}>
                    <p className="break-words text-sm font-medium text-foreground">
                      {suggestion.title}
                    </p>
                    <p className="mt-0.5 whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">
                      {suggestion.reason}
                    </p>
                    <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground">
                      {suggestion.action}
                    </p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-sm text-muted">{t('settings.memory.emptySuggestions')}</p>
            )}
          </div>
        ) : null}
      </div>
    </details>
  );
}
