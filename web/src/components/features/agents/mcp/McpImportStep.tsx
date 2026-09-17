'use client';

import { useEffect, useId, useRef, useState } from 'react';
import {
  previewMcpImport,
  saveMcpImport,
  type McpImportDraftApi,
  type McpImportPreviewApi,
  type McpLocation,
  type McpConnectionApi,
} from '@/lib/api/mcp';

const FIELD =
  'w-full rounded-lg border border-gray-200 dark:border-white/10 bg-transparent px-2.5 py-2 text-sm text-gray-900 dark:text-gray-100';

/** One draft's stored fields, rendered for confirmation. */
function DraftBody({ draft }: { draft: McpImportDraftApi }) {
  return (
    <dl className="mt-2 space-y-1 text-xs text-gray-500 dark:text-gray-400">
      <div className="flex gap-2">
        <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">
          {draft.argv.length ? 'Command' : 'Address'}
        </dt>
        <dd className="break-all font-mono">
          {draft.argv.length ? draft.argv.join(' ') : (draft.url ?? 'none')}
        </dd>
      </div>
      {draft.env_names.length ? (
        <div className="flex gap-2">
          <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">Environment</dt>
          <dd className="break-all">{draft.env_names.join(', ')}</dd>
        </div>
      ) : null}
      {draft.headers.length ? (
        <div className="flex gap-2">
          <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">Headers</dt>
          <dd className="break-all">{draft.headers.join(', ')}</dd>
        </div>
      ) : null}
      {draft.secrets.map((secret) => (
        <div key={`${secret.target}:${secret.name}`} className="flex gap-2">
          <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">Secret</dt>
          <dd className="break-all font-mono">
            {secret.name} → {secret.target} = {secret.value}
          </dd>
        </div>
      ))}
      {draft.paths.length ? (
        <div className="flex gap-2">
          <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">Paths</dt>
          <dd className="break-all">{draft.paths.join(', ')}</dd>
        </div>
      ) : null}
      {draft.package ? (
        <div className="flex gap-2">
          <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">Package</dt>
          <dd>
            {draft.package.runner} · {draft.package.ecosystem}
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

const EXAMPLES = {
  remote: JSON.stringify(
    { mcpServers: { 'my-server': { url: 'https://mcp.example.com/mcp' } } },
    null,
    2,
  ),
  command: JSON.stringify(
    {
      mcpServers: {
        'my-server': {
          command: 'npx',
          args: ['-y', 'your-mcp-package'],
          env: { API_KEY: '${env:API_KEY}' },
        },
      },
    },
    null,
    2,
  ),
};

/** Validate a pasted config in place, then add exactly the servers shown. */
export function McpImportStep({
  onAdded,
  onBusyChange,
  initialText = '',
}: {
  onAdded: (connections: McpConnectionApi[]) => void;
  onBusyChange?: (busy: boolean) => void;
  initialText?: string;
}) {
  const inputId = useId();
  const submission = useRef(false);
  const [text, setText] = useState(initialText);
  const [preview, setPreview] = useState<McpImportPreviewApi | null>(null);
  const [locations, setLocations] = useState<Record<string, McpLocation>>({});
  const [secrets, setSecrets] = useState<Record<string, Record<string, string>>>({});
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!text.trim()) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      setChecking(true);
      void previewMcpImport(text)
        .then((result) => {
          if (cancelled) return;
          setPreview(result);
          setLocations(
            Object.fromEntries(result.drafts.map((draft) => [draft.label, draft.location])),
          );
        })
        .catch((cause: unknown) => {
          if (!cancelled)
            setError(cause instanceof Error ? cause.message : 'Could not read this configuration.');
        })
        .finally(() => {
          if (!cancelled) setChecking(false);
        });
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [text]);

  function edit(value: string) {
    if (value === text) return;
    setText(value);
    setPreview(null);
    setError(null);
    setChecking(false);
    setSecrets({});
  }

  const missingSecrets =
    preview?.drafts.flatMap((draft) =>
      draft.secrets
        .filter((secret) => secret.value === '(not provided)')
        .map((secret) => ({ server: draft.label, name: secret.name })),
    ) ?? [];

  async function save() {
    if (!preview?.drafts.length || submission.current) return;
    submission.current = true;
    setSaving(true);
    onBusyChange?.(true);
    setError(null);
    try {
      const result = await saveMcpImport(text, locations, {}, undefined, secrets);
      if (!result.saved.length) {
        setError(
          result.rejected.map((entry) => `${entry.name}: ${entry.reason}`).join(' ') ||
            'No servers were added.',
        );
        return;
      }
      onAdded(result.saved);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not save this configuration.');
    } finally {
      submission.current = false;
      setSaving(false);
      onBusyChange?.(false);
    }
  }

  return (
    <div>
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {initialText ? (
          'Review the configuration below. You can edit it before adding the server.'
        ) : (
          <>
            Paste your <code className="text-xs">mcpServers</code> configuration to connect your
            tools and data. You can add several servers at once.
          </>
        )}
      </p>
      <div className="mt-4 overflow-hidden rounded-xl border border-gray-200 dark:border-white/10">
        <div className="flex items-center justify-between gap-3 border-b border-gray-200 bg-gray-50 px-3 py-2 dark:border-white/10 dark:bg-white/5">
          <label htmlFor={inputId} className="font-mono text-xs text-gray-600 dark:text-gray-300">
            mcp.json
          </label>
          <div className="flex gap-3 text-xs text-gray-500 dark:text-gray-400">
            <button
              type="button"
              disabled={saving}
              onClick={() => edit(EXAMPLES.remote)}
              className="hover:text-gray-900 dark:hover:text-gray-100"
            >
              URL example
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => edit(EXAMPLES.command)}
              className="hover:text-gray-900 dark:hover:text-gray-100"
            >
              Command example
            </button>
          </div>
        </div>
        <textarea
          id={inputId}
          aria-label="MCP configuration"
          value={text}
          onChange={(event) => edit(event.target.value)}
          rows={11}
          placeholder={EXAMPLES.remote}
          disabled={saving}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          className="block w-full resize-y bg-transparent p-4 font-mono text-xs leading-6 text-gray-900 outline-none focus:ring-2 focus:ring-inset focus:ring-gray-400 dark:text-gray-100"
        />
      </div>
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        Supports JSON and TOML configurations. Adding servers keeps your existing connections.
      </p>
      {checking ? (
        <p role="status" className="mt-3 text-xs text-gray-500 dark:text-gray-400">
          Checking configuration…
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      ) : null}
      {preview ? (
        <div className="mt-4">
          <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
            {preview.drafts.length} {preview.drafts.length === 1 ? 'server' : 'servers'} found
          </p>
          {preview.drafts.map((draft) => (
            <div
              key={draft.label}
              className="mt-3 rounded-xl border border-gray-200 p-3 dark:border-white/10"
            >
              <p className="text-sm font-medium text-gray-900 dark:text-gray-100">{draft.label}</p>
              <DraftBody draft={draft} />
              {draft.transport === 'stdio' ? (
                <div className="mt-3">
                  <label className="block text-xs font-medium text-gray-600 dark:text-gray-400">
                    Run this command on
                    <select
                      aria-label={`Run location for ${draft.label}`}
                      value={locations[draft.label] ?? draft.location}
                      disabled={saving}
                      onChange={(event) =>
                        setLocations((current) => ({
                          ...current,
                          [draft.label]: event.target.value as McpLocation,
                        }))
                      }
                      className={`mt-1 ${FIELD}`}
                    >
                      <option value="user-connector">
                        Your computer (requires a connected MCP connector)
                      </option>
                      <option value="managed-worker">Cloud worker</option>
                    </select>
                  </label>
                  <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                    Commands run where you choose. Files and packages on your computer are not
                    automatically available in the cloud.
                  </p>
                </div>
              ) : null}
              {draft.warnings.map((warning) => (
                <p key={warning} className="mt-2 text-xs text-amber-800 dark:text-amber-300">
                  {warning}
                </p>
              ))}
            </div>
          ))}
          {missingSecrets.map(({ server, name }) => (
            <label
              key={JSON.stringify([server, name])}
              className="mt-3 block text-xs font-medium text-gray-700 dark:text-gray-300"
            >
              {server} · Value for {name}
              <input
                type="password"
                autoComplete="off"
                disabled={saving}
                value={secrets[server]?.[name] ?? ''}
                onChange={(event) =>
                  setSecrets((current) => ({
                    ...current,
                    [server]: { ...current[server], [name]: event.target.value },
                  }))
                }
                className={`mt-1 ${FIELD}`}
              />
            </label>
          ))}
          {preview.rejected.length ? (
            <div
              role="alert"
              className="mt-3 rounded-lg bg-amber-50 p-3 text-xs text-amber-800 dark:bg-amber-500/10 dark:text-amber-300"
            >
              <p className="font-medium">These entries will not be added</p>
              {preview.rejected.map((entry) => (
                <p key={entry.name} className="mt-1">
                  {entry.name}: {entry.reason}
                </p>
              ))}
            </div>
          ) : null}
          <button
            type="button"
            disabled={
              saving ||
              !preview.drafts.length ||
              missingSecrets.some(({ server, name }) => !secrets[server]?.[name]?.trim())
            }
            onClick={() => void save()}
            className="mt-4 rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900"
          >
            {saving
              ? 'Adding servers…'
              : `Add ${preview.drafts.length} ${preview.drafts.length === 1 ? 'server' : 'servers'}`}
          </button>
        </div>
      ) : null}
    </div>
  );
}
