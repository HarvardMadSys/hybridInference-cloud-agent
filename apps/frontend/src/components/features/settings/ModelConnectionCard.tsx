'use client';

import { useState } from 'react';
import { useT } from '@/components/providers';
import {
  deleteModelConnection,
  updateModelConnection,
  type ModelConnection,
} from '@/lib/api/modelConnections';

import { ModelConnectionTools } from './ModelConnectionTools';

const buttonClass = 'ui-button ui-button-secondary min-h-11';

export function ModelConnectionCard({
  connection,
  available,
  onEdit,
  onChange,
  onDelete,
}: {
  connection: ModelConnection;
  available: boolean;
  onEdit: () => void;
  onChange: (connection: ModelConnection) => void;
  onDelete: () => void;
}) {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [confirm, setConfirm] = useState<'delete' | 'disable' | null>(null);

  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError(false);
    try {
      await action();
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  async function confirmAction() {
    if (confirm === 'delete') {
      await deleteModelConnection(connection.id);
      onDelete();
    } else {
      onChange(await updateModelConnection(connection.id, { enabled: false }));
    }
    setConfirm(null);
  }

  return (
    <article className="min-w-0 rounded-xl border border-border p-4" aria-label={connection.name}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="break-words text-sm font-semibold">{connection.name}</h3>
          <p className="mt-1 break-all text-xs text-muted">{connection.base_url}</p>
          <p className="mt-1 text-xs text-muted">
            {t(connection.enabled ? 'connections.enabled' : 'connections.disabled')} ·{' '}
            {connection.runtimes.join(', ')}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className={buttonClass}
            onClick={onEdit}
            disabled={busy || !available}
          >
            {t('connections.edit')}
          </button>
          <button
            type="button"
            className={buttonClass}
            disabled={busy || (!available && !connection.enabled)}
            onClick={() => {
              if (connection.enabled) setConfirm('disable');
              else
                void run(async () =>
                  onChange(await updateModelConnection(connection.id, { enabled: true })),
                );
            }}
          >
            {t(connection.enabled ? 'connections.disable' : 'connections.enable')}
          </button>
          <button
            type="button"
            className={buttonClass}
            disabled={busy}
            onClick={() => setConfirm('delete')}
          >
            {t('connections.delete')}
          </button>
        </div>
      </div>
      <p className="mt-3 break-words text-xs text-muted">{connection.models.join(', ')}</p>
      {confirm ? (
        <div
          role="group"
          aria-label={t('connections.confirm')}
          className="mt-3 space-y-3 rounded-lg border border-border p-3"
        >
          <p className="text-sm">
            {t(confirm === 'delete' ? 'connections.confirmDelete' : 'connections.confirmDisable', {
              name: connection.name,
            })}
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              className={buttonClass}
              disabled={busy}
              onClick={() => void run(confirmAction)}
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
      <ModelConnectionTools
        connection={connection}
        available={available}
        busy={busy}
        run={run}
        onChange={onChange}
      />
      {error ? (
        <p role="alert" className="mt-3 text-sm text-status-error">
          {t('connections.error')}
        </p>
      ) : null}
    </article>
  );
}
