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
import { ChevronIcon } from './icons';
import { MODEL_CONNECTIONS_ANCHOR } from './navigation';
import { announceModelConnectionsChanged } from '../agents/composer/modelConnectionEvents';

/**
 * Whether the section's body is drawn. A folded section opens from its
 * heading, and arrives open when the address names it: that is how a link
 * meaning "set up your own connection" lands on the form's side of the fold.
 */
function useFold(folded: boolean) {
  const [open, setOpen] = useState(!folded);
  const section = useRef<HTMLElement>(null);
  useEffect(() => {
    if (window.location.hash !== MODEL_CONNECTIONS_ANCHOR) return;
    setOpen(true);
    section.current?.scrollIntoView?.({ block: 'start' });
  }, []);
  return { open, toggle: () => setOpen((current) => !current), section };
}

/** The connections themselves: read when drawn, so a folded section asks for nothing. */
function Connections() {
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
      // A failed response may still have committed; ask the server again.
      announceModelConnectionsChanged();
    }
  }, []);

  useEffect(() => {
    let active = true;
    const version = ops.current.version;
    setFailed(false);
    void listModelConnections()
      .then((value) => {
        if (!active) return;
        if (version !== ops.current.version) {
          setReload((current) => current + 1);
          return;
        }
        // A later mutation can be queued in the same React batch.
        setData((current) => (version === ops.current.version ? value : current));
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
    <>
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
    </>
  );
}

/**
 * The owner's own connections. `folded` is for a deployment that provides
 * models: the section then starts as its heading alone.
 */
export function ModelConnectionsSettings({ folded = false }: { folded?: boolean }) {
  const t = useT();
  const { open, toggle, section } = useFold(folded);
  return (
    <section
      ref={section}
      aria-labelledby="settings-connections"
      className="ui-card mt-4 min-w-0 scroll-mt-4 bg-surface p-4 sm:p-5"
    >
      <h2 id="settings-connections" className="text-base font-semibold">
        {folded ? (
          <button
            type="button"
            aria-expanded={open}
            onClick={toggle}
            className="ui-focus-ring -mx-2 -my-1.5 flex min-h-11 w-[calc(100%+1rem)] items-center gap-2 rounded-lg px-2 text-left hover:bg-surface-hover"
          >
            <ChevronIcon
              className={`h-4 w-4 shrink-0 text-muted transition-transform ${open ? 'rotate-90' : ''}`}
            />
            {t('connections.title')}
          </button>
        ) : (
          t('connections.title')
        )}
      </h2>
      {open ? (
        // Folded, the heading is a button that reaches past its own line.
        <div className={folded ? 'pt-2' : undefined}>
          <Connections />
        </div>
      ) : null}
    </section>
  );
}
