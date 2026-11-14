'use client';

import { useId, useState } from 'react';

import { useT } from '@/components/providers/LocaleProvider';
import { Select } from '@/components/ui/Select';
import {
  MAX_DAILY_REQUESTS,
  MAX_GLOBAL_CONCURRENCY,
  setOperatorModelLimit,
  type OperatorModelConnection,
  type OperatorModelLimit,
} from '@/lib/api/adminOperatorModels';

import {
  allowanceMode,
  allowanceValue,
  concurrencyValue,
  countText,
  isAccessDenied,
  providedFailure,
  type AllowanceMode,
} from './providedModelsForm';

const inputClass = 'ui-input mt-1 block min-h-11 w-full rounded-lg';

/**
 * How a daily allowance is chosen: a number of requests, unlimited, or zero.
 * `zeroLabel` says what zero means where it is used: for a model's default,
 * only accounts given their own allowance; for one account, nothing.
 */
export function AllowanceInput({
  idPrefix,
  mode,
  amount,
  zeroLabel,
  disabled,
  onMode,
  onAmount,
}: {
  idPrefix: string;
  mode: AllowanceMode;
  amount: string;
  zeroLabel: string;
  disabled: boolean;
  onMode: (mode: AllowanceMode) => void;
  onAmount: (amount: string) => void;
}) {
  const t = useT();
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      <div>
        <label htmlFor={`${idPrefix}-mode`} className="block text-xs font-medium text-muted">
          {t('adminModels.limits.allowance')}
        </label>
        <Select<AllowanceMode>
          id={`${idPrefix}-mode`}
          className={inputClass}
          value={mode}
          disabled={disabled}
          onValueChange={onMode}
          placeholder={t('adminModels.limits.choose')}
          options={[
            { value: 'limited', label: t('adminModels.limits.limited') },
            { value: 'unlimited', label: t('adminModels.limits.unlimited') },
            { value: 'named', label: zeroLabel },
          ]}
        />
      </div>
      {mode === 'limited' ? (
        <div>
          <label htmlFor={`${idPrefix}-amount`} className="block text-xs font-medium text-muted">
            {t('adminModels.limits.requestsPerDay')}
          </label>
          <input
            id={`${idPrefix}-amount`}
            className={inputClass}
            inputMode="numeric"
            value={amount}
            disabled={disabled}
            maxLength={8}
            autoComplete="off"
            onChange={(event) => onAmount(event.target.value)}
          />
        </div>
      ) : null}
    </div>
  );
}

/** A saved daily allowance, in a sentence. */
function allowanceSummary(daily: number | null, t: ReturnType<typeof useT>): string {
  if (daily === null) return t('adminModels.limits.savedUnlimited');
  if (daily === 0) return t('adminModels.limits.savedNamed');
  return t('adminModels.limits.savedLimited', { count: countText(daily) });
}

/** One model's saved default, in a sentence. */
function savedSummary(limit: OperatorModelLimit | undefined, t: ReturnType<typeof useT>): string {
  if (!limit) return t('adminModels.limits.unset');
  const allowance = allowanceSummary(limit.user_daily_requests, t);
  if (limit.global_concurrency === null) return allowance;
  const count = countText(limit.global_concurrency);
  return t('adminModels.limits.savedWithCap', { allowance, count });
}

