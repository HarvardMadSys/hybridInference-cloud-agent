'use client';

import Link from 'next/link';
import type { ReactNode } from 'react';
import { useLocale } from '@/components/providers';
import type { UserMemoryView } from '@/lib/api/userMemory';

export function MemoryDetails({
  view,
  scope,
  memoriesContent,
}: {
  view: Pick<UserMemoryView, 'portrait' | 'memories' | 'suggestions'>;
  scope: 'user' | 'project';
  memoriesContent?: ReactNode;
}) {
  const { t } = useLocale();
  const hasPortrait =
    view.portrait.summary ||
    view.portrait.projects.length ||
    view.portrait.focus_areas.length ||
    view.portrait.preferred_runtime ||
    view.portrait.preferred_model;

  return (
    <div className={`mt-5 grid gap-5 ${scope === 'project' ? 'lg:grid-cols-3' : 'lg:grid-cols-2'}`}>
      <div>
        <h3 className="text-sm font-semibold text-foreground">
          {t(scope === 'project' ? 'settings.projectMemory.portrait' : 'settings.memory.portrait')}
        </h3>
        {hasPortrait ? (
          <>
            {view.portrait.summary ? (
              <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed text-muted">
                {view.portrait.summary}
              </p>
            ) : null}
            <dl className="mt-2 space-y-2 text-sm">
              {view.portrait.projects.length ? (
                <div>
                  <dt className="font-medium text-foreground">{t('settings.memory.projects')}</dt>
                  <dd className="break-words text-muted">{view.portrait.projects.join(', ')}</dd>
                </div>
              ) : null}
              {view.portrait.focus_areas.length ? (
                <div>
                  <dt className="font-medium text-foreground">{t('settings.memory.focusAreas')}</dt>
                  <dd className="break-words text-muted">{view.portrait.focus_areas.join(', ')}</dd>
                </div>
              ) : null}
              {view.portrait.preferred_runtime ? (
                <div>
                  <dt className="font-medium text-foreground">{t('settings.memory.runtime')}</dt>
                  <dd className="break-words text-muted">{view.portrait.preferred_runtime}</dd>
                </div>
              ) : null}
              {view.portrait.preferred_model ? (
                <div>
                  <dt className="font-medium text-foreground">{t('settings.memory.model')}</dt>
                  <dd className="break-words text-muted">{view.portrait.preferred_model}</dd>
                </div>
              ) : null}
            </dl>
          </>
        ) : (
          <p className="mt-2 text-sm leading-relaxed text-muted">
            {t(
              scope === 'project'
                ? 'settings.projectMemory.emptyPortrait'
                : 'settings.memory.emptyPortrait',
            )}
          </p>
        )}
      </div>

      {memoriesContent ?? (
        <div>
          <h3 className="text-sm font-semibold text-foreground">
            {t(
              scope === 'project' ? 'settings.projectMemory.memories' : 'settings.memory.memories',
            )}
          </h3>
          {view.memories.length ? (
            <ul className="mt-2 space-y-2">
              {view.memories.map((memory) => (
                <li key={memory.id} className="rounded-lg border border-border p-3">
                  <span className="text-xs font-medium text-muted">
                    {t(
                      memory.kind === 'preference'
                        ? 'settings.memory.preference'
                        : 'settings.memory.task',
                    )}
                  </span>
                  <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground">
                    {memory.content}
                  </p>
                  {memory.source_repo ? (
                    <p className="mt-1 break-words text-xs text-muted">{memory.source_repo}</p>
                  ) : null}
                  <Link
                    href={`/${encodeURIComponent(memory.source_job_id)}`}
                    className="mt-2 inline-flex min-h-8 items-center text-xs font-medium text-accent hover:underline"
                  >
                    {t('settings.memory.source')}
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-sm text-muted">
              {t(
                scope === 'project'
                  ? 'settings.projectMemory.emptyMemory'
                  : 'settings.memory.emptyMemory',
              )}
            </p>
          )}
        </div>
      )}

      {scope === 'project' ? (
        <div>
          <h3 className="text-sm font-semibold text-foreground">
            {t('settings.memory.suggestions')}
          </h3>
          {view.suggestions.length ? (
            <ul className="mt-2 space-y-2">
              {view.suggestions.map((suggestion) => (
                <li key={suggestion.id} className="rounded-lg bg-surface-muted p-3">
                  <p className="break-words text-sm font-medium text-foreground">
                    {suggestion.title}
                  </p>
                  <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">
                    {suggestion.reason}
                  </p>
                  <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground">
                    {suggestion.action}
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-sm leading-relaxed text-muted">
              {t('settings.projectMemory.emptySuggestions')}
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
}
