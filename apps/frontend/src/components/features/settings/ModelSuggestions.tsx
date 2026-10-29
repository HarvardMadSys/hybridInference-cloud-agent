'use client';

import { useId } from 'react';
import { useT } from '@/components/providers';
import type { SuggestionProblem, Suggestions } from './useModelSuggestions';

/** How many listed IDs are shown at once; typing in the model line narrows them. */
const SHOWN = 50;

const PROBLEM_KEYS = {
  unsupported: 'connections.suggest.problem.unsupported',
  auth: 'connections.suggest.problem.auth',
  unreachable: 'connections.suggest.problem.unreachable',
  rateLimited: 'connections.suggest.problem.rateLimited',
  limited: 'connections.suggest.problem.limited',
  keyRequired: 'connections.suggest.problem.keyRequired',
  failed: 'connections.suggest.problem.failed',
} as const satisfies Record<SuggestionProblem, string>;

/** The listed IDs not yet added that contain what is typed in the model line. */
function matching(listed: readonly string[], models: readonly string[], draft: string): string[] {
  const needle = draft.trim().toLowerCase();
  return listed.filter(
    (model) => !models.includes(model) && (!needle || model.toLowerCase().includes(needle)),
  );
}

function Status({
  suggestions,
  missing,
  shown,
  total,
  draft,
}: {
  suggestions: Suggestions | null;
  missing: 'details' | 'newKey' | null;
  shown: number;
  total: number;
  draft: string;
}) {
  const t = useT();
  if (suggestions?.phase === 'loading') return <>{t('connections.suggest.loading')}</>;
  if (suggestions?.phase === 'failed') return <>{t(PROBLEM_KEYS[suggestions.problem])}</>;
  if (suggestions?.phase === 'loaded') {
    if (!suggestions.models.length) return <>{t('connections.suggest.empty')}</>;
    if (!shown && !draft.trim()) return <>{t('connections.suggest.allAdded')}</>;
    if (!shown) return <>{t('connections.suggest.noMatch')}</>;
    if (shown < total) return <>{t('connections.suggest.narrow', { shown, total })}</>;
    return <>{t('connections.suggest.listed', { total: suggestions.models.length })}</>;
  }
  if (missing === 'newKey') return <>{t('connections.suggest.needsKey')}</>;
  if (missing === 'details') return <>{t('connections.suggest.needsDetails')}</>;
  return null;
}

/**
 * Suggestions for the Model IDs line from the provider's own list, asked for
 * on demand. Picking one adds it like typing it would, within the model
 * limit; nothing is saved, and every picked ID is verified with the form.
 * A failed or missing list only explains itself: typing an ID still works.
 */
export function ModelSuggestions({
  suggestions,
  missing,
  models,
  draft,
  max,
  disabled,
  onLoad,
  onPick,
}: {
  suggestions: Suggestions | null;
  /** What the form still needs before it can ask; `null` when it can. */
  missing: 'details' | 'newKey' | null;
  models: readonly string[];
  draft: string;
  max: number;
  disabled: boolean;
  onLoad: () => void;
  onPick: (model: string) => void;
}) {
  const t = useT();
  const id = useId();
  const loading = suggestions?.phase === 'loading';
  const full = models.length >= max;
  const listed = suggestions?.phase === 'loaded' ? matching(suggestions.models, models, draft) : [];
  const shown = listed.slice(0, SHOWN);
  return (
    <div className="mt-1.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <button
          type="button"
          onClick={onLoad}
          disabled={disabled || loading || missing !== null}
          aria-describedby={`${id}-hint`}
          className="ui-focus-ring inline-flex min-h-9 items-center rounded text-sm font-medium text-accent hover:underline disabled:cursor-not-allowed disabled:text-muted disabled:no-underline"
        >
          {t(loading ? 'connections.suggest.loadingShort' : 'connections.suggest.load')}
        </button>
        <p id={`${id}-hint`} className="min-w-0 flex-1 basis-56 text-xs leading-5 text-muted">
          {t('connections.suggest.hint')}
        </p>
      </div>
      <p role="status" aria-live="polite" className="text-xs leading-5 text-muted empty:hidden">
        <Status
          suggestions={suggestions}
          missing={missing}
          shown={shown.length}
          total={listed.length}
          draft={draft}
        />
      </p>
      {suggestions?.phase === 'loaded' && shown.length ? (
        <ul
          aria-label={t('connections.suggest.list')}
          className="mt-1.5 flex max-h-40 flex-wrap gap-1.5 overflow-y-auto"
        >
          {shown.map((model) => (
            <li key={model} className="max-w-full">
              <button
                type="button"
                disabled={disabled || full}
                aria-label={t('connections.suggest.pick', { model })}
                onClick={() => onPick(model)}
                className="ui-focus-ring inline-flex min-h-9 max-w-full items-center rounded-md border border-dashed border-border px-2 text-left font-mono text-[12px] break-all hover:border-accent hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
              >
                {model}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {suggestions?.phase === 'loaded' && shown.length && full ? (
        <p className="mt-1 text-xs leading-5 text-muted">{t('connections.modelLimit', { max })}</p>
      ) : null}
    </div>
  );
}
