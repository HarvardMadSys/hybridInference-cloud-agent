'use client';

import { useId, useState, type KeyboardEvent } from 'react';
import { useT } from '@/components/providers';
import { connectionReason, connectionAgentNames } from './modelConnectionDisplay';
import type { ModelStatus } from '@/lib/api/modelConnections';
import { isModelId, withModel } from './modelConnectionChanges';
import { ModelSuggestions } from './ModelSuggestions';
import type { Suggestions } from './useModelSuggestions';

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
      className={`flex max-w-full min-w-0 items-center gap-1 rounded-md border py-1 pl-2 ${
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
              ? t('connections.modelRefused', { reason: connectionReason(result.category, t) })
              : t('connections.agents', { agents: connectionAgentNames(result.runtimes) })}
          </span>
        ) : null}
      </span>
      <button
        type="button"
        disabled={disabled}
        aria-label={t('connections.removeModel', { model })}
        onClick={onRemove}
        // A small icon in a touch-sized target, kept inside the chip's height.
        className="ui-focus-ring -my-1 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded text-gray-400 hover:text-gray-700 disabled:opacity-50 dark:text-gray-500 dark:hover:text-gray-200"
      >
        <RemoveIcon />
      </button>
    </li>
  );
}

/** Model ID suggestions from the provider's list, and how to ask for them. */
export interface ModelSuggest {
  suggestions: Suggestions | null;
  /** What the form still needs before it can ask; `null` when it can. */
  missing: 'details' | 'newKey' | null;
  onLoad: () => void;
}

/**
 * Model IDs: one line to type an ID, Enter or **Add** to keep it as a chip.
 * An ID still in the line when the form is saved counts too (`pending`).
 * Below it, the provider's own list can be loaded to pick IDs from; typing
 * in the line narrows it, and any ID can still be typed by hand.
 */
export function ModelIdsField({
  models,
  draft,
  max,
  results,
  invalid,
  disabled,
  suggest,
  onModels,
  onDraft,
}: {
  models: string[];
  draft: string;
  max: number;
  /** Each model's result from the last refused save, by model ID. */
  results: Record<string, ModelStatus>;
  /** A model ID the form or the server found malformed. */
  invalid: boolean;
  disabled: boolean;
  suggest: ModelSuggest;
  onModels: (models: string[]) => void;
  onDraft: (draft: string) => void;
}) {
  const t = useT();
  const id = useId();
  const full = models.length >= max;
  const [malformed, setMalformed] = useState(false);
  const problem = malformed || invalid;

  function add() {
    if (!draft.trim() || full) return;
    if (!isModelId(draft)) {
      setMalformed(true);
      return;
    }
    onModels(withModel(models, draft));
    onDraft('');
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
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
          onChange={(event) => {
            setMalformed(false);
            onDraft(event.target.value);
          }}
          onKeyDown={onKeyDown}
          disabled={disabled || full}
          maxLength={200}
          placeholder={models.length ? t('connections.anotherModel') : 'gpt-4.1'}
          spellCheck={false}
          autoComplete="off"
          aria-invalid={problem || undefined}
          aria-describedby={problem ? `${id}-problem ${id}-hint` : `${id}-hint`}
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
      {problem ? (
        <p id={`${id}-problem`} className="mt-1 text-xs leading-5 text-status-error">
          {t('connections.invalid.model')}
        </p>
      ) : null}
      <p id={`${id}-hint`} className="mt-1 text-xs leading-5 text-muted">
        {t('connections.modelsHint', { max })}
      </p>
      <ModelSuggestions
        {...suggest}
        models={models}
        draft={draft}
        max={max}
        disabled={disabled}
        onPick={(model) => {
          if (full) return;
          setMalformed(false);
          onModels(withModel(models, model));
          // The line was narrowing the list; picking is what it was typed for.
          onDraft('');
        }}
      />
    </div>
  );
}
