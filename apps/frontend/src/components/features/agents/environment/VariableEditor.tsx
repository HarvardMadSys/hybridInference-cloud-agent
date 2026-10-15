'use client';

import { useState } from 'react';
import { useT } from '@/components/providers';
import type { EnvironmentPhase, EnvironmentVariable, VariableEdit } from '@/lib/api/environments';

export function variableEdits(values: EnvironmentVariable[]): VariableEdit[] {
  return values.map(({ name, secret, phase, value }) => ({
    name,
    secret,
    phase,
    original_name: name,
    original_secret: secret,
    operation: 'keep',
    ...(!secret ? { value: value ?? '', original_value: value ?? '' } : {}),
  }));
}

/** A saved row back at its saved type and value: keep it, rather than replace it. */
function asSaved(value: VariableEdit): Partial<VariableEdit> | null {
  if (!value.original_name || value.secret !== value.original_secret) return null;
  if (value.original_secret) {
    // A Secret's value is never shown: an empty field means "leave it as it is".
    return value.value ? null : { operation: 'keep', value: undefined };
  }
  return value.value === value.original_value ? { operation: 'keep' } : null;
}

export function RemoveIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
    >
      <path strokeLinecap="round" d="m6 6 8 8M14 6l-8 8" />
    </svg>
  );
}

function MoreIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor">
      <circle cx="5" cy="12" r="1.6" />
      <circle cx="12" cy="12" r="1.6" />
      <circle cx="19" cy="12" r="1.6" />
    </svg>
  );
}

const PHASE_LABELS = {
  both: 'environment.both',
  setup: 'environment.setupOnly',
  runtime: 'environment.runtimeOnly',
} as const;

/**
 * Rows of name, value and type. When a variable is available is a rarer
 * choice — almost every variable is wanted in both stages — so it sits behind
 * each row's options button and shows as a tag only when it is not the usual.
 *
 * `inherited` are the owner's global variables. Each one not redefined here is
 * listed read-only, with a way to redefine it for this environment.
 */
