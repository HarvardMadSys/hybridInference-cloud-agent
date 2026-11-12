'use client';

import { useId, useState, type ReactNode } from 'react';

import { ModelStatusList } from '@/components/features/settings/ModelStatusList';
import { useT } from '@/components/providers/LocaleProvider';
import {
  deleteOperatorModelConnection,
  updateOperatorModelConnection,
  type OperatorModelConnection,
} from '@/lib/api/adminOperatorModels';

import { ProvidedModelAccounts } from './ProvidedModelAccounts';
import { ProvidedModelLimits } from './ProvidedModelLimits';
import { ProvidedModelUsage } from './ProvidedModelUsage';
import { isAccessDenied, modelsWithoutLimit, providedFailure } from './providedModelsForm';

const buttonClass = 'ui-button ui-button-secondary min-h-11';

type Translate = ReturnType<typeof useT>;

/** The connection's settings beyond its models, one fact per line item. */
function facts(connection: OperatorModelConnection, t: Translate): string[] {
  return [
    t(connection.enabled ? 'connections.enabled' : 'connections.disabled'),
    ...(connection.is_default ? [t('adminModels.card.isDefault')] : []),
    ...(connection.default_model
      ? [t('adminModels.card.defaultModel', { model: connection.default_model })]
      : []),
    t('adminModels.card.userConcurrency', { count: connection.user_concurrency }),
    ...(connection.end_user_header
      ? [t('adminModels.card.header', { header: connection.end_user_header })]
      : []),
  ];
}

/**
 * Why a disabled connection cannot be enabled yet, or `''` when it can: a
 * model still has no default allowance, or model connections are switched off
 * for the whole deployment. An enabled connection can always be disabled.
 */
function enableBlocker(
  connection: OperatorModelConnection,
  missing: string[],
  featureOff: boolean,
  t: Translate,
): string {
  if (connection.enabled) return '';
  if (missing.length > 0) {
    return t('adminModels.card.enableBlocked', { models: missing.join(', ') });
  }
  return featureOff ? t('adminModels.card.enableFeatureOff') : '';
}

/**
 * A part of the card that is read from the server only once it is opened, so
 * a closed one asks nothing; closing it again keeps what it showed.
 */
function Disclosure({ show, hide, children }: { show: string; hide: string; children: ReactNode }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [visited, setVisited] = useState(false);
  return (
    <div className="mt-3 border-t border-border pt-2">
      <button
        type="button"
        className="ui-focus-ring inline-flex min-h-9 items-center rounded text-sm font-medium text-accent hover:underline"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => {
          setVisited(true);
          setOpen((value) => !value);
        }}
      >
        {open ? hide : show}
      </button>
      <div id={id} hidden={!open}>
        {visited ? children : null}
      </div>
    </div>
  );
}

/**
 * One provided connection: what it is, whether it is on, each model's
 * verification and default allowance, and, on request, the accounts that
 * differ from the defaults and the requests counted for it.
 *
 * It can be enabled only once every listed model has a default allowance, so
 * nothing is ever offered at a limit nobody chose. Disabling or deleting it
 * stops the agent processes running on it, for every account.
 */
