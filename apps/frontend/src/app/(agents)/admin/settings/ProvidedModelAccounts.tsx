'use client';

import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from 'react';

import { useT } from '@/components/providers/LocaleProvider';
import { Select } from '@/components/ui/Select';
import {
  clearOperatorModelUserLimit,
  listOperatorModelUsers,
  setOperatorModelUserLimit,
  type OperatorModelConnection,
  type OperatorModelUser,
} from '@/lib/api/adminOperatorModels';
import { APIError } from '@/lib/utils/errors';

import { AllowanceInput } from './ProvidedModelLimits';
import {
  allowanceValue,
  countText,
  isAccessDenied,
  providedFailure,
  type AllowanceMode,
} from './providedModelsForm';

const inputClass = 'ui-input mt-1 block min-h-11 w-full rounded-lg';
const cell = 'px-3 py-2 align-top';
/** The refusal of a clear for an account that has no allowance of its own (any more). */
const NO_OWN_LIMIT = 'operator_model_user_limit_not_found';

type Translate = ReturnType<typeof useT>;

/** One account's line for one model: its own allowance, if it has one, and today's use. */
interface AccountRow {
  user: OperatorModelUser;
  model: string;
  /** `undefined`: the account follows the model's default. */
  limit: number | null | undefined;
  used: number;
}

/** A line per account and model that has an own allowance or use today. */
function accountRows(users: OperatorModelUser[]): AccountRow[] {
  return users.flatMap((user) => {
    const models = [...new Set([...Object.keys(user.limits), ...Object.keys(user.used_today)])];
    return models.sort().map((model) => ({
      user,
      model,
      limit: Object.hasOwn(user.limits, model) ? user.limits[model] : undefined,
      used: user.used_today[model] ?? 0,
    }));
  });
}

function limitText(limit: number | null | undefined, t: Translate): string {
  if (limit === undefined) return t('adminModels.accounts.followsDefault');
  if (limit === null) return t('adminModels.limits.unlimited');
  return limit === 0 ? t('adminModels.accounts.withheld') : countText(limit);
}

/** An account as the table names it: its email, with the id kept beside it. */
function AccountName({ user }: { user: OperatorModelUser }) {
  return (
    <>
      <span className="block break-all">{user.email || user.account_id}</span>
      {user.email ? (
        <span className="block break-all font-mono text-[11px] text-muted">{user.account_id}</span>
      ) : null}
    </>
  );
}

/** `users` with one account's row replaced or added. */
function withUser(users: OperatorModelUser[], row: OperatorModelUser): OperatorModelUser[] {
  return users.some((user) => user.account_id === row.account_id)
    ? users.map((user) => (user.account_id === row.account_id ? row : user))
    : [...users, row];
}

/** `users` with one account's own allowance on a model removed. */
function withoutLimit(
  users: OperatorModelUser[],
  accountId: string,
  model: string,
): OperatorModelUser[] {
  return users.flatMap((user) => {
    if (user.account_id !== accountId) return [user];
    const limits = Object.fromEntries(Object.entries(user.limits).filter(([key]) => key !== model));
    // An account with no allowance of its own and no use today has no line.
    const listed = Object.keys(limits).length > 0 || Object.keys(user.used_today).length > 0;
    return listed ? [{ ...user, limits }] : [];
  });
}

/** Give one account, named by email or id, an allowance of its own on one model. */
function SetAllowanceForm({
  connection,
  onSaved,
  onAccessDenied,
}: {
  connection: OperatorModelConnection;
  onSaved: (user: OperatorModelUser) => void;
  onAccessDenied?: (cause: unknown) => void;
}) {
  const t = useT();
  const id = useId();
  const [account, setAccount] = useState('');
  const [model, setModel] = useState(connection.models[0] ?? '');
  const [mode, setMode] = useState<AllowanceMode>('limited');
  const [amount, setAmount] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const daily = allowanceValue(mode, amount);
  const chosen = connection.models.includes(model) ? model : (connection.models[0] ?? '');

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy || daily === undefined || !account.trim() || !chosen) return;
    setBusy(true);
    setError('');
    try {
      onSaved(
        await setOperatorModelUserLimit(connection.id, {
          account: account.trim(),
          model: chosen,
          daily_requests: daily,
        }),
      );
      setAccount('');
    } catch (cause) {
      if (isAccessDenied(cause)) onAccessDenied?.(cause);
      setError(providedFailure(cause, t));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void save(event)}
      aria-label={t('adminModels.accounts.setTitle')}
      className="mt-3 rounded-lg border border-border p-3"
    >
      <p className="text-sm font-medium">{t('adminModels.accounts.setTitle')}</p>
      <div className="mt-2 grid gap-2 lg:grid-cols-2">
        <div>
          <label htmlFor={`${id}-account`} className="block text-xs font-medium text-muted">
            {t('adminModels.accounts.account')}
          </label>
          <input
            id={`${id}-account`}
            className={inputClass}
            value={account}
            disabled={busy}
            maxLength={320}
            autoComplete="off"
            spellCheck={false}
            placeholder={t('adminModels.accounts.accountPlaceholder')}
            onChange={(event) => setAccount(event.target.value)}
          />
        </div>
        <div>
          <label htmlFor={`${id}-model`} className="block text-xs font-medium text-muted">
            {t('adminModels.accounts.model')}
          </label>
          <Select
            id={`${id}-model`}
            className={inputClass}
            value={chosen}
            disabled={busy}
            onValueChange={setModel}
            options={connection.models.map((item) => ({ value: item, label: item }))}
          />
        </div>
      </div>
      <div className="mt-2">
        <AllowanceInput
          idPrefix={id}
          mode={mode}
          amount={amount}
          zeroLabel={t('adminModels.accounts.withhold')}
          disabled={busy}
          onMode={setMode}
          onAmount={setAmount}
        />
      </div>
      <button
        type="submit"
        className="ui-button ui-button-secondary mt-3 min-h-11"
        disabled={busy || daily === undefined || !account.trim() || !chosen}
        aria-busy={busy || undefined}
      >
        {t(busy ? 'connections.busy' : 'adminModels.accounts.set')}
      </button>
      {error ? (
        <p role="alert" className="mt-2 text-sm text-status-error">
          {error}
        </p>
      ) : null}
    </form>
  );
}

