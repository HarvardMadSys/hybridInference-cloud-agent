'use client';

import { useId, type KeyboardEvent } from 'react';
import { useT } from '@/components/providers';
import type { ModelStatus } from '@/lib/api/modelConnections';
import { withModel } from './modelConnectionChanges';

const inputClass = 'ui-input block min-h-11 w-full min-w-0 rounded-lg font-mono text-sm';

function RemoveIcon() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="h-3.5 w-3.5">
      <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.6" fill="none" />
    </svg>
  );
}

/** One model ID as a compact chip, with the result of the last refused save, if any. */
function ModelChip({
  model,
  result,
  disabled,
  onRemove,
}: {
  model: string;
  result: ModelStatus | undefined;
  disabled: boolean;
  onRemove: () => void;
}) {
  const t = useT();
  const refused = result !== undefined && result.runtimes.length === 0;
  return (
    <li
      className={`flex max-w-full min-w-0 items-start gap-1.5 rounded-md border px-2 py-1 ${
        refused
          ? 'border-amber-300 bg-amber-50 dark:border-amber-500/40 dark:bg-amber-500/10'
          : 'border-gray-200 bg-gray-50 dark:border-white/10 dark:bg-white/5'
      }`}
    >
      <span className="min-w-0">
        <span className="block break-all font-mono text-[12px] text-gray-800 dark:text-gray-100">
          {model}
        </span>
        {result ? (
          <span
            className={`block text-[11px] ${
              refused ? 'text-amber-800 dark:text-amber-300' : 'text-status-success'
            }`}
          >
            {refused
              ? t('connections.modelRefused', { reason: result.message || t('connections.error') })
              : t('connections.agents', { agents: result.runtimes.join(', ') })}
          </span>
        ) : null}
      </span>
      <button
        type="button"
        disabled={disabled}
        aria-label={t('connections.removeModel', { model })}
        onClick={onRemove}
        className="ui-focus-ring mt-0.5 shrink-0 rounded text-gray-400 hover:text-gray-700 disabled:opacity-50 dark:text-gray-500 dark:hover:text-gray-200"
      >
        <RemoveIcon />
      </button>
    </li>
  );
}

/**
 * Model IDs: one line to type an ID, Enter or **Add** to keep it as a chip.
 * An ID still in the line when the form is saved counts too (`pending`).
 */
export function ModelIdsField({
  models,
  draft,
  max,
  results,
  disabled,
  onModels,
  onDraft,
}: {
  models: string[];
  draft: string;
  max: number;
  /** Each model's result from the last refused save, by model ID. */
  results: Record<string, ModelStatus>;
  disabled: boolean;
  onModels: (models: string[]) => void;
  onDraft: (draft: string) => void;
}) {
  const t = useT();
  const id = useId();
  const full = models.length >= max;

  function add() {
    if (!draft.trim() || full) return;
    onModels(withModel(models, draft));
    onDraft('');
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter') {
      // Enter keeps the ID; the form is saved with its own button.
      event.preventDefault();
      add();
    }
  }

  return (
    <div>
      <label htmlFor={`${id}-model`} className="block text-sm font-medium">
        {t('connections.models')}
      </label>
      {models.length ? (
        <ul aria-label={t('connections.modelList')} className="mt-1.5 flex flex-wrap gap-1.5">
          {models.map((model) => (
            <ModelChip
              key={model}
              model={model}
              result={results[model]}
              disabled={disabled}
              onRemove={() => onModels(models.filter((item) => item !== model))}
            />
          ))}
        </ul>
      ) : null}
      <div className="mt-1.5 flex gap-2">
        <input
          id={`${id}-model`}
          className={inputClass}
          value={draft}
          onChange={(event) => onDraft(event.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled || full}
          maxLength={200}
          placeholder={models.length ? t('connections.anotherModel') : 'gpt-4.1'}
          spellCheck={false}
          autoComplete="off"
          aria-describedby={`${id}-hint`}
        />
        <button
          type="button"
          onClick={add}
          disabled={disabled || full || !draft.trim()}
          className="ui-button ui-button-secondary min-h-11 shrink-0"
        >
          {t('connections.addModel')}
        </button>
      </div>
      <p id={`${id}-hint`} className="mt-1 text-xs leading-5 text-muted">
        {t('connections.modelsHint', { max })}
      </p>
    </div>
  );
}
