'use client';

import { useId, useRef, useState, type FormEvent } from 'react';

import { useT } from '@/components/providers/LocaleProvider';
import {
  getOperatorModelUsage,
  type OperatorModelConnection,
  type OperatorModelUsage,
} from '@/lib/api/adminOperatorModels';

import { countText, isAccessDenied, providedFailure, utcDay } from './providedModelsForm';

const inputClass = 'ui-input mt-1 block min-h-11 w-full rounded-lg';
const cell = 'px-3 py-2 align-top';
const DAY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Requests per UTC day, account and model over a range of days, read on
 * request. These are the requests this deployment's relay accepted, which is
 * what an allowance counts: not tokens, and not what the provider charged.
 */
export function ProvidedModelUsage({
  connection,
  onAccessDenied,
}: {
  connection: OperatorModelConnection;
  onAccessDenied?: (cause: unknown) => void;
}) {
  const t = useT();
  const id = useId();
  const [from, setFrom] = useState(() => utcDay(6));
  const [to, setTo] = useState(() => utcDay());
  const [usage, setUsage] = useState<OperatorModelUsage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const request = useRef(0);
  const valid = DAY.test(from) && DAY.test(to) && from <= to;

  async function show(event: FormEvent) {
    event.preventDefault();
    if (!valid) return;
    const current = ++request.current;
    setLoading(true);
    setError('');
    try {
      const answer = await getOperatorModelUsage(connection.id, { from, to });
      if (current === request.current) setUsage(answer);
    } catch (cause) {
      if (current !== request.current) return;
      if (isAccessDenied(cause)) onAccessDenied?.(cause);
      setError(providedFailure(cause, t));
    } finally {
      if (current === request.current) setLoading(false);
    }
  }

  return (
    <section aria-labelledby={`${id}-title`} className="mt-4">
      <h4 id={`${id}-title`} className="text-sm font-semibold">
        {t('adminModels.usage.title')}
      </h4>
      <p className="mt-1 text-xs leading-5 text-muted">{t('adminModels.usage.description')}</p>
      <form
        onSubmit={(event) => void show(event)}
        className="mt-2 grid gap-2 sm:grid-cols-[1fr_1fr_auto] sm:items-end"
      >
        <div>
          <label htmlFor={`${id}-from`} className="block text-xs font-medium text-muted">
            {t('adminModels.usage.from')}
          </label>
          <input
            id={`${id}-from`}
            type="date"
            className={inputClass}
            value={from}
            max={to}
            onChange={(event) => setFrom(event.target.value)}
          />
        </div>
        <div>
          <label htmlFor={`${id}-to`} className="block text-xs font-medium text-muted">
            {t('adminModels.usage.to')}
          </label>
          <input
            id={`${id}-to`}
            type="date"
            className={inputClass}
            value={to}
            min={from}
            onChange={(event) => setTo(event.target.value)}
          />
        </div>
        <button
          type="submit"
          className="ui-button ui-button-secondary min-h-11"
          disabled={loading || !valid}
          aria-busy={loading || undefined}
        >
          {t(loading ? 'adminModels.usage.loading' : 'adminModels.usage.show')}
        </button>
      </form>
      {!valid ? (
        <p className="mt-1 text-xs leading-5 text-status-error">{t('adminModels.usage.invalid')}</p>
      ) : null}
      {error ? (
        <p role="alert" className="mt-2 text-sm text-status-error">
          {error}
        </p>
      ) : null}
      {usage && usage.rows.length === 0 ? (
        <p className="mt-2 text-sm text-muted">{t('adminModels.usage.empty')}</p>
      ) : null}
      {usage && usage.rows.length > 0 ? (
        <div className="mt-2 overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[32rem] text-left text-sm">
            <caption className="sr-only">{t('adminModels.usage.title')}</caption>
            <thead className="border-b border-border bg-surface-muted text-xs text-muted">
              <tr>
                <th scope="col" className={`${cell} font-medium`}>
                  {t('adminModels.usage.day')}
                </th>
                <th scope="col" className={`${cell} font-medium`}>
                  {t('adminModels.accounts.account')}
                </th>
                <th scope="col" className={`${cell} font-medium`}>
                  {t('adminModels.accounts.model')}
                </th>
                <th scope="col" className={`${cell} text-right font-medium`}>
                  {t('adminModels.usage.requests')}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {usage.rows.map((row) => (
                <tr key={`${row.day}:${row.account_id}:${row.model}`}>
                  <td className={`${cell} whitespace-nowrap tabular-nums`}>{row.day}</td>
                  <td className={`${cell} break-all`} title={row.account_id}>
                    {row.email || row.account_id}
                  </td>
                  <td className={`${cell} break-all font-mono text-[13px]`}>{row.model}</td>
                  <td className={`${cell} text-right tabular-nums`}>{countText(row.requests)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {usage?.truncated ? (
        <p className="mt-2 text-xs leading-5 text-status-warning">
          {t('adminModels.usage.truncated')}
        </p>
      ) : null}
    </section>
  );
}
