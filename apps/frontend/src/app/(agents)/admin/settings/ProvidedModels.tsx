'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { announceModelConnectionsChanged } from '@/components/features/agents/composer/modelConnectionEvents';
import { useT } from '@/components/providers/LocaleProvider';
import {
  listOperatorModelConnections,
  type OperatorModelConnection,
  type OperatorModelConnectionList,
} from '@/lib/api/adminOperatorModels';

import { ProvidedConnectionCard } from './ProvidedConnectionCard';
import { ProvidedConnectionForm, type SavedConnection } from './ProvidedConnectionForm';
import { isAccessDenied, providedFailure } from './providedModelsForm';
import { PROVIDED_MODELS_TARGET } from './settingsSearchIndex';

/** `list` with one connection replaced, or added when it is new. */
function withConnection(
  list: OperatorModelConnectionList,
  connection: OperatorModelConnection,
): OperatorModelConnectionList {
  const exists = list.connections.some((item) => item.id === connection.id);
  // Only one connection is where new users start: the server moved the mark.
  const others = connection.is_default
    ? list.connections.map((item) => (item.is_default ? { ...item, is_default: false } : item))
    : list.connections;
  return {
    ...list,
    connections: exists
      ? others.map((item) => (item.id === connection.id ? connection : item))
      : [...others, connection],
  };
}

/**
 * What stands between a saved connection and anyone using it: model
 * connections switched off for the deployment, or no server that reaches the
 * model relay.
 */
function DeploymentNotices({ list }: { list: OperatorModelConnectionList }) {
  const t = useT();
  return (
    <>
      {list.feature_enabled ? null : (
        <p role="status" className="mt-4 rounded-xl bg-surface-muted p-3 text-sm leading-6">
          {t('adminModels.featureOff')}
        </p>
      )}
      {list.relay_hosts === 0 ? (
        <p
          role="alert"
          className="mt-4 rounded-xl border border-status-warning/40 p-3 text-sm leading-6 text-status-warning"
        >
          {t('adminModels.noRelayHosts')}
        </p>
      ) : null}
    </>
  );
}

/** What went wrong reading or changing the list; Retry is offered while there is no list. */
function LoadProblem({ error, onRetry }: { error: string; onRetry?: () => void }) {
  const t = useT();
  return (
    <div role="alert" className="mt-4 flex flex-wrap items-center gap-3 text-sm">
      <span className="text-status-error">{error}</span>
      {onRetry ? (
        <button type="button" className="ui-button ui-button-secondary min-h-11" onClick={onRetry}>
          {t('common.retry')}
        </button>
      ) : null}
    </div>
  );
}

/** The provided connections as the server holds them, and how the page changes its copy. */
function useProvidedConnections(onAccessDenied?: (cause: unknown) => void) {
  const t = useT();
  const [list, setList] = useState<OperatorModelConnectionList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const request = useRef(0);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      const current = ++request.current;
      const stale = () => Boolean(signal?.aborted) || current !== request.current;
      setLoading(true);
      try {
        const answer = await listOperatorModelConnections(signal);
        if (stale()) return;
        setList(answer);
        setError('');
      } catch (cause) {
        if (stale()) return;
        if (isAccessDenied(cause)) onAccessDenied?.(cause);
        setError(providedFailure(cause, t));
      } finally {
        if (!stale()) setLoading(false);
      }
    },
    [onAccessDenied, t],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  /** A connection as the server answered it; composers in this browser read it again. */
  const changed = useCallback((connection: OperatorModelConnection) => {
    setList((current) => (current ? withConnection(current, connection) : current));
    announceModelConnectionsChanged();
  }, []);

  const removed = useCallback((id: string) => {
    setList((current) =>
      current
        ? { ...current, connections: current.connections.filter((item) => item.id !== id) }
        : current,
    );
    announceModelConnectionsChanged();
  }, []);

  return { list, loading, error, load, changed, removed };
}