export function ProvidedConnectionCard({
  connection,
  featureOff,
  locked,
  onEdit,
  onChange,
  onDelete,
  onAccessDenied,
}: {
  connection: OperatorModelConnection;
  /** Model connections are switched off for the deployment: nothing can be enabled. */
  featureOff: boolean;
  /** A form is open: the card's own actions wait for it. */
  locked: boolean;
  onEdit: () => void;
  onChange: (connection: OperatorModelConnection) => void;
  onDelete: () => void;
  onAccessDenied?: (cause: unknown) => void;
}) {
  const t = useT();
  const id = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [confirm, setConfirm] = useState<'delete' | 'disable' | null>(null);
  const missing = modelsWithoutLimit(connection);
  // Why Enable is not offered yet, if it is not: said under the card's actions.
  const blockedBy = enableBlocker(connection, missing, featureOff, t);

  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await action();
      setConfirm(null);
    } catch (cause) {
      if (isAccessDenied(cause)) onAccessDenied?.(cause);
      setError(providedFailure(cause, t));
    } finally {
      setBusy(false);
    }
  }

  const setEnabled = (enabled: boolean) =>
    run(async () => onChange(await updateOperatorModelConnection(connection.id, { enabled })));

  const remove = () =>
    run(async () => {
      await deleteOperatorModelConnection(connection.id);
      onDelete();
    });
  const confirmed = () => (confirm === 'delete' ? remove() : setEnabled(false));

  return (
    <article className="min-w-0 rounded-xl border border-border p-4" aria-label={connection.name}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="break-words text-sm font-semibold">{connection.name}</h3>
          <p className="mt-1 break-all text-xs text-muted">{connection.base_url}</p>
          <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted">
            {facts(connection, t).map((fact) => (
              <li key={fact}>{fact}</li>
            ))}
          </ul>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className={buttonClass} onClick={onEdit} disabled={busy || locked}>
            {t('connections.edit')}
          </button>
          <button
            type="button"
            className={buttonClass}
            disabled={busy || locked || blockedBy !== ''}
            aria-busy={busy || undefined}
            aria-describedby={blockedBy ? `${id}-blocked` : undefined}
            onClick={() => {
              if (connection.enabled) setConfirm('disable');
              else void setEnabled(true);
            }}
          >
            {t(connection.enabled ? 'connections.disable' : 'connections.enable')}
          </button>
          <button
            type="button"
            className={buttonClass}
            disabled={busy || locked}
            onClick={() => setConfirm('delete')}
          >
            {t('connections.delete')}
          </button>
        </div>
      </div>
      {blockedBy ? (
        <p id={`${id}-blocked`} className="mt-2 text-xs leading-5 text-status-warning">
          {blockedBy}
        </p>
      ) : null}
      {connection.key_refused_at ? (
        <p role="alert" className="mt-2 text-sm leading-6 text-status-warning">
          {t('adminModels.card.keyRefused', {
            since: new Date(connection.key_refused_at).toLocaleString(),
          })}
        </p>
      ) : null}
      <div role="status" aria-live="polite">
        {busy ? <p className="mt-2 text-sm text-muted">{t('connections.busy')}</p> : null}
      </div>
      {confirm ? (
        <div
          role="group"
          aria-label={t('connections.confirm')}
          className="mt-3 space-y-3 rounded-lg border border-border p-3"
        >
          <p className="text-sm">
            {t(
              confirm === 'delete'
                ? 'adminModels.card.confirmDelete'
                : 'adminModels.card.confirmDisable',
              { name: connection.name },
            )}
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              className={buttonClass}
              disabled={busy}
              onClick={() => void confirmed()}
            >
              {t('connections.confirm')}
            </button>
            <button
              type="button"
              className={buttonClass}
              disabled={busy}
              onClick={() => setConfirm(null)}
            >
              {t('connections.cancel')}
            </button>
          </div>
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="mt-3 text-sm text-status-error">
          {error}
        </p>
      ) : null}
      <ModelStatusList statuses={connection.model_status} />
      <ProvidedModelLimits
        connection={connection}
        onChange={onChange}
        onAccessDenied={onAccessDenied}
      />
      <Disclosure show={t('adminModels.accounts.show')} hide={t('adminModels.accounts.hide')}>
        <ProvidedModelAccounts connection={connection} onAccessDenied={onAccessDenied} />
      </Disclosure>
      <Disclosure show={t('adminModels.usage.showSection')} hide={t('adminModels.usage.hide')}>
        <ProvidedModelUsage connection={connection} onAccessDenied={onAccessDenied} />
      </Disclosure>
    </article>
  );
}
