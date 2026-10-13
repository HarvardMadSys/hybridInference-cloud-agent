'use client';

import { useT } from '@/components/providers';
import type { EnvironmentPhase, EnvironmentVariable, VariableEdit } from '@/lib/api/environments';

export function variableEdits(values: EnvironmentVariable[]): VariableEdit[] {
  return values.map(({ name, secret, phase, value }) => ({
    name,
    secret,
    phase,
    original_name: name,
    operation: 'keep',
    ...(!secret ? { value: value ?? '' } : {}),
  }));
}
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
  function update(index: number, patch: Partial<VariableEdit>) {
    onChange(values.map((value, i) => (i === index ? { ...value, ...patch } : value)));
  }
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted">{t('environment.visibleToCode')}</p>
      <p className="text-xs text-muted">{t('environment.reservedKeys')}</p>
      <p className="text-xs text-muted">{t('environment.shortSecret')}</p>
      {inherited.length > 0 && (
        <details className="text-sm">
          <summary>{t('environment.globalVariables')}</summary>
          {inherited.map((v) => (
            <div key={v.name} className="flex justify-between gap-2 py-1">
              <code>{v.name}</code>
              <span>
                {values.some((e) => e.name === v.name && e.operation !== 'remove')
                  ? t('environment.overridden')
                  : t('environment.inherited')}{' '}
                · {v.secret ? t('environment.secretSet') : v.value}
              </span>
            </div>
          ))}
        </details>
      )}
      {values.map((value, index) =>
        value.operation === 'remove' ? null : (
          <div
            key={index}
            className="grid gap-2 rounded-lg border border-border p-3 sm:grid-cols-2"
          >
            <input
              aria-label={t('environment.variableName')}
              className="ui-input font-mono"
              placeholder="VARIABLE_NAME"
              value={value.name}
              disabled={!!value.original_name}
              onChange={(e) => update(index, { name: e.target.value })}
            />
            <input
              aria-label={`${t('environment.value')} ${value.name}`}
              className="ui-input font-mono"
              type={value.secret ? 'password' : 'text'}
              autoComplete="off"
              value={value.value ?? ''}
              placeholder={
                value.operation === 'keep' ? t('environment.secretSet') : t('environment.value')
              }
              onChange={(e) => update(index, { value: e.target.value, operation: 'replace' })}
            />
            <select
              aria-label={`${t('environment.type')} ${value.name}`}
              className="ui-input"
              value={value.secret ? 'secret' : 'ordinary'}
              onChange={(e) =>
                update(index, {
                  secret: e.target.value === 'secret',
                  ...(value.secret && e.target.value === 'ordinary'
                    ? { operation: 'replace', value: '' }
                    : {}),
                })
              }
            >
              <option value="secret">{t('environment.secret')}</option>
              <option value="ordinary">{t('environment.ordinary')}</option>
            </select>
            <div className="flex gap-2">
              <select
                aria-label={`${t('environment.phase')} ${value.name}`}
                className="ui-input min-w-0 flex-1"
                value={value.phase}
                onChange={(e) => update(index, { phase: e.target.value as EnvironmentPhase })}
              >
                <option value="both">{t('environment.both')}</option>
                <option value="setup">{t('environment.setupOnly')}</option>
                <option value="runtime">{t('environment.runtimeOnly')}</option>
              </select>
              <button
                type="button"
                className="ui-button ui-button-ghost"
                onClick={() => update(index, { operation: 'remove', value: undefined })}
              >
                {t('environment.remove')}
              </button>
            </div>
          </div>
        ),
      )}
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
        {t('environment.addVariable')}
      </button>
    </div>
  );
}