/**
 * Models this deployment provides: connections whose key stays on the control
 * plane and which every admitted account may use with no setup, up to a daily
 * number of requests per model. An administrator adds a connection (saved
 * disabled), gives every model a default allowance, then enables it.
 */
export default function ProvidedModelsSettings({
  onAccessDenied,
}: {
  onAccessDenied?: (cause: unknown) => void;
}) {
  const t = useT();
  const { list, loading, error, load, changed, removed } = useProvidedConnections(onAccessDenied);
  const [notice, setNotice] = useState('');
  const [editing, setEditing] = useState<string | null>(null);

  function saved(result: SavedConnection | null) {
    setEditing(null);
    setNotice(result?.problem ?? '');
    if (result) changed(result.connection);
  }

  /** Open the form for a new connection (`'new'`) or an existing one. */
  function edit(target: string) {
    setNotice('');
    setEditing(target);
  }

  const connections = list?.connections ?? [];
  const edited = connections.find((connection) => connection.id === editing);
  const formOpen = editing === 'new' || edited !== undefined;
  // Loaded, with no form open: a connection can be added, and an empty list says so.
  const idle = list !== null && editing === null;
  const featureOff = list?.feature_enabled === false;
  return (
    <section aria-labelledby={PROVIDED_MODELS_TARGET} className="ui-card mt-6 p-4 sm:p-6">
      <h2
        id={PROVIDED_MODELS_TARGET}
        tabIndex={-1}
        className="text-lg font-semibold text-foreground focus:outline-none"
      >
        {t('adminModels.title')}
      </h2>
      <p className="mt-1 max-w-3xl text-sm leading-6 text-muted">{t('adminModels.description')}</p>
      {list ? <DeploymentNotices list={list} /> : null}
      {error ? <LoadProblem error={error} onRetry={list ? undefined : () => void load()} /> : null}
      {notice ? (
        <p role="alert" className="mt-4 text-sm leading-6 text-status-warning">
          {notice}
        </p>
      ) : null}
      <div role="status" aria-live="polite">
        {loading && !list ? <p className="mt-4 text-sm text-muted">{t('common.loading')}</p> : null}
      </div>
      {idle ? (
        <button
          type="button"
          className="ui-button ui-button-secondary mt-4 min-h-11"
          // The server refuses a new connection while model connections are off.
          disabled={featureOff}
          onClick={() => edit('new')}
        >
          {t('adminModels.add')}
        </button>
      ) : null}
      {formOpen ? (
        <ProvidedConnectionForm
          key={editing ?? 'new'}
          connection={edited}
          onSaved={saved}
          onCancel={() => setEditing(null)}
          onAccessDenied={onAccessDenied}
        />
      ) : null}
      {idle && connections.length === 0 ? (
        <p className="mt-4 text-sm text-muted">{t('adminModels.empty')}</p>
      ) : null}
      <div className="mt-4 space-y-3">
        {connections.map((connection) => (
          <ProvidedConnectionCard
            key={connection.id}
            connection={connection}
            featureOff={featureOff}
            locked={editing !== null}
            onEdit={() => edit(connection.id)}
            onChange={changed}
            onDelete={() => removed(connection.id)}
            onAccessDenied={onAccessDenied}
          />
        ))}
      </div>
    </section>
  );
}

/**
 * The Models tab's panel. The section is mounted on the tab's first visit and
 * kept afterwards, so switching tabs asks nothing again and loses no edit.
 */
export function ProvidedModelsPanel({
  selected,
  onAccessDenied,
}: {
  selected: boolean;
  onAccessDenied?: (cause: unknown) => void;
}) {
  const [visited, setVisited] = useState(selected);
  useEffect(() => {
    if (selected) setVisited(true);
  }, [selected]);
  return (
    <div
      role="tabpanel"
      id="admin-settings-panel-models"
      aria-labelledby="admin-settings-tab-models"
      hidden={!selected}
      tabIndex={0}
    >
      {visited || selected ? <ProvidedModelsSettings onAccessDenied={onAccessDenied} /> : null}
    </div>
  );
}
