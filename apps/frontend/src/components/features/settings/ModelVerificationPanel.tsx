'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import {
  protocolName,
  unverifiedModels,
  VERIFY_BATCH,
  verifyModelConnection,
  type AgentPath,
  type ModelConnection,
  type ModelProtocol,
  type ModelStatus,
  type ModelVerification,
} from '@/lib/api/modelConnections';

const buttonClass = 'ui-button ui-button-secondary min-h-11';

const STATUS_TONE: Record<ModelStatus['status'], string> = {
  ready: 'text-status-success',
  declared: 'text-muted',
  unverified: 'text-muted',
  failed: 'text-status-warning',
  unsupported: 'text-status-error',
};

function resultKey(status: string | undefined) {
  if (status === 'supported') return 'connections.result.supported' as const;
  if (status === 'failed') return 'connections.result.failed' as const;
  return 'connections.result.unsupported' as const;
}

function pathText(path: AgentPath, t: ReturnType<typeof useT>): string {
  if (path.mode === 'direct') {
    return t('connections.pathDirect', {
      runtime: path.runtime,
      protocol: protocolName(path.upstream_protocol),
    });
  }
  return t('connections.pathTranslated', {
    runtime: path.runtime,
    client: protocolName(path.client_protocol),
    upstream: protocolName(path.upstream_protocol),
  });
}

/** One model's diagnostics: each API's native result and each agent's path. */
function Diagnostics({ status }: { status: ModelStatus }) {
  const t = useT();
  const results = Object.entries(status.protocols);
  if (!results.length && !status.paths.length) {
    return <p className="text-xs text-muted">{t('connections.diagnosticsNone')}</p>;
  }
  return (
    <ul className="space-y-0.5 text-xs leading-5 text-muted">
      {results.map(([protocol, result]) => (
        <li key={protocol}>
          {t(resultKey(result?.status), {
            protocol: protocolName(protocol as ModelProtocol),
            message: result?.message ?? '',
          })}
        </li>
      ))}
      {status.paths.map((path) => (
        <li key={path.runtime}>{pathText(path, t)}</li>
      ))}
    </ul>
  );
}

function ModelRow({
  status,
  verifying,
  canVerify,
  onVerify,
}: {
  status: ModelStatus;
  verifying: boolean;
  canVerify: boolean;
  onVerify: () => void;
}) {
  const t = useT();
  return (
    <li className="flex flex-wrap items-start justify-between gap-2 py-2">
      <div className="min-w-0">
        <p className="break-all text-sm font-medium">{status.model}</p>
        <p className={`text-xs ${STATUS_TONE[status.status]}`}>
          {verifying
            ? t('connections.verifying', { model: status.model })
            : t(`connections.status.${status.status}`)}
        </p>
        {status.runtimes.length ? (
          <p className="text-xs text-muted">
            {t('connections.agents', { agents: status.runtimes.join(', ') })}
          </p>
        ) : null}
        {status.status === 'unverified' ? (
          <p className="text-xs text-muted">{t('connections.unverifiedHint')}</p>
        ) : null}
        {status.message && !status.runtimes.length ? (
          <p className="text-xs text-muted">{status.message}</p>
        ) : null}
      </div>
      <button
        type="button"
        className={buttonClass}
        disabled={!canVerify}
        aria-label={t('connections.verifyModel', { model: status.model })}
        onClick={onVerify}
      >
        {t(status.status === 'unverified' ? 'connections.verify' : 'connections.verifyAgain')}
      </button>
    </li>
  );
}

/**
 * Each listed model, its state and the agents it serves, with explicit,
 * per-model verification. Verifying is the only thing here that calls the
 * provider, and a batch never covers more than `VERIFY_BATCH` models.
 */