/**
 * The accounts that differ from the defaults on this connection: those given
 * an allowance of their own, and those with use today. An allowance is set
 * for an account by email or id, and cleared to put it back on the default.
 */
export function ProvidedModelAccounts({
  connection,
  onAccessDenied,
}: {
  connection: OperatorModelConnection;
  onAccessDenied?: (cause: unknown) => void;
}) {
  const t = useT();
  const headingId = useId();
  const [users, setUsers] = useState<OperatorModelUser[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState('');
  const [error, setError] = useState('');
  const request = useRef(0);
  const connectionId = connection.id;

  const fail = useCallback(
    (cause: unknown) => {
      if (isAccessDenied(cause)) onAccessDenied?.(cause);
      setError(providedFailure(cause, t));
    },
    [onAccessDenied, t],
  );

  /** The first page, or the page after `after`, added to the ones already shown. */
  const load = useCallback(
    async (after: string | null) => {
      const current = ++request.current;
      setLoading(true);
      setError('');
      try {
        const page = await listOperatorModelUsers(connectionId, { cursor: after });
        if (current !== request.current) return;
        setUsers((shown) => (after && shown ? [...shown, ...page.users] : page.users));
        setCursor(page.next_cursor);
      } catch (cause) {
        if (current === request.current) fail(cause);
      } finally {
        if (current === request.current) setLoading(false);
      }
    },
    [connectionId, fail],
  );

  useEffect(() => {
    void load(null);
    return () => {
      request.current += 1;
    };
  }, [load]);

  async function clear(row: AccountRow) {
    const key = `${row.user.account_id}:${row.model}`;
    if (clearing) return;
    setClearing(key);
    setError('');
    let cleared = true;
    try {
      await clearOperatorModelUserLimit(connectionId, row.user.account_id, row.model);
    } catch (cause) {
      // Already cleared elsewhere: the line only has to stop saying otherwise.
      cleared = cause instanceof APIError && cause.code === NO_OWN_LIMIT;
      if (!cleared) fail(cause);
    } finally {
      setClearing('');
    }
    if (cleared) setUsers((shown) => withoutLimit(shown ?? [], row.user.account_id, row.model));
  }

  const rows = accountRows(users ?? []);
  return (
    <section aria-labelledby={headingId} className="mt-4">
      <h4 id={headingId} className="text-sm font-semibold">
        {t('adminModels.accounts.title')}
      </h4>
      <p className="mt-1 text-xs leading-5 text-muted">{t('adminModels.accounts.description')}</p>
      {error ? (
        <p role="alert" className="mt-2 text-sm text-status-error">
          {error}
        </p>
      ) : null}
      {users && rows.length === 0 ? (
        <p className="mt-2 text-sm text-muted">{t('adminModels.accounts.empty')}</p>
      ) : null}
      {rows.length > 0 ? (
        <div className="mt-2 overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[34rem] text-left text-sm">
            <caption className="sr-only">{t('adminModels.accounts.title')}</caption>
            <thead className="border-b border-border bg-surface-muted text-xs text-muted">
              <tr>
                <th scope="col" className={`${cell} font-medium`}>
                  {t('adminModels.accounts.account')}
                </th>
                <th scope="col" className={`${cell} font-medium`}>
                  {t('adminModels.accounts.model')}
                </th>
                <th scope="col" className={`${cell} font-medium`}>
                  {t('adminModels.accounts.ownAllowance')}
                </th>
                <th scope="col" className={`${cell} text-right font-medium`}>
                  {t('adminModels.accounts.usedToday')}
                </th>
                <th scope="col" className={cell}>
                  <span className="sr-only">{t('adminModels.accounts.actions')}</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((row) => {
                const key = `${row.user.account_id}:${row.model}`;
                return (
                  <tr key={key}>
                    <td className={cell}>
                      <AccountName user={row.user} />
                    </td>
                    <td className={`${cell} break-all font-mono text-[13px]`}>{row.model}</td>
                    <td className={cell}>{limitText(row.limit, t)}</td>
                    <td className={`${cell} text-right tabular-nums`}>{countText(row.used)}</td>
                    <td className={`${cell} text-right`}>
                      {row.limit !== undefined ? (
                        <button
                          type="button"
                          className="ui-button ui-button-secondary min-h-9"
                          disabled={clearing !== ''}
                          aria-busy={clearing === key || undefined}
                          aria-label={t('adminModels.accounts.clearFor', {
                            account: row.user.email || row.user.account_id,
                            model: row.model,
                          })}
                          onClick={() => void clear(row)}
                        >
                          {t('adminModels.accounts.clear')}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
      <div role="status" aria-live="polite">
        {loading ? (
          <p className="mt-2 text-sm text-muted">{t('adminModels.accounts.loading')}</p>
        ) : null}
      </div>
      {cursor && !loading ? (
        <button
          type="button"
          className="ui-button ui-button-secondary mt-2 min-h-11"
          onClick={() => void load(cursor)}
        >
          {t('adminModels.accounts.more')}
        </button>
      ) : null}
      <SetAllowanceForm
        connection={connection}
        onSaved={(user) => setUsers((shown) => withUser(shown ?? [], user))}
        onAccessDenied={onAccessDenied}
      />
    </section>
  );
}