/** One model's default allowance and its optional deployment-wide concurrency cap. */
function ModelLimitRow({
  connection,
  model,
  onChange,
  onAccessDenied,
}: {
  connection: OperatorModelConnection;
  model: string;
  onChange: (connection: OperatorModelConnection) => void;
  onAccessDenied?: (cause: unknown) => void;
}) {
  const t = useT();
  const id = useId();
  const saved = connection.limits?.[model];
  const [mode, setMode] = useState<AllowanceMode>(() => allowanceMode(saved));
  const [amount, setAmount] = useState(() =>
    saved?.user_daily_requests ? String(saved.user_daily_requests) : '',
  );
  const [cap, setCap] = useState(() =>
    saved?.global_concurrency ? String(saved.global_concurrency) : '',
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const daily = allowanceValue(mode, amount);
  const concurrency = concurrencyValue(cap);
  const valid = daily !== undefined && concurrency !== undefined;
  const changed =
    !saved || saved.user_daily_requests !== daily || saved.global_concurrency !== concurrency;

  async function save() {
    if (busy || daily === undefined || concurrency === undefined) return;
    setBusy(true);
    setError('');
    try {
      onChange(
        await setOperatorModelLimit(connection.id, {
          model,
          user_daily_requests: daily,
          global_concurrency: concurrency,
        }),
      );
    } catch (cause) {
      if (isAccessDenied(cause)) onAccessDenied?.(cause);
      setError(providedFailure(cause, t));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="py-3" aria-label={model}>
      <p className="break-all font-mono text-[13px]">{model}</p>
      <p className={`mt-0.5 text-xs ${saved ? 'text-muted' : 'text-status-warning'}`}>
        {savedSummary(saved, t)}
      </p>
      <div className="mt-2 grid gap-2 lg:grid-cols-[2fr_1fr_auto] lg:items-end">
        <AllowanceInput
          idPrefix={id}
          mode={mode}
          amount={amount}
          zeroLabel={t('adminModels.limits.named')}
          disabled={busy}
          onMode={setMode}
          onAmount={setAmount}
        />
        <div>
          <label htmlFor={`${id}-cap`} className="block text-xs font-medium text-muted">
            {t('adminModels.limits.globalConcurrency')}
          </label>
          <input
            id={`${id}-cap`}
            className={inputClass}
            inputMode="numeric"
            value={cap}
            disabled={busy}
            maxLength={4}
            autoComplete="off"
            placeholder={t('adminModels.limits.noCap')}
            onChange={(event) => setCap(event.target.value)}
          />
        </div>
        <button
          type="button"
          className="ui-button ui-button-secondary min-h-11"
          disabled={busy || !valid || !changed}
          aria-busy={busy || undefined}
          aria-label={t('adminModels.limits.saveFor', { model })}
          onClick={() => void save()}
        >
          {t(busy ? 'connections.busy' : 'connections.save')}
        </button>
      </div>
      {mode !== 'unset' && !valid ? (
        <p className="mt-1 text-xs leading-5 text-status-error">
          {t('adminModels.limits.invalid', {
            requests: countText(MAX_DAILY_REQUESTS),
            concurrency: countText(MAX_GLOBAL_CONCURRENCY),
          })}
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="mt-1 text-sm text-status-error">
          {error}
        </p>
      ) : null}
    </li>
  );
}

/**
 * Each listed model's default allowance: how many requests an account may send
 * per UTC day (a number, unlimited, or zero for "only accounts given their
 * own"), and optionally how many requests may be in flight across the whole
 * deployment. Every model needs one before the connection can be enabled.
 */
export function ProvidedModelLimits({
  connection,
  onChange,
  onAccessDenied,
}: {
  connection: OperatorModelConnection;
  onChange: (connection: OperatorModelConnection) => void;
  onAccessDenied?: (cause: unknown) => void;
}) {
  const t = useT();
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="mt-4">
      <h4 id={headingId} className="text-sm font-semibold">
        {t('adminModels.limits.title')}
      </h4>
      <p className="mt-1 text-xs leading-5 text-muted">{t('adminModels.limits.description')}</p>
      <ul className="mt-1 divide-y divide-border">
        {connection.models.map((model) => (
          <ModelLimitRow
            // A saved limit restarts the row from what the server now holds.
            key={`${model}:${JSON.stringify(connection.limits?.[model] ?? null)}`}
            connection={connection}
            model={model}
            onChange={onChange}
            onAccessDenied={onAccessDenied}
          />
        ))}
      </ul>
    </section>
  );
}
