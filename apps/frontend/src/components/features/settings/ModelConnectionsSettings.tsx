'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import {
  listModelConnections,
  MAX_MODELS,
  type ModelConnection,
  type ModelConnectionList,
} from '@/lib/api/modelConnections';
import { ModelConnectionCard } from './ModelConnectionCard';
import { ModelConnectionForm } from './ModelConnectionForm';
import { ConnectionOps, type Operate, type Settlement } from './connectionOps';

export function ModelConnectionsSettings() {
  const t = useT();
  const [data, setData] = useState<ModelConnectionList | null>(null);
  const [failed, setFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [editing, setEditing] = useState<ModelConnection | 'new' | null>(null);
  // Answers about one connection apply only when nothing else touched it
  // meanwhile; otherwise the list is re-read once they all settle.
  const ops = useRef(new ConnectionOps());
  const operate = useCallback<Operate>(async (id, action) => {
    const ticket = ops.current.begin(id);
    let outcome: Settlement | undefined;
    try {
      const value = await action();
      outcome = ops.current.settle(ticket);
      return outcome === 'apply' ? value : undefined;
    } catch (cause) {
      outcome = ops.current.settle(ticket);
      throw cause;
    } finally {
      if (outcome === 'refresh') setReload((value) => value + 1);
    }
  }, []);

  useEffect(() => {
    let active = true;
    setFailed(false);
    void listModelConnections()
      .then((value) => {
        if (active) setData(value);
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    return () => {
      active = false;
    };
  }, [reload]);

  /**
   * A connection as the server answered it. Only a create adds a card: an
   * edit's answer replaces the card it is for, and never brings back one
   * deleted meanwhile.
   */
  function saved(connection: ModelConnection, { created = false } = {}) {
    setData((current) => {
      if (!current) return current;
      const exists = current.connections.some((item) => item.id === connection.id);
      if (!exists && !created) return current;
      const connections = exists
        ? current.connections.map((item) => (item.id === connection.id ? connection : item))
        : [...current.connections, connection];
      return { ...current, connections };
    });
  }

  const available = Boolean(data?.configured && data.enabled);
  const maxModels = data?.max_models ?? MAX_MODELS;
  return (
    <section
      aria-labelledby="settings-connections"
      className="ui-card mt-4 min-w-0 bg-surface p-4 sm:p-5"
    >
      <h2 id="settings-connections" className="text-base font-semibold">
        {t('connections.title')}
      </h2>
      <p className="mt-1.5 max-w-2xl text-sm leading-6 text-muted">
        {t('connections.description')}
      </p>
      {failed ? (
        <div role="alert" className="mt-3 flex flex-wrap items-center gap-3 text-sm">
          <span className="text-status-error">{t('connections.loadError')}</span>
          <button
            type="button"
            className="ui-button ui-button-secondary min-h-11"
            onClick={() => setReload((value) => value + 1)}
          >
            {t('common.retry')}
          </button>
        </div>
      ) : null}
      {!data && !failed ? <p className="mt-3 text-sm text-muted">{t('common.loading')}</p> : null}
      {data && !available ? (
        <p className="mt-3 text-sm text-muted">{t('connections.unavailable')}</p>
      ) : null}
      {available && !editing ? (
        <button
          type="button"
          className="ui-button ui-button-secondary mt-4 min-h-11"
          onClick={() => setEditing('new')}
        >
          {t('connections.add')}
        </button>
      ) : null}
      {editing ? (
        <ModelConnectionForm
          key={editing === 'new' ? 'new' : editing.id}
          connection={editing === 'new' ? undefined : editing}
          maxModels={maxModels}
          operate={operate}
          onSaved={(connection) => {
            if (connection) saved(connection, { created: editing === 'new' });
            setEditing(null);
          }}
          onSettledAfterLeaving={() => setReload((value) => value + 1)}
          onCancel={() => setEditing(null)}
        />
      ) : null}
      {data?.connections.length === 0 && !editing ? (
        <p className="mt-4 text-sm text-muted">{t('connections.empty')}</p>
      ) : null}
      <div className="mt-4 space-y-3">
        {data?.connections.map((connection) => (
          <ModelConnectionCard
            key={`${connection.id}:${connection.updated_at}`}
            connection={connection}
            available={available && !editing}
            maxModels={maxModels}
            operate={operate}
            onEdit={() => setEditing(connection)}
            onChange={(changed) => saved(changed)}
            onDelete={() => {
              setData((current) => {
                if (!current) return current;
                return {
                  ...current,
                  connections: current.connections.filter((item) => item.id !== connection.id),
                };
              });
              if (editing !== 'new' && editing?.id === connection.id) setEditing(null);
            }}
          />
        ))}
      </div>
    </section>
  );
}