export function VariableEditor({
  values,
  onChange,
  inherited = [],
}: {
  values: VariableEdit[];
  onChange: (values: VariableEdit[]) => void;
  inherited?: EnvironmentVariable[];
}) {
  const t = useT();
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  function update(index: number, patch: Partial<VariableEdit>) {
    onChange(
      values.map((value, i) => {
        if (i !== index) return value;
        const next = { ...value, ...patch };
        return next.operation === 'remove' ? next : { ...next, ...asSaved(next) };
      }),
    );
  }
  function remove(index: number) {
    // A saved variable is removed by the save; one added since is just dropped.
    if (values[index].original_name) update(index, { operation: 'remove', value: undefined });
    else onChange(values.filter((_, i) => i !== index));
    setExpanded(new Set());
  }
  function toggle(index: number) {
    const next = new Set(expanded);
    if (next.has(index)) next.delete(index);
    else next.add(index);
    setExpanded(next);
  }
  const defined = new Set(values.filter((v) => v.operation !== 'remove').map((v) => v.name));
  const fromGlobal = inherited.filter((v) => !defined.has(v.name));
  const rows = values.filter((v) => v.operation !== 'remove');
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">{t('environment.variablesHint')}</p>
      {rows.length === 0 && fromGlobal.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted">
          {t('environment.noVariablesYet')}
        </p>
      ) : (
        <div className="divide-y divide-border rounded-lg border border-border">
          {values.map((value, index) => {
            if (value.operation === 'remove') return null;
            const open = expanded.has(index);
            const label = value.name || t('environment.variableName');
            return (
              <div key={index} className="space-y-2 p-2">
                <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)_7rem_auto]">
                  {/* A saved variable keeps its name: renaming is removing and adding. */}
                  {value.original_name ? (
                    <code className="flex min-h-8 min-w-0 items-center truncate px-1 text-sm text-foreground">
                      {value.name}
                    </code>
                  ) : (
                    <input
                      aria-label={t('environment.variableName')}
                      className="ui-input min-w-0 font-mono"
                      placeholder="VARIABLE_NAME"
                      value={value.name}
                      autoComplete="off"
                      spellCheck={false}
                      onChange={(e) => update(index, { name: e.target.value })}
                    />
                  )}
                  <div className="flex items-center gap-1 sm:order-last">
                    <button
                      type="button"
                      className="ui-icon-button ui-button-ghost"
                      aria-label={t('environment.moreOptions', { name: label })}
                      aria-expanded={open}
                      onClick={() => toggle(index)}
                    >
                      <MoreIcon />
                    </button>
                    <button
                      type="button"
                      className="ui-icon-button ui-button-ghost hover:!text-status-error"
                      aria-label={t('environment.removeVariable', { name: label })}
                      onClick={() => remove(index)}
                    >
                      <RemoveIcon />
                    </button>
                  </div>
                  <input
                    aria-label={`${t('environment.value')} ${value.name}`}
                    className="ui-input col-span-2 min-w-0 font-mono sm:col-span-1"
                    type={value.secret ? 'password' : 'text'}
                    autoComplete="off"
                    spellCheck={false}
                    value={value.value ?? ''}
                    placeholder={
                      value.operation === 'keep' && value.secret
                        ? t('environment.secretSet')
                        : t('environment.value')
                    }
                    onChange={(e) => update(index, { value: e.target.value, operation: 'replace' })}
                  />
                  <select
                    aria-label={`${t('environment.type')} ${value.name}`}
                    className="ui-input col-span-2 sm:col-span-1"
                    value={value.secret ? 'secret' : 'ordinary'}
                    onChange={(e) => {
                      const secret = e.target.value === 'secret';
                      // A typed or saved Secret is never shown in a plain field.
                      // A saved ordinary value that was only marked Secret is
                      // kept as it was.
                      const hidden =
                        value.operation === 'replace' || value.original_secret !== false;
                      update(index, {
                        secret,
                        ...(value.secret && !secret && hidden
                          ? { operation: 'replace', value: '' }
                          : {}),
                      });
                    }}
                  >
                    <option value="secret">{t('environment.secret')}</option>
                    <option value="ordinary">{t('environment.ordinary')}</option>
                  </select>
                </div>
                {value.phase !== 'both' && !open ? (
                  <p className="px-1 text-xs text-muted">{t(PHASE_LABELS[value.phase])}</p>
                ) : null}
                {open ? (
                  <div className="space-y-1 rounded-md bg-surface-muted p-2">
                    <label className="flex flex-wrap items-center gap-2 text-sm">
                      <span className="text-muted">{t('environment.phase')}</span>
                      <select
                        aria-label={`${t('environment.phase')} ${value.name}`}
                        className="ui-input"
                        value={value.phase}
                        onChange={(e) =>
                          update(index, { phase: e.target.value as EnvironmentPhase })
                        }
                      >
                        <option value="both">{t('environment.both')}</option>
                        <option value="setup">{t('environment.setupOnly')}</option>
                        <option value="runtime">{t('environment.runtimeOnly')}</option>
                      </select>
                    </label>
                    <p className="text-xs text-muted">{t('environment.phaseHelp')}</p>
                  </div>
                ) : null}
              </div>
            );
          })}
          {fromGlobal.map((v) => (
            <div
              key={`global-${v.name}`}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 bg-surface-muted/50 px-3 py-2"
            >
              <code className="min-w-0 truncate text-sm text-foreground">{v.name}</code>
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted">
                {v.secret ? '••••••' : v.value}
              </span>
              <span className="text-xs text-muted">{t('environment.fromGlobal')}</span>
              <button
                type="button"
                className="ui-button ui-button-ghost text-xs"
                onClick={() =>
                  onChange([
                    ...values,
                    {
                      name: v.name,
                      secret: v.secret,
                      phase: v.phase,
                      value: '',
                      operation: 'replace',
                    },
                  ])
                }
              >
                {t('environment.overrideHere')}
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button
          type="button"
          className="ui-button ui-button-secondary"
          onClick={() =>
            onChange([
              ...values,
              { name: '', secret: true, phase: 'both', value: '', operation: 'replace' },
            ])
          }
        >
          <span aria-hidden="true">+</span>
          {t('environment.addVariable')}
        </button>
        <details className="text-xs text-muted">
          <summary className="cursor-pointer select-none hover:text-foreground">
            {t('environment.rules')}
          </summary>
          <ul className="mt-2 max-w-md list-disc space-y-1 pl-4">
            <li>{t('environment.inferenceKeys')}</li>
            <li>{t('environment.reservedKeys')}</li>
            <li>{t('environment.shortSecret')}</li>
          </ul>
        </details>
      </div>
    </div>
  );
}