export function ModelVerificationPanel({
  connection,
  available,
  autoVerify,
  onAutoVerifyStarted,
  onVerified,
  checksPerHour = 20,
}: {
  connection: ModelConnection;
  available: boolean;
  checksPerHour?: number;
  /** "Save and verify" asked for the unverified models to be verified now. */
  autoVerify: boolean;
  onAutoVerifyStarted: () => void;
  /**
   * A kept result, with the connection version it was started on. The page
   * merges it only into that version: an edit saved meanwhile is never
   * overwritten, nor marked by a result about its previous endpoint.
   */
  onVerified: (verification: ModelVerification, startedOn: string) => void;
}) {
  const t = useT();
  const [verifying, setVerifying] = useState<string | null>(null);
  const [notes, setNotes] = useState<string[]>([]);
  const [error, setError] = useState(false);
  const id = connection.id;
  const version = connection.updated_at;
  // The models still waiting, as the page holds them: a batch counts what remains.
  const remaining = useRef(unverifiedModels(connection));
  remaining.current = unverifiedModels(connection);
  // A batch ends with the card: leaving the page, or a saved edit (which
  // remounts it), sends no further provider requests and applies nothing late.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const verify = useCallback(
    async (models: string[]) => {
      setError(false);
      setNotes([]);
      const done = new Set<string>();
      for (const model of models) {
        if (!mounted.current) return;
        setVerifying(model);
        try {
          const result = await verifyModelConnection(id, model);
          if (!mounted.current) return;
          if (result.recorded) {
            done.add(model);
            onVerified(result, version);
          } else {
            setNotes((current) => [...current, t('connections.verifyStale', { model })]);
          }
        } catch {
          setError(true);
          break;
        }
      }
      setVerifying(null);
      const left = remaining.current.filter((model) => !done.has(model)).length;
      if (left)
        setNotes((current) => [...current, t('connections.verifyRemaining', { count: left })]);
    },
    [id, version, onVerified, t],
  );

  const pending = unverifiedModels(connection);
  const canVerify = available && connection.enabled && verifying === null;
  useEffect(() => {
    if (!autoVerify) return;
    onAutoVerifyStarted();
    if (canVerify && pending.length) void verify(pending.slice(0, VERIFY_BATCH));
    // Only when asked: a render never starts a verification by itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoVerify]);

  const ready = connection.model_status.filter((s) => s.runtimes.length).length;
  return (
    <div className="mt-3">
      <p className="text-xs text-muted">
        {connection.protocol_mode === 'manual'
          ? t('connections.manualSummary')
          : t('connections.summary', { ready, total: connection.models.length })}
      </p>
      <ul className="mt-1 divide-y divide-border">
        {connection.model_status.map((status) => (
          <ModelRow
            key={status.model}
            status={status}
            verifying={verifying === status.model}
            canVerify={canVerify}
            onVerify={() => void verify([status.model])}
          />
        ))}
      </ul>
      {pending.length > 1 ? (
        <button
          type="button"
          className={`${buttonClass} mt-2`}
          disabled={!canVerify}
          onClick={() => void verify(pending.slice(0, VERIFY_BATCH))}
        >
          {t('connections.verifyPending', { count: pending.length })}
        </button>
      ) : null}
      <p className="mt-2 text-xs leading-5 text-muted">
        {t('connections.verifyHint')}{' '}
        {t('connections.verifyBatchHint', { batch: VERIFY_BATCH, limit: checksPerHour })}
      </p>
      <div role="status" aria-live="polite">
        {notes.map((note) => (
          <p key={note} className="mt-1 text-sm text-status-warning">
            {note}
          </p>
        ))}
      </div>
      {error ? (
        <p role="alert" className="mt-2 text-sm text-status-error">
          {t('connections.error')}
        </p>
      ) : null}
      <details className="mt-2">
        <summary className="ui-focus-ring cursor-pointer rounded-md py-2 text-sm font-medium">
          {t('connections.diagnostics')}
        </summary>
        <p className="mb-2 text-xs leading-5 text-muted">{t('connections.diagnosticsHint')}</p>
        <div className="space-y-2">
          {connection.model_status.map((status) => (
            <div key={status.model}>
              <p className="break-all text-xs font-medium">{status.model}</p>
              <Diagnostics status={status} />
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}
